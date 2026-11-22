import csv
import json
from pathlib import Path

from pendel import fetch
from pendel.watch import Runner, load_sources, load_state, write_state


def config():
    return {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B2"},
        "minimum_hours_by_tier": {"3": 25},
        "groups": {
            "area-a": {"cities": ["Exampleton"], "tiers": ["1", "2", "3"], "work_modes": ["onsite", "hybrid", "remote"]},
            "area-b": {"cities": ["Sampleburg"], "tiers": ["1", "2", "3"], "work_modes": ["onsite", "hybrid", "remote"]},
            "area-c": {"cities": ["Demoville"], "tiers": ["1", "2", "3"], "work_modes": ["onsite", "hybrid", "remote"]},
            "remote": {"cities": [], "tiers": ["1", "2"], "work_modes": ["remote"]},
        },
        "tiers": {
            "1": {"phrases": ["IT Support", "Service Desk"]},
            "2": {"phrases": ["Customer Support"]},
            "3": {"phrases": ["Lagerhelfer"]},
        },
        "exclusions": {"student": [r"\b(intern|werkstudent)\b"],
                        "senior": [r"\b(senior|lead)\b"],
                        "phone_heavy": [r"\b(call center|telephone support)\b"]},
    }


def source():
    return {"source_id": "greenhouse|https://boards-api.greenhouse.io/v1/boards/acme/jobs",
            "ats": "greenhouse", "token": "acme", "feed_url": "https://boards-api.greenhouse.io/v1/boards/acme/jobs",
            "career_url": "", "companies": "Acme", "source_groups": "", "source_tiers": "", "source_collections": "test"}


def listing(job_id, title="IT Support Specialist", location="Exampleton, Exampleland", mode="onsite"):
    return {"job_id": job_id, "title": title, "location": location, "country": "DE", "work_mode": mode,
            "url": f"https://jobs.example/{job_id}", "posted_at": "2026-11-15", "salary": "",
            "lang_hint": "", "description": "English is required for this role."}


def test_sources_deduplicate_endpoint_and_merge_company_aliases(tmp_path):
    p = tmp_path / "sources.csv"
    fields = ["source_id", "ats", "token", "feed_url", "career_url", "companies", "source_groups", "source_tiers", "source_collections"]
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        w.writerow({"ats": "personio", "feed_url": "https://acme.jobs.personio.de/xml?language=de", "companies": "Acme"})
        w.writerow({"ats": "personio", "feed_url": "https://acme.jobs.personio.de/xml", "companies": "Acme GmbH"})
        w.writerow({"ats": "personio", "feed_url": "https://acme.jobs.personio.de/xml?includeCompensation=true", "companies": "Acme Services"})
    rows = load_sources(p)
    assert len(rows) == 1
    assert rows[0]["companies"] == "Acme; Acme GmbH; Acme Services"
    assert len(rows[0]["feed_urls"]) == 2
    assert any("language=de" in url for url in rows[0]["feed_urls"])
    assert not any("includeCompensation" in url for url in rows[0]["feed_urls"])


def test_source_merge_fills_career_url_from_a_later_alias(tmp_path):
    p = tmp_path / "sources.csv"
    fields = ["source_id", "ats", "token", "feed_url", "career_url", "companies", "source_groups", "source_tiers", "source_collections"]
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader()
        w.writerow({"ats": "greenhouse", "token": "acme", "feed_url": "https://feed.example/jobs", "companies": "Acme"})
        w.writerow({"ats": "greenhouse", "token": "acme", "feed_url": "https://feed.example/jobs?content=true",
                    "career_url": "https://acme.example/careers", "companies": "Acme Ltd"})
    rows = load_sources(p)
    assert len(rows) == 1
    assert rows[0]["career_url"] == "https://acme.example/careers"


def test_job_identity_prefers_provider_tenant_and_stable_listing_id():
    from pendel.watch import identity_key
    src = {"ats": "greenhouse", "token": "Board-X", "feed_url": "https://feed.example/board"}
    first = identity_key(src, {"job_id": "42", "url": "https://jobs.example/old-title"})
    changed_slug = identity_key(src, {"job_id": "42", "url": "https://jobs.example/new-title"})
    assert first == changed_slug == "id:greenhouse|board-x|42"
    fallback = identity_key(src, {"job_id": "", "url": "https://jobs.example/role?utm_source=feed"})
    assert fallback == "url:https://jobs.example/role"


def test_feed_query_options_replace_existing_values():
    from pendel.fetch import with_query
    assert with_query("https://feed.example/jobs?limit=20&offset=40", "limit=100&offset=0") == \
           "https://feed.example/jobs?limit=100&offset=0"


def test_source_import_preserves_content_variants_and_merges_their_jobs(monkeypatch):
    seen_urls = []
    def fetch_variant(src, _throttle):
        seen_urls.append(src["feed_url"])
        suffix = "de" if "language=de" in src["feed_url"] else "en"
        return [listing(f"job-{suffix}")]
    monkeypatch.setattr(fetch, "fetch_jobs", fetch_variant)
    src = source() | {
        "feed_urls": ["https://acme.jobs.personio.de/xml?language=de",
                      "https://acme.jobs.personio.de/xml?language=en"]
    }
    current, statuses = Runner(config(), {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}).collect([src])
    assert len(seen_urls) == 2
    assert len(current) == 2
    assert statuses[0]["complete"] is True


def test_initial_success_is_silent_baseline_then_new_job_is_queued(monkeypatch, tmp_path):
    fetched = [listing("one")]
    monkeypatch.setattr(fetch, "fetch_jobs", lambda _source, _throttle: list(fetched))
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}
    runner = Runner(config(), state)
    assert runner.prepare([source()], tmp_path / "first") == 0
    assert state["outbox"] == []
    assert state["sources"][source()["source_id"]]["baseline_complete"] is True
    assert json.loads((tmp_path / "first/summary.json").read_text())["silent_baseline_listings"] == 1
    snapshot = list(csv.DictReader((tmp_path / "first/input-snapshot.csv").open()))
    assert snapshot[0]["description"] == "English is required for this role."
    assert snapshot[0]["identity_key"].startswith("id:greenhouse|acme|")
    assert "delivery outcomes are not recorded" in (tmp_path / "first/SUMMARY.md").read_text()

    fetched.append(listing("two", title="Service Desk Agent", location="Demoville, Germany"))
    assert runner.prepare([source()], tmp_path / "second") == 0
    second_summary = json.loads((tmp_path / "second/summary.json").read_text())
    assert second_summary["newly_seen_listings"] == 1
    assert second_summary["new_alert_candidates"] == 1
    assert len(state["outbox"]) == 1
    assert state["outbox"][0]["status"] == "pending"
    assert len(state["outbox"][0]["payload"]["items"]) == 1
    rows = list(csv.DictReader((tmp_path / "second/match.csv").open()))
    assert {r["company"] for r in rows} == {"Acme"}


def test_failed_or_partial_source_does_not_complete_baseline(monkeypatch, tmp_path):
    def partial(*_args):
        raise fetch.IncompleteFeed("pagination capped", [listing("partial")])
    monkeypatch.setattr(fetch, "fetch_jobs", partial)
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}
    runner = Runner(config(), state)
    runner.prepare([source()], tmp_path / "partial")
    status = state["sources"][source()["source_id"]]
    assert status["baseline_complete"] is False
    assert not state["jobs"]
    assert state["outbox"] == []  # one failed run is a blip, not a notice
    runner.prepare([source()], tmp_path / "partial-again")
    assert len(state["outbox"]) == 1
    assert "feed health notice" in state["outbox"][0]["payload"]["chunks"][0].lower()


def test_structured_hours_survive_collection_for_audit_and_tier_filtering(monkeypatch, tmp_path):
    item = listing("short-hours", title="Lagerhelfer", location="Exampleton, Germany")
    item["hours_per_week"] = 20
    monkeypatch.setattr(fetch, "fetch_jobs", lambda *_args: [item])
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}
    Runner(config(), state).prepare([source()], tmp_path / "hours")
    excluded = list(csv.DictReader((tmp_path / "hours/exclude.csv").open()))
    assert excluded[0]["hours_status"] == "hours_listed"
    assert excluded[0]["hours_per_week"] == "20"
    snapshot = list(csv.DictReader((tmp_path / "hours/input-snapshot.csv").open()))
    assert snapshot[0]["hours_per_week"] == "20"


def test_structured_hours_accept_numeric_and_string_values():
    from pendel.targets import _hours
    assert _hours({"hours_per_week": 32}) == ("hours_listed", 32, 32)
    assert _hours({"hours_per_week": 32.0}) == ("hours_listed", 32, 32)
    assert _hours({"hours_per_week": "32"}) == ("hours_listed", 32, 32)


def test_feed_health_notice_is_quiet_until_change_and_reports_recovery(monkeypatch, tmp_path):
    def offline(*_args):
        raise OSError("feed unavailable")

    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}
    runner = Runner(config(), state)
    monkeypatch.setattr(fetch, "fetch_jobs", offline)
    runner.prepare([source()], tmp_path / "failure-one")
    assert state["outbox"] == []
    runner.prepare([source()], tmp_path / "failure-two")
    assert len(state["outbox"]) == 1
    runner.prepare([source()], tmp_path / "failure-three")
    assert len(state["outbox"]) == 1

    monkeypatch.setattr(fetch, "fetch_jobs", lambda *_args: [])
    runner.prepare([source()], tmp_path / "recovered")
    assert len(state["outbox"]) == 2
    assert "recovered" in state["outbox"][1]["payload"]["chunks"][0].lower()

    monkeypatch.setattr(fetch, "fetch_jobs", offline)
    runner.prepare([source()], tmp_path / "blip")
    assert len(state["outbox"]) == 2
    runner.prepare([source()], tmp_path / "failure-again")
    assert len(state["outbox"]) == 3
    assert "feed health notice" in state["outbox"][2]["payload"]["chunks"][0].lower()


def test_partial_source_does_not_prevent_later_source_from_being_processed(monkeypatch, tmp_path):
    srcs = [source(), source() | {"source_id": "greenhouse|second", "token": "second", "companies": "Second"}]
    def fetch_by_source(src, _throttle):
        if src["token"] == "acme":
            raise fetch.IncompleteFeed("pagination capped", [listing("partial")])
        return [listing("complete")]
    monkeypatch.setattr(fetch, "fetch_jobs", fetch_by_source)
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}
    Runner(config(), state).prepare(srcs, tmp_path / "mixed")
    assert state["sources"][srcs[0]["source_id"]]["last_status"] == "partial"
    assert state["sources"][srcs[1]["source_id"]]["baseline_complete"] is True
    assert len(state["jobs"]) == 1


def test_excluded_listing_never_enters_notification_outbox(monkeypatch, tmp_path):
    fetched = [listing("one")]
    monkeypatch.setattr(fetch, "fetch_jobs", lambda _source, _throttle: list(fetched))
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}
    runner = Runner(config(), state)
    runner.prepare([source()], tmp_path / "baseline")
    fetched.append(listing("two", title="Senior IT Support Specialist"))
    runner.prepare([source()], tmp_path / "next")
    assert state["outbox"] == []


def test_new_tier_match_with_unknown_location_is_queued_for_review(monkeypatch, tmp_path):
    fetched = [listing("one")]
    monkeypatch.setattr(fetch, "fetch_jobs", lambda _source, _throttle: list(fetched))
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}
    runner = Runner(config(), state)
    runner.prepare([source()], tmp_path / "baseline")
    fetched.append(listing("two", title="Customer Support Specialist", location="", mode=""))
    fetched[-1]["country"] = ""
    runner.prepare([source()], tmp_path / "new")
    assert len(state["outbox"]) == 1
    item = state["outbox"][0]["payload"]["items"][0]
    assert item["status"] == "review"
    assert item["tiers"] == ["2"]
    assert not item["groups"]
    assert "does not identify" in " ".join(item["reasons"])


def test_state_roundtrip_restores_compact_tables(tmp_path):
    state_path, db_path = tmp_path / "state.json", tmp_path / "runtime.duckdb"
    state = {"schema_version": 1, "jobs": {"url:https://jobs.example/1": {
                "first_seen": "2026-11-15T12:00:00Z", "last_seen": "2026-11-15T12:00:00Z", "source_ids": ["s1"]}},
             "sources": {"s1": {"baseline_complete": True, "last_success": "2026-11-15T12:00:00Z",
                                  "last_status": "ok", "last_error": ""}},
             "outbox": [{"outbox_id": "d1", "status": "sent", "created_at": "2026-11-15T12:00:00Z",
                         "updated_at": "2026-11-15T12:00:00Z", "attempts": 1, "payload": {"items": []}, "error": ""}]}
    write_state(state_path, state, db_path)
    loaded = load_state(state_path)
    assert loaded["jobs"]["url:https://jobs.example/1"]["source_ids"] == ["s1"]
    import duckdb
    con = duckdb.connect(str(db_path), read_only=True)
    assert con.execute("SELECT count(*) FROM watch_jobs").fetchone()[0] == 1
    assert con.execute("SELECT status FROM watch_outbox").fetchone()[0] == "sent"
    con.close()


def test_claim_and_delivery_are_off_without_explicit_authorization(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("PENDEL_TELEGRAM_APPROVED", raising=False)
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": [
        {"outbox_id": "d1", "status": "pending", "created_at": "2026-11-15T12:00:00Z",
         "updated_at": "", "attempts": 0, "payload": {"items": []}, "error": ""}]}
    runner = Runner(config(), state)
    assert runner.claim() == 0
    assert state["outbox"][0]["status"] == "pending"
    assert runner.deliver(allow_send=True) == 0
    assert state["outbox"][0]["status"] == "pending"


def test_telegram_digest_chunks_keep_every_listing_and_fit_limit():
    items = []
    for i in range(30):
        items.append({"identity_key": f"job-{i}", "title": f"Support listing {i}", "company": "Example",
                      "url": f"https://jobs.example/{i}", "status": "match", "groups": ["area"], "tiers": ["1"]})
    chunks = Runner._telegram_chunks({"items": items}, max_chars=900)
    assert len(chunks) > 1
    assert all(len(chunk) <= 900 for chunk in chunks)
    combined = "\n".join(chunks)
    assert all(f"Support listing {i}" in combined for i in range(30))
    mapped_chunks, members = Runner._telegram_chunks_with_members({"items": items}, max_chars=900)
    assert mapped_chunks == chunks
    assert set(identity for chunk in members for identity in chunk) == {f"job-{i}" for i in range(30)}


def test_audit_rollups_and_receipt_count_unique_listing_statuses_without_descriptions(tmp_path):
    from pendel.audit import write_delivery_receipt, write_rollups

    fields = ["status", "groups", "tiers", "company", "title", "location", "work_mode", "country",
              "language_requirement_status", "hours_status", "reasons", "url", "identity_key", "observation"]
    rows = {
        "match": [{"status": "match", "groups": "area-a", "tiers": "1", "company": "Acme", "title": "A",
                   "location": "Exampleton", "work_mode": "onsite", "country": "DE",
                   "language_requirement_status": "explicit", "hours_status": "not_applicable", "reasons": "",
                   "url": "https://jobs.example/a", "identity_key": "id:a", "observation": "newly_seen"}],
        "review": [{"status": "review", "groups": "area-a", "tiers": "1", "company": "Acme", "title": "B",
                    "location": "", "work_mode": "", "country": "", "language_requirement_status": "language_unclear",
                    "hours_status": "hours_unclear", "reasons": "location unclear; work mode unclear", "url": "https://jobs.example/b",
                    "identity_key": "id:b", "observation": "baseline"}],
        "exclude": [{"status": "exclude", "groups": "", "tiers": "", "company": "Other", "title": "C",
                     "location": "Foreign", "work_mode": "onsite", "country": "US", "language_requirement_status": "explicit",
                     "hours_status": "not_applicable", "reasons": "outside Germany", "url": "https://jobs.example/c",
                     "identity_key": "id:c", "observation": "newly_seen"}],
    }
    for status, values in rows.items():
        with (tmp_path / f"{status}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(values)
    summary = write_rollups(tmp_path, rows, [{"identity_key": "id:a", "description": "private input text"}])
    assert summary["unique_listings"] == 3
    assert summary["missing_evidence"]["missing_location"] == 1
    assert summary["missing_evidence"]["missing_description"] == 2
    rollup = list(csv.DictReader((tmp_path / "audit-rollup.csv").open()))
    area_match = next(row for row in rollup if row["dimension"] == "group" and row["value"] == "area-a" and row["status"] == "match")
    assert area_match["unique_listings"] == "1"
    assert "private input text" not in (tmp_path / "audit-summary.md").read_text()

    state = {"outbox": [
        {"outbox_id": "digest-1", "status": "sent", "payload": {"items": [{"identity_key": "id:a"}]},
         "chunk_statuses": ["sent"], "chunk_item_ids": [["id:a"]]},
    ]}
    receipt = write_delivery_receipt(tmp_path, state, "no_send")
    assert receipt["notification_status_counts"] == {"not_sent": 2, "sent": 1}
    assert receipt["run_classification_counts"] == {"baseline": 1, "excluded": 1, "newly_eligible": 1}
    assert receipt["delivery_mode"] == "no_send"
    assert receipt["observation_counts"] == {"baseline": 1, "newly_seen": 2}
    receipt_text = (tmp_path / "notification-audit.csv").read_text()
    assert "description" not in receipt_text.lower()
    assert "synthetic-token" not in receipt_text


def test_receipt_marks_ambiguous_pending_and_unrecorded_new_items(tmp_path):
    from pendel.audit import write_delivery_receipt
    fields = ["status", "company", "identity_key", "observation", "groups", "tiers", "reasons", "url"]
    rows = [
        {"status": "review", "company": "A", "identity_key": "pending", "observation": "newly_seen"},
        {"status": "review", "company": "B", "identity_key": "uncertain", "observation": "newly_seen"},
        {"status": "review", "company": "C", "identity_key": "absent", "observation": "newly_seen"},
        {"status": "review", "company": "D", "identity_key": "prior", "observation": "prior_seen"},
    ]
    with (tmp_path / "review.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(rows)
    for status in ("match", "exclude"):
        (tmp_path / f"{status}.csv").write_text("\n")
    state = {"outbox": [
        {"outbox_id": "p", "status": "pending", "payload": {"items": [{"identity_key": "pending"}]},
         "chunk_statuses": ["pending"], "chunk_item_ids": [["pending"]]},
        {"outbox_id": "a", "status": "ambiguous", "payload": {"items": [{"identity_key": "uncertain"}]},
         "chunk_statuses": ["ambiguous"], "chunk_item_ids": [["uncertain"]]},
    ]}
    receipt = write_delivery_receipt(tmp_path, state, "send_authorized")
    assert receipt["notification_status_counts"] == {
        "ambiguous": 1, "not_recorded": 1, "not_sent": 1, "pending": 1,
    }
    assert receipt["run_classification_counts"] == {
        "newly_eligible": 2, "not_eligible": 1, "prior_seen": 1,
    }
    assert receipt["notification_reason_counts"] == {
        "no eligible listing alert candidate": 1,
        "no matching listing notification remains in retained outbox history": 1,
        "persisted outbox outcome": 2,
    }


def test_receipt_separates_current_job_outbox_from_historical_health_notices(tmp_path):
    from pendel.audit import write_delivery_receipt
    fields = ["status", "company", "identity_key", "observation", "groups", "tiers", "reasons", "url"]
    with (tmp_path / "review.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
        writer.writerow({"status": "review", "company": "Acme", "identity_key": "job-1",
                         "observation": "newly_seen", "tiers": "1"})
    for status in ("match", "exclude"):
        (tmp_path / f"{status}.csv").write_text("\n")
    (tmp_path / "summary.json").write_text(json.dumps({"run_at": "2026-11-16T05:00:00Z", "new_alert_candidates": 1}))
    state = {"outbox": [
        {"outbox_id": "health:old", "status": "sent", "created_at": "2026-11-16T04:00:00Z",
         "payload": {"items": [], "chunks": ["health"]}},
        {"outbox_id": "digest:new", "status": "pending", "created_at": "2026-11-16T05:00:00Z",
         "payload": {"items": [{"identity_key": "job-1"}], "chunks": ["job"]},
         "chunk_statuses": ["pending"], "chunk_item_ids": [["job-1"]]},
    ]}
    receipt = write_delivery_receipt(tmp_path, state, "no_send")
    assert receipt["eligible_job_alert_candidates"] == 1
    assert receipt["current_run_job_notification_sends"] == 0
    assert receipt["health_outbox_state_counts"] == {"sent": 1}
    assert receipt["current_run_outbox_state_counts"] == {"job_digest:pending": 1}
    row = next(csv.DictReader((tmp_path / "notification-audit.csv").open()))
    assert row["notification_status"] == "pending"
    assert row["outbox_period"] == "current_run"


def test_claim_advances_one_persisted_chunk_at_a_time(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "set-but-never-used")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "private-id")
    monkeypatch.setenv("PENDEL_TELEGRAM_APPROVED", "true")
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": [
        {"outbox_id": "d1", "status": "pending", "created_at": "2026-11-15T12:00:00Z",
         "updated_at": "", "attempts": 0, "chunk_index": None,
         "chunk_statuses": ["pending", "pending"],
         "payload": {"items": [], "chunks": ["first", "second"]}, "error": ""}]}
    runner = Runner(config(), state)
    runner.claim()
    item = state["outbox"][0]
    assert item["chunk_index"] == 0 and item["chunk_statuses"] == ["sending", "pending"]
    item["chunk_statuses"][0] = "sent"
    item["status"] = "pending"
    runner.claim()
    assert item["chunk_index"] == 1 and item["chunk_statuses"] == ["sent", "sending"]


def test_ambiguous_outbox_stops_later_claims(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "set-but-never-used")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "private-id")
    monkeypatch.setenv("PENDEL_TELEGRAM_APPROVED", "true")
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": [
        {"outbox_id": "uncertain", "status": "ambiguous", "chunk_statuses": ["ambiguous"],
         "payload": {"chunks": ["possibly delivered"]}},
        {"outbox_id": "later", "status": "pending", "chunk_statuses": ["pending"],
         "payload": {"chunks": ["do not send"]}},
    ]}
    Runner(config(), state).claim()
    assert state["outbox"][1]["status"] == "pending"
    assert state["outbox"][1]["chunk_statuses"] == ["pending"]


def test_delivery_ack_persists_success_without_network(monkeypatch):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            return False
        def read(self):
            return b'{"ok": true}'
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic-chat")
    monkeypatch.setenv("PENDEL_TELEGRAM_APPROVED", "true")
    monkeypatch.setattr("pendel.watch.urllib.request.urlopen", lambda *_args, **_kwargs: Response())
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": [
        {"outbox_id": "synthetic", "status": "sending", "chunk_index": 0,
         "chunk_statuses": ["sending"], "payload": {"chunks": ["synthetic digest"]},
         "attempts": 1, "created_at": "2026-11-15T12:00:00Z", "updated_at": "", "error": ""}]}
    Runner(config(), state).deliver(allow_send=True)
    assert state["outbox"][0]["status"] == "sent"
    assert state["outbox"][0]["chunk_statuses"] == ["sent"]


def test_uncertain_delivery_is_ambiguous_and_not_resent(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic-chat")
    monkeypatch.setenv("PENDEL_TELEGRAM_APPROVED", "true")
    monkeypatch.setattr("pendel.watch.urllib.request.urlopen",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()))
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": [
        {"outbox_id": "synthetic", "status": "sending", "chunk_index": 0,
         "chunk_statuses": ["sending"], "payload": {"chunks": ["synthetic digest"]},
         "attempts": 1, "created_at": "2026-11-15T12:00:00Z", "updated_at": "", "error": ""}]}
    Runner(config(), state).deliver(allow_send=True)
    assert state["outbox"][0]["status"] == "ambiguous"
    Runner(config(), state).claim()
    assert state["outbox"][0]["chunk_statuses"] == ["ambiguous"]


def test_restart_after_claim_turns_inflight_chunk_ambiguous(tmp_path, monkeypatch):
    from pendel.watch import write_state
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic-chat")
    monkeypatch.setenv("PENDEL_TELEGRAM_APPROVED", "true")
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": [
        {"outbox_id": "synthetic", "status": "pending", "chunk_index": None,
         "chunk_statuses": ["pending"], "payload": {"chunks": ["synthetic digest"]},
         "attempts": 0, "created_at": "2026-11-15T12:00:00Z", "updated_at": "", "error": ""}]}
    Runner(config(), state).claim()
    state_file, db_file = tmp_path / "state.json", tmp_path / "state.duckdb"
    write_state(state_file, state, db_file)
    restored = load_state(state_file)
    Runner(config(), restored).claim()
    assert restored["outbox"][0]["status"] == "ambiguous"
    assert restored["outbox"][0]["chunk_statuses"] == ["ambiguous"]


def test_load_state_fails_closed_when_state_is_missing(tmp_path):
    from pendel.watch import load_state
    try:
        load_state(tmp_path / "missing.json")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("missing persisted state must not silently become a fresh baseline")


def delivery_config():
    return config() | {"delivery": {"instant_tiers": ["3"], "digest_hour": 18, "timezone": "Europe/Berlin",
                                    "weekly_day": 6}}


def utc(text):
    from datetime import datetime
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def test_tier3_is_sent_instantly_and_tier12_waits_for_the_evening_digest(monkeypatch, tmp_path):
    fetched = [listing("one")]
    monkeypatch.setattr(fetch, "fetch_jobs", lambda _source, _throttle: list(fetched))
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}
    now = [utc("2026-11-18T09:47:00Z")]
    runner = Runner(delivery_config(), state, clock=lambda: now[0])
    runner.prepare([source()], tmp_path / "baseline")

    fetched += [listing("two", title="Lagerhelfer (m/w/d)"), listing("three", title="Service Desk Agent")]
    runner.prepare([source()], tmp_path / "morning")
    assert [item["outbox_id"].split(":")[0] for item in state["outbox"]] == ["instant"]
    instant = state["outbox"][0]
    assert len(instant["payload"]["chunks"]) == 1
    assert instant["payload"]["chunks"][0].startswith("🟢 T3 · Lagerhelfer")
    assert "not stated" not in instant["payload"]["chunks"][0]
    assert "Lagerhelfer" in instant["payload"]["chunks"][0]
    assert instant["chunk_item_ids"] == [[instant["payload"]["items"][0]["identity_key"]]]
    assert [x["title"] for x in state["digest_queue"]] == ["Service Desk Agent"]

    now[0] = utc("2026-11-18T17:47:00Z")
    runner.prepare([source()], tmp_path / "evening")
    daily = state["outbox"][-1]
    assert daily["outbox_id"].startswith("daily:")
    assert "TIER 1 · 1" in daily["payload"]["chunks"][0]
    assert "Instant Tier 3 alerts today: 1" in daily["payload"]["chunks"][0]
    assert state["digest_queue"] == []
    assert state["last_digest_date"] == "2026-11-18"

    now[0] = utc("2026-11-18T19:47:00Z")
    runner.prepare([source()], tmp_path / "late")
    assert len(state["outbox"]) == 2


def test_listing_in_tier3_and_another_tier_is_sent_once_instantly(monkeypatch, tmp_path):
    fetched = [listing("one")]
    monkeypatch.setattr(fetch, "fetch_jobs", lambda _source, _throttle: list(fetched))
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}
    runner = Runner(delivery_config(), state, clock=lambda: utc("2026-11-18T09:47:00Z"))
    runner.prepare([source()], tmp_path / "baseline")
    fetched.append(listing("two", title="IT Support Lagerhelfer"))
    runner.prepare([source()], tmp_path / "next")
    assert len(state["outbox"]) == 1 and state["outbox"][0]["outbox_id"].startswith("instant:")
    assert state["digest_queue"] == []


def test_digest_hour_follows_berlin_time_across_daylight_saving():
    queued = [{"identity_key": "job-1", "title": "Service Desk Agent", "company": "Acme",
               "status": "match", "groups": ["area-a"], "tiers": ["1"], "url": "https://jobs.example/1"}]
    for when, due in (("2026-07-01T16:30:00Z", True),    # 18:30 CEST
                      ("2026-11-18T16:30:00Z", False),   # 17:30 CET
                      ("2026-11-18T17:00:00Z", True)):   # 18:00 CET
        state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": [], "digest_queue": list(queued)}
        runner = Runner(delivery_config(), state, clock=lambda when=when: utc(when))
        runner._queue_alerts([], when)
        assert bool(state["outbox"]) is due, when


def test_without_delivery_config_every_run_still_sends_one_combined_digest(monkeypatch, tmp_path):
    fetched = [listing("one")]
    monkeypatch.setattr(fetch, "fetch_jobs", lambda _source, _throttle: list(fetched))
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}
    runner = Runner(config(), state, clock=lambda: utc("2026-11-18T09:47:00Z"))
    runner.prepare([source()], tmp_path / "baseline")
    fetched += [listing("two", title="Lagerhelfer"), listing("three", title="Service Desk Agent")]
    runner.prepare([source()], tmp_path / "next")
    assert len(state["outbox"]) == 1 and state["outbox"][0]["outbox_id"].startswith("digest:")
    assert len(state["outbox"][0]["payload"]["items"]) == 2
    assert "digest_queue" not in state


def test_daily_digest_chunks_keep_every_listing_and_fit_limit():
    items = [{"identity_key": f"job-{i}", "title": f"Support listing {i}", "company": "Example",
              "url": f"https://jobs.example/{i}", "status": "match", "groups": ["area"], "tiers": [str(1 + i % 2)]}
             for i in range(40)]
    chunks, members = Runner._daily_digest_chunks(items, utc("2026-11-18T17:47:00Z"), 3, max_chars=900)
    assert len(chunks) > 1
    assert all(len(chunk) <= 900 for chunk in chunks)
    assert {identity for chunk in members for identity in chunk} == {f"job-{i}" for i in range(40)}
    assert chunks[-1].endswith("Instant Tier 3 alerts today: 3")


def test_receipt_shows_listings_waiting_for_the_digest(tmp_path):
    from pendel.audit import write_delivery_receipt
    fields = ["status", "company", "identity_key", "observation", "groups", "tiers", "reasons", "url"]
    with (tmp_path / "match.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
        writer.writerow({"status": "match", "company": "A", "identity_key": "waiting",
                         "observation": "newly_seen", "tiers": "1"})
    for status in ("review", "exclude"):
        (tmp_path / f"{status}.csv").write_text("\n")
    state = {"outbox": [], "digest_queue": [{"identity_key": "waiting", "queued_at": "2026-11-18T09:47:00Z"}]}
    receipt = write_delivery_receipt(tmp_path, state, "no_send")
    assert receipt["notification_status_counts"] == {"queued_for_digest": 1}
    assert receipt["run_classification_counts"] == {"newly_eligible": 1}
    assert receipt["digest_queue_size"] == 1


def test_weekly_summary_is_sent_once_on_sunday_evening_with_seven_day_counts():
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": [],
             "instant_alerts_by_day": {"2026-11-20": 4, "2026-11-10": 99},
             "digest_items_by_day": {"2026-11-20": 3},
             "new_jobs_by_group_by_day": {"2026-11-20": {"area-a": 5, "area-b": 2}},
             "runs_by_day": {"2026-11-20": 3},
             "source_failures_by_day": {"2026-11-20": {"s-bad": 3, "s-ok": 1}}}
    statuses = [{"source_id": "s-bad", "companies": "Bad Co", "ats": "personio", "complete": False},
                {"source_id": "s-ok", "companies": "Ok Co", "ats": "lever", "complete": True}]
    now = [utc("2026-11-21T17:47:00Z")]  # Saturday 18:47 CET
    runner = Runner(delivery_config(), state, clock=lambda: now[0])
    runner._queue_alerts([], "2026-11-21T17:47:00Z", statuses)
    assert not any(x["outbox_id"].startswith("weekly:") for x in state["outbox"])

    now[0] = utc("2026-11-22T17:47:00Z")  # Sunday 18:47 CET
    runner._queue_alerts([], "2026-11-22T17:47:00Z", statuses)
    weekly = [x for x in state["outbox"] if x["outbox_id"].startswith("weekly:")]
    assert len(weekly) == 1
    text = weekly[0]["payload"]["chunks"][0]
    assert text.startswith("📊 WEEKLY · Sun 22 Nov")
    assert "Instant alerts sent: 4" in text
    assert "Jobs in daily digests: 3" in text
    assert "Best group this week: area-a (5)" in text
    assert "Bad Co (personio) 5/5" in text
    assert "Ok Co" not in text

    runner._queue_alerts([], "2026-11-22T19:47:00Z", statuses)
    assert len([x for x in state["outbox"] if x["outbox_id"].startswith("weekly:")]) == 1


def test_prepare_records_new_jobs_per_group_and_runs_per_day(monkeypatch, tmp_path):
    fetched = [listing("one")]
    monkeypatch.setattr(fetch, "fetch_jobs", lambda _source, _throttle: list(fetched))
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}
    runner = Runner(delivery_config(), state, clock=lambda: utc("2026-11-18T09:47:00Z"))
    runner.prepare([source()], tmp_path / "baseline")
    fetched.append(listing("two", title="Service Desk Agent"))
    runner.prepare([source()], tmp_path / "next")
    assert state["runs_by_day"] == {"2026-11-18": 2}
    assert state["new_jobs_by_group_by_day"]["2026-11-18"] == {"area-a": 1}
    assert state["source_failures_by_day"]["2026-11-18"] == {}


def provider_source(company, host):
    url = f"https://{company}.{host}/feed"
    return source() | {"source_id": f"x|{url}", "token": company, "feed_url": url, "companies": company}


def mixed_sources():
    return [provider_source("alpha", "personio.de"), provider_source("beta", "greenhouse.io"),
            provider_source("gamma", "personio.de"), provider_source("delta", "lever.co")]


def test_parallel_collect_matches_sequential_collect(monkeypatch):
    def fake_fetch(src, _throttle):
        return [listing(f"{src['token']}-{n}") for n in range(2)]
    monkeypatch.setattr(fetch, "fetch_jobs", fake_fetch)
    sequential = Runner(config(), {"jobs": {}, "sources": {}, "outbox": []}).collect(mixed_sources())
    parallel = Runner(config(), {"jobs": {}, "sources": {}, "outbox": []}, workers=8).collect(mixed_sources())
    assert parallel == sequential
    assert [s["source_id"] for s in parallel[1]] == [s["source_id"] for s in mixed_sources()]


def test_parallel_lanes_overlap_across_providers(monkeypatch):
    import threading
    barrier = threading.Barrier(3, timeout=5)

    def fake_fetch(src, _throttle):
        if src["token"] in {"alpha", "beta", "delta"}:
            barrier.wait()  # only passes if three providers are being fetched at the same time
        return []
    monkeypatch.setattr(fetch, "fetch_jobs", fake_fetch)
    _current, statuses = Runner(config(), {"jobs": {}, "sources": {}, "outbox": []}, workers=8).collect(mixed_sources())
    assert [s["status"] for s in statuses] == ["empty"] * 4


def test_rate_limit_skips_only_the_same_provider_in_parallel(monkeypatch):
    seen = []

    def fake_fetch(src, _throttle):
        seen.append(src["token"])
        if src["token"] == "alpha":
            raise fetch.RateLimited("alpha")
        return [listing(src["token"])]
    monkeypatch.setattr(fetch, "fetch_jobs", fake_fetch)
    _current, statuses = Runner(config(), {"jobs": {}, "sources": {}, "outbox": []}, workers=8).collect(mixed_sources())
    by_company = {s["companies"]: s["status"] for s in statuses}
    assert by_company == {"alpha": "rate_limited", "beta": "ok", "gamma": "skipped_after_rate_limit", "delta": "ok"}
    assert "gamma" not in seen


def test_shared_throttle_spaces_same_provider_but_not_others(monkeypatch):
    import threading
    from pendel.watch import SharedThrottle
    clock = [100.0]
    sleeps = []
    monkeypatch.setattr("pendel.watch.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("pendel.watch.time.sleep", lambda s: (sleeps.append(round(s, 2)), clock.__setitem__(0, clock[0] + s)))
    throttle = SharedThrottle(2.0)
    throttle.wait("https://a.jobs.personio.de/xml")
    throttle.wait("https://boards-api.greenhouse.io/v1/boards/x/jobs")
    throttle.wait("https://b.jobs.personio.de/xml")
    assert sleeps == [2.0]
    threads = [threading.Thread(target=throttle.wait, args=("https://c.jobs.personio.de/xml",)) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sleeps == [2.0, 2.0, 2.0, 2.0]


def test_connection_dropped_mid_response_fails_only_that_source(monkeypatch):
    import http.client
    other = source() | {"source_id": "greenhouse|other", "token": "other",
                        "feed_url": "https://boards-api.greenhouse.io/v1/boards/other/jobs", "companies": "Other"}

    def fetch_jobs(feed, _throttle):
        if feed["token"] == "acme":
            raise http.client.IncompleteRead(b"partial", 100)
        return [listing("two")]
    monkeypatch.setattr(fetch, "fetch_jobs", fetch_jobs)
    current, statuses = Runner(config(), {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}).collect([source(), other])
    assert [(s["source_id"], s["status"], s["complete"]) for s in statuses] == [
        (source()["source_id"], "network_error", False), ("greenhouse|other", "ok", True)]
    assert statuses[0]["error"] == "IncompleteRead"
    assert len(current) == 1


def _sending_state():
    return {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": [
        {"outbox_id": "synthetic", "status": "sending", "chunk_index": 0,
         "chunk_statuses": ["sending"], "payload": {"chunks": ["synthetic digest"]},
         "attempts": 1, "created_at": "2026-11-15T12:00:00Z", "updated_at": "", "error": ""}]}


def _raise_http(code, body=b'{"ok": false}'):
    import io
    import urllib.error

    def opener(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://api.telegram.org", code, "error", {}, io.BytesIO(body))
    return opener


def test_telegram_error_answer_is_not_delivered_rather_than_ambiguous(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic-chat")
    monkeypatch.setenv("PENDEL_TELEGRAM_APPROVED", "true")
    monkeypatch.setattr("pendel.watch.urllib.request.urlopen", _raise_http(400))
    state = _sending_state()
    Runner(config(), state).deliver(allow_send=True)
    assert state["outbox"][0]["status"] == "failed_manual"
    assert "HTTP 400" in state["outbox"][0]["error"]


def test_telegram_rate_limit_waits_and_leaves_the_chunk_pending(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic-chat")
    monkeypatch.setenv("PENDEL_TELEGRAM_APPROVED", "true")
    monkeypatch.setattr("pendel.watch.urllib.request.urlopen",
                        _raise_http(429, b'{"ok": false, "parameters": {"retry_after": 3}}'))
    slept = []
    monkeypatch.setattr("pendel.watch.time.sleep", slept.append)
    state = _sending_state()
    Runner(config(), state).deliver(allow_send=True)
    assert slept == [3]
    assert state["outbox"][0]["status"] == "pending"
    assert state["outbox"][0]["chunk_statuses"] == ["pending"]


def test_ambiguous_error_names_the_exception(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic-chat")
    monkeypatch.setenv("PENDEL_TELEGRAM_APPROVED", "true")
    monkeypatch.setattr("pendel.watch.urllib.request.urlopen",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()))
    state = _sending_state()
    Runner(config(), state).deliver(allow_send=True)
    assert "(TimeoutError)" in state["outbox"][0]["error"]


def late_config(tier3_phrases=("Lagerhelfer",)):
    cfg = delivery_config()
    cfg["delivery"]["late_match_days"] = 7
    cfg["tiers"] = cfg["tiers"] | {"3": {"phrases": list(tier3_phrases)}}
    return cfg


def test_recent_job_that_only_now_matches_is_sent_once(monkeypatch, tmp_path):
    monkeypatch.setattr(fetch, "fetch_jobs", lambda _s, _t: [listing("k", title="Kommissionierer (m/w/d)")])
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}
    runner = Runner(late_config(), state, clock=lambda: utc("2026-11-18T09:47:00Z"))
    runner.prepare([source()], tmp_path / "baseline")
    assert state["outbox"] == [] and state["jobs"]["id:greenhouse|acme|k"]["alertable"] is False

    runner.config = late_config(("Lagerhelfer", "Kommissionierer"))
    runner.prepare([source()], tmp_path / "rule-change")
    assert [x["outbox_id"].split(":")[0] for x in state["outbox"]] == ["instant"]
    assert "Kommissionierer" in state["outbox"][0]["payload"]["chunks"][0]

    runner.prepare([source()], tmp_path / "again")
    assert len(state["outbox"]) == 1


def test_backlog_from_before_the_change_goes_to_the_digest_not_instant(monkeypatch, tmp_path):
    jobs = [listing("recent", title="Lagerhelfer A"), listing("old", title="Lagerhelfer B"),
            listing("sent", title="Lagerhelfer C")]
    monkeypatch.setattr(fetch, "fetch_jobs", lambda _s, _t: list(jobs))
    key = "id:greenhouse|acme|"
    state = {"schema_version": 1, "sources": {source()["source_id"]: {"baseline_complete": True}},
             "jobs": {key + "recent": {"first_seen": "2026-11-16T10:00:00Z", "last_seen": "", "source_ids": []},
                      key + "old": {"first_seen": "2026-11-05T10:00:00Z", "last_seen": "", "source_ids": []},
                      key + "sent": {"first_seen": "2026-11-16T10:00:00Z", "last_seen": "", "source_ids": []}},
             "outbox": [{"outbox_id": "instant:x", "status": "sent", "created_at": "2026-11-16T10:00:00Z",
                         "updated_at": "2026-11-16T10:00:00Z", "attempts": 1, "error": "",
                         "chunk_statuses": ["sent"], "chunk_item_ids": [[key + "sent"]],
                         "payload": {"items": [{"identity_key": key + "sent"}], "chunks": ["x"]}}]}
    runner = Runner(late_config(), state, clock=lambda: utc("2026-11-19T09:47:00Z"))
    runner.prepare([source()], tmp_path / "first")
    assert len(state["outbox"]) == 1
    assert [x["title"] for x in state["digest_queue"]] == ["Lagerhelfer A"]
    assert all(state["jobs"][key + x]["alertable"] is True for x in ("recent", "old", "sent"))

    runner.prepare([source()], tmp_path / "second")
    assert [x["title"] for x in state["digest_queue"]] == ["Lagerhelfer A"]


def test_digest_lists_quick_start_jobs_first_and_marks_them():
    speed = {"fast_companies": ["Randstad"], "fast_title_words": ["helfer"], "medium_title_words": ["Lager"]}

    def job(key, title, company="Example", status="match", salary="", city="Zeta"):
        return {"identity_key": key, "title": title, "company": company, "status": status, "salary": salary,
                "city": city, "url": f"https://jobs.example/{key}", "groups": ["area"], "tiers": ["3"]}
    items = [job("office", "Bürokraft"), job("lager", "Lagermitarbeiter"),
             job("review", "Produktionshelfer", status="review"), job("helfer", "Produktionshelfer", city="Alpha"),
             job("agency", "Mitarbeiter Versand", company="Randstad", salary="€15 pro Stunde")]
    chunks, members = Runner._daily_digest_chunks(items, utc("2026-11-18T17:47:00Z"), 0, speed=speed)
    assert members == [["agency", "helfer", "review", "lager", "office"]]
    assert "⚡ Mitarbeiter Versand" in chunks[0] and "• Bürokraft" in chunks[0]

    _, plain = Runner._daily_digest_chunks(items, utc("2026-11-18T17:47:00Z"), 0)
    assert plain[0][-1] == "review"


def test_pay_text_reads_schema_org_values_and_saved_strings():
    from pendel.watch import pay_text
    saved = "{'@type': 'QuantitativeValue', 'maxValue': 2600, 'minValue': 2488, 'unitText': 'MONTH'}"
    assert pay_text(saved) == "2,488–2,600 €/month"
    assert pay_text({"@type": "MonetaryAmount", "currency": "EUR",
                     "value": {"@type": "QuantitativeValue", "value": 15.5, "unitText": "HOUR"}}) == "15.5 €/hour"
    assert pay_text("€15.33 - €15.34 pro Stunde") == "€15.33 - €15.34 pro Stunde"
    assert pay_text("{broken") == "{broken" and pay_text(None) == ""
    assert Runner._facts({"salary": saved}) == ["💶 2,488–2,600 €/month"]


def test_quick_start_jobs_of_all_tiers_lead_the_digest():
    speed = {"fast_title_words": ["helfer"]}
    items = [{"identity_key": "it", "title": "IT Support", "status": "match", "tiers": ["1"], "url": "u1"},
             {"identity_key": "h3", "title": "Lagerhelfer", "status": "match", "tiers": ["3"], "url": "u2"},
             {"identity_key": "o3", "title": "Bürokraft", "status": "match", "tiers": ["3"], "url": "u3"}]
    chunks, members = Runner._daily_digest_chunks(items, utc("2026-11-20T17:00:00Z"), 0, speed=speed)
    assert members == [["h3", "it", "o3"]]
    assert chunks[0].index("⚡ QUICK START · 1") < chunks[0].index("TIER 1 · 1") < chunks[0].index("TIER 3 · 1")


def test_foreign_review_places_are_skipped_but_german_towns_without_country_are_kept():
    delivery = {"alert_countries": ["DE", "NL"], "skip_review_places": ["Bangalore", "New York", "Bristol"]}
    assert Runner._foreign_place("", "Bangalore", delivery)
    assert Runner._foreign_place("", "New York City", delivery)
    assert Runner._foreign_place("GB", "Somewhere", delivery)
    assert not Runner._foreign_place("", "Worms", delivery)
    assert not Runner._foreign_place("DE", "Berlin", delivery)
    assert not Runner._foreign_place("", "Bangalore", {})


def _t3(key, title="Lagerhelfer"):
    return {"identity_key": key, "title": title, "company": "Acme", "city": "Hilden", "status": "match",
            "tiers": ["3"], "url": f"https://jobs.example/{key}", "groups": ["g"]}


def test_large_instant_batch_becomes_one_list_message():
    runner = Runner(delivery_config(), {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []},
                    clock=lambda: utc("2026-11-21T09:00:00Z"))
    local = utc("2026-11-21T10:00:00Z")
    runner._append_instant([_t3(f"k{i}") for i in range(12)], local, "now", {"instant_bundle_over": 5})
    [entry] = runner.state["outbox"]
    assert len(entry["payload"]["chunks"]) == 1 and entry["payload"]["chunks"][0].startswith("⚡ 12 new Tier 3 jobs")
    runner._append_instant([_t3("a"), _t3("b")], local, "now", {"instant_bundle_over": 5})
    assert len(runner.state["outbox"][1]["payload"]["chunks"]) == 2


def test_rebundle_moves_only_unsent_chunks_into_one_message():
    items = [_t3(f"k{i}") for i in range(4)]
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": [
        {"outbox_id": "instant:a", "status": "pending", "created_at": "t", "updated_at": "t", "attempts": 2,
         "error": "", "chunk_statuses": ["sent", "sent", "pending", "pending"],
         "chunk_item_ids": [[x["identity_key"]] for x in items],
         "payload": {"items": items, "chunks": ["m0", "m1", "m2", "m3"]}},
        {"outbox_id": "daily:b", "status": "pending", "created_at": "t", "updated_at": "t", "attempts": 0,
         "error": "", "chunk_statuses": ["pending"], "chunk_item_ids": [["d"]],
         "payload": {"items": [_t3("d")], "chunks": ["digest"]}}]}
    runner = Runner(delivery_config(), state, clock=lambda: utc("2026-11-21T09:00:00Z"))
    assert runner.rebundle() == 0
    old, daily, new = state["outbox"]
    assert (old["status"], old["chunk_statuses"]) == ("cancelled", ["sent", "sent", "cancelled", "cancelled"])
    assert daily["status"] == "pending"
    assert [x["identity_key"] for x in new["payload"]["items"]] == ["k2", "k3"]
    assert new["status"] == "pending" and len(new["payload"]["chunks"]) == 1
    assert runner.rebundle() == 0 and len(state["outbox"]) == 3


def test_rebundle_refuses_while_a_message_needs_review():
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": [
        {"outbox_id": "instant:x", "status": "ambiguous", "chunk_statuses": ["ambiguous"], "payload": {}}]}
    assert Runner(delivery_config(), state).rebundle() == 1


def test_uncertain_send_is_retried_when_configured_then_needs_review(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "synthetic-chat")
    monkeypatch.setenv("PENDEL_TELEGRAM_APPROVED", "true")
    monkeypatch.setattr("pendel.watch.urllib.request.urlopen",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError()))
    cfg = delivery_config()
    cfg["delivery"]["uncertain_retries"] = 2
    state = _sending_state()
    entry = state["outbox"][0]
    for attempt in (1, 2):
        Runner(cfg, state).deliver(allow_send=True)
        assert (entry["status"], entry["chunk_statuses"][0]) == ("pending", "pending")
        assert f"retry {attempt} of 2" in entry["error"]
        entry["chunk_statuses"][0], entry["status"] = "sending", "sending"
    Runner(cfg, state).deliver(allow_send=True)
    assert entry["status"] == "ambiguous" and entry["chunk_uncertain"] == [3]
