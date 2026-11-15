"""Compatibility wrapper for the config-driven matching engine."""
from __future__ import annotations

import re

from pendel.targets import evaluate_job


def detect_lang(text: str) -> str:
    words = re.findall(r"[a-zäöüß]+", (text or "").casefold())
    if len(words) < 15:
        return ""
    stop = {
        "en": {"the", "and", "you", "with", "for", "our", "are", "will", "your", "this", "experience", "team", "work"},
        "de": {"und", "der", "die", "das", "mit", "für", "wir", "sie", "ihre", "bei", "eine", "auf", "sind", "werden"},
        "nl": {"het", "een", "van", "voor", "met", "wij", "jij", "bij", "zijn", "naar", "ons", "jouw", "werk"},
    }
    counts = {language: sum(word in words for word in terms) for language, terms in stop.items()}
    language = max(counts, key=counts.get)
    return language if counts[language] >= 3 else ""


def tag_job(job: dict, config: dict) -> dict:
    """Classify against caller-supplied private target settings."""
    return evaluate_job(job, config)


def decide(tags: dict) -> tuple[str, list[str]]:
    """Keep the familiar verdict shape while exposing uncertainty as review."""
    return tags["status"], list(tags.get("reasons", []))
