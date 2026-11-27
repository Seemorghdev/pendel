from datetime import date
from types import SimpleNamespace

from pendel import fetch
from pendel.fetch import IncompleteFeed, connect, load_feeds, run, store, with_query
from pendel.readers import job


def jobs(*ids):
    return [job(job_id=i, title=f"Job {i}") for i in ids]


def test_store_counts_new_and_does_not_duplicate(tmp_path):
    con = connect(tmp_path / "t.duckdb")
    day1, day2 = date(2026, 11, 13), date(2026, 11, 14)
    assert store(con, "Acme", "greenhouse", jobs("1", "2"), day1) == 2
    assert store(con, "Acme", "greenhouse", jobs("1", "2"), day1) == 0          # same-day re-run
    assert store(con, "Acme", "greenhouse", jobs("2", "3", "3"), day2) == 1     # one new, one listed twice
    rows = dict(con.execute("SELECT job_id, first_seen FROM jobs ORDER BY job_id").fetchall())
    assert rows == {"1": day1, "2": day1, "3": day2}
    open_today = {r[0] for r in con.execute("SELECT job_id FROM jobs WHERE last_seen = ?", [day2]).fetchall()}
    assert open_today == {"2", "3"}                                             # job 1 has closed


def test_same_job_id_at_two_companies_is_two_jobs(tmp_path):
    con = connect(tmp_path / "t.duckdb")
    store(con, "A", "lever", jobs("1"), date(2026, 11, 13))
    store(con, "B", "lever", jobs("1"), date(2026, 11, 13))
    assert con.execute("SELECT count(*) FROM jobs").fetchone()[0] == 2


def test_with_query():
    assert with_query("https://a/b", "x=1") == "https://a/b?x=1"
    assert with_query("https://a/b?mode=json", "x=1") == "https://a/b?mode=json&x=1"


def test_load_feeds_filters_fixture_rows(tmp_path):
    feeds = tmp_path / "feeds.csv"
    feeds.write_text(
        "company,ats,token,feed_url,review\n"
        "Acme,greenhouse,acme,https://feed.example/acme,accepted\n"
        "Example,softgarden,example,https://feed.example/example,check\n",
        encoding="utf-8",
    )
    accepted = load_feeds(feeds, "accepted")
    assert len(accepted) == 1
    assert accepted[0]["company"] == "Acme"
    assert accepted[0]["ats"] == "greenhouse"


def test_partial_feed_is_logged_and_does_not_abort_later_sources(monkeypatch, tmp_path):
    feeds = tmp_path / "feeds.csv"
    feeds.write_text(
        "company,ats,token,feed_url,review\n"
        "Partial,smartrecruiters,partial,https://api.smartrecruiters.com/v1/companies/partial,accepted\n"
        "Complete,greenhouse,complete,https://boards-api.greenhouse.io/v1/boards/complete/jobs,accepted\n",
        encoding="utf-8",
    )
    def fetch_one(feed, _throttle):
        if feed["company"] == "Partial":
            raise IncompleteFeed("provider pagination cap", jobs("not-stored"))
        return jobs("complete-1")
    monkeypatch.setattr(fetch, "fetch_jobs", fetch_one)
    args = SimpleNamespace(feeds=str(feeds), db=str(tmp_path / "run.duckdb"), review="accepted",
                           company=None, limit=0, delay=0)
    assert run(args) == 0
    con = connect(tmp_path / "run.duckdb")
    logs = con.execute("SELECT company, status FROM fetch_log ORDER BY company").fetchall()
    saved = con.execute("SELECT company, job_id FROM jobs").fetchall()
    con.close()
    assert logs == [("Complete", "ok"), ("Partial", "partial")]
    assert saved == [("Complete", "complete-1")]
