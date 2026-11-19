"""Employer job-alert emails from one Gmail label (S6).

Follows the private housing watcher's Gmail intake: read-only OAuth, a single
label, allow-listed link hosts and processed-message tracking. It never opens
job pages. Each allowed alert link becomes a job whose location and description
come from the email text around it, and the normal matcher decides the rest.

S10: an entry with "board": true is a job board (BA Jobbörse, Indeed, StepStone).
Its cards name a different employer each time, so company and location are read
from the card's text lines in the order given by "card_fields". "job_paths", when
set, keeps only links whose path starts with one of them.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
from html.parser import HTMLParser
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SECRET_NAMES = ("PENDEL_GMAIL_CLIENT_ID", "PENDEL_GMAIL_CLIENT_SECRET", "PENDEL_GMAIL_REFRESH_TOKEN")
DEFAULT_LABEL = "Pendel/Jobs"
DEFAULT_QUERY = "newer_than:14d"
SOURCE_ID = "email_alert|gmail"
BLOCK_TAGS = {"article", "li", "tr", "td", "section"}
TRACKING_KEYS = {"utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "utm_id",
                 "gh_src", "ref", "src", "trk", "mc_cid", "mc_eid"}
UNWRAP_KEYS = ("url", "target", "dest", "redirect", "redirect_url", "u")
# Links in alert emails that are never a job.
SKIP_RX = re.compile(
    r"\b(abmelden|abbestellen|unsubscribe|datenschutz|privacy|impressum|imprint|einstellungen|settings|"
    r"preferences|alle (?:jobs|stellen)|all jobs|view all|login|log in|anmelden|registrieren|profil|"
    r"hilfe|help|kontakt|contact|job[- ]?alert|job[- ]?agent|jobticker|job[- ]?abo|newsletter|"
    r"suchauftrag|suche bearbeiten|edit search|facebook|instagram|linkedin|xing|youtube|tiktok)\b", re.I)
# Button labels; the real title is elsewhere in the same card.
BUTTON_RX = re.compile(
    r"^(job ansehen|jetzt ansehen|stelle ansehen|stellenanzeige ansehen|zum job|zur stelle|details|mehr erfahren|"
    r"mehr informationen|jetzt bewerben|bewerben|view job|view details|see job|apply|apply now|"
    r"learn more|read more|ansehen|öffnen)$", re.I)
URL_RX = re.compile(r"https?://[^\s<>\"']+")
MAX_CONTEXT = 600
_AMOUNT = r"\d{1,3}(?:[.,]\d{3})*(?:[.,]\d{1,2})?"
# "€15.33 - €15.34 pro Stunde", "2.400 € brutto/Monat", "ab 14,50 €/h"
SALARY_RX = re.compile(
    rf"(?:ab\s+)?(?:€\s?{_AMOUNT}|{_AMOUNT}\s?(?:€|EUR))(?:\s*(?:-|–|bis)\s*(?:€\s?{_AMOUNT}|{_AMOUNT}\s?(?:€|EUR)?))?"
    r"(?:\s*(?:brutto|netto))?(?:\s*(?:pro|/|je)\s*(?:Stunde|Std\.?|h|Monat|Jahr|Woche))?", re.I)
DATE_RX = re.compile(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b")
NOISE_RX = re.compile(r"\b(jetzt ansehen|jetzt bewerben|job ansehen|zum job|mehr erfahren|"
                      r"job[- ]?id:?\s*\w+|vollzeit|teilzeit|minijob|befristet|unbefristet)\b", re.I)


def salary_in(text: str) -> str:
    found = SALARY_RX.search(text or "")
    return _normal(found.group(0)) if found else ""


def place_in(context: str, employer: str) -> str:
    """The location part of an alert card: drops pay, dates, buttons and the employer name."""
    text = SALARY_RX.sub(" ", context or "")
    text = DATE_RX.sub(" ", text)
    text = NOISE_RX.sub(" ", text)
    for word in re.split(r"\s*/\s*", employer or ""):
        if len(word) >= 3:
            text = re.sub(re.escape(word), " ", text, flags=re.I)
    return _normal(re.sub(r"^[\s·|,:;–-]+|[\s·|,:;–-]+$", "", re.sub(r"(?:\s*[·|]\s*)+", " · ", text)))


def board_fields(fragments: list[str], title: str, fields: list[str]) -> dict[str, str]:
    """Company and location of a job-board card: its other text lines, in the configured order.

    Lines that are only pay, a date or a button are skipped; "Acme GmbH · 18.11.2026" becomes "Acme GmbH"."""
    lines = []
    for fragment in fragments:
        if fragment == title or BUTTON_RX.match(fragment):
            continue
        cleaned = place_in(fragment, "")
        if len(cleaned) >= 2 and not SKIP_RX.search(cleaned):
            lines.append(cleaned)
    return {field: value for field, value in zip(fields, lines) if field}


class MailClient(Protocol):
    def message_ids(self, label: str, query: str) -> list[str]: ...
    def message(self, message_id: str) -> dict: ...


def configured(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return all(env.get(name) for name in SECRET_NAMES)


def _http_json(url: str, headers: dict[str, str] | None = None, data: bytes | None = None) -> dict:
    with urlopen(Request(url, headers=headers or {}, data=data), timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


class GmailClient:
    """Read-only Gmail API access with the owner's refresh token."""

    def __init__(self, env: Mapping[str, str] | None = None):
        env = os.environ if env is None else env
        self._client_id, self._client_secret, self._refresh_token = (env.get(n, "") for n in SECRET_NAMES)
        self._access_token = ""

    def _headers(self) -> dict[str, str]:
        if not self._access_token:
            body = urlencode({"client_id": self._client_id, "client_secret": self._client_secret,
                              "refresh_token": self._refresh_token, "grant_type": "refresh_token"}).encode()
            result = _http_json("https://oauth2.googleapis.com/token",
                                {"Content-Type": "application/x-www-form-urlencoded"}, body)
            self._access_token = result.get("access_token", "")
            if not self._access_token:
                raise PermissionError("Google did not return an access token")
        return {"Authorization": f"Bearer {self._access_token}"}

    def message_ids(self, label: str, query: str) -> list[str]:
        labels = _http_json("https://gmail.googleapis.com/gmail/v1/users/me/labels", self._headers())
        found = next((item for item in labels.get("labels", []) if item.get("name") == label), None)
        if not found:
            raise LookupError(f"Gmail label {label!r} was not found")
        ids: list[str] = []
        page_token = ""
        for _ in range(5):
            params = {"labelIds": found["id"], "maxResults": "100", "q": query}
            if page_token:
                params["pageToken"] = page_token
            page = _http_json("https://gmail.googleapis.com/gmail/v1/users/me/messages?" + urlencode(params),
                              self._headers())
            ids.extend(item["id"] for item in page.get("messages", []) if item.get("id"))
            page_token = page.get("nextPageToken", "")
            if not page_token:
                break
        return ids

    def message(self, message_id: str) -> dict:
        return _http_json(f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{quote(message_id)}?format=full",
                          self._headers())


def _normal(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("​", "")).strip()


def _decode_part(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")


def _walk_mime(payload: dict):
    if payload.get("body", {}).get("data"):
        yield payload.get("mimeType", ""), _decode_part(payload["body"]["data"])
    for part in payload.get("parts", []):
        yield from _walk_mime(part)


def clean_url(url: str) -> str:
    url = html.unescape(url.strip())
    for _ in range(2):
        params = parse_qs(urlsplit(url).query)
        target = next((params[k][0] for k in UNWRAP_KEYS if params.get(k)), "")
        if not target.startswith(("http://", "https://")):
            break
        url = target
    p = urlsplit(url)
    query = [(k, v) for k, values in parse_qs(p.query, keep_blank_values=True).items()
             if k.casefold() not in TRACKING_KEYS for v in values]
    return urlunsplit((p.scheme.casefold() or "https", p.netloc.casefold(), p.path.rstrip("/"),
                       urlencode(sorted(query)), ""))


def _host_in(host: str, domains: list[str]) -> bool:
    host = host.casefold()
    return any(host == d.casefold() or host.endswith("." + d.casefold()) for d in domains)


class _MailLinks(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[dict] = []
        self.anchors: list[dict] = []
        self.active: dict | None = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        cls = (attrs.get("class") or "").casefold()
        if tag in BLOCK_TAGS or any(term in cls for term in ("card", "job", "result", "listing")):
            self.blocks.append({"tag": tag, "text": []})
        if tag == "a" and attrs.get("href"):
            self.active = {"href": attrs["href"], "text": [], "blocks": list(self.blocks)}

    def handle_endtag(self, tag):
        if tag == "a" and self.active is not None:
            self.active["text"] = _normal(" ".join(self.active["text"]))
            self.anchors.append(self.active)
            self.active = None
        if self.blocks and tag == self.blocks[-1]["tag"]:
            self.blocks.pop()

    def handle_data(self, data):
        text = _normal(data)
        if not text:
            return
        for block in self.blocks:
            block["text"].append(text)
        if self.active is not None:
            self.active["text"].append(text)


def _card_text(anchor: dict) -> list[str]:
    """Text of the smallest enclosing block that holds more than the link itself.

    A block whose text is longer than MAX_CONTEXT is the whole email, not one job card."""
    for block in reversed(anchor["blocks"]):
        if len(" ".join(block["text"])) > MAX_CONTEXT:
            break
        if any(x != anchor["text"] for x in block["text"]):
            return list(block["text"])
    return [anchor["text"]] if anchor["text"] else []


def _raw_links(html_part: str, text_part: str) -> list[tuple[str, str, list[str]]]:
    """(href, link text, nearby text fragments) for every link in the email."""
    if html_part:
        parser = _MailLinks()
        parser.feed(html_part)
        return [(a["href"], a["text"], _card_text(a)) for a in parser.anchors]
    links = []
    lines = [_normal(line) for line in text_part.splitlines()]
    for index, line in enumerate(lines):
        for match in URL_RX.finditer(line):
            before = line[:match.start()].strip(" :-–|")
            # Only the lines directly above the link, up to a blank line or another link.
            context: list[str] = []
            for previous in reversed(lines[max(0, index - 3):index]):
                if not previous or URL_RX.search(previous):
                    break
                context.insert(0, previous)
            title = before or (context[-1] if context else "")
            links.append((match.group(0).rstrip(".,);]"), title, context + ([before] if before else [])))
    return links


def alert_jobs(message: dict, employers: list[dict], received_at: str = "") -> tuple[list[dict], set[str]]:
    """Jobs found in one alert email, and the link hosts of an email with no recognised job."""
    parts = list(_walk_mime(message.get("payload", {})))
    html_part = next((body for mime, body in parts if mime == "text/html"), "")
    text_part = next((body for mime, body in parts if mime == "text/plain"), "")
    headers = message.get("payload", {}).get("headers", [])
    sender = next((_normal(h.get("value")) for h in headers if (h.get("name") or "").casefold() == "from"), "")
    sender_domain = sender.rsplit("@", 1)[-1].strip(" >").casefold() if "@" in sender else ""
    by_sender = next((e for e in employers if sender_domain and _host_in(sender_domain, e.get("sender_domains", []))), None)

    jobs: dict[str, dict] = {}
    hosts: set[str] = set()
    for href, text, fragments in _raw_links(html_part, text_part):
        url = clean_url(href)
        p = urlsplit(url)
        host = p.hostname or ""
        if p.scheme != "https" or not host:
            continue
        hosts.add(host)
        employer = next((e for e in employers if _host_in(host, e.get("job_hosts", []))), None)
        redirect = False
        # A click-tracking host can be shared by several employers; the sender decides first.
        if employer is None and by_sender and _host_in(host, by_sender.get("redirect_hosts", [])):
            employer, redirect = by_sender, True
        if employer is None:
            employer = next((e for e in employers if _host_in(host, e.get("redirect_hosts", []))), None)
            redirect = employer is not None
        if employer is None or p.path in ("", "/"):
            continue
        paths = employer.get("job_paths", [])
        if paths and not redirect and not any(p.path.startswith(x) for x in paths):
            continue
        title = text
        if not title or BUTTON_RX.match(title):
            title = next((x for x in fragments if len(x) >= 4 and not BUTTON_RX.match(x) and not SKIP_RX.search(x)), "")
        if len(title) < 4 or SKIP_RX.search(title) or SKIP_RX.search(text or ""):
            continue
        context = _normal(" ".join(x for x in fragments if x != title and not BUTTON_RX.match(x)))
        # A context this long is the whole email, not one job card. With no card
        # text, the title is the only place a city can be ("Lagerhelfer – Ulm").
        card = context if len(context) <= MAX_CONTEXT else ""
        company = employer["name"]
        if employer.get("board"):
            found = board_fields(fragments if card else [], title, employer.get("card_fields", ["company", "location"]))
            company = found.get("company") or employer["name"]
            location = found.get("location") or place_in(card, company) or title
        else:
            location = place_in(card, company) or title
        job = {"title": title, "company": company, "location": location, "url": url,
               "posted_at": received_at, "description": card or title, "salary": salary_in(card)}
        if redirect:
            # Click-tracking links differ per email; identify the job by its content.
            material = "\x1f".join((company, title, location)).casefold()
            job["job_id"] = "mail-" + hashlib.sha256(material.encode()).hexdigest()[:20]
        prior = jobs.get(url)
        if prior is None or len(title) > len(prior["title"]):
            jobs[url] = job
    return list(jobs.values()), (set() if jobs else hosts)


def read_label(client: MailClient, settings: dict, mail_state: dict,
               now_iso: Callable[[], str]) -> tuple[str, str, list[dict], bool]:
    """Read new alert emails. Returns status, error, jobs, complete, like a feed fetch."""
    label = settings.get("gmail_label", DEFAULT_LABEL)
    try:
        ids = client.message_ids(label, settings.get("gmail_query", DEFAULT_QUERY))
    except PermissionError as exc:
        return "auth_error", type(exc).__name__, [], False
    except LookupError:
        return "label_missing", f"Gmail label {label!r} was not found", [], False
    except Exception as exc:
        code = getattr(exc, "code", "")
        return "network_error", f"{type(exc).__name__} {code}".strip(), [], False
    processed = set(mail_state.get("processed_messages", []))
    jobs: list[dict] = []
    failed = 0
    unparsed_hosts: set[str] = set()
    unparsed = 0
    for message_id in ids:
        if message_id in processed:
            continue
        try:
            message = client.message(message_id)
        except Exception:
            failed += 1  # left unprocessed, so the next run retries it
            continue
        found, hosts = alert_jobs(message, settings.get("employers", []), now_iso())
        if found:
            jobs.extend(found)
        else:
            unparsed += 1
            unparsed_hosts.update(hosts)
        processed.add(message_id)
    # Only ids still inside the label query window can come back, so this stays small.
    mail_state["processed_messages"] = sorted(processed & set(ids))
    notes = []
    if failed:
        notes.append(f"{failed} alert emails could not be read; retried next run")
    if unparsed:
        shown = ", ".join(sorted(unparsed_hosts)[:8]) or "none"
        notes.append(f"{unparsed} alert emails had no recognised job links (link hosts: {shown})")
    status = "message_errors" if failed else ("ok" if jobs else "empty")
    return status, "; ".join(notes), jobs, True
