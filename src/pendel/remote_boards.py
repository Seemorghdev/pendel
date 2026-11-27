"""S18: readers for remote-job boards with documented public APIs.

Each board asks to be credited and linked; every job keeps the board's own listing
URL and a ``via`` name, which the Telegram messages show. Request frequency is
limited by ``fetch_intervals_minutes`` in the target config (Remotive asks for at
most 4 requests a day).

| ats       | endpoint                              | notes                                   |
|-----------|---------------------------------------|-----------------------------------------|
| remotive  | https://remotive.com/api/remote-jobs  | all active jobs in one response         |
| jobicy    | https://jobicy.com/api/jobs           | last 7 days, cursor pages of up to 100  |
| remoteok  | https://remoteok.com/api              | first array element is the legal notice |
| himalayas | https://himalayas.app/jobs/api        | newest first, cursor pages of 20        |
| weworkremotely | https://weworkremotely.com/…/*.rss | S19: public RSS; asks for links back    |
"""

from __future__ import annotations

import html
import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

from pendel import fetch

NAMES = {"remotive": "Remotive", "jobicy": "Jobicy", "remoteok": "Remote OK", "himalayas": "Himalayas",
         "weworkremotely": "We Work Remotely"}
JOBICY_MAX_PAGES = 10
HIMALAYAS_MAX_PAGES = 25
DESCRIPTION_CHARS = 20_000
# Timezone restrictions that only cover the Americas mean working nights from Armenia.
NIGHT_MAX_UTC_OFFSET = -3


def _text(value) -> str:
    """Plain text from an HTML description, short enough to keep in reports."""
    raw = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", str(value or ""), flags=re.I)
    raw = re.sub(r"<[^>]+>", " ", raw)
    return re.sub(r"[ \t]+", " ", html.unescape(raw)).strip()[:DESCRIPTION_CHARS]


def _iso(value) -> str:
    if isinstance(value, (int, float)) and value > 0:
        return datetime.fromtimestamp(value, timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return str(value or "")


def _pay(low, high, currency, period="") -> str:
    def amount(x):
        try:
            number = float(x)
        except (TypeError, ValueError):
            return ""
        return f"{number:,.0f}" if number > 0 else ""
    low, high = amount(low), amount(high)
    span = f"{low}–{high}" if low and high and low != high else (low or high)
    if not span:
        return ""
    unit = {"yearly": "year", "annual": "year", "monthly": "month", "hourly": "hour",
            "weekly": "week", "daily": "day"}.get(str(period or "").casefold(), "")
    return f"{span} {str(currency or 'USD').upper()}" + (f"/{unit}" if unit else "")


def _job(job_id, title, company, location, url, posted, salary, description, schedule="") -> dict:
    return {"job_id": str(job_id or ""), "title": str(title or "").strip(), "company": str(company or "").strip(),
            "location": str(location or "").strip(), "country": "", "work_mode": "remote",
            "url": str(url or ""), "posted_at": _iso(posted), "salary": salary, "lang_hint": "",
            "description": _text(description), "schedule": schedule}


def parse_remotive(data: dict) -> list[dict]:
    return [_job(x.get("id"), x.get("title"), x.get("company_name"), x.get("candidate_required_location"),
                 x.get("url"), x.get("publication_date"), str(x.get("salary") or ""), x.get("description"))
            for x in data.get("jobs", []) if isinstance(x, dict)]


def parse_jobicy(data: dict) -> list[dict]:
    return [_job(x.get("id"), x.get("jobTitle"), x.get("companyName"), x.get("jobGeo"), x.get("url"),
                 x.get("pubDate"), _pay(x.get("salaryMin"), x.get("salaryMax"), x.get("salaryCurrency"),
                                        x.get("salaryPeriod")), x.get("jobDescription"))
            for x in data.get("jobs", []) if isinstance(x, dict)]


def parse_remoteok(data: list) -> list[dict]:
    # The first element is the API's legal notice, not a job.
    return [_job(x.get("id"), x.get("position"), x.get("company"), x.get("location") or "",
                 x.get("url"), x.get("date") or x.get("epoch"),
                 _pay(x.get("salary_min"), x.get("salary_max"), "USD", "yearly"), x.get("description"))
            for x in data if isinstance(x, dict) and x.get("id") and x.get("position")]


def parse_himalayas(data: dict) -> list[dict]:
    out = []
    for x in data.get("jobs", []):
        if not isinstance(x, dict):
            continue
        # An empty restriction list means the job is open to any country.
        places = [str(p) for p in x.get("locationRestrictions") or []]
        zones = [z for z in x.get("timezoneRestrictions") or [] if isinstance(z, (int, float))]
        night = bool(zones) and max(zones) <= NIGHT_MAX_UTC_OFFSET
        out.append(_job(x.get("guid") or x.get("applicationLink"), x.get("title"), x.get("companyName"),
                        ", ".join(places) or "Worldwide", x.get("guid") or x.get("applicationLink"),
                        x.get("pubDate"), _pay(x.get("minSalary"), x.get("maxSalary"), x.get("currency"),
                                               x.get("salaryPeriod")),
                        x.get("description") or x.get("excerpt"), "night" if night else ""))
    return out


def parse_weworkremotely(xml_text: str) -> list[dict]:
    """S19: We Work Remotely's public RSS. Titles read "Company: Role"; the region is
    its own element ("Anywhere in the World", "EMEA Only", "USA Only")."""
    root = ElementTree.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        fields = {child.tag.rsplit("}", 1)[-1].casefold(): (child.text or "").strip() for child in item}
        company, _, role = fields.get("title", "").partition(":")
        if not role:
            company, role = "", company
        place = ", ".join(x for x in (fields.get("region", ""), fields.get("country", "")) if x)
        try:
            posted = parsedate_to_datetime(fields.get("pubdate", "")).astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            posted = ""
        link = fields.get("link") or fields.get("guid", "")
        out.append(_job(link, role, company, place, link, posted.replace("+00:00", "Z"), "", fields.get("description")))
    return out


def fetch_jobs(source: dict, throttle) -> list[dict]:
    """Return the parsed jobs of one board feed URL. Pages are capped; the newest jobs
    come first, so a capped board is still treated as complete."""
    ats, url = source["ats"], source["feed_url"]
    if ats == "remotive":
        return parse_remotive(json.loads(fetch.get(url, throttle)))
    if ats == "remoteok":
        return parse_remoteok(json.loads(fetch.get(url, throttle)))
    if ats == "weworkremotely":
        return parse_weworkremotely(fetch.get(url, throttle))
    if ats in {"jobicy", "himalayas"}:
        parse = parse_jobicy if ats == "jobicy" else parse_himalayas
        pages = JOBICY_MAX_PAGES if ats == "jobicy" else HIMALAYAS_MAX_PAGES
        out: list[dict] = []
        cursor = ""
        for _ in range(pages):
            data = json.loads(fetch.get(fetch.with_query(url, f"cursor={cursor}") if cursor else url, throttle))
            out.extend(parse(data))
            cursor = data.get("nextCursor") or ""
            if not cursor:
                break
        return out
    raise ValueError(f"no remote board reader for ats '{ats}'")
