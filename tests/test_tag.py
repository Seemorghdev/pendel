from pendel.targets import evaluate_job


def config():
    return {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B2"},
        "minimum_hours_by_tier": {"3": 25},
        "groups": {
            "area-a": {"cities": ["Exampleton"], "tiers": ["1", "2", "3"],
                       "work_modes": ["onsite", "hybrid", "remote"]},
            "area-b": {"cities": ["Sampleburg"], "tiers": ["1", "2", "3"],
                       "work_modes": ["onsite", "hybrid", "remote"]},
            "remote": {"cities": [], "tiers": ["1", "2"], "work_modes": ["remote"]},
        },
        "tiers": {
            "1": {"phrases": ["IT Support", "Service Desk"]},
            "2": {"phrases": ["Workforce Planner", "Customer Support", "Trust & Safety"]},
            "3": {"phrases": ["Warehouse Associate", "Lagerhelfer", "Production Helper"]},
        },
        "exclusions": {
            "student": [r"\b(intern|working student)\b"],
            "senior": [r"\b(senior|lead|director)\b"],
            "phone_heavy": [r"\b(inbound|outbound|call center|telephone support)\b"],
        },
    }


def job(**overrides):
    base = {
        "title": "IT Support Specialist",
        "location": "Exampleton, Exampleland",
        "country": "DE",
        "work_mode": "onsite",
        "language_requirement": "English required",
        "description": "English is the working language.",
    }
    return base | overrides


def test_actual_job_city_assigns_group_not_source_location():
    result = evaluate_job(job(location="Sampleburg, Exampleland", source_groups="elsewhere"), config())
    assert result["status"] == "match"
    assert result["group_tier_matches"] == [{"group_id": "area-b", "tier": "1", "city": "Sampleburg"}]


def test_remote_group_requires_explicit_country_and_mode():
    result = evaluate_job(job(location="Remote, Germany", work_mode="remote"), config())
    assert result["status"] == "match"
    assert result["group_tier_matches"] == [{"group_id": "remote", "tier": "1"}]
    uncertain = evaluate_job(job(location="Remote", country="", work_mode="remote"), config())
    assert uncertain["status"] == "review"
    outside = evaluate_job(job(location="Remote, Spain", country="Spain", work_mode="remote"), config())
    assert outside["status"] == "exclude"


def test_language_requirements_are_separate_from_advert_language():
    b2 = evaluate_job(job(language_requirement="German B2 required",
                          description="Die Stellenanzeige ist auf Deutsch."), config())
    assert b2["status"] == "match"
    assert b2["language_requirement_status"] == "language_allowed"
    assert b2["advert_language"] == "de"
    c1 = evaluate_job(job(language_requirement="German C1 required"), config())
    assert c1["status"] == "exclude"
    unspecified = evaluate_job(job(language_requirement="German required", description=""), config())
    assert unspecified["status"] == "review"
    assert unspecified["language_requirement_status"] == "language_unclear"
    fluent = evaluate_job(job(language_requirement="English fluent; German fluent"), config())
    assert fluent["status"] == "exclude"
    mixed = evaluate_job(job(language_requirement="English C1 and German B2 accepted"), config())
    assert mixed["status"] == "match"
    english_only = evaluate_job(job(language_requirement="English required; German not required"), config())
    assert english_only["status"] == "match"
    ad_only = evaluate_job(job(language_requirement="", description="Diese Stellenanzeige ist vollständig auf Deutsch."), config())
    assert ad_only["status"] == "review"


def test_multilingual_requirement_excludes_non_allowed_language():
    result = evaluate_job(job(language_requirement="English and Dutch required"), config())
    assert result["status"] == "exclude"
    assert any("additional language requirement" in reason for reason in result["reasons"])


def test_city_match_with_unclear_work_mode_stays_in_review():
    result = evaluate_job(job(work_mode=""), config())
    assert result["status"] == "review"
    assert "work mode is unclear" in result["reasons"]


def test_tier_hours_are_driven_by_configured_threshold():
    full = evaluate_job(job(title="Lagerhelfer Vollzeit"), config())
    assert full["status"] == "match"
    enough = evaluate_job(job(title="Lagerhelfer", schedule="25 hours per week"), config())
    assert enough["status"] == "match"
    short = evaluate_job(job(title="Lagerhelfer Teilzeit", schedule="20 hours per week"), config())
    assert short["status"] == "exclude"
    unknown = evaluate_job(job(title="Lagerhelfer"), config())
    assert unknown["status"] == "review"


def test_roles_without_a_private_hours_threshold_are_not_tied_to_tier_number():
    result = evaluate_job(job(title="Customer Support", schedule="8 hours per week"), config())
    assert result["status"] == "match"


def test_phone_student_and_senior_exclusions_are_explicit():
    for title, pattern in [
        ("Inbound Customer Support", "phone_heavy"),
        ("Working Student IT Support", "student"),
        ("Senior IT Support", "senior"),
    ]:
        result = evaluate_job(job(title=title), config())
        assert result["status"] == "exclude"
        assert any(pattern in reason for reason in result["reasons"])


def test_configured_groups_can_match_but_remote_tier_three_is_disabled():
    for city, group in [("Exampleton", "area-a"), ("Sampleburg", "area-b")]:
        result = evaluate_job(job(location=f"{city}, Exampleland"), config())
        assert result["group_tier_matches"][0]["group_id"] == group
    result = evaluate_job(job(title="Warehouse Associate", location="Remote, Germany", work_mode="remote",
                              schedule="40 hours per week"), config())
    assert result["status"] == "exclude"
    assert not result["group_tier_matches"]
