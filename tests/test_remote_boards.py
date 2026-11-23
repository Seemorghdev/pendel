import json

from pendel import fetch, remote_boards


def test_remotive_jobs_keep_board_link_company_and_location():
    data = {"jobs": [{"id": 7, "url": "https://remotive.com/remote-jobs/support/agent-7", "title": "Support Agent",
                      "company_name": "Globex", "candidate_required_location": "Europe",
                      "publication_date": "2026-11-20T10:00:00", "salary": "$2k/month",
                      "description": "<p>Help <b>customers</b></p>"}]}
    [job] = remote_boards.parse_remotive(data)
    assert (job["job_id"], job["company"], job["location"], job["work_mode"]) == ("7", "Globex", "Europe", "remote")
    assert job["url"].startswith("https://remotive.com/") and job["description"] == "Help customers"


def test_remoteok_skips_the_legal_notice_and_reads_salary():
    data = [{"legal": "notice"}, {"id": "11", "position": "Virtual Assistant", "company": "Initech", "location": "",
                                  "url": "https://remoteOK.com/remote-jobs/11", "date": "2026-11-22T02:00:05+00:00",
                                  "salary_min": 20000, "salary_max": 30000, "description": "x"}]
    [job] = remote_boards.parse_remoteok(data)
    assert job["title"] == "Virtual Assistant" and job["salary"] == "20,000–30,000 USD/year"


def test_himalayas_empty_restrictions_mean_worldwide_and_americas_only_zones_mean_night():
    data = {"jobs": [
        {"title": "Chat Support", "companyName": "Umbrella", "locationRestrictions": [], "timezoneRestrictions": [-8, -5],
         "guid": "https://himalayas.app/companies/umbrella/jobs/chat-support", "pubDate": 1795408614,
         "minSalary": None, "maxSalary": None, "currency": None, "salaryPeriod": "annual", "description": "y"},
        {"title": "Moderator", "companyName": "Umbrella", "locationRestrictions": ["Germany", "Armenia"],
         "timezoneRestrictions": [1, 4], "guid": "https://himalayas.app/companies/umbrella/jobs/moderator",
         "pubDate": 1795408614, "description": "z"}]}
    night, day = remote_boards.parse_himalayas(data)
    assert (night["location"], night["schedule"]) == ("Worldwide", "night")
    assert (day["location"], day["schedule"]) == ("Germany, Armenia", "")
    assert night["posted_at"].endswith("Z")


def test_cursor_boards_follow_next_cursor_until_it_ends(monkeypatch):
    pages = {"": {"jobs": [{"id": 1, "jobTitle": "A"}], "nextCursor": "c2"},
             "c2": {"jobs": [{"id": 2, "jobTitle": "B"}], "nextCursor": None}}
    seen = []

    def get(url, _throttle):
        seen.append(url)
        return json.dumps(pages["c2" if "cursor=c2" in url else ""])
    monkeypatch.setattr(fetch, "get", get)
    jobs = remote_boards.fetch_jobs({"ats": "jobicy", "feed_url": "https://jobicy.com/api/jobs?count=100"}, None)
    assert [j["title"] for j in jobs] == ["A", "B"] and len(seen) == 2
