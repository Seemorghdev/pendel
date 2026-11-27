"""S15: check candidate job feeds before they join the watch list.

The searching agent lists candidate companies (job system + board token or feed URL).
This fetches each feed once, politely, runs every job through the normal matcher and
reports how many jobs are in allowed countries and how many are Tier 1/2 hits there.
Feeds worth watching are written as ready-to-append watch-source rows.

It never writes state, never sends anything and never opens single job pages.

Usage:
  python -m pendel.discover candidates.csv --config config/targets.json \
      --existing config/sources.csv --out discovery/
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from pendel import fetch
from pendel.targets import evaluate_job, load_config
from pendel.watch import SOURCE_FIELDS, _endpoint_key, load_sources

# Public board endpoints by job system. Only used when a candidate gives no feed_url;
# the agent checks each template against an existing source of the same system first.
FEED_TEMPLATES = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
    "lever": "https://api.lever.co/v0/postings/{token}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{token}",
    "recruitee": "https://{token}.recruitee.com/api/offers/",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{token}/postings",
    "personio": "https://{token}.jobs.personio.de/xml",
    "teamtailor": "https://{token}.teamtailor.com/jobs.rss",
    "pinpoint": "https://{token}.pinpointhq.com/postings.json",
}
REPORT_FIELDS = ["decision", "ats", "token", "companies", "feed_url", "status", "error", "jobs",
                 "jobs_in_allowed_countries", "tier12_hits", "tier12_matches", "examples", "note"]
MIN_ALLOWED_JOBS = 5


def feed_url(row: dict) -> str:
    url = (row.get("feed_url") or "").strip()
    if url:
        return url
    template = FEED_TEMPLATES.get((row.get("ats") or "").strip().casefold())
    token = (row.get("token") or "").strip()
    return template.format(token=token) if template and token else ""


def score(jobs: list[dict], company: str, config: dict) -> dict:
    """Counts for one feed: jobs in allowed countries, and Tier 1/2 hits among them."""
    rules = config.get("countries") or {}
    allowed = {str(x).upper() for x in rules.get("allowed", ["DE"])} | \
              {str(x).upper() for x in rules.get("remote_allowed", ["DE"])}
    in_allowed = hits = matches = 0
    examples: list[str] = []
    for job in jobs:
        tagged = evaluate_job(dict(job, company=company), config)
        if tagged["country"] in allowed:
            in_allowed += 1
        tier12 = [m for m in tagged["group_tier_matches"] if m["tier"] in ("1", "2")]
        if tier12:
            hits += 1
            matches += tagged["status"] == "match"
            if len(examples) < 3:
                examples.append(f"{job.get('title', '')} · {job.get('location', '')}")
    return {"jobs": len(jobs), "jobs_in_allowed_countries": in_allowed, "tier12_hits": hits,
            "tier12_matches": matches, "examples": " | ".join(examples)}


def decide(counts: dict) -> str:
    if counts["tier12_hits"]:
        return "keep"
    if counts["jobs_in_allowed_countries"] >= MIN_ALLOWED_JOBS:
        return "maybe"
    return "drop"


def run(candidates_path: Path, config_path: Path, existing_path: Path | None, out_dir: Path,
        delay: float = 2.0) -> int:
    config = load_config(config_path)
    known = {row["source_id"] for row in load_sources(existing_path)} if existing_path else set()
    throttle = fetch.HostThrottle(max(2.0, delay))
    report, keep_rows = [], []
    with candidates_path.open(newline="", encoding="utf-8-sig") as handle:
        candidates = list(csv.DictReader(handle))
    for row in candidates:
        ats = (row.get("ats") or "").strip().casefold()
        token = (row.get("token") or "").strip()
        url = feed_url(row)
        company = (row.get("companies") or token).strip()
        line = {"ats": ats, "token": token, "companies": company, "feed_url": url,
                "note": row.get("note", ""), "status": "ok", "error": ""}
        if not url:
            report.append(line | {"decision": "drop", "status": "no_feed_url"})
            continue
        if _endpoint_key(ats, url, token) in known:
            report.append(line | {"decision": "drop", "status": "already_watched"})
            continue
        try:
            jobs = fetch.fetch_jobs({"ats": ats, "feed_url": url, "token": token}, throttle)
        except fetch.IncompleteFeed as exc:
            jobs, line["status"], line["error"] = exc.jobs, "partial", str(exc)[:200]
        except Exception as exc:  # one bad candidate never stops the others
            report.append(line | {"decision": "drop", "status": "error",
                                  "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            continue
        counts = score(jobs, company, config)
        decision = decide(counts)
        report.append(line | counts | {"decision": decision})
        if decision == "keep":
            known.add(_endpoint_key(ats, url, token))
            keep_rows.append({"source_id": "", "ats": ats, "token": token, "feed_url": url,
                              "career_url": row.get("career_url", ""), "companies": company,
                              "source_groups": "12-western-europe", "source_tiers": "1; 2",
                              "source_collections": "s15-europe"})
    out_dir.mkdir(parents=True, exist_ok=True)
    order = {"keep": 0, "maybe": 1, "drop": 2}
    report.sort(key=lambda r: (order[r["decision"]], -int(r.get("tier12_hits") or 0)))
    with (out_dir / "discovery-report.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=REPORT_FIELDS, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(report)
    with (out_dir / "sources-to-add.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=SOURCE_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(keep_rows)
    totals = {d: sum(r["decision"] == d for r in report) for d in order}
    print(f"candidates {len(report)}: keep {totals['keep']}, maybe {totals['maybe']}, drop {totals['drop']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("candidates", type=Path)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--existing", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--delay", type=float, default=2.0)
    args = parser.parse_args(argv)
    return run(args.candidates, args.config, args.existing, args.out, args.delay)


if __name__ == "__main__":
    sys.exit(main())
