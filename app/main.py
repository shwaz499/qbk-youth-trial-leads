from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import os
import re
import threading
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .analysis import answer_locally, answer_with_llm, get_recent_messages, search_messages
from .config import get_settings, salesmessage_oauth
from .db import get_conn, init_db
from .daysmart import DaysmartApiError, DaysmartClient
from .ingest import sync_conversations
from .salesmessage import SalesmessageApiError, SalesmessageClient
from .unified import (
    FTYC_DISCOUNT_ID,
    FTYC_REGISTRATION_MATCH_WINDOW_SECONDS,
    _as_decimal,
    _as_dt,
    _as_int,
    _is_youth_trial_class_name,
    _upsert_daysmart_customer,
    _upsert_daysmart_class_registration,
    _upsert_ftyc_trial_registration,
    recompute_risk_alerts,
    sync_daysmart_to_unified,
    sync_salesmessage_to_unified,
)
from .youth_kpis import (
    LOCAL_TZ as YOUTH_KPI_LOCAL_TZ,
    build_youth_kpi_dashboard,
    build_youth_kpi_email_preview,
    build_youth_kpi_timeseries,
    clear_youth_kpi_dashboard_cache,
    _load_attendance_fallback,
    _load_cached_location_report_checkins_all,
    _load_daysmart_api_checkins,
    _load_location_report_checkins,
    _load_roster_checkins,
    _parse_ts as _parse_youth_kpi_ts,
    _attendance_override_status,
    _registration_checked_in_from_cached_sources,
    _refresh_recent_daysmart_ftyc_team_leads,
)

app = FastAPI(title="Salesmessage AI Agent", version="0.1.0")
settings = get_settings()
static_dir = Path(__file__).parent / "static"
snapshot_requested = os.getenv("YOUTH_KPI_SNAPSHOT_MODE", "").strip().lower() in {"1", "true", "yes", "on"}
snapshot_target = os.getenv("YOUTH_KPI_SNAPSHOT_TARGET", "").strip().lower()
snapshot_mode = snapshot_requested and snapshot_target in {"", "render"}
snapshot_dir = Path(os.getenv("YOUTH_KPI_SNAPSHOT_DIR", Path(__file__).parent / "snapshot")).resolve()
if not snapshot_mode:
    init_db(settings.database_url)
app.mount("/static", StaticFiles(directory=static_dir), name="static")
sync_state_lock = threading.Lock()
sync_state: dict[str, Any] = {
    "running": False,
    "stage": "idle",
    "started_at": None,
    "finished_at": None,
    "last_error": None,
    "last_result": None,
    "salesmessage_progress": None,
}
LEAD_CUTOFF_DATE = "2026-03-01"
LEAD_CUTOFF_TIMESTAMP = "2026-03-01T00:00:00+00:00"
APP_PASSWORD = os.getenv("APP_PASSWORD", "qbkadmin")
AUTH_COOKIE_NAME = "qbk_youth_auth"
AUTH_COOKIE_MAX_AGE = 60 * 60 * 24 * 30
YOUTH_SALESMESSAGE_NUMBER_ID = int(os.getenv("YOUTH_SALESMESSAGE_NUMBER_ID", "222659"))
TRIAL_MISSED_GRACE_PERIOD = dt.timedelta(hours=1)


def _trial_class_at(value: str | None) -> dt.datetime | None:
    return _parse_youth_kpi_ts(value, naive_tz=YOUTH_KPI_LOCAL_TZ)


def _trial_class_is_missed(class_at: dt.datetime | None, now_local: dt.datetime) -> bool:
    if class_at is None:
        return False
    class_local = class_at.astimezone(YOUTH_KPI_LOCAL_TZ)
    return now_local >= class_local + TRIAL_MISSED_GRACE_PERIOD


def _trial_class_time_key(item: dict[str, Any]) -> str | None:
    candidates = [
        item.get("free_trial_class_at"),
        item.get("scheduled_class_at"),
        item.get("trial_class_when"),
        item.get("free_trial_class_date"),
        item.get("scheduled_class_date"),
        item.get("free_trial_class_display"),
        item.get("scheduled_class_display"),
    ]
    for raw_value in candidates:
        if not isinstance(raw_value, str) or not raw_value.strip():
            continue
        value = raw_value.strip()
        display_match = re.search(
            r"(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s*\d{1,2}/\d{1,2}/\d{2}\s+\d{1,2}:\d{2}\s*[AP]M",
            value,
        )
        date_value = display_match.group(0) if display_match else value
        parsed = _trial_class_at(date_value)
        if parsed is None:
            for fmt in ("%a, %m/%d/%y %I:%M %p", "%m/%d/%y %I:%M %p"):
                try:
                    local_value = dt.datetime.strptime(date_value, fmt).replace(tzinfo=YOUTH_KPI_LOCAL_TZ)
                    parsed = local_value.astimezone(dt.timezone.utc)
                    break
                except ValueError:
                    continue
        if parsed is not None:
            return parsed.replace(second=0, microsecond=0).isoformat()
    return None


class SyncRequest(BaseModel):
    filters: list[str] = Field(
        default_factory=lambda: ["open", "closed", "unassigned", "pending"]
    )
    inbox_ids: list[int] | None = None
    min_last_message_at: str | None = LEAD_CUTOFF_TIMESTAMP
    full_history: bool = False
    conversation_page_size: int = 100
    message_page_size: int = 0
    max_message_pages_per_conversation: int = 0


class HostedSyncRequest(SyncRequest):
    daysmart_max_pages: int = 2
    daysmart_page_size: int = 200


class AskRequest(BaseModel):
    question: str
    search_query: str | None = None
    conversation_id: int | None = None
    max_context_messages: int = 30


class RiskRecomputeRequest(BaseModel):
    inactivity_days: int = 14
    outreach_days: int = 30


class DaySmartSyncRequest(BaseModel):
    max_pages: int = 25
    page_size: int = 200


class TrialLeadUpdateRequest(BaseModel):
    trial_status: str | None = None
    account_created: bool | None = None
    added_to_class: bool | None = None


class TrialLeadNoteRequest(BaseModel):
    note_key: str
    note_text: str = ""


class TrialLeadStatusOverrideRequest(BaseModel):
    override_key: str
    status_override: str = ""


class AlertUpdateRequest(BaseModel):
    status: str


class LoginRequest(BaseModel):
    password: str


def _normalize_phone(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 10:
        return None
    return digits[-10:]


def _normalize_name(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    ascii_like = "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in ascii_like)
    cleaned = " ".join(cleaned.split())
    return cleaned or None


def _normalize_email(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _clear_kpi_cache_if_ftyc_changed(result: dict[str, Any]) -> None:
    try:
        changed = int(result.get("leads_upserted") or 0) > 0
    except (TypeError, ValueError):
        changed = False
    if changed:
        clear_youth_kpi_dashboard_cache()


def _lead_source_url(lead_row: dict[str, Any]) -> str | None:
    source_system = str(lead_row.get("source_system") or "")
    source_ref = str(lead_row.get("source_ref") or "").strip()
    if source_system == "salesmessage" and source_ref.isdigit():
        return f"https://app.salesmessage.com/conversations/{source_ref}"
    metadata = _json_loads(lead_row.get("metadata_json"), {})
    if source_system == "gmail_eventbrite" and isinstance(metadata, dict):
        gmail_meta = metadata.get("gmail")
        if isinstance(gmail_meta, dict):
            url = gmail_meta.get("display_url")
            if isinstance(url, str) and url.strip():
                return url.strip()
    return None


def _salesmessage_compose_url(phone: str | None) -> str | None:
    normalized_phone = _normalize_phone(phone)
    if not normalized_phone:
        return None
    e164_phone = f"+1{normalized_phone}"
    params = urlencode(
        {
            "phone": e164_phone,
            "number": e164_phone,
            "to": e164_phone,
            "inbox_id": settings.youth_inbox_id,
            "number_id": YOUTH_SALESMESSAGE_NUMBER_ID,
        }
    )
    return f"https://app.salesmessage.com/conversations/create?{params}"


def _salesmessage_conversation_url_for_phone(conn, phone: str | None) -> str | None:
    normalized_phone = _normalize_phone(phone)
    if not normalized_phone:
        return None
    row = conn.execute(
        """
        SELECT id
        FROM conversations
        WHERE inbox_id = ?
          AND (
            coalesce(contact_number, '') LIKE ?
            OR coalesce(raw_json, '') LIKE ?
          )
        ORDER BY coalesce(last_message_at, started_at, updated_at, '') DESC
        LIMIT 1
        """,
        (settings.youth_inbox_id, f"%{normalized_phone}%", f"%{normalized_phone}%"),
    ).fetchone()
    if row is None:
        return None
    return f"https://app.salesmessage.com/conversations/{row['id']}"


def _phone_click_url(conn, lead_row: dict[str, Any]) -> tuple[str | None, str | None]:
    if lead_row.get("source_system") == "salesmessage":
        return _lead_source_url(lead_row), "conversation"
    if lead_row.get("source_system") == "gmail_eventbrite":
        compose_url = _salesmessage_compose_url(lead_row.get("contact_phone"))
        if compose_url:
            return compose_url, "compose"
    return _lead_source_url(lead_row), "source"


def _eventbrite_child_names(lead_row: dict[str, Any]) -> list[str]:
    metadata = _json_loads(lead_row.get("metadata_json"), {})
    if not isinstance(metadata, dict):
        return []
    order_meta = metadata.get("eventbrite_order")
    if not isinstance(order_meta, dict):
        return []
    children = order_meta.get("children")
    if not isinstance(children, list):
        return []
    names: list[str] = []
    for child in children:
        if not isinstance(child, dict):
            continue
        name = str(child.get("child_name") or "").strip()
        if name:
            names.append(name)
    return names


def _eventbrite_child_rows(
    lead_row: dict[str, Any],
    child_customers: list[dict[str, Any]],
) -> list[dict[str, Any] | None] | None:
    eventbrite_child_names = _eventbrite_child_names(lead_row)
    if lead_row.get("source_system") != "gmail_eventbrite" or not eventbrite_child_names:
        return None

    matched_children_by_name: dict[str, dict[str, Any]] = {}
    for child_customer in child_customers:
        normalized = _normalize_name(child_customer.get("full_name"))
        if normalized:
            matched_children_by_name[normalized] = child_customer

    row_children: list[dict[str, Any] | None] = []
    for child_name in eventbrite_child_names:
        normalized = _normalize_name(child_name)
        matched_child = matched_children_by_name.get(normalized or "")
        if matched_child is not None:
            row_children.append(matched_child)
        else:
            row_children.append({"_eventbrite_child_name": child_name})
    return row_children


def _dedupe_trial_lead_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def dedupe_key(item: dict[str, Any]) -> tuple[Any, ...]:
        parent_name = _normalize_name(item.get("parent_name") or item.get("contact_name"))
        child_name = _normalize_name(item.get("child_name"))
        child_id = item.get("child_daysmart_customer_id")
        class_key = _trial_class_time_key(item)
        class_display = str(
            item.get("free_trial_class_display")
            or item.get("scheduled_class_display")
            or item.get("free_trial_class_name")
            or ""
        ).strip()
        class_identity = class_key or class_display or None
        if parent_name and child_name and class_key:
            return ("family_child_class_time", parent_name, child_name, class_key)
        if child_id not in (None, ""):
            return ("daysmart_child_class", str(child_id), class_identity)
        phone = _normalize_phone(item.get("parent_phone") or item.get("contact_phone"))
        if child_name and phone:
            return ("child_phone_class", child_name, phone, class_identity)
        return (
            "full",
            parent_name,
            child_name,
            phone,
            class_identity,
        )

    def item_score(item: dict[str, Any]) -> tuple[int, str]:
        class_status = str(item.get("free_trial_class_status") or "")
        parent_name = str(item.get("parent_name") or item.get("contact_name") or "").strip()
        class_display = str(item.get("free_trial_class_display") or "").strip()
        source_system = str(item.get("source_system") or "")
        score = 0
        if item.get("child_daysmart_customer_id") not in (None, ""):
            score += 40
        if class_display:
            score += 20
        if source_system == "daysmart_ftyc" and class_display:
            score += 40
        if class_status and class_status != "pending-registration":
            score += 20
        if source_system == "daysmart_ftyc":
            score += 10
        if item.get("conversation_url"):
            score += 8
        if item.get("latest_inbound_text") or item.get("latest_inbound_signal"):
            score += 6
        if parent_name and parent_name != "--":
            score += 5
        if _normalize_phone(item.get("parent_phone") or item.get("contact_phone")):
            score += 3
        return score, str(item.get("last_interaction_at") or item.get("updated_at") or "")

    deduped_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for item in items:
        key = dedupe_key(item)
        if key not in deduped_by_key:
            deduped_by_key[key] = item
            order.append(key)
            continue
        existing = deduped_by_key[key]
        if item_score(item) > item_score(existing):
            for field in ("parent_name", "contact_name", "parent_phone", "contact_phone", "conversation_url", "phone_url"):
                if not item.get(field) and existing.get(field):
                    item[field] = existing[field]
            deduped_by_key[key] = item
    primary_items = [deduped_by_key[key] for key in order]
    class_names = {
        _normalize_name(item.get("child_name"))
        for item in primary_items
        if item.get("free_trial_class_display")
    }
    class_names = {
        name
        for name in class_names
        if name and len(str(name).split()) >= 2
    }
    return [
        item
        for item in primary_items
        if item.get("free_trial_class_display")
        or _normalize_name(item.get("child_name")) not in class_names
    ]


def _trial_lead_note_key(item: dict[str, Any]) -> str:
    child_id = item.get("child_daysmart_customer_id")
    if child_id not in (None, ""):
        return f"daysmart_child:{child_id}"
    lead_key = str(item.get("lead_key") or "").strip()
    if lead_key:
        return f"lead:{lead_key}"
    return ":".join(
        part
        for part in (
            "fallback",
            _normalize_name(item.get("parent_name") or item.get("contact_name")) or "",
            _normalize_name(item.get("child_name")) or "",
            _normalize_phone(item.get("contact_phone")) or "",
        )
        if part
    )


def _trial_lead_status_override_key(item: dict[str, Any]) -> str:
    item_key = str(item.get("item_key") or "").strip()
    if item_key:
        return f"item:{item_key}"
    return _trial_lead_note_key(item)


def _attach_trial_lead_status_overrides(items: list[dict[str, Any]]) -> None:
    override_keys = [_trial_lead_status_override_key(item) for item in items]
    overrides_by_key: dict[str, str] = {}
    if override_keys:
        placeholders = ",".join("?" for _ in override_keys)
        with get_conn(settings.database_url) as conn:
            rows = conn.execute(
                f"""
                SELECT override_key, status_override
                FROM youth_trial_lead_status_overrides
                WHERE override_key IN ({placeholders})
                """,
                override_keys,
            ).fetchall()
        overrides_by_key = {
            str(row["override_key"]): str(row["status_override"] or "")
            for row in rows
        }
    for item, override_key in zip(items, override_keys):
        item["local_status_override_key"] = override_key
        item["local_status_override"] = overrides_by_key.get(override_key, "")


def _attach_trial_lead_notes(items: list[dict[str, Any]]) -> None:
    note_keys = [_trial_lead_note_key(item) for item in items]
    notes_by_key: dict[str, str] = {}
    if note_keys:
        placeholders = ",".join("?" for _ in note_keys)
        with get_conn(settings.database_url) as conn:
            rows = conn.execute(
                f"""
                SELECT note_key, note_text
                FROM youth_trial_lead_notes
                WHERE note_key IN ({placeholders})
                """,
                note_keys,
            ).fetchall()
        notes_by_key = {
            str(row["note_key"]): str(row["note_text"] or "")
            for row in rows
        }
    for item, note_key in zip(items, note_keys):
        item["local_note_key"] = note_key
        item["local_note"] = notes_by_key.get(note_key, "")


def _lead_default_sort_timestamp(item: dict[str, Any]) -> float | None:
    value = (
        item.get("latest_incoming_salesmessage_at")
        if item.get("source_system") == "salesmessage"
        else item.get("last_interaction_at")
    )
    parsed = _as_dt(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=YOUTH_KPI_LOCAL_TZ)
    return parsed.timestamp()


def _lead_secondary_sort_timestamp(item: dict[str, Any]) -> float | None:
    parsed = _as_dt(item.get("last_interaction_at") or item.get("updated_at"))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=YOUTH_KPI_LOCAL_TZ)
    return parsed.timestamp()


def _sort_trial_lead_items_default(items: list[dict[str, Any]]) -> None:
    items.sort(
        key=lambda item: (
            _lead_default_sort_timestamp(item) is not None,
            _lead_default_sort_timestamp(item) or float("-inf"),
            _lead_secondary_sort_timestamp(item) or float("-inf"),
        ),
        reverse=True,
    )


def _daysmart_account_url(customer_id: int) -> str:
    company = settings.daysmart_company
    return (
        "https://apps.daysmartrecreation.com/dash/admin/index.php"
        f"?Action=CustomerInfo&CustomerID={customer_id}&company={company}"
    )


def _daysmart_client() -> DaysmartClient:
    return DaysmartClient(
        client_id=settings.daysmart_api_client_id,
        client_secret=settings.daysmart_api_secret,
        base_url=settings.daysmart_base_url,
    )


def _refresh_recent_daysmart_customers(max_pages: int = 2, page_size: int = 200) -> dict[str, Any]:
    client = _daysmart_client()
    first_rows, last_page = client.list_customers(page_number=1, page_size=page_size)
    if not first_rows or last_page < 1:
        return {"customers_upserted": 0, "pages_checked": 0}

    start_page = max(1, last_page - max_pages + 1)
    upserted = 0
    customer_ids: list[int] = []
    with get_conn(settings.database_url) as conn:
        for page in range(start_page, last_page + 1):
            rows = first_rows if page == 1 else client.list_customers(
                page_number=page,
                page_size=page_size,
            )[0]
            for row in rows:
                _upsert_daysmart_customer(settings.database_url, row, conn=conn)
                upserted += 1
                customer_id = _as_int(row.get("id"))
                if customer_id is not None:
                    customer_ids.append(customer_id)
    return {
        "customers_upserted": upserted,
        "pages_checked": max(0, last_page - start_page + 1),
        "last_page": last_page,
        "customer_ids": customer_ids,
    }


def _refresh_recent_salesmessage_headers() -> dict[str, Any]:
    client = SalesmessageClient(
        token=settings.salesmessage_api_token,
        base_url=settings.salesmessage_base_url,
        max_retries=1,
    )
    stats = sync_conversations(
        client=client,
        db_path=settings.database_url,
        filters=["open", "closed", "unassigned", "pending"],
        conv_page_size=100,
        message_page_size=0,
        max_message_pages_per_conversation=0,
        target_inbox_ids={settings.youth_inbox_id, settings.legacy_youth_inbox_id},
        min_last_message_at=LEAD_CUTOFF_TIMESTAMP,
        full_history=False,
        progress_callback=None,
    )
    unified_stats = sync_salesmessage_to_unified(
        settings.database_url,
        settings.youth_inbox_id,
        cutoff_date=None,
        legacy_youth_inbox_id=settings.legacy_youth_inbox_id,
    )
    return {**stats, "unified": unified_stats}


def _registration_dict_from_row(row: Any) -> dict[str, Any]:
    return {
        "registration_id": int(row["registration_id"]),
        "customer_id": int(row["customer_id"]),
        "team_or_event_id": row["team_or_event_id"],
        "event_name": row["event_name"],
        "event_start": row["event_start"],
        "created_at": row["created_at"],
    }


def _refresh_daysmart_trial_registrations_for_customers(
    customer_ids: set[int],
    *,
    page_size: int = 50,
) -> dict[str, Any]:
    if not customer_ids:
        return {"customers_checked": 0, "registrations_upserted": 0, "ftyc_upserted": 0}

    client = _daysmart_client()
    registrations_upserted = 0
    ftyc_upserted = 0
    with get_conn(settings.database_url) as conn:
        for customer_id in sorted(customer_ids):
            try:
                event_rows, _ = client.list_event_registrations(
                    page_number=1,
                    page_size=page_size,
                    filters={"customer_id": customer_id},
                    sort="-time",
                )
            except Exception:
                continue

            for row in event_rows:
                attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
                event_id = _as_int(attrs.get("event_id"))
                event_name: str | None = None
                event_start: str | None = None
                if event_id is not None:
                    try:
                        event_name, event_start = _daysmart_event_summary(event_id)
                    except Exception:
                        event_name, event_start = None, None
                _upsert_daysmart_class_registration(
                    settings.database_url,
                    source_type="event_registration",
                    row=row,
                    event_name=event_name,
                    event_start=event_start,
                    conn=conn,
                )
                registrations_upserted += 1

            try:
                invoice_rows, _ = client.list_invoice_items(
                    page_number=1,
                    page_size=page_size,
                    filters={"customer_id": customer_id},
                    sort="-date",
                )
            except Exception:
                continue

            candidates = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT registration_id, customer_id, team_or_event_id, event_name, event_start, created_at
                    FROM daysmart_class_registrations
                    WHERE source_type = 'event_registration'
                      AND customer_id = ?
                    """,
                    (customer_id,),
                ).fetchall()
            ]
            for invoice_item in invoice_rows:
                attrs = invoice_item.get("attributes") if isinstance(invoice_item.get("attributes"), dict) else {}
                if _as_int(attrs.get("discount_id")) != FTYC_DISCOUNT_ID:
                    continue
                if attrs.get("is_reversal") or attrs.get("reversal_item_id") or attrs.get("reversed_item_id"):
                    continue
                price = _as_decimal(attrs.get("price"))
                if price is None or price >= 0:
                    continue
                invoice_ts = _as_dt(attrs.get("created_at")) or _as_dt(attrs.get("date"))
                if invoice_ts is None:
                    continue

                best: dict[str, Any] | None = None
                best_event_ts: dt.datetime | None = None
                for candidate in candidates:
                    if not _is_youth_trial_class_name(candidate.get("event_name")):
                        continue
                    registration_ts = _as_dt(candidate.get("created_at"))
                    if registration_ts is None:
                        continue
                    delta = abs((registration_ts - invoice_ts).total_seconds())
                    if delta > FTYC_REGISTRATION_MATCH_WINDOW_SECONDS:
                        continue
                    event_ts = _as_dt(candidate.get("event_start")) or registration_ts
                    if best is None or event_ts > (best_event_ts or event_ts):
                        best = candidate
                        best_event_ts = event_ts

                if best is None:
                    continue
                _upsert_ftyc_trial_registration(
                    conn,
                    registration=_registration_dict_from_row(best),
                    invoice_item=invoice_item,
                )
                ftyc_upserted += 1

    return {
        "customers_checked": len(customer_ids),
        "registrations_upserted": registrations_upserted,
        "ftyc_upserted": ftyc_upserted,
    }


def _daysmart_team_summary(team_id: int) -> tuple[str | None, str | None]:
    client = _daysmart_client()
    payload = client._get(f"/api/v1/teams/{team_id}")
    data = payload.get("data") if isinstance(payload, dict) else None
    attrs = data.get("attributes") if isinstance(data, dict) and isinstance(data.get("attributes"), dict) else {}
    return attrs.get("name"), attrs.get("start_date")


def _set_sync_state(updates: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if updates:
        merged.update(updates)
    if kwargs:
        merged.update(kwargs)
    with sync_state_lock:
        sync_state.update(merged)
        return dict(sync_state)


def _get_sync_state() -> dict[str, Any]:
    with sync_state_lock:
        return dict(sync_state)


def _auth_signature(payload: str) -> str:
    return hmac.new(APP_PASSWORD.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


def _auth_token() -> str:
    payload = "authenticated"
    return f"{payload}.{_auth_signature(payload)}"


def _is_authenticated(request: Request) -> bool:
    token = request.cookies.get(AUTH_COOKIE_NAME, "")
    if "." not in token:
        return False
    payload, signature = token.split(".", 1)
    if payload != "authenticated":
        return False
    return hmac.compare_digest(signature, _auth_signature(payload))


def _snapshot_dashboard_path(days: int, window: str | None) -> Path:
    normalized_window = (window or "").strip().lower()
    if normalized_window in {"this_year", "all_time"}:
        return snapshot_dir / f"dashboard__window_{normalized_window}.json"
    return snapshot_dir / f"dashboard__days_{int(days or 7)}.json"


def _snapshot_timeseries_path(days: int, window: str | None, granularity: str) -> Path:
    normalized_window = (window or "").strip().lower()
    normalized_granularity = (granularity or "month").strip().lower()
    if normalized_window in {"this_year", "all_time"}:
        return snapshot_dir / f"timeseries__window_{normalized_window}__{normalized_granularity}.json"
    return snapshot_dir / f"timeseries__days_{int(days or 7)}__{normalized_granularity}.json"


def _load_snapshot_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Snapshot data is missing: {path.name}")
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Snapshot data is invalid: {path.name}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail=f"Snapshot data is invalid: {path.name}")
    return payload


@app.middleware("http")
async def require_auth(request: Request, call_next):
    public_paths = {
        "/",
        "/health",
        "/youth-kpis",
        "/youth-deals",
        "/api/auth/status",
        "/api/login",
        "/oauth/salesmessage/start",
        "/oauth/salesmessage/callback",
    }
    path = request.url.path
    if path in public_paths or path.startswith("/static"):
        return await call_next(request)
    if not _is_authenticated(request):
        return JSONResponse(status_code=401, content={"detail": "Authentication required."})
    return await call_next(request)


def _customer_attrs(customer_row: dict[str, Any]) -> dict[str, Any]:
    payload = _json_loads(customer_row.get("raw_json"), {})
    if not isinstance(payload, dict):
        return {}
    attrs = payload.get("attributes")
    return attrs if isinstance(attrs, dict) else {}


def _customer_birthdate(customer_row: dict[str, Any]) -> dt.date | None:
    value = _customer_attrs(customer_row).get("birthdate")
    if not isinstance(value, str) or not value:
        return None
    try:
        return dt.date.fromisoformat(value[:10])
    except ValueError:
        return None


def _is_parent_candidate(
    customer_row: dict[str, Any],
    *,
    cutoff: dt.date,
    child_name: str | None = None,
) -> bool:
    birthdate = _customer_birthdate(customer_row)
    if birthdate is not None:
        return birthdate < cutoff
    normalized_child_name = _normalize_name(child_name)
    normalized_customer_name = _normalize_name(customer_row.get("full_name"))
    return bool(normalized_customer_name and normalized_customer_name != normalized_child_name)


def _find_daysmart_matches(conn: Any, lead_row: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    metadata = _json_loads(lead_row.get("metadata_json"), {})
    contact = metadata.get("contact") if isinstance(metadata, dict) else {}
    if not isinstance(contact, dict):
        contact = {}

    if str(lead_row.get("source_system") or "") == "daysmart_ftyc":
        daysmart_meta = metadata.get("daysmart_ftyc") if isinstance(metadata, dict) else {}
        if not isinstance(daysmart_meta, dict):
            daysmart_meta = {}
        try:
            child_customer_id = int(daysmart_meta.get("customer_id"))
        except (TypeError, ValueError):
            child_customer_id = None
        child = None
        if child_customer_id is not None:
            child_row = conn.execute(
                "SELECT * FROM daysmart_customers WHERE customer_id = ?",
                (child_customer_id,),
            ).fetchone()
            if child_row is not None:
                child = dict(child_row)
        parent = None
        if child is not None:
            phone = _normalize_phone(
                lead_row.get("contact_phone")
                or child.get("phone_day")
                or child.get("phone_mobile")
                or child.get("phone_emergency")
            )
            email = _normalize_email(child.get("email"))
            cutoff = dt.date.today().replace(year=dt.date.today().year - 18)
            if phone:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM daysmart_customers
                    WHERE customer_id != ?
                      AND (
                        normalized_phone_day = ?
                        OR normalized_phone_mobile = ?
                        OR normalized_phone_night = ?
                        OR normalized_phone_emergency = ?
                      )
                    ORDER BY updated_at DESC
                    LIMIT 10
                    """,
                    (child_customer_id, phone, phone, phone, phone),
                ).fetchall()
                for row in rows:
                    row_dict = dict(row)
                    if _is_parent_candidate(row_dict, cutoff=cutoff, child_name=child.get("full_name")):
                        parent = row_dict
                        break
            if parent is None and email:
                rows = conn.execute(
                    """
                    SELECT *
                    FROM daysmart_customers
                    WHERE customer_id != ?
                      AND normalized_email = ?
                    ORDER BY updated_at DESC
                    LIMIT 10
                    """,
                    (child_customer_id, email),
                ).fetchall()
                for row in rows:
                    row_dict = dict(row)
                    if _is_parent_candidate(row_dict, cutoff=cutoff, child_name=child.get("full_name")):
                        parent = row_dict
                        break
        return parent, [child] if child is not None else []

    phone = _normalize_phone(
        lead_row.get("contact_phone")
        or contact.get("number")
        or contact.get("formatted_number")
        or contact.get("phone")
    )
    email = _normalize_email(contact.get("email"))
    name = _normalize_name(lead_row.get("contact_name") or contact.get("full_name"))
    phone_matches: list[dict[str, Any]] = []

    if phone:
        rows = conn.execute(
            """
            SELECT *
            FROM daysmart_customers
            WHERE normalized_phone_day = ?
               OR normalized_phone_mobile = ?
               OR normalized_phone_night = ?
               OR normalized_phone_emergency = ?
            ORDER BY updated_at DESC
            LIMIT 10
            """,
            (phone, phone, phone, phone),
        ).fetchall()
        phone_matches = [dict(row) for row in rows]

    cutoff = dt.date.today().replace(year=dt.date.today().year - 18)
    parent: dict[str, Any] | None = None
    if phone_matches:
        if name:
            for row in phone_matches:
                if row["normalized_name"] == name:
                    parent = row
                    break
        if parent is None and email:
            for row in phone_matches:
                if row["normalized_email"] == email:
                    parent = row
                    break
        if parent is None:
            adult_candidates: list[tuple[dt.date, dict[str, Any]]] = []
            for row in phone_matches:
                birthdate = _customer_birthdate(row)
                if birthdate is not None and birthdate < cutoff:
                    adult_candidates.append((birthdate, row))
            if adult_candidates:
                adult_candidates.sort(key=lambda item: item[0])
                parent = adult_candidates[0][1]
        if parent is None:
            parent = phone_matches[0]

    if parent is None and email and name:
        row = conn.execute(
            """
            SELECT *
            FROM daysmart_customers
            WHERE normalized_email = ?
              AND normalized_name = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (email, name),
        ).fetchone()
        if row is not None:
            parent = dict(row)

    children: list[dict[str, Any]] = []
    if phone_matches:
        parent_id = parent["customer_id"] if parent is not None else None
        non_parent = [row for row in phone_matches if row["customer_id"] != parent_id]
        age_candidates: list[tuple[dt.date, dict[str, Any]]] = []
        for row in non_parent:
            birthdate = _customer_birthdate(row)
            if birthdate is not None and birthdate >= cutoff:
                age_candidates.append((birthdate, row))
        if age_candidates:
            age_candidates.sort(key=lambda item: item[0], reverse=True)
            children = [row for _, row in age_candidates]
        elif len(non_parent) == 1:
            children = [non_parent[0]]

    return parent, children


def _related_daysmart_customer_ids(parent: dict[str, Any] | None, child: dict[str, Any] | None) -> list[int]:
    related: set[int] = set()
    for row in (parent, child):
        if row is not None and isinstance(row, dict) and row.get("customer_id") is not None:
            related.add(int(row["customer_id"]))
    return sorted(related)


def _daysmart_has_class_registration(conn: Any, customer_id: int | None) -> bool:
    if customer_id is None:
        return False
    row = conn.execute(
        """
        SELECT 1
        FROM daysmart_class_registrations
        WHERE customer_id = ?
        LIMIT 1
        """,
        (customer_id,),
    ).fetchone()
    return row is not None


def _format_daysmart_class_date(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    parsed = None
    cleaned = value.strip().replace("Z", "+00:00")
    for candidate in (cleaned, cleaned.replace(" ", "T")):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
            break
        except ValueError:
            continue
    if parsed is None:
        try:
            parsed = dt.datetime.strptime(cleaned[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            try:
                parsed = dt.datetime.strptime(cleaned[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return value[:10]
    has_time = bool(re.search(r"\d{1,2}:\d{2}:\d{2}", cleaned))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone()
        return parsed.strftime("%a, %-m/%-d/%y %-I:%M %p")
    return parsed.strftime("%a, %-m/%-d/%y %-I:%M %p") if has_time else parsed.strftime("%a, %-m/%-d/%y")


def _clean_trial_class_name(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if cleaned.startswith("Junior Classes - "):
        cleaned = cleaned[len("Junior Classes - "):].strip()
    cleaned = re.sub(r"\(\s*(\d+)\s*-\s*(\d+)\s*y/o\s*\)", r"(\1-\2)", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _contains_any_token(value: str | None, tokens: tuple[str, ...]) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return any(token in lowered for token in tokens)


def _is_youth_class_name(value: str | None) -> bool:
    return _contains_any_token(value, ("seals", "cubs"))


def _is_allowed_membership_name(value: str | None) -> bool:
    if not _contains_any_token(value, ("seals", "cubs")):
        return False
    return not _contains_any_token(value, ("beach lions", "staff"))


def _membership_display_label(product_name: str | None) -> str:
    base = (product_name or "").strip() or "Unnamed membership"
    return base


def _membership_is_currently_active_record(record: dict[str, Any]) -> bool:
    today = dt.datetime.now(YOUTH_KPI_LOCAL_TZ).date()
    created_at = _as_dt(record.get("created_at"))
    if created_at is not None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=YOUTH_KPI_LOCAL_TZ)
        if created_at.astimezone(YOUTH_KPI_LOCAL_TZ).date() > today:
            return False
    expires_at = _as_dt(record.get("expires_at"))
    if expires_at is None:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=YOUTH_KPI_LOCAL_TZ)
    return expires_at.astimezone(YOUTH_KPI_LOCAL_TZ).date() >= today


def _daysmart_event_summary(event_id: int) -> tuple[str | None, str | None]:
    client = _daysmart_client()
    payload = client._get(f"/api/v1/events/{event_id}")
    data = payload.get("data") if isinstance(payload, dict) else None
    attrs = data.get("attributes") if isinstance(data, dict) and isinstance(data.get("attributes"), dict) else {}
    name = attrs.get("desc") or attrs.get("name")
    home_team_id = attrs.get("hteam_id")
    if not name and home_team_id:
        try:
            team_name, _ = _daysmart_team_summary(int(home_team_id))
            name = team_name or name
        except Exception:
            pass
    return name, attrs.get("start")


def _daysmart_event_registration_exists(registration_id: int) -> bool:
    client = _daysmart_client()
    try:
        payload = client._get(f"/api/v1/event-registrations/{registration_id}")
    except Exception as exc:
        if "failed with 404" in str(exc):
            return False
        raise
    data = payload.get("data") if isinstance(payload, dict) else None
    return isinstance(data, dict)


def _daysmart_first_class(
    conn: Any,
    customer_ids: list[int],
    *,
    verify_live: bool = False,
) -> tuple[str | None, str | None]:
    registration = _daysmart_first_class_registration(conn, customer_ids, verify_live=verify_live)
    if registration is None:
        return None, None
    return registration.get("event_name_clean"), registration.get("event_start_display")


def _daysmart_first_class_registration(
    conn: Any,
    customer_ids: list[int],
    *,
    verify_live: bool = False,
    min_started_at: dt.datetime | None = None,
) -> dict[str, Any] | None:
    if not customer_ids:
        return None
    placeholders = ",".join("?" for _ in customer_ids)
    rows = conn.execute(
        f"""
        SELECT
            registration_id,
            customer_id,
            event_id AS team_or_event_id,
            event_name,
            event_start,
            registration_created_at AS created_at,
            raw_json AS registration_raw_json
        FROM daysmart_ftyc_trial_registrations
        WHERE customer_id IN ({placeholders})
        ORDER BY coalesce(event_start, registration_created_at, '') DESC, registration_id DESC
        """,
        customer_ids,
    ).fetchall()
    candidates: list[tuple[bool, dt.datetime | None, int, dict[str, Any]]] = []
    for row in rows:
        row_dict = dict(row)
        registration_id = int(row_dict["registration_id"])
        if verify_live:
            try:
                if not _daysmart_event_registration_exists(registration_id):
                    conn.execute(
                        """
                        DELETE FROM daysmart_class_registrations
                        WHERE source_type = 'event_registration'
                          AND registration_id = ?
                        """,
                        (registration_id,),
                    )
                    continue
            except Exception:
                # If DaySmart lookup fails transiently, keep the cached row rather than
                # blanking the class from a temporary API issue.
                pass
        name = row_dict.get("event_name")
        start_at = row_dict.get("event_start") or row_dict.get("created_at")
        event_id = row_dict.get("team_or_event_id")
        if (not name or not start_at) and event_id:
            try:
                fresh_name, fresh_start = _daysmart_event_summary(int(event_id))
                if fresh_name or fresh_start:
                    conn.execute(
                        """
                        UPDATE daysmart_class_registrations
                        SET event_name = coalesce(?, event_name),
                            event_start = coalesce(?, event_start),
                            updated_at = ?
                        WHERE source_type = 'event_registration'
                          AND registration_id = ?
                        """,
                        (
                            fresh_name,
                            fresh_start,
                            dt.datetime.now(dt.timezone.utc).isoformat(),
                            row_dict["registration_id"],
                        ),
                    )
                    name = fresh_name or name
                    start_at = fresh_start or start_at
            except Exception:
                pass
        name = _clean_trial_class_name(name)
        if not _is_youth_class_name(name):
            continue
        class_at = _as_dt(start_at)
        if min_started_at is not None and (class_at is None or class_at < min_started_at):
            continue
        if name or start_at:
            row_dict["event_name_clean"] = name
            row_dict["event_start_display"] = _format_daysmart_class_date(start_at)
            created_at = _trial_class_at(row_dict.get("created_at"))
            class_local = class_at.astimezone(YOUTH_KPI_LOCAL_TZ) if class_at is not None else None
            created_local = created_at.astimezone(YOUTH_KPI_LOCAL_TZ) if created_at is not None else None
            looks_like_registration_time = bool(
                class_local is not None
                and created_local is not None
                and class_local.date() == created_local.date()
                and class_local.hour < 8
            )
            candidates.append((looks_like_registration_time, class_at, registration_id, row_dict))
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            item[0],
            -((item[1] or dt.datetime.min.replace(tzinfo=dt.timezone.utc)).timestamp()),
            -item[2],
        )
    )
    return candidates[0][3]


def _format_time_component(value: str) -> str:
    match = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(AM|PM)$", value.strip().upper().replace(".", ""))
    if not match:
        return value.strip()
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    meridiem = match.group(3)
    return f"{hour}:{minute:02d} {meridiem}"


def _normalize_trial_class_when(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.replace("’", "'").split())
    if not cleaned:
        return None

    iso_display = _format_daysmart_class_date(cleaned)
    if iso_display and iso_display != cleaned:
        return iso_display

    year = dt.date.today().year
    month_lookup = {
        "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
        "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
        "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9, "october": 10,
        "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    }

    date_part = None
    mmdd = re.search(r"\b(\d{1,2})/(\d{1,2})\b", cleaned)
    if mmdd:
        try:
            date_part = dt.date(year, int(mmdd.group(1)), int(mmdd.group(2)))
        except ValueError:
            date_part = None
    else:
        month_day = re.search(
            r"\b(January|Jan|February|Feb|March|Mar|April|Apr|May|June|Jun|July|Jul|August|Aug|September|Sep|Sept|October|Oct|November|Nov|December|Dec)\s+(\d{1,2})(?:st|nd|rd|th)?\b",
            cleaned,
            re.IGNORECASE,
        )
        if month_day:
            month = month_lookup[month_day.group(1).lower()]
            day = int(month_day.group(2))
            try:
                date_part = dt.date(year, month, day)
            except ValueError:
                date_part = None

    time_match = re.search(
        r"(\d{1,2}(?::\d{2})?\s*(?:AM|PM))(?:\s*-\s*(\d{1,2}(?::\d{2})?\s*(?:AM|PM)))?",
        cleaned,
        re.IGNORECASE,
    )
    time_display = None
    if time_match:
        start = _format_time_component(time_match.group(1))
        end = _format_time_component(time_match.group(2)) if time_match.group(2) else None
        time_display = f"{start}-{end}" if end else start

    weekday_match = re.search(
        r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)(?:'s class)?\b",
        cleaned,
        re.IGNORECASE,
    )
    weekday_label = weekday_match.group(1)[:3].title() if weekday_match else None

    if date_part is not None:
        prefix = weekday_label or date_part.strftime("%a")
        date_display = f"{prefix}, {date_part.strftime('%b %-d, %Y')}"
        return f"{date_display}, {time_display}" if time_display else date_display

    if weekday_match:
        weekday = weekday_match.group(1).title()
        return f"{weekday}, {time_display}" if time_display else weekday

    return cleaned


def _daysmart_memberships(
    conn: Any,
    customer_ids: list[int],
    *,
    min_created_at: dt.datetime | None = None,
) -> list[str]:
    if not customer_ids:
        return []
    placeholders = ",".join("?" for _ in customer_ids)
    rows = conn.execute(
        f"""
        SELECT cm.customer_id, m.product_name, m.created_at, m.expires_at, m.membership_id
        FROM daysmart_memberships m
        JOIN daysmart_customer_memberships cm ON cm.membership_id = m.membership_id
        WHERE cm.customer_id IN ({placeholders})
        ORDER BY cm.customer_id ASC, coalesce(m.expires_at, '') DESC, coalesce(m.created_at, '') ASC, m.membership_id ASC
        """,
        customer_ids,
    ).fetchall()
    seen_customers: set[int] = set()
    items: list[str] = []
    for row in rows:
        customer_id = int(row["customer_id"])
        if customer_id in seen_customers:
            continue
        base = (row["product_name"] or "").strip() or "Unnamed membership"
        if not _is_allowed_membership_name(base):
            continue
        if not _membership_is_currently_active_record(
            {"created_at": row["created_at"], "expires_at": row["expires_at"]}
        ):
            continue
        seen_customers.add(customer_id)
        items.append(_membership_display_label(base))
    return items


def _daysmart_has_excluded_registration(conn: Any, customer_ids: list[int]) -> bool:
    if not customer_ids:
        return False
    placeholders = ",".join("?" for _ in customer_ids)
    rows = conn.execute(
        f"""
        SELECT event_name
        FROM daysmart_class_registrations
        WHERE source_type = 'event_registration'
          AND customer_id IN ({placeholders})
        """,
        customer_ids,
    ).fetchall()
    for row in rows:
        if _contains_any_token(_clean_trial_class_name(row["event_name"]), ("beach lions",)):
            return True
    return False


def _live_customer_memberships(
    customer_id: int,
    *,
    min_created_at: dt.datetime | None = None,
) -> list[str]:
    client = _daysmart_client()
    payload = client._get(f"/api/v1/customers/{customer_id}?include=memberships")
    data = payload.get("data") if isinstance(payload, dict) else None
    relationships = data.get("relationships") if isinstance(data, dict) and isinstance(data.get("relationships"), dict) else {}
    memberships_rel = relationships.get("memberships") if isinstance(relationships, dict) else {}
    membership_data = memberships_rel.get("data") if isinstance(memberships_rel, dict) else []
    if not isinstance(membership_data, list):
        membership_data = []

    included = payload.get("included") if isinstance(payload, dict) else []
    included_map: dict[int, dict[str, Any]] = {}
    if isinstance(included, list):
        for item in included:
            if not isinstance(item, dict):
                continue
            if item.get("type") != "memberships":
                continue
            try:
                membership_id = int(item.get("id"))
            except Exception:
                continue
            included_map[membership_id] = item

    first_membership: tuple[str, str, int] | None = None
    for rel in membership_data:
        if not isinstance(rel, dict):
            continue
        try:
            membership_id = int(rel.get("id"))
        except Exception:
            continue
        membership_payload = included_map.get(membership_id)
        if membership_payload is None:
            try:
                membership_payload = client.get_membership(membership_id)
            except Exception:
                membership_payload = None
        if membership_payload is not None:
            attrs = membership_payload.get("attributes") if isinstance(membership_payload.get("attributes"), dict) else {}
            product_name = None
            included_items = membership_payload.get("_included") if isinstance(membership_payload, dict) else []
            if isinstance(included_items, list):
                for inc in included_items:
                    if not isinstance(inc, dict):
                        continue
                    if inc.get("type") == "products":
                        inc_attrs = inc.get("attributes") if isinstance(inc.get("attributes"), dict) else {}
                        product_name = inc_attrs.get("name") or inc_attrs.get("desc")
                        if product_name:
                            break
            if product_name is None:
                product_name = attrs.get("product_name")
            if product_name is None:
                product_id = attrs.get("prod_id") or attrs.get("product_id")
                if product_id not in (None, ""):
                    try:
                        product = client.get_product(int(product_id))
                        product_attrs = (
                            product.get("attributes")
                            if isinstance(product.get("attributes"), dict)
                            else {}
                        )
                        product_name = product_attrs.get("name") or product_attrs.get("desc")
                    except Exception:
                        product_name = None
            base = (product_name or "").strip() or "Unnamed membership"
            if not _is_allowed_membership_name(base):
                continue
            created = (attrs.get("created") or attrs.get("created_at") or "").strip()
            expires = (attrs.get("expires") or attrs.get("expires_at") or "").strip()
            if not _membership_is_currently_active_record(
                {"created_at": created, "expires_at": expires}
            ):
                continue
            if first_membership is None or (created, membership_id) < (first_membership[1], first_membership[2]):
                first_membership = (_membership_display_label(base), created, membership_id)

    return [first_membership[0]] if first_membership is not None else []


def _run_hosted_sync(req: HostedSyncRequest) -> None:
    _set_sync_state(
        running=True,
        stage="syncing_salesmessage",
        started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        finished_at=None,
        last_error=None,
        last_result=None,
        salesmessage_progress=None,
    )
    try:
        salesmessage_result = sync(
            SyncRequest(
                filters=req.filters,
                inbox_ids=req.inbox_ids,
                min_last_message_at=req.min_last_message_at,
                full_history=req.full_history,
                conversation_page_size=req.conversation_page_size,
                message_page_size=req.message_page_size,
                max_message_pages_per_conversation=req.max_message_pages_per_conversation,
            )
        )
        _set_sync_state(stage="syncing_daysmart", last_result={"salesmessage": salesmessage_result})
        daysmart_client = DaysmartClient(
            client_id=settings.daysmart_api_client_id,
            client_secret=settings.daysmart_api_secret,
            base_url=settings.daysmart_base_url,
        )
        daysmart_result = sync_daysmart_to_unified(
            client=daysmart_client,
            db_path=settings.database_url,
            max_pages=req.daysmart_max_pages,
            page_size=req.daysmart_page_size,
        )
        with get_conn(settings.database_url) as conn:
            daysmart_ftyc_result = _refresh_recent_daysmart_ftyc_team_leads(
                conn,
                settings.database_url,
            )
        _clear_kpi_cache_if_ftyc_changed(daysmart_ftyc_result)
        risk_stats = recompute_risk_alerts(settings.database_url)
        _set_sync_state(
            running=False,
            stage="completed",
            finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            last_result={
                "salesmessage": salesmessage_result,
                "daysmart": daysmart_result,
                "daysmart_ftyc_team_leads": daysmart_ftyc_result,
                "risk": risk_stats,
            },
            last_error=None,
            salesmessage_progress=None,
        )
    except Exception as exc:
        _set_sync_state(
            running=False,
            stage="failed",
            finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            last_error=str(exc),
        )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/")
def ui() -> FileResponse:
    return FileResponse(static_dir / "index.html")


@app.get("/youth-kpis")
def youth_kpis_ui() -> FileResponse:
    return FileResponse(static_dir / "youth-kpis.html")


@app.get("/youth-deals")
def youth_deals_ui() -> FileResponse:
    return FileResponse(static_dir / "youth-deals.html")


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict[str, bool]:
    return {"authenticated": _is_authenticated(request), "snapshot_mode": snapshot_mode}


@app.post("/api/login")
def login(req: LoginRequest, request: Request) -> JSONResponse:
    if req.password != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Incorrect password.")
    response = JSONResponse({"ok": True, "authenticated": True, "snapshot_mode": snapshot_mode})
    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=_auth_token(),
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        max_age=AUTH_COOKIE_MAX_AGE,
        path="/",
    )
    return response


@app.post("/api/logout")
def logout() -> JSONResponse:
    response = JSONResponse({"ok": True})
    response.delete_cookie(key=AUTH_COOKIE_NAME, path="/")
    return response


@app.get("/oauth/salesmessage/start")
def salesmessage_oauth_start() -> RedirectResponse:
    oauth = salesmessage_oauth()
    try:
        return RedirectResponse(oauth.authorization_url())
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/oauth/salesmessage/callback")
def salesmessage_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    if error:
        raise HTTPException(status_code=400, detail=f"Salesmsg OAuth failed: {error}")
    if not code:
        raise HTTPException(status_code=400, detail="Salesmsg OAuth callback did not include a code.")
    try:
        token_payload = salesmessage_oauth().exchange_code(code=code, state=state)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {
        "ok": True,
        "message": "Salesmsg OAuth connected. You can close this tab and refresh the local app.",
        "token_type": token_payload.get("token_type", "Bearer"),
        "expires_in": token_payload.get("expires_in"),
    }


@app.post("/sync")
def sync(req: SyncRequest) -> dict[str, Any]:
    client = SalesmessageClient(
        token=settings.salesmessage_api_token,
        base_url=settings.salesmessage_base_url,
    )
    try:
        stats = sync_conversations(
            client=client,
            db_path=settings.database_url,
            filters=req.filters,
            conv_page_size=req.conversation_page_size,
            message_page_size=req.message_page_size,
            max_message_pages_per_conversation=req.max_message_pages_per_conversation,
            target_inbox_ids=set(req.inbox_ids or [settings.youth_inbox_id, settings.legacy_youth_inbox_id]),
            min_last_message_at=req.min_last_message_at,
            full_history=req.full_history,
            progress_callback=_set_sync_state,
        )
    except SalesmessageApiError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _set_sync_state(stage="rebuilding_youth_leads")
    unified_stats = sync_salesmessage_to_unified(
        settings.database_url,
        settings.youth_inbox_id,
        cutoff_date=None,
        legacy_youth_inbox_id=settings.legacy_youth_inbox_id,
    )
    _set_sync_state(stage="recomputing_risk")
    risk_stats = recompute_risk_alerts(settings.database_url)
    return {"ok": True, **stats, "unified": unified_stats, "risk": risk_stats}


@app.post("/sync/start")
def sync_start(req: HostedSyncRequest) -> dict[str, Any]:
    if snapshot_mode:
        raise HTTPException(status_code=403, detail="This Render build is snapshot-only. Refresh locally and upload a new snapshot.")
    current = _get_sync_state()
    if current.get("running"):
        return {"ok": True, "started": False, **current}
    _set_sync_state(
        running=True,
        stage="queued",
        started_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        finished_at=None,
        last_error=None,
        last_result=None,
        salesmessage_progress=None,
    )
    worker = threading.Thread(target=_run_hosted_sync, args=(req,), daemon=True)
    worker.start()
    return {"ok": True, "started": True, **_get_sync_state()}


@app.get("/sync/status")
def sync_status() -> dict[str, Any]:
    return {"ok": True, **_get_sync_state()}


@app.post("/sync/daysmart")
def sync_daysmart(req: DaySmartSyncRequest) -> dict[str, Any]:
    if snapshot_mode:
        raise HTTPException(status_code=403, detail="This Render build is snapshot-only. Refresh locally and upload a new snapshot.")
    client = DaysmartClient(
        client_id=settings.daysmart_api_client_id,
        client_secret=settings.daysmart_api_secret,
        base_url=settings.daysmart_base_url,
    )
    try:
        stats = sync_daysmart_to_unified(
            client=client,
            db_path=settings.database_url,
            max_pages=req.max_pages,
            page_size=req.page_size,
        )
    except DaysmartApiError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with get_conn(settings.database_url) as conn:
        daysmart_ftyc_result = _refresh_recent_daysmart_ftyc_team_leads(
            conn,
            settings.database_url,
        )
    _clear_kpi_cache_if_ftyc_changed(daysmart_ftyc_result)
    risk_stats = recompute_risk_alerts(settings.database_url)
    return {"ok": True, **stats, "daysmart_ftyc_team_leads": daysmart_ftyc_result, "risk": risk_stats}


@app.post("/risk/recompute")
def recompute_risk(req: RiskRecomputeRequest) -> dict[str, Any]:
    stats = recompute_risk_alerts(
        db_path=settings.database_url,
        inactivity_days=req.inactivity_days,
        outreach_days=req.outreach_days,
    )
    return {"ok": True, **stats}


@app.get("/risk/alerts")
def list_risk_alerts(status: str = "open", limit: int = 200) -> dict[str, Any]:
    with get_conn(settings.database_url) as conn:
        rows = conn.execute(
            """
            SELECT alert_id, family_key, child_key, rule_code, severity, status,
                   details_json, last_triggered_at, created_at, updated_at
            FROM youth_risk_alerts
            WHERE status = ?
            ORDER BY severity DESC, updated_at DESC
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/dashboard/summary")
def dashboard_summary() -> dict[str, Any]:
    with get_conn(settings.database_url) as conn:
        families = conn.execute("SELECT count(*) AS c FROM youth_families").fetchone()["c"]
        children = conn.execute("SELECT count(*) AS c FROM youth_children").fetchone()["c"]
        attendance = conn.execute("SELECT count(*) AS c FROM youth_attendance_events").fetchone()["c"]
        leads = conn.execute(
            "SELECT count(*) AS c FROM youth_trial_leads",
        ).fetchone()["c"]
        open_alerts = conn.execute(
            "SELECT count(*) AS c FROM youth_risk_alerts WHERE status='open'"
        ).fetchone()["c"]
        high_alerts = conn.execute(
            "SELECT count(*) AS c FROM youth_risk_alerts WHERE status='open' AND severity='high'"
        ).fetchone()["c"]
        salesmessage_last = conn.execute("SELECT max(created_at) AS ts FROM messages").fetchone()["ts"]
        daysmart_last = conn.execute(
            "SELECT max(event_at) AS ts FROM youth_attendance_events"
        ).fetchone()["ts"]
        outreach_last = conn.execute(
            "SELECT max(outreach_at) AS ts FROM youth_family_outreach"
        ).fetchone()["ts"]

    return {
        "counts": {
            "families": families,
            "children": children,
            "attendance_events": attendance,
            "trial_leads": leads,
            "open_alerts": open_alerts,
            "high_alerts": high_alerts,
        },
        "freshness": {
            "salesmessage_last_message_at": salesmessage_last,
            "daysmart_last_checkin_at": daysmart_last,
            "last_outreach_at": outreach_last,
        },
    }


@app.get("/dashboard/risk-alerts")
def dashboard_risk_alerts(status: str = "open", limit: int = 200) -> dict[str, Any]:
    with get_conn(settings.database_url) as conn:
        rows = conn.execute(
            """
            SELECT alert_id, family_key, child_key, rule_code, severity, status,
                   details_json, last_triggered_at, created_at, updated_at
            FROM youth_risk_alerts
            WHERE status = ?
            ORDER BY
                CASE severity WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC,
                updated_at DESC
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        try:
            d["details"] = json.loads(d.get("details_json") or "{}")
        except json.JSONDecodeError:
            d["details"] = {}
        items.append(d)
    return {"items": items}


@app.patch("/dashboard/risk-alerts/{alert_id}")
def update_risk_alert(alert_id: int, req: AlertUpdateRequest) -> dict[str, Any]:
    if req.status not in {"open", "resolved"}:
        raise HTTPException(status_code=400, detail="Unsupported alert status")

    with get_conn(settings.database_url) as conn:
        exists = conn.execute(
            "SELECT 1 FROM youth_risk_alerts WHERE alert_id = ?",
            (alert_id,),
        ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Alert not found")
        conn.execute(
            """
            UPDATE youth_risk_alerts
            SET status = ?, updated_at = datetime('now')
            WHERE alert_id = ?
            """,
            (req.status, alert_id),
        )
    return {"ok": True, "alert_id": alert_id, "status": req.status}


@app.get("/dashboard/trial-leads")
def dashboard_trial_leads(
    limit: int = 1000,
    refresh: bool = False,
    attendance_refresh: bool = False,
) -> dict[str, Any]:
    # Keep page loads read-mostly. Salesmessage sync runs through /sync/start so
    # the UI can poll progress instead of blocking the table request.
    salesmessage_refresh: dict[str, Any] = {"skipped": "use_sync_start"}
    daysmart_refresh: dict[str, Any] = {"customers_upserted": 0, "pages_checked": 0}
    daysmart_ftyc_refresh: dict[str, Any] = {"source": "daysmart-ftyc-team-leads", "leads_upserted": 0}
    if refresh:
        try:
            daysmart_refresh = _refresh_recent_daysmart_customers()
        except DaysmartApiError as exc:
            daysmart_refresh = {"customers_upserted": 0, "pages_checked": 0, "error": str(exc)[:240]}
        except Exception as exc:
            daysmart_refresh = {"customers_upserted": 0, "pages_checked": 0, "error": str(exc)[:240]}
    recent_daysmart_customer_ids = set(daysmart_refresh.get("customer_ids") or [])

    sql = """
        WITH first_message_times AS (
            SELECT
                conversation_id,
                MIN(coalesce(created_at, sent_at, received_at, '')) AS first_message_at
            FROM messages
            GROUP BY conversation_id
        ),
        latest_incoming_message_times AS (
            SELECT
                conversation_id,
                MAX(coalesce(received_at, created_at, sent_at, '')) AS latest_incoming_message_at
            FROM messages
            WHERE status = 'received'
            GROUP BY conversation_id
        )
        SELECT t.lead_key, t.family_key, t.inbox_id, t.contact_name, t.contact_phone, t.trial_status,
               account_created, added_to_class, trial_class_name, trial_class_when,
               last_interaction_at, t.updated_at, source_system, source_ref, metadata_json,
               c.started_at AS conversation_started_at,
               c.last_message_at AS conversation_last_message_at,
               fmt.first_message_at AS conversation_first_message_at,
               limt.latest_incoming_message_at AS latest_incoming_salesmessage_at
        FROM youth_trial_leads t
        LEFT JOIN conversations c ON c.id = CAST(t.source_ref AS INTEGER)
        LEFT JOIN first_message_times fmt ON fmt.conversation_id = c.id
        LEFT JOIN latest_incoming_message_times limt ON limt.conversation_id = c.id
        WHERE (
            coalesce(
                nullif(fmt.first_message_at, ''),
                nullif(c.started_at, ''),
                nullif(t.last_interaction_at, ''),
                nullif(c.last_message_at, ''),
                ''
            ) >= ?
            OR coalesce(t.last_interaction_at, '') >= ?
            OR t.source_system = 'gmail_eventbrite'
          )
    """
    params: list[Any] = [LEAD_CUTOFF_DATE, LEAD_CUTOFF_DATE]
    sql += """
        ORDER BY
            julianday(
                CASE
                    WHEN source_system = 'salesmessage'
                    THEN coalesce(latest_incoming_salesmessage_at, '')
                    ELSE coalesce(last_interaction_at, '')
                END
            ) DESC,
            julianday(coalesce(last_interaction_at, '')) DESC
        LIMIT ?
    """
    params.append(limit)

    with get_conn(settings.database_url) as conn:
        try:
            daysmart_ftyc_refresh = _refresh_recent_daysmart_ftyc_team_leads(conn, settings.database_url)
            _clear_kpi_cache_if_ftyc_changed(daysmart_ftyc_refresh)
        except Exception as exc:
            daysmart_ftyc_refresh = {
                "source": "daysmart-ftyc-team-leads",
                "leads_upserted": 0,
                "error": str(exc)[:240],
            }
        rows = conn.execute(sql, params).fetchall()
        prepared_rows: list[tuple[dict[str, Any], Any, list[dict[str, Any] | None]]] = []
        trial_registration_customer_ids: set[int] = set()

        for row in rows:
            item = dict(row)
            if _contains_any_token(item.get("trial_class_name"), ("beach lions",)):
                continue
            if _contains_any_token(item.get("metadata_json"), ("beach lions",)):
                continue
            source_ref = str(item.get("source_ref") or "").strip()
            item["conversation_id"] = int(source_ref) if source_ref.isdigit() else None
            item["conversation_url"] = _lead_source_url(item)
            item["phone_url"], item["phone_url_kind"] = _phone_click_url(conn, item)
            item["trial_class_when_display"] = _normalize_trial_class_when(item.get("trial_class_when"))
            parent_customer, child_customers = _find_daysmart_matches(conn, item)

            if parent_customer is None and item.get("source_system") not in {"gmail_eventbrite", "daysmart_ftyc"}:
                continue

            if parent_customer is not None:
                item["parent_daysmart_customer_id"] = parent_customer["customer_id"]
                item["parent_daysmart_url"] = _daysmart_account_url(int(parent_customer["customer_id"]))
            else:
                item["parent_daysmart_customer_id"] = None
                item["parent_daysmart_url"] = None

            eventbrite_row_children = _eventbrite_child_rows(item, child_customers)
            if eventbrite_row_children is not None:
                row_children = eventbrite_row_children
            elif child_customers:
                row_children: list[dict[str, Any] | None] = child_customers
            else:
                row_children = [None]
            prepared_rows.append((item, parent_customer, row_children))
            for child_customer in child_customers:
                try:
                    child_customer_id = int(child_customer["customer_id"])
                except (TypeError, ValueError):
                    continue
                latest_ftyc = conn.execute(
                    """
                    SELECT event_start, registration_created_at
                    FROM daysmart_ftyc_trial_registrations
                    WHERE customer_id = ?
                    ORDER BY coalesce(event_start, registration_created_at, '') DESC, registration_id DESC
                    LIMIT 1
                    """,
                    (child_customer_id,),
                ).fetchone()
                should_refresh_trials = latest_ftyc is None
                if latest_ftyc is not None:
                    latest_ftyc_at = _as_dt(
                        latest_ftyc["event_start"] or latest_ftyc["registration_created_at"]
                    )
                    # A rescheduled no-show usually means the child already has an
                    # older FTYC row. Recheck recent children only after that cached
                    # trial date has passed so normal page loads stay quick.
                    now_utc = dt.datetime.now(dt.timezone.utc)
                    should_refresh_trials = (
                        latest_ftyc_at is not None
                        and latest_ftyc_at < now_utc
                        and latest_ftyc_at >= now_utc - dt.timedelta(days=21)
                    )
                if (
                    len(trial_registration_customer_ids) < 10
                    and child_customer_id in recent_daysmart_customer_ids
                    and should_refresh_trials
                ):
                    trial_registration_customer_ids.add(child_customer_id)

        try:
            trial_registration_refresh = _refresh_daysmart_trial_registrations_for_customers(
                trial_registration_customer_ids,
                page_size=20,
            )
        except Exception as exc:
            trial_registration_refresh = {"error": str(exc)[:240], "customers_checked": 0}

        prelim_items: list[tuple[dict[str, Any], dict[str, Any] | None, int | None]] = []
        trial_days: set[str] = set()
        for item, parent_customer, row_children in prepared_rows:
            for child_customer in row_children:
                row_item = dict(item)
                memberships: list[str] = []

                if child_customer is not None:
                    eventbrite_placeholder_name = child_customer.get("_eventbrite_child_name") if isinstance(child_customer, dict) else None
                    if eventbrite_placeholder_name and "customer_id" not in child_customer:
                        row_item["lead_key"] = f"{item['lead_key']}:child:eventbrite:{_normalize_name(eventbrite_placeholder_name) or 'unknown'}"
                        row_item["child_name"] = eventbrite_placeholder_name
                        row_item["child_daysmart_customer_id"] = None
                        row_item["child_daysmart_url"] = None
                    else:
                        row_item["lead_key"] = f"{item['lead_key']}:child:{child_customer['customer_id']}"
                        row_item["child_name"] = child_customer.get("full_name")
                        row_item["child_daysmart_customer_id"] = child_customer["customer_id"]
                        row_item["child_daysmart_url"] = _daysmart_account_url(int(child_customer["customer_id"]))
                else:
                    row_item["child_name"] = None
                    row_item["child_daysmart_customer_id"] = None
                    row_item["child_daysmart_url"] = None
                row_item["parent_name"] = (
                    parent_customer.get("full_name")
                    if parent_customer is not None
                    else row_item.get("contact_name")
                ) or row_item.get("contact_name")

                related_customer_ids = _related_daysmart_customer_ids(parent_customer, child_customer)
                child_customer_id_value = (
                    int(child_customer["customer_id"])
                    if child_customer is not None and isinstance(child_customer, dict) and child_customer.get("customer_id") is not None
                    else None
                )
                membership_customer_ids = (
                    [child_customer_id_value]
                    if child_customer_id_value is not None
                    else related_customer_ids
                )
                lead_started_at = _as_dt(
                    row_item.get("conversation_first_message_at")
                    or row_item.get("conversation_started_at")
                    or row_item.get("last_interaction_at")
                    or row_item.get("conversation_last_message_at")
                )
                if (
                    child_customer_id_value is not None
                    and child_customer_id_value in recent_daysmart_customer_ids
                ):
                    try:
                        memberships = _live_customer_memberships(
                            child_customer_id_value,
                            min_created_at=lead_started_at,
                        )
                    except Exception:
                        memberships = []
                if not memberships and membership_customer_ids:
                    memberships = _daysmart_memberships(
                        conn,
                        membership_customer_ids,
                        min_created_at=lead_started_at,
                    )
                class_min_started_at = (
                    None
                    if row_item.get("source_system") == "daysmart_ftyc"
                    else lead_started_at
                )
                first_class_registration = _daysmart_first_class_registration(
                    conn,
                    related_customer_ids,
                    min_started_at=class_min_started_at,
                )
                first_class_name = (
                    first_class_registration.get("event_name_clean")
                    if first_class_registration is not None
                    else None
                )
                first_class_date = (
                    first_class_registration.get("event_start_display")
                    if first_class_registration is not None
                    else None
                )
                if (
                    not first_class_name
                    and _daysmart_has_excluded_registration(conn, related_customer_ids)
                ):
                    continue
                eventbrite_class_name = _clean_trial_class_name(row_item.get("trial_class_name"))
                eventbrite_class_date = row_item.get("trial_class_when_display")
                row_item["free_trial_class_name"] = first_class_name or eventbrite_class_name
                row_item["free_trial_class_date"] = first_class_date or eventbrite_class_date
                if row_item["free_trial_class_name"] and row_item["free_trial_class_date"]:
                    row_item["free_trial_class_display"] = f"{row_item['free_trial_class_name']} - {row_item['free_trial_class_date']}"
                else:
                    row_item["free_trial_class_display"] = row_item["free_trial_class_name"] or row_item["free_trial_class_date"] or ""

                row_item["memberships"] = memberships
                row_item["has_membership"] = bool(memberships)
                row_item["memberships_display"] = ", ".join(memberships) if memberships else "--"
                if first_class_registration is not None:
                    trial_dt = _trial_class_at(
                        first_class_registration.get("event_start")
                        or first_class_registration.get("created_at")
                    )
                    if trial_dt is not None:
                        trial_days.add(trial_dt.astimezone(YOUTH_KPI_LOCAL_TZ).date().isoformat())
                elif row_item.get("source_system") == "gmail_eventbrite":
                    eventbrite_trial_dt = _trial_class_at(row_item.get("trial_class_when"))
                    if eventbrite_trial_dt is not None:
                        trial_days.add(eventbrite_trial_dt.astimezone(YOUTH_KPI_LOCAL_TZ).date().isoformat())
                prelim_items.append(
                    (
                        row_item,
                        first_class_registration,
                        child_customer_id_value,
                    )
                )

        roster_lookup, _roster_source = _load_roster_checkins()
        if refresh:
            api_checkin_lookup, _api_source = _load_daysmart_api_checkins()
        else:
            api_checkin_lookup, _api_source = {}, {"source": "skipped-page-load", "entries": 0}
        attendance_fallback = _load_attendance_fallback(conn)
        if refresh or attendance_refresh:
            location_trial_days = trial_days
            if attendance_refresh and not refresh:
                today_local = dt.datetime.now(dt.timezone.utc).astimezone(YOUTH_KPI_LOCAL_TZ).date()
                earliest_local = today_local - dt.timedelta(days=3)
                location_trial_days = {
                    day
                    for day in trial_days
                    if earliest_local <= dt.date.fromisoformat(day) <= today_local
                }
            location_trial_checkins, _location_source = _load_location_report_checkins(location_trial_days)
        else:
            location_trial_checkins, _location_source = {}, {"source": "skipped-page-load", "entries": 0, "days": 0}
        location_customer_checkins, _cached_location_source = _load_cached_location_report_checkins_all()
        now_local = dt.datetime.now(dt.timezone.utc).astimezone(YOUTH_KPI_LOCAL_TZ)

        items: list[dict[str, Any]] = []
        for row_item, first_class_registration, child_customer_id in prelim_items:
            row_item["free_trial_class_status"] = ""
            if (
                first_class_registration is not None
                and child_customer_id is not None
                and row_item.get("free_trial_class_display")
            ):
                trial_dt = _trial_class_at(
                    first_class_registration.get("event_start")
                    or first_class_registration.get("created_at")
                )
                checked_in = False
                if trial_dt is not None:
                    child_key = f"daysmart:child:{child_customer_id}"
                    checked_in, _checkin_timestamp = _registration_checked_in_from_cached_sources(
                        first_class_registration,
                        child_customer_id,
                        child_key,
                        roster_lookup,
                        api_checkin_lookup=api_checkin_lookup,
                        attendance_fallback=attendance_fallback,
                    )
                    trial_day = trial_dt.astimezone(YOUTH_KPI_LOCAL_TZ).date()
                    trial_day_key = trial_day.isoformat()
                    if not checked_in and location_trial_checkins.get((trial_day_key, str(child_customer_id))):
                        checked_in = True
                    if not checked_in:
                        for checkin_dt in location_customer_checkins.get(str(child_customer_id), []):
                            if checkin_dt.astimezone(YOUTH_KPI_LOCAL_TZ).date() == trial_day:
                                checked_in = True
                                break
                    attendance_override_status = _attendance_override_status(
                        customer_id=child_customer_id,
                        registration=first_class_registration,
                    )
                    if attendance_override_status in {"no_show", "not_attended", "ignore_checkin"}:
                        checked_in = False
                    if checked_in:
                        row_item["free_trial_class_status"] = "checked_in"
                    elif _trial_class_is_missed(trial_dt, now_local):
                        row_item["free_trial_class_status"] = "missed"
                    else:
                        row_item["free_trial_class_status"] = "upcoming"
            elif (
                row_item.get("source_system") == "gmail_eventbrite"
                and row_item.get("free_trial_class_display")
            ):
                eventbrite_trial_dt = _trial_class_at(row_item.get("trial_class_when"))
                checked_in = False
                if eventbrite_trial_dt is not None and child_customer_id is not None:
                    trial_day = eventbrite_trial_dt.astimezone(YOUTH_KPI_LOCAL_TZ).date()
                    trial_day_key = trial_day.isoformat()
                    if location_trial_checkins.get((trial_day_key, str(child_customer_id))):
                        checked_in = True
                    if not checked_in:
                        for checkin_dt in location_customer_checkins.get(str(child_customer_id), []):
                            if checkin_dt.astimezone(YOUTH_KPI_LOCAL_TZ).date() == trial_day:
                                checked_in = True
                                break
                    if not checked_in:
                        for item in api_checkin_lookup.get(str(child_customer_id), []):
                            item_dt = item.get("datetime")
                            if isinstance(item_dt, dt.datetime) and item_dt.astimezone(YOUTH_KPI_LOCAL_TZ).date() == trial_day:
                                checked_in = True
                                break
                if checked_in:
                    row_item["free_trial_class_status"] = "checked_in"
                elif _trial_class_is_missed(eventbrite_trial_dt, now_local):
                    row_item["free_trial_class_status"] = "missed"
                else:
                    row_item["free_trial_class_status"] = "pending-registration"
            items.append(row_item)
    items = _dedupe_trial_lead_items(items)
    _sort_trial_lead_items_default(items)
    note_keys = [_trial_lead_note_key(item) for item in items]
    notes_by_key: dict[str, str] = {}
    if note_keys:
        placeholders = ",".join("?" for _ in note_keys)
        with get_conn(settings.database_url) as note_conn:
            note_rows = note_conn.execute(
                f"""
                SELECT note_key, note_text
                FROM youth_trial_lead_notes
                WHERE note_key IN ({placeholders})
                """,
                note_keys,
            ).fetchall()
        notes_by_key = {str(row["note_key"]): str(row["note_text"] or "") for row in note_rows}
    for item, note_key in zip(items, note_keys):
        item["local_note_key"] = note_key
        item["local_note"] = notes_by_key.get(note_key, "")
    return {
        "items": items,
        "salesmessage_refresh": salesmessage_refresh,
        "daysmart_refresh": daysmart_refresh,
        "daysmart_ftyc_refresh": daysmart_ftyc_refresh,
        "trial_registration_refresh": trial_registration_refresh,
    }


@app.get("/dashboard/youth-kpis")
def dashboard_youth_kpis(
    days: int = 7,
    window: str | None = None,
    include_attendance: bool = True,
    refresh: bool = False,
) -> dict[str, Any]:
    if snapshot_mode:
        payload = _load_snapshot_json(_snapshot_dashboard_path(days, window))
        payload["snapshot_mode"] = True
        return payload
    payload = build_youth_kpi_dashboard(
        settings.database_url,
        youth_inbox_id=settings.youth_inbox_id,
        days=days,
        window=window,
        include_attendance=include_attendance,
        force_refresh=refresh,
    )
    items = payload.get("items") or []
    _attach_trial_lead_notes(items)
    _attach_trial_lead_status_overrides(items)
    return payload


@app.get("/dashboard/youth-kpis/timeseries")
def dashboard_youth_kpi_timeseries(
    days: int = 7,
    window: str | None = None,
    granularity: str = "month",
    refresh: bool = False,
) -> dict[str, Any]:
    if snapshot_mode:
        payload = _load_snapshot_json(_snapshot_timeseries_path(days, window, granularity))
        payload["snapshot_mode"] = True
        return payload
    return build_youth_kpi_timeseries(
        settings.database_url,
        youth_inbox_id=settings.youth_inbox_id,
        days=days,
        window=window,
        granularity=granularity,
        force_refresh=refresh,
    )


@app.get("/dashboard/youth-kpis/email-preview")
def dashboard_youth_kpi_email_preview(days: int = 7, window: str | None = None) -> dict[str, Any]:
    preview = build_youth_kpi_email_preview(
        settings.database_url,
        youth_inbox_id=settings.youth_inbox_id,
        days=days,
        window=window,
    )
    return {"ok": True, **preview}


@app.patch("/dashboard/trial-leads/{lead_key}")
def update_trial_lead(lead_key: str, req: TrialLeadUpdateRequest) -> dict[str, Any]:
    updates: list[str] = []
    params: list[Any] = []
    if req.trial_status is not None:
        updates.append("trial_status = ?")
        params.append(req.trial_status)
    if req.account_created is not None:
        updates.append("account_created = ?")
        params.append(1 if req.account_created else 0)
    if req.added_to_class is not None:
        updates.append("added_to_class = ?")
        params.append(1 if req.added_to_class else 0)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields provided for update")

    updates.append("updated_at = datetime('now')")
    with get_conn(settings.database_url) as conn:
        exists = conn.execute(
            "SELECT 1 FROM youth_trial_leads WHERE lead_key = ?",
            (lead_key,),
        ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="Lead not found")
        conn.execute(
            f"UPDATE youth_trial_leads SET {', '.join(updates)} WHERE lead_key = ?",
            (*params, lead_key),
        )
    return {"ok": True, "lead_key": lead_key}


@app.post("/dashboard/trial-lead-notes")
def update_trial_lead_note(req: TrialLeadNoteRequest) -> dict[str, Any]:
    note_key = req.note_key.strip()
    if not note_key:
        raise HTTPException(status_code=400, detail="Missing note key")
    note_text = req.note_text[:2000]
    with get_conn(settings.database_url) as conn:
        if note_text.strip():
            conn.execute(
                """
                INSERT INTO youth_trial_lead_notes (note_key, note_text, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(note_key) DO UPDATE SET
                    note_text = excluded.note_text,
                    updated_at = excluded.updated_at
                """,
                (note_key, note_text),
            )
        else:
            conn.execute(
                "DELETE FROM youth_trial_lead_notes WHERE note_key = ?",
                (note_key,),
            )
    return {"ok": True, "note_key": note_key}


@app.post("/dashboard/trial-lead-status-override")
def update_trial_lead_status_override(req: TrialLeadStatusOverrideRequest) -> dict[str, Any]:
    override_key = req.override_key.strip()
    if not override_key:
        raise HTTPException(status_code=400, detail="Missing override key")
    status_override = req.status_override.strip().lower()
    allowed = {"", "open", "closed_lost"}
    if status_override not in allowed:
        raise HTTPException(status_code=400, detail="Unsupported status override")
    with get_conn(settings.database_url) as conn:
        if status_override:
            conn.execute(
                """
                INSERT INTO youth_trial_lead_status_overrides (override_key, status_override, updated_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(override_key) DO UPDATE SET
                    status_override = excluded.status_override,
                    updated_at = excluded.updated_at
                """,
                (override_key, status_override),
            )
        else:
            conn.execute(
                "DELETE FROM youth_trial_lead_status_overrides WHERE override_key = ?",
                (override_key,),
            )
    return {"ok": True, "override_key": override_key, "status_override": status_override}


@app.get("/conversations")
def list_conversations(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    with get_conn(settings.database_url) as conn:
        rows = conn.execute(
            """
            SELECT id, contact_name, contact_number, started_at, closed_at, last_message_at
            FROM conversations
            ORDER BY coalesce(last_message_at, '') DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/conversations/{conversation_id}/messages")
def list_messages(conversation_id: int, limit: int = 200) -> dict[str, Any]:
    with get_conn(settings.database_url) as conn:
        rows = conn.execute(
            """
            SELECT id, conversation_id, body, status, message_type, source, created_at
            FROM messages
            WHERE conversation_id = ?
            ORDER BY coalesce(created_at, '') ASC
            LIMIT ?
            """,
            (conversation_id, limit),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@app.get("/search")
def search(query: str, conversation_id: int | None = None, limit: int = 25) -> dict[str, Any]:
    try:
        items = search_messages(
            db_path=settings.database_url,
            query=query,
            limit=limit,
            conversation_id=conversation_id,
        )
    except Exception as exc:  # FTS parser can throw on invalid syntax.
        raise HTTPException(status_code=400, detail=f"Search failed: {exc}") from exc
    return {"items": items}


@app.post("/ask")
def ask(req: AskRequest) -> dict[str, Any]:
    query = req.search_query or req.question
    context = []
    try:
        context = search_messages(
            db_path=settings.database_url,
            query=query,
            limit=req.max_context_messages,
            conversation_id=req.conversation_id,
        )
    except Exception:
        context = []

    if not context:
        context = get_recent_messages(
            db_path=settings.database_url,
            limit=req.max_context_messages,
            conversation_id=req.conversation_id,
        )

    if not settings.openai_api_key:
        result = answer_locally(req.question, context)
        result["context_size"] = len(context)
        return result

    try:
        result = answer_with_llm(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            question=req.question,
            context_rows=context,
        )
    except Exception as exc:
        result = answer_locally(req.question, context)
        result.setdefault("uncertainties", []).append(f"LLM unavailable: {exc}")
    result["context_size"] = len(context)
    return result
