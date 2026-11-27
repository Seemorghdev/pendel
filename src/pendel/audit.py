"""Privacy-safe decision rollups and post-delivery notification receipts."""
from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from pathlib import Path


EVIDENCE_FIELDS = (
    "missing_location", "missing_country", "missing_work_mode",
    "missing_language_evidence", "missing_hours", "missing_description", "missing_identity",
)


def _values(text: str, separator: str = ";") -> list[str]:
    return sorted({part.strip() for part in (text or "").split(separator) if part.strip()})


def _row_identity(row: dict[str, str]) -> str:
    identity = row.get("identity_key", "").strip()
    if not identity:
        material = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
        return "unkeyed:" + hashlib.sha256(material.encode()).hexdigest()
    return identity


def _missing_evidence(row: dict[str, str]) -> dict[str, int]:
    return {
        "missing_location": int(not row.get("location", "").strip() or
                                "actual job location does not identify" in row.get("reasons", "").casefold()),
        "missing_country": int(not row.get("country", "").strip() or
                                "remote work country not stated" in row.get("reasons", "").casefold()),
        "missing_work_mode": int(not row.get("work_mode", "").strip() or
                                  "work mode is unclear" in row.get("reasons", "").casefold()),
        "missing_language_evidence": int(row.get("language_requirement_status", "") == "language_unclear"),
        "missing_hours": int(row.get("hours_status", "") in {"hours_unclear", "part_time_unknown", "hours_range"}),
        "missing_description": int(not row.get("has_description", "1")),
        "missing_identity": int(not row.get("identity_key", "").strip()),
    }


def write_rollups(output_dir: str | Path, reports: dict[str, list[dict]], input_snapshots: list[dict] | None = None) -> dict:
    """Write dimensional unique-listing counts; never include descriptions or URLs."""
    output = Path(output_dir)
    # Dimension/status accumulator stores identities to make each count unique.
    buckets: dict[tuple[str, str, str], dict[str, dict[str, int]]] = defaultdict(dict)
    listing_rows = []
    all_rows = [row for rows in reports.values() for row in rows]
    descriptions = {row.get("identity_key", ""): bool(str(row.get("description", "")).strip())
                    for row in (input_snapshots or [])}
    for row in all_rows:
        identity = _row_identity(row)
        evid = _missing_evidence(row | {"has_description": "1" if descriptions.get(identity, False) else ""})
        listing_rows.append({
            "identity_key": identity,
            "status": row.get("status", ""),
            "observation": row.get("observation", "not_recorded"),
            "company": row.get("company", ""),
            "groups": row.get("groups", ""),
            "tiers": row.get("tiers", ""),
            "reasons": row.get("reasons", ""),
            "has_description": int(descriptions.get(identity, False)),
            **evid,
        })
        dimensions = [("company", [row.get("company", "")])]
        dimensions += [("group", _values(row.get("groups", "")))]
        dimensions += [("tier", _values(row.get("tiers", "")))]
        dimensions += [("reason", _values(row.get("reasons", "")))]
        for dimension, values in dimensions:
            for value in values:
                if not value:
                    continue
                key = (dimension, value, row.get("status", ""))
                # If a listing has multiple feed sources, its identity is still counted once.
                target = buckets[key].setdefault(identity, {field: 0 for field in EVIDENCE_FIELDS})
                for field in EVIDENCE_FIELDS:
                    target[field] = max(target[field], evid[field])

    rollup = []
    for (dimension, value, status), identities in sorted(buckets.items()):
        entry = {"dimension": dimension, "value": value, "status": status,
                 "unique_listings": len(identities)}
        for field in EVIDENCE_FIELDS:
            entry[field] = sum(flags[field] for flags in identities.values())
        rollup.append(entry)
    _write_csv(output / "audit-rollup.csv", rollup,
               ["dimension", "value", "status", "unique_listings", *EVIDENCE_FIELDS])
    _write_csv(output / "listing-audit.csv", listing_rows,
               ["identity_key", "status", "observation", "company", "groups", "tiers", "reasons", "has_description", *EVIDENCE_FIELDS])

    decision_counts = {status: len({r["identity_key"] for r in listing_rows if r["status"] == status})
                       for status in ("match", "review", "exclude")}
    missing_counts = {field: sum(r[field] for r in listing_rows) for field in EVIDENCE_FIELDS}
    summary = {"unique_listings": len({r["identity_key"] for r in listing_rows}),
               "decisions": decision_counts, "missing_evidence": missing_counts,
               "observation_counts": _counts(listing_rows, "observation")}
    lines = ["# Decision audit", "", "Counts use stable unique listing identities. The rollup contains no job descriptions or listing URLs.",
             "", "## Decisions", ""]
    lines.extend(f"- {name.title()}: {count}" for name, count in decision_counts.items())
    lines.extend(["", "## Missing evidence", ""])
    labels = {"missing_location": "Location", "missing_country": "Country",
              "missing_work_mode": "Work mode", "missing_language_evidence": "Language requirement evidence",
              "missing_hours": "Weekly hours", "missing_description": "Description",
              "missing_identity": "Stable identity"}
    lines.extend(f"- {labels[key]}: {value}" for key, value in missing_counts.items())
    lines.extend(["", "## Observation", ""])
    lines.extend(f"- {name}: {count}" for name, count in summary["observation_counts"].items())
    lines.extend(["", "## Rollups", "", "See [audit-rollup.csv](audit-rollup.csv) for all company, group, tier, and reason rows; [listing-audit.csv](listing-audit.csv) contains per-listing evidence flags.", ""])
    totals: dict[tuple[str, str], int] = defaultdict(int)
    for entry in rollup:
        totals[(entry["dimension"], entry["value"])] += entry["unique_listings"]
    for dimension in ("company", "group", "tier", "reason"):
        lines.extend([f"### Top {dimension}s", "", "| Value | Unique listings |", "|---|---:|"])
        selected = sorted(((value, count) for (dim, value), count in totals.items() if dim == dimension),
                          key=lambda pair: (-pair[1], pair[0]))[:10]
        lines.extend(f"| {value.replace('|', '\\|')} | {count} |" for value, count in selected)
        lines.append("")
    (output / "audit-summary.md").write_text("\n".join(lines), encoding="utf-8")
    (output / "audit-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def _counts(rows: list[dict], field: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        key = row.get(field, "") or "not_recorded"
        result[key] = result.get(key, 0) + 1
    return dict(sorted(result.items()))


def write_delivery_receipt(output_dir: str | Path, state: dict, delivery_mode: str = "unknown") -> dict:
    """Write a separate receipt from final persisted state after delivery phase."""
    output = Path(output_dir)
    decision_rows = []
    for status in ("match", "review", "exclude"):
        path = output / f"{status}.csv"
        if path.exists():
            with path.open(newline="", encoding="utf-8-sig") as handle:
                decision_rows.extend(csv.DictReader(handle))

    try:
        run_at = json.loads((output / "summary.json").read_text(encoding="utf-8")).get("run_at", "")
    except (OSError, json.JSONDecodeError):
        run_at = ""
    notification_by_identity: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
    outbox_state_counts: dict[str, int] = defaultdict(int)
    outbox_kind_state_counts: dict[str, int] = defaultdict(int)
    current_run_outbox_counts: dict[str, int] = defaultdict(int)
    for item in state.get("outbox", []):
        payload = item.get("payload", {})
        identities = payload.get("items", [])
        by_chunk = item.get("chunk_item_ids") or []
        statuses = item.get("chunk_statuses", [])
        item_status = item.get("status", "not_recorded")
        item_id = item.get("outbox_id", "")
        outbox_type = "health" if item_id.startswith("health:") else "job_digest"
        outbox_state_counts[item_status] += 1
        outbox_kind_state_counts[f"{outbox_type}:{item_status}"] += 1
        if run_at and str(item.get("created_at", "")) >= run_at:
            current_run_outbox_counts[f"{outbox_type}:{item_status}"] += 1
        period = "current_run" if run_at and str(item.get("created_at", "")) >= run_at else "prior_state"
        if by_chunk and statuses:
            for index, members in enumerate(by_chunk):
                status = statuses[index] if index < len(statuses) else item.get("status", "not_recorded")
                for identity in members:
                    notification_by_identity[identity].append((status, item_id, period))
        else:
            status = item_status
            for entry in identities:
                identity = entry.get("identity_key", "")
                if identity:
                    notification_by_identity[identity].append((status, item_id, period))
    # Tier 1/2 listings waiting for the daily digest are recorded, not yet in the outbox.
    for entry in state.get("digest_queue", []):
        identity = entry.get("identity_key", "")
        if identity:
            period = "current_run" if run_at and str(entry.get("queued_at", "")) >= run_at else "prior_state"
            notification_by_identity[identity].append(("queued_for_digest", "digest-queue", period))

    rows = []
    precedence = {"ambiguous": 5, "failed_manual": 4, "sending": 3, "pending": 2, "queued_for_digest": 2, "sent": 1}
    for raw in decision_rows:
        identity = _row_identity(raw)
        status = raw.get("status", "")
        observation = raw.get("observation", "not_recorded")
        tiers = raw.get("tiers", "")
        outcomes = notification_by_identity.get(identity, [])
        if outcomes:
            delivery_status = max((entry[0] for entry in outcomes), key=lambda x: precedence.get(x, 0))
            selected = [entry for entry in outcomes if entry[0] == delivery_status]
            period = "current_run" if any(entry[2] == "current_run" for entry in selected) else "prior_state"
            notification_reason = "persisted outbox outcome"
        elif status == "exclude":
            delivery_status, period = "not_sent", "none"
            notification_reason = "excluded by current decision rules"
        elif observation == "baseline":
            delivery_status, period = "not_sent", "none"
            notification_reason = "quiet initial baseline"
        elif observation == "prior_seen":
            delivery_status, period = "not_recorded", "prior_state"
            notification_reason = "no matching listing notification remains in retained outbox history"
        elif observation == "newly_seen" and status in {"match", "review"} and tiers.strip():
            delivery_status, period = "not_recorded", "current_run"
            notification_reason = "eligible new listing has no persisted outbox item"
        else:
            delivery_status, period = "not_sent", "none"
            notification_reason = "no eligible listing alert candidate"
        if status == "exclude":
            classification = "excluded"
        elif observation in {"baseline", "prior_seen"}:
            classification = observation
        elif delivery_status in {"pending", "sent", "ambiguous", "sending", "failed_manual", "queued_for_digest"}:
            classification = "newly_eligible"
        elif delivery_status == "not_recorded":
            classification = "not_recorded"
        else:
            classification = "not_eligible"
        rows.append({"identity_key": identity, "decision": status, "observation": observation,
                     "run_classification": classification, "notification_status": delivery_status,
                     "notification_reason": notification_reason, "outbox_period": period,
                     "outbox_ids": "; ".join(sorted({x[1] for x in outcomes if x[1]})),
                     "company": raw.get("company", ""), "groups": raw.get("groups", ""),
                     "tiers": raw.get("tiers", ""), "reasons": raw.get("reasons", "")})
    fields = ["identity_key", "decision", "observation", "run_classification", "notification_status",
              "notification_reason", "outbox_period", "outbox_ids", "company", "groups", "tiers", "reasons"]
    _write_csv(output / "notification-audit.csv", rows, fields)
    counts = _counts(rows, "notification_status")
    reason_counts = _counts(rows, "notification_reason")
    classifications = _counts(rows, "run_classification")
    try:
        collection_summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        collection_summary = {}
    health_counts = {key.split(":", 1)[1]: value for key, value in outbox_kind_state_counts.items()
                     if key.startswith("health:")}
    summary = {"delivery_mode": delivery_mode,
               "run_at": run_at,
               "eligible_job_alert_candidates": collection_summary.get("new_alert_candidates", 0),
               "observation_counts": _counts(rows, "observation"),
               "decision_counts": _counts(rows, "decision"),
               "notification_status_counts": counts,
               "notification_reason_counts": reason_counts,
               "run_classification_counts": classifications,
               "unique_listings": len({r["identity_key"] for r in rows}),
               "outbox_state_counts": dict(sorted(outbox_state_counts.items())),
               "outbox_kind_state_counts": dict(sorted(outbox_kind_state_counts.items())),
               "current_run_outbox_state_counts": dict(sorted(current_run_outbox_counts.items())),
               "health_outbox_state_counts": dict(sorted(health_counts.items())),
               "digest_queue_size": len(state.get("digest_queue", [])),
               "current_run_job_notification_sends": sum(
                   1 for row in rows if row["notification_status"] == "sent" and row["outbox_period"] == "current_run")}
    (output / "delivery-receipt.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# Post-delivery receipt", "", "This receipt reflects watcher state after the delivery phase. It is separate from the collection report uploaded before delivery.",
             f"Run mode: `{delivery_mode}`. `not_sent` means no listing notification was sent for the stated reason; `not_recorded` means a prior or eligible listing has no matching persisted notification record.",
             f"Eligible job alert candidates: {summary['eligible_job_alert_candidates']}; current-run job notification sends recorded: {summary['current_run_job_notification_sends']}.",
             f"Listings waiting for the daily digest: {summary['digest_queue_size']}.",
             "", "## Listing notification states", ""]
    lines.extend(f"- Notification status — {key}: {value}" for key, value in sorted(counts.items()))
    lines.extend(f"- Notification reason — {key}: {value}" for key, value in sorted(reason_counts.items()))
    lines.extend(f"- Run classification — {key}: {value}" for key, value in sorted(classifications.items()))
    lines.extend(["", "## Listing observations", ""])
    lines.extend(f"- {key}: {value}" for key, value in sorted(summary["observation_counts"].items()))
    lines.extend(["", "## Outbox state (all retained items)", ""])
    lines.extend(f"- {key}: {value}" for key, value in sorted(summary["outbox_state_counts"].items()))
    lines.extend(["", "### Outbox type and state", ""])
    lines.extend(f"- {key}: {value}" for key, value in sorted(summary["outbox_kind_state_counts"].items()))
    lines.extend(["", "### Current-run outbox type and state", ""])
    lines.extend(f"- {key}: {value}" for key, value in sorted(summary["current_run_outbox_state_counts"].items()))
    lines.append("")
    (output / "delivery-receipt.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
