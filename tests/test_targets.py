from pendel.targets import evaluate_job, load_config, norm


def test_load_config_rejects_missing_group_and_tier_sections(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text('{"groups": {}}', encoding="utf-8")
    try:
        load_config(p)
    except ValueError as exc:
        assert "groups and tiers" in str(exc)
    else:
        raise AssertionError("invalid config should fail")


def test_load_config_rejects_missing_language_policy(tmp_path):
    p = tmp_path / "missing-language-policy.json"
    p.write_text('{"groups": {}, "tiers": {}}', encoding="utf-8")
    try:
        load_config(p)
    except ValueError as exc:
        assert "language_policy" in str(exc)
    else:
        raise AssertionError("missing language policy should fail early")


def test_normalization_is_generic():
    assert norm("Café") == norm("cafe")


def test_city_group_match_uses_actual_listing_location():
    cfg = {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B2"},
        "groups": {
            "area-a": {"cities": ["Exampleton"], "tiers": ["1"], "work_modes": ["onsite"]},
            "remote": {"cities": [], "tiers": ["1"], "work_modes": ["remote"]},
        },
        "tiers": {"1": {"phrases": ["Service Desk"]}},
        "exclusions": {},
    }
    result = evaluate_job({"title": "Service Desk Agent", "location": "Exampleton, Germany",
                           "country": "DE", "work_mode": "onsite",
                           "language_requirement": "English required"}, cfg)
    assert result["group_tier_matches"] == [{"group_id": "area-a", "tier": "1", "city": "Exampleton"}]


def test_tier_match_with_unknown_location_or_mode_is_reviewable():
    cfg = {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B1"},
        "groups": {
            "area-a": {"cities": ["Exampleton"], "tiers": ["2"], "work_modes": ["onsite", "hybrid"]},
            "remote": {"cities": [], "tiers": ["2"], "work_modes": ["remote"]},
        },
        "tiers": {"2": {"phrases": ["Customer Support"]}},
        "exclusions": {},
    }
    result = evaluate_job({"title": "Customer Support Agent", "location": "", "work_mode": "",
                           "language_requirement": "English required"}, cfg)
    assert result["status"] == "review"
    assert result["tier_candidates"] == ["2"]
    assert not result["group_tier_matches"]
    assert any("location" in reason for reason in result["reasons"])


def test_role_without_a_target_tier_is_excluded_instead_of_reviewed():
    cfg = {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B1"},
        "groups": {"area-a": {"cities": ["Exampleton"], "tiers": ["1", "2"], "work_modes": ["onsite"]},
                   "remote": {"cities": [], "tiers": ["1", "2"], "work_modes": ["remote"]}},
        "tiers": {"1": {"phrases": ["IT Support"]}, "2": {"phrases": ["Customer Support"]}},
        "exclusions": {},
    }
    result = evaluate_job({"title": "Finance Analyst", "location": "", "work_mode": "",
                           "language_requirement": ""}, cfg)
    assert result["status"] == "exclude"
    assert not result["tier_candidates"]
    assert any("no configured tier phrase" in reason for reason in result["reasons"])


def test_explicit_non_german_remote_regions_are_excluded_without_country_field():
    cfg = {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B1"},
        "groups": {"area-a": {"cities": ["Exampleton"], "tiers": ["2"], "work_modes": ["onsite"]},
                   "remote": {"cities": [], "tiers": ["2"], "work_modes": ["remote"]}},
        "tiers": {"2": {"phrases": ["Customer Support"]}},
        "exclusions": {},
    }
    for location in ("Remote (North America)", "Canada-only", "Remote India", "US only", "Remote Caribbean"):
        result = evaluate_job({"title": "Customer Support Agent", "location": location,
                               "work_mode": "remote", "language_requirement": "English required"}, cfg)
        assert result["status"] == "exclude", location
        assert any("outside Germany" in reason for reason in result["reasons"])
    mixed = evaluate_job({"title": "Customer Support Agent", "location": "Remote Germany/Canada",
                          "work_mode": "remote", "language_requirement": "English required"}, cfg)
    assert mixed["status"] == "review"
    assert not any("restricted outside Germany" in reason for reason in mixed["reasons"])


def test_configured_non_target_title_overrides_incidental_tier_phrase():
    cfg = {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B1"},
        "groups": {"area-a": {"cities": ["Exampleton"], "tiers": ["2"], "work_modes": ["onsite"]},
                   "remote": {"cities": [], "tiers": ["2"], "work_modes": ["remote"]}},
        "tiers": {"2": {"phrases": ["Quality Assurance"]}},
        "exclusions": {"non_target_title": [r"\bfirmware\b"]},
    }
    result = evaluate_job({"title": "Firmware Validation Engineer | Quality Assurance", "location": "Exampleton",
                           "work_mode": "onsite", "language_requirement": "English required"}, cfg)
    assert result["status"] == "exclude"
    assert not result["group_tier_matches"]
    assert any(reason.startswith("non_target_title:") for reason in result["reasons"])


def test_explicit_non_german_country_is_excluded_even_if_city_matches():
    cfg = {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B1"},
        "groups": {"area-a": {"cities": ["Exampleton"], "tiers": ["1"], "work_modes": ["onsite"]},
                   "remote": {"cities": [], "tiers": ["1"], "work_modes": ["remote"]}},
        "tiers": {"1": {"phrases": ["Service Desk"]}},
        "exclusions": {},
    }
    result = evaluate_job({"title": "Service Desk Agent", "location": "Exampleton, France",
                           "country": "FR", "work_mode": "onsite",
                           "language_requirement": "English required"}, cfg)
    assert result["status"] == "exclude"
    assert not result["group_tier_matches"]


def test_optional_german_b2_and_english_c1_keep_language_requirements_separate():
    cfg = {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B1"},
        "groups": {"area-a": {"cities": ["Exampleton"], "tiers": ["1"], "work_modes": ["onsite"]},
                   "remote": {"cities": [], "tiers": ["1"], "work_modes": ["remote"]}},
        "tiers": {"1": {"phrases": ["Service Desk"]}},
        "exclusions": {},
    }
    result = evaluate_job({"title": "Service Desk Agent", "location": "Exampleton, Germany",
                           "country": "DE", "work_mode": "onsite",
                           "language_requirement": "English C1 required; German B2 preferred"}, cfg)
    assert result["status"] == "review"
    assert result["language_requirement_status"] == "language_allowed"
    assert any("preferred" in reason for reason in result["reasons"])


def test_german_compound_deutschkenntnisse_with_required_b2_is_excluded():
    cfg = {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B1"},
        "groups": {"area-a": {"cities": ["Exampleton"], "tiers": ["3"], "work_modes": ["onsite"]},
                   "remote": {"cities": [], "tiers": [], "work_modes": ["remote"]}},
        "tiers": {"3": {"phrases": ["Lagerist"]}},
        "exclusions": {},
    }
    result = evaluate_job({"title": "Lagerist", "location": "Exampleton, Germany", "country": "DE",
                           "work_mode": "onsite", "language_requirement": "",
                           "description": "Gute Deutschkenntnisse (mindestens B2) erforderlich."}, cfg)
    assert result["language_requirement_status"] == "language_mismatch"
    assert result["german_level"] == "B2"
    assert result["status"] == "exclude"
    assert any("German requirement exceeds" in reason for reason in result["reasons"])


def test_verhandlungssichere_deutschkenntnisse_exceeds_b1_limit():
    cfg = {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B1"},
        "groups": {"area-a": {"cities": ["Exampleton"], "tiers": ["2"], "work_modes": ["onsite"]},
                   "remote": {"cities": [], "tiers": [], "work_modes": ["remote"]}},
        "tiers": {"2": {"phrases": ["Customer Support"]}},
        "exclusions": {},
    }
    result = evaluate_job({
        "title": "Customer Support Agent",
        "location": "Exampleton, Germany",
        "country": "DE",
        "work_mode": "onsite",
        "description": "Verhandlungssichere Deutschkenntnisse sind erforderlich.",
    }, cfg)
    assert result["language_requirement_status"] == "language_mismatch"
    assert result["status"] == "exclude"
    assert any("German requirement exceeds" in reason for reason in result["reasons"])


def test_fluently_german_is_required_and_bonus_languages_are_optional():
    cfg = {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B1"},
        "groups": {"area-a": {"cities": ["Exampleton"], "tiers": ["2"], "work_modes": ["onsite"]},
                   "remote": {"cities": [], "tiers": [], "work_modes": ["remote"]}},
        "tiers": {"2": {"phrases": ["Customer Support"]}},
        "exclusions": {},
    }
    job = {"title": "Customer Support Agent", "location": "Exampleton, Germany", "country": "DE",
           "work_mode": "onsite"}
    required = evaluate_job(job | {"description": "Our staff communicate fluently in German and English."}, cfg)
    assert required["language_requirement_status"] == "language_mismatch"
    assert required["status"] == "exclude"

    optional = evaluate_job(job | {"description": "German/French fluency is a bonus."}, cfg)
    assert optional["language_requirement_status"] != "language_mismatch"
    assert optional["status"] == "review"


def test_tier_three_hours_threshold_accepts_32_excludes_20_and_reviews_unknown():
    cfg = {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B1"},
        "minimum_hours_by_tier": {"3": 30},
        "groups": {"area-a": {"cities": ["Exampleton"], "tiers": ["3"], "work_modes": ["onsite"]},
                   "remote": {"cities": [], "tiers": [], "work_modes": ["remote"]}},
        "tiers": {"3": {"phrases": ["Lagerhelfer"]}},
        "exclusions": {},
    }
    base = {"title": "Lagerhelfer", "location": "Exampleton, Germany", "country": "DE",
            "work_mode": "onsite", "language_requirement": "English required"}
    accepted = evaluate_job(base | {"schedule": "32 hours per week"}, cfg)
    below = evaluate_job(base | {"schedule": "20 hours per week"}, cfg)
    unclear = evaluate_job(base | {"schedule": "Part-time"}, cfg)
    range_qualifies = evaluate_job(base | {"schedule": "30-40 h/Woche"}, cfg)
    range_crosses = evaluate_job(base | {"schedule": "20–32 Stunden pro Woche"}, cfg)
    range_below = evaluate_job(base | {"schedule": "20-29 h/Woche"}, cfg)
    assert accepted["status"] == "match"
    assert below["status"] == "exclude"
    assert unclear["status"] == "review"
    assert range_qualifies["status"] == "match"
    assert range_qualifies["hours_status"] == "hours_range"
    assert range_qualifies["hours_per_week"] == 30
    assert range_qualifies["hours_per_week_max"] == 40
    assert range_crosses["status"] == "review"
    assert "range crosses" in " ".join(range_crosses["reasons"])
    assert range_below["status"] == "exclude"
    assert range_below["hours_per_week_max"] == 29


def test_hours_range_parser_accepts_hyphen_en_dash_and_h_per_week_forms():
    from pendel.targets import _hours
    assert _hours({"description": "30-40 h/Woche"}) == ("hours_range", 30, 40)
    assert _hours({"description": "30–40 hours per week"}) == ("hours_range", 30, 40)


def _tier3_cfg(soft=True):
    cfg = {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B1"},
        "minimum_hours_by_tier": {"3": 30},
        "groups": {
            "area-a": {"cities": ["Exampleton"], "tiers": ["1", "3"], "work_modes": ["onsite"]},
            "remote": {"cities": [], "tiers": ["1"], "work_modes": ["remote"]},
        },
        "tiers": {"1": {"phrases": ["Service Desk"]}, "3": {"phrases": ["Lagerhelfer"]}},
        "exclusions": {},
    }
    if soft:
        cfg["soft_review_reasons_by_tier"] = {"3": [
            "required language not stated clearly", "German requirement level is unclear",
            "work mode is unclear", "hours/full-time status is unclear",
        ]}
    return cfg


def test_tier3_unclear_details_become_a_match_but_keep_their_reasons():
    job = {"title": "Lagerhelfer (m/w/d)", "location": "Exampleton", "country": "DE"}
    result = evaluate_job(job, _tier3_cfg())
    assert result["status"] == "match"
    assert any("hours" in reason for reason in result["reasons"])
    assert evaluate_job(job, _tier3_cfg(soft=False))["status"] == "review"


def test_soft_reasons_do_not_override_exclusions_or_other_tiers():
    cfg = _tier3_cfg()
    short = {"title": "Lagerhelfer", "location": "Exampleton", "country": "DE",
             "work_mode": "onsite", "hours_per_week": 20}
    assert evaluate_job(short, cfg)["status"] == "exclude"
    tier1 = {"title": "Service Desk Agent", "location": "Exampleton", "country": "DE"}
    assert evaluate_job(tier1, cfg)["status"] == "review"


def test_germany_wide_group_matches_tier3_anywhere_in_germany_only():
    cfg = _tier3_cfg()
    cfg["groups"]["11-germany-tier3"] = {"anywhere_in_country": "DE", "tiers": ["3"], "work_modes": ["onsite"]}
    far = {"title": "Lagerhelfer (m/w/d)", "location": "Ludwigsfelde, Brandenburg, Deutschland"}
    result = evaluate_job(far, cfg)
    assert result["status"] == "match"
    assert result["group_tier_matches"] == [{"group_id": "11-germany-tier3", "tier": "3", "city": "Ludwigsfelde"}]
    abroad = {"title": "Lagerhelfer", "location": "Wien", "country": "AT"}
    assert evaluate_job(abroad, cfg)["status"] == "exclude"
    tier1 = {"title": "Service Desk Agent", "location": "Ludwigsfelde", "country": "DE"}
    assert evaluate_job(tier1, cfg)["status"] == "review"  # tier 1 still needs one of the 8 groups


def _europe_cfg():
    return {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B1"},
        "countries": {"allowed": ["DE", "NL", "ES"], "remote_allowed": ["DE", "NL", "ES", "FR"],
                      "remote_regions": ["EU", "Europe", "EMEA"]},
        "groups": {
            "remote": {"cities": [], "tiers": ["1"], "work_modes": ["remote"]},
            "europe": {"anywhere_in_country": ["NL", "ES"], "tiers": ["1"], "work_modes": ["onsite", "hybrid"]},
        },
        "tiers": {"1": {"phrases": ["IT Support"]}},
        "exclusions": {},
    }


def test_europe_group_takes_onsite_jobs_in_listed_countries_only():
    cfg = _europe_cfg()
    barcelona = evaluate_job({"title": "IT Support Analyst", "location": "Barcelona", "country": "ES",
                           "work_mode": "onsite"}, cfg)
    assert barcelona["group_tier_matches"] == [{"group_id": "europe", "tier": "1", "city": "Barcelona"}]
    paris = evaluate_job({"title": "IT Support Analyst", "location": "Paris", "country": "FR", "work_mode": "onsite"}, cfg)
    assert paris["status"] == "exclude"
    assert any("outside allowed countries (FR)" in r for r in paris["reasons"])


def test_remote_jobs_in_eu_countries_or_eu_regions_match_the_remote_group():
    cfg = _europe_cfg()
    for job in ({"title": "IT Support", "location": "Remote, France", "country": "FR", "work_mode": "remote"},
                {"title": "IT Support", "location": "Remote - EMEA", "work_mode": "remote"},
                {"title": "IT Support", "location": "Europe (Remote)", "work_mode": "remote"}):
        assert evaluate_job(job, cfg)["group_tier_matches"] == [{"group_id": "remote", "tier": "1"}], job
    us = evaluate_job({"title": "IT Support", "location": "Remote (US)", "country": "US", "work_mode": "remote"}, cfg)
    assert us["status"] == "exclude"


def test_without_a_countries_block_germany_rules_are_unchanged():
    cfg = _europe_cfg()
    del cfg["countries"]
    result = evaluate_job({"title": "IT Support", "location": "Dublin", "country": "IE", "work_mode": "onsite"}, cfg)
    assert result["status"] == "exclude"
    assert any("outside Germany (IE)" in r for r in result["reasons"])
    eu = evaluate_job({"title": "IT Support", "location": "Remote - EMEA", "work_mode": "remote"}, cfg)
    assert eu["group_tier_matches"] == []
