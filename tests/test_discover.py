import csv
import json

from pendel import discover, fetch

CONFIG = {
    "remote_group_id": "remote",
    "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B1"},
    "countries": {"allowed": ["DE", "NL", "ES"], "remote_allowed": ["DE", "NL", "ES", "FR"],
                  "remote_regions": ["EU", "EMEA"]},
    "groups": {
        "remote": {"cities": [], "tiers": ["1", "2"], "work_modes": ["remote"]},
        "12-western-europe": {"anywhere_in_country": ["NL", "ES"], "tiers": ["1", "2"],
                              "work_modes": ["onsite", "hybrid"]},
    },
    "tiers": {"1": {"phrases": ["IT Support"]}, "2": {"phrases": ["Customer Support"]}},
    "exclusions": {},
}


def job(title, location, country, mode="onsite"):
    return {"job_id": title + location, "title": title, "location": location, "country": country,
            "work_mode": mode, "url": "https://jobs.example/" + title, "description": "English is required."}


FEEDS = {
    "https://boards-api.greenhouse.io/v1/boards/acme/jobs": [
        job("IT Support Analyst", "Amsterdam", "NL"), job("Engineer", "Madrid", "ES")],
    "https://api.ashbyhq.com/posting-api/job-board/usonly": [job("IT Support", "Austin", "US")],
    "https://boards-api.greenhouse.io/v1/boards/big/jobs": [job("Engineer", "Barcelona", "ES")] * 6,
}


def write_csv(path, rows, fields):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_candidates_are_scored_and_only_useful_new_feeds_are_kept(tmp_path, monkeypatch):
    def fake_fetch(feed, _throttle):
        if feed["token"] == "broken":
            raise OSError("timed out")
        return FEEDS[feed["feed_url"]]
    monkeypatch.setattr(fetch, "fetch_jobs", fake_fetch)
    (tmp_path / "targets.json").write_text(json.dumps(CONFIG))
    fields = ["ats", "token", "feed_url", "companies", "career_url", "note"]
    write_csv(tmp_path / "candidates.csv", [
        {"ats": "greenhouse", "token": "acme", "companies": "Acme"},
        {"ats": "ashby", "token": "usonly", "companies": "US Only"},
        {"ats": "greenhouse", "token": "big", "companies": "Big"},
        {"ats": "lever", "token": "broken", "companies": "Broken"},
        {"ats": "greenhouse", "token": "known", "companies": "Known"},
        {"ats": "workday", "token": "x", "companies": "No Template"},
    ], fields)
    write_csv(tmp_path / "sources.csv", [
        {"ats": "greenhouse", "token": "known", "feed_url": "https://boards-api.greenhouse.io/v1/boards/known/jobs",
         "companies": "Known"}], ["ats", "token", "feed_url", "companies"])

    assert discover.run(tmp_path / "candidates.csv", tmp_path / "targets.json", tmp_path / "sources.csv",
                        tmp_path / "out", delay=0) == 0
    report = {r["companies"]: r for r in csv.DictReader((tmp_path / "out/discovery-report.csv").open())}
    assert (report["Acme"]["decision"], report["Acme"]["tier12_hits"]) == ("keep", "1")
    assert report["US Only"]["decision"] == "drop"
    assert report["Big"]["decision"] == "maybe"
    assert report["Broken"]["status"] == "error"
    assert report["Known"]["status"] == "already_watched"
    assert report["No Template"]["status"] == "no_feed_url"
    added = list(csv.DictReader((tmp_path / "out/sources-to-add.csv").open()))
    assert [r["companies"] for r in added] == ["Acme"]
    assert added[0]["feed_url"] == "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
