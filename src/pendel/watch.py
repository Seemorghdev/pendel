"""Collect public ATS feeds, apply private rules, and maintain a durable outbox.

Typical private-runner phases:
  prepare -> persist state branch -> claim -> persist state branch -> deliver -> persist.

The state JSON is a compact logical export stored outside this public package.
Each invocation restores it into a fresh DuckDB file before processing.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import duckdb

from pendel import fetch
from pendel.targets import evaluate_job, load_config

STATE_VERSION = 1
STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS watch_jobs (
  identity_key VARCHAR PRIMARY KEY, first_seen TIMESTAMPTZ, last_seen TIMESTAMPTZ,
  source_ids_json VARCHAR
);
CREATE TABLE IF NOT EXISTS watch_sources (
  source_id VARCHAR PRIMARY KEY, baseline_complete BOOLEAN, last_success TIMESTAMPTZ,
  last_status VARCHAR, last_error VARCHAR
);
CREATE TABLE IF NOT EXISTS watch_outbox (
  outbox_id VARCHAR PRIMARY KEY, status VARCHAR, created_at TIMESTAMPTZ,
  updated_at TIMESTAMPTZ, attempts INTEGER, payload_json VARCHAR, error VARCHAR
);
"""

SOURCE_FIELDS = ["source_id", "ats", "token", "feed_url", "career_url", "companies",
                 "source_groups", "source_tiers", "source_collections"]
REPORT_FIELDS = ["status", "groups", "tiers", "company", "title", "location", "work_mode", "country",
                 "language_requirement_status", "german_level", "advert_language", "hours_status",
                 "hours_per_week", "commute_status", "commute_note", "posted_at", "first_seen", "url", "reasons", "source_ids"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utc_now()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _endpoint_key(ats: str, url: str, token: str = "") -> str:
    p = urlsplit(url.strip())
    tenant = token.strip().casefold()
    if tenant:
        return f"{ats.casefold()}|{tenant}"
    # Query parameters on a feed URL often control payload options or language;
    # they do not identify a different employer tenant.
    normalized = urlunsplit((p.scheme.casefold(), p.netloc.casefold(), p.path.rstrip("/") or "/", "", ""))
    return f"{ats.casefold()}|{normalized}"


def _feed_variant(url: str) -> str:
    """Canonicalize fetch-only options while preserving content variants such as language."""
    p = urlsplit(url.strip())
    ignored = {"content", "includecompensation", "limit", "offset", "per_page"}
    query = [(key, value) for key, value in parse_qsl(p.query, keep_blank_values=True)
             if key.casefold() not in ignored]
    return urlunsplit((p.scheme.casefold(), p.netloc.casefold(), p.path.rstrip("/") or "/",
                       urlencode(sorted(query)), ""))


def load_sources(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    merged: dict[str, dict[str, str]] = {}
    for row in rows:
        ats, url = (row.get("ats") or "").strip().casefold(), (row.get("feed_url") or "").strip()
        if not ats or not url:
            continue
        token = (row.get("token") or "").strip()
        key = _endpoint_key(ats, url, token)
        if key not in merged:
            merged[key] = {field: (row.get(field) or "").strip() for field in SOURCE_FIELDS}
            merged[key]["source_id"] = key
            merged[key]["feed_urls"] = [_feed_variant(url)]
        else:
            prior = merged[key]
            if not prior.get("career_url") and (row.get("career_url") or "").strip():
                prior["career_url"] = row["career_url"].strip()
            variant = _feed_variant(url)
            if variant not in prior["feed_urls"]:
                prior["feed_urls"].append(variant)
                prior["feed_urls"].sort()
            for field in ("companies", "source_groups", "source_tiers", "source_collections"):
                values = {x.strip() for x in (prior.get(field, "") + ";" + (row.get(field) or "")).split(";") if x.strip()}
                prior[field] = "; ".join(sorted(values))
    return [merged[k] for k in sorted(merged)]


def identity_key(source: dict[str, str], item: dict) -> str:
    external_id = str(item.get("job_id") or "").strip()
    tenant = (source.get("token") or "").strip().casefold()
    if not tenant:
        p = urlsplit(source.get("feed_url", ""))
        tenant = f"{p.netloc.casefold()}{p.path.rstrip('/')}"
    if external_id:
        return f"id:{source['ats'].casefold()}|{tenant}|{external_id}"
    url = (item.get("url") or "").strip()
    if url:
        p = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                 if k.casefold() not in {"utm_source", "utm_medium", "utm_campaign", "gh_src"}]
        canonical = urlunsplit((p.scheme.casefold(), p.netloc.casefold(), p.path.rstrip("/"),
                                urlencode(sorted(query)), ""))
        return "url:" + canonical
    material = f"{source['ats']}|{tenant}|" + json.dumps(item, sort_keys=True, default=str)
    return "fallback:" + hashlib.sha256(material.encode()).hexdigest()


def _state_empty() -> dict:
    return {"schema_version": STATE_VERSION, "jobs": {}, "sources": {}, "outbox": []}


def load_state(path: str | Path, bootstrap: bool = False) -> dict:
    p = Path(path)
    if not p.exists():
        if bootstrap:
            return _state_empty()
        raise FileNotFoundError(f"persisted state is missing: {p}; use --bootstrap only for an intentional first run")
    with p.open(encoding="utf-8") as handle:
        state = json.load(handle)
    if state.get("schema_version") != STATE_VERSION:
        raise ValueError(f"unsupported state schema version: {state.get('schema_version')}")
    state.setdefault("jobs", {})
    state.setdefault("sources", {})
    state.setdefault("outbox", [])
    return state


def write_state(path: str | Path, state: dict, db_path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db))
    con.execute(STATE_SCHEMA)
    con.execute("DELETE FROM watch_jobs")
    con.execute("DELETE FROM watch_sources")
    con.execute("DELETE FROM watch_outbox")
    for key, value in state["jobs"].items():
        con.execute("INSERT INTO watch_jobs VALUES (?, ?, ?, ?)",
                    [key, value.get("first_seen"), value.get("last_seen"), json.dumps(value.get("source_ids", []))])
    for key, value in state["sources"].items():
        con.execute("INSERT INTO watch_sources VALUES (?, ?, ?, ?, ?)",
                    [key, bool(value.get("baseline_complete")), value.get("last_success"), value.get("last_status", ""), value.get("last_error", "")])
    for value in state["outbox"]:
        con.execute("INSERT INTO watch_outbox VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [value["outbox_id"], value["status"], value["created_at"], value.get("updated_at"),
                     int(value.get("attempts", 0)), json.dumps(value.get("payload", {}), ensure_ascii=False), value.get("error", "")])
    con.close()
    raw = json.dumps(state, ensure_ascii=False, separators=(",", ":")) + "\n"
    fd, temp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, p)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Runner:
    def __init__(self, config: dict, state: dict, delay: float = 2.0):
        self.config = config
        self.state = state
        self.throttle = fetch.HostThrottle(max(2.0, delay))

    def collect(self, sources: list[dict[str, str]]) -> tuple[dict[str, dict], list[dict]]:
        current: dict[str, dict] = {}
        statuses: list[dict] = []
        blocked_hosts: set[str] = set()
        for source in sources:
            urls = source.get("feed_urls") or [source["feed_url"]]
            host = ".".join((urlsplit(urls[0]).hostname or "").split(".")[-2:])
            status, error, jobs, complete = "ok", "", [], True
            if host in blocked_hosts:
                status, error, complete = "skipped_after_rate_limit", "provider host rate-limited earlier in this run", False
            else:
                all_variant_jobs: list[dict] = []
                for variant_url in urls:
                    try:
                        all_variant_jobs.extend(fetch.fetch_jobs(source | {"feed_url": variant_url}, self.throttle))
                    except fetch.RateLimited as exc:
                        status, error, complete = "rate_limited", str(exc), False
                        blocked_hosts.add(host)
                        break
                    except fetch.IncompleteFeed as exc:
                        status, error, complete = "partial", str(exc), False
                    except urllib.error.HTTPError as exc:
                        status, error, complete = f"http_{exc.code}", str(exc.reason)[:200], False
                        if exc.code == 429:
                            blocked_hosts.add(host)
                            break
                    except (urllib.error.URLError, TimeoutError, OSError) as exc:
                        status, error, complete = "network_error", str(getattr(exc, "reason", exc))[:200], False
                    except (ValueError, KeyError, TypeError, AttributeError) as exc:
                        status, error, complete = "parse_error", f"{type(exc).__name__}: {exc}"[:200], False
                jobs = all_variant_jobs
            if status == "ok" and not jobs:
                status = "empty"
            statuses.append({"source_id": source["source_id"], "companies": source["companies"],
                             "ats": source["ats"], "status": status, "error": error,
                             "job_count": len(jobs), "complete": complete})
            if not complete:
                continue
            for item in jobs:
                ident = identity_key(source, item)
                value = {
                    "identity_key": ident, "job_id": str(item.get("job_id") or ""), "company": source["companies"],
                    "title": str(item.get("title") or ""), "location": str(item.get("location") or ""),
                    "country": str(item.get("country") or ""), "work_mode": str(item.get("work_mode") or ""),
                    "url": str(item.get("url") or ""), "posted_at": str(item.get("posted_at") or ""),
                    "salary": str(item.get("salary") or ""), "lang_hint": str(item.get("lang_hint") or ""),
                    "description": str(item.get("description") or ""), "source_ids": [source["source_id"]],
                }
                tagged = evaluate_job(value, self.config)
                value["match"] = tagged
                if ident in current:
                    old = current[ident]
                    old["source_ids"] = sorted(set(old["source_ids"] + value["source_ids"]))
                    if len(value["description"]) > len(old["description"]):
                        value["source_ids"] = old["source_ids"]
                        current[ident] = value
                else:
                    current[ident] = value
        return current, statuses

    def prepare(self, sources: list[dict[str, str]], output_dir: str | Path) -> int:
        now = iso()
        current, statuses = self.collect(sources)
        new_items: list[dict] = []
        baseline_count = 0
        by_status = Counter()
        # Persist the set of currently degraded feeds so scheduled runs can
        # report transitions once, then stay quiet until the set changes or
        # all feeds recover.
        prior_health = self.state.get("feed_health_signature", [])
        degraded = sorted([s["source_id"], s["status"]] for s in statuses if not s["complete"])
        if degraded != prior_health:
            if degraded:
                detail_by_id = {s["source_id"]: s for s in statuses}
                lines = ["Pendel feed health notice — some public feeds need attention."]
                for source_id, status_name in degraded[:12]:
                    entry = detail_by_id[source_id]
                    lines.append(f"• {entry['companies']} ({entry['ats']}): {status_name}")
                if len(degraded) > 12:
                    lines.append(f"• Plus {len(degraded) - 12} other affected feeds; see the private run report.")
            elif prior_health:
                lines = ["Pendel feed health recovered — all monitored public feeds completed successfully."]
            else:
                lines = []
            if lines:
                message = "\n".join(lines)
                transition_key = json.dumps([prior_health, degraded, now], ensure_ascii=False)
                notice_id = "health:" + hashlib.sha256(transition_key.encode()).hexdigest()[:20]
                self.state["outbox"].append({
                    "outbox_id": notice_id, "status": "pending", "created_at": now,
                    "updated_at": now, "attempts": 0, "error": "",
                    "chunk_statuses": ["pending"],
                    "payload": {"items": [], "chunks": [message]},
                })
            self.state["feed_health_signature"] = degraded
        baseline_before = {key: bool(value.get("baseline_complete"))
                           for key, value in self.state["sources"].items()}
        for status in statuses:
            src_state = self.state["sources"].setdefault(status["source_id"], {"baseline_complete": False})
            baseline_before.setdefault(status["source_id"], bool(src_state.get("baseline_complete")))
            src_state["last_status"] = status["status"]
            src_state["last_error"] = status["error"]
            by_status[status["status"]] += 1
            if status["complete"]:
                src_state["last_success"] = now
                src_state["last_error"] = ""
                src_state["baseline_complete"] = True
            # A complete empty result is a valid success and completes baseline.

        for ident, value in current.items():
            seen = self.state["jobs"].get(ident)
            if seen is None:
                seen = {"first_seen": now, "last_seen": now, "source_ids": value["source_ids"]}
                self.state["jobs"][ident] = seen
                first_observation = any(baseline_before.get(sid, False) for sid in value["source_ids"])
                if not first_observation:
                    baseline_count += 1
            else:
                seen["last_seen"] = now
                seen["source_ids"] = sorted(set(seen.get("source_ids", []) + value["source_ids"]))
                first_observation = False
            value["first_seen"] = seen["first_seen"]
            value["status"] = value["match"]["status"]
            if value["status"] in ("match", "review"):
                value["groups"] = "; ".join(sorted({x["group_id"] for x in value["match"]["group_tier_matches"]}))
                value["tiers"] = "; ".join(sorted({x["tier"] for x in value["match"]["group_tier_matches"]}))
            # Tier-matched jobs with unresolved location/work mode are still
            # actionable review alerts. Known exclusions never reach this path.
            if first_observation and value["match"]["status"] in {"match", "review"} \
                    and value["match"]["tier_candidates"]:
                new_items.append(value)

        if new_items:
            outbox_id = "digest:" + hashlib.sha256((now + json.dumps([x["identity_key"] for x in new_items])).encode()).hexdigest()[:20]
            payload = {"items": [self._digest_item(x) for x in new_items]}
            payload["chunks"] = self._telegram_chunks(payload)
            self.state["outbox"].append({"outbox_id": outbox_id, "status": "pending", "created_at": now,
                                          "updated_at": now, "attempts": 0, "error": "",
                                          "chunk_statuses": ["pending"] * len(payload["chunks"]),
                                          "payload": payload})

        reports = {"match": [], "review": [], "exclude": []}
        for value in current.values():
            tagged = value["match"]
            group_ids = {x["group_id"] for x in tagged["group_tier_matches"]}
            group_ids.update(group for group, _ in tagged["matched_city_groups"])
            if tagged["work_mode"] == "remote" and tagged["country"] == "DE":
                group_ids.add(self.config.get("remote_group_id", "09-remote-germany"))
            value["groups"] = "; ".join(sorted(group_ids))
            value["tiers"] = "; ".join(sorted(set(tagged["tier_candidates"]), key=str))
            reports[tagged["status"]].append(self._report_row(value))
        output = Path(output_dir)
        output.mkdir(parents=True, exist_ok=True)
        for label, rows in reports.items():
            self._write_csv(output / f"{label}.csv", rows)
        self._write_csv(output / "source-status.csv", statuses, list(statuses[0]) if statuses else
                        ["source_id", "companies", "ats", "status", "error", "job_count", "complete"])
        summary = {
            "run_at": now, "sources": len(sources), "source_status": dict(by_status),
            "incomplete_sources": sum(1 for status in statuses if not status["complete"]),
            "current_unique_listings": len(current), "first_seen": len(new_items),
            "silent_baseline_listings": baseline_count,
            "current_decisions": {key: len(rows) for key, rows in reports.items()},
            "telegram_outbox_pending": sum(1 for item in self.state["outbox"] if item["status"] == "pending"),
            "delivery_enabled": False,
        }
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (output / "SUMMARY.md").write_text(self._markdown_summary(summary, reports), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        # Feed-level failures are represented in source-status.csv, the
        # transition notice, and the summary. Returning success lets the
        # workflow persist and deliver those notices while keeping incomplete
        # sources out of both baseline and first-seen accounting.
        return 0

    @staticmethod
    def _digest_item(value: dict) -> dict:
        tagged = value["match"]
        return {"identity_key": value["identity_key"], "title": value["title"], "company": value["company"],
                "url": value["url"], "status": tagged["status"],
                "groups": sorted({x["group_id"] for x in tagged["group_tier_matches"]}),
                "tiers": sorted(set(tagged["tier_candidates"])),
                "reasons": tagged["reasons"]}

    @staticmethod
    def _report_row(value: dict) -> dict:
        tagged = value["match"]
        return {"status": tagged["status"], "groups": value.get("groups", ""), "tiers": value.get("tiers", ""),
                "company": value["company"], "title": value["title"], "location": value["location"],
                "work_mode": tagged["work_mode"], "country": tagged["country"],
                "language_requirement_status": tagged["language_requirement_status"],
                "german_level": tagged["german_level"], "advert_language": tagged["advert_language"],
                "hours_status": tagged["hours_status"], "hours_per_week": tagged["hours_per_week"] or "",
                "commute_status": "remote" if tagged["work_mode"] == "remote" else "route_review",
                "commute_note": "Remote role" if tagged["work_mode"] == "remote" else
                                "City-level group only; verify public-transport route to the exact worksite",
                "posted_at": value["posted_at"], "first_seen": value["first_seen"], "url": value["url"],
                "reasons": "; ".join(tagged["reasons"]), "source_ids": "; ".join(value["source_ids"])}

    @staticmethod
    def _write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
        fields = fields or REPORT_FIELDS
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _markdown_summary(summary: dict, reports: dict) -> str:
        lines = [f"# Watch run — {summary['run_at']}", "", "## Decisions", "",
                 f"- Match: {len(reports['match'])}", f"- Review: {len(reports['review'])}",
                 f"- Excluded: {len(reports['exclude'])}", "",
                 "Unclear language, work mode, location, or Tier 3 hours appear in `review.csv`.",
                 "Descriptions and explicit language requirements are assessed separately.",
                 "A city-group match is not a confirmed commute; verify public transport to the exact worksite.", "",
                 "## Feed health", "", f"- Sources: {summary['sources']}",
                 f"- Results: {json.dumps(summary['source_status'], sort_keys=True)}", "",
                 "## First-seen baseline", "", f"- First observed in this run: {summary['first_seen']}",
                 f"- Quiet initial baseline: {summary['silent_baseline_listings']}",
                 "- Failed or partial feeds do not complete a baseline or mark listings closed."]
        if summary.get("incomplete_sources"):
            lines.extend([f"- Incomplete sources: {summary['incomplete_sources']}; first-seen counts include completed sources only."])
        lines.extend(["",
                 "## Notifications", "", "- Delivery is disabled in this run.",
                 f"- Pending outbox messages: {summary['telegram_outbox_pending']}", ""])
        return "\n".join(lines)

    def claim(self) -> int:
        if os.environ.get("PENDEL_TELEGRAM_APPROVED") != "true":
            print("Telegram delivery has not been approved; no outbox item was claimed.")
            return 0
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            print("Telegram secrets are not configured; no outbox item was claimed.")
            return 0
        now = iso()
        # A persisted 'sending' entry means an earlier attempt may have reached Telegram.
        for item in self.state["outbox"]:
            for index, status in enumerate(item.get("chunk_statuses", [])):
                if status == "sending":
                    item["chunk_statuses"][index] = "ambiguous"
                    item["status"] = "ambiguous"
                    item["updated_at"] = now
                    item["error"] = f"chunk {index + 1} may have reached Telegram; it will not be retried automatically"
        if any(item.get("status") in {"ambiguous", "failed_manual"} for item in self.state["outbox"]):
            print("An earlier Telegram delivery needs manual review; no later digest was claimed.")
            return 0
        claimed = None
        for item in self.state["outbox"]:
            if item["status"] != "pending":
                continue
            statuses = item.setdefault("chunk_statuses", ["pending"] * len(item.get("payload", {}).get("chunks", [])))
            index = next((i for i, status in enumerate(statuses) if status == "pending"), None)
            if index is not None:
                item["chunk_statuses"][index] = "sending"
                item["chunk_index"] = index
                item["attempts"] = int(item.get("attempts", 0)) + 1
                item["updated_at"] = now
                item["status"] = "sending"
                claimed = item
                break
        if claimed is None:
            print("No pending digest to claim.")
            return 0
        print(f"Claimed digest chunk {claimed['chunk_index'] + 1}; outbox id {claimed['outbox_id']}.")
        return 0

    def deliver(self, allow_send: bool) -> int:
        if not allow_send or os.environ.get("PENDEL_TELEGRAM_APPROVED") != "true":
            print("Delivery not authorized; no Telegram request made.")
            return 0
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            print("Telegram secrets are not configured; no Telegram request made.")
            return 0
        item = next((x for x in self.state["outbox"]
                     if x.get("chunk_index") is not None and
                     x.get("chunk_statuses", [])[x["chunk_index"]] == "sending"), None)
        if item is None:
            print("No claimed digest to deliver.")
            return 0
        index = item["chunk_index"]
        body = item["payload"]["chunks"][index]
        if len(body) > 4096:
            item["status"] = "failed_manual"
            item["chunk_statuses"][index] = "failed_manual"
            item["error"] = "message exceeds Telegram's limit; review required"
            item["updated_at"] = iso()
            print("Claimed Telegram chunk exceeds the message limit; no request made.")
            return 1
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        request = urllib.request.Request(url, data=json.dumps({"chat_id": chat_id, "text": body,
                               "disable_web_page_preview": True}).encode(),
                               headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not result.get("ok"):
                item["chunk_statuses"][index] = "failed_manual"
                item["status"] = "failed_manual"
                item["error"] = "Telegram returned a non-success result; review before retry"
            else:
                item["chunk_statuses"][index] = "sent"
                item["status"] = "sent" if all(s == "sent" for s in item["chunk_statuses"]) else "pending"
                item["error"] = ""
        except (TimeoutError, urllib.error.URLError, OSError):
            item["chunk_statuses"][index] = "ambiguous"
            item["status"] = "ambiguous"
            item["error"] = "delivery outcome uncertain; manual review required; no automatic retry"
        except (urllib.error.HTTPError, ValueError):
            item["chunk_statuses"][index] = "failed_manual"
            item["status"] = "failed_manual"
            item["error"] = "Telegram rejected the request; review before retry"
        item["updated_at"] = iso()
        print(f"Telegram digest state recorded as {item['status']}.")
        return 0

    @staticmethod
    def _telegram_chunks(payload: dict, max_chars: int = 3900) -> list[str]:
        items = payload.get("items", [])
        matches = [x for x in items if x.get("status") == "match"]
        reviews = [x for x in items if x.get("status") == "review"]
        header = f"New job watch — {len(matches)} matches, {len(reviews)} to review."
        chunks: list[str] = []
        current = header
        current_section = ""
        for section, group in (("Matches", matches), ("Needs review", reviews)):
            for job in group:
                group_names = ", ".join(job.get("groups", []))[:300]
                tiers = ", ".join("Tier " + x for x in job.get("tiers", []))[:100]
                title = str(job.get("title", ""))[:350]
                company = str(job.get("company", ""))[:250]
                entry_lines = [f"• {title} — {company} ({group_names}; {tiers})"]
                if section == "Needs review" and job.get("reasons"):
                    entry_lines.append("Review: " + "; ".join(job["reasons"])[:350])
                if job.get("url"):
                    entry_lines.append(str(job["url"])[:1000])
                entry = "\n".join(entry_lines)
                prefix = "\n\n" + section + "\n" if current_section != section else "\n"
                if len(current) + len(prefix) + len(entry) > max_chars and current != header:
                    chunks.append(current)
                    current = header + "\n(continued)"
                    current_section = ""
                    prefix = "\n\n" + section + "\n"
                if current_section != section:
                    current += prefix
                    current_section = section
                else:
                    current += prefix
                current += entry
        if current != header:
            chunks.append(current)
        return chunks or [header]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=("prepare", "claim", "deliver"))
    parser.add_argument("--sources", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--output-dir", default="watch-report")
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--allow-send", action="store_true", help="requires PENDEL_TELEGRAM_APPROVED=true as well")
    parser.add_argument("--bootstrap", action="store_true", help="create empty state only for the intentional initial baseline")
    args = parser.parse_args()
    try:
        state = load_state(args.state, bootstrap=args.bootstrap)
        config = load_config(args.config)
        runner = Runner(config, state, args.delay)
        if args.phase == "prepare":
            code = runner.prepare(load_sources(args.sources), args.output_dir)
        elif args.phase == "claim":
            code = runner.claim()
        else:
            code = runner.deliver(args.allow_send)
        write_state(args.state, state, args.db)
        return code
    except Exception as exc:
        print(f"watch run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
