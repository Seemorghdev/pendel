# Pendel implementation notes

## Public package

The public repository holds reusable feed readers, generic state handling, matching logic, tests, and a sample static page. Personal employer lists, job-search locations, role preferences, and notification state belong in a separate private runtime.

## Supported intake

Use confirmed public feeds from Greenhouse, Lever, Ashby, Recruitee, SmartRecruiters, Personio, Softgarden, and Pinpoint. Keep one source identity per provider and tenant, poll all content-affecting variants, and ignore fetch-only query options when deriving identity. Identify jobs by provider, tenant, and stable provider listing ID first; fall back to the canonical public listing URL.

The collector reports each source separately. Successful empty results remain distinct from parse errors. Pagination caps are partial results. Failed or partial feeds neither complete a new source's silent baseline nor mark old listings closed. A 429 blocks further requests to that provider host for the run.

## Matching and reports

The private runtime supplies the JSON configuration at run time. A job is classified from its actual listing location, work mode, title, and published description. The language requirement is kept separate from the detected language of the advert. Unknown location, language, work mode, or configured minimum hours is sent to review with a reason; explicit mismatches are excluded with their evidence. Per-tier language and hours limits live only in private configuration.

Each run produces separate match, review, exclusion, and feed-health reports. City-level grouping is a first pass; verify the route to the exact worksite before treating a listing as reachable without a car.

## Durable state and alerts

The private scheduled runner stores compact logical exports on a separate private state branch and restores them into DuckDB on each run. The first successful complete poll of each source is silent. Subsequent first-seen eligible listings enter a durable outbox. A claimed notification is persisted before sending. An interrupted send is marked ambiguous and is not retried automatically.

Telegram delivery requires configured repository secrets and explicit approval. The workflow remains manual-only until its code, test results, dry-run, and recipient are reviewed. Never print secrets or send a test alert to an unconfirmed destination.

## Limits

- Public job-feed data is a snapshot and may be stale or incomplete.
- An endpoint's successful response is not proof that a posting is still open.
- Keyword matching is triage, not an application decision.
- Listings with unclear requirements stay visible for review.
