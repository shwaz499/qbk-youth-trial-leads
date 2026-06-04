from __future__ import annotations

import json
import os
import secrets
import time
import base64
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import requests

DEFAULT_SCOPES = (
    "contacts:read",
    "contacts:write",
    "conversations:read",
    "conversations:write",
    "messages:read",
    "messages:write",
    "teams:read",
    "users:read",
    "tags:read",
    "tags:write",
    "custom-fields:read",
    "custom-fields:write",
    "attachments:read",
    "attachments:write",
    "numbers:read",
)


class SalesmessageOAuthError(RuntimeError):
    pass


def _expand_path(value: str | None) -> Path:
    raw = (value or "~/.qbk/salesmessage_oauth.json").strip()
    return Path(raw).expanduser()


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True))
    path.chmod(0o600)


def _first_value(*values: str | None) -> str:
    for value in values:
        cleaned = (value or "").strip()
        if cleaned:
            return cleaned
    return ""


def _scopes_from_env(value: str | None) -> list[str]:
    raw = (value or "").replace(",", " ")
    scopes = [scope.strip() for scope in raw.split() if scope.strip()]
    return scopes or list(DEFAULT_SCOPES)


def _jwt_expires_at(token: str) -> float:
    parts = token.split(".")
    if len(parts) < 2:
        return 0
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + ("=" * (-len(parts[1]) % 4))))
    except Exception:
        return 0
    try:
        return float(payload.get("exp") or 0)
    except (TypeError, ValueError):
        return 0


class SalesmessageOAuth:
    def __init__(self, *, mcp_env: dict[str, str], base_url: str) -> None:
        self.mcp_env = mcp_env
        self.base_url = base_url.rstrip("/")
        self.client_id = _first_value(
            os.getenv("SALESMESSAGE_OAUTH_CLIENT_ID"),
            os.getenv("SALESMESSAGE_CLIENT_ID"),
            mcp_env.get("SALESMESSAGE_OAUTH_CLIENT_ID"),
            mcp_env.get("SALESMESSAGE_CLIENT_ID"),
        )
        self.client_secret = _first_value(
            os.getenv("SALESMESSAGE_OAUTH_CLIENT_SECRET"),
            os.getenv("SALESMESSAGE_CLIENT_SECRET"),
            mcp_env.get("SALESMESSAGE_OAUTH_CLIENT_SECRET"),
            mcp_env.get("SALESMESSAGE_CLIENT_SECRET"),
        )
        self.redirect_uri = _first_value(
            os.getenv("SALESMESSAGE_OAUTH_REDIRECT_URI"),
            mcp_env.get("SALESMESSAGE_OAUTH_REDIRECT_URI"),
            "http://localhost:8001/oauth/salesmessage/callback",
        )
        self.authorize_url = _first_value(
            os.getenv("SALESMESSAGE_OAUTH_AUTHORIZE_URL"),
            mcp_env.get("SALESMESSAGE_OAUTH_AUTHORIZE_URL"),
            "https://app.salesmessage.com/oauth/authorize",
        )
        self.token_url = _first_value(
            os.getenv("SALESMESSAGE_OAUTH_TOKEN_URL"),
            mcp_env.get("SALESMESSAGE_OAUTH_TOKEN_URL"),
            f"{self.base_url}/oauth/token",
        )
        self.refresh_url = _first_value(
            os.getenv("SALESMESSAGE_OAUTH_REFRESH_URL"),
            mcp_env.get("SALESMESSAGE_OAUTH_REFRESH_URL"),
            f"{self.base_url}/oauth/token/refresh",
        )
        self.token_file = _expand_path(
            _first_value(
                os.getenv("SALESMESSAGE_OAUTH_TOKEN_FILE"),
                mcp_env.get("SALESMESSAGE_OAUTH_TOKEN_FILE"),
            )
        )
        self.state_file = self.token_file.with_suffix(".state.json")
        self.scopes = _scopes_from_env(
            _first_value(os.getenv("SALESMESSAGE_OAUTH_SCOPES"), mcp_env.get("SALESMESSAGE_OAUTH_SCOPES"))
        )

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def authorization_url(self) -> str:
        if not self.configured:
            raise SalesmessageOAuthError("Salesmsg OAuth client ID/secret are not configured.")
        state = secrets.token_urlsafe(32)
        _write_json(self.state_file, {"state": state, "created_at": int(time.time())})
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
        }
        return f"{self.authorize_url}?{urlencode(params)}"

    def exchange_code(self, *, code: str, state: str | None) -> dict[str, Any]:
        expected = str(_load_json(self.state_file).get("state") or "")
        if expected and state != expected:
            raise SalesmessageOAuthError("Salesmsg OAuth state did not match. Please start the connection again.")
        payload = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "redirect_uri": self.redirect_uri,
            "code": code,
        }
        data = self._post_token(self.token_url, payload)
        return self._save_token_response(data)

    def cached_access_token(self, *, fallback_token: str = "") -> str:
        if not self.configured:
            return fallback_token
        env_access_token = _first_value(
            os.getenv("SALESMESSAGE_OAUTH_ACCESS_TOKEN"),
            self.mcp_env.get("SALESMESSAGE_OAUTH_ACCESS_TOKEN"),
        )
        if env_access_token:
            env_expires_at = _first_value(
                os.getenv("SALESMESSAGE_OAUTH_ACCESS_TOKEN_EXPIRES_AT"),
                self.mcp_env.get("SALESMESSAGE_OAUTH_ACCESS_TOKEN_EXPIRES_AT"),
            )
            try:
                expires_at = float(env_expires_at)
            except (TypeError, ValueError):
                expires_at = _jwt_expires_at(env_access_token)
            if not expires_at or expires_at > time.time() + 300:
                return env_access_token
        saved = _load_json(self.token_file)
        access_token = str(saved.get("access_token") or "")
        expires_at = float(saved.get("expires_at") or 0)
        if access_token and expires_at > time.time() + 300:
            return access_token
        return fallback_token

    def access_token(self, *, fallback_token: str = "") -> str:
        if not self.configured:
            return fallback_token
        cached_token = self.cached_access_token()
        if cached_token:
            return cached_token
        saved = _load_json(self.token_file)
        access_token = str(saved.get("access_token") or "")
        refresh_token = _first_value(
            str(saved.get("refresh_token") or ""),
            os.getenv("SALESMESSAGE_OAUTH_REFRESH_TOKEN"),
            self.mcp_env.get("SALESMESSAGE_OAUTH_REFRESH_TOKEN"),
        )
        if not refresh_token:
            return access_token or fallback_token
        payload = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": refresh_token,
        }
        data = self._post_token(self.refresh_url, payload)
        token_payload = self._save_token_response(data)
        return str(token_payload.get("access_token") or fallback_token)

    def _post_token(self, url: str, payload: dict[str, str]) -> dict[str, Any]:
        try:
            response = requests.post(url, json=payload, timeout=30)
            if response.status_code == 404:
                response = requests.post(url, data=payload, timeout=30)
        except requests.RequestException as exc:
            raise SalesmessageOAuthError(f"Salesmsg OAuth request failed: {exc}") from exc
        if response.status_code >= 400:
            raise SalesmessageOAuthError(
                f"Salesmsg OAuth request failed with {response.status_code}: {response.text[:500]}"
            )
        data = response.json()
        if not isinstance(data, dict) or not data.get("access_token"):
            raise SalesmessageOAuthError("Salesmsg OAuth response did not include an access token.")
        return data

    def _save_token_response(self, data: dict[str, Any]) -> dict[str, Any]:
        saved = _load_json(self.token_file)
        expires_in = int(data.get("expires_in") or 3600)
        payload = {
            **saved,
            **data,
            "expires_at": int(time.time()) + max(0, expires_in - 60),
            "updated_at": int(time.time()),
        }
        if saved.get("refresh_token") and not payload.get("refresh_token"):
            payload["refresh_token"] = saved["refresh_token"]
        _write_json(self.token_file, payload)
        return payload
