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
    assert accepted["status"] == "match"
    assert below["status"] == "exclude"
    assert unclear["status"] == "review"
