# QBK Youth KPI Dashboard

Standalone deployment for the QBK Youth KPI page.

This app is separate from the trial leads dashboard. It serves the Youth KPI dashboard at the root URL and keeps the supporting sync/API endpoints needed to populate it from Salesmessage and DaySmart.

## What it does

- Opens directly to the Youth KPI page
- Syncs Youth inbox conversation headers from Salesmessage
- Syncs customer, registration, and membership data from DaySmart
- Builds a youth funnel view for recent leads
- Shows KPI summary, status buckets, detail rows, and email preview

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload --port 8001
```

## Required environment variables

- `SALESMESSAGE_API_TOKEN`
- `SALESMESSAGE_BASE_URL`
- `YOUTH_INBOX_ID`
- `DASH_API_CLIENT_ID`
- `DASH_API_SECRET`
- `DASH_API_BASE_URL`
- `DAYSMART_COMPANY`
- `APP_PASSWORD`

## Recommended environment variables

- `DATABASE_URL`
  Default: `youth_kpi.db`
- `OPENAI_API_KEY`
- `OPENAI_MODEL`

## Render

This repo is designed to run as a simple Python web service on Render.

Recommended settings:

- Runtime: `python`
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

Important note:

- If you deploy with SQLite only, the synced data is not guaranteed to persist across restarts or redeploys.
- The app will still work, but you may need to click `Sync` again after a cold start.
- If you want durable data later, the clean next step is adding a persistent disk or moving to Postgres.
