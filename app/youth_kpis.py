from __future__ import annotations

import datetime as dt
import json
import os
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .config import get_settings
from .db import get_conn
from .daysmart import DaysmartApiError, DaysmartClient

LOCAL_TZ = ZoneInfo("America/New_York")
YOUTH_CLASS_TOKENS = ("seals", "cubs", "beach lions")
CHECKIN_MATCH_WINDOW_HOURS = 6
ADMIN_LOGIN_URL = "https://apps.daysmartrecreation.com/dash/admin/index.php?Action=Auth/login"
ADMIN_LOGIN_VALIDATE_URL = "https://apps.daysmartrecreation.com/dash/admin/index.php?Action=Auth/validateLogin.json&extension=json"
ADMIN_LOCATION_CHECKIN_REPORT_URL = "https://apps.daysmartrecreation.com/dash/admin/index.php?Action=Report/locationCheckIn&company={company}"
REPORT_TABLE_RE = re.compile(r'<table[^>]+id="results-table"[^>]*>.*?<tbody>(?P<tbody>.*?)</tbody>', re.S | re.I)
REPORT_ROW_RE = re.compile(r"<tr[^>]*>(?P<row>.*?)</tr>", re.S | re.I)
REPORT_CELL_RE = re.compile(r"<td[^>]*>(?P<cell>.*?)</td>", re.S | re.I)


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def _clean_text(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    text = value.replace("\u2019", "'")
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


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


def _daysmart_client() -> DaysmartClient:
    settings = get_settings()
    return DaysmartClient(
        client_id=settings.daysmart_api_client_id,
        client_secret=settings.daysmart_api_secret,
        base_url=settings.daysmart_base_url,
    )


def _strip_html(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text)).strip()


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

    adult_cutoff = dt.date.today().replace(year=dt.date.today().year - 21)
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
            if birthdate is not None and birthdate >= adult_cutoff:
                age_candidates.append((birthdate, row))
        if age_candidates:
            age_candidates.sort(key=lambda item: item[0], reverse=True)
            children = [row for _, row in age_candidates]
        elif len(non_parent) == 1:
            children = [non_parent[0]]

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
    lowered = value.lower()
    return any(token in lowered for token in YOUTH_CLASS_TOKENS)


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
    day_count = 0
    error_count = 0
    last_error = None

    for event_day in sorted(days):
        day_count += 1
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
                lookup.setdefault((event_day, customer_id), []).append(visit_dt.isoformat())
                day_hits += 1
            if day_hits == 0:
                continue
            for key, values in list(lookup.items()):
                if key[0] == event_day:
                    values.sort()
        except Exception as exc:
            error_count += 1
            last_error = str(exc)[:240]

    meta = {
        "source": "daysmart-location-checkins",
        "entries": len(lookup),
        "days": day_count,
        "errors": error_count,
    }
    if last_error:
        meta["detail"] = last_error
    return lookup, meta


def _load_attendance_fallback(conn: Any) -> set[tuple[str, str]]:
    rows = conn.execute(
        """
        SELECT child_key, event_at
        FROM youth_attendance_events
        """
    ).fetchall()
    items: set[tuple[str, str]] = set()
    for row in rows:
        event_dt = _parse_ts(row["event_at"], naive_tz=LOCAL_TZ)
        if event_dt is None:
            continue
        items.add((str(row["child_key"]), event_dt.astimezone(LOCAL_TZ).date().isoformat()))
    return items


def _load_membership_map(conn: Any, customer_ids: set[int]) -> dict[int, list[str]]:
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
    membership_map: dict[int, list[str]] = {}
    seen: dict[int, set[str]] = {}
    for row in rows:
        customer_id = int(row["customer_id"])
        seen.setdefault(customer_id, set())
        base = (row["product_name"] or "").strip() or "Unnamed membership"
        expires = (row["expires_at"] or "").strip()
        label = f"{base} ({expires[:10]})" if expires else base
        if label in seen[customer_id]:
            continue
        seen[customer_id].add(label)
        membership_map.setdefault(customer_id, []).append(label)
    return membership_map


def _load_registration_map(conn: Any, customer_ids: set[int]) -> dict[int, list[dict[str, Any]]]:
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
        name = _clean_trial_class_name(item.get("event_name"))
        if not _is_youth_class_name(name):
            continue
        item["event_name_clean"] = name
        registration_map.setdefault(int(item["customer_id"]), []).append(item)
    return registration_map


def _registration_checked_in_from_cached_sources(
    registration: dict[str, Any],
    customer_id: int,
    child_key: str,
    roster_lookup: dict[tuple[str, str | None, str], dict[str, Any]],
    api_checkin_lookup: dict[str, list[dict[str, Any]]],
    attendance_fallback: set[tuple[str, str]],
) -> tuple[bool, str | None]:
    event_dt = _parse_ts(
        registration.get("event_start") or registration.get("created_at"),
        naive_tz=LOCAL_TZ,
    )
    if event_dt is None:
        return False, None
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
    if (child_key, event_day) in attendance_fallback:
        return True, event_day
    return False, None


def _registration_checked_in(
    registration: dict[str, Any],
    customer_id: int,
    child_key: str,
    roster_lookup: dict[tuple[str, str | None, str], dict[str, Any]],
    location_checkin_lookup: dict[tuple[str, str], list[str]],
    api_checkin_lookup: dict[str, list[dict[str, Any]]],
    attendance_fallback: set[tuple[str, str]],
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


def build_youth_kpi_dashboard(
    db_path: str,
    *,
    youth_inbox_id: int,
    days: int = 7,
) -> dict[str, Any]:
    days = max(1, min(days, 90))
    now_utc = dt.datetime.now(dt.timezone.utc)
    window_start = (now_utc - dt.timedelta(days=days)).date().isoformat()

    roster_lookup, roster_source = _load_roster_checkins()
    with get_conn(db_path) as conn:
        lead_rows = conn.execute(
            """
            SELECT
                t.lead_key,
                t.family_key,
                t.contact_name,
                t.contact_phone,
                t.trial_status,
                t.last_interaction_at,
                t.source_ref,
                t.metadata_json,
                c.started_at AS conversation_started_at,
                c.closed_at AS conversation_closed_at,
                c.last_message_at AS conversation_last_message_at
            FROM youth_trial_leads t
            LEFT JOIN conversations c ON c.id = CAST(t.source_ref AS INTEGER)
            WHERE t.inbox_id = ?
              AND coalesce(c.started_at, '') >= ?
            ORDER BY coalesce(c.started_at, '') DESC, CAST(t.source_ref AS INTEGER) DESC
            """,
            (youth_inbox_id, window_start),
        ).fetchall()

        matched_rows: list[dict[str, Any]] = []
        all_customer_ids: set[int] = set()
        for row in lead_rows:
            lead = dict(row)
            parent_customer, child_customers = _find_daysmart_matches(conn, lead)
            lead["parent_customer"] = parent_customer
            lead["child_customers"] = child_customers
            matched_rows.append(lead)
            if parent_customer is not None:
                all_customer_ids.add(int(parent_customer["customer_id"]))
            for child in child_customers:
                all_customer_ids.add(int(child["customer_id"]))

        membership_map = _load_membership_map(conn, all_customer_ids)
        registration_map = _load_registration_map(conn, all_customer_ids)
        api_checkin_lookup, api_source = _load_daysmart_api_checkins()
        attendance_fallback = _load_attendance_fallback(conn)
        location_checkin_days: set[str] = set()
        now_local = now_utc.astimezone(LOCAL_TZ)
        for customer_id, registrations in registration_map.items():
            for registration in registrations:
                event_dt = _parse_ts(
                    registration.get("event_start") or registration.get("created_at"),
                    naive_tz=LOCAL_TZ,
                )
                if event_dt is None or event_dt.astimezone(LOCAL_TZ) > now_local:
                    continue
                checked_in, _ = _registration_checked_in_from_cached_sources(
                    registration,
                    customer_id,
                    f"daysmart:child:{customer_id}",
                    roster_lookup,
                    api_checkin_lookup,
                    attendance_fallback,
                )
                if checked_in:
                    continue
                event_day = event_dt.astimezone(LOCAL_TZ).date().isoformat()
                location_checkin_days.add(event_day)
        location_checkin_lookup, location_source = _load_location_report_checkins(location_checkin_days)

        attendance_source = {
            "source": "roster-cache + daysmart-location-checkins + daysmart-checkin-events",
            "entries": int(roster_source.get("entries", 0))
            + int(location_source.get("entries", 0))
            + int(api_source.get("entries", 0)),
            "roster_entries": int(roster_source.get("entries", 0)),
            "location_entries": int(location_source.get("entries", 0)),
            "location_days": int(location_source.get("days", 0)),
            "location_errors": int(location_source.get("errors", 0)),
            "api_entries": int(api_source.get("entries", 0)),
            "path": roster_source.get("path"),
            "location_status": location_source.get("source"),
            "api_status": api_source.get("source"),
        }
        if location_source.get("detail"):
            attendance_source["location_detail"] = location_source.get("detail")
        if api_source.get("detail"):
            attendance_source["api_detail"] = api_source.get("detail")

        items: list[dict[str, Any]] = []
        lead_groups: dict[int, dict[str, Any]] = {}
        unmatched_leads = 0

        for lead in matched_rows:
            conversation_id = int(lead["source_ref"])
            started_at = _parse_ts(lead.get("conversation_started_at"))
            closed_at = _parse_ts(lead.get("conversation_closed_at"))
            last_interaction_at = _parse_ts(
                lead.get("last_interaction_at") or lead.get("conversation_last_message_at")
            )
            confirmation = _conversation_confirmation(conn, conversation_id, lead.get("trial_status"))
            parent_customer = lead.get("parent_customer")
            child_customers = lead.get("child_customers") or []

            if parent_customer is None:
                unmatched_leads += 1

            rows_to_render = child_customers or [None]
            lead_item_rows: list[dict[str, Any]] = []
            for child in rows_to_render:
                child_id = int(child["customer_id"]) if child is not None else None
                child_key = f"daysmart:child:{child_id}" if child_id is not None else None
                registrations = registration_map.get(child_id or -1, [])
                membership_labels = membership_map.get(child_id or -1, [])
                attended_registration = None
                attended_at = None
                future_registrations: list[dict[str, Any]] = []
                past_unattended: list[dict[str, Any]] = []

                for registration in registrations:
                    event_dt = _parse_ts(
                        registration.get("event_start") or registration.get("created_at"),
                        naive_tz=LOCAL_TZ,
                    )
                    if event_dt is None:
                        continue
                    checked_in, checked_in_at = _registration_checked_in(
                        registration,
                        child_id or 0,
                        child_key or "",
                        roster_lookup,
                        location_checkin_lookup,
                        api_checkin_lookup,
                        attendance_fallback,
                    )
                    if checked_in and attended_registration is None:
                        attended_registration = registration
                        attended_at = checked_in_at
                    elif event_dt.astimezone(LOCAL_TZ) >= now_local:
                        future_registrations.append(registration)
                    else:
                        past_unattended.append(registration)

                future_registrations.sort(
                    key=lambda item: _parse_ts(
                        item.get("event_start") or item.get("created_at"),
                        naive_tz=LOCAL_TZ,
                    ) or now_utc
                )
                past_unattended.sort(
                    key=lambda item: _parse_ts(
                        item.get("event_start") or item.get("created_at"),
                        naive_tz=LOCAL_TZ,
                    ) or now_utc,
                    reverse=True,
                )

                scheduled_registration = future_registrations[0] if future_registrations else None
                recent_past_registration = past_unattended[0] if past_unattended else None
                displayed_registration = scheduled_registration or attended_registration or recent_past_registration

                has_account = parent_customer is not None
                has_scheduled = bool(registrations)
                has_confirmed = bool(confirmation["confirmed"] and has_scheduled)
                has_attended = attended_registration is not None
                has_membership = bool(membership_labels)
                has_no_show = bool(
                    recent_past_registration is not None
                    and attended_registration is None
                    and not future_registrations
                    and not has_membership
                )

                item = {
                    "item_key": f"{lead['lead_key']}:child:{child_id}" if child_id is not None else lead["lead_key"],
                    "conversation_id": conversation_id,
                    "conversation_url": f"https://app.salesmessage.com/conversations/{conversation_id}",
                    "lead_started_at": started_at.isoformat() if started_at else None,
                    "lead_started_at_display": _format_local(started_at),
                    "last_interaction_at": last_interaction_at.isoformat() if last_interaction_at else None,
                    "last_interaction_at_display": _format_local(last_interaction_at),
                    "parent_name": lead.get("contact_name") or (parent_customer or {}).get("full_name") or "--",
                    "parent_phone": lead.get("contact_phone") or "--",
                    "parent_daysmart_url": _daysmart_account_url(
                        int(parent_customer["customer_id"]) if parent_customer is not None else None
                    ),
                    "child_name": child.get("full_name") if child is not None else None,
                    "child_daysmart_url": _daysmart_account_url(child_id),
                    "trial_status": lead.get("trial_status") or "unknown",
                    "conversation_confirmed": bool(confirmation["confirmed"]),
                    "latest_inbound_signal": confirmation["latest_inbound_signal"],
                    "latest_inbound_text": confirmation["latest_inbound_text"],
                    "free_trial_class_display": _display_registration(displayed_registration),
                    "scheduled_class_display": _display_registration(scheduled_registration),
                    "attended_class_display": _display_registration(attended_registration),
                    "attended_at": attended_at,
                    "attended_at_display": _format_local(
                        _parse_ts(attended_at, naive_tz=LOCAL_TZ)
                    ) if attended_at else "--",
                    "has_account": has_account,
                    "has_scheduled": has_scheduled,
                    "has_confirmed": has_confirmed,
                    "has_attended": has_attended,
                    "has_no_show": has_no_show,
                    "has_membership": has_membership,
                    "memberships": membership_labels,
                    "memberships_display": ", ".join(membership_labels) if membership_labels else "--",
                }
                items.append(item)
                lead_item_rows.append(item)

            lead_status = _lead_status(
                closed_at=closed_at,
                has_account=parent_customer is not None,
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
                "has_account": parent_customer is not None,
                "has_scheduled": any(row["has_scheduled"] for row in lead_item_rows),
                "has_confirmed": any(row["has_confirmed"] for row in lead_item_rows),
                "has_attended": any(row["has_attended"] for row in lead_item_rows),
                "has_no_show": any(row["has_no_show"] for row in lead_item_rows),
                "has_membership": any(row["has_membership"] for row in lead_item_rows),
            }

        for item in items:
            item["lead_status"] = lead_groups[item["conversation_id"]]["status"]

    status_counts = Counter(group["status"] for group in lead_groups.values())
    total_leads = len(lead_groups)
    accounts_created = sum(1 for group in lead_groups.values() if group["has_account"])
    scheduled = sum(1 for group in lead_groups.values() if group["has_scheduled"])
    confirmed = sum(1 for group in lead_groups.values() if group["has_confirmed"])
    attended = sum(1 for group in lead_groups.values() if group["has_attended"])
    memberships = sum(1 for group in lead_groups.values() if group["has_membership"])
    no_shows = sum(1 for group in lead_groups.values() if group["has_no_show"])

    items.sort(
        key=lambda item: (
            item.get("lead_started_at") or "",
            item.get("parent_name") or "",
            item.get("child_name") or "",
        ),
        reverse=True,
    )

    summary_cards = [
        {"key": "new_leads", "label": "New Leads", "count": total_leads, "subtext": f"{days} day cohort"},
        {
            "key": "accounts_created",
            "label": "DaySmart Accounts",
            "count": accounts_created,
            "subtext": _format_percent(accounts_created, total_leads),
        },
        {
            "key": "scheduled_trials",
            "label": "Trials Scheduled",
            "count": scheduled,
            "subtext": _format_percent(scheduled, total_leads),
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
            "subtext": _format_percent(memberships, total_leads),
        },
    ]

    conversation_statuses = [
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

    return {
        "window": {
            "days": days,
            "start_date": window_start,
            "end_date": now_utc.date().isoformat(),
            "generated_at": now_utc.isoformat(),
        },
        "summary": {
            "lead_conversations": total_leads,
            "account_created": accounts_created,
            "scheduled_trials": scheduled,
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
            "matched_lead_count": accounts_created,
            "unmatched_lead_count": unmatched_leads,
            "detail_rows": len(items),
        },
        "items": items,
    }


def build_youth_kpi_email_preview(
    db_path: str,
    *,
    youth_inbox_id: int,
    days: int = 7,
) -> dict[str, str]:
    dashboard = build_youth_kpi_dashboard(db_path, youth_inbox_id=youth_inbox_id, days=days)
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
        f"Trials scheduled: {summary['scheduled_trials']}",
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
            class_part = f" | {item['free_trial_class_display']}" if item.get("free_trial_class_display") else ""
            lines.append(f"- {item['parent_name']}{child_name} | {item['lead_status']}{class_part}")

    return {
        "subject": subject,
        "body": "\n".join(lines),
    }
