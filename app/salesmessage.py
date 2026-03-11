from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Any

import requests


class SalesmessageApiError(RuntimeError):
    pass


@dataclass
class SalesmessageClient:
    token: str
    base_url: str
    timeout_seconds: int = 30
    max_retries: int = 3
    retry_backoff_seconds: float = 1.0

    def _request(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        if not self.token:
            raise SalesmessageApiError("SALESMESSAGE_API_TOKEN is missing")
        url = f"{self.base_url}{path}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        retryable_statuses = {429, 500, 502, 503, 504}
        last_error: str | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    params=params,
                    headers=headers,
                    timeout=self.timeout_seconds,
                )
            except requests.RequestException as exc:
                last_error = f"{method} {path} request error: {exc}"
                if attempt >= self.max_retries:
                    raise SalesmessageApiError(last_error) from exc
                time.sleep(self.retry_backoff_seconds * attempt)
                continue

            if response.status_code < 400:
                if not response.content:
                    return None
                return response.json()

            last_error = f"{method} {path} failed with {response.status_code}: {response.text[:500]}"
            if response.status_code not in retryable_statuses or attempt >= self.max_retries:
                raise SalesmessageApiError(last_error)
            time.sleep(self.retry_backoff_seconds * attempt)

        raise SalesmessageApiError(last_error or f"{method} {path} failed")

    def list_conversations(
        self,
        filter_name: str,
        limit: int = 100,
        offset: int = 0,
        inbox_id: int | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"filter": filter_name, "limit": limit, "offset": offset}
        if inbox_id is not None:
            params["inbox_id"] = inbox_id
        payload = self._request(
            "GET",
            "/conversations",
            params=params,
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
