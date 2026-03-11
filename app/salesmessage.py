from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


class SalesmessageApiError(RuntimeError):
    pass


@dataclass
class SalesmessageClient:
    token: str
    base_url: str
    timeout_seconds: int = 30

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.token:
            raise SalesmessageApiError("SALESMESSAGE_API_TOKEN is missing")
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        response = requests.request(
            method=method,
            url=url,
            params=params,
            headers=headers,
            timeout=self.timeout_seconds,
        )
        if response.status_code >= 400:
            raise SalesmessageApiError(
                f"{method} {path} failed with {response.status_code}: {response.text[:500]}"
            )
        if not response.content:
            return None
        return response.json()

    def list_conversations(
        self,
        filter_name: str,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            "/conversations",
            params={"filter": filter_name, "limit": limit, "offset": offset},
        )
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return data
        return []

    def get_messages_paginated(
        self,
        conversation_id: int,
        per_page: int = 100,
        page: int = 1,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        payload = self._request(
            "GET",
            f"/messages/{conversation_id}/paginated",
            params={"per_page": per_page, "page": page},
        )
        if not isinstance(payload, dict):
            return [], {}
        data = payload.get("data")
        meta = payload.get("meta")
        if not isinstance(data, list):
            data = []
        if not isinstance(meta, dict):
            meta = {}
        return data, meta
