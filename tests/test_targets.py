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
