# Salesmessage AI Agent (Starter)

This service now ingests **Salesmessage + DaySmart** into a unified youth-program schema, then computes retention/operations risk alerts.

## Features

- Sync conversations from Salesmessage API (`/sync`)
- Sync customers + check-ins from DaySmart (`/sync/daysmart`)
- Canonical youth schema: families, children, attendance, outreach, trial leads, risk alerts
- Recompute/list risk alerts (`/risk/recompute`, `/risk/alerts`)
- Store normalized data in SQLite
- Full-text search over message bodies (`/search`)
- Q&A endpoint with citations (`/ask`), powered by OpenAI Responses API

## Setup

1. Create a virtual environment and install dependencies:

```bash
cd salesmessage_agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. Create `.env` from `.env.example` and set values:

```bash
cp .env.example .env
```

Required values:

- `SALESMESSAGE_API_TOKEN`: your Salesmessage bearer token
- `SALESMESSAGE_BASE_URL`: Salesmessage API base URL (`https://api.salesmessage.com/pub/v2.2`)
- `DASH_API_CLIENT_ID`: DaySmart client ID
- `DASH_API_SECRET`: DaySmart client secret
- `DASH_API_BASE_URL`: DaySmart API base URL (default `https://api.dashplatform.com`)
- `OPENAI_API_KEY`: needed for `/ask` AI answers

3. Run API server:

```bash
uvicorn app.main:app --reload --port 8000
```

## Deploy

### GitHub

This directory is intended to be published as its own standalone repo, not as part of the larger parent workspace.

Suggested repo name:

- `qbk-youth-dashboard`

### Render

Create a Render web service with:

- Runtime: `python`
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`

Required environment variables:

- `SALESMESSAGE_API_TOKEN`
- `SALESMESSAGE_BASE_URL`
- `YOUTH_INBOX_ID`
- `DASH_API_CLIENT_ID`
- `DASH_API_SECRET`
- `DASH_API_BASE_URL`
- `DAYSMART_COMPANY`

Optional:

- `OPENAI_API_KEY`
- `OPENAI_MODEL`

## API

- `GET /health`
- `POST /sync`
- `POST /sync/daysmart`
- `GET /conversations`
- `GET /conversations/{conversation_id}/messages`
- `GET /search?query=your+query`
- `POST /ask`
- `POST /risk/recompute`
- `GET /risk/alerts`

### Example sync request

```bash
curl -X POST http://localhost:8000/sync \
  -H "Content-Type: application/json" \
  -d '{"filters":["open","closed","unread","assigned","unassigned"]}'
```

### Example ask request

```bash
curl -X POST http://localhost:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question":"What are the top objections in the last messages?","max_context_messages":40}'
```

### Example DaySmart sync request

```bash
curl -X POST http://localhost:8000/sync/daysmart \
  -H "Content-Type: application/json" \
  -d '{"max_pages":10,"page_size":200}'
```

### Example risk recompute request

```bash
curl -X POST http://localhost:8000/risk/recompute \
  -H "Content-Type: application/json" \
  -d '{"inactivity_days":14,"outreach_days":30}'
```

## Notes

- This starter is intentionally minimal and local-first.
- For production: move to Postgres, add row-level permissions, add audit logs, and schedule incremental sync.

## MCP server mode

You can run this project as an MCP server (stdio transport) and expose tools to any MCP client.

1. Install deps:

```bash
cd salesmessage_agent
source .venv/bin/activate
pip install -r requirements.txt
```

2. Make sure `.env` is configured (`SALESMESSAGE_API_TOKEN` required).

3. Start MCP server:

```bash
python -m app.mcp_server
```

Exposed MCP tools:
- `sync_salesmessage`
- `sync_daysmart`
- `list_conversations`
- `get_conversation_messages`
- `search_synced_messages`
- `ask_salesmessage`
- `recompute_risk`
- `list_risk_alerts`
