"""Detect each company's recruiting system (ATS) from its careers page.

Reads companies.csv, fetches each careers page once, recognises known ATS links
in the HTML (and in the final URL after redirects), and writes a NEW csv with
ats, ats_token and feed_url filled in. It never overwrites an existing file.

Polite by design: robots.txt is respected, one page request per company,
at least --delay seconds between requests, honest User-Agent, and the run
skips a company whose site answers HTTP 429, and aborts on 429s from several sites in
a row or on repeated network failures.

Standard library only.

Usage:
  python -m pendel.detect companies/companies.csv companies/companies-ats.csv --limit 3
  python -m pendel.detect companies/companies.csv companies/companies-ats.csv
"""
import argparse
import csv
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.robotparser
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

USER_AGENT = "pendel/0.1 (personal job-search tool, one request per company; +https://github.com/Seemorghdev/pendel)"
MAX_BYTES = 3_000_000
TIMEOUT = 20
MAX_NETWORK_ERRORS_IN_ROW = 5
MAX_RATE_LIMITED_IN_ROW = 3

UNKNOWN_ATS = {"", "unknown", "?", "none", "n/a", "na"}
URL_COLUMNS = ["careers_url", "careers_page", "career_url", "careers", "jobs_url", "url", "website"]
NAME_COLUMNS = ["company", "name", "company_name"]

# Path segments / subdomains that look like tokens but are not company boards.
BAD_TOKENS = {
    "", "www", "app", "api", "apply", "jobs", "careers", "embed", "v0", "v1", "j",
    "static", "assets", "cdn", "help", "support", "en", "de", "nl", "en-us",
    "js", "css", "img", "images", "privacy", "terms", "login", "signup",
    "careers-analytics",  # Recruitee tracking script, present on many sites
}

# (ats, kind, regex, feed template). kind "open" = public JSON/XML feed,
# kind "closed" = record only (watch by email alert or by hand).
# Each regex has a named group "t" for the board token; feed templates
# may also use any other named group from the same regex.
PATTERNS = [
    ("greenhouse", "open", r"greenhouse\.io/embed/job_board(?:/js)?\?for=(?P<t>[A-Za-z0-9_-]+)",
     "https://boards-api.greenhouse.io/v1/boards/{t}/jobs"),
    ("greenhouse", "open", r"boards-api\.greenhouse\.io/v1/boards/(?P<t>[A-Za-z0-9_-]+)",
     "https://boards-api.greenhouse.io/v1/boards/{t}/jobs"),
    ("greenhouse", "open", r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?P<t>[A-Za-z0-9_-]+)",
     "https://boards-api.greenhouse.io/v1/boards/{t}/jobs"),
    ("lever", "open", r"jobs\.(?P<region>eu\.)?lever\.co/(?P<t>[A-Za-z0-9_.-]+)",
     "https://api.{region}lever.co/v0/postings/{t}?mode=json"),
    ("ashby", "open", r"jobs\.ashbyhq\.com/(?P<t>[A-Za-z0-9_.%-]+)",
     "https://api.ashbyhq.com/posting-api/job-board/{t}"),
    ("recruitee", "open", r"(?P<t>[a-z0-9-]+)\.recruitee\.com",
     "https://{t}.recruitee.com/api/offers/"),
    ("personio", "open", r"(?P<t>[a-z0-9-]+)\.jobs\.personio\.(?P<tld>de|com)",
     "https://{t}.jobs.personio.{tld}/xml"),
    ("smartrecruiters", "open", r"(?:jobs|careers)\.smartrecruiters\.com/(?P<t>[A-Za-z0-9_-]+)",
     "https://api.smartrecruiters.com/v1/companies/{t}/postings"),
    ("smartrecruiters", "open", r"api\.smartrecruiters\.com/v1/companies/(?P<t>[A-Za-z0-9_-]+)",
     "https://api.smartrecruiters.com/v1/companies/{t}/postings"),
    ("workable", "open", r"apply\.workable\.com/(?P<t>[A-Za-z0-9_-]+)",
     "https://apply.workable.com/api/v1/widget/accounts/{t}"),
    ("workable", "open", r"(?P<t>[a-z0-9-]+)\.workable\.com",
     "https://apply.workable.com/api/v1/widget/accounts/{t}"),
    # Seen often in NL; recorded, feed left empty until their terms are checked.
    ("teamtailor", "closed", r"(?P<t>[a-z0-9-]+)\.teamtailor\.com", ""),
    ("homerun", "closed", r"(?P<t>[a-z0-9-]+)\.homerun\.co", ""),
    # Closed systems: record only.
    ("workday", "closed",
     r"(?P<t>[a-z0-9_-]+\.wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?(?P<site>[A-Za-z0-9_-]+)", ""),
    ("successfactors", "closed", r"career\d*\.successfactors\.(?:com|eu)/[^\"'\s]*company=(?P<t>[A-Za-z0-9_-]+)", ""),
    ("successfactors", "closed", r"(?P<t>[a-z0-9-]+)\.jobs2web\.com", ""),
    ("taleo", "closed", r"(?P<t>[a-z0-9-]+)\.taleo\.net", ""),
]
COMPILED = [(ats, kind, re.compile(rx, re.IGNORECASE), feed) for ats, kind, rx, feed in PATTERNS]


def detect(text):
    """Return (ats, token, feed_url, note) for the best match in text, or None."""
    hits = Counter()          # ats -> number of matches
    tokens = {}               # ats -> Counter of tokens
    examples = {}             # (ats, token) -> (feed template, groupdict)
    for ats, kind, rx, feed in COMPILED:
        for m in rx.finditer(text):
            g = {k: (v or "") for k, v in m.groupdict().items()}
            tok = g["t"]
            if tok.lower() in BAD_TOKENS:
                continue
            if ats == "workday":
                tok = f"{tok}/{g['site']}"
            hits[ats] += 1
            tokens.setdefault(ats, Counter())[tok] += 1
            examples.setdefault((ats, tok), (feed, g))
    if not hits:
        return None
    ats = hits.most_common(1)[0][0]
    tok = tokens[ats].most_common(1)[0][0]
    feed, g = examples[(ats, tok)]
    feed_url = feed.format(**g) if feed else ""
    notes = []
    if len(hits) > 1:
        notes.append("also seen: " + ", ".join(a for a in hits if a != ats))
    if len(tokens[ats]) > 1:
        notes.append("other tokens: " + ", ".join(t for t in tokens[ats] if t != tok))
    if ats == "greenhouse" and ".eu.greenhouse.io" in text.lower():
        notes.append("EU Greenhouse board: verify feed in JW-3")
    if not feed:
        notes.append("record only")
    return ats, tok, feed_url, "; ".join(notes)


class Throttle:
    def __init__(self, delay):
        self.delay = delay
        self.last = 0.0

    def wait(self):
        gap = time.monotonic() - self.last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self.last = time.monotonic()


class RateLimited(Exception):
    pass


def http_get(url, throttle):
    throttle.wait()
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read(MAX_BYTES)
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.geturl(), raw.decode(charset, errors="replace")


_robots_cache = {}


def allowed_by_robots(url, throttle):
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots_cache:
        rp = urllib.robotparser.RobotFileParser()
        try:
            _, body = http_get(base + "/robots.txt", throttle)
            rp.parse(body.splitlines())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                raise RateLimited(base + "/robots.txt")
            # 401/403 means "no access"; anything else (404 etc.) means no rules.
            rp.parse(["User-agent: *", "Disallow: /"] if e.code in (401, 403) else [])
        except (urllib.error.URLError, TimeoutError, OSError):
            rp.parse([])
        _robots_cache[base] = rp
    return _robots_cache[base].can_fetch(USER_AGENT, url)


def pick_column(fieldnames, wanted, override, label):
    if override:
        if override not in fieldnames:
            sys.exit(f"error: --{label}-col '{override}' not in header: {fieldnames}")
        return override
    lower = {f.lower().strip(): f for f in fieldnames}
    for w in wanted:
        if w in lower:
            return lower[w]
    if label == "name":
        return None
    sys.exit(f"error: no {label} column found. Header is: {fieldnames}. Pass --{label}-col.")


def run(args):
    with open(args.input, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    url_col = pick_column(fieldnames, URL_COLUMNS, args.url_col, "url")
    name_col = pick_column(fieldnames, NAME_COLUMNS, args.name_col, "name")

    out_fields = fieldnames + [c for c in ["ats", "ats_token", "feed_url", "detect_status",
                                            "detect_note", "final_url", "checked_at"]
                               if c not in fieldnames]
    throttle = Throttle(args.delay)
    statuses, found = Counter(), Counter()
    net_errors_in_row = limited_in_row = 0
    done = 0

    # "x" mode: fails if the output already exists, so nothing is overwritten.
    try:
        out = open(args.output, "x", newline="", encoding="utf-8")
    except FileExistsError:
        sys.exit(f"error: {args.output} already exists. Pick a new output file name.")

    aborted = None
    with out:
        writer = csv.DictWriter(out, fieldnames=out_fields)
        writer.writeheader()
        for i, row in enumerate(rows):
            for c in out_fields:
                row.setdefault(c, "")
            name = row.get(name_col, "") if name_col else f"row {i + 2}"
            url = (row.get(url_col) or "").strip()

            if aborted or (args.limit and done >= args.limit):
                row["detect_status"] = row["detect_status"] or "not_run"
                writer.writerow(row)
                continue
            if (row.get("ats") or "").strip().lower() not in UNKNOWN_ATS and not args.all:
                row["detect_status"] = "skipped_known"
                statuses["skipped_known"] += 1
                writer.writerow(row)
                continue
            if not url.startswith(("http://", "https://")):
                row["detect_status"] = "no_url"
                statuses["no_url"] += 1
                writer.writerow(row)
                continue

            done += 1
            row["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            status = note = ""
            try:
                # The URL itself may already be an ATS board.
                hit = detect(url)
                if hit is None:
                    if not allowed_by_robots(url, throttle):
                        status = "robots_disallow"
                    else:
                        final_url, html = http_get(url, throttle)
                        row["final_url"] = final_url if final_url != url else ""
                        hit = detect(final_url + "\n" + html)
                        if hit is None:
                            status = "not_found"
                            note = "no known ATS link; page may load jobs with JavaScript: check by hand"
                net_errors_in_row = limited_in_row = 0
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    status, note = "rate_limited", "site asked us to slow down; try again another day"
                    limited_in_row += 1
                else:
                    status = f"http_{e.code}"
                    limited_in_row = 0
                net_errors_in_row = 0
            except RateLimited as e:
                status, note = "rate_limited", f"429 at {e}; try again another day"
                limited_in_row += 1
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                status = "network_error"
                note = str(getattr(e, "reason", e))[:200]
                net_errors_in_row += 1
                if net_errors_in_row >= MAX_NETWORK_ERRORS_IN_ROW:
                    aborted = f"{net_errors_in_row} network errors in a row (connection down?)"
            # Each company has its own site, so one 429 only skips that company.
            # Several in a row means something shared is limiting us: stop.
            if limited_in_row >= MAX_RATE_LIMITED_IN_ROW:
                aborted = f"HTTP 429 from {limited_in_row} sites in a row"

            if hit:
                row["ats"], row["ats_token"], row["feed_url"], note = hit
                status = "found"
                found[row["ats"]] += 1
            row["detect_status"], row["detect_note"] = status, note
            statuses[status] += 1
            writer.writerow(row)
            out.flush()
            print(f"[{done:>3}] {name[:40]:<40} {status:<16} {row['ats']} {row['ats_token']}", flush=True)

    print("\nSUMMARY")
    print(f"rows in input: {len(rows)}  checked this run: {done}")
    print("status: " + ", ".join(f"{k}={v}" for k, v in statuses.most_common()))
    print("ats found: " + (", ".join(f"{k}={v}" for k, v in found.most_common()) or "none"))
    print(f"output: {args.output}")
    if aborted:
        print(f"ABORTED: {aborted}. Rows after this point are marked not_run.")
        return 2
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input", nargs="?", help="companies.csv")
    ap.add_argument("output", nargs="?", help="new output csv (must not exist)")
    ap.add_argument("--limit", type=int, default=0, help="check only the first N companies")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between requests (min 2)")
    ap.add_argument("--all", action="store_true", help="also re-check rows whose ats is already known")
    ap.add_argument("--url-col", help="name of the careers URL column, if not auto-detected")
    ap.add_argument("--name-col", help="name of the company name column, if not auto-detected")
    args = ap.parse_args()
    if not args.input or not args.output:
        ap.error("input and output are required")
    args.delay = max(args.delay, 2.0)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
