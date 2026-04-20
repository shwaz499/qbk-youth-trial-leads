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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .analysis import answer_locally, answer_with_llm, get_recent_messages, search_messages
from .config import get_settings
from .db import get_conn, init_db
from .daysmart import DaysmartApiError, DaysmartClient
from .ingest import sync_conversations
from .salesmessage import SalesmessageApiError, SalesmessageClient
from .unified import recompute_risk_alerts, sync_daysmart_to_unified, sync_salesmessage_to_unified
from .youth_kpis import build_youth_kpi_dashboard, build_youth_kpi_email_preview

app = FastAPI(title="Salesmessage AI Agent", version="0.1.0")
settings = get_settings()
init_db(settings.database_url)
static_dir = Path(__file__).parent / "static"
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


class SyncRequest(BaseModel):
    filters: list[str] = Field(
        default_factory=lambda: ["open", "closed"]
    )
    inbox_ids: list[int] | None = None
    min_last_message_at: str | None = LEAD_CUTOFF_TIMESTAMP
    conversation_page_size: int = 100
    message_page_size: int = 0
    max_message_pages_per_conversation: int = 0


class HostedSyncRequest(SyncRequest):
    daysmart_max_pages: int = 6
    daysmart_page_size: int = 100


class AskRequest(BaseModel):
    question: str
    search_query: str | None = None
    conversation_id: int | None = None
    max_context_messages: int = 30


class RiskRecomputeRequest(BaseModel):
    inactivity_days: int = 14
    outreach_days: int = 30


class DaySmartSyncRequest(BaseModel):
    max_pages: int = 6
    page_size: int = 100


class TrialLeadUpdateRequest(BaseModel):
    trial_status: str | None = None
    account_created: bool | None = None
    added_to_class: bool | None = None


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


@app.middleware("http")
async def require_auth(request: Request, call_next):
    public_paths = {"/", "/health", "/api/auth/status", "/api/login"}
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


def _find_daysmart_matches(conn: Any, lead_row: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    metadata = _json_loads(lead_row.get("metadata_json"), {})
    contact = metadata.get("contact") if isinstance(metadata, dict) else {}
    if not isinstance(contact, dict):
        contact = {}

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

    cutoff = dt.date.today().replace(year=dt.date.today().year - 21)
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

    if parent is None and email:
        row = conn.execute(
            """
            SELECT *
            FROM daysmart_customers
            WHERE normalized_email = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (email,),
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
        if row is not None:
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


def _is_youth_class_name(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return any(token in lowered for token in ("seals", "cubs", "beach lions"))


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


def _daysmart_first_class(conn: Any, customer_ids: list[int]) -> tuple[str | None, str | None]:
    if not customer_ids:
        return None, None
    placeholders = ",".join("?" for _ in customer_ids)
    rows = conn.execute(
        f"""
        SELECT registration_id, team_or_event_id, event_name, event_start, created_at
        FROM daysmart_class_registrations
        WHERE source_type = 'event_registration'
          AND customer_id IN ({placeholders})
        ORDER BY coalesce(event_start, ''), coalesce(created_at, '')
        """,
        customer_ids,
    ).fetchall()
    for row in rows:
        row_dict = dict(row)
        registration_id = int(row_dict["registration_id"])
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
        if name or start_at:
            return name, _format_daysmart_class_date(start_at)
    return None, None


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


def _daysmart_memberships(conn: Any, customer_ids: list[int]) -> list[str]:
    if not customer_ids:
        return []
    placeholders = ",".join("?" for _ in customer_ids)
    rows = conn.execute(
        f"""
        SELECT m.product_name, m.expires_at, m.created_at
        FROM daysmart_memberships m
        JOIN daysmart_customer_memberships cm ON cm.membership_id = m.membership_id
        WHERE cm.customer_id IN ({placeholders})
        ORDER BY coalesce(m.expires_at, m.created_at, '') DESC, m.membership_id DESC
        """,
        customer_ids,
    ).fetchall()
    seen: set[str] = set()
    items: list[str] = []
    for row in rows:
        base = (row["product_name"] or "").strip() or "Unnamed membership"
        expires = (row["expires_at"] or "").strip()
        label = f"{base} ({expires[:10]})" if expires else base
        if label in seen:
            continue
        seen.add(label)
        items.append(label)
    return items


def _live_customer_memberships(customer_id: int) -> list[str]:
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

    membership_labels: list[str] = []
    seen: set[str] = set()
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
            base = (product_name or "").strip() or "Unnamed membership"
            expires = (attrs.get("expires") or attrs.get("term_date") or "").strip()
            label = f"{base} ({expires[:10]})" if expires else base
            if label not in seen:
                seen.add(label)
                membership_labels.append(label)

    return membership_labels


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
                conversation_page_size=req.conversation_page_size,
                message_page_size=req.message_page_size,
                max_message_pages_per_conversation=req.max_message_pages_per_conversation,
            )
        )
        _set_sync_state(stage="syncing_daysmart", last_result={"salesmessage": salesmessage_result})
        daysmart_result = sync_daysmart(
            DaySmartSyncRequest(
                max_pages=req.daysmart_max_pages,
                page_size=req.daysmart_page_size,
            )
        )
        _set_sync_state(
            running=False,
            stage="completed",
            finished_at=dt.datetime.now(dt.timezone.utc).isoformat(),
            last_result={"salesmessage": salesmessage_result, "daysmart": daysmart_result},
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
    return FileResponse(static_dir / "youth-kpis.html")


@app.get("/youth-kpis")
def youth_kpis_ui() -> FileResponse:
    return FileResponse(static_dir / "youth-kpis.html")


@app.get("/api/auth/status")
def auth_status(request: Request) -> dict[str, bool]:
    return {"authenticated": _is_authenticated(request)}


@app.post("/api/login")
def login(req: LoginRequest, request: Request) -> JSONResponse:
    if req.password != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="Incorrect password.")
    response = JSONResponse({"ok": True, "authenticated": True})
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
            target_inbox_ids=set(req.inbox_ids or [settings.youth_inbox_id]),
            min_last_message_at=req.min_last_message_at,
            progress_callback=_set_sync_state,
        )
    except SalesmessageApiError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _set_sync_state(stage="rebuilding_youth_leads")
    unified_stats = sync_salesmessage_to_unified(
        settings.database_url,
        settings.youth_inbox_id,
        cutoff_date=LEAD_CUTOFF_DATE,
    )
    _set_sync_state(stage="recomputing_risk")
    risk_stats = recompute_risk_alerts(settings.database_url)
    return {"ok": True, **stats, "unified": unified_stats, "risk": risk_stats}


@app.post("/sync/start")
def sync_start(req: HostedSyncRequest) -> dict[str, Any]:
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
    risk_stats = recompute_risk_alerts(settings.database_url)
    return {"ok": True, **stats, "risk": risk_stats}


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
            "SELECT count(*) AS c FROM youth_trial_leads WHERE inbox_id = ?",
            (settings.youth_inbox_id,),
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
def dashboard_trial_leads(limit: int = 1000) -> dict[str, Any]:
    sql = """
        SELECT t.lead_key, t.family_key, t.inbox_id, t.contact_name, t.contact_phone, t.trial_status,
               account_created, added_to_class, trial_class_name, trial_class_when,
               last_interaction_at, t.updated_at, source_ref, metadata_json,
               c.started_at AS conversation_started_at
        FROM youth_trial_leads t
        LEFT JOIN conversations c ON c.id = CAST(t.source_ref AS INTEGER)
        WHERE t.inbox_id = ?
          AND (
            coalesce(c.started_at, '') >= ?
            OR coalesce(t.last_interaction_at, '') >= ?
          )
    """
    params: list[Any] = [settings.youth_inbox_id, LEAD_CUTOFF_DATE, LEAD_CUTOFF_DATE]
    sql += " ORDER BY coalesce(last_interaction_at, '') DESC LIMIT ?"
    params.append(limit)

    with get_conn(settings.database_url) as conn:
        rows = conn.execute(sql, params).fetchall()
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["conversation_id"] = int(item["source_ref"])
            item["conversation_url"] = f"https://app.salesmessage.com/conversations/{item['source_ref']}"
            item["trial_class_when_display"] = _normalize_trial_class_when(item.get("trial_class_when"))
            parent_customer, child_customers = _find_daysmart_matches(conn, item)

            if parent_customer is None:
                continue

            if parent_customer is not None:
                item["parent_daysmart_customer_id"] = parent_customer["customer_id"]
                item["parent_daysmart_url"] = _daysmart_account_url(int(parent_customer["customer_id"]))
            else:
                item["parent_daysmart_customer_id"] = None
                item["parent_daysmart_url"] = None

            row_children: list[dict[str, Any] | None] = child_customers or [None]
            for child_customer in row_children:
                row_item = dict(item)
                memberships: list[str] = []

                if child_customer is not None:
                    row_item["lead_key"] = f"{item['lead_key']}:child:{child_customer['customer_id']}"
                    row_item["child_name"] = child_customer.get("full_name")
                    row_item["child_daysmart_customer_id"] = child_customer["customer_id"]
                    row_item["child_daysmart_url"] = _daysmart_account_url(int(child_customer["customer_id"]))
                else:
                    row_item["child_name"] = None
                    row_item["child_daysmart_customer_id"] = None
                    row_item["child_daysmart_url"] = None

                related_customer_ids = _related_daysmart_customer_ids(parent_customer, child_customer)
                membership_customer_ids = (
                    [int(child_customer["customer_id"])]
                    if child_customer is not None
                    else related_customer_ids
                )
                if child_customer is not None:
                    try:
                        memberships = _live_customer_memberships(int(child_customer["customer_id"]))
                    except Exception:
                        memberships = []
                if not memberships and membership_customer_ids:
                    memberships = _daysmart_memberships(conn, membership_customer_ids)
                first_class_name, first_class_date = _daysmart_first_class(conn, related_customer_ids)
                row_item["free_trial_class_name"] = first_class_name
                row_item["free_trial_class_date"] = first_class_date
                if first_class_name and first_class_date:
                    row_item["free_trial_class_display"] = f"{first_class_name} - {first_class_date}"
                else:
                    row_item["free_trial_class_display"] = first_class_name or first_class_date or ""

                row_item["memberships"] = memberships
                row_item["has_membership"] = bool(memberships)
                row_item["memberships_display"] = ", ".join(memberships) if memberships else "--"
                items.append(row_item)
    return {"items": items}


@app.get("/dashboard/youth-kpis")
def dashboard_youth_kpis(days: int = 7) -> dict[str, Any]:
    return build_youth_kpi_dashboard(
        settings.database_url,
        youth_inbox_id=settings.youth_inbox_id,
        days=days,
    )


@app.get("/dashboard/youth-kpis/email-preview")
def dashboard_youth_kpi_email_preview(days: int = 7) -> dict[str, Any]:
    preview = build_youth_kpi_email_preview(
        settings.database_url,
        youth_inbox_id=settings.youth_inbox_id,
        days=days,
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
