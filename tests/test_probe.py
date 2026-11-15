import json

from pendel.probe import (PROVIDERS, check_ashby, check_greenhouse, check_lever, check_personio, check_recruitee,
                          check_smartrecruiters, check_workable, grade, slugs)


def test_generic_host_words_are_not_slugs():
    assert slugs("Alliander", "https://werkenbij.alliander.com/vacatures") == ["alliander"]


def test_board_url_token_comes_first_and_ats_host_is_never_a_slug():
    assert slugs("Appen", "https://jobs.lever.co/appen?location=Germany") == ["appen"]
    assert slugs("Welocalize", "https://jobs.lever.co/weloglobal")[0] == "weloglobal"
    assert "lever" not in slugs("Welocalize", "https://jobs.lever.co/weloglobal")
    assert "myworkdayjobs" not in slugs("Acme", "https://acme.wd3.myworkdayjobs.com/en-US/careers")


def test_workable_is_not_probed():
    assert "workable" not in {p[1] for p in PROVIDERS}


def test_grade():
    assert grade(0, []) == "found_empty"
    assert grade(5, ["Exampleton", "Exampleton"], ["Exampleton"]) == "found"
    assert grade(5, ["Sampleburg"], ["Exampleton"]) == "found_far"
    assert grade(5, ["Remote - EMEA"]) == "found"
    assert grade(22, ["Exampleland", "Sample Islands"], ["Exampleton"]) == "found_far"
    # one nearby job anywhere in the list is enough
    assert grade(4, ["Northport", "Sampleburg", "Southport"], ["Sampleburg"]) == "found"


def test_slugs_from_name_and_url():
    assert slugs("Redwood Software", "https://www.redwood.com/careers/") == ["redwoodsoftware", "redwood-software", "redwood"]
    assert slugs("Mollie", "https://www.mollie.com/careers") == ["mollie"]
    assert slugs("NS (Nederlandse Spoorwegen)", "https://www.werkenbijns.nl/") == ["werkenbijns"]
    assert slugs("Microsoft Netherlands", "https://careers.microsoft.com/") == ["microsoft"]
    assert slugs("METRO AG / METRO.digital", "")[0] == "metro"
    assert slugs("Düsseldorf Data", "")[0] == "dusseldorfdata"


def test_slugs_capped_at_three():
    assert len(slugs("Royal HaskoningDHV", "https://www.royalhaskoningdhv.com/en/careers")) <= 3


def test_greenhouse():
    body = json.dumps({"jobs": [{"location": {"name": "Amsterdam"}}]})
    assert check_greenhouse(body) == (1, "", ["Amsterdam"])
    assert check_greenhouse('{"status": 404}') is None
    assert check_greenhouse("not json") is None


def test_lever_accepts_empty_board():
    assert check_lever("[]") == (0, "", [])
    assert check_lever('{"ok": false, "error": "Document not found"}') is None


def test_ashby():
    assert check_ashby(json.dumps({"jobs": [{"location": "Utrecht"}]})) == (1, "", ["Utrecht"])


def test_recruitee_reads_company_name():
    body = json.dumps({"offers": [{"company_name": "bunq", "city": "Amsterdam"}]})
    assert check_recruitee(body) == (1, "bunq", ["Amsterdam"])


def test_personio_needs_its_xml_root():
    xml = "<workzag-jobs><position><office>Düsseldorf</office></position></workzag-jobs>"
    assert check_personio(xml) == (1, "", ["Düsseldorf"])
    assert check_personio("<html>Personio</html>") is None


def test_smartrecruiters_ignores_empty_results():
    assert check_smartrecruiters('{"totalFound": 0, "content": []}') is None
    body = json.dumps({"totalFound": 2, "content": [{"company": {"name": "METRO"}, "location": {"city": "Düsseldorf"}}]})
    assert check_smartrecruiters(body) == (2, "METRO", ["Düsseldorf"])


def test_workable():
    assert check_workable(json.dumps({"name": "Acme", "jobs": [{"city": "Eindhoven"}]})) == (1, "Acme", ["Eindhoven"])
    assert check_workable("{}") is None
