from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import requests


class DaysmartApiError(RuntimeError):
    pass


@dataclass
class DaysmartClient:
    client_id: str
    client_secret: str
    base_url: str = "https://api.dashplatform.com"
    timeout_seconds: int = 30
    _access_token: str | None = field(default=None, init=False, repr=False)

    def _token(self) -> str:
        if self._access_token:
            return self._access_token
        if not self.client_id or not self.client_secret:
            raise DaysmartApiError("DASH_API_CLIENT_ID or DASH_API_SECRET is missing")
        url = f"{self.base_url}/v1/auth/token"
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        headers = {
            "Content-Type": "application/vnd.api+json",
            "Accept": "application/vnd.api+json",
        }
        response = requests.post(url, json=payload, headers=headers, timeout=self.timeout_seconds)
        if response.status_code >= 400:
            raise DaysmartApiError(
                f"POST /v1/auth/token failed with {response.status_code}: {response.text[:500]}"
            )
        data = response.json()
        token = data.get("access_token") or data.get("token")
        if not isinstance(token, str) or not token:
            raise DaysmartApiError("No access token returned by DaySmart auth")
        self._access_token = token
        return token

    def _request(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        token = self._token()
        url = f"{self.base_url}{endpoint}"
        headers = {
            "Accept": "application/vnd.api+json",
            "Content-Type": "application/vnd.api+json",
            "Authorization": f"Bearer {token}",
        }
        response = requests.get(url, headers=headers, params=params, timeout=self.timeout_seconds)
        if response.status_code >= 400:
            raise DaysmartApiError(
                f"GET {endpoint} failed with {response.status_code}: {response.text[:500]}"
            )
        return response.json()

    def _get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        # Historical app code expects a private _get helper for one-off lookups.
        return self._request(endpoint, params=params)

    def list_customers(self, page_number: int = 1, page_size: int = 200) -> tuple[list[dict[str, Any]], int]:
        payload = self._request(
            "/api/v1/customers",
            params={"page[number]": page_number, "page[size]": page_size},
        )
        data = payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(data, list):
            data = []
        last_page = (
            ((payload.get("meta") or {}).get("page") or {}).get("last-page")
            if isinstance(payload, dict)
            else 1
        )
        if not isinstance(last_page, int) or last_page < 1:
            last_page = page_number
        return data, last_page

    def list_memberships(
        self,
        page_number: int = 1,
        page_size: int = 200,
    ) -> tuple[list[dict[str, Any]], int]:
        payload = self._request(
            "/api/v1/memberships",
            params={"page[number]": page_number, "page[size]": page_size},
        )
        data = payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(data, list):
            data = []
        last_page = (
            ((payload.get("meta") or {}).get("page") or {}).get("last-page")
            if isinstance(payload, dict)
            else 1
        )
        if not isinstance(last_page, int) or last_page < 1:
            last_page = page_number
        return data, last_page

    def get_membership(self, membership_id: int | str) -> dict[str, Any]:
        payload = self._request(f"/api/v1/memberships/{membership_id}?include=product")
        data = payload.get("data") if isinstance(payload, dict) else None
        included = payload.get("included") if isinstance(payload, dict) else []
        if not isinstance(data, dict):
            raise DaysmartApiError(f"Membership {membership_id} did not return a valid payload")
        if not isinstance(included, list):
            included = []
        data["_included"] = included
        return data

    def get_product(self, product_id: int | str) -> dict[str, Any]:
        payload = self._request(f"/api/v1/products/{product_id}")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            raise DaysmartApiError(f"Product {product_id} did not return a valid payload")
        return data

    def list_checkin_events(
        self,
        page_number: int = 1,
        page_size: int = 200,
    ) -> tuple[list[dict[str, Any]], int]:
        payload = self._request(
            "/api/v1/check-in-events",
            params={"page[number]": page_number, "page[size]": page_size},
        )
        data = payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(data, list):
            data = []
        last_page = (
            ((payload.get("meta") or {}).get("page") or {}).get("last-page")
            if isinstance(payload, dict)
            else 1
        )
        if not isinstance(last_page, int) or last_page < 1:
            last_page = page_number
        return data, last_page

    def list_registrations(
        self,
        page_number: int = 1,
        page_size: int = 200,
    ) -> tuple[list[dict[str, Any]], int]:
        payload = self._request(
            "/api/v1/registrations",
            params={"page[number]": page_number, "page[size]": page_size},
        )
        data = payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(data, list):
            data = []
        last_page = (
            ((payload.get("meta") or {}).get("page") or {}).get("last-page")
            if isinstance(payload, dict)
            else 1
        )
        if not isinstance(last_page, int) or last_page < 1:
            last_page = page_number
        return data, last_page

    def list_event_registrations(
        self,
        page_number: int = 1,
        page_size: int = 200,
    ) -> tuple[list[dict[str, Any]], int]:
        payload = self._request(
            "/api/v1/event-registrations",
            params={"page[number]": page_number, "page[size]": page_size},
        )
        data = payload.get("data") if isinstance(payload, dict) else []
        if not isinstance(data, list):
            data = []
        last_page = (
            ((payload.get("meta") or {}).get("page") or {}).get("last-page")
            if isinstance(payload, dict)
            else 1
        )
        if not isinstance(last_page, int) or last_page < 1:
            last_page = page_number
        return data, last_page
