import pytest

from pendel.detect import detect


@pytest.mark.parametrize("html, ats, token", [
    ('<a href="https://boards.greenhouse.io/acme/jobs/123">', "greenhouse", "acme"),
    ('<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme2">', "greenhouse", "acme2"),
    ("https://job-boards.eu.greenhouse.io/acme3", "greenhouse", "acme3"),
    ('<a href="https://jobs.lever.co/acme">', "lever", "acme"),
    ('<a href="https://jobs.eu.lever.co/acme">', "lever", "acme"),
    ('<iframe src="https://jobs.ashbyhq.com/Acme">', "ashby", "Acme"),
    ('<a href="https://acme.recruitee.com/o/it-support">', "recruitee", "acme"),
    ('<a href="https://acme.jobs.personio.de/job/1">', "personio", "acme"),
    ('<a href="https://jobs.smartrecruiters.com/AcmeBV/123">', "smartrecruiters", "AcmeBV"),
    ('<a href="https://apply.workable.com/acme/j/ABC/">', "workable", "acme"),
    ("https://backroads.wd1.myworkdayjobs.com/en-US/BR/job/x", "workday", "backroads.wd1/BR"),
    ('<a href="https://acme.taleo.net/careersection">', "taleo", "acme"),
    ('<a href="https://acme.teamtailor.com/jobs">', "teamtailor", "acme"),
])
def test_detects_ats_and_token(html, ats, token):
    got = detect(html)
    assert got is not None
    assert got[:2] == (ats, token)


def test_no_ats_returns_none():
    assert detect("<p>No ATS here, just www.example.com/careers</p>") is None


def test_recruitee_tracking_script_is_not_a_board():
    assert detect('<script src="https://careers-analytics.recruitee.com/tracker.js"></script>') is None


@pytest.mark.parametrize("url, feed", [
    ("https://jobs.lever.co/acme", "https://api.lever.co/v0/postings/acme?mode=json"),
    ("https://jobs.eu.lever.co/acme", "https://api.eu.lever.co/v0/postings/acme?mode=json"),
    ("https://acme.jobs.personio.de/", "https://acme.jobs.personio.de/xml"),
    ("https://boards.greenhouse.io/acme", "https://boards-api.greenhouse.io/v1/boards/acme/jobs"),
])
def test_feed_urls(url, feed):
    assert detect(url)[2] == feed


def test_closed_systems_have_no_feed():
    ats, _, feed, note = detect("https://backroads.wd1.myworkdayjobs.com/en-US/BR/job/x")
    assert ats == "workday"
    assert feed == ""
    assert "record only" in note


def test_most_frequent_ats_wins_and_others_are_noted():
    html = ('<a href="https://jobs.lever.co/acme">a</a><a href="https://jobs.lever.co/acme/1">b</a>'
            '<a href="https://boards.greenhouse.io/other">c</a>')
    ats, token, _, note = detect(html)
    assert (ats, token) == ("lever", "acme")
    assert "greenhouse" in note
