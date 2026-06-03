from __future__ import annotations

import datetime as dt
import difflib
import json
import os
import re
import tempfile
import threading
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .config import get_settings
from .db import get_conn
from .daysmart import DaysmartApiError, DaysmartClient
from .unified import (
    FTYC_DISCOUNT_ID,
    FTYC_REGISTRATION_MATCH_WINDOW_SECONDS,
    _as_decimal as _daysmart_as_decimal,
    _as_dt as _daysmart_as_dt,
    _as_int as _daysmart_as_int,
    _is_youth_trial_class_name,
    _upsert_daysmart_class_registration,
    _upsert_ftyc_trial_registration,
)

LOCAL_TZ = ZoneInfo("America/New_York")
YOUTH_CLASS_TOKENS = ("seals", "cubs")
EXCLUDED_CLASS_TOKENS = ("beach lions",)
ALLOWED_MEMBERSHIP_TOKENS = ("seals", "cubs")
EXCLUDED_MEMBERSHIP_TOKENS = ("beach lions", "staff")
CHECKIN_MATCH_WINDOW_HOURS = 6
TRIAL_MISSED_GRACE_PERIOD = dt.timedelta(hours=1)
ADMIN_LOGIN_URL = "https://apps.daysmartrecreation.com/dash/admin/index.php?Action=Auth/login"
ADMIN_LOGIN_VALIDATE_URL = "https://apps.daysmartrecreation.com/dash/admin/index.php?Action=Auth/validateLogin.json&extension=json"
ADMIN_LOCATION_CHECKIN_REPORT_URL = "https://apps.daysmartrecreation.com/dash/admin/index.php?Action=Report/locationCheckIn&company={company}"
ADMIN_CUSTOMER_CHECKINS_URL = "https://apps.daysmartrecreation.com/dash/admin/index.php?Action=AdminCheckin/getCustomerCheckins&customerID={customer_id}&company={company}"
ADMIN_CUSTOMER_PROFILE_URL = "https://apps.daysmartrecreation.com/dash/admin/index.php?Action=CustomerInfo&CustomerID={customer_id}&company={company}"
REPORT_TABLE_RE = re.compile(r'<table[^>]+id="results-table"[^>]*>.*?<tbody>(?P<tbody>.*?)</tbody>', re.S | re.I)
REPORT_ROW_RE = re.compile(r"<tr[^>]*>(?P<row>.*?)</tr>", re.S | re.I)
REPORT_CELL_RE = re.compile(r"<td[^>]*>(?P<cell>.*?)</td>", re.S | re.I)
LOCATION_REPORT_CACHE_PATH = Path(tempfile.gettempdir()) / "qbk_youth_location_report_cache.json"
CUSTOMER_CHECKINS_CACHE_PATH = Path(tempfile.gettempdir()) / "qbk_youth_customer_checkins_cache.json"
RECENT_LOCATION_CACHE_TTL_SECONDS = 15 * 60
HISTORICAL_LOCATION_CACHE_TTL_SECONDS = 30 * 24 * 60 * 60
YOUTH_KPI_CACHE_PATH = Path(tempfile.gettempdir()) / "qbk_youth_kpi_cache.json"
YOUTH_KPI_CACHE_VERSION = 27
ATTENDANCE_OVERRIDES_PATH = Path(__file__).with_name("youth_attendance_overrides.json")
_DASHBOARD_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}
_DASHBOARD_CACHE_LOCK = threading.Lock()
_ATTENDANCE_OVERRIDES_CACHE: tuple[float, list[dict[str, Any]]] | None = None
_LIVE_GMAIL_EVENTBRITE_REFRESH_CACHE: tuple[float, dict[str, Any]] | None = None


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _safe_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _attendance_overrides() -> list[dict[str, Any]]:
    global _ATTENDANCE_OVERRIDES_CACHE
    try:
        mtime = ATTENDANCE_OVERRIDES_PATH.stat().st_mtime
    except FileNotFoundError:
        _ATTENDANCE_OVERRIDES_CACHE = None
        return []

    if _ATTENDANCE_OVERRIDES_CACHE and _ATTENDANCE_OVERRIDES_CACHE[0] == mtime:
        return _ATTENDANCE_OVERRIDES_CACHE[1]

    payload = _json_loads(ATTENDANCE_OVERRIDES_PATH.read_text(), {})
    rows = payload.get("overrides") if isinstance(payload, dict) else []
    overrides = [row for row in rows if isinstance(row, dict)]
    _ATTENDANCE_OVERRIDES_CACHE = (mtime, overrides)
    return overrides


def _refresh_recent_live_gmail_eventbrite_leads() -> dict[str, Any]:
    global _LIVE_GMAIL_EVENTBRITE_REFRESH_CACHE
    now = time.time()
    if _LIVE_GMAIL_EVENTBRITE_REFRESH_CACHE is not None:
        cached_at, cached_result = _LIVE_GMAIL_EVENTBRITE_REFRESH_CACHE
        if now - cached_at < 10 * 60:
            result = dict(cached_result)
            result["cached"] = True
            return result

    from .import_eventbrite_gmail_orders import import_live_eventbrite_messages

    result = dict(import_live_eventbrite_messages(max_results=100))
    _LIVE_GMAIL_EVENTBRITE_REFRESH_CACHE = (now, result)
    return result


def _attendance_override_status(
    *,
    customer_id: int | None,
    registration: dict[str, Any] | None,
) -> str | None:
    if customer_id is None:
        return None

    registration_id = _safe_int((registration or {}).get("registration_id"))
    event_id = _safe_int((registration or {}).get("team_or_event_id") or (registration or {}).get("event_id"))
    event_start = _parse_ts(
        (registration or {}).get("event_start") or (registration or {}).get("created_at"),
        naive_tz=LOCAL_TZ,
    )
    event_day = event_start.astimezone(LOCAL_TZ).date().isoformat() if event_start else None

    for override in _attendance_overrides():
        if _safe_int(override.get("customer_id")) != customer_id:
            continue
        override_registration_id = _safe_int(override.get("registration_id"))
        if override_registration_id is not None and registration_id != override_registration_id:
            continue
        override_event_id = _safe_int(override.get("event_id") or override.get("team_or_event_id"))
        if override_event_id is not None and event_id != override_event_id:
            continue
        override_event_start = _parse_ts(str(override.get("event_start") or ""), naive_tz=LOCAL_TZ)
        override_event_day = (
            override_event_start.astimezone(LOCAL_TZ).date().isoformat()
            if override_event_start
            else None
        )
        if override_event_day is not None and event_day != override_event_day:
            continue
        status = str(override.get("status") or "").strip().lower()
        return status or None
    return None


def _clean_text(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    text = value.replace("\u2019", "'")
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _contains_any_token(value: str | None, tokens: tuple[str, ...]) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return any(token in lowered for token in tokens)


def _is_excluded_class_name(value: str | None) -> bool:
    return _contains_any_token(value, EXCLUDED_CLASS_TOKENS)


def _is_allowed_membership_name(value: str | None) -> bool:
    if not _contains_any_token(value, ALLOWED_MEMBERSHIP_TOKENS):
        return False
    return not _contains_any_token(value, EXCLUDED_MEMBERSHIP_TOKENS)


def _membership_display_label(product_name: str | None) -> str:
    base = (product_name or "").strip() or "Unnamed membership"
    return base


def _membership_is_currently_active(record: dict[str, Any]) -> bool:
    today = dt.datetime.now(LOCAL_TZ).date()
    created_at = _parse_ts(record.get("created_at"), naive_tz=LOCAL_TZ)
    if created_at is not None and created_at.astimezone(LOCAL_TZ).date() > today:
        return False
    expires_at = _parse_ts(record.get("expires_at"), naive_tz=LOCAL_TZ)
    if expires_at is None:
        return True
    return expires_at.astimezone(LOCAL_TZ).date() >= today


def _normalize_phone(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 10:
        return None
    return digits[-10:]


def _normalize_email(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower()
    return cleaned or None


def _normalize_name(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    ascii_like = "".join(
        ch for ch in unicodedata.normalize("NFKD", value) if not unicodedata.combining(ch)
    )
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in ascii_like)
    cleaned = " ".join(cleaned.split())
    return cleaned or None


def _name_match_score(left: str | None, right: str | None) -> float:
    left_norm = _normalize_name(left)
    right_norm = _normalize_name(right)
    if not left_norm or not right_norm:
        return 0.0
    if left_norm == right_norm:
        return 1.0
    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    token_score = (
        len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        if left_tokens and right_tokens
        else 0.0
    )
    seq_score = difflib.SequenceMatcher(None, left_norm, right_norm).ratio()
    return max(token_score, seq_score)


def _resolve_legacy_csv_name_roles(
    imported_name: str | None,
    parent_customer: dict[str, Any] | None,
    child_customers: list[dict[str, Any]],
) -> tuple[str, str | None]:
    imported = (imported_name or "").strip() or None
    parent_name = ((parent_customer or {}).get("full_name") or "").strip() or None
    minor_cutoff = dt.date.today().replace(year=dt.date.today().year - 18)
    parent_birthdate = _customer_birthdate(parent_customer) if parent_customer is not None else None
    parent_is_minor = bool(parent_birthdate is not None and parent_birthdate > minor_cutoff)
    family_people = ([parent_customer] if parent_customer is not None else []) + list(child_customers or [])
    adult_names: list[str] = []
    minor_names: list[str] = []
    for person in family_people:
        if not isinstance(person, dict):
            continue
        full_name = ((person.get("full_name") or "").strip() or None)
        birthdate = _customer_birthdate(person)
        if not full_name or birthdate is None:
            continue
        if birthdate > minor_cutoff:
            minor_names.append(full_name)
        else:
            adult_names.append(full_name)
    if imported is None:
        return parent_name or "--", None

    # When the matched DaySmart family resolves cleanly to one adult and one minor,
    # trust that structure over the imported CSV name, which is mixed parent/child data.
    if len(adult_names) == 1 and len(minor_names) == 1:
        return adult_names[0], minor_names[0]

    imported_norm = _normalize_name(imported)
    parent_norm = _normalize_name(parent_name)
    if imported_norm and parent_norm and imported_norm == parent_norm:
        if parent_is_minor:
            return "--", parent_name or imported
        return parent_name or imported, None

    exact_child = next(
        (
            child
            for child in child_customers
            if _normalize_name(child.get("full_name")) == imported_norm
        ),
        None,
    )
    if exact_child is not None:
        return parent_name or "--", (exact_child.get("full_name") or imported)

    parent_score = _name_match_score(imported, parent_name)
    best_child_name = None
    best_child_score = 0.0
    for child in child_customers:
        child_name = child.get("full_name")
        score = _name_match_score(imported, child_name)
        if score > best_child_score:
            best_child_score = score
            best_child_name = child_name

    if parent_is_minor:
        return "--", parent_name or imported

    if parent_score >= max(0.72, best_child_score):
        return parent_name or imported, None

    if best_child_name is not None:
        return parent_name or "--", best_child_name

    if parent_name:
        return parent_name, imported
    return imported, None


def _eventbrite_child_names(metadata_json: str | None) -> list[str]:
    metadata = _json_loads(metadata_json, {})
    if not isinstance(metadata, dict):
        return []
    eventbrite = metadata.get("eventbrite_order")
    if not isinstance(eventbrite, dict):
        return []
    children = eventbrite.get("children")
    if not isinstance(children, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for child in children:
        if not isinstance(child, dict):
            continue
        name = str(child.get("child_name") or "").strip()
        normalized = _normalize_name(name)
        if name and normalized not in seen:
            names.append(name)
            if normalized:
                seen.add(normalized)
    return names


def _eventbrite_child_identities(metadata_json: str | None) -> list[dict[str, str]]:
    metadata = _json_loads(metadata_json, {})
    if not isinstance(metadata, dict):
        return []
    eventbrite = metadata.get("eventbrite_order")
    if not isinstance(eventbrite, dict):
        return []
    children = eventbrite.get("children")
    if not isinstance(children, list):
        return []

    identities: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    for child in children:
        if not isinstance(child, dict):
            continue
        name = str(child.get("child_name") or "").strip()
        email = str(child.get("email") or "").strip()
        phone = str(child.get("phone") or "").strip()
        normalized = (
            _normalize_name(name) or "",
            _normalize_email(email) or "",
            _normalize_phone(phone) or "",
        )
        if not any(normalized) or normalized in seen:
            continue
        seen.add(normalized)
        identities.append({"name": name, "email": email, "phone": phone})
    return identities


def _parse_ts(
    value: str | None,
    *,
    naive_tz: dt.tzinfo = dt.timezone.utc,
) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip().replace("Z", "+00:00")
    for candidate in (cleaned, cleaned.replace(" ", "T")):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=naive_tz).astimezone(dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        except ValueError:
            continue
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(cleaned[:19], fmt)
            return parsed.replace(tzinfo=naive_tz).astimezone(dt.timezone.utc)
        except ValueError:
            continue
    return None


def _format_local(value: dt.datetime | None, *, with_time: bool = True) -> str:
    if value is None:
        return "--"
    local_value = value.astimezone(LOCAL_TZ)
    if with_time:
        return local_value.strftime("%a, %-m/%-d/%y %-I:%M %p")
    return local_value.strftime("%a, %-m/%-d/%y")


def _format_percent(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0%"
    return f"{round((numerator / denominator) * 100):.0f}%"


def _effective_lead_timestamp(*values: str | None) -> dt.datetime | None:
    for value in values:
        parsed = _parse_ts(value, naive_tz=LOCAL_TZ)
        if parsed is not None:
            return parsed
    return None


def _salesmessage_conversation_for_phone(
    conn: Any,
    *,
    phone: str | None,
    youth_inbox_id: int,
    before_at: str | None = None,
) -> dict[str, Any] | None:
    normalized_phone = _normalize_phone(phone)
    if normalized_phone is None:
        return None

    before_ts = _parse_ts(before_at, naive_tz=LOCAL_TZ)
    rows = [
        dict(row)
        for row in conn.execute(
            """
            WITH first_message_times AS (
                SELECT
                    conversation_id,
                    MIN(coalesce(created_at, sent_at, received_at, '')) AS first_message_at
                FROM messages
                GROUP BY conversation_id
            )
            SELECT
                c.id,
                c.started_at,
                c.closed_at,
                c.last_message_at,
                fmt.first_message_at,
                coalesce(
                    nullif(fmt.first_message_at, ''),
                    nullif(c.started_at, ''),
                    nullif(c.last_message_at, '')
                ) AS effective_lead_at
            FROM conversations c
            LEFT JOIN first_message_times fmt ON fmt.conversation_id = c.id
            WHERE c.inbox_id = ?
              AND substr(
                    replace(replace(replace(replace(replace(replace(c.contact_number, '+', ''), ' ', ''), '-', ''), '(', ''), ')', ''), '.', ''),
                    -10
                  ) = ?
            ORDER BY coalesce(
                nullif(fmt.first_message_at, ''),
                nullif(c.started_at, ''),
                nullif(c.last_message_at, ''),
                ''
            ) DESC
            """,
            (youth_inbox_id, normalized_phone),
        ).fetchall()
    ]
    if not rows:
        return None

    if before_ts is not None:
        for row in rows:
            effective = _effective_lead_timestamp(
                row.get("first_message_at"),
                row.get("started_at"),
                row.get("last_message_at"),
            )
            if effective is not None and effective <= before_ts:
                return row
    return rows[0]


def _attach_salesmessage_conversation_to_ftyc_lead(
    conn: Any,
    lead: dict[str, Any],
    *,
    youth_inbox_id: int,
) -> None:
    if str(lead.get("source_system") or "") != "daysmart_ftyc":
        return

    conversation = _salesmessage_conversation_for_phone(
        conn,
        phone=lead.get("contact_phone"),
        youth_inbox_id=youth_inbox_id,
        before_at=lead.get("trial_class_when") or lead.get("last_interaction_at"),
    )
    if conversation is None:
        return

    lead["salesmessage_conversation_id"] = conversation.get("id")
    lead["conversation_started_at"] = conversation.get("started_at")
    lead["conversation_closed_at"] = conversation.get("closed_at")
    lead["conversation_last_message_at"] = conversation.get("last_message_at")
    lead["conversation_first_message_at"] = conversation.get("first_message_at")


def _daysmart_client() -> DaysmartClient:
    settings = get_settings()
    return DaysmartClient(
        client_id=settings.daysmart_api_client_id,
        client_secret=settings.daysmart_api_secret,
        base_url=settings.daysmart_base_url,
    )


def _class_start_from_team_registration(
    team_name: str | None,
    reference_at: dt.datetime | None,
) -> str | None:
    if reference_at is None:
        return None
    local_date = reference_at.astimezone(LOCAL_TZ).date()
    name = (team_name or "").lower()
    if "cubs" in name:
        time_by_weekday = {
            1: dt.time(17, 0),  # Tuesday
            2: dt.time(16, 30),  # Wednesday
            5: dt.time(9, 0),  # Saturday
        }
    elif "seals" in name:
        time_by_weekday = {
            0: dt.time(16, 30),  # Monday
            1: dt.time(15, 30),  # Tuesday
            2: dt.time(16, 30),  # Wednesday
            5: dt.time(9, 0),  # Saturday
        }
    else:
        time_by_weekday = {}
    class_time = time_by_weekday.get(local_date.weekday(), reference_at.astimezone(LOCAL_TZ).time())
    return dt.datetime.combine(local_date, class_time, tzinfo=LOCAL_TZ).isoformat()


def _upsert_daysmart_ftyc_lead(
    conn: Any,
    *,
    customer_id: int,
    registration_id: int,
    event_name: str | None,
    event_start: str | None,
    registration_created_at: str | None,
    contact_name: str | None,
    contact_phone: str | None,
    metadata: dict[str, Any],
) -> None:
    lead_key = f"daysmart_ftyc:customer:{customer_id}"
    family_key = f"daysmart:family:ftyc:{customer_id}"
    started_at = registration_created_at or event_start
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO youth_trial_leads (
            lead_key, family_key, inbox_id, contact_name, contact_phone, trial_status,
            account_created, added_to_class, trial_class_name, trial_class_when,
            last_interaction_at, source_system, source_ref, metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'scheduled', 1, 1, ?, ?, ?, 'daysmart_ftyc', ?, ?, ?, ?)
        ON CONFLICT(lead_key) DO UPDATE SET
            contact_name=coalesce(excluded.contact_name, youth_trial_leads.contact_name),
            contact_phone=coalesce(excluded.contact_phone, youth_trial_leads.contact_phone),
            trial_class_name=excluded.trial_class_name,
            trial_class_when=excluded.trial_class_when,
            last_interaction_at=excluded.last_interaction_at,
            source_ref=excluded.source_ref,
            metadata_json=excluded.metadata_json,
            updated_at=excluded.updated_at
        """,
        (
            lead_key,
            family_key,
            0,
            contact_name,
            contact_phone,
            event_name,
            event_start,
            started_at,
            str(registration_id),
            json.dumps(metadata, ensure_ascii=True, separators=(",", ":")),
            now,
            now,
        ),
    )


def _refresh_recent_daysmart_ftyc_team_leads(
    conn: Any,
    db_path: str,
    *,
    page_size: int = 25,
    max_pages: int = 1,
) -> dict[str, Any]:
    client = _daysmart_client()
    invoice_rows: list[dict[str, Any]] = []
    errors = 0
    try:
        first_rows, last_page = client.list_invoice_items(
            page_number=1,
            page_size=page_size,
            filters={"discount_id": FTYC_DISCOUNT_ID},
            sort="-date",
        )
        for page in range(1, min(last_page, max_pages) + 1):
            rows = first_rows if page == 1 else client.list_invoice_items(
                page_number=page,
                page_size=page_size,
                filters={"discount_id": FTYC_DISCOUNT_ID},
                sort="-date",
            )[0]
            invoice_rows.extend(rows)
    except Exception:
        return {"source": "daysmart-ftyc-team-leads", "invoice_items": 0, "leads_upserted": 0, "errors": 1}

    team_cache: dict[int, str | None] = {}
    leads_upserted = 0
    registrations_upserted = 0
    ftyc_upserted = 0
    customer_cache: dict[int, dict[str, Any] | None] = {}
    seen_customers: set[int] = set()
    for invoice_item in invoice_rows:
        attrs = invoice_item.get("attributes") if isinstance(invoice_item.get("attributes"), dict) else {}
        if _daysmart_as_int(attrs.get("discount_id")) != FTYC_DISCOUNT_ID:
            continue
        if attrs.get("is_reversal") or attrs.get("reversal_item_id") or attrs.get("reversed_item_id"):
            continue
        price = _daysmart_as_decimal(attrs.get("price"))
        if price is None or price >= 0:
            continue
        customer_id = _daysmart_as_int(attrs.get("customer_id"))
        if customer_id is None:
            continue
        if customer_id in seen_customers:
            continue
        invoice_ts = _daysmart_as_dt(attrs.get("created_at")) or _daysmart_as_dt(attrs.get("date"))
        if invoice_ts is None:
            continue
        invoice_team_id = _daysmart_as_int(attrs.get("team_id"))

        event_match = _matching_ftyc_event_registration(
            client,
            customer_id=customer_id,
            invoice_ts=invoice_ts,
            invoice_team_id=invoice_team_id,
        )
        if event_match is not None:
            seen_customers.add(customer_id)
            _upsert_daysmart_class_registration(
                db_path,
                source_type="event_registration",
                row=event_match["row"],
                event_name=event_match.get("event_name"),
                event_start=event_match.get("event_start"),
                conn=conn,
            )
            registrations_upserted += 1
            registration = {
                "registration_id": int(event_match["registration_id"]),
                "customer_id": customer_id,
                "team_or_event_id": event_match.get("team_or_event_id"),
                "event_name": event_match.get("event_name"),
                "event_start": event_match.get("event_start"),
                "created_at": event_match.get("created_at"),
            }
            _upsert_ftyc_trial_registration(conn, registration=registration, invoice_item=invoice_item)
            customer = customer_cache.get(customer_id)
            if customer_id not in customer_cache:
                customer = conn.execute(
                    "SELECT * FROM daysmart_customers WHERE customer_id = ?",
                    (customer_id,),
                ).fetchone()
                customer_cache[customer_id] = dict(customer) if customer is not None else None
            customer_dict = customer_cache.get(customer_id)
            phone = None
            if customer_dict is not None:
                phone = (
                    customer_dict.get("phone_day")
                    or customer_dict.get("phone_mobile")
                    or customer_dict.get("phone_emergency")
                )
            _upsert_daysmart_ftyc_lead(
                conn,
                customer_id=customer_id,
                registration_id=int(event_match["registration_id"]),
                event_name=event_match.get("event_name"),
                event_start=event_match.get("event_start"),
                registration_created_at=event_match.get("created_at"),
                contact_name=None,
                contact_phone=phone,
                metadata={
                    "daysmart_ftyc": {
                        "customer_id": customer_id,
                        "registration_id": int(event_match["registration_id"]),
                        "invoice_item_id": _daysmart_as_int(invoice_item.get("id")),
                        "invoice_id": _daysmart_as_int(attrs.get("invoice_id")),
                        "source": "event_registration",
                    }
                },
            )
            leads_upserted += 1
            ftyc_upserted += 1
            continue
        try:
            registration_rows, _ = client.list_registrations(
                page_number=1,
                page_size=25,
                filters={"customer_id": customer_id},
            )
        except Exception:
            errors += 1
            continue
        best: dict[str, Any] | None = None
        best_event_ts: dt.datetime | None = None
        best_event_name: str | None = None
        best_event_start: str | None = None
        for registration in registration_rows:
            reg_attrs = registration.get("attributes") if isinstance(registration.get("attributes"), dict) else {}
            registration_id = _daysmart_as_int(registration.get("id"))
            team_id = _daysmart_as_int(reg_attrs.get("team_id"))
            registration_ts = _daysmart_as_dt(
                reg_attrs.get("create_date") or reg_attrs.get("created_at") or reg_attrs.get("updated_at")
            )
            if registration_id is None or team_id is None or registration_ts is None:
                continue
            delta = abs((registration_ts - invoice_ts).total_seconds())
            if delta > FTYC_REGISTRATION_MATCH_WINDOW_SECONDS:
                continue
            if team_id not in team_cache:
                try:
                    team_payload = client._get(f"/api/v1/teams/{team_id}")
                    team = team_payload.get("data") if isinstance(team_payload, dict) else None
                    team_attrs = (
                        team.get("attributes")
                        if isinstance(team, dict) and isinstance(team.get("attributes"), dict)
                        else {}
                    )
                    team_cache[team_id] = team_attrs.get("name") or team_attrs.get("desc")
                except Exception:
                    team_cache[team_id] = None
            event_name = team_cache.get(team_id)
            if not _is_youth_trial_class_name(event_name):
                continue
            event_start = _class_start_from_team_registration(event_name, invoice_ts)
            event_ts = _daysmart_as_dt(event_start) or registration_ts
            if best is None or event_ts > (best_event_ts or event_ts):
                best = registration
                best_event_ts = event_ts
                best_event_name = event_name
                best_event_start = event_start
        if best is None:
            continue
        seen_customers.add(customer_id)
        _upsert_daysmart_class_registration(
            db_path,
            source_type="registration",
            row=best,
            event_name=best_event_name,
            event_start=best_event_start,
            conn=conn,
        )
        registrations_upserted += 1
        registration_id = int(best["id"])
        registration = {
            "registration_id": registration_id,
            "customer_id": customer_id,
            "team_or_event_id": _daysmart_as_int((best.get("attributes") or {}).get("team_id")),
            "event_name": best_event_name,
            "event_start": best_event_start,
            "created_at": (best.get("attributes") or {}).get("create_date")
            or (best.get("attributes") or {}).get("created_at")
            or (best.get("attributes") or {}).get("updated_at"),
        }
        _upsert_ftyc_trial_registration(conn, registration=registration, invoice_item=invoice_item)
        ftyc_upserted += 1
        customer = customer_cache.get(customer_id)
        if customer_id not in customer_cache:
            customer = conn.execute(
                "SELECT * FROM daysmart_customers WHERE customer_id = ?",
                (customer_id,),
            ).fetchone()
            customer_cache[customer_id] = dict(customer) if customer is not None else None
        customer_dict = customer_cache.get(customer_id)
        phone = None
        if customer_dict is not None:
            phone = (
                customer_dict.get("phone_day")
                or customer_dict.get("phone_mobile")
                or customer_dict.get("phone_emergency")
            )
        _upsert_daysmart_ftyc_lead(
            conn,
            customer_id=customer_id,
            registration_id=registration_id,
            event_name=best_event_name,
            event_start=best_event_start,
            registration_created_at=registration["created_at"],
            contact_name=None,
            contact_phone=phone,
            metadata={
                "daysmart_ftyc": {
                    "customer_id": customer_id,
                    "registration_id": registration_id,
                    "invoice_item_id": _daysmart_as_int(invoice_item.get("id")),
                    "invoice_id": _daysmart_as_int(attrs.get("invoice_id")),
                    "source": "team_registration",
                }
            },
        )
        leads_upserted += 1
    return {
        "source": "daysmart-ftyc-team-leads",
        "invoice_items": len(invoice_rows),
        "registrations_upserted": registrations_upserted,
        "ftyc_upserted": ftyc_upserted,
        "leads_upserted": leads_upserted,
        "errors": errors,
    }


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


def _load_location_cache() -> dict[str, Any]:
    if not LOCATION_REPORT_CACHE_PATH.exists():
        return {}
    try:
        payload = json.loads(LOCATION_REPORT_CACHE_PATH.read_text())
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_location_cache(payload: dict[str, Any]) -> None:
    LOCATION_REPORT_CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=True))


def _load_customer_checkins_cache() -> dict[str, Any]:
    if not CUSTOMER_CHECKINS_CACHE_PATH.exists():
        return {}
    try:
        payload = json.loads(CUSTOMER_CHECKINS_CACHE_PATH.read_text())
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_customer_checkins_cache(payload: dict[str, Any]) -> None:
    CUSTOMER_CHECKINS_CACHE_PATH.write_text(json.dumps(payload, ensure_ascii=True))


def _cache_path_mtime(path: Path | None) -> float:
    if path is None or not path.exists():
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _cache_key_string(cache_key: tuple[Any, ...]) -> str:
    return json.dumps(cache_key, separators=(",", ":"), sort_keys=False, default=str)


def _load_youth_kpi_cache_store() -> dict[str, Any]:
    if not YOUTH_KPI_CACHE_PATH.exists():
        return {"version": YOUTH_KPI_CACHE_VERSION, "entries": {}}
    try:
        payload = json.loads(YOUTH_KPI_CACHE_PATH.read_text())
    except Exception:
        return {"version": YOUTH_KPI_CACHE_VERSION, "entries": {}}
    if not isinstance(payload, dict):
        return {"version": YOUTH_KPI_CACHE_VERSION, "entries": {}}
    if payload.get("version") != YOUTH_KPI_CACHE_VERSION:
        return {"version": YOUTH_KPI_CACHE_VERSION, "entries": {}}
    entries = payload.get("entries")
    if not isinstance(entries, dict):
        entries = {}
    return {"version": YOUTH_KPI_CACHE_VERSION, "entries": entries}


def _write_youth_kpi_cache_store(store: dict[str, Any]) -> None:
    try:
        YOUTH_KPI_CACHE_PATH.write_text(json.dumps(store, ensure_ascii=True))
    except Exception:
        pass


def clear_youth_kpi_dashboard_cache() -> None:
    with _DASHBOARD_CACHE_LOCK:
        _DASHBOARD_CACHE.clear()
    try:
        YOUTH_KPI_CACHE_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def _cached_dashboard_get(cache_key: tuple[Any, ...]) -> dict[str, Any] | None:
    key = _cache_key_string(cache_key)
    with _DASHBOARD_CACHE_LOCK:
        cached = _DASHBOARD_CACHE.get(cache_key)
        if cached is None:
            store = _load_youth_kpi_cache_store()
            payload = store.get("entries", {}).get(key)
            if not isinstance(payload, dict):
                return None
            _DASHBOARD_CACHE[cache_key] = (time.time(), payload)
            return payload
        return cached[1]


def _cached_dashboard_set(cache_key: tuple[Any, ...], payload: dict[str, Any]) -> None:
    with _DASHBOARD_CACHE_LOCK:
        _DASHBOARD_CACHE[cache_key] = (time.time(), payload)
        store = _load_youth_kpi_cache_store()
        entries = store.setdefault("entries", {})
        if isinstance(entries, dict):
            entries[_cache_key_string(cache_key)] = payload
        _write_youth_kpi_cache_store(store)


def _location_cache_ttl_seconds(event_day: str) -> int:
    try:
        parsed_day = dt.date.fromisoformat(event_day)
    except ValueError:
        return RECENT_LOCATION_CACHE_TTL_SECONDS
    recent_cutoff = dt.datetime.now(LOCAL_TZ).date() - dt.timedelta(days=1)
    if parsed_day >= recent_cutoff:
        return RECENT_LOCATION_CACHE_TTL_SECONDS
    return HISTORICAL_LOCATION_CACHE_TTL_SECONDS


def _location_cache_entry_fresh(entry: dict[str, Any] | None, event_day: str) -> bool:
    if not isinstance(entry, dict):
        return False
    fetched_at = _parse_ts(entry.get("fetched_at"))
    if fetched_at is None:
        return False
    try:
        parsed_day = dt.date.fromisoformat(event_day)
    except ValueError:
        parsed_day = None
    if parsed_day is not None and parsed_day < dt.datetime.now(LOCAL_TZ).date():
        fetched_local_day = fetched_at.astimezone(LOCAL_TZ).date()
        if fetched_local_day <= parsed_day:
            return False
    age_seconds = (dt.datetime.now(dt.timezone.utc) - fetched_at).total_seconds()
    return age_seconds <= _location_cache_ttl_seconds(event_day)


def _daysmart_account_url(customer_id: int | None) -> str | None:
    if customer_id is None:
        return None
    return (
        "https://apps.daysmartrecreation.com/dash/admin/index.php"
        f"?Action=CustomerInfo&CustomerID={customer_id}&company=qbksports"
    )


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


def _find_daysmart_matches(
    conn: Any,
    lead_row: dict[str, Any],
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    metadata = _json_loads(lead_row.get("metadata_json"), {})
    contact = metadata.get("contact") if isinstance(metadata, dict) else {}
    if not isinstance(contact, dict):
        contact = {}

    if str(lead_row.get("source_system") or "") == "daysmart_ftyc":
        daysmart_meta = metadata.get("daysmart_ftyc") if isinstance(metadata, dict) else {}
        if not isinstance(daysmart_meta, dict):
            daysmart_meta = {}
        child_customer_id = _safe_int(daysmart_meta.get("customer_id"))
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
            adult_cutoff = dt.date.today().replace(year=dt.date.today().year - 18)
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
                adult_candidates = [
                    dict(row)
                    for row in rows
                    if (_customer_birthdate(dict(row)) or adult_cutoff) < adult_cutoff
                ]
                if adult_candidates:
                    parent = adult_candidates[0]
            if parent is None and email:
                row = conn.execute(
                    """
                    SELECT *
                    FROM daysmart_customers
                    WHERE customer_id != ?
                      AND normalized_email = ?
                    ORDER BY updated_at DESC
                    LIMIT 10
                    """,
                    (child_customer_id, email),
                ).fetchone()
                if row is not None:
                    row_dict = dict(row)
                    birthdate = _customer_birthdate(row_dict)
                    if birthdate is not None and birthdate < adult_cutoff:
                        parent = row_dict
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

    adult_cutoff = dt.date.today().replace(year=dt.date.today().year - 18)
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
                if birthdate is not None and birthdate < adult_cutoff:
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
            if birthdate is not None and birthdate >= adult_cutoff:
                age_candidates.append((birthdate, row))
        if age_candidates:
            age_candidates.sort(key=lambda item: item[0], reverse=True)
            children = [row for _, row in age_candidates]
        elif len(non_parent) == 1:
            children = [non_parent[0]]

    if str(lead_row.get("source_system") or "") == "gmail_eventbrite":
        eventbrite_children: list[dict[str, Any]] = []
        seen_child_ids = {int(child["customer_id"]) for child in children if child.get("customer_id") is not None}
        for child_identity in _eventbrite_child_identities(lead_row.get("metadata_json")):
            child_name = _normalize_name(child_identity.get("name"))
            child_email = _normalize_email(child_identity.get("email"))
            child_phone = _normalize_phone(child_identity.get("phone"))
            row = None
            if child_email and child_name:
                row = conn.execute(
                    """
                    SELECT *
                    FROM daysmart_customers
                    WHERE normalized_email = ?
                      AND normalized_name = ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (child_email, child_name),
                ).fetchone()
            if row is None and child_phone and child_name:
                row = conn.execute(
                    """
                    SELECT *
                    FROM daysmart_customers
                    WHERE normalized_name = ?
                      AND (
                        normalized_phone_day = ?
                        OR normalized_phone_mobile = ?
                        OR normalized_phone_night = ?
                        OR normalized_phone_emergency = ?
                      )
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (child_name, child_phone, child_phone, child_phone, child_phone),
                ).fetchone()
            if row is None and child_name:
                row = conn.execute(
                    """
                    SELECT *
                    FROM daysmart_customers
                    WHERE normalized_name = ?
                    ORDER BY updated_at DESC
                    LIMIT 1
                    """,
                    (child_name,),
                ).fetchone()
            if row is None:
                continue
            child = dict(row)
            child_id = int(child["customer_id"])
            if child_id in seen_child_ids:
                continue
            seen_child_ids.add(child_id)
            eventbrite_children.append(child)

        if eventbrite_children:
            existing_by_name = {
                _normalize_name(child.get("full_name")): child
                for child in children
                if _normalize_name(child.get("full_name"))
            }
            for child in eventbrite_children:
                normalized_child_name = _normalize_name(child.get("full_name"))
                if normalized_child_name and normalized_child_name in existing_by_name:
                    continue
                children.append(child)

    return parent, children


def _clean_trial_class_name(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if cleaned.startswith("Junior Classes - "):
        cleaned = cleaned[len("Junior Classes - "):].strip()
    cleaned = re.sub(r"\(\s*(\d+)\s*-\s*(\d+)\s*y/o\s*\)", r"(\1-\2)", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _is_youth_class_name(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    return _contains_any_token(value, YOUTH_CLASS_TOKENS)


def _affirmative_inbound(text: str) -> bool:
    lowered = text.lower()
    patterns = [
        r"^yes\b",
        r"^yep\b",
        r"^yup\b",
        r"^yeah\b",
        r"^ya\b",
        r"^sure\b",
        r"^ok\b",
        r"^okay\b",
        r"^confirm\b",
        r"^confirmed\b",
        r"\bconfirm!?\b",
        r"\bconfirmed!?\b",
        r"\byes\b.*\bconfirm(?:ing|ed)?\b",
        r"\bhi[, ]+yes\b",
        r"\byes i (?:will|am)\b",
        r"\bi(?:'|’)ll be there\b",
        r"\bi will be there\b",
        r"\bi(?:'|’)m in\b",
        r"\bcount me in\b",
        r"\bworks for me\b",
        r"\bthat works\b",
        r"\bsounds good\b",
        r"\bsounds great\b",
        r"\bsee you then\b",
        r"\bsee you tomorrow\b",
        r"\bokay perfect\b",
        r"\bperfect\b",
        r"\blooking forward to it\b",
        r"\blooking forward to tomorrow\b",
        r"\bdone\b",
        r"\bok done\b",
        r"\bokay done\b",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def _negative_inbound(text: str) -> bool:
    lowered = text.lower()
    patterns = [
        r"\bcan'?t\b",
        r"\bwon'?t\b",
        r"\bnot coming\b",
        r"\bunable\b",
        r"\breschedule\b",
        r"\bsorry\b",
        r"\bno\b",
    ]
    return any(re.search(pattern, lowered) for pattern in patterns)


def _conversation_confirmation(conn: Any, conversation_id: int, trial_status: str | None) -> dict[str, Any]:
    conv_row = conn.execute("SELECT raw_json FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    confirmed_tag = False
    if conv_row is not None:
        payload = _json_loads(conv_row["raw_json"], {})
        contact = payload.get("contact") if isinstance(payload, dict) else {}
        tags = [
            _clean_text(tag.get("name")).lower()
            for tag in (contact.get("tags") or [])
            if isinstance(tag, dict) and _clean_text(tag.get("name"))
        ]
        confirmed_tag = "confirmed class" in tags

    rows = conn.execute(
        """
        SELECT id, body, created_at, sent_at, received_at, user_id
        FROM messages
        WHERE conversation_id = ?
        ORDER BY coalesce(created_at, received_at, sent_at) DESC
        LIMIT 40
        """,
        (conversation_id,),
    ).fetchall()

    latest_inbound_text = None
    latest_inbound_at = None
    latest_inbound_signal = None
    for row in rows:
        body = _clean_text(row["body"])
        if not body or row["user_id"]:
            continue
        latest_inbound_text = body
        latest_inbound_at = row["created_at"] or row["received_at"] or row["sent_at"]
        if _negative_inbound(body):
            latest_inbound_signal = "negative"
        elif _affirmative_inbound(body):
            latest_inbound_signal = "affirmative"
        break

    confirmed = bool(
        confirmed_tag
        or (trial_status or "").lower() == "confirmed"
        or latest_inbound_signal == "affirmative"
    )
    return {
        "confirmed": confirmed,
        "confirmed_tag": confirmed_tag,
        "latest_inbound_signal": latest_inbound_signal,
        "latest_inbound_text": latest_inbound_text,
        "latest_inbound_at": latest_inbound_at,
    }


def _load_roster_checkins() -> tuple[dict[tuple[str, str | None, str], dict[str, Any]], dict[str, Any]]:
    workspace_root = Path(__file__).resolve().parents[2]
    candidates = [
        workspace_root / "qbk-roster-checkin" / ".checkin_positive_cache.json",
    ]
    chosen = next((path for path in candidates if path.exists()), None)
    if chosen is None:
        return {}, {"source": "missing", "path": None, "entries": 0}
    try:
        payload = json.loads(chosen.read_text())
    except Exception:
        return {}, {"source": "unreadable", "path": str(chosen), "entries": 0}

    lookup: dict[tuple[str, str | None, str], dict[str, Any]] = {}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            parts = key.split(":")
            if len(parts) == 3:
                day, event_id, customer_id = parts
                lookup[(day, event_id, customer_id)] = value
            elif len(parts) == 2:
                day, customer_id = parts
                lookup[(day, None, customer_id)] = value
    return lookup, {"source": "roster-cache", "path": str(chosen), "entries": len(lookup)}


def _load_daysmart_api_checkins(
    *,
    max_pages: int = 20,
    page_size: int = 100,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    try:
        client = _daysmart_client()
        rows: list[dict[str, Any]] = []
        included_regs: dict[str, dict[str, Any]] = {}
        for page in range(1, max_pages + 1):
            payload = client._get(
                "/api/v1/check-in-events",
                params={
                    "page[number]": page,
                    "page[size]": page_size,
                    "sort": "-datetime",
                    "include": "eventRegistrations",
                },
            )
            data = payload.get("data", []) if isinstance(payload, dict) else []
            included = payload.get("included", []) if isinstance(payload, dict) else []
            if not isinstance(data, list):
                data = []
            if not isinstance(included, list):
                included = []
            for item in included:
                if str(item.get("type")) != "event-registrations":
                    continue
                attrs = item.get("attributes")
                included_regs[str(item.get("id"))] = attrs if isinstance(attrs, dict) else {}
            if not data:
                break
            rows.extend(data)
            if len(data) < page_size:
                break

        lookup: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            attrs = row.get("attributes")
            if not isinstance(attrs, dict):
                continue
            if attrs.get("action") != "in":
                continue
            checkin_dt = _parse_ts(attrs.get("datetime"), naive_tz=LOCAL_TZ)
            if checkin_dt is None:
                continue
            reg_links = ((((row.get("relationships") or {}).get("eventRegistrations")) or {}).get("data")) or []
            if not isinstance(reg_links, list):
                continue
            for reg_link in reg_links:
                if not isinstance(reg_link, dict):
                    continue
                reg_attrs = included_regs.get(str(reg_link.get("id"))) or {}
                customer_id = str(reg_attrs.get("customer_id") or "").strip()
                event_id = str(reg_attrs.get("event_id") or "").strip() or None
                if not customer_id:
                    continue
                lookup.setdefault(customer_id, []).append(
                    {
                        "datetime": checkin_dt,
                        "event_id": event_id,
                    }
                )

        for items in lookup.values():
            items.sort(key=lambda item: item["datetime"])

        entry_count = sum(len(items) for items in lookup.values())
        return lookup, {"source": "daysmart-checkin-events", "entries": entry_count}
    except DaysmartApiError as exc:
        return {}, {"source": "daysmart-checkin-events-error", "entries": 0, "detail": str(exc)[:240]}
    except Exception as exc:
        return {}, {"source": "daysmart-checkin-events-error", "entries": 0, "detail": str(exc)[:240]}


def _parse_admin_checkin_datetime(value: str | None) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip().upper()
    for fmt in ("%m/%d/%Y %I:%M%p", "%m/%d/%Y %I:%M %p"):
        try:
            parsed = dt.datetime.strptime(cleaned, fmt)
            return parsed.replace(tzinfo=LOCAL_TZ).astimezone(dt.timezone.utc)
        except ValueError:
            continue
    return _parse_ts(value, naive_tz=LOCAL_TZ)


def _load_legacy_customer_checkins(
    customer_ids: set[int] | list[int],
    *,
    max_customers: int = 75,
) -> tuple[dict[str, list[dt.datetime]], dict[str, Any]]:
    if not customer_ids:
        return {}, {"source": "daysmart-customer-checkins", "entries": 0, "customers": 0}

    company = os.getenv("DAYSMART_COMPANY", "qbksports").strip() or "qbksports"
    username = os.getenv("DAYSMART_USERNAME", "").strip()
    password = os.getenv("DAYSMART_PASSWORD", "").strip()
    if not username or not password:
        return {}, {"source": "daysmart-customer-checkins-missing-creds", "entries": 0, "customers": 0}

    cache_payload = _load_customer_checkins_cache()
    cache_dirty = False
    now_utc = dt.datetime.now(dt.timezone.utc)
    lookup: dict[str, list[dt.datetime]] = {}
    cached_customers = 0
    fetched_customers = 0
    error_count = 0
    last_error = None
    selected_ids = sorted(customer_ids)[:max_customers]

    def use_cached(customer_key: str, entry: Any) -> bool:
        if not isinstance(entry, dict):
            return False
        fetched_at = _parse_ts(entry.get("fetched_at"))
        if fetched_at is None or (now_utc - fetched_at) > dt.timedelta(seconds=RECENT_LOCATION_CACHE_TTL_SECONDS):
            return False
        values = entry.get("checkins")
        if not isinstance(values, list):
            return False
        parsed_values = [
            _parse_admin_checkin_datetime(str(value))
            for value in values
            if isinstance(value, str)
        ]
        clean_values = sorted({value for value in parsed_values if value is not None})
        if clean_values:
            lookup[customer_key] = clean_values
        return True

    missing_ids: list[int] = []
    for customer_id in selected_ids:
        customer_key = str(customer_id)
        if use_cached(customer_key, cache_payload.get(customer_key)):
            cached_customers += 1
        else:
            missing_ids.append(customer_id)

    if missing_ids:
        try:
            with requests.Session() as session:
                session.headers.update({"User-Agent": "QBKYouthKPI/1.0"})
                session.get(ADMIN_LOGIN_URL, timeout=30)
                login_response = session.post(
                    ADMIN_LOGIN_VALIDATE_URL,
                    data={
                        "_method": "POST",
                        "company_code": company,
                        "username": username,
                        "password": password,
                    },
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": ADMIN_LOGIN_URL,
                    },
                    timeout=30,
                )
                login_response.raise_for_status()
                login_payload = login_response.json()
                if login_payload.get("success") != "Login Successful":
                    raise RuntimeError("DaySmart admin login failed for customer check-ins.")

                for customer_id in missing_ids:
                    customer_key = str(customer_id)
                    try:
                        response = session.get(
                            ADMIN_CUSTOMER_CHECKINS_URL.format(
                                customer_id=customer_id,
                                company=company,
                            ),
                            headers={
                                "X-Requested-With": "XMLHttpRequest",
                                "Accept": "application/json, text/javascript, */*; q=0.01",
                                "Referer": ADMIN_CUSTOMER_PROFILE_URL.format(
                                    customer_id=customer_id,
                                    company=company,
                                ),
                            },
                            timeout=30,
                        )
                        response.raise_for_status()
                        payload = response.json()
                        data = payload.get("data") if isinstance(payload, dict) else {}
                        checkins = data.get("checkins") if isinstance(data, dict) else []
                        if not isinstance(checkins, list):
                            checkins = []
                        display_values: list[str] = []
                        parsed_values: list[dt.datetime] = []
                        for item in checkins:
                            if not isinstance(item, dict):
                                continue
                            display = str(item.get("checkinDatetime") or "").strip()
                            parsed = _parse_admin_checkin_datetime(display)
                            if parsed is None:
                                continue
                            display_values.append(display)
                            parsed_values.append(parsed)
                        if parsed_values:
                            lookup[customer_key] = sorted(set(parsed_values))
                        cache_payload[customer_key] = {
                            "fetched_at": now_utc.isoformat(),
                            "checkins": display_values,
                        }
                        cache_dirty = True
                        fetched_customers += 1
                    except Exception as exc:
                        error_count += 1
                        last_error = str(exc)[:240]
        except Exception as exc:
            error_count += max(1, len(missing_ids))
            last_error = str(exc)[:240]

    if cache_dirty:
        try:
            _write_customer_checkins_cache(cache_payload)
        except OSError:
            pass

    entries = sum(len(values) for values in lookup.values())
    meta = {
        "source": "daysmart-customer-checkins",
        "entries": entries,
        "customers": len(selected_ids),
        "fetched_customers": fetched_customers,
        "cached_customers": cached_customers,
        "errors": error_count,
    }
    if last_error:
        meta["detail"] = last_error
    return lookup, meta


def _load_location_report_checkins(
    days: set[str],
) -> tuple[dict[tuple[str, str], list[str]], dict[str, Any]]:
    if not days:
        return {}, {"source": "daysmart-location-checkins", "entries": 0, "days": 0}

    company = os.getenv("DAYSMART_COMPANY", "qbksports").strip() or "qbksports"
    username = os.getenv("DAYSMART_USERNAME", "").strip()
    password = os.getenv("DAYSMART_PASSWORD", "").strip()
    if not username or not password:
        return {}, {"source": "daysmart-location-checkins-missing-creds", "entries": 0, "days": 0}

    lookup: dict[tuple[str, str], list[str]] = {}
    cache_payload = _load_location_cache()
    cache_dirty = False
    day_count = 0
    error_count = 0
    last_error = None
    cached_day_count = 0

    for event_day in sorted(days):
        day_count += 1
        cached_entry = cache_payload.get(event_day)
        if _location_cache_entry_fresh(cached_entry, event_day):
            cached_rows = cached_entry.get("rows")
            if isinstance(cached_rows, dict):
                for customer_id, values in cached_rows.items():
                    if not isinstance(customer_id, str) or not isinstance(values, list):
                        continue
                    clean_values = [str(value) for value in values if isinstance(value, str)]
                    if clean_values:
                        lookup[(event_day, customer_id)] = sorted(clean_values)
                cached_day_count += 1
                continue
        try:
            selected_dt = dt.date.fromisoformat(event_day)
            selected_display = selected_dt.strftime("%m/%d/%Y")
            payload = {
                "_method": "POST",
                "facility_ids[]": ["1"],
                "membership_ids[]": ["0"],
                "custom_field_id": "0",
                "start_date": selected_display,
                "end_date": selected_display,
                "do_search": "1",
            }
            with requests.Session() as session:
                session.headers.update({"User-Agent": "QBKYouthKPI/1.0"})
                session.get(ADMIN_LOGIN_URL, timeout=30)
                login_response = session.post(
                    ADMIN_LOGIN_VALIDATE_URL,
                    data={
                        "_method": "POST",
                        "company_code": company,
                        "username": username,
                        "password": password,
                    },
                    headers={
                        "X-Requested-With": "XMLHttpRequest",
                        "Referer": ADMIN_LOGIN_URL,
                    },
                    timeout=30,
                )
                login_response.raise_for_status()
                login_payload = login_response.json()
                if login_payload.get("success") != "Login Successful":
                    raise RuntimeError("DaySmart admin login failed for check-in refresh.")

                report_response = session.post(
                    ADMIN_LOCATION_CHECKIN_REPORT_URL.format(company=company),
                    data=payload,
                    timeout=30,
                )
                report_response.raise_for_status()
                html_text = report_response.text

            table_match = REPORT_TABLE_RE.search(html_text)
            if not table_match:
                raise RuntimeError("Location check-in report did not include results table.")

            tbody = table_match.group("tbody")
            day_hits = 0
            day_rows: dict[str, list[str]] = {}
            for row_match in REPORT_ROW_RE.finditer(tbody):
                cells = [cell_match.group("cell") for cell_match in REPORT_CELL_RE.finditer(row_match.group("row"))]
                if len(cells) < 5:
                    continue
                customer_id = _strip_html(cells[1])
                visit_display = _strip_html(cells[4])
                if not customer_id or not customer_id.isdigit():
                    continue
                try:
                    visit_dt = dt.datetime.strptime(visit_display, "%m/%d/%Y %I:%M%p")
                except ValueError:
                    continue
                visit_iso = visit_dt.isoformat()
                lookup.setdefault((event_day, customer_id), []).append(visit_iso)
                day_rows.setdefault(customer_id, []).append(visit_iso)
                day_hits += 1
            for key, values in list(lookup.items()):
                if key[0] == event_day:
                    values.sort()
            for values in day_rows.values():
                values.sort()
            cache_payload[event_day] = {
                "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "rows": day_rows,
            }
            cache_dirty = True
        except Exception as exc:
            error_count += 1
            last_error = str(exc)[:240]
            if isinstance(cached_entry, dict):
                cached_rows = cached_entry.get("rows")
                if isinstance(cached_rows, dict):
                    for customer_id, values in cached_rows.items():
                        if not isinstance(customer_id, str) or not isinstance(values, list):
                            continue
                        clean_values = [str(value) for value in values if isinstance(value, str)]
                        if clean_values:
                            lookup[(event_day, customer_id)] = sorted(clean_values)

    if cache_dirty:
        try:
            _write_location_cache(cache_payload)
        except OSError:
            pass

    meta = {
        "source": "daysmart-location-checkins",
        "entries": len(lookup),
        "days": day_count,
        "errors": error_count,
        "cached_days": cached_day_count,
    }
    if last_error:
        meta["detail"] = last_error
    return lookup, meta


def _load_attendance_fallback(conn: Any) -> dict[str, list[dt.datetime]]:
    rows = conn.execute(
        """
        SELECT child_key, event_at
        FROM youth_attendance_events
        """
    ).fetchall()
    lookup: dict[str, set[dt.datetime]] = {}
    for row in rows:
        event_dt = _parse_ts(row["event_at"], naive_tz=LOCAL_TZ)
        if event_dt is None:
            continue
        lookup.setdefault(str(row["child_key"]), set()).add(event_dt)
    return {child_key: sorted(values) for child_key, values in lookup.items()}


def _load_cached_location_report_checkins_all() -> tuple[dict[str, list[dt.datetime]], dict[str, Any]]:
    payload = _load_location_cache()
    lookup: dict[str, set[dt.datetime]] = {}
    day_count = 0
    entry_count = 0
    for event_day, entry in payload.items():
        if not isinstance(entry, dict):
            continue
        rows = entry.get("rows")
        if not isinstance(rows, dict):
            continue
        day_count += 1
        for customer_id, values in rows.items():
            if not isinstance(values, list):
                continue
            for value in values:
                parsed = _parse_ts(str(value), naive_tz=LOCAL_TZ) if isinstance(value, str) else None
                if parsed is None:
                    try:
                        parsed_day = dt.date.fromisoformat(str(event_day))
                        parsed = dt.datetime.combine(parsed_day, dt.time(12, 0), tzinfo=LOCAL_TZ).astimezone(dt.timezone.utc)
                    except ValueError:
                        continue
                lookup.setdefault(str(customer_id), set()).add(parsed)
                entry_count += 1
    return (
        {customer_id: sorted(values) for customer_id, values in lookup.items()},
        {"source": "cached-location-report", "entries": entry_count, "days": day_count},
    )


def _build_roster_customer_checkins(
    roster_lookup: dict[tuple[str, str | None, str], dict[str, Any]],
) -> dict[str, list[dt.datetime]]:
    lookup: dict[str, set[dt.datetime]] = {}
    for (event_day, _event_id, customer_id), item in roster_lookup.items():
        if not isinstance(item, dict) or not item.get("checked_in"):
            continue
        parsed = _parse_ts(item.get("timestamp"), naive_tz=LOCAL_TZ)
        if parsed is None:
            try:
                parsed_day = dt.date.fromisoformat(str(event_day))
                parsed = dt.datetime.combine(parsed_day, dt.time(12, 0), tzinfo=LOCAL_TZ).astimezone(dt.timezone.utc)
            except ValueError:
                continue
        lookup.setdefault(str(customer_id), set()).add(parsed)
    return {customer_id: sorted(values) for customer_id, values in lookup.items()}


def _build_attendance_fallback_lookup(
    attendance_fallback: dict[str, list[dt.datetime]],
) -> dict[str, list[dt.datetime]]:
    return {
        str(child_key): sorted(
            item for item in values if isinstance(item, dt.datetime)
        )
        for child_key, values in attendance_fallback.items()
    }


def _first_checkin_after_lead_started(
    *,
    customer_id: int,
    child_key: str,
    lead_started_at: dt.datetime | None,
    roster_customer_checkins: dict[str, list[dt.datetime]],
    api_checkin_lookup: dict[str, list[dict[str, Any]]],
    location_customer_checkins: dict[str, list[dt.datetime]],
    attendance_fallback_lookup: dict[str, list[dt.datetime]],
) -> dt.datetime | None:
    candidates: list[dt.datetime] = []
    customer_key = str(customer_id)
    candidates.extend(roster_customer_checkins.get(customer_key, []))
    candidates.extend(location_customer_checkins.get(customer_key, []))
    candidates.extend(attendance_fallback_lookup.get(child_key, []))
    for item in api_checkin_lookup.get(customer_key, []):
        item_dt = item.get("datetime")
        if isinstance(item_dt, dt.datetime):
            candidates.append(item_dt)
    if not candidates:
        return None
    candidates = sorted(set(candidates))
    if lead_started_at is None:
        return candidates[0]
    on_or_after = [candidate for candidate in candidates if candidate >= lead_started_at]
    if on_or_after:
        return on_or_after[0]
    before = [candidate for candidate in candidates if candidate < lead_started_at]
    if before:
        return before[-1]
    return None


def _load_membership_map(conn: Any, customer_ids: set[int]) -> dict[int, list[dict[str, Any]]]:
    if not customer_ids:
        return {}
    placeholders = ",".join("?" for _ in customer_ids)
    rows = conn.execute(
        f"""
        SELECT cm.customer_id, m.product_name, m.created_at, m.expires_at, m.membership_id
        FROM daysmart_customer_memberships cm
        JOIN daysmart_memberships m ON m.membership_id = cm.membership_id
        WHERE cm.customer_id IN ({placeholders})
        ORDER BY cm.customer_id, coalesce(m.expires_at, '') DESC, coalesce(m.created_at, '') ASC, m.membership_id ASC
        """,
        tuple(sorted(customer_ids)),
    ).fetchall()
    membership_map: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        customer_id = int(row["customer_id"])
        base = (row["product_name"] or "").strip() or "Unnamed membership"
        if not _is_allowed_membership_name(base):
            continue
        record = {
            "label": _membership_display_label(base),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }
        if not _membership_is_currently_active(record):
            continue
        membership_map.setdefault(customer_id, []).append(record)
    return membership_map


def _load_program_membership_map(conn: Any, customer_ids: set[int]) -> dict[int, list[dict[str, Any]]]:
    if not customer_ids:
        return {}
    placeholders = ",".join("?" for _ in customer_ids)
    rows = conn.execute(
        f"""
        SELECT cm.customer_id, m.product_name, m.expires_at, m.created_at
        FROM daysmart_customer_memberships cm
        JOIN daysmart_memberships m ON m.membership_id = cm.membership_id
        WHERE cm.customer_id IN ({placeholders})
        ORDER BY cm.customer_id, coalesce(m.expires_at, m.created_at, '') DESC
        """,
        tuple(sorted(customer_ids)),
    ).fetchall()
    membership_map: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        customer_id = int(row["customer_id"])
        base = (row["product_name"] or "").strip() or "Unnamed membership"
        if not _is_allowed_membership_name(base):
            continue
        expires = (row["expires_at"] or "").strip()
        label = f"{base} ({expires[:10]})" if expires else base
        record = {
            "label": label,
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }
        if not _membership_is_currently_active(record):
            continue
        membership_map.setdefault(customer_id, []).append(record)
    return membership_map


def _membership_labels_after_started(
    records: list[dict[str, Any]],
    started_at: dt.datetime | None,
) -> list[str]:
    labels: list[str] = []
    for record in records:
        label = str(record.get("label") or "").strip()
        if not label:
            continue
        if not _membership_is_currently_active(record):
            continue
        labels.append(label)
    return labels


def _daysmart_team_summary(team_id: int) -> tuple[str | None, str | None]:
    client = _daysmart_client()
    payload = client._get(f"/api/v1/teams/{team_id}")
    data = payload.get("data") if isinstance(payload, dict) else None
    attrs = data.get("attributes") if isinstance(data, dict) and isinstance(data.get("attributes"), dict) else {}
    return attrs.get("name"), attrs.get("start_date")


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


def _matching_ftyc_event_registration(
    client: DaysmartClient,
    *,
    customer_id: int,
    invoice_ts: dt.datetime,
    invoice_team_id: int | None = None,
    page_size: int = 50,
) -> dict[str, Any] | None:
    """Prefer the exact class event created with an FTYC invoice over team-date guesses."""
    try:
        event_rows, _ = client.list_event_registrations(
            page_number=1,
            page_size=page_size,
            filters={"customer_id": customer_id},
            sort="-time",
        )
    except Exception:
        return None

    best: dict[str, Any] | None = None
    best_event_ts: dt.datetime | None = None
    for row in event_rows:
        attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
        registration_id = _daysmart_as_int(row.get("id"))
        event_id = _daysmart_as_int(attrs.get("event_id"))
        registration_ts = _daysmart_as_dt(attrs.get("time") or attrs.get("updated_at"))
        if registration_id is None or event_id is None or registration_ts is None:
            continue

        try:
            payload = client._get(f"/api/v1/events/{event_id}")
        except Exception:
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        event_attrs = (
            data.get("attributes")
            if isinstance(data, dict) and isinstance(data.get("attributes"), dict)
            else {}
        )
        home_team_id = _daysmart_as_int(event_attrs.get("hteam_id"))
        if invoice_team_id is not None and home_team_id is not None and home_team_id != invoice_team_id:
            continue
        event_start = event_attrs.get("start")
        event_ts = _daysmart_as_dt(event_start) or registration_ts
        delta = abs((registration_ts - invoice_ts).total_seconds())
        is_original_booking = delta <= FTYC_REGISTRATION_MATCH_WINDOW_SECONDS
        is_later_same_team_reschedule = (
            invoice_team_id is not None
            and home_team_id == invoice_team_id
            and event_ts >= invoice_ts
        )
        if not is_original_booking and not is_later_same_team_reschedule:
            continue

        event_name = event_attrs.get("desc") or event_attrs.get("name")
        if not event_name and home_team_id:
            try:
                team_name, _ = _daysmart_team_summary(int(home_team_id))
                event_name = team_name or event_name
            except Exception:
                pass
        if not _is_youth_trial_class_name(event_name):
            continue

        if best is None or event_ts > (best_event_ts or event_ts):
            best = {
                "row": row,
                "registration_id": registration_id,
                "customer_id": customer_id,
                "team_or_event_id": event_id,
                "event_name": event_name,
                "event_start": event_start,
                "created_at": attrs.get("time") or attrs.get("updated_at"),
            }
            best_event_ts = event_ts
    return best


def _registration_dict_from_row(row: Any) -> dict[str, Any]:
    return {
        "registration_id": int(row["registration_id"]),
        "customer_id": int(row["customer_id"]),
        "team_or_event_id": row["team_or_event_id"],
        "event_name": row["event_name"],
        "event_start": row["event_start"],
        "created_at": row["created_at"],
    }


def _live_customer_memberships(customer_id: int) -> list[dict[str, Any]]:
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
            if item.get("type") not in {"memberships", "membership"}:
                continue
            membership_id = _daysmart_as_int(item.get("id"))
            if membership_id is not None:
                included_map[membership_id] = item

    memberships: list[dict[str, Any]] = []
    for rel in membership_data:
        if not isinstance(rel, dict):
            continue
        membership_id = _daysmart_as_int(rel.get("id"))
        if membership_id is None:
            continue
        membership_payload = included_map.get(membership_id)
        if membership_payload is None:
            try:
                membership_payload = client.get_membership(membership_id)
            except Exception:
                membership_payload = None
        if membership_payload is None:
            continue

        attrs = membership_payload.get("attributes") if isinstance(membership_payload.get("attributes"), dict) else {}
        product_name = attrs.get("product_name")
        included_items = membership_payload.get("_included") if isinstance(membership_payload, dict) else []
        if isinstance(included_items, list):
            for inc in included_items:
                if not isinstance(inc, dict) or inc.get("type") not in {"products", "product"}:
                    continue
                inc_attrs = inc.get("attributes") if isinstance(inc.get("attributes"), dict) else {}
                product_name = inc_attrs.get("name") or inc_attrs.get("desc") or product_name
                if product_name:
                    break
        if not product_name:
            product_id = attrs.get("prod_id") or attrs.get("product_id")
            if product_id not in (None, ""):
                try:
                    product = client.get_product(int(product_id))
                    product_attrs = product.get("attributes") if isinstance(product.get("attributes"), dict) else {}
                    product_name = product_attrs.get("name") or product_attrs.get("desc")
                except Exception:
                    product_name = None

        base = (product_name or "").strip() or "Unnamed membership"
        if not _is_allowed_membership_name(base):
            continue
        created = (attrs.get("created") or attrs.get("created_at") or "").strip()
        expires = (
            attrs.get("expires")
            or attrs.get("expires_at")
            or attrs.get("expiration")
            or attrs.get("expiration_date")
            or ""
        )
        record = {
            "label": _membership_display_label(base),
            "created_at": created,
            "expires_at": str(expires or "").strip(),
        }
        if not _membership_is_currently_active(record):
            continue
        memberships.append(record)

    return memberships


def _refresh_live_daysmart_facts_for_customers(
    conn: Any,
    db_path: str,
    customer_ids: set[int],
    *,
    max_customers: int = 200,
    page_size: int = 50,
) -> dict[str, Any]:
    if not customer_ids:
        return {
            "customers_checked": 0,
            "registrations_upserted": 0,
            "ftyc_upserted": 0,
            "live_memberships_found": 0,
            "errors": 0,
            "live_memberships": {},
        }

    client = _daysmart_client()
    registrations_upserted = 0
    ftyc_upserted = 0
    errors = 0
    live_memberships: dict[int, list[dict[str, Any]]] = {}

    if isinstance(customer_ids, set):
        customer_sequence = sorted(customer_ids, reverse=True)
    else:
        customer_sequence = list(customer_ids)
    seen_customer_ids: set[int] = set()
    ordered_customer_ids: list[int] = []
    for customer_id in customer_sequence:
        if customer_id in seen_customer_ids:
            continue
        seen_customer_ids.add(customer_id)
        ordered_customer_ids.append(customer_id)

    for customer_id in ordered_customer_ids[:max_customers]:
        try:
            memberships = _live_customer_memberships(customer_id)
            if memberships:
                live_memberships[customer_id] = memberships
        except Exception:
            errors += 1

        try:
            event_rows, _ = client.list_event_registrations(
                page_number=1,
                page_size=page_size,
                filters={"customer_id": customer_id},
                sort="-time",
            )
        except Exception:
            errors += 1
            event_rows = []

        for row in event_rows:
            attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
            event_id = _daysmart_as_int(attrs.get("event_id"))
            event_name: str | None = None
            event_start: str | None = None
            if event_id is not None:
                try:
                    event_name, event_start = _daysmart_event_summary(event_id)
                except Exception:
                    event_name, event_start = None, None
            _upsert_daysmart_class_registration(
                db_path,
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
            errors += 1
            invoice_rows = []

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
            if _daysmart_as_int(attrs.get("discount_id")) != FTYC_DISCOUNT_ID:
                continue
            if attrs.get("is_reversal") or attrs.get("reversal_item_id") or attrs.get("reversed_item_id"):
                continue
            price = _daysmart_as_decimal(attrs.get("price"))
            if price is None or price >= 0:
                continue
            invoice_ts = _daysmart_as_dt(attrs.get("created_at")) or _daysmart_as_dt(attrs.get("date"))
            if invoice_ts is None:
                continue

            best: dict[str, Any] | None = None
            best_event_ts: dt.datetime | None = None
            for candidate in candidates:
                if not _is_youth_trial_class_name(candidate.get("event_name")):
                    continue
                registration_ts = _daysmart_as_dt(candidate.get("created_at"))
                if registration_ts is None:
                    continue
                delta = abs((registration_ts - invoice_ts).total_seconds())
                if delta > FTYC_REGISTRATION_MATCH_WINDOW_SECONDS:
                    continue
                event_ts = _daysmart_as_dt(candidate.get("event_start")) or registration_ts
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
        "customers_checked": min(len(ordered_customer_ids), max_customers),
        "registrations_upserted": registrations_upserted,
        "ftyc_upserted": ftyc_upserted,
        "live_memberships_found": len(live_memberships),
        "errors": errors,
        "live_memberships": live_memberships,
    }


def _refresh_live_memberships_for_customers(
    customer_ids: list[int] | set[int],
    *,
    max_customers: int = 75,
) -> dict[str, Any]:
    if not customer_ids:
        return {
            "customers_checked": 0,
            "live_memberships_found": 0,
            "errors": 0,
            "live_memberships": {},
        }

    errors = 0
    live_memberships: dict[int, list[dict[str, Any]]] = {}
    seen_ids: set[int] = set()
    selected_ids: list[int] = []
    for customer_id in customer_ids:
        if customer_id in seen_ids:
            continue
        seen_ids.add(customer_id)
        selected_ids.append(customer_id)
        if len(selected_ids) >= max_customers:
            break
    for customer_id in selected_ids:
        try:
            memberships = _live_customer_memberships(customer_id)
            if memberships:
                live_memberships[customer_id] = memberships
        except Exception:
            errors += 1

    return {
        "customers_checked": len(selected_ids),
        "live_memberships_found": len(live_memberships),
        "errors": errors,
        "live_memberships": live_memberships,
    }


def _load_registration_map(conn: Any, customer_ids: set[int]) -> dict[int, list[dict[str, Any]]]:
    if not customer_ids:
        return {}
    placeholders = ",".join("?" for _ in customer_ids)
    rows = conn.execute(
        f"""
        SELECT
            f.registration_id,
            f.customer_id,
            f.event_id AS team_or_event_id,
            f.event_name,
            f.event_start,
            f.registration_created_at AS created_at,
            f.invoice_item_id,
            f.invoice_id,
            f.discount_id,
            f.invoice_created_at,
            d.raw_json AS registration_raw_json
        FROM daysmart_ftyc_trial_registrations f
        LEFT JOIN daysmart_class_registrations d
          ON d.source_type = 'event_registration'
         AND d.registration_id = f.registration_id
        WHERE f.customer_id IN ({placeholders})
        ORDER BY f.customer_id ASC, coalesce(f.event_start, f.registration_created_at, '') DESC, f.registration_id DESC
        """,
        tuple(sorted(customer_ids)),
    ).fetchall()
    registration_map: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        name = _clean_trial_class_name(item.get("event_name"))
        if not _is_youth_class_name(name):
            continue
        item["event_name_clean"] = name
        registration_map.setdefault(int(item["customer_id"]), []).append(item)
    return registration_map


def _load_program_registration_map(conn: Any, customer_ids: set[int]) -> dict[int, list[dict[str, Any]]]:
    if not customer_ids:
        return {}
    placeholders = ",".join("?" for _ in customer_ids)
    rows = conn.execute(
        f"""
        SELECT registration_id, customer_id, team_or_event_id, event_name, event_start, created_at
        FROM daysmart_class_registrations
        WHERE source_type = 'event_registration'
          AND customer_id IN ({placeholders})
        ORDER BY customer_id ASC, coalesce(event_start, created_at, '') ASC, registration_id ASC
        """,
        tuple(sorted(customer_ids)),
    ).fetchall()
    registration_map: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        item = dict(row)
        item["event_name_clean"] = _clean_trial_class_name(item.get("event_name"))
        registration_map.setdefault(int(item["customer_id"]), []).append(item)
    return registration_map


def _lead_matches_excluded_program(
    lead: dict[str, Any],
    child_customers: list[dict[str, Any]],
    program_registration_map: dict[int, list[dict[str, Any]]],
    allowed_registration_map: dict[int, list[dict[str, Any]]],
) -> bool:
    if _is_excluded_class_name(lead.get("trial_class_name")):
        return True
    metadata_json = lead.get("metadata_json")
    if _is_excluded_class_name(metadata_json):
        return True
    recent_message = lead.get("recent_message")
    if _is_excluded_class_name(recent_message):
        return True

    if not child_customers:
        return False

    has_allowed_registration = False
    has_excluded_registration = False
    for child in child_customers:
        child_id = int(child["customer_id"])
        if allowed_registration_map.get(child_id):
            has_allowed_registration = True
        for registration in program_registration_map.get(child_id, []):
            if _is_excluded_class_name(registration.get("event_name_clean") or registration.get("event_name")):
                has_excluded_registration = True
                break
    return has_excluded_registration and not has_allowed_registration


def _registration_checked_in_from_cached_sources(
    registration: dict[str, Any],
    customer_id: int,
    child_key: str,
    roster_lookup: dict[tuple[str, str | None, str], dict[str, Any]],
    api_checkin_lookup: dict[str, list[dict[str, Any]]],
    attendance_fallback: dict[str, list[dt.datetime]],
) -> tuple[bool, str | None]:
    event_dt = _parse_ts(
        registration.get("event_start") or registration.get("created_at"),
        naive_tz=LOCAL_TZ,
    )
    if event_dt is None:
        return False, None
    registration_payload = _json_loads(registration.get("registration_raw_json"), {})
    registration_attrs = (
        registration_payload.get("attributes")
        if isinstance(registration_payload, dict) and isinstance(registration_payload.get("attributes"), dict)
        else {}
    )
    raw_checked_in = registration_attrs.get("checked_in")
    if raw_checked_in not in (None, "", 0, "0", False):
        return True, event_dt.isoformat()
    event_day = event_dt.astimezone(LOCAL_TZ).date().isoformat()
    event_id = str(registration.get("team_or_event_id") or "").strip() or None
    customer_key = str(customer_id)
    exact = roster_lookup.get((event_day, event_id, customer_key))
    if exact and exact.get("checked_in"):
        return True, exact.get("timestamp")
    day_only = roster_lookup.get((event_day, None, customer_key))
    if day_only and day_only.get("checked_in"):
        return True, day_only.get("timestamp")
    window_start = event_dt - dt.timedelta(hours=CHECKIN_MATCH_WINDOW_HOURS)
    window_end = event_dt + dt.timedelta(hours=CHECKIN_MATCH_WINDOW_HOURS)
    for item in api_checkin_lookup.get(customer_key, []):
        item_dt = item.get("datetime")
        if not isinstance(item_dt, dt.datetime):
            continue
        item_event_id = item.get("event_id")
        if event_id and item_event_id and str(item_event_id) == event_id:
            return True, item_dt.isoformat()
        if window_start <= item_dt <= window_end:
            return True, item_dt.isoformat()
    for item_dt in attendance_fallback.get(child_key, []):
        if item_dt.astimezone(LOCAL_TZ).date().isoformat() == event_day:
            return True, item_dt.isoformat()
    return False, None


def _registration_checked_in(
    registration: dict[str, Any],
    customer_id: int,
    child_key: str,
    roster_lookup: dict[tuple[str, str | None, str], dict[str, Any]],
    location_checkin_lookup: dict[tuple[str, str], list[str]],
    api_checkin_lookup: dict[str, list[dict[str, Any]]],
    attendance_fallback: dict[str, list[dt.datetime]],
) -> tuple[bool, str | None]:
    checked_in, timestamp = _registration_checked_in_from_cached_sources(
        registration,
        customer_id,
        child_key,
        roster_lookup,
        api_checkin_lookup,
        attendance_fallback,
    )
    if checked_in:
        return checked_in, timestamp

    event_dt = _parse_ts(
        registration.get("event_start") or registration.get("created_at"),
        naive_tz=LOCAL_TZ,
    )
    if event_dt is None:
        return False, None
    event_day = event_dt.astimezone(LOCAL_TZ).date().isoformat()
    location_hits = location_checkin_lookup.get((event_day, str(customer_id))) or []
    if location_hits:
        return True, location_hits[0]
    return False, None


def _display_registration(registration: dict[str, Any] | None) -> str:
    if not registration:
        return ""
    name = registration.get("event_name_clean") or registration.get("event_name") or ""
    event_dt = _parse_ts(
        registration.get("event_start") or registration.get("created_at"),
        naive_tz=LOCAL_TZ,
    )
    label = _format_local(event_dt) if event_dt else ""
    if name and label:
        return f"{name} - {label}"
    return name or label


def _registration_for_checkin_day(
    registrations: list[dict[str, Any]],
    checkin_at: dt.datetime | None,
) -> dict[str, Any] | None:
    if checkin_at is None:
        return None
    checkin_day = checkin_at.astimezone(LOCAL_TZ).date()
    matches: list[tuple[float, dict[str, Any]]] = []
    for registration in registrations:
        event_dt = _parse_ts(
            registration.get("event_start") or registration.get("created_at"),
            naive_tz=LOCAL_TZ,
        )
        if event_dt is None or event_dt.astimezone(LOCAL_TZ).date() != checkin_day:
            continue
        matches.append((abs((event_dt - checkin_at).total_seconds()), registration))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    return matches[0][1]


def _lead_status(
    *,
    closed_at: dt.datetime | None,
    has_account: bool,
    has_scheduled: bool,
    has_attended: bool,
    has_no_show: bool,
    has_membership: bool,
    last_interaction_at: dt.datetime | None,
    now: dt.datetime,
) -> str:
    if has_membership:
        return "Membership purchased"
    if has_attended:
        return "Attended"
    if has_no_show:
        return "No-show"
    if has_scheduled:
        return "Scheduled"
    if has_account:
        return "Account created"
    if closed_at is not None:
        return "Lost"
    if last_interaction_at is not None and last_interaction_at <= now - dt.timedelta(days=2):
        return "Needs follow-up"
    return "New"


def _resolve_window(days: int, window: str | None) -> tuple[str, dt.date, dt.date, int]:
    now_local = dt.datetime.now(dt.timezone.utc).astimezone(LOCAL_TZ)
    end_date = now_local.date()
    normalized_window = (window or "").strip().lower()
    if normalized_window == "this_year":
        start_date = dt.date(end_date.year, 1, 1)
        resolved_days = max(1, (end_date - start_date).days + 1)
        return "This Year", start_date, end_date, resolved_days
    if normalized_window == "all_time":
        start_date = dt.date(2000, 1, 1)
        resolved_days = max(1, (end_date - start_date).days + 1)
        return "All Time", start_date, end_date, resolved_days
    resolved_days = max(1, min(days, 90))
    start_date = end_date - dt.timedelta(days=resolved_days)
    return f"Last {resolved_days} Days", start_date, end_date, resolved_days


def _summary_cards_for_window(
    *,
    window_label: str,
    total_leads: int,
    accounts_created: int,
    attended: int,
    memberships: int,
) -> list[dict[str, Any]]:
    return [
        {"key": "new_leads", "label": "New Leads", "count": total_leads, "subtext": window_label},
        {
            "key": "accounts_created",
            "label": "DaySmart Accounts",
            "count": accounts_created,
            "subtext": _format_percent(accounts_created, total_leads),
        },
        {
            "key": "attended_trials",
            "label": "Checked In",
            "count": attended,
            "subtext": _format_percent(attended, total_leads),
        },
        {
            "key": "memberships",
            "label": "Memberships",
            "count": memberships,
            "subtext": _format_percent(memberships, attended),
        },
    ]


def _conversation_status_items(status_counts: Counter[str]) -> list[dict[str, Any]]:
    return [
        {"label": label, "count": status_counts.get(label, 0)}
        for label in (
            "New",
            "Needs follow-up",
            "Account created",
            "Scheduled",
            "Attended",
            "No-show",
            "Membership purchased",
            "Lost",
        )
    ]


def _merge_append_only_item(existing: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    existing_no_show = bool(existing.get("has_no_show")) or str(existing.get("lead_status") or "").lower() == "no-show"
    fresh_has_class = fresh.get("free_trial_class_display") not in (None, "", "--") or fresh.get("scheduled_class_display") not in (None, "", "--")
    allow_class_update = existing_no_show and fresh_has_class

    for field in (
        "conversation_id",
        "conversation_url",
        "lead_started_at",
        "lead_started_at_display",
        "last_interaction_at",
        "last_interaction_at_display",
        "parent_name",
        "parent_phone",
        "parent_daysmart_url",
        "child_name",
        "child_daysmart_url",
        "trial_status",
        "conversation_confirmed",
        "latest_inbound_signal",
        "latest_inbound_text",
        "lead_status",
        ):
        if fresh.get(field) not in (None, "", [], {}):
            merged[field] = fresh.get(field)

    for field in (
        "free_trial_class_display",
        "scheduled_class_display",
    ):
        fresh_value = fresh.get(field)
        if allow_class_update and fresh_value not in (None, "", "--"):
            merged[field] = fresh_value
        elif merged.get(field) in (None, "", "--") and fresh_value not in (None, "", "--"):
            merged[field] = fresh_value

    for field in (
        "attended_class_display",
        "attended_at",
        "attended_at_display",
        "memberships_display",
    ):
        if merged.get(field) in (None, "", "--") and fresh.get(field) not in (None, "", "--"):
            merged[field] = fresh.get(field)

    for field in ("memberships", "all_memberships"):
        existing_values = merged.get(field) or []
        fresh_values = fresh.get(field) or []
        if not existing_values and fresh_values:
            merged[field] = fresh_values

    merged["has_account"] = bool(existing.get("has_account") or fresh.get("has_account"))
    merged["has_scheduled"] = bool(existing.get("has_scheduled") or fresh.get("has_scheduled"))
    merged["has_confirmed"] = bool(existing.get("has_confirmed") or fresh.get("has_confirmed"))
    merged["has_attended"] = bool(existing.get("has_attended") or fresh.get("has_attended"))
    merged["has_membership"] = bool(existing.get("has_membership") or fresh.get("has_membership"))
    merged["has_no_show"] = bool(fresh.get("has_no_show"))

    merged["attendance_pending"] = bool(existing.get("attendance_pending") and fresh.get("attendance_pending"))
    if merged["has_membership"]:
        merged["lead_status"] = "Membership purchased"
    elif merged["has_attended"]:
        merged["lead_status"] = "Attended"
    elif merged["has_no_show"]:
        merged["lead_status"] = "No-show"
    elif merged["has_scheduled"]:
        merged["lead_status"] = "Scheduled"
    elif merged["has_account"]:
        merged["lead_status"] = "Account created"
    elif fresh.get("lead_status") not in (None, ""):
        merged["lead_status"] = fresh.get("lead_status")
    return merged


def _item_child_id(item: dict[str, Any]) -> str | None:
    child_id = item.get("child_daysmart_customer_id")
    if child_id not in (None, ""):
        return str(child_id)
    item_key = str(item.get("item_key") or "")
    match = re.search(r":child:(\d+)", item_key)
    if match:
        return match.group(1)
    match = re.search(r"daysmart_ftyc:customer:\d+:child:(\d+)", item_key)
    if match:
        return match.group(1)
    return None


def _dedupe_dashboard_item_key(item: dict[str, Any]) -> tuple[Any, ...]:
    child_id = _item_child_id(item)
    class_display = str(item.get("free_trial_class_display") or "").strip()
    if child_id and class_display:
        return ("child_class", child_id, class_display)
    parent_phone = _normalize_phone(item.get("parent_phone"))
    child_name = _normalize_name(item.get("child_name"))
    if child_name and parent_phone and class_display:
        return ("child_phone_class", child_name, parent_phone, class_display)
    return ("item", str(item.get("item_key") or ""))


def _dashboard_item_score(item: dict[str, Any]) -> tuple[int, str]:
    score = 0
    if item.get("conversation_url"):
        score += 40
    if item.get("latest_inbound_text") or item.get("latest_inbound_signal"):
        score += 30
    if _item_child_id(item):
        score += 20
    if item.get("free_trial_class_display"):
        score += 10
    if item.get("parent_phone") not in (None, "", "--"):
        score += 5
    if item.get("source_system") == "daysmart_ftyc":
        score += 2
    return score, str(item.get("last_interaction_at") or item.get("lead_started_at") or "")


def _dedupe_dashboard_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[tuple[Any, ...], dict[str, Any]] = {}
    order: list[tuple[Any, ...]] = []
    for item in items:
        key = _dedupe_dashboard_item_key(item)
        if key not in deduped:
            deduped[key] = item
            order.append(key)
            continue
        existing = deduped[key]
        if _dashboard_item_score(item) > _dashboard_item_score(existing):
            replacement = dict(item)
            for field in ("parent_daysmart_url", "child_daysmart_url", "parent_phone"):
                if replacement.get(field) in (None, "", "--") and existing.get(field) not in (None, "", "--"):
                    replacement[field] = existing[field]
            deduped[key] = replacement
    return [deduped[key] for key in order]


def _roll_up_dashboard_items(
    items: list[dict[str, Any]],
    *,
    window: dict[str, Any],
    attendance_source: dict[str, Any],
    include_attendance: bool,
) -> dict[str, Any]:
    items = _dedupe_dashboard_items(items)
    status_counts: Counter[str] = Counter()
    lead_groups: dict[int, dict[str, Any]] = {}
    for item in items:
        conversation_id = item.get("conversation_id")
        if not isinstance(conversation_id, int):
            continue
        group = lead_groups.setdefault(
            conversation_id,
            {
                "status": item.get("lead_status") or "New",
                "has_account": False,
                "has_scheduled": False,
                "has_confirmed": False,
                "has_attended": False,
                "has_no_show": False,
                "has_membership": False,
                "membership_count": 0,
            },
        )
        group["status"] = item.get("lead_status") or group["status"]
        group["has_account"] = bool(group["has_account"] or item.get("has_account"))
        group["has_scheduled"] = bool(group["has_scheduled"] or item.get("has_scheduled"))
        group["has_confirmed"] = bool(group["has_confirmed"] or item.get("has_confirmed"))
        group["has_attended"] = bool(group["has_attended"] or item.get("has_attended"))
        group["has_no_show"] = bool(group["has_no_show"] or item.get("has_no_show"))
        group["has_membership"] = bool(group["has_membership"] or item.get("has_membership"))
        if item.get("has_membership"):
            group["membership_count"] += 1

    for group in lead_groups.values():
        status_counts[group["status"]] += 1

    total_leads = len(lead_groups)
    accounts_created = sum(1 for group in lead_groups.values() if group["has_account"])
    scheduled = sum(1 for group in lead_groups.values() if group["has_scheduled"])
    confirmed = sum(1 for group in lead_groups.values() if group["has_confirmed"])
    attended = sum(1 for group in lead_groups.values() if group["has_attended"])
    memberships = sum(int(group.get("membership_count", 0)) for group in lead_groups.values())
    no_shows = sum(1 for group in lead_groups.values() if group["has_no_show"])
    unmatched_leads = max(total_leads - accounts_created, 0)

    return {
        "window": window,
        "summary": {
            "lead_conversations": total_leads,
            "account_created": accounts_created,
            "confirmed_trials": confirmed,
            "attended_trials": attended,
            "no_shows": no_shows,
            "memberships": memberships,
            "unmatched_leads": unmatched_leads,
            "child_rows": sum(1 for item in items if item.get("child_name")),
        },
        "summary_cards": _summary_cards_for_window(
            window_label=window.get("label") or "Window",
            total_leads=total_leads,
            accounts_created=accounts_created,
            attended=attended,
            memberships=memberships,
        ),
        "conversation_statuses": _conversation_status_items(status_counts),
        "data_quality": {
            "attendance_source": attendance_source,
            "attendance_deferred": not include_attendance,
            "matched_lead_count": accounts_created,
            "unmatched_lead_count": unmatched_leads,
            "detail_rows": len(items),
        },
        "items": items,
    }


def _merge_cached_dashboard(
    cached_dashboard: dict[str, Any],
    fresh_dashboard: dict[str, Any],
    *,
    include_attendance: bool,
) -> dict[str, Any]:
    cached_items = cached_dashboard.get("items") if isinstance(cached_dashboard, dict) else []
    fresh_items = fresh_dashboard.get("items") if isinstance(fresh_dashboard, dict) else []
    merged_by_key: dict[str, dict[str, Any]] = {}

    if isinstance(cached_items, list):
        for item in cached_items:
            if not isinstance(item, dict):
                continue
            item_key = str(item.get("item_key") or "")
            if item_key:
                merged_by_key[item_key] = dict(item)

    if isinstance(fresh_items, list):
        for item in fresh_items:
            if not isinstance(item, dict):
                continue
            item_key = str(item.get("item_key") or "")
            if not item_key:
                continue
            existing = merged_by_key.get(item_key)
            if existing is None:
                merged_by_key[item_key] = dict(item)
            else:
                merged_by_key[item_key] = _merge_append_only_item(existing, item)

    merged_items = list(merged_by_key.values())
    merged_items.sort(
        key=lambda item: (
            item.get("lead_started_at") or "",
            item.get("parent_name") or "",
            item.get("child_name") or "",
        ),
        reverse=True,
    )
    return _roll_up_dashboard_items(
        merged_items,
        window=fresh_dashboard.get("window") or cached_dashboard.get("window") or {},
        attendance_source=(
            fresh_dashboard.get("data_quality", {}).get("attendance_source")
            or cached_dashboard.get("data_quality", {}).get("attendance_source")
            or {}
        ),
        include_attendance=include_attendance,
    )


def build_youth_kpi_dashboard(
    db_path: str,
    *,
    youth_inbox_id: int,
    days: int = 7,
    window: str | None = None,
    include_attendance: bool = True,
    force_refresh: bool = False,
) -> dict[str, Any]:
    normalized_window = (window or "").strip().lower()
    window_label, start_date, end_date, resolved_days = _resolve_window(days, normalized_window)
    now_utc = dt.datetime.now(dt.timezone.utc)
    window_start = start_date.isoformat()
    cache_key = (
        "dashboard",
        YOUTH_KPI_CACHE_VERSION,
        str(db_path),
        youth_inbox_id,
        normalized_window or "days",
        resolved_days,
        include_attendance,
    )
    cached_dashboard = _cached_dashboard_get(cache_key)
    if cached_dashboard is not None and not force_refresh:
        return cached_dashboard

    roster_lookup = {}
    roster_source: dict[str, Any] = {"source": "disabled", "path": None, "entries": 0}
    if include_attendance:
        roster_lookup, roster_source = _load_roster_checkins()
    gmail_eventbrite_source: dict[str, Any] = {"source": "skipped"}
    if force_refresh and normalized_window != "all_time":
        try:
            gmail_eventbrite_source = _refresh_recent_live_gmail_eventbrite_leads()
        except Exception as exc:
            gmail_eventbrite_source = {
                "source": "live-gmail-eventbrite",
                "fetched": 0,
                "imported": 0,
                "skipped_existing": 0,
                "skipped_similar": 0,
                "skipped_not_matching": 0,
                "errors": 1,
                "detail": str(exc)[:240],
            }
    with get_conn(db_path) as conn:
        daysmart_ftyc_team_source: dict[str, Any] = {"source": "skipped", "leads_upserted": 0}
        if force_refresh and normalized_window != "all_time":
            try:
                daysmart_ftyc_team_source = _refresh_recent_daysmart_ftyc_team_leads(conn, db_path)
            except Exception as exc:
                daysmart_ftyc_team_source = {
                    "source": "daysmart-ftyc-team-leads",
                    "invoice_items": 0,
                    "registrations_upserted": 0,
                    "leads_upserted": 0,
                    "errors": 1,
                    "detail": str(exc)[:240],
                }

        lead_rows = conn.execute(
            """
            WITH first_message_times AS (
                SELECT
                    conversation_id,
                    MIN(coalesce(created_at, sent_at, received_at, '')) AS first_message_at
                FROM messages
                GROUP BY conversation_id
            )
            SELECT
                t.lead_key,
                t.family_key,
                t.contact_name,
                t.contact_phone,
                t.trial_status,
                t.trial_class_name,
                t.trial_class_when,
                t.last_interaction_at,
                t.source_system,
                t.source_ref,
                t.metadata_json,
                c.started_at AS conversation_started_at,
                c.closed_at AS conversation_closed_at,
                c.last_message_at AS conversation_last_message_at,
                fmt.first_message_at AS conversation_first_message_at,
                coalesce(
                    nullif(fmt.first_message_at, ''),
                    nullif(c.started_at, ''),
                    nullif(t.last_interaction_at, ''),
                    nullif(c.last_message_at, '')
                ) AS effective_lead_at
            FROM youth_trial_leads t
            LEFT JOIN conversations c ON t.source_system = 'salesmessage'
                                     AND c.id = CAST(t.source_ref AS INTEGER)
            LEFT JOIN first_message_times fmt ON fmt.conversation_id = c.id
            WHERE coalesce(
                nullif(fmt.first_message_at, ''),
                nullif(c.started_at, ''),
                nullif(t.last_interaction_at, ''),
                nullif(c.last_message_at, ''),
                ''
            ) >= ?
               OR t.source_system = 'daysmart_ftyc'
            ORDER BY coalesce(
                nullif(fmt.first_message_at, ''),
                nullif(c.started_at, ''),
                nullif(t.last_interaction_at, ''),
                nullif(c.last_message_at, ''),
                ''
            ) DESC,
                     CAST(t.source_ref AS INTEGER) DESC
            """,
            (window_start,),
        ).fetchall()

        matched_rows: list[dict[str, Any]] = []
        all_customer_ids: set[int] = set()
        child_customer_ids: set[int] = set()
        ordered_child_customer_ids: list[int] = []
        for row in lead_rows:
            lead = dict(row)
            _attach_salesmessage_conversation_to_ftyc_lead(
                conn,
                lead,
                youth_inbox_id=youth_inbox_id,
            )
            started_at = _effective_lead_timestamp(
                lead.get("conversation_first_message_at"),
                lead.get("conversation_started_at"),
                lead.get("last_interaction_at"),
                lead.get("conversation_last_message_at"),
            )
            if started_at is None or started_at.astimezone(LOCAL_TZ).date() < start_date:
                continue
            parent_customer, child_customers = _find_daysmart_matches(conn, lead)
            lead["parent_customer"] = parent_customer
            lead["child_customers"] = child_customers
            matched_rows.append(lead)
            if parent_customer is not None:
                all_customer_ids.add(int(parent_customer["customer_id"]))
            for child in child_customers:
                child_customer_id = int(child["customer_id"])
                all_customer_ids.add(child_customer_id)
                if child_customer_id not in child_customer_ids:
                    ordered_child_customer_ids.append(child_customer_id)
                    child_customer_ids.add(child_customer_id)

        live_refresh_source: dict[str, Any] = {
            "customers_checked": 0,
            "registrations_upserted": 0,
            "ftyc_upserted": 0,
            "live_memberships_found": 0,
            "errors": 0,
        }
        live_memberships: dict[int, list[dict[str, Any]]] = {}
        if force_refresh and all_customer_ids and normalized_window != "all_time":
            try:
                live_refresh_source = _refresh_live_daysmart_facts_for_customers(
                    conn,
                    db_path,
                    ordered_child_customer_ids,
                    max_customers=40,
                )
            except Exception as exc:
                live_refresh_source = {
                    "customers_checked": 0,
                    "registrations_upserted": 0,
                    "ftyc_upserted": 0,
                    "live_memberships_found": 0,
                    "errors": 1,
                    "detail": str(exc)[:240],
                    "live_memberships": {},
                }
            live_memberships = {
                int(customer_id): list(labels)
                for customer_id, labels in (live_refresh_source.get("live_memberships") or {}).items()
            }
        if force_refresh and child_customer_ids:
            membership_refresh_source = _refresh_live_memberships_for_customers(ordered_child_customer_ids)
            refreshed_memberships = {
                int(customer_id): list(labels)
                for customer_id, labels in (membership_refresh_source.get("live_memberships") or {}).items()
            }
            live_memberships.update(refreshed_memberships)
            live_refresh_source["membership_customers_checked"] = membership_refresh_source.get(
                "customers_checked", 0
            )
            live_refresh_source["membership_live_found"] = membership_refresh_source.get(
                "live_memberships_found", 0
            )
            live_refresh_source["membership_errors"] = membership_refresh_source.get("errors", 0)
            live_refresh_source["live_memberships_found"] = len(live_memberships)

        membership_map = _load_membership_map(conn, all_customer_ids)
        membership_map.update(live_memberships)
        registration_map = _load_registration_map(conn, all_customer_ids)
        program_membership_map = _load_program_membership_map(conn, all_customer_ids)
        for customer_id, labels in live_memberships.items():
            program_membership_map[customer_id] = labels
        program_registration_map = _load_program_registration_map(conn, all_customer_ids)
        ftyc_trial_days: set[str] = set()
        for registrations in registration_map.values():
            for registration in registrations:
                trial_dt = _parse_ts(
                    registration.get("event_start") or registration.get("created_at"),
                    naive_tz=LOCAL_TZ,
                )
                if trial_dt is not None:
                    ftyc_trial_days.add(trial_dt.astimezone(LOCAL_TZ).date().isoformat())
        now_local = now_utc.astimezone(LOCAL_TZ)
        if include_attendance:
            api_checkin_lookup, api_source = _load_daysmart_api_checkins()
            attendance_fallback = _load_attendance_fallback(conn)
            roster_customer_checkins = _build_roster_customer_checkins(roster_lookup)
            attendance_fallback_lookup = _build_attendance_fallback_lookup(attendance_fallback)
            legacy_customer_checkins, legacy_source = _load_legacy_customer_checkins(child_customer_ids)
            if legacy_source.get("entries"):
                location_trial_checkins = {}
                location_fetch_source = {
                    "source": "skipped-daysmart-location-report-legacy-customer-checkins",
                    "entries": 0,
                    "days": 0,
                    "errors": 0,
                }
            else:
                location_trial_checkins, location_fetch_source = _load_location_report_checkins(ftyc_trial_days)
            location_customer_checkins, location_source = _load_cached_location_report_checkins_all()
            for customer_id, values in legacy_customer_checkins.items():
                merged_values = set(location_customer_checkins.get(customer_id, []))
                merged_values.update(values)
                location_customer_checkins[customer_id] = sorted(merged_values)
            attendance_source = {
                "source": "roster-cache + daysmart-customer-checkins + cached-location-report + daysmart-checkin-events",
                "entries": int(roster_source.get("entries", 0))
                + int(location_source.get("entries", 0))
                + int(location_fetch_source.get("entries", 0))
                + int(api_source.get("entries", 0))
                + int(legacy_source.get("entries", 0)),
                "roster_entries": int(roster_source.get("entries", 0)),
                "legacy_customer_entries": int(legacy_source.get("entries", 0)),
                "legacy_customer_count": int(legacy_source.get("customers", 0)),
                "legacy_customer_fetched": int(legacy_source.get("fetched_customers", 0)),
                "legacy_customer_cached": int(legacy_source.get("cached_customers", 0)),
                "legacy_customer_errors": int(legacy_source.get("errors", 0)),
                "location_entries": int(location_fetch_source.get("entries", 0)),
                "cached_location_entries": int(location_source.get("entries", 0)),
                "location_days": int(location_fetch_source.get("days", 0)),
                "cached_location_days": int(location_source.get("days", 0)),
                "location_errors": int(location_fetch_source.get("errors", 0)),
                "api_entries": int(api_source.get("entries", 0)),
                "path": roster_source.get("path"),
                "location_status": location_fetch_source.get("source"),
                "cached_location_status": location_source.get("source"),
                "api_status": api_source.get("source"),
                "legacy_customer_status": legacy_source.get("source"),
            }
            if location_fetch_source.get("detail"):
                attendance_source["location_detail"] = location_fetch_source.get("detail")
            if api_source.get("detail"):
                attendance_source["api_detail"] = api_source.get("detail")
            if legacy_source.get("detail"):
                attendance_source["legacy_customer_detail"] = legacy_source.get("detail")
            attendance_source["live_daysmart_refresh"] = {
                key: value
                for key, value in live_refresh_source.items()
                if key != "live_memberships"
            }
            attendance_source["daysmart_ftyc_team_leads"] = daysmart_ftyc_team_source
            attendance_source["gmail_eventbrite"] = gmail_eventbrite_source
        else:
            api_checkin_lookup = {}
            attendance_fallback = set()
            roster_customer_checkins = {}
            attendance_fallback_lookup = {}
            location_trial_checkins = {}
            location_customer_checkins = {}
            attendance_source = {
                "source": "deferred",
                "entries": 0,
                "roster_entries": 0,
                "location_entries": 0,
                "location_days": 0,
                "location_errors": 0,
                "api_entries": 0,
                "path": None,
                "location_status": "deferred",
                "api_status": "deferred",
                "live_daysmart_refresh": {
                    key: value
                    for key, value in live_refresh_source.items()
                    if key != "live_memberships"
                },
                "daysmart_ftyc_team_leads": daysmart_ftyc_team_source,
                "gmail_eventbrite": gmail_eventbrite_source,
            }

        items: list[dict[str, Any]] = []
        lead_groups: dict[int, dict[str, Any]] = {}
        unmatched_leads = 0

        for lead in matched_rows:
            child_customers = lead.get("child_customers") or []
            if _lead_matches_excluded_program(
                lead,
                child_customers,
                program_registration_map,
                registration_map,
            ):
                continue
            source_ref = str(lead.get("source_ref") or "").strip()
            source_system = str(lead.get("source_system") or "")
            salesmessage_conversation_id = _safe_int(lead.get("salesmessage_conversation_id"))
            conversation_id = (
                salesmessage_conversation_id
                if salesmessage_conversation_id is not None
                else int(source_ref) if source_ref.isdigit() else 0
            )
            conversation_url = None
            if source_system == "salesmessage" and source_ref.isdigit():
                conversation_url = f"https://app.salesmessage.com/conversations/{source_ref}"
            elif salesmessage_conversation_id is not None:
                conversation_url = f"https://app.salesmessage.com/conversations/{salesmessage_conversation_id}"
            elif source_system == "gmail_eventbrite":
                metadata = _json_loads(lead.get("metadata_json"), {})
                if isinstance(metadata, dict):
                    gmail_meta = metadata.get("gmail")
                    if isinstance(gmail_meta, dict):
                        url = gmail_meta.get("display_url")
                        if isinstance(url, str) and url.strip():
                            conversation_url = url.strip()
            started_at = _effective_lead_timestamp(
                lead.get("conversation_first_message_at"),
                lead.get("conversation_started_at"),
                lead.get("last_interaction_at"),
                lead.get("conversation_last_message_at"),
            )
            closed_at = _parse_ts(lead.get("conversation_closed_at"))
            last_interaction_at = _parse_ts(
                lead.get("last_interaction_at") or lead.get("conversation_last_message_at")
            )
            confirmation = _conversation_confirmation(conn, conversation_id, lead.get("trial_status"))
            parent_customer = lead.get("parent_customer")
            imported_child_name = (lead.get("contact_name") or "").strip() or None
            legacy_parent_name = None
            legacy_child_name = None
            eventbrite_children = _eventbrite_child_names(lead.get("metadata_json"))
            if source_system == "legacy_csv":
                legacy_parent_name, legacy_child_name = _resolve_legacy_csv_name_roles(
                    imported_child_name,
                    parent_customer,
                    child_customers,
                )

            if parent_customer is None:
                unmatched_leads += 1

            rows_to_render = child_customers or [None]
            lead_item_rows: list[dict[str, Any]] = []
            for child in rows_to_render:
                child_id = int(child["customer_id"]) if child is not None else None
                child_key = f"daysmart:child:{child_id}" if child_id is not None else None
                all_registrations_for_child = registration_map.get(child_id or -1, [])
                registrations = []
                for registration in all_registrations_for_child:
                    registration_at = _parse_ts(
                        registration.get("event_start") or registration.get("created_at"),
                        naive_tz=LOCAL_TZ,
                    )
                    if started_at is not None and (registration_at is None or registration_at < started_at):
                        continue
                    registrations.append(registration)
                membership_labels = _membership_labels_after_started(
                    membership_map.get(child_id or -1, []),
                    started_at,
                )
                raw_membership_labels = _membership_labels_after_started(
                    program_membership_map.get(child_id or -1, []),
                    started_at,
                )
                checked_registration = None
                if include_attendance and child_id is not None and child_key is not None:
                    registrations_by_class_time = sorted(
                        registrations,
                        key=lambda item: (
                            _parse_ts(item.get("event_start") or item.get("created_at"), naive_tz=LOCAL_TZ)
                            or dt.datetime.max.replace(tzinfo=dt.timezone.utc)
                        ),
                    )
                    for candidate in registrations_by_class_time:
                        candidate_at = _parse_ts(
                            candidate.get("event_start") or candidate.get("created_at"),
                            naive_tz=LOCAL_TZ,
                        )
                        if candidate_at is None:
                            continue
                        candidate_checked, _candidate_timestamp = _registration_checked_in_from_cached_sources(
                            candidate,
                            child_id,
                            child_key,
                            roster_lookup,
                            api_checkin_lookup=api_checkin_lookup,
                            attendance_fallback=attendance_fallback,
                        )
                        if not candidate_checked:
                            candidate_day = candidate_at.astimezone(LOCAL_TZ).date()
                            candidate_day_key = candidate_day.isoformat()
                            if location_trial_checkins.get((candidate_day_key, str(child_id))):
                                candidate_checked = True
                            for checkin_dt in location_customer_checkins.get(str(child_id), []):
                                if checkin_dt.astimezone(LOCAL_TZ).date() == candidate_day:
                                    candidate_checked = True
                                    break
                        if candidate_checked:
                            checked_registration = candidate
                            break

                trial_registration = checked_registration or (registrations[0] if registrations else None)
                eventbrite_trial_registration = None
                if (
                    trial_registration is None
                    and source_system == "gmail_eventbrite"
                    and (lead.get("trial_class_name") or lead.get("trial_class_when"))
                ):
                    eventbrite_trial_registration = {
                        "event_name_clean": lead.get("trial_class_name"),
                        "event_start": lead.get("trial_class_when"),
                    }
                displayed_trial_registration = trial_registration or eventbrite_trial_registration
                trial_class_at = _parse_ts(
                    (displayed_trial_registration or {}).get("event_start")
                    or (displayed_trial_registration or {}).get("created_at"),
                    naive_tz=LOCAL_TZ,
                )
                checked_trial_at = None
                has_account = parent_customer is not None or child_id is not None
                has_scheduled = displayed_trial_registration is not None
                has_confirmed = bool(confirmation["confirmed"] and has_scheduled)
                has_membership = bool(membership_labels)

                if (
                    include_attendance
                    and trial_registration is not None
                    and trial_class_at is not None
                    and child_id is not None
                    and child_key is not None
                ):
                    checked_in, _checkin_timestamp = _registration_checked_in_from_cached_sources(
                        trial_registration,
                        child_id,
                        child_key,
                        roster_lookup,
                        api_checkin_lookup=api_checkin_lookup,
                        attendance_fallback=attendance_fallback,
                    )
                    if not checked_in:
                        trial_day = trial_class_at.astimezone(LOCAL_TZ).date()
                        trial_day_key = trial_day.isoformat()
                        if location_trial_checkins.get((trial_day_key, str(child_id))):
                            checked_in = True
                        for checkin_dt in location_customer_checkins.get(str(child_id), []):
                            if checkin_dt.astimezone(LOCAL_TZ).date() == trial_day:
                                checked_in = True
                                break
                    if checked_in:
                        checked_trial_at = trial_class_at

                if (
                    include_attendance
                    and checked_trial_at is None
                    and child_id is not None
                    and child_key is not None
                ):
                    historical_checkin_at = _first_checkin_after_lead_started(
                        customer_id=child_id,
                        child_key=child_key,
                        lead_started_at=started_at,
                        roster_customer_checkins=roster_customer_checkins,
                        api_checkin_lookup=api_checkin_lookup,
                        location_customer_checkins=location_customer_checkins,
                        attendance_fallback_lookup=attendance_fallback_lookup,
                    )
                    if historical_checkin_at is not None:
                        checked_trial_at = historical_checkin_at
                        historical_registration = _registration_for_checkin_day(
                            all_registrations_for_child,
                            historical_checkin_at,
                        )
                        if historical_registration is not None:
                            trial_registration = historical_registration
                            displayed_trial_registration = historical_registration
                            trial_class_at = _parse_ts(
                                historical_registration.get("event_start")
                                or historical_registration.get("created_at"),
                                naive_tz=LOCAL_TZ,
                            )
                        elif trial_registration is not None:
                            trial_registration = dict(trial_registration)
                            trial_registration["event_start"] = historical_checkin_at.isoformat()
                            displayed_trial_registration = trial_registration
                            trial_class_at = historical_checkin_at

                attendance_override_status = _attendance_override_status(
                    customer_id=child_id,
                    registration=trial_registration,
                )
                if attendance_override_status in {"no_show", "not_attended", "ignore_checkin"}:
                    checked_trial_at = None

                has_attended = checked_trial_at is not None
                has_no_show = bool(
                    include_attendance
                    and trial_class_at is not None
                    and now_local >= trial_class_at.astimezone(LOCAL_TZ) + TRIAL_MISSED_GRACE_PERIOD
                    and not has_attended
                    and not has_membership
                )
                free_trial_class_status = ""
                if trial_class_at is not None:
                    if has_attended:
                        free_trial_class_status = "checked_in"
                    elif has_no_show:
                        free_trial_class_status = "missed"
                    elif eventbrite_trial_registration is not None and trial_registration is None:
                        free_trial_class_status = "pending-registration"
                    else:
                        free_trial_class_status = "upcoming"

                display_parent_name = (parent_customer or {}).get("full_name") or lead.get("contact_name") or "--"
                display_child_name = child.get("full_name") if child is not None else None
                if source_system == "legacy_csv":
                    display_parent_name = legacy_parent_name or "--"
                    if display_child_name is None:
                        display_child_name = legacy_child_name
                elif source_system == "gmail_eventbrite" and display_child_name is None and eventbrite_children:
                    display_child_name = ", ".join(eventbrite_children)
                elif source_system == "daysmart_ftyc":
                    display_parent_name = (parent_customer or {}).get("full_name") or "--"

                item = {
                    "item_key": f"{lead['lead_key']}:child:{child_id}" if child_id is not None else lead["lead_key"],
                    "conversation_id": conversation_id,
                    "conversation_url": conversation_url,
                    "lead_started_at": started_at.isoformat() if started_at else None,
                    "lead_started_at_display": _format_local(started_at),
                    "last_interaction_at": last_interaction_at.isoformat() if last_interaction_at else None,
                    "last_interaction_at_display": _format_local(last_interaction_at),
                    "parent_name": display_parent_name,
                    "parent_phone": lead.get("contact_phone") or "--",
                    "parent_daysmart_url": _daysmart_account_url(
                        int(parent_customer["customer_id"]) if parent_customer is not None else None
                    ),
                    "child_name": display_child_name,
                    "child_daysmart_customer_id": child_id,
                    "child_daysmart_url": _daysmart_account_url(child_id),
                    "trial_status": lead.get("trial_status") or "unknown",
                    "conversation_confirmed": bool(confirmation["confirmed"]),
                    "latest_inbound_signal": confirmation["latest_inbound_signal"],
                    "latest_inbound_text": confirmation["latest_inbound_text"],
                    "free_trial_class_display": _display_registration(displayed_trial_registration),
                    "free_trial_class_status": free_trial_class_status,
                    "attended_at": checked_trial_at.isoformat() if isinstance(checked_trial_at, dt.datetime) else None,
                    "attended_at_display": _format_local(
                        checked_trial_at
                    ) if isinstance(checked_trial_at, dt.datetime) else "--",
                    "has_account": has_account,
                    "has_scheduled": has_scheduled,
                    "has_confirmed": has_confirmed,
                    "has_attended": has_attended,
                    "has_no_show": has_no_show,
                    "attendance_pending": not include_attendance,
                    "has_membership": has_membership,
                    "memberships": membership_labels,
                    "memberships_display": ", ".join(membership_labels) if membership_labels else "--",
                    "all_memberships": raw_membership_labels,
                }
                items.append(item)
                lead_item_rows.append(item)

            lead_status = _lead_status(
                closed_at=closed_at,
                has_account=any(row["has_account"] for row in lead_item_rows),
                has_scheduled=any(row["has_scheduled"] for row in lead_item_rows),
                has_attended=any(row["has_attended"] for row in lead_item_rows),
                has_no_show=any(row["has_no_show"] for row in lead_item_rows),
                has_membership=any(row["has_membership"] for row in lead_item_rows),
                last_interaction_at=last_interaction_at,
                now=now_utc,
            )

            lead_groups[conversation_id] = {
                "conversation_id": conversation_id,
                "contact_name": lead.get("contact_name") or "--",
                "status": lead_status,
                "has_account": any(row["has_account"] for row in lead_item_rows),
                "has_scheduled": any(row["has_scheduled"] for row in lead_item_rows),
                "has_confirmed": any(row["has_confirmed"] for row in lead_item_rows),
                "has_attended": any(row["has_attended"] for row in lead_item_rows),
                "has_no_show": any(row["has_no_show"] for row in lead_item_rows),
                "has_membership": any(row["has_membership"] for row in lead_item_rows),
                "membership_count": sum(1 for row in lead_item_rows if row["has_membership"]),
            }

        for item in items:
            item["lead_status"] = lead_groups[item["conversation_id"]]["status"]

    items = _dedupe_dashboard_items(items)

    status_counts = Counter(group["status"] for group in lead_groups.values())
    total_leads = len(lead_groups)
    accounts_created = sum(1 for group in lead_groups.values() if group["has_account"])
    scheduled = sum(1 for group in lead_groups.values() if group["has_scheduled"])
    confirmed = sum(1 for group in lead_groups.values() if group["has_confirmed"])
    attended = sum(1 for group in lead_groups.values() if group["has_attended"])
    memberships = sum(int(group.get("membership_count", 0)) for group in lead_groups.values())
    no_shows = sum(1 for group in lead_groups.values() if group["has_no_show"])

    items.sort(
        key=lambda item: (
            item.get("lead_started_at") or "",
            item.get("parent_name") or "",
            item.get("child_name") or "",
        ),
        reverse=True,
    )

    summary_cards = _summary_cards_for_window(
        window_label=window_label,
        total_leads=total_leads,
        accounts_created=accounts_created,
        attended=attended,
        memberships=memberships,
    )

    conversation_statuses = _conversation_status_items(status_counts)

    display_start_date = start_date
    if normalized_window == "all_time" and matched_rows:
        started_dates = [
            started_at.astimezone(LOCAL_TZ).date()
            for started_at in (
                _effective_lead_timestamp(
                    row.get("conversation_first_message_at"),
                    row.get("conversation_started_at"),
                    row.get("last_interaction_at"),
                    row.get("conversation_last_message_at"),
                )
                for row in matched_rows
            )
            if started_at is not None
        ]
        if started_dates:
            display_start_date = min(started_dates)

    dashboard = {
        "window": {
            "days": resolved_days,
            "label": window_label,
            "kind": normalized_window or "days",
            "start_date": display_start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "generated_at": now_utc.isoformat(),
        },
        "summary": {
            "lead_conversations": total_leads,
            "account_created": accounts_created,
            "confirmed_trials": confirmed,
            "attended_trials": attended,
            "no_shows": no_shows,
            "memberships": memberships,
            "unmatched_leads": unmatched_leads,
            "child_rows": sum(1 for item in items if item.get("child_name")),
        },
        "summary_cards": summary_cards,
        "conversation_statuses": conversation_statuses,
        "data_quality": {
            "attendance_source": attendance_source,
            "attendance_deferred": not include_attendance,
            "matched_lead_count": accounts_created,
            "unmatched_lead_count": unmatched_leads,
            "detail_rows": len(items),
        },
        "items": items,
    }
    if cached_dashboard is not None and force_refresh:
        dashboard = _merge_cached_dashboard(
            cached_dashboard,
            dashboard,
            include_attendance=include_attendance,
        )
    _cached_dashboard_set(cache_key, dashboard)
    return dashboard


def build_youth_kpi_email_preview(
    db_path: str,
    *,
    youth_inbox_id: int,
    days: int = 7,
    window: str | None = None,
) -> dict[str, str]:
    dashboard = build_youth_kpi_dashboard(
        db_path,
        youth_inbox_id=youth_inbox_id,
        days=days,
        window=window,
    )
    window = dashboard["window"]
    summary = dashboard["summary"]
    statuses = dashboard["conversation_statuses"]
    items = dashboard["items"][:12]

    start_label = dt.date.fromisoformat(window["start_date"]).strftime("%b %-d")
    end_label = dt.date.fromisoformat(window["end_date"]).strftime("%b %-d, %Y")
    subject = f"Youth Lead Funnel - {start_label} to {end_label}"

    lines = [
        f"Youth lead funnel for {start_label} to {end_label}",
        "",
        f"New leads: {summary['lead_conversations']}",
        f"DaySmart accounts created: {summary['account_created']}",
        f"Checked in: {summary['attended_trials']}",
        f"No-shows: {summary['no_shows']}",
        f"Memberships purchased: {summary['memberships']}",
        "",
        "Conversation status:",
    ]
    for status in statuses:
        lines.append(f"- {status['label']}: {status['count']}")

    if items:
        lines.extend(["", "Recent lead detail:"])
        for item in items:
            child_name = f" / {item['child_name']}" if item.get("child_name") else ""
            lines.append(f"- {item['parent_name']}{child_name} | {item['lead_status']}")

    return {
        "subject": subject,
        "body": "\n".join(lines),
    }


def _timeseries_bucket_bounds(bucket_start: dt.date, granularity: str) -> tuple[dt.date, dt.date]:
    if granularity == "week":
        bucket_end = bucket_start + dt.timedelta(days=6)
    elif granularity == "quarter":
        next_month = bucket_start.month + 3
        next_year = bucket_start.year
        if next_month > 12:
            next_month -= 12
            next_year += 1
        bucket_end = dt.date(next_year, next_month, 1) - dt.timedelta(days=1)
    elif granularity == "year":
        bucket_end = dt.date(bucket_start.year, 12, 31)
    else:
        if bucket_start.month == 12:
            next_month = dt.date(bucket_start.year + 1, 1, 1)
        else:
            next_month = dt.date(bucket_start.year, bucket_start.month + 1, 1)
        bucket_end = next_month - dt.timedelta(days=1)
    return bucket_start, bucket_end


def _timeseries_bucket_start(day: dt.date, granularity: str) -> dt.date:
    if granularity == "week":
        return day - dt.timedelta(days=day.weekday())
    if granularity == "quarter":
        quarter_start_month = ((day.month - 1) // 3) * 3 + 1
        return dt.date(day.year, quarter_start_month, 1)
    if granularity == "year":
        return dt.date(day.year, 1, 1)
    return dt.date(day.year, day.month, 1)


def _timeseries_bucket_label(bucket_start: dt.date, granularity: str) -> str:
    if granularity == "week":
        return f"Week of {bucket_start.strftime('%-m/%-d/%y')}"
    if granularity == "quarter":
        quarter = ((bucket_start.month - 1) // 3) + 1
        return f"Q{quarter} {bucket_start.year}"
    if granularity == "year":
        return str(bucket_start.year)
    return bucket_start.strftime("%b %Y")


def build_youth_kpi_timeseries(
    db_path: str,
    *,
    youth_inbox_id: int,
    days: int = 7,
    window: str | None = None,
    granularity: str = "month",
    force_refresh: bool = False,
) -> dict[str, Any]:
    normalized_granularity = (granularity or "month").strip().lower()
    if normalized_granularity not in {"week", "month", "quarter", "year"}:
        normalized_granularity = "month"
    normalized_window = (window or "").strip().lower()

    cache_key = (
        "timeseries",
        YOUTH_KPI_CACHE_VERSION,
        str(db_path),
        youth_inbox_id,
        normalized_window or "days",
        days,
        normalized_granularity,
    )
    cached_payload = _cached_dashboard_get(cache_key)
    if cached_payload is not None and not force_refresh:
        return cached_payload

    dashboard = build_youth_kpi_dashboard(
        db_path,
        youth_inbox_id=youth_inbox_id,
        days=days,
        window=window,
        include_attendance=True,
        force_refresh=force_refresh,
    )

    lead_groups: dict[int, dict[str, Any]] = {}
    for item in dashboard.get("items", []):
        conversation_id = item.get("conversation_id")
        if not isinstance(conversation_id, int):
            continue
        lead_dt = _effective_lead_timestamp(
            item.get("lead_started_at"),
            item.get("last_interaction_at"),
        )
        if lead_dt is None:
            continue
        lead_day = lead_dt.astimezone(LOCAL_TZ).date()
        group = lead_groups.setdefault(
            conversation_id,
            {
                "lead_day": lead_day,
                "has_attended": False,
                "has_membership": False,
                "membership_count": 0,
            },
        )
        if lead_day < group["lead_day"]:
            group["lead_day"] = lead_day
        group["has_attended"] = bool(group["has_attended"] or item.get("has_attended"))
        group["has_membership"] = bool(group["has_membership"] or item.get("has_membership"))
        if item.get("has_membership"):
            group["membership_count"] += 1

    buckets: dict[dt.date, dict[str, Any]] = {}
    for group in lead_groups.values():
        bucket_start = _timeseries_bucket_start(group["lead_day"], normalized_granularity)
        bucket = buckets.setdefault(
            bucket_start,
            {
                "start_date": bucket_start,
                "leads": 0,
                "checked_in": 0,
                "memberships": 0,
            },
        )
        bucket["leads"] += 1
        if group["has_attended"]:
            bucket["checked_in"] += 1
        bucket["memberships"] += int(group.get("membership_count", 0))

    items: list[dict[str, Any]] = []
    for bucket_start in sorted(buckets):
        bucket = buckets[bucket_start]
        leads = int(bucket["leads"])
        checked_in = int(bucket["checked_in"])
        memberships = int(bucket["memberships"])
        start_date, end_date = _timeseries_bucket_bounds(bucket_start, normalized_granularity)
        items.append(
            {
                "label": _timeseries_bucket_label(bucket_start, normalized_granularity),
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "leads": leads,
                "checked_in": checked_in,
                "checkin_percentage": round((checked_in / leads) * 100, 1) if leads else 0.0,
                "memberships": memberships,
                "membership_percentage": round((memberships / checked_in) * 100, 1) if checked_in else 0.0,
            }
        )

    payload = {
        "granularity": normalized_granularity,
        "window_label": dashboard.get("window", {}).get("label", "All Time"),
        "window_start_date": dashboard.get("window", {}).get("start_date"),
        "window_end_date": dashboard.get("window", {}).get("end_date"),
        "totals": {
            "leads": dashboard.get("summary", {}).get("lead_conversations", 0),
            "checked_in": dashboard.get("summary", {}).get("attended_trials", 0),
            "checkin_percentage": round(
                (
                    (dashboard.get("summary", {}).get("attended_trials", 0)
                    / max(dashboard.get("summary", {}).get("lead_conversations", 0), 1))
                    * 100
                ),
                1,
            ) if dashboard.get("summary", {}).get("lead_conversations", 0) else 0.0,
            "memberships": dashboard.get("summary", {}).get("memberships", 0),
            "membership_percentage": round(
                (
                    (dashboard.get("summary", {}).get("memberships", 0)
                    / max(dashboard.get("summary", {}).get("attended_trials", 0), 1))
                    * 100
                ),
                1,
            ) if dashboard.get("summary", {}).get("attended_trials", 0) else 0.0,
        },
        "items": items,
    }
    _cached_dashboard_set(cache_key, payload)
    return payload
