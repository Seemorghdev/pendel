"""Probe open recruiting-system feeds directly by guessing each company's board name.

For companies the page scan couldn't place, try a few name guesses ("mollie",
"redwood-software", ...) against each open system's public job feed. The first
board with jobs wins. Results are for review: a short name can collide with
another company's board, so board name, job count and sample locations are
recorded, and hits are graded:
  found        board has jobs, some in NL/DE or remote
  found_far    board has jobs, none near us (likely another company or entity)
  found_empty  board exists but has no jobs right now

Workable is not probed: its API answers for any name and rate-limits quickly.
Workable boards are still picked up by the page scan (pendel.detect).

Rows whose ATS is already known with a token just get their feed URL filled in.
Closed systems (Workday etc.) are left alone.

Polite: at least --delay seconds between requests to the same provider, honest
User-Agent, abort on HTTP 429 or on repeated network failures. These are
documented public job-board APIs, so robots.txt (meant for crawlers) is not
consulted here.

Usage:
  python -m pendel.probe companies/companies-located.csv companies/companies-probed.csv --limit 5
  python -m pendel.probe companies/companies-located.csv companies/companies-probed.csv --recheck Channable
"""
import argparse
import csv
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import urlparse

from pendel.detect import USER_AGENT, detect
from pendel.locate import CITIES, load_cities

TIMEOUT = 15
MAX_BYTES = 5_000_000
MAX_NETWORK_ERRORS_IN_ROW = 10
MAX_SLUGS = 3

CLOSED = {"workday", "successfactors", "taleo", "teamtailor", "homerun"}
NAME_STOP = {"netherlands", "nederland", "germany", "deutschland", "europe", "benelux", "group", "groep",
             "bv", "nv", "ag", "gmbh", "se", "inc", "the", "holding"}
HOST_STOP = {"www", "careers", "career", "jobs", "job", "karriere", "com", "nl", "de", "eu", "app", "io", "co",
             "org", "net", "global", "werkenbij", "werkenbijde", "werkenbijhet", "join", "work", "apply"}
ATS_HOSTS = {"lever", "greenhouse", "ashbyhq", "recruitee", "personio", "smartrecruiters", "workable",
             "myworkdayjobs", "successfactors", "teamtailor", "homerun", "taleo"}
NEAR_WORDS = {"remote", "emea", "europe"}


def norm(s):
    s = unicodedata.normalize("NFKD", s.lower())
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def near_us(locations, near_terms=None):
    """True when sample locations contain a caller-supplied target or generic remote marker."""
    terms = set(NEAR_WORDS)
    if near_terms:
        terms.update(norm(str(term)) for term in near_terms if str(term).strip())
    elif CITIES.exists():
        try:
            terms.update(load_cities())
        except (OSError, KeyError, ValueError):
            pass
    text = norm(" | ".join(locations))
    return any(w in text for w in terms)


def grade(count, locations, near_terms=None):
    if not count:
        return "found_empty"
    return "found" if near_us(locations, near_terms) else "found_far"


def slugs(name, careers_url=""):
    """A few likely board names, most specific first.

    If the careers URL already is a board (jobs.lever.co/appen), its token
    comes first; the recruiting system's own host name is never a guess.
    """
    out = []
    board = detect(careers_url) if careers_url else None
    if board and board[0] not in CLOSED:
        out.append(board[1])
    base = re.sub(r"\(.*?\)", " ", name).split("/")[0]
    words = [w for w in re.findall(r"[a-z0-9]+", norm(base)) if w not in NAME_STOP]
    if words:
        out += ["".join(words), "-".join(words), words[0]]
    host = urlparse(careers_url).hostname or ""
    parts = [p for p in host.split(".") if p not in HOST_STOP and p not in ATS_HOSTS]
    if parts:
        out.append(parts[0])
    seen, result = set(), []
    for s in out:
        if len(s) >= 3 and s not in seen:
            seen.add(s)
            result.append(s)
    return result[:MAX_SLUGS]


# ---- per-provider feed URL and response check -------------------------------
# Each check gets the response body and returns None (no board) or
# (job_count, board_name, [sample locations]).

def _json(body):
    try:
        return json.loads(body)
    except ValueError:
        return None


def check_greenhouse(body):
    d = _json(body)
    if not isinstance(d, dict) or not isinstance(d.get("jobs"), list):
        return None
    jobs = d["jobs"]
    return len(jobs), "", [((j.get("location") or {}).get("name") or "") for j in jobs]


def check_lever(body):
    d = _json(body)
    if not isinstance(d, list):
        return None
    return len(d), "", [((j.get("categories") or {}).get("location") or "") for j in d]


def check_ashby(body):
    d = _json(body)
    if not isinstance(d, dict) or not isinstance(d.get("jobs"), list):
        return None
    jobs = d["jobs"]
    return len(jobs), "", [str(j.get("location") or "") for j in jobs]


def check_recruitee(body):
    d = _json(body)
    if not isinstance(d, dict) or not isinstance(d.get("offers"), list):
        return None
    offers = d["offers"]
    name = offers[0].get("company_name", "") if offers else ""
    return len(offers), name, [o.get("city") or o.get("location") or "" for o in offers]


def check_personio(body):
    if "<workzag-jobs" not in body:
        return None
    offices = re.findall(r"<office>(.*?)</office>", body)
    return body.count("<position>"), "", offices


def check_smartrecruiters(body):
    d = _json(body)
    # This API answers 200 with zero results for any name, so only a non-empty board counts.
    if not isinstance(d, dict) or not d.get("totalFound"):
        return None
    content = d.get("content") or []
    name = ((content[0].get("company") or {}).get("name") or "") if content else ""
    return d["totalFound"], name, [((p.get("location") or {}).get("city") or "") for p in content]


def check_workable(body):
    d = _json(body)
    if not isinstance(d, dict) or "jobs" not in d:
        return None
    jobs = d.get("jobs") or []
    return len(jobs), d.get("name", ""), [j.get("city") or j.get("country") or "" for j in jobs]


# (provider key, ats name, feed template, check, subdomain-based)
PROVIDERS = [
    ("greenhouse", "greenhouse", "https://boards-api.greenhouse.io/v1/boards/{t}/jobs", check_greenhouse, False),
    ("lever", "lever", "https://api.lever.co/v0/postings/{t}?mode=json", check_lever, False),
    ("lever_eu", "lever", "https://api.eu.lever.co/v0/postings/{t}?mode=json", check_lever, False),
    ("ashby", "ashby", "https://api.ashbyhq.com/posting-api/job-board/{t}", check_ashby, False),
    ("recruitee", "recruitee", "https://{t}.recruitee.com/api/offers/", check_recruitee, True),
    ("personio_de", "personio", "https://{t}.jobs.personio.de/xml", check_personio, True),
    ("personio_com", "personio", "https://{t}.jobs.personio.com/xml", check_personio, True),
    ("smartrecruiters", "smartrecruiters", "https://api.smartrecruiters.com/v1/companies/{t}/postings",
     check_smartrecruiters, False),
]
FEED_FOR_ATS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
    "lever": "https://api.lever.co/v0/postings/{t}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{t}",
    "recruitee": "https://{t}.recruitee.com/api/offers/",
    "personio": "https://{t}.jobs.personio.de/xml",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{t}/postings",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{t}",
}


class RateLimited(Exception):
    pass


class ProviderThrottle:
    """At least `delay` seconds between two requests to the same provider."""

    def __init__(self, delay):
        self.delay = delay
        self.last = {}

    def wait(self, key):
        gap = time.monotonic() - self.last.get(key, 0.0)
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self.last[key] = time.monotonic()


def fetch(url, key, throttle):
    """Return the body for a 200, None for 404/410/400 (no such board). Raises on other errors."""
    throttle.wait(key)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, text/xml"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            return resp.read(MAX_BYTES).decode(resp.headers.get_content_charset() or "utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RateLimited(url)
        if e.code in (400, 404, 410):
            return None
        raise


def probe_company(name, careers_url, throttle, near_terms=None):
    """Return (hit, requests_made). hit = dict with probe_status, or None.

    A board with jobs near us ends the search. Otherwise the best fallback is
    kept: a board with jobs elsewhere (found_far), then an empty board.
    """
    made = 0
    rank = {"found_far": 1, "found_empty": 2}
    fallback = None
    for slug in slugs(name, careers_url):
        for key, ats, tmpl, check, subdomain in PROVIDERS:
            url = tmpl.format(t=slug)
            made += 1
            try:
                body = fetch(url, key, throttle)
            except urllib.error.URLError:
                if subdomain:      # unknown subdomain: DNS miss, just no board
                    continue
                raise
            if body is None:
                continue
            result = check(body)
            if result is None:
                continue
            count, board, locs = result
            hit = {"ats": ats, "ats_token": slug, "feed_url": url, "probe_jobs": str(count),
                   "probe_board_name": board, "probe_locations": " | ".join(l for l in locs[:3] if l),
                   "probe_status": grade(count, locs, near_terms)}
            if hit["probe_status"] == "found":
                return hit, made
            if fallback is None or rank[hit["probe_status"]] < rank[fallback["probe_status"]]:
                fallback = hit
    return fallback, made


def run(args):
    near_terms = None
    cities_path = Path(args.cities) if args.cities else None
    if cities_path and cities_path.exists():
        near_terms = set(load_cities(cities_path))
    elif cities_path:
        raise FileNotFoundError(f"target city file not found: {cities_path}")
    with open(args.input, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)
    extra = ["probe_status", "probe_jobs", "probe_board_name", "probe_locations", "probed_at"]
    out_fields = fieldnames + [c for c in extra if c not in fieldnames]
    recheck = {norm(n) for n in args.recheck}

    try:
        out = open(args.output, "x", newline="", encoding="utf-8")
    except FileExistsError:
        sys.exit(f"error: {args.output} already exists. Pick a new output file name.")

    throttle = ProviderThrottle(args.delay)
    statuses, found = Counter(), Counter()
    probed = requests = errors_in_row = 0
    aborted = None

    with out:
        writer = csv.DictWriter(out, fieldnames=out_fields)
        writer.writeheader()
        for row in rows:
            for c in out_fields:
                row.setdefault(c, "")
            name = row.get("company", "")
            ats = (row.get("ats") or "").strip().lower()
            token = (row.get("ats_token") or "").strip()

            if aborted or (args.limit and probed >= args.limit):
                row["probe_status"] = "not_run"
            elif ats in CLOSED:
                row["probe_status"] = "closed"
            elif ats in FEED_FOR_ATS and token and norm(name) not in recheck:
                if not row.get("feed_url"):
                    row["feed_url"] = FEED_FOR_ATS[ats].format(t=token)
                    row["probe_status"] = "filled_known"
                else:
                    row["probe_status"] = "has_feed"
            else:
                probed += 1
                row["probed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                try:
                    hit, made = probe_company(name, row.get("careers_url", ""), throttle, near_terms)
                    requests += made
                    errors_in_row = 0
                    if hit:
                        row.update(hit)
                        found[f"{hit['ats']}:{hit['probe_status']}"] += 1
                    else:
                        row["probe_status"] = "none"
                except RateLimited as e:
                    aborted = f"HTTP 429 at {e}"
                    row["probe_status"] = "rate_limited"
                except (urllib.error.URLError, TimeoutError, OSError) as e:
                    row["probe_status"] = "error"
                    row["probe_board_name"] = str(getattr(e, "reason", e))[:120]
                    errors_in_row += 1
                    if errors_in_row >= MAX_NETWORK_ERRORS_IN_ROW:
                        aborted = f"{errors_in_row} companies in a row failed with network errors"
                print(f"[{probed:>3}] {name[:38]:<38} {row['probe_status']:<12} "
                      f"{row['ats'] if row['probe_status'].startswith('found') else ''} {row['probe_jobs']}", flush=True)
            statuses[row["probe_status"]] += 1
            writer.writerow(row)
            out.flush()

    print("\nSUMMARY")
    print(f"rows: {len(rows)}  probed: {probed}  requests: {requests}")
    print("status: " + ", ".join(f"{k}={v}" for k, v in statuses.most_common()))
    print("found by ats: " + (", ".join(f"{k}={v}" for k, v in found.most_common()) or "none"))
    print(f"output: {args.output}")
    if aborted:
        print(f"ABORTED: {aborted}. Remaining rows are marked not_run.")
        return 2
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("input")
    ap.add_argument("output", help="new output csv (must not exist)")
    ap.add_argument("--limit", type=int, default=0, help="probe only the first N companies")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds between requests to one provider (min 2)")
    ap.add_argument("--recheck", nargs="*", default=[], help="company names to probe even if a token is known")
    ap.add_argument("--cities", help="optional private cities.csv used only to grade sample locations")
    args = ap.parse_args()
    args.delay = max(args.delay, 2.0)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
