# Deploying to Railway

One Railway project, three services from this one repo. The default
`<service>.up.railway.app` domain works for the MVP; swap in
`cps.illinoisanswers.org` later with a single CNAME.

## 1. Postgres
Dashboard → New → Database → PostgreSQL. Done.

## 2. Web service
- New → GitHub Repo → this repo (Railway builds the Dockerfile automatically).
- Settings → Deploy → Custom Start Command:
  `sh -c "alembic upgrade head && python -m isbe_notifier.seeds && uvicorn isbe_notifier.web.app:app --host 0.0.0.0 --port $PORT"`
- Settings → Networking → Generate Domain.
- Variables (see `.env.example` for the full list):
  - `DATABASE_URL` → reference Postgres: `postgresql+psycopg://${{Postgres.PGUSER}}:${{Postgres.PGPASSWORD}}@${{Postgres.PGHOST}}:${{Postgres.PGPORT}}/${{Postgres.PGDATABASE}}`
  - `BASE_URL` → the generated domain (https://…)
  - `SECRET_KEY` → long random string (`openssl rand -hex 32`)
  - `VAPID_PRIVATE_KEY` / `VAPID_PUBLIC_KEY` → `uv run python -m isbe_notifier.notify.push genkeys`
  - `VAPID_CLAIMS_EMAIL` → `mailto:<your address>`
  - `ADMIN_TOKEN` → long random string; admin page is `/admin?token=…`
  - Email (phase: SES production access granted): `EMAIL_BACKEND=ses`,
    `EMAIL_FROM`, `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`.
    Until then leave `EMAIL_BACKEND=console` (emails appear in logs).

## 3. Poller service
- New → GitHub Repo → same repo.
- Custom Start Command: `python -m isbe_notifier.poller`
- Same `DATABASE_URL`, `SECRET_KEY`, `BASE_URL`, email and VAPID variables
  (the poller sends the notifications).
- No public domain needed.

## 4. Guardrails (do this first)
- Workspace → Usage → set a **hard usage limit ($20/mo)** and an alert ($10).
- Postgres → Backups: confirm daily backups are on.

## 5. Load the CPS whitelist
Create `whitelist.csv` (`race_slug,committee_id` — slugs are `president`,
`d1a` … `d10b`), then run it against the production DB:
`railway run python -m isbe_notifier.load_whitelist whitelist.csv`
(or temporarily set DATABASE_URL locally to the Railway external URL).

## Amazon SES setup (email)
1. AWS Console → SES → verify the sending domain (e.g. `mail.illinoisanswers.org`):
   add the 3 DKIM CNAMEs + SPF/MX records SES shows you.
2. Create an IAM user with `ses:SendRawEmail` only; put its key pair in the
   Railway variables.
3. While in SES sandbox you can only email verified addresses — fine for testing.
   Request production access (Sending statistics → Request production access);
   describe the double-opt-in flow and List-Unsubscribe headers in the request.
4. Flip `EMAIL_BACKEND=ses`.

## DNS cutover (when ready)
1. Add CNAME `cps.illinoisanswers.org` → the Railway web service domain.
2. Railway → web service → Settings → Networking → Custom Domain.
3. Update `BASE_URL`. (Old verify/manage links keep working — tokens don't
   embed the domain.)
