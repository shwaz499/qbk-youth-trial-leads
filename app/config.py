from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import tomllib

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    salesmessage_api_token: str
    salesmessage_base_url: str
    youth_inbox_id: int
    daysmart_api_client_id: str
    daysmart_api_secret: str
    daysmart_base_url: str
    daysmart_company: str
    database_url: str
    openai_api_key: str | None
    openai_model: str


def _load_codex_mcp_env(server_name: str) -> dict[str, str]:
    config_path = Path.home() / ".codex" / "config.toml"
    if not config_path.exists():
        return {}
    try:
        data = tomllib.loads(config_path.read_text())
    except Exception:
        return {}
    servers = data.get("mcp_servers")
    if not isinstance(servers, dict):
        return {}
    server = servers.get(server_name)
    if not isinstance(server, dict):
        return {}
    env = server.get("env")
    if not isinstance(env, dict):
        return {}
    return {str(k): str(v) for k, v in env.items()}



def get_settings() -> Settings:
    token = os.getenv("SALESMESSAGE_API_TOKEN", "").strip()
    qbk_env = _load_codex_mcp_env("qbk-sports-admin")
    return Settings(
        salesmessage_api_token=token,
        salesmessage_base_url=os.getenv(
            "SALESMESSAGE_BASE_URL", "https://api.dev.salesmessage.com/qa/pub/v2.2"
        ).rstrip("/"),
        youth_inbox_id=int(os.getenv("YOUTH_INBOX_ID", "207883").strip() or "207883"),
        daysmart_api_client_id=os.getenv("DASH_API_CLIENT_ID", "").strip()
        or qbk_env.get("DASH_API_CLIENT_ID", "").strip(),
        daysmart_api_secret=os.getenv("DASH_API_SECRET", "").strip()
        or qbk_env.get("DASH_API_SECRET", "").strip(),
        daysmart_base_url=os.getenv("DASH_API_BASE_URL", "https://api.dashplatform.com").rstrip("/"),
        daysmart_company=os.getenv("DAYSMART_COMPANY", "qbksports").strip() or "qbksports",
        database_url=os.getenv("DATABASE_URL", "salesmessage_agent.db"),
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip() or None,
        openai_model=os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip(),
    )
