from pendel.locate import km, locate, norm


def test_norm_ignores_case_accents_and_spaces():
    assert norm(" Sample City ") == norm("sample city")


def test_distance_uses_caller_supplied_coordinates():
    assert 30 < km((0.0, 0.0), (0.0, 0.4)) < 50


def test_remote_rows_have_no_coordinates_and_keep_country_suffix():
    cities = {"sampleton": (52.0, 8.0, "DE")}
    out, missing = locate([{"company": "R", "city": "Remote DE"}, {"company": "S", "city": "Remote"}], cities)
    assert missing == []
    assert out[0]["lat"] == "" and out[0]["country"] == "DE"
    assert out[1]["country"] == ""


def test_locate_reports_unknown_names_and_only_computes_distance_with_home():
    rows = [{"company": "A", "city": "Sampleton"}, {"company": "B", "city": "Atlantis"}]
    cities = {"sampleton": (10.0, 20.0, "DE")}
    out, missing = locate(rows, cities)
    assert out[0]["country"] == "DE"
    assert out[0]["km_from_home"] == ""
    assert out[1]["lat"] == ""
    assert missing == ["Atlantis"]
    out, _ = locate(rows[:1], cities, home=(10.0, 20.1))
    assert int(out[0]["km_from_home"]) > 5
