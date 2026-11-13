# pendel plan (agreed 13 Nov 2026)

**Goal:** every morning, the new English tech roles at companies within commuting reach of Duisburg. Also a public portfolio project.

**Decisions:** alert first, page second · repo public from day one · alerts by Telegram · start with the 87 companies, grow after the coverage review.

**Roles:** owner decides, publishes, applies. Overseer writes code and run files. Agent runs and reports in `runs/RESULT-*.md`.

**Alongside all of this:** 2 applications a day.

## Stage 0: Repo setup
- P0 Skeleton, tests, CI, sample page (`runs/RUN-P1.md`). Done when tests pass locally and the repo is on GitHub.

## Stage 1: Data foundation (~1–2 days)
1. Detect each company's recruiting system (`pendel.detect`).
2. Add lat/lon per company.
3. Coverage review: how many have a readable public feed?
- **Decision point:** under ~30 readable → grow the company list before Stage 2.

## Stage 2: Pipeline (~3–4 days)
1. Readers for the 2–3 most common recruiting systems, then the rest.
2. Store jobs in DuckDB with `first_seen`; re-runs add no duplicates.
3. Tag each job: language, category, salary if listed.
- Done when one command on the Mac fills the database cleanly.

## Stage 3: Output (~2 days)
1. **Daily Telegram alert** with today's new matching roles. ← first
2. Export `web/data/jobs.json`; the page shows real data.
- Done when the owner uses it every morning.

## Stage 4: Automate and publish (~1 day)
1. Daily GitHub Actions run (Telegram token as a repo secret); page on GitHub Pages.
2. README with screenshot; LinkedIn post.

## Later
Real train times instead of km. More regions.

## Data contract (the only thing the page depends on)

```json
{
  "generated_at": "2026-11-13T06:00:00Z",
  "companies": [{"id", "name", "city", "lat", "lon", "remote", "categories": [], "ats", "feed_open"}],
  "jobs": [{"company_id", "title", "lang", "salary", "first_seen", "url"}]
}
```
