"""Collect public ATS feeds, apply private rules, and maintain a durable outbox.

Typical private-runner phases:
  prepare -> persist state branch -> claim -> persist state branch -> deliver -> persist.
  With --claim-next, deliver also claims the next chunk, so each message needs one persist.

The state JSON is a compact logical export stored outside this public package.
Each invocation restores it into a fresh DuckDB file before processing.
"""
from __future__ import annotations

import argparse
import ast
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

from pendel import fetch, mail_alerts, remote_boards
from pendel.audit import write_delivery_receipt, write_rollups
from pendel.targets import evaluate_job, load_config, norm

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
# S18: titles or locations that mean working American hours, i.e. nights from Armenia (🌙).
NIGHT_RX = (r"\b(US|U\.S\.|American?|North American) (business )?(hours|time ?zones?)\b|(?-i:\b(EST|EDT|PST|PDT|CST|CDT)\b)"
            r"|\b(Eastern|Pacific|Central) Time\b|\bAmericas\b|\bnight shift\b|\bovernight\b|\bgraveyard\b")
# S20: how a board job's location was checked against the employer's own listing.
VERIFY_LABELS = {"yes": "✅ employer listing", "unknown": "❓ board label only"}
ROLE_MATCH_MIN = 0.75
EMPLOYER_ATS = (("greenhouse", "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"),
                ("lever", "https://api.lever.co/v0/postings/{slug}?mode=json"),
                ("ashby", "https://api.ashbyhq.com/posting-api/job-board/{slug}"))
COMPANY_SUFFIX_RX = r"\b(inc|incorporated|llc|ltd|limited|gmbh|ag|bv|b\.v|sa|sas|srl|plc|corp|corporation|co|company|group|hq)\b\.?"


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


PAY_UNITS = {"HOUR": "hour", "DAY": "day", "WEEK": "week", "MONTH": "month", "YEAR": "year"}


def pay_text(value) -> str:
    """S13: readable pay. A schema.org MonetaryAmount/QuantitativeValue (a dict, or the
    str() of one already saved in state) becomes e.g. "2,488–2,600 €/month"."""
    if isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            value = ast.literal_eval(value.strip())
        except (ValueError, SyntaxError):
            return value.strip()
    if not isinstance(value, dict):
        return str(value or "").strip()
    inner = value.get("value") if isinstance(value.get("value"), dict) else value
    currency = str(value.get("currency") or inner.get("currency") or "EUR").upper()
    unit = PAY_UNITS.get(str(inner.get("unitText") or value.get("unitText") or "").upper(), "")

    def amount(x):
        try:
            number = float(str(x).replace(",", "."))
        except (TypeError, ValueError):
            return ""
        return f"{number:,.2f}".rstrip("0").rstrip(".") if number < 100 else f"{number:,.0f}"
    low, high = amount(inner.get("minValue")), amount(inner.get("maxValue"))
    single = amount(inner.get("value")) if not isinstance(inner.get("value"), dict) else ""
    span = f"{low}–{high}" if low and high and low != high else (low or high or single)
    if not span:
        return ""
    symbol = "€" if currency == "EUR" else currency
    return f"{span} {symbol}" + (f"/{unit}" if unit else "")


def _posted_since(posted, since: str) -> bool:
    """S18: True if an ISO ``posted`` time is at or after ``since``. An unknown date counts as recent."""
    try:
        when = datetime.fromisoformat(str(posted or "").strip().replace("Z", "+00:00"))
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when >= datetime.fromisoformat(since.replace("Z", "+00:00"))


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
        reader = remote_boards.fetch_jobs if source["ats"] in remote_boards.NAMES else fetch.fetch_jobs
        for variant_url in source.get("feed_urls") or [source["feed_url"]]:
            try:
                jobs.extend(reader(source | {"feed_url": variant_url}, self.throttle))
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
            board = source["ats"] in remote_boards.NAMES
            for item in jobs:
                # S18: a job board lists many employers; the company comes from each job.
                self._add_job(current, source, item, company=(item.get("company") or None) if board else None)
        self._collect_email(current, statuses)
        return current, statuses

    def _due_sources(self, sources: list[dict[str, str]]) -> list[dict[str, str]]:
        """S18: leave out sources fetched less than ``fetch_intervals_minutes[ats]`` ago.
        A skipped source is simply not part of this run: no status, no failure, no baseline change."""
        intervals = self.config.get("fetch_intervals_minutes") or {}
        now = self.clock()
        due = []
        for source in sources:
            minutes = intervals.get(source["ats"])
            last = (self.state["sources"].get(source["source_id"]) or {}).get("last_attempt")
            if minutes and last:
                try:
                    since = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                except ValueError:
                    since = None
                if since and now - since < timedelta(minutes=int(minutes)):
                    continue
            due.append(source)
        return due

    def _add_job(self, current: dict[str, dict], source: dict[str, str], item: dict,
                 company: str | None = None) -> None:
        ident = identity_key(source, item)
        value = {
            "identity_key": ident, "job_id": str(item.get("job_id") or ""),
            "company": company or source["companies"],
            "title": str(item.get("title") or ""), "location": str(item.get("location") or ""),
            "country": str(item.get("country") or ""), "work_mode": str(item.get("work_mode") or ""),
            "url": str(item.get("url") or ""), "posted_at": str(item.get("posted_at") or ""),
            "salary": pay_text(item.get("salary")), "lang_hint": str(item.get("lang_hint") or ""),
            "employment_type": str(item.get("employment_type") or ""),
            "schedule": str(item.get("schedule") or ""),
            "hours": str(item.get("hours") or ""),
            "hours_per_week": str(item.get("hours_per_week") or ""),
            "description": str(item.get("description") or ""), "source_ids": [source["source_id"]],
            "via": remote_boards.NAMES.get(source["ats"], ""),
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
        sources = self._due_sources(sources)
        current, statuses = self.collect(sources)
        new_items: list[dict] = []
        baseline_count = 0
        by_status = Counter()
        # Persist the set of currently degraded feeds so scheduled runs can
        # report transitions once, then stay quiet until the set changes or
        # all feeds recover. A feed counts as degraded only after failing two
        # runs in a row, so one-off network blips send nothing.
        streaks = self.state.setdefault("source_fail_streak", {})
        for s in statuses:
            streaks[s["source_id"]] = 0 if s["complete"] else streaks.get(s["source_id"], 0) + 1
        for source_id in set(streaks) - {s["source_id"] for s in statuses}:
            del streaks[source_id]
        prior_health = self.state.get("feed_health_signature", [])
        degraded = sorted([s["source_id"], s["status"]] for s in statuses
                          if not s["complete"] and streaks[s["source_id"]] >= 2)
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
            src_state["last_attempt"] = now
            by_status[status["status"]] += 1
            if status["complete"]:
                src_state["last_success"] = now
                src_state["last_error"] = ""
                src_state["baseline_complete"] = True
            # A complete empty result is a valid success and completes baseline.

        late_days = (self.config.get("delivery") or {}).get("late_match_days")
        late_since = iso(self.clock() - timedelta(days=int(late_days))) if late_days else ""
        already_sent = self._alerted_ids() if late_days else set()
        # S18: a new board source normally starts with a silent baseline. For these ats the
        # jobs it lists on its first run are alerted too, if posted in the last first_run_days.
        delivery = self.config.get("delivery") or {}
        first_run_ats = {str(x) for x in delivery.get("alert_on_first_run", [])}
        first_run_since = iso(self.clock() - timedelta(days=int(delivery.get("first_run_days", 14))))
        ats_by_source = {s["source_id"]: s["ats"] for s in statuses}
        catch_up: list[dict] = []
        for ident, value in current.items():
            seen = self.state["jobs"].get(ident)
            if seen is None:
                seen = {"first_seen": now, "last_seen": now, "source_ids": value["source_ids"]}
                self.state["jobs"][ident] = seen
                has_prior_baseline = any(baseline_before.get(sid, False) for sid in value["source_ids"])
                first_run = any(ats_by_source.get(sid) in first_run_ats for sid in value["source_ids"]) \
                    and _posted_since(value.get("posted_at"), first_run_since)
                if has_prior_baseline or first_run:
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
            alertable = value["match"]["status"] in {"match", "review"} and bool(self._alert_tiers(value))
            if alertable and value["match"]["status"] == "review" and self._foreign_place(
                    value["match"].get("country") or value.get("country"), value.get("location"),
                    self.config.get("delivery") or {}):
                alertable = False
            if value["observation"] == "newly_seen" and alertable:
                new_items.append(value)
            elif late_days and value["observation"] == "prior_seen" and alertable \
                    and not seen.get("alertable") and seen["first_seen"] >= late_since \
                    and ident not in already_sent:
                # S11: a recent job that only now matches (e.g. after a rule change).
                # Without a recorded earlier state it is backlog: one digest, not instant messages.
                (new_items if "alertable" in seen else catch_up).append(value)
            if late_days:
                seen["alertable"] = alertable

        if catch_up:
            queue = self.state.setdefault("digest_queue", [])
            queued = {x["identity_key"] for x in queue}
            queue.extend(self._digest_item(x) | {"queued_at": now} for x in catch_up if x["identity_key"] not in queued)
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

    def _alert_tiers(self, value: dict) -> set[str]:
        """S18: the tiers a job is alerted for. A tier in ``delivery.remote_only_tiers`` counts
        only for a remote job whose listed location is open from where the owner is now
        (S18b: ``delivery.remote_here``), and whose title doesn't ask for another language."""
        tagged = value["match"]
        delivery = self.config.get("delivery") or {}
        remote_only = {str(x) for x in delivery.get("remote_only_tiers", [])}
        tiers = {str(t) for t in tagged.get("tier_candidates", [])}
        if not remote_only & tiers:
            return tiers
        open_here = tagged.get("work_mode") == "remote" and self._open_here(value, delivery.get("remote_here") or {})
        return {t for t in tiers if t not in remote_only or open_here}

    @staticmethod
    def _open_here(value: dict, rule: dict) -> bool:
        """S18b: True when the location names one of ``rule.places`` (e.g. Armenia, also inside
        a long country list) or is only open regions (Worldwide, EMEA, Europe). A location
        that lists only other countries, or is empty, is not open here. A title naming one of
        ``rule.other_languages`` ("Swedish Translation Rater") is not either."""
        title = str(value.get("title") or "")
        others = [str(x) for x in rule.get("other_languages", [])]
        if others and re.search(r"\b(" + "|".join(map(re.escape, others)) + r")\b", title, re.I):
            return False
        # S19: boards label many jobs "Anywhere in the World" whose title or text limits them
        # to a region ("Support Engineer (APAC)", "must be located in the US").
        title_regions = [str(x) for x in rule.get("title_regions", [])]
        if title_regions and re.search(r"(?<![\w.])(" + "|".join(map(re.escape, title_regions)) + r")(?![\w])", title):
            return False
        text = str(value.get("description") or "")
        if any(re.search(str(p), text, re.I) for p in rule.get("restricted_phrases", [])):
            return False
        location = re.sub(r"\([^)]*\)", " ", str(value.get("location") or ""))
        places = [str(x) for x in rule.get("places", [])]
        if places and re.search(r"\b(" + "|".join(map(re.escape, places)) + r")\b", location, re.I):
            return True
        regions = {norm(str(x)).strip() for x in rule.get("regions", [])}
        pieces = [norm(re.sub(r"^((fully\s+)?remote|anywhere in)\s*[-–:]?\s*", "", p.strip(), flags=re.I)).strip()
                  for p in re.split(r"[,;/|]|\bor\b|\band\b", location) if p.strip()]
        # "EMEA Only" (We Work Remotely) is the region EMEA; "Anywhere in the World" is Worldwide.
        pieces = [re.sub(r"\s+only$", "", x).strip() for x in pieces]
        pieces = ["worldwide" if x in {"the world", "world"} else x for x in pieces]
        pieces = [x for x in pieces if x]
        # S20: a list of places is a choice ("US, Europe"), so one open region is enough.
        # A bare "Remote" names no region, so it doesn't count as open.
        return any(p in regions for p in pieces)

    def _verify_board_jobs(self, values: list[dict]) -> None:
        """S20: for each job-board job, look for the same role on the employer's public
        Greenhouse, Lever or Ashby board and judge its location from there. Sets
        ``value["verified"]``: "yes" (open from here), "no" (limited elsewhere) or "unknown"
        (no employer listing found, or it just says "Remote")."""
        delivery = self.config.get("delivery") or {}
        rule = delivery.get("remote_here") or {}
        budget = int(delivery.get("verify_max_companies", 15))
        boards: dict[str, list[dict]] = {}
        for value in values:
            if not value.get("via"):
                continue
            company = str(value.get("company") or "")
            if company not in boards:
                if budget <= 0:
                    value["verified"] = "unknown"
                    continue
                budget -= 1
                boards[company] = self._employer_jobs(company)
            # The closest title wins; an employer may post one role once per country, so all
            # equally close listings count, and the role is open if any of them is.
            scored = [(self._role_score(j.get("title"), value.get("title")), j) for j in boards[company]]
            best = max((s for s, _ in scored), default=0.0)
            matches = [j for s, j in scored if best >= ROLE_MATCH_MIN and s == best]
            if not matches:
                value["verified"] = "unknown"
                continue
            verdicts = []
            for match in matches:
                location = str(match.get("location") or "")
                listing = {"title": value.get("title"), "location": location,
                           "description": str(match.get("description") or "")}
                if self._open_here(listing, rule):
                    verdicts.append(("yes", location))
                elif re.sub(r"[\W_]+|remote", "", location, flags=re.I) == "" and not any(
                        re.search(str(p), listing["description"], re.I) for p in rule.get("restricted_phrases", [])):
                    verdicts.append(("unknown", location))
                else:
                    verdicts.append(("no", location))
            verdict = next((v for v in ("yes", "unknown", "no") if any(x == v for x, _ in verdicts)))
            value["verified"] = verdict
            value["verified_location"] = "; ".join(dict.fromkeys(loc for x, loc in verdicts if x == verdict))[:300]

    def _employer_jobs(self, company: str) -> list[dict]:
        """S20: the jobs on the employer's public ATS board, found by trying its name as a
        board slug. Where it was found (or that nothing was) is cached in state for 7 days."""
        cache = self.state.setdefault("employer_ats", {})
        key = norm(company).strip()
        base = re.sub(COMPANY_SUFFIX_RX, " ", key)
        words = re.findall(r"[a-z0-9]+", base)
        if not words:
            return []
        slugs = list(dict.fromkeys(["".join(words), "-".join(words)]))
        known = cache.get(key) or {}
        fresh = known.get("checked_at") and _posted_since(known["checked_at"], iso(self.clock() - timedelta(days=7)))
        tries = [(known["ats"], known["slug"])] if fresh and known.get("ats") else \
            [] if fresh else [(ats, slug) for ats, _ in EMPLOYER_ATS for slug in slugs]
        urls = dict(EMPLOYER_ATS)
        for ats, slug in tries:
            feed = {"ats": ats, "token": slug, "feed_url": urls[ats].format(slug=slug)}
            try:
                jobs = fetch.fetch_jobs(feed, self.throttle)
            except urllib.error.HTTPError as exc:
                if exc.code in (404, 410):
                    continue
                return []
            except (fetch.RateLimited, fetch.IncompleteFeed, urllib.error.URLError, TimeoutError, OSError,
                    http.client.HTTPException, ValueError, KeyError, TypeError, AttributeError):
                return []
            if jobs:
                cache[key] = {"ats": ats, "slug": slug, "checked_at": iso(self.clock())}
                return jobs
        if not fresh:
            cache[key] = {"ats": "", "slug": "", "checked_at": iso(self.clock())}
        return []

    @staticmethod
    def _role_score(a, b) -> float:
        """S20: how alike two job titles are (shared words / all words), 0 to 1."""
        def words(x):
            return set(re.findall(r"[a-z0-9]+", norm(str(x or ""))))
        left, right = words(a), words(b)
        if not left or not right:
            return 0.0
        return len(left & right) / len(left | right)

    @staticmethod
    def _same_role(a, b) -> bool:
        """S20: the same job title, allowing small differences ("Support Agent (EMEA)")."""
        return Runner._role_score(a, b) >= ROLE_MATCH_MIN

    @staticmethod
    def _digest_item(value: dict) -> dict:
        tagged = value["match"]
        cities = [x["city"] for x in tagged["group_tier_matches"] if x.get("city")]
        night = value.get("schedule") == "night" or bool(re.search(
            NIGHT_RX, f"{value.get('title', '')} {value.get('location', '')}", re.I))
        return {"identity_key": value["identity_key"], "title": value["title"], "company": value["company"],
                "url": value["url"], "status": tagged["status"],
                "groups": sorted({x["group_id"] for x in tagged["group_tier_matches"]}),
                "tiers": sorted(set(tagged["tier_candidates"])),
                "reasons": tagged["reasons"],
                "city": cities[0] if cities else str(value.get("location") or ""),
                "salary": str(value.get("salary") or ""),
                "hours_per_week": tagged.get("hours_per_week"),
                "german_level": tagged.get("german_level", ""),
                "via": str(value.get("via") or ""), "night": night,
                "verified": str(value.get("verified") or "")}

    @staticmethod
    def _foreign_place(country, location, delivery: dict) -> bool:
        """S13/S13b: a review job that is clearly abroad isn't worth a message. Either its country is
        known and outside delivery.alert_countries, or (as for most foreign listings, which state no
        country) its location names one of delivery.skip_review_places."""
        country = str(country or "").upper()
        allowed = {str(x).upper() for x in delivery.get("alert_countries", [])}
        if allowed and country and country not in allowed:
            return True
        place = str(location or "")
        return any(re.search(rf"(?<!\w){re.escape(str(x))}(?!\w)", place, re.I)
                   for x in delivery.get("skip_review_places", []))

    def _alerted_ids(self) -> set[str]:
        """Jobs already in any outbox message (sent, pending or cancelled) or waiting for the digest."""
        ids = {x.get("identity_key", "") for x in self.state.get("digest_queue", [])}
        for entry in self.state["outbox"]:
            ids.update(x.get("identity_key", "") for x in entry.get("payload", {}).get("items", []))
        return ids - {""}

    def _append_instant(self, items: list[dict], local: datetime, now: str, delivery: dict) -> None:
        """S16: one message per job, unless the batch is larger than
        ``delivery.instant_bundle_over``; then it goes out as one ranked list.
        Each Telegram message costs about a minute of run time (two state pushes)."""
        limit = delivery.get("instant_bundle_over")
        label = delivery.get("instant_title", "new Tier 3 jobs")
        if limit is not None and len(items) > int(limit):
            chunks, members = self._daily_digest_chunks(
                items, local, 0, speed=delivery.get("hire_speed"), compact=bool(delivery.get("compact_lists")),
                title=f"⚡ {len(items)} {label} · {local:%a %d %b}")
            self._append_outbox("instant:", items, chunks, members, now)
        else:
            chunks = [self._instant_message(item, local) for item in items]
            self._append_outbox("instant:", items, chunks, [[x["identity_key"]] for x in items], now)

    def rebundle(self) -> int:
        """S16, run once under the state lock: move the unsent chunks of pending instant
        messages into one list message. Sent chunks stay sent; the old entries are cancelled."""
        delivery = self.config.get("delivery") or {}
        if any(x.get("status") in {"ambiguous", "failed_manual", "sending"} for x in self.state["outbox"]):
            print("An outbox entry needs manual review first; nothing was rebundled.")
            return 1
        now = iso()
        items: list[dict] = []
        seen: set[str] = set()
        old: list[str] = []
        for entry in self.state["outbox"]:
            if entry["status"] != "pending" or not entry["outbox_id"].startswith("instant:") \
                    or entry.get("rebundled"):
                continue
            statuses = entry.get("chunk_statuses", [])
            unsent = {key for i, keys in enumerate(entry.get("chunk_item_ids", []))
                      if i < len(statuses) and statuses[i] == "pending" for key in keys}
            for item in entry["payload"].get("items", []):
                if item.get("identity_key") in unsent and item["identity_key"] not in seen:
                    seen.add(item["identity_key"])
                    items.append(item)
            entry["chunk_statuses"] = ["cancelled" if s == "pending" else s for s in statuses]
            entry["status"] = "cancelled"
            entry["updated_at"] = now
            entry["error"] = "unsent chunks moved into one list message (S16 rebundle)"
            old.append(entry["outbox_id"])
        if not items:
            print("No pending instant messages to rebundle.")
            return 0
        local = self.clock().astimezone(ZoneInfo(delivery.get("timezone", "Europe/Berlin")))
        chunks, members = self._daily_digest_chunks(
            items, local, 0, speed=delivery.get("hire_speed"), compact=bool(delivery.get("compact_lists")),
            title=f"⚡ {len(items)} Tier 3 jobs you haven't seen yet · {local:%a %d %b}")
        self._append_outbox("instant:", items, chunks, members, now)
        self.state["outbox"][-1]["rebundled"] = True
        print(f"Rebundled {len(items)} jobs from {len(old)} messages into {len(chunks)} chunk(s).")
        return 0

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
        candidates = [x for x in new_items if instant_tiers & self._alert_tiers(x)]
        # S20: check board jobs against the employer's own listing; drop the ones it limits elsewhere.
        if delivery.get("verify_board_jobs"):
            self._verify_board_jobs(candidates)
        instant = [self._digest_item(x) for x in candidates if x.get("verified") != "no"]
        instant_ids = {x["identity_key"] for x in candidates}
        # S18b: boards repost one role under several ids; send it once per batch (and not in the digest).
        seen_roles: set[tuple[str, str]] = set()
        instant = [x for x in instant if not ((role := (norm(x["title"]), norm(x["company"]))) in seen_roles
                                              or seen_roles.add(role))]
        if instant:
            self._append_instant(instant, local, now, delivery)
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
                chunks, members = self._daily_digest_chunks(queue, local, by_day.get(today, 0),
                                                            speed=delivery.get("hire_speed"),
                                                            compact=bool(delivery.get("compact_lists")))
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
    def _facts(item: dict) -> list[str]:
        """Pay, hours and German level, only when the listing states them."""
        facts = []
        pay = pay_text(item.get("salary"))
        if pay:
            facts.append(f"💶 {pay[:120]}")
        if item.get("hours_per_week"):
            facts.append(f"⏱ {item['hours_per_week']} h/week")
        if item.get("german_level"):
            facts.append(f"🗣 German {str(item['german_level'])[:20]}")
        return facts

    @staticmethod
    def _instant_message(item: dict, local: datetime) -> str:
        tiers = "+".join(item.get("tiers", [])) or "?"
        place = " · ".join(x for x in (str(item.get("city") or "")[:150], str(item.get("company") or "")[:150]) if x)
        lines = [f"🟢 T{tiers} · {str(item.get('title', ''))[:300]}"]
        if place:
            lines.append(f"📍 {place}")
        facts = Runner._facts(item)
        if item.get("night"):
            facts.append("🌙 US hours")
        if VERIFY_LABELS.get(str(item.get("verified") or "")):
            facts.append(VERIFY_LABELS[str(item["verified"])])
        if facts:
            lines.append("  ".join(facts))
        # A match may still carry informational reasons; only a review needs a look.
        if item.get("status") == "review" and item.get("reasons"):
            lines.append("⚠️ " + "; ".join(item["reasons"][:2])[:250])
        if item.get("url"):
            lines.append(str(item["url"])[:1000] + (f"  (via {item['via']})" if item.get("via") else ""))
        return "\n".join(lines)

    @staticmethod
    def _hire_speed(job: dict, speed: dict | None) -> int:
        """S12: 0 = likely quick start (temp agency or no-qualification title), 1 = other
        logistics/production work, 2 = the rest. Words come from ``delivery.hire_speed``."""
        if not speed:
            return 2
        title = str(job.get("title") or "").casefold()
        company = str(job.get("company") or "").casefold()
        if any(str(x).casefold() in company for x in speed.get("fast_companies", [])) \
                or any(str(x).casefold() in title for x in speed.get("fast_title_words", [])):
            return 0
        if any(str(x).casefold() in title for x in speed.get("medium_title_words", [])):
            return 1
        return 2

    @staticmethod
    def _daily_digest_chunks(items: list[dict], local: datetime, instant_count: int,
                             max_chars: int = 3900, speed: dict | None = None,
                             title: str | None = None, compact: bool = False) -> tuple[list[str], list[list[str]]]:
        header = title or f"📋 {len(items)} new jobs · {local:%a %d %b}"
        footer = f"\n\nInstant Tier 3 alerts today: {instant_count}" if instant_count else ""
        by_tier: dict[str, list[dict]] = {}
        quick: list[dict] = []
        for item in items:
            # S13: quick-start jobs of every tier come first, in their own section.
            if speed and Runner._hire_speed(item, speed) == 0:
                quick.append(item)
            else:
                by_tier.setdefault(min(item.get("tiers") or ["?"]), []).append(item)
        sections = ([("⚡ QUICK START", quick)] if quick else []) + [(f"TIER {t}", by_tier[t]) for t in sorted(by_tier)]
        chunks: list[str] = []
        members: list[list[str]] = []
        current, current_members = header, []
        for label, group in sections:
            # Quickest hire first, then matches before reviews, stated pay first, then by city.
            jobs = sorted(group, key=lambda x: (Runner._hire_speed(x, speed), x.get("status") != "match",
                                                not x.get("salary"), str(x.get("city") or "").casefold()))
            section = f"\n\n{label} · {len(jobs)}"
            current += section
            for job in jobs:
                place = " · ".join(x for x in (str(job.get("city") or ", ".join(job.get("groups", [])))[:120],
                                               str(job.get("company", ""))[:120]) if x)
                mark = "⚡" if speed and Runner._hire_speed(job, speed) == 0 else "•"
                if compact:
                    # S17: one line per job (title · city · company · pay), then the link.
                    # Reviews are marked ❔ instead of listing their reasons.
                    mark = "❔" if job.get("status") == "review" else mark
                    pay = pay_text(job.get("salary"))[:60]
                    extra = ("🌙 US hours" if job.get("night") else "",
                             VERIFY_LABELS.get(str(job.get("verified") or ""), ""),
                             f"via {job['via']}" if job.get("via") else "")
                    entry = "\n" + " · ".join(x for x in (f"{mark} {str(job.get('title', ''))[:120]}",
                                                          place, pay, *extra) if x)
                else:
                    entry = f"\n{mark} {str(job.get('title', ''))[:250]}"
                    detail = " · ".join([place] + Runner._facts(job)) if place else " · ".join(Runner._facts(job))
                    if job.get("night"):
                        detail = " · ".join(x for x in (detail, "🌙 US hours") if x)
                    if job.get("via"):
                        detail = " · ".join(x for x in (detail, f"via {job['via']}") if x)
                    if detail:
                        entry += f"\n  {detail}"
                    if job.get("status") == "review" and job.get("reasons"):
                        entry += "\n  ⚠️ " + "; ".join(job["reasons"][:2])[:200]
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
        # S18: with delivery.instant_first, instant messages go before older digests and notices.
        order = self.state["outbox"]
        if (self.config.get("delivery") or {}).get("instant_first"):
            order = sorted(order, key=lambda x: not str(x.get("outbox_id", "")).startswith("instant:"))
        for item in order:
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
            with urllib.request.urlopen(request, timeout=30) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not result.get("ok"):
                item["chunk_statuses"][index] = "failed_manual"
                item["status"] = "failed_manual"
                item["error"] = "Telegram returned a non-success result; review before retry"
            else:
                item["chunk_statuses"][index] = "sent"
                item["status"] = "sent" if all(s == "sent" for s in item["chunk_statuses"]) else "pending"
                item["error"] = ""
        # HTTPError is a URLError (and an OSError), so it must be caught first. It means
        # Telegram answered with an error, so the message was certainly not delivered.
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                # Rate limited: nothing was sent. Wait as asked and leave the chunk pending.
                try:
                    wait = int(json.loads(exc.read().decode("utf-8")).get("parameters", {}).get("retry_after", 5))
                except (ValueError, OSError):
                    wait = 5
                time.sleep(min(max(wait, 1), 60))
                item["chunk_statuses"][index] = "pending"
                item["status"] = "pending"
                item["error"] = f"Telegram rate limit (HTTP 429); waited {wait}s, will retry"
            else:
                item["chunk_statuses"][index] = "failed_manual"
                item["status"] = "failed_manual"
                item["error"] = f"Telegram rejected the request (HTTP {exc.code}); not delivered; review before retry"
        except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException) as exc:
            # S16b: the owner accepts an occasional duplicate over a blocked feed, so an
            # uncertain send is retried up to delivery.uncertain_retries times before it
            # needs manual review. Off (0) unless configured.
            retries = int((self.config.get("delivery") or {}).get("uncertain_retries", 0))
            tries = item.setdefault("chunk_uncertain", [0] * len(item["chunk_statuses"]))
            tries += [0] * (len(item["chunk_statuses"]) - len(tries))
            item["chunk_uncertain"] = tries
            tries[index] += 1
            if tries[index] <= retries:
                item["chunk_statuses"][index] = "pending"
                item["status"] = "pending"
                item["error"] = (f"delivery outcome uncertain ({type(exc).__name__}); "
                                 f"retry {tries[index]} of {retries}, a duplicate is possible")
            else:
                item["chunk_statuses"][index] = "ambiguous"
                item["status"] = "ambiguous"
                item["error"] = f"delivery outcome uncertain ({type(exc).__name__}); manual review required; no automatic retry"
        except ValueError:
            item["chunk_statuses"][index] = "failed_manual"
            item["status"] = "failed_manual"
            item["error"] = "Telegram response was not valid JSON; review before retry"
        item["updated_at"] = iso()
        print(f"Telegram digest state recorded as {item['status']}. {item['error']}".rstrip())
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
    parser.add_argument("--phase", required=True, choices=("prepare", "claim", "deliver", "receipt", "rebundle"))
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
    parser.add_argument("--claim-next", action="store_true",
                        help="deliver phase: after recording the outcome, claim the next pending chunk")
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
        elif args.phase == "rebundle":
            code = runner.rebundle()
        elif args.phase == "receipt":
            if not args.report_dir:
                raise ValueError("--report-dir is required for the receipt phase")
            receipt = write_delivery_receipt(args.report_dir, state, args.delivery_mode)
            print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
            code = 0
        else:
            code = runner.deliver(args.allow_send)
            # S17: record this chunk's outcome and claim the next one in the same state
            # write, so the runner persists once per message. claim() claims nothing
            # while an entry needs review.
            if code == 0 and args.claim_next:
                code = runner.claim()
        if args.phase != "receipt":
            write_state(args.state, state, args.db)
        return code
    except Exception as exc:
        print(f"watch run failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
