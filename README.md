# ISBE Filing Notifier

Watches the Illinois State Board of Elections ["Latest Reports Filed"](https://elections.il.gov/rss/LatestReportsFiled.aspx)
feed and notifies subscribers by email and web push when committees they follow — or the
2026 CPS Board races they care about — file campaign-finance reports.

See [PROJECT_PLAN.md](PROJECT_PLAN.md) for the full design.

## Local development

```bash
uv sync                      # install deps into .venv
docker compose up -d db      # local Postgres on :5432
uv run alembic upgrade head  # apply migrations
uv run python -m isbe_notifier.seeds   # seed the 21 CPS races

uv run pytest                # tests (parsers run against captured ISBE HTML fixtures)
uv run ruff check .

uv run python -m isbe_notifier.poller  # run the engine (console email backend by default)
uv run uvicorn isbe_notifier.web.app:app --reload   # run the web app on :8000
```

Copy `.env.example` to `.env` to override settings. With `EMAIL_BACKEND=console`
(the default) emails are logged, not sent — no AWS account needed for development.

## Architecture

- `src/isbe_notifier/scraper/` — RSS feed parser + A1List/B1List/CommitteeDetail page parsers
- `src/isbe_notifier/poller.py` — the engine: poll → dedupe → scrape → resolve committee →
  match → notify. First run bootstraps the existing feed backlog without scraping it.
- `src/isbe_notifier/matching.py` — committee follows + B-1 "Office – District" race matching
- `src/isbe_notifier/notify/` — content builder, SES/console email, web push, signed tokens
- `src/isbe_notifier/web/` — FastAPI signup PWA, verify/manage/unsubscribe, admin
- `migrations/` — Alembic; `tests/fixtures/` — captured ISBE HTML/XML used by tests

## Operational notes

- ISBE serves HTTP 403 to non-browser User-Agents; the scraper client sends a browser UA.
- The feed holds the last 1,000 items and is NOT strictly ordered by the guid sequence
  number — dedupe checks each item against the `feed_items` table.
- Paper filings have no `<link>`; they're notified as type + PDF link only.
- Filings with more rows than one list page captures the first page and logs a warning
  (rare; revisit if it appears in logs).
