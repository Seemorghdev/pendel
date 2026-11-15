"""Turn each recruiting system's public feed into one common job shape.

Parsers are pure: they take the decoded response (JSON object or XML text)
and return a list of Job dicts. No network here; see pendel.fetch.

Job fields:
  job_id      id from the recruiting system (string)
  title
  location    free text as published
  country     ISO 3166 alpha-2 when the feed gives it, else ""
  work_mode   "remote" | "hybrid" | "onsite" | ""   ("" = not stated)
  url         public job page
  posted_at   ISO date (YYYY-MM-DD) when given, else ""
  salary      short text when given, else ""
  lang_hint   language code when the feed states it, else ""
  description plain text, truncated
"""
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# Requirements ("fluent German") usually sit at the end of a posting, so keep it whole.
DESC_MAX = 20000

COUNTRY_NAMES = {
    "germany": "DE", "deutschland": "DE", "netherlands": "NL", "the netherlands": "NL", "nederland": "NL",
    "austria": "AT", "österreich": "AT", "switzerland": "CH", "schweiz": "CH", "belgium": "BE",
    "united kingdom": "GB", "uk": "GB", "england": "GB", "scotland": "GB",
    "france": "FR", "spain": "ES", "portugal": "PT", "italy": "IT", "poland": "PL", "ireland": "IE",
    "denmark": "DK", "sweden": "SE", "norway": "NO", "finland": "FI", "iceland": "IS", "estonia": "EE",
    "latvia": "LV", "lithuania": "LT", "czech republic": "CZ", "czechia": "CZ", "slovakia": "SK",
    "hungary": "HU", "romania": "RO", "bulgaria": "BG", "greece": "GR", "croatia": "HR", "slovenia": "SI",
    "serbia": "RS", "albania": "AL", "north macedonia": "MK", "bosnia": "BA", "montenegro": "ME",
    "kosovo": "XK", "cyprus": "CY", "malta": "MT", "luxembourg": "LU", "ukraine": "UA", "georgia": "GE",
    "armenia": "AM", "azerbaijan": "AZ", "turkey": "TR", "türkiye": "TR", "israel": "IL",
    "united states": "US", "usa": "US", "canada": "CA", "mexico": "MX", "brazil": "BR", "argentina": "AR",
    "chile": "CL", "colombia": "CO", "peru": "PE", "uruguay": "UY", "paraguay": "PY", "ecuador": "EC",
    "guatemala": "GT", "el salvador": "SV", "costa rica": "CR",
    "india": "IN", "philippines": "PH", "singapore": "SG", "malaysia": "MY", "indonesia": "ID",
    "thailand": "TH", "vietnam": "VN", "japan": "JP", "south korea": "KR", "korea": "KR", "china": "CN",
    "hong kong": "HK", "taiwan": "TW", "australia": "AU", "new zealand": "NZ",
    "united arab emirates": "AE", "uae": "AE", "saudi arabia": "SA", "qatar": "QA", "egypt": "EG",
    "morocco": "MA", "south africa": "ZA", "nigeria": "NG", "kenya": "KE",
}


def text(s, limit=DESC_MAX):
    """HTML (possibly escaped) to plain text."""
    if not s:
        return ""
    s = html.unescape(s)
    s = re.sub(r"<(br|/p|/li|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*", "\n", s).strip()
    return s[:limit]


def country_code(value):
    """'DE', 'de', 'Germany', 'Exampleton, Germany' -> 'DE'; unknown -> ''."""
    if not value:
        return ""
    v = value.strip()
    if len(v) == 2 and v.isalpha():
        return v.upper()
    low = v.lower()
    for name, code in COUNTRY_NAMES.items():
        if re.search(rf"\b{re.escape(name)}\b", low):
            return code
    return ""


def mode_from(value):
    """Normalise the many spellings of workplace type."""
    if not value:
        return ""
    v = str(value).lower().replace("-", "").replace("_", "").replace(" ", "")
    if "remote" in v:
        return "remote"
    if "hybrid" in v:
        return "hybrid"
    if v in ("onsite", "office", "inoffice", "inperson"):
        return "onsite"
    return ""


def mode_from_text(*parts):
    """Fallback: look for remote/hybrid in title or location."""
    s = " ".join(p for p in parts if p).lower()
    if re.search(r"\bhybrid\b", s):
        return "hybrid"
    if re.search(r"\b(remote|home ?office|work from home|fully remote)\b", s):
        return "remote"
    return ""


def iso_date(value):
    """ISO string or epoch ms -> 'YYYY-MM-DD'."""
    if value in (None, ""):
        return ""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    m = re.match(r"\d{4}-\d{2}-\d{2}", str(value))
    return m.group(0) if m else ""


def rss_date(value):
    """Parse an RSS publication date into YYYY-MM-DD when possible."""
    if not value:
        return ""
    direct = iso_date(value)
    if direct:
        return direct
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, OverflowError):
        return ""
    return parsed.date().isoformat() if parsed else ""


def job(**kw):
    base = dict(job_id="", title="", location="", country="", work_mode="", url="", posted_at="",
                salary="", lang_hint="", description="")
    base.update({k: ("" if v is None else v) for k, v in kw.items()})
    base["job_id"] = str(base["job_id"])
    return base


# ---- Greenhouse: GET boards-api.greenhouse.io/v1/boards/{t}/jobs?content=true
def parse_greenhouse(data):
    out = []
    for j in data.get("jobs", []):
        loc = (j.get("location") or {}).get("name", "")
        out.append(job(
            job_id=j.get("id"), title=j.get("title", ""), location=loc,
            country=country_code(loc.split(",")[-1]) if loc else "",
            work_mode=mode_from_text(loc, j.get("title", "")),
            url=j.get("absolute_url", ""),
            posted_at=iso_date(j.get("first_published") or j.get("updated_at")),
            lang_hint=j.get("language") or "",
            description=text(j.get("content", "")),
        ))
    return out


# ---- Lever: GET api.lever.co/v0/postings/{t}?mode=json  (list)
def parse_lever(data):
    out = []
    for j in data:
        cat = j.get("categories") or {}
        loc = cat.get("location", "")
        sal = j.get("salaryRange") or {}
        salary = ""
        if sal.get("min") or sal.get("max"):
            salary = f"{sal.get('min', '')}–{sal.get('max', '')} {sal.get('currency', '')} {sal.get('interval', '')}".strip()
        out.append(job(
            job_id=j.get("id"), title=j.get("text", ""), location=loc,
            country=country_code(j.get("country", "")) or country_code(loc),
            work_mode=mode_from(j.get("workplaceType")) or mode_from_text(loc, j.get("text", "")),
            url=j.get("hostedUrl", ""), posted_at=iso_date(j.get("createdAt")), salary=salary,
            description=text(j.get("descriptionPlain") or j.get("description", "")),
        ))
    return out


# ---- Ashby: GET api.ashbyhq.com/posting-api/job-board/{t}?includeCompensation=true
def parse_ashby(data):
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        loc = j.get("location", "")
        comp = j.get("compensation") or {}
        out.append(job(
            job_id=j.get("id"), title=j.get("title", ""), location=loc,
            country=country_code(addr.get("addressCountry", "")) or country_code(loc),
            work_mode=mode_from(j.get("workplaceType")) or ("remote" if j.get("isRemote") else "")
                      or mode_from_text(loc, j.get("title", "")),
            url=j.get("jobUrl", ""), posted_at=iso_date(j.get("publishedAt")),
            salary=comp.get("compensationTierSummary", "") or "",
            description=text(j.get("descriptionPlain") or j.get("descriptionHtml", "")),
        ))
    return out


# ---- Recruitee: GET {t}.recruitee.com/api/offers/
def parse_recruitee(data):
    out = []
    for o in data.get("offers", []):
        if o.get("remote"):
            mode = "remote"
        elif o.get("hybrid"):
            mode = "hybrid"
        elif o.get("on_site"):
            mode = "onsite"
        else:
            mode = mode_from_text(o.get("location", ""), o.get("title", ""))
        sal = o.get("salary") or {}
        salary = ""
        if isinstance(sal, dict) and (sal.get("min") or sal.get("max")):
            salary = f"{sal.get('min', '')}–{sal.get('max', '')} {sal.get('currency', '')} {sal.get('period', '')}".strip()
        out.append(job(
            job_id=o.get("id"), title=o.get("title", ""), location=o.get("location") or o.get("city", ""),
            country=country_code(o.get("country_code", "")) or country_code(o.get("country", "")),
            work_mode=mode, url=o.get("careers_url", ""), posted_at=iso_date(o.get("published_at")),
            salary=salary, description=text(o.get("description", "")),
        ))
    return out


# ---- SmartRecruiters: GET api.smartrecruiters.com/v1/companies/{t}/postings?limit=100&offset=N
def parse_smartrecruiters(data, token):
    out = []
    for p in data.get("content", []):
        loc = p.get("location") or {}
        if loc.get("remote"):
            mode = "remote"
        elif loc.get("hybrid"):
            mode = "hybrid"
        else:
            mode = mode_from_text(p.get("name", ""))
        place = ", ".join(x for x in (loc.get("city"), loc.get("region"), loc.get("country", "").upper()) if x)
        out.append(job(
            job_id=p.get("id"), title=p.get("name", ""), location=place,
            country=country_code(loc.get("country", "")), work_mode=mode,
            url=f"https://jobs.smartrecruiters.com/{token}/{p.get('id')}",
            posted_at=iso_date(p.get("releasedDate")),
            lang_hint=((p.get("language") or {}).get("code") or "")[:2],
        ))
    return out


# ---- Personio: GET {t}.jobs.personio.de/xml
def parse_personio(xml_text, token, tld="de"):
    root = ET.fromstring(xml_text)
    out = []
    for p in root.iter("position"):
        get = lambda tag: (p.findtext(tag) or "").strip()
        desc = " ".join(text(d.findtext("value") or "") for d in p.iter("jobDescription"))
        office = get("office")
        out.append(job(
            job_id=get("id"), title=get("name"), location=office,
            country=country_code(office), work_mode=mode_from_text(office, get("name")),
            url=f"https://{token}.jobs.personio.{tld}/job/{get('id')}",
            posted_at=iso_date(get("createdAt")), description=desc[:DESC_MAX],
        ))
    return out


def parse_softgarden(data):
    """Parse the public Softgarden jobs.feed.json / JSON-LD payload."""
    postings = []
    def walk(value):
        if isinstance(value, dict):
            kind = value.get("@type", "")
            if (kind == "JobPosting" or (isinstance(kind, list) and "JobPosting" in kind)):
                postings.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
    walk(data)
    out = []
    for p in postings:
        ident = p.get("identifier") or {}
        job_id = ident.get("value", "") if isinstance(ident, dict) else ident
        locations = p.get("jobLocation") or []
        if not isinstance(locations, list):
            locations = [locations]
        places = []
        for item in locations:
            address = item.get("address", {}) if isinstance(item, dict) else {}
            if isinstance(address, list):
                for candidate in address:
                    if isinstance(candidate, dict):
                        places.append(", ".join(str(x) for x in
                                                 (candidate.get("addressLocality", ""), candidate.get("addressCountry", "")) if x))
            elif isinstance(address, dict):
                places.append(", ".join(str(x) for x in
                                         (address.get("addressLocality", ""), address.get("addressCountry", "")) if x))
        location = "; ".join(x for x in places if x)
        url = p.get("url", "")
        out.append(job(
            job_id=job_id or url or p.get("title", ""), title=text(p.get("title", "")),
            location=location, country=country_code(location),
            work_mode=mode_from(p.get("jobLocationType")) or mode_from_text(location, p.get("title", "")),
            url=url, posted_at=iso_date(p.get("datePosted")),
            salary=str((p.get("baseSalary") or {}).get("value", "")) if isinstance(p.get("baseSalary"), dict) else "",
            description=text(p.get("description", "")),
        ))
    return _unique_jobs(out)


def parse_pinpoint(data):
    """Parse the public Pinpoint postings.json response."""
    items = data.get("data", []) if isinstance(data, dict) else data
    out = []
    for p in items if isinstance(items, list) else []:
        loc = p.get("location") or {}
        if not isinstance(loc, dict):
            loc = {"name": str(loc)}
        location = ", ".join(str(x) for x in
                              (loc.get("name"), loc.get("city"), loc.get("province"), loc.get("country")) if x)
        out.append(job(
            job_id=p.get("id") or p.get("url") or p.get("title", ""),
            title=text(p.get("title", "")), location=location,
            country=country_code(loc.get("country", "") or location),
            work_mode=mode_from(p.get("workplace_type_text") or p.get("workplace_type"))
                      or mode_from_text(location, p.get("title", "")),
            url=p.get("url", ""), posted_at=iso_date(p.get("created_at")),
            description=text(p.get("description", "")),
        ))
    return _unique_jobs(out)


def _xml_local_name(tag):
    return tag.rsplit("}", 1)[-1].casefold()


def _xml_values(element, names):
    wanted = {name.casefold() for name in names}
    values = []
    for child in element.iter():
        if child is element or _xml_local_name(child.tag) not in wanted:
            continue
        value = " ".join(part.strip() for part in child.itertext() if part.strip())
        if value and value not in values:
            values.append(value)
    return values


def _xml_value(element, names):
    return next(iter(_xml_values(element, names)), "")


def parse_teamtailor_rss(xml_text):
    """Parse Teamtailor's public jobs RSS feed into Pendel's common job shape."""
    root = ET.fromstring(xml_text)
    out = []
    for item in (node for node in root.iter() if _xml_local_name(node.tag) == "item"):
        title = _xml_value(item, ("title",))
        url = _xml_value(item, ("link", "url", "apply_url"))
        if not url:
            link_node = next((node for node in item.iter()
                              if _xml_local_name(node.tag) == "link" and node.get("href")), None)
            url = link_node.get("href", "") if link_node is not None else ""
        guid = _xml_value(item, ("guid", "id", "global_id"))
        location = "; ".join(_xml_values(item, ("location", "locations")))
        remote = _xml_value(item, ("remote", "remote_status", "is_remote"))
        mode = "remote" if remote.casefold() in {"yes", "true", "1", "remote", "fully remote"} else ""
        published = _xml_value(item, ("pubdate", "published", "published_at", "date"))
        description = _xml_value(item, ("description", "encoded", "content"))
        metadata = []
        for label, aliases in (("Department", ("department",)), ("Role", ("role",)),
                               ("Division", ("division",))):
            value = _xml_value(item, aliases)
            if value:
                metadata.append(f"{label}: {text(value, 500)}")
        if remote:
            metadata.append(f"Teamtailor remote status: {remote}")
        if metadata:
            description = "\n\n".join(part for part in (text(description), *metadata) if part)
        out.append(job(
            job_id=guid or url or title,
            title=text(title),
            location=text(location, 1000),
            country=country_code(location),
            work_mode=mode or mode_from_text(location, title),
            url=url,
            posted_at=rss_date(published),
            description=description[:DESC_MAX],
        ))
    return out


def parse_trakstar_frontend(data):
    """Parse the documented public Trakstar Hire Frontend openings response."""
    # The public Frontend API returns records under ``objects`` (with older
    # responses/documentation sometimes calling them ``openings``).
    openings = (data.get("openings") or data.get("objects") or []) if isinstance(data, dict) else []
    if not isinstance(openings, list):
        raise ValueError("Trakstar Frontend response has no openings list")
    out = []
    for opening in openings:
        if not isinstance(opening, dict):
            continue
        loc = opening.get("location") or {}
        if isinstance(loc, dict):
            city = loc.get("city") or ""
            state = loc.get("state") or ""
            country = loc.get("country") or ""
            location = ", ".join(part for part in (city, state, country) if part)
        else:
            location = str(loc)
            country = location
        description = text(opening.get("description", ""))
        metadata = []
        for label, value in (("Position type", opening.get("position_type")),
                             ("Team", opening.get("team")),
                             ("Remote allowed", opening.get("allows_remote"))):
            if value not in (None, ""):
                metadata.append(f"{label}: {value}")
        if metadata:
            description = "\n\n".join(part for part in (description, *metadata) if part)
        out.append(job(
            job_id=opening.get("id"),
            title=opening.get("title", ""),
            location=location,
            country=country_code(country),
            # allows_remote permits remote work; it does not prove this role is fully remote.
            work_mode=mode_from_text(location, opening.get("title", "")),
            url=opening.get("hosted_url", ""),
            # close_date is not a publication date.
            description=description[:DESC_MAX],
        ))
    return _unique_jobs(out)


def _unique_jobs(items):
    unique = {}
    for item in items:
        key = item.get("job_id") or item.get("url")
        if key:
            unique[str(key)] = item
    return list(unique.values())
