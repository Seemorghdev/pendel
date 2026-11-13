# pendel

Find English-language tech jobs at companies you can actually commute to.

Pick a home point and a radius. pendel shows which companies inside it have open roles today, which are new, and which are in English. It is built for a cross-border commute: living in the Rhine-Ruhr area, working in the Netherlands or Düsseldorf.

> Status: early. The page runs on sample data; the daily pipeline is being built.

## How it works

```
companies.csv ──► detect ──► fetch ──► classify ──► export ──► web/
 (who, where)     (which      (public    (language,    (jobs.json)  (static page:
                  recruiting   feeds,     category,                  radius, map,
                  system)      DuckDB)    first seen)                filters)
```

1. **detect**: open each company's careers page once and recognise its recruiting system (Greenhouse, Lever, Ashby, Recruitee, Personio, SmartRecruiters, Workable). Closed systems such as Workday are recorded, not read.
2. **fetch**: once a day, read each company's public job feed and store every posting in DuckDB with the date it was first seen.
3. **classify**: language of the posting, role category from the title, salary when listed.
4. **export**: write `web/data/jobs.json`. The page reads only that file.

## Rules it follows

- **Only public recruiting-system feeds and company career pages.** No LinkedIn, Indeed, StepStone, Glassdoor or Bundesagentur Jobbörse: their terms forbid automated access.
- One request per company per run, at least 2 seconds apart, an honest User-Agent, `robots.txt` respected, and it stops on rate limits.

## Run it

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
python -m pendel.detect companies/companies.csv companies/companies-ats.csv --limit 3
```

Open `web/index.html` through a local server (`python -m http.server -d web`) to see the page.

## Stack

Python, DuckDB, plain HTML/JS, GitHub Actions.

## Licence

MIT
