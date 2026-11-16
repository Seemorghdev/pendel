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


def test_feed_health_notice_is_quiet_until_change_and_reports_recovery(monkeypatch, tmp_path):
    def offline(*_args):
        raise OSError("feed unavailable")

    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}
    runner = Runner(config(), state)
    monkeypatch.setattr(fetch, "fetch_jobs", offline)
    runner.prepare([source()], tmp_path / "failure-one")
    assert len(state["outbox"]) == 1
    runner.prepare([source()], tmp_path / "failure-two")
    assert len(state["outbox"]) == 1

    monkeypatch.setattr(fetch, "fetch_jobs", lambda *_args: [])
    runner.prepare([source()], tmp_path / "recovered")
    assert len(state["outbox"]) == 2
    assert "recovered" in state["outbox"][1]["payload"]["chunks"][0].lower()

    monkeypatch.setattr(fetch, "fetch_jobs", offline)
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
    receipt = write_delivery_receipt(tmp_path, state)
    assert receipt["notification_status_counts"] == {"not_recorded": 2, "sent": 1}
    assert receipt["run_classification_counts"] == {"baseline": 1, "excluded": 1, "newly_eligible": 1}
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
    receipt = write_delivery_receipt(tmp_path, state)
    assert receipt["notification_status_counts"] == {
        "ambiguous": 1, "not_recorded": 2, "pending": 1,
    }
    assert receipt["run_classification_counts"] == {
        "newly_eligible": 2, "not_recorded": 1, "prior_seen": 1,
    }


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
