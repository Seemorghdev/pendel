from pendel import readers as r


def test_text_strips_escaped_html():
    assert r.text("&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;One&lt;/li&gt;&lt;/ul&gt;") == "Hello & welcome\nOne"


def test_country_code():
    assert r.country_code("de") == "DE"
    assert r.country_code("Exampleton, Germany") == "DE"
    assert r.country_code("Amsterdam, The Netherlands") == "NL"
    assert r.country_code("Remote") == ""


def test_mode():
    assert r.mode_from("Remote") == "remote"
    assert r.mode_from("On-site") == "onsite"
    assert r.mode_from("OnSite") == "onsite"
    assert r.mode_from("hybrid") == "hybrid"
    assert r.mode_from_text("Exampleton (Hybrid)") == "hybrid"
    assert r.mode_from_text("Remote - Germany") == "remote"
    assert r.mode_from_text("Exampleton") == ""


def test_iso_date():
    assert r.iso_date("2026-11-03T10:00:00Z") == "2026-11-03"
    assert r.iso_date(1762128000000) == "2025-11-03"
    assert r.iso_date(None) == ""


def test_greenhouse():
    data = {"jobs": [{"id": 123, "title": "IT Support Engineer", "absolute_url": "https://x/1",
                      "location": {"name": "Remote, Germany"}, "first_published": "2026-11-10T08:00:00Z",
                      "content": "&lt;p&gt;English team&lt;/p&gt;"}]}
    (j,) = r.parse_greenhouse(data)
    assert j["job_id"] == "123"
    assert j["country"] == "DE" and j["work_mode"] == "remote"
    assert j["posted_at"] == "2026-11-10" and j["description"] == "English team"


def test_lever():
    data = [{"id": "abc", "text": "Support Specialist", "hostedUrl": "https://jobs.lever.co/x/abc",
             "categories": {"location": "Amsterdam"}, "workplaceType": "hybrid", "country": "NL",
             "createdAt": 1762128000000, "descriptionPlain": "Hi",
             "salaryRange": {"min": 3000, "max": 3600, "currency": "EUR", "interval": "per-month-salary"}}]
    (j,) = r.parse_lever(data)
    assert (j["country"], j["work_mode"]) == ("NL", "hybrid")
    assert j["salary"].startswith("3000–3600 EUR")


def test_ashby_skips_unlisted_and_reads_country():
    data = {"jobs": [
        {"id": "a1", "title": "Ops Analyst", "location": "Exampleton", "workplaceType": "Remote", "isListed": True,
         "jobUrl": "https://jobs.ashbyhq.com/x/a1", "publishedAt": "2026-11-12T09:00:00.000+00:00",
         "address": {"postalAddress": {"addressCountry": "Germany"}},
         "compensation": {"compensationTierSummary": "€45K – €55K"}},
        {"id": "a2", "title": "Hidden", "isListed": False},
    ]}
    (j,) = r.parse_ashby(data)
    assert (j["country"], j["work_mode"], j["salary"]) == ("DE", "remote", "€45K – €55K")


def test_recruitee_flags():
    data = {"offers": [
        {"id": 9, "title": "Service Desk", "location": "Utrecht, Netherlands", "country_code": "NL",
         "remote": False, "hybrid": True, "careers_url": "https://x.recruitee.com/o/sd",
         "published_at": "2026-11-01 10:00:00 UTC", "description": "<p>Dutch required</p>"}]}
    (j,) = r.parse_recruitee(data)
    assert (j["country"], j["work_mode"], j["posted_at"]) == ("NL", "hybrid", "2026-11-01")


def test_smartrecruiters_builds_url_and_language():
    data = {"totalFound": 1, "content": [
        {"id": "744000", "name": "IT Support (m/w/d)", "releasedDate": "2026-11-05T12:00:00.000Z",
         "location": {"city": "Düsseldorf", "country": "de", "remote": False},
         "language": {"code": "de-DE"}}]}
    (j,) = r.parse_smartrecruiters(data, "METROMAKRO")
    assert j["url"] == "https://jobs.smartrecruiters.com/METROMAKRO/744000"
    assert (j["country"], j["lang_hint"]) == ("DE", "de")


def test_personio_xml():
    xml = """<?xml version="1.0"?><workzag-jobs><position><id>55</id><office>Düsseldorf</office>
      <name>Customer Support (English)</name><createdAt>2026-10-30T09:00:00+00:00</createdAt>
      <jobDescriptions><jobDescription><name>About</name><value>&lt;p&gt;Remote possible&lt;/p&gt;</value>
      </jobDescription></jobDescriptions></position></workzag-jobs>"""
    (j,) = r.parse_personio(xml, "tonies")
    assert j["url"] == "https://tonies.jobs.personio.de/job/55"
    assert j["posted_at"] == "2026-10-30" and "Remote possible" in j["description"]


def test_softgarden_job_posting_jsonld():
    data = {"@graph": [{"@type": "JobPosting", "identifier": {"value": "sg-1"},
                        "title": "Warehouse Associate", "url": "https://jobs.example/sg-1",
                        "datePosted": "2026-11-10", "jobLocation": {"address": {
                            "addressLocality": "Exampleton", "addressCountry": "Germany"}},
                        "description": "English required"}]}
    (j,) = r.parse_softgarden(data)
    assert (j["job_id"], j["title"], j["country"], j["posted_at"]) == ("sg-1", "Warehouse Associate", "DE", "2026-11-10")


def test_pinpoint_public_postings_json():
    data = {"data": [{"id": "pp-1", "title": "IT Support", "url": "https://example.pinpointhq.com/postings/pp-1",
                      "location": {"city": "Exampleton", "country": "Germany"},
                      "workplace_type": "Hybrid", "created_at": "2026-11-11T08:00:00Z",
                      "description": "English role"}]}
    (j,) = r.parse_pinpoint(data)
    assert (j["job_id"], j["country"], j["work_mode"], j["posted_at"]) == ("pp-1", "DE", "hybrid", "2026-11-11")
