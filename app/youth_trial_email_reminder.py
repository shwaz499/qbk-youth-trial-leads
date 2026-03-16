from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any
from zoneinfo import ZoneInfo

from .config import get_settings
from .db import get_conn, init_db
from .daysmart import DaysmartClient
from .ingest import sync_conversations
from .salesmessage import SalesmessageClient
from .unified import sync_daysmart_to_unified, sync_salesmessage_to_unified

LOCAL_TZ = ZoneInfo("America/New_York")
YOUTH_INBOX_ID = 207883
YOUTH_RECIPIENT_EMAILS = [
    "josh@qbksports.com",
    "leon@qbksports.com",
    "owen@qbksports.com",
    "info@qbksports.com",
]
REMINDER_FROM_EMAIL = "josh@qbksports.com"
MAIL_TRANSPORT = "mailapp"
MAIL_ACCOUNTS_SCRIPT = 'tell application "Mail" to get the name of every account'
REMINDER_WINDOW_MINUTES_MIN = 45
REMINDER_WINDOW_MINUTES_MAX = 75
SYNC_LOOKBACK_DAYS = 30
DAYSMART_CACHE_LOOKAHEAD_DAYS = 10
DAYSMART_PAGE_SIZE = 100
DAYSMART_SYNC_MAX_PAGES = 6
LEAD_CUTOFF_DATE = "2026-03-01"
YOUTH_CLASS_TOKENS = ("seals", "cubs")


@dataclass
class YouthScheduledEvent:
    event_id: int
    starts_at: dt.datetime
    team_name: str


@dataclass
class YouthLeadMatch:
    lead_key: str
    conversation_id: int
    parent_name: str
    child_name: str | None
    phone: str | None
    conversation_url: str


@dataclass
class YouthNoAccountLead:
    conversation_id: int
    parent_name: str
    phone: str | None
    conversation_url: str
    class_label: str


@dataclass
class SalesmessageClassSignal:
    starts_at: dt.datetime
    class_name: str | None
    source_message_id: int | None
    source_message_at: dt.datetime


@dataclass
class YouthReminderEmail:
    class_day: dt.date
    events: list[tuple[YouthScheduledEvent, list[YouthLeadMatch], list[YouthNoAccountLead]]]


def _now_local() -> dt.datetime:
    return dt.datetime.now(LOCAL_TZ)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _clean_text(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(without_tags)).strip()


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


def _normalize_phone(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    if len(digits) < 10:
        return None
    return digits[-10:]


def _parse_salesmessage_ts(value: str | None) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip().replace("Z", "+00:00")
    for candidate in (cleaned, cleaned.replace(" ", "T")):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=LOCAL_TZ)
            return parsed.astimezone(LOCAL_TZ)
        except ValueError:
            continue
    return None


def _parse_daysmart_ts(value: str | None) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip().replace("Z", "+00:00")
    for candidate in (cleaned, cleaned.replace(" ", "T")):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=LOCAL_TZ)
            return parsed.astimezone(LOCAL_TZ)
        except ValueError:
            continue
    return None


def _clean_trial_class_name(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if cleaned.startswith("Junior Classes - "):
        cleaned = cleaned[len("Junior Classes - "):].strip()
    cleaned = re.sub(r"\(\s*(\d+)\s*-\s*(\d+)\s*y/o\s*\)", r"(\1-\2)", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _is_target_youth_class(value: str | None) -> bool:
    if not isinstance(value, str):
        return False
    lowered = value.lower()
    return any(token in lowered for token in YOUTH_CLASS_TOKENS)


def _format_class_label(starts_at: dt.datetime) -> str:
    return starts_at.strftime("%a, %-m/%-d/%y %-I:%M %p")


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
        r"^ok!+$",
        r"^okay!+$",
        r"^confirm\b",
        r"^confirmed\b",
        r"\bconfirm!?\b",
        r"\bconfirmed!?\b",
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


def _next_weekday(anchor: dt.date, weekday_name: str) -> dt.date:
    weekdays = {
        "monday": 0,
        "tuesday": 1,
        "wednesday": 2,
        "thursday": 3,
        "friday": 4,
        "saturday": 5,
        "sunday": 6,
    }
    target = weekdays[weekday_name.lower()]
    days_ahead = (target - anchor.weekday()) % 7
    return anchor + dt.timedelta(days=days_ahead)


def _parse_time_components(time_text: str) -> tuple[int, int] | None:
    match = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", time_text, re.IGNORECASE)
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or "0")
    meridiem = match.group(3).lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    if meridiem == "am" and hour == 12:
        hour = 0
    return hour, minute


def _parse_month_name_date(text: str, anchor: dt.datetime) -> dt.datetime | None:
    match = re.search(
        r"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday),?\s+"
        r"(january|february|march|april|may|june|july|august|september|october|november|december)\s+"
        r"(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{2,4}))?"
        r"(?:\s+at\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)))?",
        text,
        re.IGNORECASE,
    )
    if not match:
        return None
    month_lookup = {
        "january": 1,
        "february": 2,
        "march": 3,
        "april": 4,
        "may": 5,
        "june": 6,
        "july": 7,
        "august": 8,
        "september": 9,
        "october": 10,
        "november": 11,
        "december": 12,
    }
    month = month_lookup[match.group(1).lower()]
    day = int(match.group(2))
    year_group = match.group(3)
    year = anchor.year
    if year_group:
        year = int(year_group)
        if year < 100:
            year += 2000
    hour = 0
    minute = 0
    if match.group(4):
        time_bits = _parse_time_components(match.group(4))
        if time_bits:
            hour, minute = time_bits
    try:
        return dt.datetime(year, month, day, hour, minute, tzinfo=LOCAL_TZ)
    except ValueError:
        return None


def _parse_class_datetime(text: str, anchor: dt.datetime) -> dt.datetime | None:
    lowered = text.lower()
    month_name = _parse_month_name_date(lowered, anchor)
    if month_name is not None:
        return month_name

    explicit = re.search(
        r"\b(?:mon(?:day)?|tue(?:sday)?|wed(?:nesday)?|thu(?:rsday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)?\s*"
        r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?"
        r".{0,20}?"
        r"(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b",
        lowered,
        re.IGNORECASE,
    )
    if explicit:
        month = int(explicit.group(1))
        day = int(explicit.group(2))
        year_group = explicit.group(3)
        year = anchor.year
        if year_group:
            year = int(year_group)
            if year < 100:
                year += 2000
        time_bits = _parse_time_components(explicit.group(4))
        if time_bits:
            hour, minute = time_bits
            try:
                return dt.datetime(year, month, day, hour, minute, tzinfo=LOCAL_TZ)
            except ValueError:
                return None

    relative = re.search(r"\b(today|tomorrow)\b.{0,20}?(\d{1,2}(?::\d{2})?\s*(?:am|pm))\b", lowered, re.IGNORECASE)
    if relative:
        base_date = anchor.date() + dt.timedelta(days=1 if relative.group(1).lower() == "tomorrow" else 0)
        time_bits = _parse_time_components(relative.group(2))
        if time_bits:
            hour, minute = time_bits
            return dt.datetime.combine(base_date, dt.time(hour, minute), tzinfo=LOCAL_TZ)

    weekday_time = re.search(
        r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b.{0,24}?(\d{1,2}(?::\d{2})?\s*(?:am|pm))",
        lowered,
        re.IGNORECASE,
    )
    if weekday_time:
        base_date = _next_weekday(anchor.date(), weekday_time.group(1))
        time_bits = _parse_time_components(weekday_time.group(2))
        if time_bits:
            hour, minute = time_bits
            return dt.datetime.combine(base_date, dt.time(hour, minute), tzinfo=LOCAL_TZ)
    return None


def _candidate_class_name(text: str) -> str | None:
    cleaned = _clean_text(text)
    if not cleaned:
        return None
    match = re.search(r"\b(Cubs|Seals(?:\s*\([^)]*\))?)\b", cleaned, re.IGNORECASE)
    if not match:
        return None
    return _clean_trial_class_name(match.group(1))


def _message_implies_specific_class(text: str) -> bool:
    lowered = text.lower()
    if "schedule for free trial classes" in lowered:
        return False
    if "which day works best" in lowered:
        return False
    triggers = [
        "works great",
        "works perfectly",
        "added to the",
        "added to the class roster",
        "added to the roster",
        "looking forward to seeing",
        "free trial class",
        "confirming the free trial class",
        "your daughter on the roster",
        "your child on the roster",
    ]
    return any(trigger in lowered for trigger in triggers)


def _load_recent_messages(conn: Any, conversation_id: int, limit: int = 40) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, body, created_at, sent_at, received_at, user_id
        FROM messages
        WHERE conversation_id = ?
        ORDER BY coalesce(created_at, received_at, sent_at) DESC
        LIMIT ?
        """,
        (conversation_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def _daysmart_client() -> DaysmartClient:
    settings = get_settings()
    return DaysmartClient(
        client_id=settings.daysmart_api_client_id,
        client_secret=settings.daysmart_api_secret,
        base_url=settings.daysmart_base_url,
    )


def _json_loads(value: str | None, fallback: Any) -> Any:
    if not isinstance(value, str) or not value.strip():
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


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


def _find_daysmart_matches(conn: Any, lead_row: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
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
            parent = phone_matches[0]

    child: dict[str, Any] | None = None
    if parent is not None:
        family_ids = _json_loads(parent.get("family_ids_json"), [])
        child_ids = _json_loads(parent.get("child_ids_json"), [])
        ids = sorted({int(v) for v in (family_ids + child_ids) if str(v).isdigit()})
        if ids:
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(
                f"SELECT * FROM daysmart_customers WHERE customer_id IN ({placeholders}) ORDER BY updated_at DESC",
                ids,
            ).fetchall()
            candidates = [dict(row) for row in rows]
            non_parent = [row for row in candidates if int(row["customer_id"]) != int(parent["customer_id"])]
            if non_parent:
                cutoff = dt.date.today() - dt.timedelta(days=365 * 18)
                age_candidates: list[tuple[dt.date, dict[str, Any]]] = []
                for row in non_parent:
                    birthdate = _customer_birthdate(row)
                    if birthdate is not None and birthdate >= cutoff:
                        age_candidates.append((birthdate, row))
                if age_candidates:
                    age_candidates.sort(key=lambda item: item[0], reverse=True)
                    child = age_candidates[0][1]
                elif len(non_parent) == 1:
                    child = non_parent[0]
    return parent, child


def _related_daysmart_customer_ids(parent: dict[str, Any] | None, child: dict[str, Any] | None) -> list[int]:
    related: set[int] = set()
    for row in (parent, child):
        if row is not None:
            related.add(int(row["customer_id"]))
    return sorted(related)


def _mail_accounts() -> list[str]:
    result = subprocess.run(
        ["osascript", "-e", MAIL_ACCOUNTS_SCRIPT],
        check=True,
        capture_output=True,
        text=True,
    )
    return [item.strip() for item in result.stdout.split(",") if item.strip()]


def _send_via_mailapp(subject: str, body: str, *, to_emails: list[str], from_email: str) -> None:
    accounts = _mail_accounts()
    if from_email not in accounts:
        raise RuntimeError(f'Mail.app account "{from_email}" is not configured on this Mac')
    body_value = json.dumps(body + "\n\n")
    subject_value = json.dumps(subject)
    from_value = json.dumps(from_email)
    recipients_script = "\n".join(
        f'            make new to recipient at end of to recipients with properties {{address:{json.dumps(email)}}}'
        for email in to_emails
    )
    script = f'''
    tell application "Mail"
        set newMessage to make new outgoing message with properties {{subject:{subject_value}, content:{body_value}, visible:false}}
        tell newMessage
            set sender to {from_value}
{recipients_script}
            send
        end tell
    end tell
    '''
    subprocess.run(["osascript", "-e", script], check=True)


def refresh_daysmart_schedule_cache(*, lookahead_days: int = DAYSMART_CACHE_LOOKAHEAD_DAYS) -> dict[str, Any]:
    settings = get_settings()
    init_db(settings.database_url)
    now = _now_local()
    updated_at = _utc_now()
    with get_conn(settings.database_url) as conn:
        window_start = (now - dt.timedelta(days=1)).isoformat()
        window_end = (now + dt.timedelta(days=lookahead_days)).isoformat()
        rows = conn.execute(
            """
            SELECT team_or_event_id, event_name, event_start
            FROM daysmart_class_registrations
            WHERE source_type = 'event_registration'
              AND coalesce(event_start, '') >= ?
              AND coalesce(event_start, '') <= ?
            GROUP BY team_or_event_id, event_name, event_start
            ORDER BY event_start ASC
            """,
            (window_start, window_end),
        ).fetchall()
        events: list[YouthScheduledEvent] = []
        for row in rows:
            row_dict = dict(row)
            starts_at = _parse_daysmart_ts(row_dict.get("event_start"))
            event_name = _clean_trial_class_name(row_dict.get("event_name"))
            event_id = row_dict.get("team_or_event_id")
            if starts_at is None or not str(event_id).isdigit() or not _is_target_youth_class(event_name):
                continue
            events.append(
                YouthScheduledEvent(
                    event_id=int(event_id),
                    starts_at=starts_at,
                    team_name=event_name or "Youth Class",
                )
            )
        unique_events: dict[int, YouthScheduledEvent] = {event.event_id: event for event in events}
        for event in events:
            conn.execute(
                """
                INSERT INTO youth_trial_schedule_events (
                    event_id, starts_at, team_name, raw_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    starts_at=excluded.starts_at,
                    team_name=excluded.team_name,
                    raw_json=excluded.raw_json,
                    updated_at=excluded.updated_at
                """,
                (
                    event.event_id,
                    event.starts_at.isoformat(),
                    event.team_name,
                    json.dumps(
                        {
                            "event_id": event.event_id,
                            "starts_at": event.starts_at.isoformat(),
                            "team_name": event.team_name,
                        },
                        ensure_ascii=True,
                    ),
                    updated_at,
                ),
            )
        cutoff = (now - dt.timedelta(days=2)).isoformat()
        conn.execute("DELETE FROM youth_trial_schedule_events WHERE starts_at < ?", (cutoff,))
    return {
        "ok": True,
        "events_cached": len(unique_events),
        "lookahead_days": lookahead_days,
        "updated_at": updated_at,
    }


def _load_cached_youth_events(conn: Any, *, window_start: dt.datetime, window_end: dt.datetime) -> list[YouthScheduledEvent]:
    rows = conn.execute(
        """
        SELECT event_id, starts_at, team_name
        FROM youth_trial_schedule_events
        WHERE starts_at >= ?
          AND starts_at <= ?
        ORDER BY starts_at ASC
        """,
        (window_start.isoformat(), window_end.isoformat()),
    ).fetchall()
    events: list[YouthScheduledEvent] = []
    for row in rows:
        row_dict = dict(row)
        starts_at = _parse_daysmart_ts(row_dict.get("starts_at"))
        if starts_at is None:
            continue
        events.append(
            YouthScheduledEvent(
                event_id=int(row_dict["event_id"]),
                starts_at=starts_at,
                team_name=_clean_trial_class_name(str(row_dict.get("team_name") or "Youth Class")) or "Youth Class",
            )
        )
    return events


def _load_candidate_youth_leads(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT lead_key, family_key, inbox_id, contact_name, contact_phone, trial_status,
               last_interaction_at, source_ref, metadata_json
        FROM youth_trial_leads
        WHERE inbox_id = ?
          AND coalesce(last_interaction_at, '') >= ?
        ORDER BY coalesce(last_interaction_at, '') DESC
        """,
        (YOUTH_INBOX_ID, LEAD_CUTOFF_DATE),
    ).fetchall()
    return [dict(row) for row in rows]


def _load_event_registration_map(conn: Any, event_ids: list[int]) -> dict[int, set[int]]:
    if not event_ids:
        return {}
    placeholders = ",".join("?" for _ in event_ids)
    rows = conn.execute(
        f"""
        SELECT team_or_event_id, customer_id
        FROM daysmart_class_registrations
        WHERE source_type = 'event_registration'
          AND team_or_event_id IN ({placeholders})
        """,
        event_ids,
    ).fetchall()
    registrations: dict[int, set[int]] = {}
    for row in rows:
        event_id = int(row["team_or_event_id"])
        registrations.setdefault(event_id, set()).add(int(row["customer_id"]))
    return registrations


def _extract_salesmessage_class_signal(conn: Any, conversation_id: int) -> tuple[SalesmessageClassSignal | None, str | None, bool]:
    recent_messages = _load_recent_messages(conn, conversation_id)
    latest_inbound_signal: str | None = None
    parsed_signal: SalesmessageClassSignal | None = None
    has_confirmed_tag = False

    conv_row = conn.execute("SELECT raw_json FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if conv_row is not None:
        payload = _json_loads(conv_row["raw_json"], {})
        contact = payload.get("contact") if isinstance(payload, dict) else {}
        tags = [
            _clean_text(tag.get("name")).lower()
            for tag in (contact.get("tags") or [])
            if isinstance(tag, dict) and _clean_text(tag.get("name"))
        ]
        has_confirmed_tag = "confirmed class" in tags

    for msg in recent_messages:
        text = _clean_text(msg.get("body"))
        if not text:
            continue
        created_at = _parse_salesmessage_ts(msg.get("created_at") or msg.get("received_at") or msg.get("sent_at"))
        if created_at is None:
            continue
        if not msg.get("user_id") and latest_inbound_signal is None:
            if _negative_inbound(text):
                latest_inbound_signal = "negative"
            elif _affirmative_inbound(text):
                latest_inbound_signal = "affirmative"
        if parsed_signal is None and _message_implies_specific_class(text):
            class_dt = _parse_class_datetime(text, created_at)
            class_name = _candidate_class_name(text)
            if class_dt is not None:
                parsed_signal = SalesmessageClassSignal(
                    starts_at=class_dt,
                    class_name=class_name,
                    source_message_id=msg.get("id"),
                    source_message_at=created_at,
                )
    return parsed_signal, latest_inbound_signal, has_confirmed_tag


def _notification_key(class_day: dt.date, recipients: list[str]) -> str:
    normalized = ",".join(sorted(email.lower() for email in recipients))
    return f"{class_day.isoformat()}|{normalized}"


def _already_sent(conn: Any, key: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM youth_trial_email_notifications WHERE notification_key = ?",
        (key,),
    ).fetchone()
    return row is not None


def _record_sent(conn: Any, key: str, class_day: dt.date, class_label: str, recipients: list[str], payload: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO youth_trial_email_notifications (
            notification_key, class_day, class_label, recipient_email, payload_json, sent_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(notification_key) DO NOTHING
        """,
        (
            key,
            class_day.isoformat(),
            class_label,
            ", ".join(recipients),
            json.dumps(payload, ensure_ascii=True),
            _utc_now(),
        ),
    )


def _build_youth_email(reminder: YouthReminderEmail) -> EmailMessage:
    first_event = reminder.events[0][0]
    subject = f"Youth Free Trial Reminder: {first_event.starts_at.strftime('%a, %-m/%-d/%y')}"
    lines = [
        f"Youth free trial classes for {first_event.starts_at.strftime('%A, %B %-d, %Y')}",
        "",
    ]
    for event, attendees, no_account_created in reminder.events:
        lines.append(f"{event.team_name} - {_format_class_label(event.starts_at)}")
        if attendees:
            for attendee in attendees:
                line = f"- {attendee.parent_name}"
                if attendee.child_name:
                    line += f" | child: {attendee.child_name}"
                if attendee.phone:
                    line += f" | {attendee.phone}"
                line += f" | {attendee.conversation_url}"
                lines.append(line)
        else:
            lines.append("- No DaySmart youth trial registrations")
        if no_account_created:
            lines.append("No account created")
            for lead in no_account_created:
                line = f"- {lead.parent_name}"
                if lead.phone:
                    line += f" | {lead.phone}"
                line += f" | {lead.conversation_url}"
                lines.append(line)
        lines.append("")

    lines.append(f"Generated at {_now_local().strftime('%a, %-m/%-d/%y %-I:%M %p %Z')}")
    msg = EmailMessage()
    msg["To"] = ", ".join(YOUTH_RECIPIENT_EMAILS)
    msg["From"] = REMINDER_FROM_EMAIL
    msg["Subject"] = subject
    msg.set_content("\n".join(lines))
    return msg


def _sync_youth_sources() -> dict[str, Any]:
    settings = get_settings()
    salesmessage_client = SalesmessageClient(
        token=settings.salesmessage_api_token,
        base_url=settings.salesmessage_base_url,
    )
    cutoff = (_now_local() - dt.timedelta(days=SYNC_LOOKBACK_DAYS)).astimezone(dt.timezone.utc).isoformat()
    sync_result = sync_conversations(
        client=salesmessage_client,
        db_path=settings.database_url,
        filters=["open", "closed"],
        conv_page_size=100,
        message_page_size=50,
        max_message_pages_per_conversation=1,
        target_inbox_ids={YOUTH_INBOX_ID},
        min_last_message_at=cutoff,
    )
    youth_result = sync_salesmessage_to_unified(
        db_path=settings.database_url,
        youth_inbox_id=YOUTH_INBOX_ID,
        cutoff_date=LEAD_CUTOFF_DATE,
    )
    daysmart_result = sync_daysmart_to_unified(
        client=_daysmart_client(),
        db_path=settings.database_url,
        max_pages=DAYSMART_SYNC_MAX_PAGES,
        page_size=DAYSMART_PAGE_SIZE,
    )
    return {
        "salesmessage": sync_result,
        "youth_unified": youth_result,
        "daysmart": daysmart_result,
    }


def _build_reminders(conn: Any, *, now: dt.datetime, test_send: bool) -> list[YouthReminderEmail]:
    upper_window = now + dt.timedelta(days=DAYSMART_CACHE_LOOKAHEAD_DAYS if test_send else 2)
    events = _load_cached_youth_events(conn, window_start=now - dt.timedelta(days=1), window_end=upper_window)
    leads = _load_candidate_youth_leads(conn)
    synthetic_event_key = -1
    for lead in leads:
        parent, _child = _find_daysmart_matches(conn, lead)
        if parent is not None:
            continue
        conversation_id = int(lead["source_ref"])
        signal, latest_inbound_signal, has_confirmed_tag = _extract_salesmessage_class_signal(conn, conversation_id)
        if signal is None or latest_inbound_signal == "negative":
            continue
        if not (has_confirmed_tag or latest_inbound_signal == "affirmative"):
            continue
        if signal.starts_at < now - dt.timedelta(days=1) or signal.starts_at > upper_window:
            continue
        signal_name = _clean_trial_class_name(signal.class_name)
        if signal_name and not _is_target_youth_class(signal_name):
            continue
        matched_existing = False
        for event in events:
            delta_minutes = abs((signal.starts_at - event.starts_at).total_seconds()) / 60
            name_matches = not signal_name or signal_name.lower() in event.team_name.lower() or event.team_name.lower() in signal_name.lower()
            if delta_minutes <= 30 and name_matches:
                matched_existing = True
                break
        if matched_existing:
            continue
        events.append(
            YouthScheduledEvent(
                event_id=synthetic_event_key,
                starts_at=signal.starts_at,
                team_name=signal_name or "Youth Class",
            )
        )
        synthetic_event_key -= 1
    if not events:
        return []
    events_by_day: dict[dt.date, list[YouthScheduledEvent]] = {}
    for event in events:
        events_by_day.setdefault(event.starts_at.date(), []).append(event)

    reminders: list[YouthReminderEmail] = []
    for class_day, day_events in sorted(events_by_day.items()):
        day_events.sort(key=lambda event: event.starts_at)
        first_event = day_events[0]
        minutes_until_first = (first_event.starts_at - now).total_seconds() / 60
        if test_send:
            if minutes_until_first < 0:
                continue
        else:
            if not (REMINDER_WINDOW_MINUTES_MIN <= minutes_until_first <= REMINDER_WINDOW_MINUTES_MAX):
                continue

        event_ids = [event.event_id for event in day_events if event.event_id > 0]
        registrations = _load_event_registration_map(conn, event_ids)
        event_buckets: dict[int, tuple[YouthScheduledEvent, list[YouthLeadMatch], list[YouthNoAccountLead]]] = {}
        seen_event_entries: set[tuple[str, int]] = set()
        seen_no_account: set[int] = set()

        for lead in leads:
            parent, child = _find_daysmart_matches(conn, lead)
            related_ids = _related_daysmart_customer_ids(parent, child)
            conversation_id = int(lead["source_ref"])
            conversation_url = f"https://app.salesmessage.com/conversations/{conversation_id}"

            matched_event_ids: set[int] = set()
            if related_ids:
                for event_id, customer_ids in registrations.items():
                    if customer_ids.intersection(related_ids):
                        matched_event_ids.add(event_id)

            if matched_event_ids:
                for event in day_events:
                    if event.event_id not in matched_event_ids:
                        continue
                    dedupe_key = (lead["lead_key"], event.event_id)
                    if dedupe_key in seen_event_entries:
                        continue
                    seen_event_entries.add(dedupe_key)
                    bucket = event_buckets.get(event.event_id)
                    if bucket is None:
                        bucket = (event, [], [])
                        event_buckets[event.event_id] = bucket
                    attendees = bucket[1]
                    attendees.append(
                        YouthLeadMatch(
                            lead_key=lead["lead_key"],
                            conversation_id=conversation_id,
                            parent_name=str(lead.get("contact_name") or "Unknown"),
                            child_name=str(child.get("full_name")) if child is not None and child.get("full_name") else None,
                            phone=lead.get("contact_phone"),
                            conversation_url=conversation_url,
                        )
                    )
                continue

            signal, latest_inbound_signal, has_confirmed_tag = _extract_salesmessage_class_signal(conn, conversation_id)
            if parent is not None or signal is None:
                continue
            if latest_inbound_signal == "negative":
                continue
            if not (has_confirmed_tag or latest_inbound_signal == "affirmative"):
                continue
            matched_event = None
            for event in day_events:
                delta_minutes = abs((signal.starts_at - event.starts_at).total_seconds()) / 60
                signal_name = (signal.class_name or "").lower()
                event_name = event.team_name.lower()
                class_matches = not signal_name or signal_name in event_name or event_name in signal_name
                if delta_minutes <= 30 and class_matches:
                    matched_event = event
                    break
            if matched_event is None or conversation_id in seen_no_account:
                continue
            seen_no_account.add(conversation_id)
            bucket = event_buckets.get(matched_event.event_id)
            if bucket is None:
                bucket = (matched_event, [], [])
                event_buckets[matched_event.event_id] = bucket
            bucket[2].append(
                YouthNoAccountLead(
                    conversation_id=conversation_id,
                    parent_name=str(lead.get("contact_name") or "Unknown"),
                    phone=lead.get("contact_phone"),
                    conversation_url=conversation_url,
                    class_label=f"{matched_event.team_name} - {_format_class_label(matched_event.starts_at)}",
                )
            )

        event_groups = sorted(event_buckets.values(), key=lambda item: item[0].starts_at)
        for _, attendees, no_account_created in event_groups:
            attendees.sort(key=lambda item: item.parent_name.lower())
            no_account_created.sort(key=lambda item: item.parent_name.lower())

        if event_groups:
            reminders.append(
                YouthReminderEmail(
                    class_day=class_day,
                    events=event_groups,
                )
            )
    return reminders


def run(*, dry_run: bool = False, skip_sync: bool = False, test_send: bool = False) -> dict[str, Any]:
    settings = get_settings()
    init_db(settings.database_url)
    sync_result = None
    if not skip_sync:
        sync_result = _sync_youth_sources()
    refresh_result = refresh_daysmart_schedule_cache()
    now = _now_local()

    with get_conn(settings.database_url) as conn:
        reminders = _build_reminders(conn, now=now, test_send=test_send)
        sent: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []

        for reminder in reminders:
            class_label = reminder.events[0][0].starts_at.strftime("%a, %-m/%-d/%y") if reminder.events else reminder.class_day.isoformat()
            notification_key = _notification_key(reminder.class_day, YOUTH_RECIPIENT_EMAILS)
            if not test_send and _already_sent(conn, notification_key):
                skipped.append({"class_day": reminder.class_day.isoformat(), "reason": "already_sent"})
                continue
            message = _build_youth_email(reminder)
            if test_send:
                message.replace_header("Subject", f"[TEST] {message['Subject']}")
            payload = {
                "subject": message["Subject"],
                "events": [
                    {
                        "event_id": event.event_id,
                        "team_name": event.team_name,
                        "starts_at": event.starts_at.isoformat(),
                        "attendees": [item.parent_name for item in attendees],
                        "no_account_created": [item.parent_name for item in no_account_created],
                    }
                    for event, attendees, no_account_created in reminder.events
                ],
            }
            if not dry_run:
                _send_via_mailapp(
                    message["Subject"],
                    message.get_content(),
                    to_emails=YOUTH_RECIPIENT_EMAILS,
                    from_email=REMINDER_FROM_EMAIL,
                )
                if not test_send:
                    _record_sent(conn, notification_key, reminder.class_day, class_label, YOUTH_RECIPIENT_EMAILS, payload)
            sent.append(
                {
                    "class_day": reminder.class_day.isoformat(),
                    "event_count": len(reminder.events),
                    "no_account_created_count": len(reminder.no_account_created),
                    "events": [
                        {
                            "team_name": event.team_name,
                            "starts_at": event.starts_at.isoformat(),
                            "attendees": [item.parent_name for item in attendees],
                            "no_account_created": [item.parent_name for item in no_account_created],
                        }
                        for event, attendees, no_account_created in reminder.events
                    ],
                    "dry_run": dry_run,
                    "test_send": test_send,
                }
            )

    return {
        "ok": True,
        "sync_result": sync_result,
        "schedule_refresh": refresh_result,
        "sent": sent,
        "skipped": skipped,
        "test_send": test_send,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Send youth free trial reminder emails one hour before class.")
    parser.add_argument("--dry-run", action="store_true", help="Preview emails without sending them.")
    parser.add_argument("--skip-sync", action="store_true", help="Use the existing local DB without refreshing sources.")
    parser.add_argument("--test-send", action="store_true", help="Send a one-off test email for the next upcoming youth trial day.")
    parser.add_argument(
        "--refresh-daysmart-schedule",
        action="store_true",
        help="Refresh the cached DaySmart youth schedule and exit.",
    )
    args = parser.parse_args()
    if args.refresh_daysmart_schedule:
        result = refresh_daysmart_schedule_cache()
    else:
        result = run(dry_run=args.dry_run, skip_sync=args.skip_sync, test_send=args.test_send)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
