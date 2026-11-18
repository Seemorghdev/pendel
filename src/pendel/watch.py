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
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import duckdb

from pendel import fetch, mail_alerts
from pendel.audit import write_delivery_receipt, write_rollups
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
                 "hours_per_week", "hours_per_week_max", "commute_status", "commute_note", "posted_at", "first_seen", "url", "reasons", "source_ids",
                 "identity_key", "observation"]


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


def canonical_url(url: str) -> str:
    p = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k.casefold() not in {"utm_source", "utm_medium", "utm_campaign", "gh_src"}]
    return urlunsplit((p.scheme.casefold(), p.netloc.casefold(), p.path.rstrip("/"),
                       urlencode(sorted(query)), ""))


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
        return "url:" + canonical_url(url)
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


def provider_key(url: str) -> str:
    """Throttle and rate-limit key: the last two host labels, as fetch.HostThrottle uses."""
    return ".".join((urlsplit(url).hostname or "").split(".")[-2:])


class SharedThrottle:
    """fetch.HostThrottle spacing, safe to share between fetch threads.

    Requests to one provider key stay at least ``delay`` seconds apart even when
    two threads reach the same key; different keys never wait for each other.
    """

    def __init__(self, delay: float):
        self.delay = delay
        self.last: dict[str, float] = {}
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def wait(self, url: str) -> None:
        key = provider_key(url)
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            gap = time.monotonic() - self.last.get(key, 0.0)
            if gap < self.delay:
                time.sleep(self.delay - gap)
            self.last[key] = time.monotonic()


class Runner:
    def __init__(self, config: dict, state: dict, delay: float = 2.0,
                 clock: Callable[[], datetime] | None = None, workers: int = 1,
                 mail: mail_alerts.MailClient | None = None):
        self.config = config
        self.state = state
        self.throttle = SharedThrottle(max(2.0, delay)) if workers > 1 else fetch.HostThrottle(max(2.0, delay))
        self.clock = clock or utc_now
        self.workers = max(1, workers)
        self.mail = mail

    def _fetch_source(self, source: dict[str, str], blocked: bool) -> tuple[str, str, list[dict], bool, bool]:
        """Fetch every feed variant of one source. Returns status, error, jobs, complete, rate_limited."""
        if blocked:
            return "skipped_after_rate_limit", "provider host rate-limited earlier in this run", [], False, False
        status, error, complete, limited = "ok", "", True, False
        jobs: list[dict] = []
        for variant_url in source.get("feed_urls") or [source["feed_url"]]:
            try:
                jobs.extend(fetch.fetch_jobs(source | {"feed_url": variant_url}, self.throttle))
            except fetch.RateLimited as exc:
                status, error, complete, limited = "rate_limited", str(exc), False, True
                break
            except fetch.IncompleteFeed as exc:
                status, error, complete = "partial", str(exc), False
            except urllib.error.HTTPError as exc:
                status, error, complete = f"http_{exc.code}", str(exc.reason)[:200], False
                if exc.code == 429:
                    limited = True
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                status, error, complete = "network_error", str(getattr(exc, "reason", exc))[:200], False
            # http.client errors (IncompleteRead, BadStatusLine) are not OSErrors:
            # a connection dropped mid-response must fail this source, not the run.
            except http.client.HTTPException as exc:
                status, error, complete = "network_error", type(exc).__name__, False
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                status, error, complete = "parse_error", f"{type(exc).__name__}: {exc}"[:200], False
        return status, error, jobs, complete, limited

    def _fetch_all(self, sources: list[dict[str, str]]) -> list[tuple[str, str, list[dict], bool, bool]]:
        """Fetch results in source order. Each provider key is one sequential lane,
        so a rate limit still skips the rest of that provider exactly as before;
        with ``workers`` > 1 the lanes run side by side."""
        lanes: dict[str, list[int]] = {}
        for index, source in enumerate(sources):
            lanes.setdefault(provider_key((source.get("feed_urls") or [source["feed_url"]])[0]), []).append(index)
        results: list = [None] * len(sources)

        def run_lane(indexes: list[int]) -> None:
            blocked = False
            for index in indexes:
                results[index] = self._fetch_source(sources[index], blocked)
                blocked = blocked or results[index][4]

        if self.workers == 1:
            blocked_keys: set[str] = set()
            for index, source in enumerate(sources):
                key = provider_key((source.get("feed_urls") or [source["feed_url"]])[0])
                results[index] = self._fetch_source(source, key in blocked_keys)
                if results[index][4]:
                    blocked_keys.add(key)
        else:
            with ThreadPoolExecutor(max_workers=min(self.workers, len(lanes) or 1)) as pool:
                for future in [pool.submit(run_lane, indexes) for indexes in lanes.values()]:
                    future.result()
        return results

    def collect(self, sources: list[dict[str, str]]) -> tuple[dict[str, dict], list[dict]]:
        current: dict[str, dict] = {}
        statuses: list[dict] = []
        for source, (status, error, jobs, complete, _limited) in zip(sources, self._fetch_all(sources)):
            if status == "ok" and not jobs:
                status = "empty"
            statuses.append({"source_id": source["source_id"], "companies": source["companies"],
                             "ats": source["ats"], "status": status, "error": error,
                             "job_count": len(jobs), "complete": complete})
            if not complete:
                continue
            for item in jobs:
                self._add_job(current, source, item)
        self._collect_email(current, statuses)
        return current, statuses

    def _add_job(self, current: dict[str, dict], source: dict[str, str], item: dict,
                 company: str | None = None) -> None:
        ident = identity_key(source, item)
        value = {
            "identity_key": ident, "job_id": str(item.get("job_id") or ""),
            "company": company or source["companies"],
            "title": str(item.get("title") or ""), "location": str(item.get("location") or ""),
            "country": str(item.get("country") or ""), "work_mode": str(item.get("work_mode") or ""),
            "url": str(item.get("url") or ""), "posted_at": str(item.get("posted_at") or ""),
            "salary": str(item.get("salary") or ""), "lang_hint": str(item.get("lang_hint") or ""),
            "employment_type": str(item.get("employment_type") or ""),
            "schedule": str(item.get("schedule") or ""),
            "hours": str(item.get("hours") or ""),
            "hours_per_week": str(item.get("hours_per_week") or ""),
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

    def _collect_email(self, current: dict[str, dict], statuses: list[dict]) -> None:
        """S6: jobs from employer alert emails, as one more source after the feeds.

        A job whose link is already a feed job only adds this source to that job."""
        settings = self.config.get("email_alerts")
        if self.mail is None or not settings:
            return
        source = {"source_id": mail_alerts.SOURCE_ID, "ats": "email_alert", "token": "gmail",
                  "feed_url": "", "companies": "Email job alerts"}
        status, error, jobs, complete = mail_alerts.read_label(
            self.mail, settings, self.state.setdefault("email_alerts", {}), lambda: iso(self.clock()))
        statuses.append({"source_id": source["source_id"], "companies": source["companies"],
                         "ats": source["ats"], "status": status, "error": error,
                         "job_count": len(jobs), "complete": complete})
        if not complete:
            return
        feed_by_url = {canonical_url(value["url"]): ident for ident, value in current.items() if value["url"]}
        for item in jobs:
            feed_ident = feed_by_url.get(canonical_url(item["url"]))
            if feed_ident is not None:
                known = current[feed_ident]
                known["source_ids"] = sorted(set(known["source_ids"]) | {source["source_id"]})
                continue
            self._add_job(current, source, item, company=item["company"])

    def prepare(self, sources: list[dict[str, str]], output_dir: str | Path) -> int:
        now = iso(self.clock())
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
                has_prior_baseline = any(baseline_before.get(sid, False) for sid in value["source_ids"])
                if has_prior_baseline:
                    value["observation"] = "newly_seen"
                else:
                    value["observation"] = "baseline"
                    baseline_count += 1
            else:
                seen["last_seen"] = now
                seen["source_ids"] = sorted(set(seen.get("source_ids", []) + value["source_ids"]))
                has_prior_baseline = False
                value["observation"] = "prior_seen"
            value["first_seen"] = seen["first_seen"]
            value["status"] = value["match"]["status"]
            if value["status"] in ("match", "review"):
                value["groups"] = "; ".join(sorted({x["group_id"] for x in value["match"]["group_tier_matches"]}))
                value["tiers"] = "; ".join(sorted({x["tier"] for x in value["match"]["group_tier_matches"]}))
            # Tier-matched jobs with unresolved location/work mode are still
            # actionable review alerts. Known exclusions never reach this path.
            if value["observation"] == "newly_seen" and value["match"]["status"] in {"match", "review"} \
                    and value["match"]["tier_candidates"]:
                new_items.append(value)

        self._queue_alerts(new_items, now, statuses)

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
        snapshot_rows = [{
            "identity_key": value["identity_key"], "source_ids": "; ".join(value["source_ids"]),
            "company": value["company"], "job_id": value["job_id"], "title": value["title"], "location": value["location"],
            "work_mode": value["work_mode"], "country": value["country"], "url": value["url"],
            "posted_at": value["posted_at"], "salary": value["salary"], "lang_hint": value["lang_hint"],
            "employment_type": value["employment_type"], "schedule": value["schedule"],
            "hours": value["hours"], "hours_per_week": value["hours_per_week"],
            "description": value["description"],
        } for value in current.values()]
        self._write_csv(output / "input-snapshot.csv", snapshot_rows,
                        ["identity_key", "source_ids", "company", "job_id", "title", "location", "work_mode", "country",
                         "url", "posted_at", "salary", "lang_hint", "employment_type", "schedule", "hours",
                         "hours_per_week", "description"])
        for label, rows in reports.items():
            self._write_csv(output / f"{label}.csv", rows)
        audit_summary = write_rollups(output, reports, snapshot_rows)
        self._write_csv(output / "source-status.csv", statuses, list(statuses[0]) if statuses else
                        ["source_id", "companies", "ats", "status", "error", "job_count", "complete"])
        summary = {
            "run_at": now, "sources": len(sources), "source_status": dict(by_status),
            "incomplete_sources": sum(1 for status in statuses if not status["complete"]),
            "current_unique_listings": len(current), "first_seen": len(new_items),
            "new_alert_candidates": len(new_items),
            "newly_seen_listings": audit_summary["observation_counts"].get("newly_seen", 0),
            "prior_seen_listings": audit_summary["observation_counts"].get("prior_seen", 0),
            "silent_baseline_listings": baseline_count,
            "current_decisions": {key: len(rows) for key, rows in reports.items()},
            "decision_audit": audit_summary,
            "telegram_outbox_pending": sum(1 for item in self.state["outbox"] if item["status"] == "pending"),
            "delivery_status": "not_recorded_before_delivery_phase",
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
        cities = [x["city"] for x in tagged["group_tier_matches"] if x.get("city")]
        return {"identity_key": value["identity_key"], "title": value["title"], "company": value["company"],
                "url": value["url"], "status": tagged["status"],
                "groups": sorted({x["group_id"] for x in tagged["group_tier_matches"]}),
                "tiers": sorted(set(tagged["tier_candidates"])),
                "reasons": tagged["reasons"],
                "city": cities[0] if cities else str(value.get("location") or ""),
                "salary": str(value.get("salary") or ""),
                "hours_per_week": tagged.get("hours_per_week"),
                "german_level": tagged.get("german_level", "")}

    def _append_outbox(self, prefix: str, items: list[dict], chunks: list[str],
                       chunk_item_ids: list[list[str]], now: str) -> None:
        material = now + json.dumps([x.get("identity_key", "") for x in items])
        self.state["outbox"].append({"outbox_id": prefix + hashlib.sha256(material.encode()).hexdigest()[:20],
                                      "status": "pending", "created_at": now, "updated_at": now,
                                      "attempts": 0, "error": "",
                                      "chunk_statuses": ["pending"] * len(chunks),
                                      "chunk_item_ids": chunk_item_ids,
                                      "payload": {"items": items, "chunks": chunks}})

    def _queue_alerts(self, new_items: list[dict], now: str, statuses: list[dict] | None = None) -> None:
        """Route new listings to the outbox.

        Without a ``delivery`` config block every run sends one combined digest,
        as before. With it, ``instant_tiers`` listings go out in this run, one
        message each; the rest wait in ``digest_queue`` for the first run after
        ``digest_hour`` local time, which sends them as one daily digest. On
        ``weekly_day`` the same run also sends a weekly summary.
        """
        delivery = self.config.get("delivery")
        if not delivery:
            if new_items:
                payload = {"items": [self._digest_item(x) for x in new_items]}
                chunks, members = self._telegram_chunks_with_members(payload)
                self._append_outbox("digest:", payload["items"], chunks, members, now)
            return

        local = self.clock().astimezone(ZoneInfo(delivery.get("timezone", "Europe/Berlin")))
        today = local.date().isoformat()
        instant_tiers = {str(tier) for tier in delivery.get("instant_tiers", [])}
        instant = [self._digest_item(x) for x in new_items if instant_tiers & set(x["match"]["tier_candidates"])]
        instant_ids = {x["identity_key"] for x in instant}
        if instant:
            chunks = [self._instant_message(item, local) for item in instant]
            self._append_outbox("instant:", instant, chunks, [[x["identity_key"]] for x in instant], now)
        by_day = self.state.setdefault("instant_alerts_by_day", {})
        by_day[today] = by_day.get(today, 0) + len(instant)

        queue = self.state.setdefault("digest_queue", [])
        queued = {x["identity_key"] for x in queue}
        for value in new_items:
            if value["identity_key"] not in instant_ids and value["identity_key"] not in queued:
                queue.append(self._digest_item(value) | {"queued_at": now})

        groups_today = self.state.setdefault("new_jobs_by_group_by_day", {}).setdefault(today, {})
        for value in new_items:
            for group in sorted({x["group_id"] for x in value["match"]["group_tier_matches"]}) or ["no group"]:
                groups_today[group] = groups_today.get(group, 0) + 1
        if statuses is not None:
            runs = self.state.setdefault("runs_by_day", {})
            runs[today] = runs.get(today, 0) + 1
            failures = self.state.setdefault("source_failures_by_day", {}).setdefault(today, {})
            for status in statuses:
                if not status["complete"]:
                    failures[status["source_id"]] = failures.get(status["source_id"], 0) + 1

        due = local.hour >= int(delivery.get("digest_hour", 18))
        if due and self.state.get("last_digest_date") != today:
            if queue or by_day.get(today):
                chunks, members = self._daily_digest_chunks(queue, local, by_day.get(today, 0))
                self._append_outbox("daily:", list(queue), chunks, members, now)
            self.state.setdefault("digest_items_by_day", {})[today] = len(queue)
            self.state["digest_queue"] = []
            self.state["last_digest_date"] = today

        weekly_day = delivery.get("weekly_day")
        if (weekly_day is not None and due and local.weekday() == int(weekly_day)
                and self.state.get("last_weekly_date") != today):
            labels = {s["source_id"]: f"{s.get('companies', '')} ({s.get('ats', '')})" for s in statuses or []}
            message = self._weekly_message(self.state, local, labels)
            self._append_outbox("weekly:", [], [message], [[]], now)
            self.state["last_weekly_date"] = today

        cutoff = (local.date() - timedelta(days=14)).isoformat()
        for key in ("instant_alerts_by_day", "digest_items_by_day", "new_jobs_by_group_by_day",
                    "runs_by_day", "source_failures_by_day"):
            days = self.state.get(key, {})
            for day in [d for d in days if d < cutoff]:
                del days[day]

    @staticmethod
    def _weekly_message(state: dict, local: datetime, labels: dict[str, str]) -> str:
        """Seven-day summary ending today, from counts kept by ``_queue_alerts``."""
        week = {(local.date() - timedelta(days=n)).isoformat() for n in range(7)}
        def total(key: str) -> int:
            return sum(v for d, v in state.get(key, {}).items() if d in week)
        groups: Counter = Counter()
        failures: Counter = Counter()
        for day in week:
            groups.update(state.get("new_jobs_by_group_by_day", {}).get(day, {}))
            failures.update(state.get("source_failures_by_day", {}).get(day, {}))
        runs = total("runs_by_day")
        failing = sorted((sid for sid, count in failures.items() if runs and count * 2 > runs),
                         key=lambda sid: (-failures[sid], sid))
        lines = [f"📊 WEEKLY · {local:%a %d %b}",
                 f"Instant alerts sent: {total('instant_alerts_by_day')}",
                 f"Jobs in daily digests: {total('digest_items_by_day')}"]
        if groups:
            ranked = sorted(groups.items(), key=lambda item: (-item[1], item[0]))
            lines.append("New jobs by group: " + " · ".join(f"{g} {n}" for g, n in ranked)[:1500])
            lines.append(f"Best group this week: {ranked[0][0]} ({ranked[0][1]})")
        else:
            lines.append("No new jobs this week.")
        if failing:
            shown = [f"{labels.get(sid, sid)[:120]} {failures[sid]}/{runs}" for sid in failing[:10]]
            more = f" · plus {len(failing) - 10} more" if len(failing) > 10 else ""
            lines.append("Feeds failing most runs: " + " · ".join(shown) + more)
        return "\n".join(lines)[:3900]

    @staticmethod
    def _instant_message(item: dict, local: datetime) -> str:
        tiers = " + ".join(item.get("tiers", [])) or "?"
        groups = ", ".join(item.get("groups", []))
        place = str(item.get("city") or "location not stated")[:200] + (f" ({groups[:200]})" if groups else "")
        hours = item.get("hours_per_week")
        lines = [f"🟢 TIER {tiers} · NEW · {local:%H:%M}",
                 str(item.get("title", ""))[:350],
                 f"{str(item.get('company', ''))[:250]} · {place}",
                 f"💶 {str(item.get('salary') or 'not stated')[:120]}  ⏱ hours: {hours if hours else 'not stated'}",
                 f"🗣 German: {item.get('german_level') or 'not stated'}"]
        if item.get("status") == "review" and item.get("reasons"):
            lines.append("Check: " + "; ".join(item["reasons"])[:350])
        if item.get("url"):
            lines.append(str(item["url"])[:1000])
        return "\n".join(lines)

    @staticmethod
    def _daily_digest_chunks(items: list[dict], local: datetime, instant_count: int,
                             max_chars: int = 3900) -> tuple[list[str], list[list[str]]]:
        header = f"📋 DAILY DIGEST · {local:%a %d %b}"
        footer = f"\n\nInstant alerts sent today: {instant_count}"
        by_tier: dict[str, list[dict]] = {}
        for item in items:
            by_tier.setdefault(min(item.get("tiers") or ["?"]), []).append(item)
        chunks: list[str] = []
        members: list[list[str]] = []
        current, current_members = header, []
        for tier in sorted(by_tier):
            jobs = sorted(by_tier[tier], key=lambda x: x.get("status") != "match")
            section = f"\n\nTIER {tier} · {len(jobs)} new"
            current += section
            for job in jobs:
                entry = (f"\n• {str(job.get('title', ''))[:300]} · {str(job.get('company', ''))[:200]}"
                         f" · {str(job.get('city') or ', '.join(job.get('groups', [])))[:150]}")
                if job.get("status") == "review" and job.get("reasons"):
                    entry += "\n  check: " + "; ".join(job["reasons"])[:250]
                if job.get("url"):
                    entry += "\n  " + str(job["url"])[:1000]
                if len(current) + len(entry) + len(footer) > max_chars and current_members:
                    chunks.append(current)
                    members.append(current_members)
                    current, current_members = header + " (continued)" + section, []
                current += entry
                current_members.append(str(job.get("identity_key", "")))
        chunks.append(current + footer)
        members.append(current_members)
        return chunks, members

    @staticmethod
    def _report_row(value: dict) -> dict:
        tagged = value["match"]
        return {"status": tagged["status"], "groups": value.get("groups", ""), "tiers": value.get("tiers", ""),
                "company": value["company"], "title": value["title"], "location": value["location"],
                "work_mode": tagged["work_mode"], "country": tagged["country"],
                "language_requirement_status": tagged["language_requirement_status"],
                "german_level": tagged["german_level"], "advert_language": tagged["advert_language"],
                "hours_status": tagged["hours_status"], "hours_per_week": tagged["hours_per_week"] or "",
                "hours_per_week_max": tagged.get("hours_per_week_max") or "",
                "commute_status": "remote" if tagged["work_mode"] == "remote" else "route_review",
                "commute_note": "Remote role" if tagged["work_mode"] == "remote" else
                                "City-level group only; verify public-transport route to the exact worksite",
                "posted_at": value["posted_at"], "first_seen": value["first_seen"], "url": value["url"],
                "reasons": "; ".join(tagged["reasons"]), "source_ids": "; ".join(value["source_ids"]),
                "identity_key": value["identity_key"], "observation": value.get("observation", "not_recorded")}

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
                 "## First-seen baseline", "",
                 f"- Newly seen after an existing source baseline: {summary['newly_seen_listings']}",
                 f"- Previously seen and still present: {summary['prior_seen_listings']}",
                 f"- New eligible alert candidates: {summary['new_alert_candidates']}",
                 f"- Quiet initial baseline: {summary['silent_baseline_listings']}",
                 "- Failed or partial feeds do not complete a baseline or mark listings closed."]
        if summary.get("incomplete_sources"):
            lines.extend([f"- Incomplete sources: {summary['incomplete_sources']}; first-seen counts include completed sources only."])
        lines.extend(["",
                 "## Notifications", "", "- This collection artifact is uploaded before delivery; delivery outcomes are not recorded here.",
                 f"- Outbox messages pending at collection time: {summary['telegram_outbox_pending']}",
                 "- See the separate post-delivery receipt artifact for persisted chunk outcomes.", ""])
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
        return Runner._telegram_chunks_with_members(payload, max_chars)[0]

    @staticmethod
    def _telegram_chunks_with_members(payload: dict, max_chars: int = 3900) -> tuple[list[str], list[list[str]]]:
        items = payload.get("items", [])
        matches = [x for x in items if x.get("status") == "match"]
        reviews = [x for x in items if x.get("status") == "review"]
        header = f"New job watch — {len(matches)} matches, {len(reviews)} to review."
        chunks: list[str] = []
        chunk_members: list[list[str]] = []
        current = header
        current_members: list[str] = []
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
                    chunk_members.append(current_members)
                    current = header + "\n(continued)"
                    current_members = []
                    current_section = ""
                    prefix = "\n\n" + section + "\n"
                if current_section != section:
                    current += prefix
                    current_section = section
                else:
                    current += prefix
                current += entry
                current_members.append(str(job.get("identity_key", "")))
        if current != header:
            chunks.append(current)
            chunk_members.append(current_members)
        return (chunks or [header], chunk_members or [[]])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", required=True, choices=("prepare", "claim", "deliver", "receipt"))
    parser.add_argument("--sources", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--output-dir", default="watch-report")
    parser.add_argument("--report-dir", help="prepared report directory used by the receipt phase")
    parser.add_argument("--delivery-mode", choices=("no_send", "send_authorized", "unknown"), default="unknown",
                        help="delivery mode of the workflow that produced the receipt")
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--fetch-workers", type=int, default=1,
                        help="fetch this many provider hosts side by side; each host keeps --delay spacing")
    parser.add_argument("--allow-send", action="store_true", help="requires PENDEL_TELEGRAM_APPROVED=true as well")
    parser.add_argument("--bootstrap", action="store_true", help="create empty state only for the intentional initial baseline")
    args = parser.parse_args()
    try:
        state = load_state(args.state, bootstrap=args.bootstrap)
        config = load_config(args.config)
        mail = None
        if args.phase == "prepare" and config.get("email_alerts") and mail_alerts.configured():
            mail = mail_alerts.GmailClient()
        runner = Runner(config, state, args.delay, workers=args.fetch_workers, mail=mail)
        if args.phase == "prepare":
            code = runner.prepare(load_sources(args.sources), args.output_dir)
        elif args.phase == "claim":
            code = runner.claim()
        elif args.phase == "receipt":
            if not args.report_dir:
                raise ValueError("--report-dir is required for the receipt phase")
            receipt = write_delivery_receipt(args.report_dir, state, args.delivery_mode)
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            code = 0
        else:
            code = runner.deliver(args.allow_send)
        if args.phase != "receipt":
            write_state(args.state, state, args.db)
        return code
    except Exception as exc:
        print(f"watch run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
