"""Private-configuration-driven job eligibility rules.

The reusable package contains matching logic only. City groups, preferred job
phrases, language limits, and personal exclusions live in a private JSON file
passed to :func:`evaluate_job` at runtime.
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from pendel.readers import COUNTRY_NAMES, country_code


def norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(c for c in value if not unicodedata.combining(c))


def load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config.get("groups"), dict) or not isinstance(config.get("tiers"), dict):
        raise ValueError("target config must define groups and tiers objects")
    policy = config.get("language_policy")
    if (not isinstance(policy, dict) or not isinstance(policy.get("allowed_languages"), list)
            or str(policy.get("max_german_level") or "").casefold() not in {"a1", "a2", "b1", "b2", "c1", "c2"}):
        raise ValueError("target config must define language_policy.allowed_languages and max_german_level")
    if any(not isinstance(code, str) or not code.strip() for code in policy["allowed_languages"]):
        raise ValueError("language_policy.allowed_languages must contain non-empty language codes")
    thresholds = config.get("minimum_hours_by_tier", {})
    if not isinstance(thresholds, dict) or any(not str(k).isdigit() or int(v) <= 0 for k, v in thresholds.items()):
        raise ValueError("minimum_hours_by_tier must map tier numbers to positive hour limits")
    return config


def _has_phrase(text: str, phrase: str) -> bool:
    haystack, needle = norm(text), norm(phrase)
    if not needle:
        return False
    if re.fullmatch(r"[\w+#./ -]+", needle):
        return re.search(rf"(?<!\w){re.escape(needle)}(?!\w)", haystack) is not None
    return needle in haystack


def _tier_hits(title: str, tiers: dict[str, Any]) -> list[str]:
    return [str(tier) for tier, spec in tiers.items()
            if any(_has_phrase(title, phrase) for phrase in spec.get("phrases", []))]


def _city_matches(location: str, groups: dict[str, Any]) -> list[tuple[str, str]]:
    text = norm(location)
    matches: list[tuple[int, str, str]] = []
    for group_id, spec in groups.items():
        for city in spec.get("cities", []):
            alias_values = [city, *spec.get("city_aliases", {}).get(city, [])]
            for alias in alias_values:
                key = norm(alias)
                if key and re.search(rf"(?<!\w){re.escape(key)}(?!\w)", text):
                    matches.append((text.find(key), group_id, city))
                    break
    # Keep the actual city match and all groups that explicitly contain it.
    found: dict[tuple[str, str], int] = {}
    for offset, group_id, city in sorted(matches):
        found.setdefault((group_id, city), offset)
    return [(g, c) for (g, c), _ in sorted(found.items(), key=lambda item: (item[1], item[0]))]


def _mode(job: dict[str, Any]) -> str:
    raw = " ".join(str(job.get(k) or "") for k in ("work_mode", "title", "location"))
    value = norm(raw)
    if re.search(r"\b(remote|fully remote|work from home|home office|mobiles arbeiten|telearbeit)\b", value):
        return "remote"
    if re.search(r"\b(hybrid|hybride|hybrid work|teilweise mobil)\b", value):
        return "hybrid"
    if job.get("work_mode"):
        return norm(str(job["work_mode"]))
    if re.search(r"\b(on[- ]?site|in[- ]office|vor ort|office-based)\b", value):
        return "onsite"
    return ""


def _country(job: dict[str, Any]) -> str:
    structured = country_code(str(job.get("country") or ""))
    if structured:
        return structured
    location = str(job.get("location") or "")
    low = location.casefold()
    codes = {code for name, code in COUNTRY_NAMES.items()
             if re.search(rf"(?<!\w){re.escape(name.casefold())}(?!\w)", low)}
    valid_codes = set(COUNTRY_NAMES.values())
    codes.update(code for code in re.findall(r"(?<![A-Za-z])([A-Z]{2})(?![A-Za-z])", location)
                 if code in valid_codes)
    return next(iter(codes)) if len(codes) == 1 else ""


def _language(job: dict[str, Any], config: dict[str, Any]) -> tuple[str, str, list[str]]:
    policy = config.get("language_policy", {})
    allowed = {str(x).casefold() for x in policy.get("allowed_languages", [])}
    max_german = str(policy.get("max_german_level") or "").casefold()
    level_order = {"a1": 1, "a2": 2, "b1": 3, "b2": 4, "c1": 5, "c2": 6}
    aliases = {
        "en": ("english", "englisch"), "de": ("german", "deutsch"),
        "nl": ("dutch", "nederlands", "holländisch", "hollaendisch"),
        "fr": ("french", "französisch", "franzoesisch"), "es": ("spanish", "spanisch"),
        "it": ("italian", "italienisch"), "pl": ("polish", "polnisch"),
        "pt": ("portuguese", "portugiesisch"), "sv": ("swedish", "schwedisch"),
        "da": ("danish", "dänisch", "daenisch"), "no": ("norwegian", "norwegisch"),
        "fi": ("finnish", "finnisch"), "cs": ("czech", "tschechisch"),
        "hu": ("hungarian", "ungarisch"), "tr": ("turkish", "türkisch", "tuerkisch"),
        "ar": ("arabic", "arabisch"), "zh": ("chinese", "chinesisch"),
        "ja": ("japanese", "japanisch"), "ko": ("korean", "koreanisch"),
        "ru": ("russian", "russisch"), "el": ("greek", "griechisch"),
        "ro": ("romanian", "rumänisch", "rumaenisch"),
    }
    language_branches = []
    for code, values in aliases.items():
        for index, alias in enumerate(values):
            if code == "de" and alias == "deutsch":
                alias_pattern = r"\bdeutsch(?:kenntnis(?:se|sen)?)?\b"
            else:
                alias_pattern = rf"\b{re.escape(alias)}\b"
            language_branches.append(f"(?P<lang_{code}_{index}>{alias_pattern})")
    lang_rx = re.compile("|".join(language_branches), re.I)
    level_rx = re.compile(r"\b([abc][12])\b", re.I)
    req_markers = re.compile(
        r"\b(required|required to|must|need to|proficient|fluent(?:ly)?|working language|language skills|"
        r"kenntnisse|kenntnis|erforderlich|vorausgesetzt|verhandlungssicher\w*|muttersprachlich|fliessend|fließend)\b|"
        r"deutschkenntnis(?:se|sen)?",
        re.I,
    )
    optional_markers = re.compile(
        r"\b(preferred|nice to have|an advantage|wünschenswert|wuenschenswert|von vorteil|accepted|acceptable|ausreichend|bonus|bonuses)\b",
        re.I,
    )
    negated_markers = re.compile(
        r"\b(not required|not necessary|not needed|isn't required|is not a requirement|"
        r"nicht erforderlich|nicht notwendig|nicht vorausgesetzt|kein(?:e)?\s+\w+\s+(?:erforderlich|notwendig))\b",
        re.I,
    )
    fluency = re.compile(
        r"\b(fluent(?:ly)?|native|business fluent|near-native|verhandlungssicher\w*|muttersprachlich|fliessend|fließend|excellent|very good|fluency)\b",
        re.I,
    )
    fields = [
        (str(job.get("language_requirement") or ""), True),
        (str(job.get("language_requirements") or ""), True),
        (str(job.get("requirements_and_details") or ""), False),
        (str(job.get("title") or ""), False),
        (str(job.get("description") or job.get("description_text") or ""), False),
    ]
    observations: dict[str, list[tuple[str, bool, str]]] = {code: [] for code in aliases}
    for raw, authoritative in fields:
        if not raw:
            continue
        clauses = re.split(r"(?<=[.!?;\n])|\s+\band\b\s+|\s+\bund\b\s+|\s*,\s*", raw, flags=re.I)
        merged_clauses: list[str] = []
        for clause in clauses:
            current_norm = norm(clause)
            current_has_language = bool(lang_rx.search(current_norm))
            current_has_level = bool(level_rx.search(current_norm))
            if merged_clauses and len(current_norm) <= 100:
                previous_norm = norm(merged_clauses[-1])
                previous_has_language = bool(lang_rx.search(previous_norm))
                previous_has_level = bool(level_rx.search(previous_norm))
                if ((previous_has_language and not previous_has_level and current_has_level and not current_has_language)
                        or (previous_has_level and not previous_has_language and current_has_language and not current_has_level)):
                    merged_clauses[-1] += " " + clause
                    continue
            merged_clauses.append(clause)
        clauses = merged_clauses
        for clause in clauses:
            normalized = norm(clause)
            hits = list(lang_rx.finditer(normalized))
            if not hits:
                continue
            for index, hit in enumerate(hits):
                start = hits[index - 1].end() if index else 0
                end = hits[index + 1].start() if index + 1 < len(hits) else len(normalized)
                context = (normalized[max(0, hit.start() - 24):min(len(normalized), hit.end() + 48)]
                           if len(hits) == 1 else normalized[start:end])
                if negated_markers.search(context):
                    continue
                explicit = authoritative or bool(req_markers.search(clause))
                if not explicit and not (level_rx.search(context) or fluency.search(context)):
                    continue
                optional = bool(optional_markers.search(context) or optional_markers.search(clause))
                level = level_rx.search(context)
                code = hit.lastgroup.split("_")[1] if hit.lastgroup else ""
                if code not in observations:
                    continue
                observations[code].append(
                    (level.group(1).upper() if level else "", optional, context)
                )
    reasons: list[str] = []
    german_level = ""
    mismatch = False
    for code, entries in observations.items():
        if not entries:
            continue
        if code == "de":
            levels = [level for level, _, _ in entries if level]
            german_level = max(levels, key=lambda value: level_order[value.casefold()]) if levels else ""
            too_high = bool(german_level and max_german in level_order and
                            level_order[german_level.casefold()] > level_order[max_german])
            asks_fluency = any(fluency.search(context) for _, _, context in entries)
            optional = all(is_optional for _, is_optional, _ in entries)
            if (too_high or (asks_fluency and max_german != "c2")) and optional:
                reasons.append("German above the configured limit is preferred; review whether it is required")
            elif too_high or (asks_fluency and max_german != "c2"):
                mismatch |= not optional
                reasons.append("German requirement exceeds the configured level" if too_high or asks_fluency
                               else "German requirement level is unclear")
            elif not german_level:
                reasons.append("German requirement level is unclear")
            elif code not in allowed:
                mismatch |= not optional
                reasons.append("German requirement is not an allowed configured language")
        elif code not in allowed:
            mismatch |= not all(is_optional for _, is_optional, _ in entries)
            reasons.append(f"additional language requirement: {aliases[code][0]}")
    has_english = "en" in allowed and bool(observations["en"])
    german_levels = [level for level, _, _ in observations["de"] if level]
    has_allowed_german = (
        "de" in allowed and bool(german_levels) and max_german in level_order
        and all(level_order[level.casefold()] <= level_order[max_german] for level in german_levels)
        and not any(fluency.search(context) for _, _, context in observations["de"])
    )
    has_allowed = has_english or has_allowed_german
    if not any(observations.values()):
        reasons.append("required language not stated clearly")
    status = "language_mismatch" if mismatch else ("language_allowed" if has_allowed else "language_unclear")
    return status, german_level, reasons


def _hours(job: dict[str, Any]) -> tuple[str, int | None, int | None]:
    structured_hours = job.get("hours_per_week")
    if isinstance(structured_hours, (int, float)) and not isinstance(structured_hours, bool):
        if structured_hours > 0:
            exact = int(structured_hours)
            return "hours_listed", exact, exact
    elif isinstance(structured_hours, str) and re.fullmatch(r"\s*\d{1,3}(?:[.,]\d+)?\s*", structured_hours):
        parsed_hours = float(structured_hours.strip().replace(",", "."))
        if parsed_hours > 0:
            exact = int(parsed_hours)
            return "hours_listed", exact, exact
    text = " ".join(str(job.get(k) or "") for k in
                    ("schedule", "hours", "hours_per_week", "employment_type", "title", "description", "description_text"))
    low = norm(text)
    ranges = re.findall(
        r"\b(\d{1,3})\s*(?:-|–|—|to|bis)\s*(\d{1,3})\s*(?:hours?|h|std\.?|stunden|uur)"
        r"\s*(?:(?:/|per|pro|a)\s*)?(?:week|woche|wk)?\b", low,
    )
    if ranges:
        bounds = [(int(low_value), int(high_value)) for low_value, high_value in ranges
                  if int(low_value) <= int(high_value)]
        if bounds:
            low_value = min(pair[0] for pair in bounds)
            high_value = max(pair[1] for pair in bounds)
            return "hours_range", low_value, high_value
    values = [int(n) for n in re.findall(r"\b(\d{1,2})\s*(?:hours?|std\.?|stunden|uur)\s*(?:per|pro|a)?\s*(?:week|woche)?\b", low)]
    if values:
        exact = max(values)
        return "hours_listed", exact, exact
    if re.search(r"\b(full[- ]?time|vollzeit|temps plein)\b", low):
        return "full_time", None, None
    if re.search(r"\b(part[- ]?time|teilzeit|minijob|aushilfe)\b", low):
        return "part_time_unknown", None, None
    return "hours_unclear", None, None


def _evidence_exclusions(job: dict[str, Any], config: dict[str, Any]) -> list[str]:
    title = str(job.get("title") or "")
    body = " ".join(str(job.get(k) or "") for k in ("title", "description", "description_text"))
    rules = config.get("exclusions", {})
    out = []
    for label, patterns in rules.items():
        for pattern in patterns if isinstance(patterns, list) else [patterns]:
            if re.search(pattern, title if label in {"student", "senior", "non_target_title"} else body, re.I):
                out.append(f"{label}: {pattern}")
                break
    return out


def _review_title_exception(title: str, config: dict[str, Any]) -> str:
    """Return a configured title exception that downgrades a senior exclusion to review."""
    for pattern in config.get("review_title_exceptions", []):
        if re.search(str(pattern), title, re.I):
            return str(pattern)
    return ""


def _explicit_remote_region(location: str) -> str:
    """Recognize explicit non-Germany remote restrictions without guessing cities."""
    text = norm(location)
    if re.search(r"\b(germany|deutschland)\b|(?<!\w)de(?!\w)", text, re.I):
        return ""
    regions = (
        ("North America", r"\bnorth\s+america(?:n)?\b|\bna[- ]only\b"),
        ("United States", r"\bunited states(?: of america)?\b|\b(?:u\.s\.?(?:a\.)?|usa|us)(?:[- ]only)?(?=\W|$)"),
        ("Canada", r"\bcanada(?:[- ]only)?\b"),
        ("India", r"\bindia(?:[- ]only)?\b"),
        ("Caribbean", r"\bcaribbean\b"),
    )
    return next((name for name, pattern in regions if re.search(pattern, text, re.I)), "")


def evaluate_job(job: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Return deterministic group/tier matches and an explicit review reason list."""
    title = str(job.get("title") or "")
    groups = config["groups"]
    tiers = config["tiers"]
    tier_hits = _tier_hits(title, tiers)
    city_matches = _city_matches(str(job.get("location") or ""), groups)
    work_mode = _mode(job)
    country = _country(job)
    language_status, german_level, language_reasons = _language(job, config)
    hour_status, hours, hours_max = _hours(job)
    exclusions = _evidence_exclusions(job, config)
    review_title_exception = _review_title_exception(title, config)
    if review_title_exception:
        exclusions = [reason for reason in exclusions if not reason.startswith("senior:")]
    if work_mode == "remote" and not country:
        remote_region = _explicit_remote_region(str(job.get("location") or ""))
        if remote_region:
            exclusions.append(f"remote eligibility restricted outside Germany ({remote_region})")
    if language_status == "language_mismatch":
        exclusions.extend(language_reasons)

    # A specific country allowance is required for a Germany-remote match.
    remote_reasons: list[str] = []
    remote_explicit = work_mode == "remote" and country == "DE"
    if work_mode == "remote" and not country:
        remote_reasons.append("remote work country not stated")
    elif work_mode == "remote" and country != "DE":
        exclusions.append(f"remote work outside Germany ({country})")
    # A recognized country outside Germany is decisive even when the feed
    # omits a city or happens to mention a target city in another field.
    elif country and country != "DE":
        exclusions.append(f"job location outside Germany ({country})")
    remote_tiers = {str(x) for x in groups.get(config.get("remote_group_id", "09-remote-germany"), {}).get("tiers", [])}
    if remote_explicit and tier_hits and not (set(tier_hits) & remote_tiers):
        exclusions.append("job tier is not enabled for Germany remote searches")

    candidates: list[dict[str, str]] = []
    for group_id, spec in groups.items():
        if group_id == config.get("remote_group_id", "09-remote-germany"):
            if remote_explicit:
                for tier in tier_hits:
                    if tier in {str(x) for x in spec.get("tiers", [])}:
                        candidates.append({"group_id": group_id, "tier": tier})
            continue
        for city_group, city in city_matches:
            if city_group != group_id:
                continue
            allowed_modes = {str(x).casefold() for x in spec.get("work_modes", [])}
            for tier in tier_hits:
                if tier not in {str(x) for x in spec.get("tiers", [])}:
                    continue
                if work_mode and work_mode.casefold() not in allowed_modes:
                    continue
                candidates.append({"group_id": group_id, "tier": tier, "city": city})

    reasons = []
    if not tier_hits:
        reasons.append("no configured tier phrase matched the title")
    if not candidates and tier_hits:
        if not city_matches and not remote_explicit:
            reasons.append("actual job location does not identify a configured city group")
        elif work_mode == "remote" and not country:
            reasons.extend(remote_reasons)
        else:
            reasons.append("actual job location/work mode is outside configured group rules")
    elif city_matches and not work_mode:
        reasons.append("work mode is unclear")
    for reason in language_reasons:
        if reason not in exclusions:
            reasons.append(reason)
    if review_title_exception:
        reasons.append("configured senior-title exception requires human review")
    below_threshold = False
    for tier in tier_hits:
        threshold = config.get("minimum_hours_by_tier", {}).get(tier)
        if threshold is None:
            continue
        threshold = int(threshold)
        if hour_status == "hours_listed" and hours is not None and hours < threshold:
            below_threshold = True
            reasons.append(f"Tier {tier} role lists fewer than the configured {threshold} hours per week")
        elif hour_status == "hours_range" and hours is not None and hours_max is not None:
            if hours >= threshold:
                continue
            if hours_max < threshold:
                below_threshold = True
                reasons.append(f"Tier {tier} role lists fewer than the configured {threshold} hours per week")
            else:
                reasons.append(f"Tier {tier} hours range crosses the configured {threshold}-hour minimum")
        elif hour_status in {"part_time_unknown", "hours_unclear"}:
            reasons.append(f"Tier {tier} hours/full-time status is unclear against the configured {threshold}-hour minimum")
        elif hour_status == "full_time":
            continue
    reasons.extend(exclusions)

    if exclusions or below_threshold or not tier_hits:
        status = "exclude"
        candidates = []
    elif reasons or not candidates:
        status = "review"
    else:
        status = "match"

    # Advert language is informational and never substitutes for language requirements.
    ad_language = str(job.get("lang_hint") or "").split("-", 1)[0].lower()
    if not ad_language:
        sample = str(job.get("description") or job.get("description_text") or "")
        sample_norm = norm(sample)
        if re.search(r"\b(auf deutsch|in deutscher sprache)\b", sample_norm):
            ad_language = "de"
        elif re.search(r"\b(in english|english-language advert)\b", sample_norm):
            ad_language = "en"
        tokens = re.findall(r"[a-zäöüß]+", norm(sample))
        stops = {
            "en": {"the", "and", "you", "with", "for", "our", "are", "will", "your", "this", "experience", "team", "work"},
            "de": {"und", "der", "die", "das", "mit", "fur", "wir", "sie", "ihre", "bei", "eine", "auf", "sind", "werden"},
        }
        counts = {key: sum(word in words for word in tokens) for key, words in stops.items()}
        if not ad_language:
            ad_language = max(counts, key=counts.get) if tokens and max(counts.values()) >= 3 else ""

    return {
        "status": status,
        "group_tier_matches": candidates,
        "tier_candidates": tier_hits,
        "matched_city_groups": city_matches,
        "work_mode": work_mode,
        "country": country,
        "language_requirement_status": language_status,
        "german_level": german_level,
        "advert_language": ad_language,
        "hours_status": hour_status,
        "hours_per_week": hours,
        "hours_per_week_max": hours_max,
        "reasons": reasons,
    }
