from __future__ import annotations

import datetime as dt
import argparse
import base64
import html
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests

from .config import REPO_ROOT, WORKSPACE_ROOT, get_settings
from .youth_kpis import _normalize_email, _normalize_name, _normalize_phone

EVENTBRITE_SUBJECT_PREFIX = "Order Notification for Free Youth Indoor Beach Volleyball Class"
EVENTBRITE_ALLOWED_SUBJECTS = (
    "Order Notification for Free Youth Indoor Beach Volleyball Class (Ages 10-17)",
    "Order Notification for Free Youth Indoor Beach Volleyball Class (Ages 10-12)",
    "Order Notification for Free Youth Indoor Beach Volleyball Class (Ages 13-17)",
    "Order Notification for Free Youth Indoor Beach Volleyball Class (Ages 6-9)",
)
EVENTBRITE_LOCAL_TZ = ZoneInfo("America/New_York")
ORDER_ID_RE = re.compile(r"Order\s*#\s*(\d+)", re.IGNORECASE)
EVENT_TITLE_RE = re.compile(
    r"An order for\s+\[?(Free Youth Indoor Beach Volleyball Class \([^)]+\))\]?\s+just came through",
    re.IGNORECASE,
)
EVENT_DATETIME_RE = re.compile(
    r"(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"([A-Z][a-z]+\s+\d{1,2},\s+\d{4})\s+from\s+"
    r"(\d{1,2}:\d{2}\s+[AP]M)\s+to\s+(\d{1,2}:\d{2}\s+[AP]M)\s+\(ET\)",
    re.IGNORECASE,
)
TICKET_SECTION_RE = re.compile(r"Ticket #\d+:\s+[^\n]+", re.IGNORECASE)
TOP_CONTACT_RE = re.compile(
    r"Below, you'll find a copy of the order confirmation email for:\n+"
    r"(?P<name>[^\n]+)\n+"
    r"(?P<email>[^\n]+)\n+"
    r"Order\s*#",
    re.IGNORECASE,
)
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_READONLY_SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)
LIVE_GMAIL_DEFAULT_QUERY = (
    'newer_than:30d subject:"Order Notification for Free Youth Indoor Beach Volleyball Class"'
)


def _clean_text(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    text = value.replace("\r", "")
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _resolve_workspace_file(raw_value: str | None, default_name: str) -> Path:
    raw = (raw_value or default_name).strip() or default_name
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    for base in (
        WORKSPACE_ROOT / "salesmessage_agent",
        WORKSPACE_ROOT,
        REPO_ROOT,
        Path.cwd(),
    ):
        candidate = base / path
        if candidate.exists():
            return candidate
    return WORKSPACE_ROOT / "salesmessage_agent" / path


def _gmail_credentials() -> Any:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = _resolve_workspace_file(
        os.getenv("GMAIL_OAUTH_TOKEN_FILE"),
        "gmail_eod_token.json",
    )
    if not token_path.exists():
        raise FileNotFoundError(f"Gmail token file not found: {token_path}")
    creds = Credentials.from_authorized_user_file(str(token_path), list(GMAIL_READONLY_SCOPES))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    if not creds.valid:
        raise RuntimeError("Gmail credentials are not valid")
    return creds


def _gmail_get(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
    creds = _gmail_credentials()
    response = requests.get(
        f"{GMAIL_API_BASE}/{path.lstrip('/')}",
        headers={"Authorization": f"Bearer {creds.token}"},
        params=params or {},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()


def _decode_gmail_body(data: str | None) -> str:
    if not isinstance(data, str) or not data:
        return ""
    padding = "=" * (-len(data) % 4)
    try:
        return base64.urlsafe_b64decode((data + padding).encode("ascii")).decode("utf-8", "ignore")
    except Exception:
        return ""


def _html_to_text(value: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", value)
    text = re.sub(r"(?i)</p\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def _message_body_from_payload(payload: dict[str, Any]) -> str:
    plain_parts: list[str] = []
    html_parts: list[str] = []

    def visit(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType") or "").lower()
        body_data = (part.get("body") or {}).get("data") if isinstance(part.get("body"), dict) else None
        decoded = _decode_gmail_body(body_data)
        if decoded:
            if mime_type == "text/plain":
                plain_parts.append(decoded)
            elif mime_type == "text/html":
                html_parts.append(_html_to_text(decoded))
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                visit(child)

    visit(payload)
    return "\n".join(plain_parts or html_parts).strip()


def _headers_from_payload(payload: dict[str, Any]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for header in payload.get("headers") or []:
        if not isinstance(header, dict):
            continue
        name = str(header.get("name") or "").strip().lower()
        value = str(header.get("value") or "").strip()
        if name:
            headers[name] = value
    return headers


def _gmail_message_to_import_payload(message: dict[str, Any]) -> dict[str, Any]:
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    headers = _headers_from_payload(payload)
    internal_date = message.get("internalDate")
    email_ts = None
    if internal_date not in (None, ""):
        try:
            email_ts = dt.datetime.fromtimestamp(int(internal_date) / 1000, tz=dt.timezone.utc).isoformat()
        except (TypeError, ValueError):
            email_ts = None
    return {
        "id": message.get("id"),
        "subject": headers.get("subject", ""),
        "from_": headers.get("from", ""),
        "email_ts": email_ts,
        "display_url": f"https://mail.google.com/mail/#all/{message.get('id')}",
        "body": _message_body_from_payload(payload),
    }


def fetch_live_eventbrite_messages(
    *,
    query: str = LIVE_GMAIL_DEFAULT_QUERY,
    max_results: int = 100,
) -> list[dict[str, Any]]:
    search_payload = _gmail_get(
        "messages",
        params={
            "q": query,
            "maxResults": max(1, min(int(max_results or 100), 500)),
        },
    )
    messages: list[dict[str, Any]] = []
    for item in search_payload.get("messages") or []:
        message_id = item.get("id") if isinstance(item, dict) else None
        if not message_id:
            continue
        full_message = _gmail_get(
            f"messages/{message_id}",
            params={"format": "full"},
        )
        messages.append(_gmail_message_to_import_payload(full_message))
    return messages


def _parse_iso(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc).isoformat()


def _parse_event_start(body: str) -> str | None:
    match = EVENT_DATETIME_RE.search(body)
    if match is None:
        return None
    date_part = match.group(2)
    start_part = match.group(3).upper()
    try:
        parsed = dt.datetime.strptime(
            f"{date_part} {start_part}",
            "%B %d, %Y %I:%M %p",
        ).replace(tzinfo=EVENTBRITE_LOCAL_TZ)
    except ValueError:
        return None
    return parsed.astimezone(dt.timezone.utc).isoformat()


def _parse_event_title(subject: str, body: str) -> str | None:
    cleaned_subject = (subject or "").strip()
    if cleaned_subject in EVENTBRITE_ALLOWED_SUBJECTS:
        return cleaned_subject.removeprefix("Order Notification for ").strip()
    match = EVENT_TITLE_RE.search(body)
    if match is not None:
        return match.group(1).strip()
    return None


def _parse_order_id(body: str) -> str | None:
    match = ORDER_ID_RE.search(body)
    return match.group(1) if match is not None else None


def _parse_ticket_sections(body: str) -> list[dict[str, str]]:
    tickets: list[dict[str, str]] = []
    matches = list(TICKET_SECTION_RE.finditer(body))
    if not matches:
        return tickets

    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        block = body[start:end].strip()
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue

        header_name = lines[1]

        def field_value(label: str) -> str:
            for i, line in enumerate(lines):
                if line.lower() == label.lower() and i + 1 < len(lines):
                    return lines[i + 1].strip()
            return ""

        first = field_value("First Name")
        last = field_value("Last Name")
        email = field_value("Email")
        phone = field_value("Cell Phone")
        parent = field_value("Parent's Name")
        age_group = field_value("What age group does your child belong in?")
        child_name = " ".join(part for part in (first, last) if part).strip()
        if not (child_name or header_name or parent):
            continue
        tickets.append(
            {
                "child_name": child_name or header_name,
                "first_name": first,
                "last_name": last,
                "email": email,
                "phone": phone,
                "parent_name": parent,
                "age_group": age_group,
            }
        )
    return tickets


def _parse_top_contact(body: str) -> tuple[str, str]:
    match = TOP_CONTACT_RE.search(body)
    if match is None:
        return "", ""
    return match.group("name").strip(), match.group("email").strip()


def _parse_order_summary_attendees(body: str) -> list[str]:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    try:
        start = lines.index("Order Summary")
    except ValueError:
        return []

    attendees: list[str] = []
    skip_lines = {
        "Order Summary",
        "Order",
        "Offline payment",
        "General Admission",
        "$0.00",
        "View and manage your order online",
        "Printable PDF tickets are attached to this email",
    }
    for index, line in enumerate(lines[start + 1 :], start=start + 1):
        if line in skip_lines or line.startswith("#"):
            continue
        if line.startswith("Contact the organizer"):
            break
        if index + 2 < len(lines):
            if re.fullmatch(r"\d+\s*x", lines[index + 1], re.IGNORECASE) and lines[index + 2] == "General Admission":
                attendees.append(line)
    return attendees


def _fallback_tickets_from_order_summary(body: str) -> list[dict[str, str]]:
    parent_name, parent_email = _parse_top_contact(body)
    if not parent_name:
        return []
    attendees = _parse_order_summary_attendees(body)
    if not attendees:
        return []

    normalized_parent = _normalize_name(parent_name)
    filtered_attendees = [name for name in attendees if _normalize_name(name) != normalized_parent]
    if filtered_attendees:
        attendees = filtered_attendees

    tickets: list[dict[str, str]] = []
    for attendee in attendees:
        parts = attendee.split()
        first = parts[0] if parts else ""
        last = " ".join(parts[1:]) if len(parts) > 1 else ""
        tickets.append(
            {
                "child_name": attendee,
                "first_name": first,
                "last_name": last,
                "email": parent_email,
                "phone": "",
                "parent_name": parent_name,
                "age_group": "",
            }
        )
    return tickets


def _make_family_key(identity: str) -> str:
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()
    return f"gmail_eventbrite:family:{digest[:16]}"


def _make_identity(phone: str | None, email: str | None, name: str | None) -> str | None:
    if phone:
        return f"phone:{phone}"
    if email:
        return f"email:{email}"
    if name:
        return f"name:{name}"
    return None


def _load_existing_order_refs(conn: sqlite3.Connection) -> set[str]:
    source_refs: set[str] = set()
    rows = conn.execute(
        """
        SELECT source_ref
        FROM youth_trial_leads
        WHERE source_system = 'gmail_eventbrite'
        """
    ).fetchall()
    for row in rows:
        if row["source_ref"]:
            source_refs.add(str(row["source_ref"]))
    return source_refs


def _iter_metadata_identity_values(metadata_json: str | None) -> tuple[list[str], list[str], list[str], dict[str, set[str]]]:
    phones: list[str] = []
    emails: list[str] = []
    names: list[str] = []
    parent_children: dict[str, set[str]] = {}
    try:
        metadata = json.loads(metadata_json or "{}")
    except json.JSONDecodeError:
        metadata = {}
    if not isinstance(metadata, dict):
        return phones, emails, names, parent_children

    contact = metadata.get("contact")
    if isinstance(contact, dict):
        phone = _normalize_phone(contact.get("phone") or contact.get("number"))
        email = _normalize_email(contact.get("email"))
        name = _normalize_name(contact.get("full_name") or contact.get("name"))
        if phone:
            phones.append(phone)
        if email:
            emails.append(email)
        if name:
            names.append(name)

    eventbrite_order = metadata.get("eventbrite_order")
    if isinstance(eventbrite_order, dict):
        children = eventbrite_order.get("children")
        if isinstance(children, list):
            for child in children:
                if not isinstance(child, dict):
                    continue
                child_name = _normalize_name(child.get("child_name"))
                parent_name = _normalize_name(child.get("parent_name"))
                phone = _normalize_phone(child.get("phone"))
                email = _normalize_email(child.get("email"))
                if phone:
                    phones.append(phone)
                if email:
                    emails.append(email)
                if child_name:
                    names.append(child_name)
                if parent_name:
                    names.append(parent_name)
                    if child_name:
                        parent_children.setdefault(parent_name, set()).add(child_name)
    return phones, emails, names, parent_children


def _load_existing_lead_identities(conn: sqlite3.Connection) -> dict[str, Any]:
    index: dict[str, Any] = {
        "phones": set(),
        "emails": set(),
        "names": set(),
        "parent_children": {},
    }
    rows = conn.execute(
        """
        SELECT
            t.contact_name,
            t.contact_phone,
            t.metadata_json,
            f.primary_contact_name,
            f.primary_contact_phone,
            f.primary_contact_email,
            f.metadata_json AS family_metadata_json
        FROM youth_trial_leads t
        LEFT JOIN youth_families f ON f.family_key = t.family_key
        """
    ).fetchall()
    for row in rows:
        for phone_value in (row["contact_phone"], row["primary_contact_phone"]):
            phone = _normalize_phone(phone_value)
            if phone:
                index["phones"].add(phone)
        email = _normalize_email(row["primary_contact_email"])
        if email:
            index["emails"].add(email)
        for name_value in (row["contact_name"], row["primary_contact_name"]):
            name = _normalize_name(name_value)
            if name:
                index["names"].add(name)
        for metadata_json in (row["metadata_json"], row["family_metadata_json"]):
            phones, emails, names, parent_children = _iter_metadata_identity_values(metadata_json)
            index["phones"].update(phones)
            index["emails"].update(emails)
            index["names"].update(names)
            for parent_name, children in parent_children.items():
                index["parent_children"].setdefault(parent_name, set()).update(children)
    return index


def _similar_existing_lead_reason(
    index: dict[str, Any],
    *,
    phone: str | None,
    email: str | None,
    parent_name: str | None,
    child_names: list[str],
) -> str | None:
    if phone and phone in index["phones"]:
        return "phone"
    if email and email in index["emails"]:
        return "email"
    normalized_parent = _normalize_name(parent_name)
    normalized_children = {name for name in (_normalize_name(child) for child in child_names) if name}
    if normalized_parent and normalized_children:
        existing_children = index["parent_children"].get(normalized_parent, set())
        if existing_children & normalized_children:
            return "parent_child_name"
    if normalized_parent and normalized_parent in index["names"] and not (phone or email):
        return "parent_name"
    if normalized_children and normalized_children & index["names"] and not (phone or email):
        return "child_name"
    return None


def import_messages(messages: list[dict[str, Any]]) -> dict[str, int]:
    settings = get_settings()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn = sqlite3.connect(settings.database_url, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        existing_source_refs = _load_existing_order_refs(conn)
        existing_identities = _load_existing_lead_identities(conn)
        imported = 0
        skipped_existing = 0
        skipped_similar = 0
        skipped_not_matching = 0

        for message in messages:
            subject = str(message.get("subject") or "").strip()
            if subject not in EVENTBRITE_ALLOWED_SUBJECTS:
                skipped_not_matching += 1
                continue

            body = _clean_text(message.get("body"))
            order_id = _parse_order_id(body)
            tickets = _parse_ticket_sections(body)
            if not tickets:
                tickets = _fallback_tickets_from_order_summary(body)
            if not order_id or not tickets:
                skipped_not_matching += 1
                continue

            first_ticket = tickets[0]
            parent_name = first_ticket.get("parent_name") or ""
            parent_phone_raw = first_ticket.get("phone") or ""
            parent_email_raw = first_ticket.get("email") or ""
            normalized_phone = _normalize_phone(parent_phone_raw)
            normalized_email = _normalize_email(parent_email_raw)
            normalized_name = _normalize_name(parent_name)
            identity = _make_identity(normalized_phone, normalized_email, normalized_name)
            if identity is None:
                skipped_not_matching += 1
                continue
            if order_id in existing_source_refs:
                skipped_existing += 1
                continue
            similar_reason = _similar_existing_lead_reason(
                existing_identities,
                phone=normalized_phone,
                email=normalized_email,
                parent_name=parent_name,
                child_names=[str(ticket.get("child_name") or "") for ticket in tickets],
            )
            if similar_reason:
                skipped_similar += 1
                continue

            family_key = _make_family_key(identity)
            lead_key = f"gmail_eventbrite:order:{order_id}"
            created_at = _parse_iso(message.get("email_ts")) or now
            metadata = {
                "contact": {
                    "full_name": parent_name,
                    "email": parent_email_raw,
                    "phone": parent_phone_raw,
                },
                "gmail": {
                    "message_id": message.get("id"),
                    "subject": subject,
                    "display_url": message.get("display_url"),
                    "from": message.get("from_"),
                    "email_ts": message.get("email_ts"),
                },
                "eventbrite_order": {
                    "order_id": order_id,
                    "event_title": _parse_event_title(subject, body),
                    "event_start": _parse_event_start(body),
                    "children": tickets,
                },
            }
            conn.execute(
                """
                INSERT INTO youth_families (
                    family_key, primary_contact_name, primary_contact_phone, primary_contact_email,
                    family_status, source_system, source_ref, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(family_key) DO UPDATE SET
                    primary_contact_name=excluded.primary_contact_name,
                    primary_contact_phone=excluded.primary_contact_phone,
                    primary_contact_email=excluded.primary_contact_email,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    family_key,
                    parent_name,
                    parent_phone_raw,
                    parent_email_raw,
                    "eventbrite_signup",
                    "gmail_eventbrite",
                    order_id,
                    json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                    created_at,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO youth_trial_leads (
                    lead_key, family_key, inbox_id, contact_name, contact_phone, trial_status, account_created,
                    added_to_class, trial_class_name, trial_class_when, last_interaction_at,
                    source_system, source_ref, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lead_key) DO UPDATE SET
                    family_key=excluded.family_key,
                    contact_name=excluded.contact_name,
                    contact_phone=excluded.contact_phone,
                    trial_status=excluded.trial_status,
                    trial_class_name=excluded.trial_class_name,
                    trial_class_when=excluded.trial_class_when,
                    last_interaction_at=excluded.last_interaction_at,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    lead_key,
                    family_key,
                    None,
                    parent_name,
                    parent_phone_raw,
                    "invited",
                    0,
                    0,
                    metadata["eventbrite_order"]["event_title"],
                    metadata["eventbrite_order"]["event_start"],
                    created_at,
                    "gmail_eventbrite",
                    order_id,
                    json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                    created_at,
                    now,
                ),
            )
            imported += 1
            existing_source_refs.add(order_id)
            if normalized_phone:
                existing_identities["phones"].add(normalized_phone)
            if normalized_email:
                existing_identities["emails"].add(normalized_email)
            if normalized_name:
                existing_identities["names"].add(normalized_name)
            child_names = {name for name in (_normalize_name(ticket.get("child_name")) for ticket in tickets) if name}
            if normalized_name and child_names:
                existing_identities["parent_children"].setdefault(normalized_name, set()).update(child_names)

        conn.commit()
    finally:
        conn.close()

    return {
        "imported": imported,
        "skipped_existing": skipped_existing,
        "skipped_similar": skipped_similar,
        "skipped_not_matching": skipped_not_matching,
    }


def import_live_eventbrite_messages(
    *,
    query: str = LIVE_GMAIL_DEFAULT_QUERY,
    max_results: int = 100,
) -> dict[str, int | str]:
    messages = fetch_live_eventbrite_messages(query=query, max_results=max_results)
    result: dict[str, int | str] = import_messages(messages)
    result["fetched"] = len(messages)
    result["source"] = "live-gmail-eventbrite"
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Import Eventbrite youth trial Gmail orders.")
    parser.add_argument("json_path", nargs="?", help="Saved Gmail message payload JSON.")
    parser.add_argument("--live", action="store_true", help="Fetch current matching messages from Gmail.")
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument("--query", default=LIVE_GMAIL_DEFAULT_QUERY)
    args = parser.parse_args(argv[1:])

    if args.live:
        result = import_live_eventbrite_messages(query=args.query, max_results=args.max_results)
        print(json.dumps(result, indent=2))
        return 0

    if not args.json_path:
        print("usage: python -m app.import_eventbrite_gmail_orders [--live] <json>", file=sys.stderr)
        return 1
    path = Path(args.json_path).expanduser().resolve()
    payload = json.loads(path.read_text())
    if not isinstance(payload, list):
        print("input json must be a list of Gmail message payloads", file=sys.stderr)
        return 1
    result = import_messages(payload)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
