# ISBE Filing Notifier — Project Plan

A service that watches the Illinois State Board of Elections "Latest Reports Filed" RSS feed,
scrapes filing details, and notifies subscribers by email and/or web push — with first-class
support for the 2026 Chicago Public Schools Board races (President + districts 1a–10b) and
the ability to follow any ISBE committee.

**Decisions made:** Hosting on Railway (hard usage cap ~$20/mo) · Email via Amazon SES ·
Python/FastAPI · Scope: CPS races + arbitrary committee follows.

---

## 1. Architecture

One GitHub repo → one Railway project with three services:

```
┌─────────────────────────── Railway project ───────────────────────────┐
│                                                                        │
│  ┌────────────┐      ┌──────────────┐      ┌────────────────────────┐  │
│  │  poller    │─────▶│   Postgres   │◀─────│  web (FastAPI)         │  │
│  │  (worker)  │      └──────────────┘      │  signup PWA + API      │  │
│  └─────┬──────┘                            └───────────┬────────────┘  │
│        │ scrape                                        │               │
└────────┼────────────────────────────────────────────── ┼ ──────────────┘
         ▼                                               ▼
  elections.il.gov                          cps.illinoisanswers.org (CNAME)
         │                                  Amazon SES (email out)
         └── RSS feed + A1List/B1List/CommitteeDetail pages
```

- **poller** — long-running Python worker. Every ~90 seconds: fetch the RSS feed
  (browser User-Agent required — ISBE 403s non-browser clients), deduplicate using the
  incrementing sequence number embedded in each item's `guid`, and for each new item:
  classify the report type, fetch + parse the linked page, resolve the committee,
  match against subscriptions, and enqueue notifications.
- **web** — FastAPI app serving the signup page/PWA, subscription API, verification +
  unsubscribe endpoints, and a minimal admin view. Sends queued notifications is shared
  library code; the poller calls it directly after matching (no separate queue service —
  Postgres `notifications` table is the queue/audit log).
- **Postgres** — single source of truth. Railway-managed, automatic backups.

Both services build from the same repo/Dockerfile (different start commands), share a
`core/` package (models, parsers, matching, notification senders).

## 2. Data flow (the engine)

1. `GET /rss/LatestReportsFiled.aspx` (feed holds last 1,000 items, TTL 5 min; we poll at
   90s for near-real-time without abusing the source; conditional GET if supported,
   exponential backoff on errors).
2. New item → store in `feed_items` (guid sequence = natural dedupe key; survives restarts).
3. Classify from description: `A-1`, `B-1`, `D-1`, `D-2` (+ amendments, final reports,
   letters/correspondence). Paper filings have no `<link>` — store with PDF-viewer URL,
   notify with type + link only.
4. Electronically filed A-1/B-1 → fetch list page **for every filing statewide** (not just
   subscriber-matched ones — this builds the complete donation/expenditure history that the
   future weekly-digest feature aggregates), parse the single HTML table:
   - **A-1:** Contributed By, Address, Amount, Date Received, Description (+ vendor cols).
   - **B-1:** Vendor + address, Amount, Date, Purpose, Supporting/Opposing, Candidate Name,
     **Office – District**.
5. Resolve committee: list pages link to `CommitteeDetail.aspx` with an encrypted ID; fetch
   it once, read the plain ISBE committee ID off the page, cache the mapping in `committees`.
6. Match subscriptions:
   - **Committee follows** (incl. the CPS whitelist): plain committee ID match.
   - **CPS race match for B-1s:** any B-1 row whose Office–District matches a race's
     configured patterns (e.g. `Chicago School Board, District 7`; 2026 sub-district label
     format unknown until ISBE publishes one — patterns live in the DB, editable without
     deploys) notifies that race's subscribers *regardless of which committee filed it*.
7. Write one row per (subscriber, filing) to `notifications`, then send: SES for email,
   `pywebpush` (VAPID) for push. Retries with backoff; status recorded.

### Notification content by report type
| Type | Content |
|---|---|
| A-1 | Committee, contributor name(s), date(s) received, amount(s), total, link |
| B-1 | Committee, vendor, amount(s), date(s), purpose, supporting/opposing, candidate, office–district, link |
| D-1 / amendment | Committee + "filed a Statement of Organization", link |
| D-2 (all variants) | Committee + "filed a Quarterly/Final Report", link |
| Paper / letters | Committee + type + PDF link |

## 3. Data model (Postgres)

- `feed_items` — guid_seq (PK), committee_name, report_type, url, pub_date, source,
  processed_at, error
- `committees` — isbe_id (PK), name, encrypted_id, detail cached fields, last_seen
- `filings` — id, feed_item, committee, type, **is_amendment flag, amends_filing_id**
  (nullable self-reference — lets future aggregates exclude superseded originals so
  amended donations are never double-counted)
- `filing_lines` — filing, kind (contribution|expenditure), name, address, amount
  (**NUMERIC**), date (**DATE** — typed for SQL aggregation), purpose,
  supporting_opposing, candidate_name, office_district
- `races` — slug (president, d1a … d10b), label, office_district_patterns[]
- `race_committees` — race ↔ committee whitelist (user-provided, admin-editable)
- `subscribers` — id, email (unique, citext), email_verified_at, verify/unsubscribe token
  hashes, created_at
- `subscriptions` — subscriber ↔ (race | committee), wants_email, wants_push
- `push_subscriptions` — subscriber, endpoint (unique), p256dh, auth, user_agent, created_at
- `notifications` — subscriber, filing, channel, status, sent_at (audit + idempotency:
  unique on subscriber+filing+channel so restarts never double-send)

## 4. Web app / PWA

- Single signup page: email field + checkbox grid (Board President, districts 1a–10b) +
  committee search ("follow any Illinois committee") + channel choice (email / push / both).
- Committee search backed by a local `committees` table seeded from ISBE's downloadable
  committee list, refreshed nightly (fallback: on-demand lookup by committee ID).
- **No passwords.** Email signup = double opt-in (SES verification link). Manage/unsubscribe
  via signed tokenized links (also `List-Unsubscribe` header on every email).
- PWA: `manifest.json` + service worker; push opt-in flow requests permission only on user
  action; works installed-to-homescreen on Android/desktop; iOS Safari supports web push for
  installed PWAs (16.4+) — we'll surface "Add to Home Screen" guidance for iOS users.

## 5. Security & data hygiene

- PII stored: email address only. No passwords, no names.
- All tokens stored hashed; signed URLs (itsdangerous) with expiry for verify/manage links.
- Rate limiting on signup/verify endpoints; Pydantic validation everywhere; HTTPS only;
  security headers (CSP etc.) via middleware.
- Secrets (SES keys, VAPID keys, DB URL) in Railway environment variables — never in git.
- Scraper etiquette: 90s cadence, single concurrent fetch, backoff + alert on repeated
  failures, identifiable but browser-like UA.
- Ops guardrails: Railway usage cap $20/mo + alert at $10; healthcheck endpoint; poller
  heartbeat row — if stale >10 min, web app emails the admin (you).
- Postgres: Railway automated backups + weekly `pg_dump` to repo-external storage optional.

## 6. Build phases

**Phase 0 — Scaffold (½ day):** repo layout, Dockerfile, docker-compose for local dev
(Postgres + app), Alembic migrations, CI (GitHub Actions: lint+test), Railway project,
domain CNAME.

**Phase 1 — Engine (2–3 days):** feed poller, parsers for A-1/B-1/A1List/B1List/
CommitteeDetail (built against captured HTML fixtures with unit tests), committee
resolution + caching, race matching, full pipeline writing to Postgres. Runs headless;
verify against live feed.

**Phase 2 — Email + signup (2 days):** FastAPI signup page, double opt-in via SES
(sandbox first, then production-access request), notification templates, unsubscribe/manage
links, notification sender with idempotency.

**Phase 3 — PWA + push (1–2 days):** manifest, service worker, VAPID keys, push subscribe/
unsubscribe flow, push notification payloads + click-through to filing link.

**Phase 4 — Committee follows + polish (1–2 days):** committee list sync + search UI,
arbitrary-committee subscriptions, minimal admin page (recent filings, subscriber counts,
poller health), deploy hardening.

## 6a. Future feature (not in MVP): weekly digest email

A weekly summary of all donations, aggregated from `filing_lines`. The MVP accommodates it
by design — no work needed now beyond the choices above:
- Complete data: every A-1/B-1 statewide is scraped and stored (flow step 4).
- Aggregation-ready schema: typed amount/date columns; amendment linkage prevents
  double-counting (data model).
- Job slot: runs as a scheduled job in the poller worker, same pattern as the nightly
  committee sync.
- Later additions when built: `wants_weekly_digest` column on `subscriptions` (one
  migration), a digest email template, and a `digest` channel value in `notifications`.

## 7. What I need from you

1. **CPS committee whitelist** — committee IDs mapped to races (President, 1a–10b).
2. **AWS account** for SES + ability to add DNS records (DKIM/SPF) for the sending domain
   (suggest `alerts@mail.illinoisanswers.org` or similar) — and submit the SES
   production-access request (I'll draft the justification text).
3. **Railway account** (GitHub login) + set the usage cap.
4. **DNS CNAME** for the chosen subdomain (e.g. `cps.illinoisanswers.org`) → Railway.
5. **GitHub repo** location (org or personal).

## 8. Cost recap

Railway ~$5–10/mo (capped at $20) · SES ≈ $0.10 per 1,000 emails · push free ·
domain already owned → **expected total ≈ $6–11/mo.**
