"""Fetch every accepted feed once and store the jobs in DuckDB.

Each job is keyed by (company, job_id). A job seen for the first time gets
first_seen = today; a job seen again only gets last_seen updated. A job is
open if its last_seen equals the date of the latest successful fetch for its
company. Re-running on the same day adds nothing new.

Polite: at least --delay seconds between requests to the same host, honest
User-Agent, one listing request per company (SmartRecruiters pages through
100 at a time). On HTTP 429 that provider is skipped for the rest of the run.

Usage:
  python -m pendel.fetch companies/feeds.csv --db data/pendel.duckdb --limit 3
  python -m pendel.fetch companies/feeds.csv --db data/pendel.duckdb
  python -m pendel.fetch companies/feeds.csv --db data/pendel.duckdb --review check
"""
import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import duckdb

from pendel import readers
from pendel.detect import USER_AGENT

TIMEOUT = 30
MAX_BYTES = 40_000_000
SR_PAGE = 100
SR_MAX_PAGES = 20
TEAMTAILOR_PAGE = 100
TEAMTAILOR_MAX_PAGES = 20
TRAKSTAR_PAGE = 250
TRAKSTAR_MAX_PAGES = 20

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    company     TEXT NOT NULL,
    ats         TEXT,
    job_id      TEXT NOT NULL,
    title       TEXT,
    location    TEXT,
    country     TEXT,
    work_mode   TEXT,
    url         TEXT,
    posted_at   TEXT,
    salary      TEXT,
    lang_hint   TEXT,
    description TEXT,
    first_seen  DATE NOT NULL,
    last_seen   DATE NOT NULL,
    PRIMARY KEY (company, job_id)
);
CREATE TABLE IF NOT EXISTS fetch_log (
    run_at   TIMESTAMP,
    company  TEXT,
    ats      TEXT,
    status   TEXT,
    jobs     INTEGER,
    new_jobs INTEGER,
    error    TEXT
);
"""

UPSERT = """
INSERT INTO jobs (company, ats, job_id, title, location, country, work_mode, url, posted_at,
                  salary, lang_hint, description, first_seen, last_seen)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (company, job_id) DO UPDATE SET
    title = excluded.title, location = excluded.location, country = excluded.country,
    work_mode = excluded.work_mode, url = excluded.url, salary = excluded.salary,
    lang_hint = excluded.lang_hint, description = excluded.description,
    last_seen = excluded.last_seen
"""
FIELDS = ["job_id", "title", "location", "country", "work_mode", "url", "posted_at",
          "salary", "lang_hint", "description"]


class RateLimited(Exception):
    pass


class IncompleteFeed(Exception):
    """Provider pagination was capped; partial results must not reset a baseline."""
    def __init__(self, message, jobs=None):
        super().__init__(message)
        self.jobs = jobs or []


class HostThrottle:
    def __init__(self, delay):
        self.delay = delay
        self.last = {}

    def wait(self, url):
        host = urlparse(url).hostname or ""
        # all *.recruitee.com / *.personio.de boards share one provider
        key = ".".join(host.split(".")[-2:])
        gap = time.monotonic() - self.last.get(key, 0.0)
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self.last[key] = time.monotonic()


def get(url, throttle):
    throttle.wait(url)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json, text/xml"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read(MAX_BYTES).decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            retry_after = e.headers.get("Retry-After", "") if e.headers else ""
            raise RateLimited(f"{url} (Retry-After: {retry_after or 'unspecified'})") from e
        if 500 <= e.code < 600:
            time.sleep(2)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read(MAX_BYTES).decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
        raise


def with_query(url, query):
    parts = urlsplit(url)
    additions = parse_qsl(query, keep_blank_values=True)
    replaced = {key.casefold() for key, _ in additions}
    params = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
              if key.casefold() not in replaced]
    params.extend(additions)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params), parts.fragment))


def fetch_jobs(feed, throttle):
    """Return the parsed job list for one feeds.csv row."""
    ats, url, token = feed["ats"], feed["feed_url"], feed["token"]
    if ats == "greenhouse":
        return readers.parse_greenhouse(json.loads(get(with_query(url, "content=true"), throttle)))
    if ats == "lever":
        return readers.parse_lever(json.loads(get(url, throttle)))
    if ats == "ashby":
        return readers.parse_ashby(json.loads(get(with_query(url, "includeCompensation=true"), throttle)))
    if ats == "recruitee":
        return readers.parse_recruitee(json.loads(get(url, throttle)))
    if ats == "personio":
        tld = "com" if ".personio.com" in url else "de"
        return readers.parse_personio(get(url, throttle), token, tld)
    if ats == "softgarden":
        return readers.parse_softgarden(json.loads(get(url, throttle)))
    if ats == "teamtailor":
        out = []
        offset = 0
        for page in range(TEAMTAILOR_MAX_PAGES):
            page_url = with_query(url, f"offset={offset}&per_page={TEAMTAILOR_PAGE}")
            jobs = readers.parse_teamtailor_rss(get(page_url, throttle))
            out.extend(jobs)
            if len(jobs) < TEAMTAILOR_PAGE:
                break
            offset += len(jobs)
        else:
            raise IncompleteFeed(
                f"Teamtailor RSS pagination capped at {len(out)} postings", out,
            )
        return out
    if ats == "recruiterbox":
        out = []
        total_found = None
        offset = 0
        for page in range(TRAKSTAR_MAX_PAGES):
            page_url = with_query(
                url, f"client_name={token}&limit={TRAKSTAR_PAGE}&offset={offset}",
            )
            data = json.loads(get(page_url, throttle))
            jobs = readers.parse_trakstar_frontend(data)
            out.extend(jobs)
            raw_openings = data.get("openings", []) if isinstance(data, dict) else []
            raw_count = len(raw_openings) if isinstance(raw_openings, list) else len(jobs)
            offset += raw_count
            meta = data.get("meta") if isinstance(data, dict) else {}
            if isinstance(meta, dict):
                raw_total = meta.get("total_count", meta.get("total"))
                try:
                    total_found = int(raw_total) if raw_total is not None else None
                except (TypeError, ValueError):
                    total_found = None
            if total_found is not None:
                if len(out) >= total_found:
                    break
            elif len(jobs) < TRAKSTAR_PAGE:
                break
        else:
            raise IncompleteFeed(
                f"Recruiterbox pagination capped at {len(out)} postings", out,
            )
        if total_found is not None and len(out) < total_found:
            raise IncompleteFeed(
                f"Recruiterbox pagination returned {len(out)} of {total_found} postings", out,
            )
        return out
    if ats == "pinpoint":
        return readers.parse_pinpoint(json.loads(get(url, throttle)))
    if ats == "smartrecruiters":
        out = []
        total_found = None
        for page in range(SR_MAX_PAGES):
            data = json.loads(get(with_query(url, f"limit={SR_PAGE}&offset={page * SR_PAGE}"), throttle))
            out += readers.parse_smartrecruiters(data, token)
            total_found = int(data.get("totalFound") or 0)
            if (page + 1) * SR_PAGE >= total_found:
                break
        if total_found is not None and len(out) < total_found:
            raise IncompleteFeed(f"SmartRecruiters pagination capped at {len(out)} of {total_found} postings", out)
        return out
    raise ValueError(f"no reader for ats '{ats}'")


def connect(db_path):
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute(SCHEMA)
    return con


def store(con, company, ats, jobs, today):
    """Upsert jobs; return number of jobs new to the database."""
    jobs = [j for j in jobs if j["job_id"]]
    if not jobs:
        return 0
    known = {r[0] for r in con.execute("SELECT job_id FROM jobs WHERE company = ?", [company]).fetchall()}
    seen = set()
    rows = []
    for j in jobs:
        if j["job_id"] in seen:          # some feeds list a job twice
            continue
        seen.add(j["job_id"])
        rows.append([company, ats] + [j[f] for f in FIELDS] + [today, today])
    con.executemany(UPSERT, rows)
    return len(seen - known)


def load_feeds(path, review):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f) if r["review"] == review and r["feed_url"]]


def run(args):
    feeds = load_feeds(args.feeds, args.review)
    if args.company:
        feeds = [f for f in feeds if f["company"] in args.company]
    if args.limit:
        feeds = feeds[:args.limit]
    con = connect(args.db)
    today = datetime.now(timezone.utc).date()
    run_at = datetime.now(timezone.utc).replace(tzinfo=None)
    throttle = HostThrottle(args.delay)
    blocked = set()
    statuses = Counter()
    total = new_total = 0

    for i, feed in enumerate(feeds, 1):
        name, ats = feed["company"], feed["ats"]
        status, n, new, err = "ok", 0, 0, ""
        if ats in blocked:
            status = "skipped_rate_limited"
        else:
            try:
                jobs = fetch_jobs(feed, throttle)
                n = len(jobs)
                new = store(con, name, ats, jobs, today)
            except RateLimited as e:
                status, err = "rate_limited", str(e)
                blocked.add(ats)
            except IncompleteFeed as e:
                status, err = "partial", str(e)[:200]
            except urllib.error.HTTPError as e:
                status, err = f"http_{e.code}", e.reason if isinstance(e.reason, str) else ""
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                status, err = "network_error", str(getattr(e, "reason", e))[:200]
            except (ValueError, KeyError, TypeError, AttributeError) as e:
                status, err = "parse_error", f"{type(e).__name__}: {e}"[:200]
        con.execute("INSERT INTO fetch_log VALUES (?, ?, ?, ?, ?, ?, ?)", [run_at, name, ats, status, n, new, err])
        statuses[status] += 1
        total += n
        new_total += new
        print(f"[{i:>3}] {name[:34]:<34} {ats:<15} {status:<20} jobs={n:<4} new={new}", flush=True)

    open_jobs = con.execute("SELECT count(*) FROM jobs WHERE last_seen = ?", [today]).fetchone()[0]
    all_jobs = con.execute("SELECT count(*) FROM jobs").fetchone()[0]
    con.close()
    print("\nSUMMARY")
    print(f"feeds: {len(feeds)}  jobs fetched: {total}  new today: {new_total}")
    print("status: " + ", ".join(f"{k}={v}" for k, v in statuses.most_common()))
    print(f"database: {args.db}  open jobs: {open_jobs}  all jobs ever: {all_jobs}")
    if blocked:
        print("RATE LIMITED: " + ", ".join(sorted(blocked)))
        return 2
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("feeds", help="companies/feeds.csv")
    ap.add_argument("--db", default="data/pendel.duckdb")
    ap.add_argument("--review", default="accepted", help="which review status to fetch (accepted|check)")
    ap.add_argument("--company", nargs="*", help="only these company names")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between requests to one host (min 2)")
    args = ap.parse_args()
    args.delay = max(args.delay, 2.0)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
