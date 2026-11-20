import base64

from pendel import fetch, mail_alerts
from pendel.mail_alerts import alert_jobs, clean_url, read_label
from pendel.watch import Runner

EMPLOYERS = [
    {"name": "DHL", "job_hosts": ["careers.dhl.com"], "redirect_hosts": [], "sender_domains": []},
    {"name": "Lidl", "job_hosts": ["jobs.lidl.de"], "redirect_hosts": [], "sender_domains": []},
    {"name": "Hermes", "job_hosts": [], "redirect_hosts": ["click.mail.example"], "sender_domains": ["hermes.example"]},
    {"name": "Acme", "job_hosts": ["jobs.example"], "redirect_hosts": [], "sender_domains": []},
]


def config():
    return {
        "remote_group_id": "remote",
        "language_policy": {"allowed_languages": ["en", "de"], "max_german_level": "B2"},
        "minimum_hours_by_tier": {"3": 25},
        "groups": {
            "area-a": {"cities": ["Exampleton"], "tiers": ["1", "2", "3"], "work_modes": ["onsite", "hybrid"]},
            "area-b": {"cities": ["Sampleburg"], "tiers": ["1", "2", "3"], "work_modes": ["onsite", "hybrid"]},
            "remote": {"cities": [], "tiers": ["1", "2"], "work_modes": ["remote"]},
        },
        "tiers": {"1": {"phrases": ["IT Support"]}, "3": {"phrases": ["Lagerhelfer"]}},
        "exclusions": {"senior": [r"\b(senior|lead)\b"]},
        "email_alerts": {"gmail_label": "Pendel/Jobs", "employers": EMPLOYERS},
    }


def mail(html="", text="", sender="DHL Jobs <jobalert@dhl.com>"):
    def part(mime, body):
        return {"mimeType": mime, "body": {"data": base64.urlsafe_b64encode(body.encode()).decode()}}
    parts = ([part("text/plain", text)] if text else []) + ([part("text/html", html)] if html else [])
    return {"payload": {"mimeType": "multipart/alternative",
                        "headers": [{"name": "From", "value": sender}], "parts": parts}}


def dhl_card(job_id, title="Lagerhelfer (m/w/d)", city="Exampleton"):
    return (f'<tr><td><a href="https://careers.dhl.com/job/{job_id}?utm_source=alert&amp;lang=de">{title}</a></td>'
            f"<td>{city}</td><td>Vollzeit</td></tr>")


def dhl_email(*cards):
    return mail(html="<table>" + "".join(cards) + "</table>"
                '<p><a href="https://careers.dhl.com/alerts/unsubscribe">Job-Alert abmelden</a>'
                '<a href="https://www.facebook.com/dhl">Facebook</a></p>')


class FakeGmail:
    def __init__(self, messages=None, error=None):
        self.messages = dict(messages or {})
        self.error = error
        self.reads = []

    def message_ids(self, label, query):
        if self.error:
            raise self.error
        return list(self.messages)

    def message(self, message_id):
        self.reads.append(message_id)
        return self.messages[message_id]


def test_html_card_becomes_one_job_with_its_city_and_no_tracking():
    jobs, hosts = alert_jobs(dhl_email(dhl_card("123")), EMPLOYERS, "2026-11-18T10:00:00Z")
    assert hosts == set()
    assert len(jobs) == 1
    job = jobs[0]
    assert job["company"] == "DHL"
    assert job["title"] == "Lagerhelfer (m/w/d)"
    assert job["url"] == "https://careers.dhl.com/job/123?lang=de"
    assert "Exampleton" in job["location"] and "Vollzeit" in job["description"]
    assert "job_id" not in job


def test_button_link_takes_the_title_from_its_card():
    html = ('<div class="job-card"><h3>Lagerhelfer</h3><p>Sampleburg</p>'
            '<a href="https://jobs.lidl.de/stelle/9">Job ansehen</a></div>')
    jobs, _ = alert_jobs(mail(html=html, sender="Lidl <jobagent@lidl.de>"), EMPLOYERS)
    assert [(j["title"], j["company"], j["location"]) for j in jobs] == [("Lagerhelfer", "Lidl", "Sampleburg")]


def test_plain_text_alert_uses_the_line_above_the_link():
    text = "Neue Jobs für Sie\n\nLagerhelfer (m/w/d) – Exampleton\nhttps://jobs.lidl.de/stelle/55\n\nAbmelden: https://jobs.lidl.de/unsubscribe\n"
    jobs, _ = alert_jobs(mail(text=text), EMPLOYERS)
    assert [(j["title"], j["url"]) for j in jobs] == [("Lagerhelfer (m/w/d) – Exampleton", "https://jobs.lidl.de/stelle/55")]
    assert "Exampleton" in jobs[0]["location"]


def test_skip_words_do_not_drop_real_titles():
    jobs, _ = alert_jobs(dhl_email(dhl_card("7", title="Production Helper")), EMPLOYERS)
    assert [j["title"] for j in jobs] == ["Production Helper"]


def test_redirect_links_get_the_same_identity_in_two_emails():
    def hermes(click):
        html = (f'<tr><td><a href="https://click.mail.example/c/{click}">Lagerhelfer</a></td>'
                "<td>Exampleton</td></tr>")
        return mail(html=html, sender="Hermes <noreply@hermes.example>")
    first, _ = alert_jobs(hermes("aaa"), EMPLOYERS)
    second, _ = alert_jobs(hermes("bbb"), EMPLOYERS)
    assert first[0]["company"] == "Hermes"
    assert first[0]["job_id"] == second[0]["job_id"]
    assert first[0]["job_id"].startswith("mail-")


def test_email_without_known_job_links_reports_its_hosts():
    html = '<a href="https://jobs.unknown.example/role/1">Lagerhelfer</a>'
    jobs, hosts = alert_jobs(mail(html=html), EMPLOYERS)
    assert jobs == [] and hosts == {"jobs.unknown.example"}
    state = {}
    status, error, found, complete = read_label(FakeGmail({"m1": mail(html=html)}), config()["email_alerts"],
                                                state, lambda: "2026-11-18T10:00:00Z")
    assert (status, found, complete) == ("empty", [], True)
    assert "jobs.unknown.example" in error
    assert state["processed_messages"] == ["m1"]


def test_clean_url_unwraps_redirect_parameters():
    wrapped = "https://track.example/r?url=https%3A%2F%2Fcareers.dhl.com%2Fjob%2F5%3Futm_medium%3Demail"
    assert clean_url(wrapped) == "https://careers.dhl.com/job/5"


def test_first_intake_is_silent_then_new_email_is_queued_once(tmp_path):
    gmail = FakeGmail({"m1": dhl_email(dhl_card("1"))})
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}
    runner = Runner(config(), state, mail=gmail)
    assert runner.prepare([], tmp_path / "first") == 0
    assert state["outbox"] == []
    assert state["sources"][mail_alerts.SOURCE_ID]["baseline_complete"] is True

    gmail.messages["m2"] = dhl_email(dhl_card("2", city="Sampleburg"))
    assert runner.prepare([], tmp_path / "second") == 0
    assert gmail.reads == ["m1", "m2"]  # m1 is not read again
    assert len(state["outbox"]) == 1
    [item] = state["outbox"][0]["payload"]["items"]
    assert item["company"] == "DHL"
    assert item["url"] == "https://careers.dhl.com/job/2?lang=de"

    assert runner.prepare([], tmp_path / "third") == 0
    assert len(state["outbox"]) == 1


def test_email_job_already_in_a_feed_only_adds_the_email_source(monkeypatch):
    feed_source = {"source_id": "greenhouse|acme", "ats": "greenhouse", "token": "acme",
                   "feed_url": "https://boards-api.greenhouse.io/v1/boards/acme/jobs", "companies": "Acme"}
    listing = {"job_id": "one", "title": "Lagerhelfer", "location": "Exampleton", "country": "DE",
               "work_mode": "onsite", "url": "https://jobs.example/one", "description": "Vollzeit"}
    monkeypatch.setattr(fetch, "fetch_jobs", lambda _source, _throttle: [dict(listing)])
    html = '<tr><td><a href="https://jobs.example/one?utm_source=alert">Lagerhelfer</a></td><td>Exampleton</td></tr>'
    runner = Runner(config(), {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []},
                    mail=FakeGmail({"m1": mail(html=html)}))
    current, statuses = runner.collect([feed_source])
    assert list(current) == ["id:greenhouse|acme|one"]
    assert current["id:greenhouse|acme|one"]["source_ids"] == sorted(["greenhouse|acme", mail_alerts.SOURCE_ID])
    assert [s["source_id"] for s in statuses] == ["greenhouse|acme", mail_alerts.SOURCE_ID]


def test_gmail_auth_failure_is_an_incomplete_source_with_a_health_notice(tmp_path):
    state = {"schema_version": 1, "jobs": {}, "sources": {}, "outbox": []}
    runner = Runner(config(), state, mail=FakeGmail(error=PermissionError("expired")))
    assert runner.prepare([], tmp_path / "run") == 0
    assert state["sources"][mail_alerts.SOURCE_ID]["baseline_complete"] is False
    assert state["sources"][mail_alerts.SOURCE_ID]["last_status"] == "auth_error"
    assert runner.prepare([], tmp_path / "run-two") == 0  # notices need two failed runs in a row
    assert "Email job alerts (email_alert): auth_error" in state["outbox"][0]["payload"]["chunks"][0]


def test_unreadable_message_is_retried_next_run():
    class Flaky(FakeGmail):
        def message(self, message_id):
            self.reads.append(message_id)
            if len(self.reads) == 1:
                raise TimeoutError()
            return self.messages[message_id]
    gmail, state = Flaky({"m1": dhl_email(dhl_card("1"))}), {}
    status, error, jobs, complete = read_label(gmail, config()["email_alerts"], state, lambda: "")
    assert (status, jobs, complete) == ("message_errors", [], True)
    assert "retried next run" in error and state["processed_messages"] == []
    status, _, jobs, _ = read_label(gmail, config()["email_alerts"], state, lambda: "")
    assert status == "ok" and len(jobs) == 1


def test_without_gmail_secrets_nothing_is_read():
    assert mail_alerts.configured({}) is False
    assert mail_alerts.configured({name: "x" for name in mail_alerts.SECRET_NAMES}) is True


def test_randstad_card_gives_a_clean_city_and_the_pay():
    randstad = EMPLOYERS + [{"name": "Randstad", "job_hosts": ["randstad.de"], "redirect_hosts": [], "sender_domains": []}]
    html = ('<tr><td><a href="https://www.randstad.de/jobs/kommissionierer-mwd_krefeld_c01344457">Kommissionierer (m/w/d)</a></td>'
            "<td>Randstad · 18/11/2026</td><td>Krefeld</td><td>€15.33 - €15.34 pro Stunde</td>"
            '<td><a href="https://www.randstad.de/jobs/kommissionierer-mwd_krefeld_c01344457">Jetzt ansehen</a></td></tr>')
    [job] = alert_jobs(mail(html=html, sender="Randstad <no-reply@randstad.de>"), randstad)[0]
    assert job["location"] == "Krefeld"
    assert job["salary"] == "€15.33 - €15.34 pro Stunde"
    assert job["company"] == "Randstad"


def test_place_keeps_hyphenated_city_names():
    assert mail_alerts.place_in("Neu-Ulm · Vollzeit", "DHL") == "Neu-Ulm"
    assert mail_alerts.salary_in("ab 14,50 €/h brutto") == "ab 14,50 €/h"


BA = {"name": "Jobbörse (BA)", "board": True, "job_hosts": ["arbeitsagentur.de"],
      "job_paths": ["/jobsuche/jobdetail/"], "card_fields": ["company", "location"],
      "redirect_hosts": [], "sender_domains": ["arbeitsagentur.de"]}


def test_job_board_card_gives_the_real_employer_and_city():
    html = ('<tr><td><a href="https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1234567890-S">'
            "Helfer Lager (m/w/d)</a></td><td>Muster Logistik GmbH · 18.11.2026</td><td>Exampleton</td>"
            '<td>Vollzeit</td></tr>'
            '<tr><td><a href="https://www.arbeitsagentur.de/jobsuche/suche?was=Helfer">Alle Stellen</a></td></tr>'
            '<tr><td><a href="https://www.arbeitsagentur.de/datenschutz">Datenschutz</a></td></tr>')
    [job], _ = alert_jobs(mail(html=html, sender="BA <noreply@arbeitsagentur.de>"), EMPLOYERS + [BA])
    assert (job["company"], job["location"]) == ("Muster Logistik GmbH", "Exampleton")
    assert job["url"] == "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1234567890-S"


def test_job_board_link_outside_job_paths_is_not_a_job():
    html = '<tr><td><a href="https://www.arbeitsagentur.de/jobsuche/suche?was=Lager">Lagerhelfer in Exampleton</a></td></tr>'
    jobs, hosts = alert_jobs(mail(html=html, sender="BA <noreply@arbeitsagentur.de>"), [BA])
    assert jobs == [] and hosts == {"www.arbeitsagentur.de"}


def test_job_board_card_without_lines_falls_back_to_the_board_name():
    text = "Lagerhelfer – Exampleton\nhttps://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1-S\n"
    [job], _ = alert_jobs(mail(text=text, sender="BA <noreply@arbeitsagentur.de>"), [BA])
    assert job["company"] == "Jobbörse (BA)" and job["title"] == "Lagerhelfer – Exampleton"


def test_employer_entries_keep_their_own_name():
    html = ('<tr><td><a href="https://careers.dhl.com/job/9">Lagerhelfer (m/w/d)</a></td>'
            "<td>Some Other GmbH</td><td>Exampleton</td></tr>")
    [job], _ = alert_jobs(mail(html=html), EMPLOYERS + [BA])
    assert job["company"] == "DHL"


BA_MARKED = BA | {"card_fields": ["company"], "field_markers": {"location": "📍"}}


def ba_card(n, ref, title, company, extra_lines, city, split_marker=False):
    rows = "".join(f"<tr><td>{x}</td></tr>" for x in extra_lines)
    place = f"<td>📍</td><td>{city}</td>" if split_marker else f"<td>📍 {city}</td>"
    url = f"https://www.arbeitsagentur.de/jobsuche/jobdetail/{ref}"
    return (f'<table><tr><td><h2><a href="{url}"><span>{n}.</span>\r\n      <span>{title}</span></a></h2></td></tr>'
            f"<tr><td>{company}</td></tr>{rows}<tr>{place}</tr><tr><td>⏱ Vollzeit</td></tr>"
            f'<tr><td><a href="{url}">Stelle ansehen➔</a></td></tr></table>')


def test_ba_card_city_comes_from_the_pin_line_not_from_optional_lines():
    html = ("<div>"
            + ba_card(2, "13280-YX3R0ZV1S0AZTA4K-S", "Lagerhelfer (m/w/d)", "TERO GmbH",
                      ["15,33 €/Std.", "Schicht/Nacht/Wochenende"], "Hilden")
            + ba_card(3, "12147-20261119228254-S", "Produktionsmitarbeiter (m/w/d)", "JobImpulse Süd GmbH Trier",
                      ["15,33 €/Std.", "ab 30.11.2026"], "Trier (3 km)", split_marker=True)
            + '<p><a href="https://www.arbeitsagentur.de/jobsuche/suche?was=Lagerhelfer">Alle aktuellen Stellen ansehen</a></p>'
            "</div>")
    jobs, _ = alert_jobs(mail(html=html, sender="BA <jobsuche@arbeitsagentur.de>"), [BA_MARKED])
    got = sorted((j["title"], j["company"], j["location"], j["salary"]) for j in jobs)
    assert got == [("Lagerhelfer (m/w/d)", "TERO GmbH", "Hilden", "15,33 €/Std."),
                   ("Produktionsmitarbeiter (m/w/d)", "JobImpulse Süd GmbH Trier", "Trier", "15,33 €/Std.")]
    assert all("Vollzeit" in j["description"] for j in jobs)


def test_arrow_button_is_never_a_title():
    assert mail_alerts.BUTTON_RX.match("Stelle ansehen➔")


STEPSTONE = {"name": "StepStone", "board": True, "job_hosts": [], "redirect_hosts": ["click.stepstone.de"],
             "sender_domains": ["stepstone.de"], "card_fields": ["company", "location"]}


def test_stepstone_tracking_links_give_the_job_with_employer_and_city():
    html = ('<table><tr><td><a href="https://click.stepstone.de/f/a/AAA">Magnera GmbH</a></td></tr>'
            '<tr><td><a href="https://click.stepstone.de/f/a/BBB"><b>IT On-Site-Support (m/w/d)</b></a></td></tr>'
            '<tr><td><img alt="company">Magnera GmbH</td></tr><tr><td><img alt="location">Falkenhagen, Prignitz</td></tr>'
            "<tr><td>Feste Anstellung</td></tr><tr><td>Vollzeit</td></tr>"
            "<tr><td>31.000 - 45.000 €/Jahr (geschätzt für Vollzeit)</td></tr></table>"
            '<p><a href="https://click.stepstone.de/f/a/CCC">Lebenslauf hochladen</a></p>'
            '<p><a href="https://click.stepstone.de/f/a/DDD">E-Mail-Einstellungen verwalten</a></p>')
    jobs, _ = alert_jobs(mail(html=html, sender="Stepstone <info@jobagent.stepstone.de>"), [STEPSTONE])
    by_title = {j["title"]: j for j in jobs}
    job = by_title["IT On-Site-Support (m/w/d)"]
    assert (job["company"], job["location"]) == ("Magnera GmbH", "Falkenhagen, Prignitz")
    assert job["job_id"].startswith("mail-") and "Vollzeit" in job["description"]
    assert not {"Lebenslauf hochladen", "E-Mail-Einstellungen verwalten"} & set(by_title)
