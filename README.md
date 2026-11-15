# Pendel

Pendel is a small Python toolkit for reading public applicant-tracking-system feeds, normalizing job listings, and applying caller-supplied matching rules. A private runtime can reuse the library while keeping employer lists, location groups, search preferences, and notification state outside this public repository.

## Pipeline

```text
private source manifest -> public-feed readers -> first-seen state -> private rules -> match/review reports
```

- **Public readers:** Greenhouse, Lever, Ashby, Recruitee, SmartRecruiters, Personio, Softgarden, Pinpoint, Teamtailor RSS, and the Trakstar Hire Frontend API.
- **Matching:** rules are loaded from a JSON file supplied by the caller. This package contains no personal location or role-target configuration.
- **State:** the watcher imports a compact logical state export into DuckDB, tracks first-seen provider/tenant/listing IDs and feed health, and writes a new state export. A failed or partial feed cannot complete a baseline or mark jobs closed.
- **Notifications:** the outbox is durable and delivery is opt-in. The Telegram adapter requires both an explicit command flag and an approval environment setting.

## Guardrails

- Use only public ATS job feeds and documented public endpoints.
- Do not automate LinkedIn, other job boards, or Bundesagentur job-search pages.
- Do not query private ATS endpoints or internal APIs.
- Throttle requests by host, stop querying a rate-limited host for the run, and retain failures in the source report.
- Keep personal source lists, target configuration, state, run reports, and secrets in a private runtime.

## Local development

```sh
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

The private watcher supplies its own source manifest, target configuration, state export, database path, and report directory. The public package does not contain user-specific defaults.

## License

MIT
