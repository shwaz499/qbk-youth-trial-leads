from __future__ import annotations

import datetime as dt
import html
import json
import re
import unicodedata
from typing import Any

from .db import get_conn
from .daysmart import DaysmartApiError, DaysmartClient

DAYSMART_HISTORY_CUTOFF = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True)


def _normalize_phone(value: str | None) -> str | None:
    if not isinstance(value, str):
        return None
    digits = re.sub(r"\D+", "", value)
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
    cleaned = re.sub(r"[^a-z0-9]+", " ", ascii_like.lower()).strip()
    return cleaned or None


def _as_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _relationship_ids(value: Any) -> list[int]:
    if isinstance(value, dict):
        data = value.get("data")
        if isinstance(data, list):
            return _relationship_ids(data)
        if isinstance(data, dict):
            return _relationship_ids([data])
        return []
    if not isinstance(value, list):
        return []
    ids: list[int] = []
    for item in value:
        if isinstance(item, dict):
            item_id = item.get("id")
            if item_id is None and isinstance(item.get("data"), dict):
                item_id = item["data"].get("id")
            parsed = _as_int(item_id)
            if parsed is not None:
                ids.append(parsed)
        else:
            parsed = _as_int(item)
            if parsed is not None:
                ids.append(parsed)
    return ids


def _clean_text(value: str | None) -> str:
    if not isinstance(value, str):
        return ""
    text = html.unescape(value)
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _as_dt(value: str | None) -> dt.datetime | None:
    if not value or not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    try:
        parsed = dt.datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(cleaned[:19], fmt)
            return parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def _scope_key(family_key: str | None, child_key: str | None) -> str:
    return f"family:{family_key or ''}|child:{child_key or ''}"


def _daysmart_row_ts(row: dict[str, Any], *field_names: str) -> dt.datetime | None:
    attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
    for field_name in field_names:
        parsed = _as_dt(attrs.get(field_name))
        if parsed is not None:
            return parsed
    return None


def _page_latest_ts(
    rows: list[dict[str, Any]],
    *field_names: str,
) -> dt.datetime | None:
    latest: dt.datetime | None = None
    for row in rows:
        parsed = _daysmart_row_ts(row, *field_names)
        if parsed is None:
            continue
        if latest is None or parsed > latest:
            latest = parsed
    return latest


def _find_cutoff_start_page(
    fetch_page: Any,
    *,
    last_page: int,
    page_size: int,
    cutoff: dt.datetime,
    date_fields: tuple[str, ...],
) -> int:
    if last_page <= 1:
        return 1

    low = 1
    high = last_page
    result = last_page
    while low <= high:
        mid = (low + high) // 2
        rows, _ = fetch_page(page_number=mid, page_size=page_size)
        latest = _page_latest_ts(rows, *date_fields)
        if latest is not None and latest >= cutoff:
            result = mid
            high = mid - 1
        else:
            low = mid + 1

    return max(1, result - 1)


def _upsert_family(
    db_path: str,
    *,
    family_key: str,
    source_system: str,
    source_ref: str,
    primary_contact_name: str | None,
    primary_contact_phone: str | None,
    primary_contact_email: str | None,
    family_status: str,
    metadata: dict[str, Any],
    conn: Any | None = None,
) -> None:
    now = _utc_now()
    owns_conn = conn is None
    if conn is None:
        with get_conn(db_path) as fresh_conn:
            _upsert_family(
                db_path,
                family_key=family_key,
                source_system=source_system,
                source_ref=source_ref,
                primary_contact_name=primary_contact_name,
                primary_contact_phone=primary_contact_phone,
                primary_contact_email=primary_contact_email,
                family_status=family_status,
                metadata=metadata,
                conn=fresh_conn,
            )
        return
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
            family_status=excluded.family_status,
            source_ref=excluded.source_ref,
            metadata_json=excluded.metadata_json,
            updated_at=excluded.updated_at
        """,
        (
            family_key,
            primary_contact_name,
            primary_contact_phone,
            primary_contact_email,
            family_status,
            source_system,
            source_ref,
            _json(metadata),
            now,
            now,
        ),
    )


def _upsert_child(
    db_path: str,
    *,
    child_key: str,
    family_key: str,
    child_name: str,
    program_track: str | None,
    started_at: str | None,
    is_active: bool,
    source_system: str,
    source_ref: str,
    metadata: dict[str, Any],
    conn: Any | None = None,
) -> None:
    now = _utc_now()
    if conn is None:
        with get_conn(db_path) as fresh_conn:
            _upsert_child(
                db_path,
                child_key=child_key,
                family_key=family_key,
                child_name=child_name,
                program_track=program_track,
                started_at=started_at,
                is_active=is_active,
                source_system=source_system,
                source_ref=source_ref,
                metadata=metadata,
                conn=fresh_conn,
            )
        return
    conn.execute(
        """
        INSERT INTO youth_children (
            child_key, family_key, child_name, program_track, started_at, is_active,
            source_system, source_ref, metadata_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(child_key) DO UPDATE SET
            family_key=excluded.family_key,
            child_name=excluded.child_name,
            program_track=excluded.program_track,
            started_at=excluded.started_at,
            is_active=excluded.is_active,
            source_ref=excluded.source_ref,
            metadata_json=excluded.metadata_json,
            updated_at=excluded.updated_at
        """,
        (
            child_key,
            family_key,
            child_name,
            program_track,
            started_at,
            1 if is_active else 0,
            source_system,
            source_ref,
            _json(metadata),
            now,
            now,
        ),
    )


def _upsert_trial_lead(
    db_path: str,
    *,
    lead_key: str,
    family_key: str,
    inbox_id: int,
    contact_name: str | None,
    contact_phone: str | None,
    trial_status: str,
    account_created: bool,
    added_to_class: bool,
    trial_class_name: str | None,
    trial_class_when: str | None,
    last_interaction_at: str | None,
    source_ref: str,
    metadata: dict[str, Any],
) -> None:
    now = _utc_now()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO youth_trial_leads (
                lead_key, family_key, inbox_id, contact_name, contact_phone, trial_status, account_created,
                added_to_class, trial_class_name, trial_class_when, last_interaction_at,
                source_system, source_ref, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lead_key) DO UPDATE SET
                family_key=excluded.family_key,
                inbox_id=excluded.inbox_id,
                contact_name=excluded.contact_name,
                contact_phone=excluded.contact_phone,
                trial_status=excluded.trial_status,
                account_created=CASE
                    WHEN youth_trial_leads.account_created = 1 THEN 1
                    ELSE excluded.account_created
                END,
                added_to_class=CASE
                    WHEN youth_trial_leads.added_to_class = 1 THEN 1
                    ELSE excluded.added_to_class
                END,
                trial_class_name=excluded.trial_class_name,
                trial_class_when=excluded.trial_class_when,
                last_interaction_at=excluded.last_interaction_at,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                lead_key,
                family_key,
                inbox_id,
                contact_name,
                contact_phone,
                trial_status,
                1 if account_created else 0,
                1 if added_to_class else 0,
                trial_class_name,
                trial_class_when,
                last_interaction_at,
                "salesmessage",
                source_ref,
                _json(metadata),
                now,
                now,
            ),
    )


def _upsert_daysmart_customer(db_path: str, row: dict[str, Any], *, conn: Any | None = None) -> None:
    customer_id = _as_int(row.get("id"))
    if customer_id is None:
        return
    attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
    relationships = row.get("relationships") if isinstance(row.get("relationships"), dict) else {}
    full_name = (
        attrs.get("full_name")
        or f"{attrs.get('first_name', '')} {attrs.get('last_name', '')}".strip()
        or None
    )
    child_ids = _relationship_ids(relationships.get("children"))
    family_ids = sorted(
        {
            *(_relationship_ids(relationships.get("family"))),
            *(_relationship_ids(relationships.get("parents"))),
            *(_relationship_ids(relationships.get("spouses"))),
        }
    )
    if conn is None:
        with get_conn(db_path) as fresh_conn:
            _upsert_daysmart_customer(db_path, row, conn=fresh_conn)
        return
    conn.execute(
        """
        INSERT INTO daysmart_customers (
            customer_id, full_name, email, phone_day, phone_mobile, phone_night, phone_emergency,
            normalized_name, normalized_email, normalized_phone_day, normalized_phone_mobile,
            normalized_phone_night, normalized_phone_emergency, child_ids_json, family_ids_json,
            api_url, raw_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(customer_id) DO UPDATE SET
            full_name=excluded.full_name,
            email=excluded.email,
            phone_day=excluded.phone_day,
            phone_mobile=excluded.phone_mobile,
            phone_night=excluded.phone_night,
            phone_emergency=excluded.phone_emergency,
            normalized_name=excluded.normalized_name,
            normalized_email=excluded.normalized_email,
            normalized_phone_day=excluded.normalized_phone_day,
            normalized_phone_mobile=excluded.normalized_phone_mobile,
            normalized_phone_night=excluded.normalized_phone_night,
            normalized_phone_emergency=excluded.normalized_phone_emergency,
            child_ids_json=excluded.child_ids_json,
            family_ids_json=excluded.family_ids_json,
            api_url=excluded.api_url,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """,
        (
            customer_id,
            full_name,
            attrs.get("email"),
            attrs.get("phone_day"),
            attrs.get("phone_mobile"),
            attrs.get("phone_night"),
            attrs.get("phone_emergency"),
            _normalize_name(full_name),
            _normalize_email(attrs.get("email")),
            _normalize_phone(attrs.get("phone_day")),
            _normalize_phone(attrs.get("phone_mobile")),
            _normalize_phone(attrs.get("phone_night")),
            _normalize_phone(attrs.get("phone_emergency")),
            _json(child_ids),
            _json(family_ids),
            ((row.get("links") or {}) if isinstance(row.get("links"), dict) else {}).get("self"),
            _json(row),
            _utc_now(),
        ),
    )


def _upsert_daysmart_membership(
    db_path: str,
    row: dict[str, Any],
    *,
    product_name: str | None,
    conn: Any | None = None,
) -> None:
    membership_id = _as_int(row.get("id"))
    if membership_id is None:
        return
    attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
    bill_customer_id = _as_int(attrs.get("bill_cust_id"))
    product_id = _as_int(attrs.get("prod_id"))
    if conn is None:
        with get_conn(db_path) as fresh_conn:
            _upsert_daysmart_membership(db_path, row, product_name=product_name, conn=fresh_conn)
        return
    conn.execute(
            """
            INSERT INTO daysmart_memberships (
                membership_id, bill_customer_id, product_id, product_name,
                created_at, expires_at, auto_renew, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(membership_id) DO UPDATE SET
                bill_customer_id=excluded.bill_customer_id,
                product_id=excluded.product_id,
                product_name=excluded.product_name,
                created_at=excluded.created_at,
                expires_at=excluded.expires_at,
                auto_renew=excluded.auto_renew,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                membership_id,
                bill_customer_id,
                product_id,
                product_name,
                attrs.get("created"),
                attrs.get("expires") or attrs.get("term_date"),
                1 if bool(attrs.get("auto_renew")) else 0,
                _json(row),
                _utc_now(),
            ),
    )
    if bill_customer_id is not None:
        conn.execute(
            """
            INSERT INTO daysmart_customer_memberships (customer_id, membership_id)
            VALUES (?, ?)
            ON CONFLICT(customer_id, membership_id) DO NOTHING
            """,
            (bill_customer_id, membership_id),
        )


def _upsert_daysmart_class_registration(
    db_path: str,
    *,
    source_type: str,
    row: dict[str, Any],
    event_name: str | None = None,
    event_start: str | None = None,
    conn: Any | None = None,
) -> None:
    registration_id = _as_int(row.get("id"))
    if registration_id is None:
        return
    attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
    customer_id = _as_int(attrs.get("customer_id"))
    if customer_id is None:
        return
    if source_type == "registration":
        team_or_event_id = _as_int(attrs.get("team_id"))
        created_at = attrs.get("create_date") or attrs.get("created_at") or attrs.get("updated_at")
    else:
        team_or_event_id = _as_int(attrs.get("event_id"))
        created_at = attrs.get("time") or attrs.get("created_at") or attrs.get("updated_at")
    if conn is None:
        with get_conn(db_path) as fresh_conn:
            _upsert_daysmart_class_registration(
                db_path,
                source_type=source_type,
                row=row,
                event_name=event_name,
                event_start=event_start,
                conn=fresh_conn,
            )
        return
    conn.execute(
            """
            INSERT INTO daysmart_class_registrations (
                source_type, registration_id, customer_id, team_or_event_id,
                event_name, event_start, created_at, raw_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_type, registration_id) DO UPDATE SET
                customer_id=excluded.customer_id,
                team_or_event_id=excluded.team_or_event_id,
                event_name=excluded.event_name,
                event_start=excluded.event_start,
                created_at=excluded.created_at,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                source_type,
                registration_id,
                customer_id,
                team_or_event_id,
                event_name,
                event_start,
                created_at,
                _json(row),
                _utc_now(),
            ),
    )


def _is_added_to_class(text: str) -> bool:
    lowered = text.lower()
    positive_patterns = [
        "added to the",
        "added you to",
        "added her to",
        "added him to",
        "added to class",
        "added to the class",
        "added to the roster",
        "on the roster",
        "added to our",
        "you're all set",
        "you are all set",
        "she is all set",
        "he is all set",
        "you're all set for",
        "you are all set for",
        "she's all set for",
        "he's all set for",
    ]
    return any(pattern in lowered for pattern in positive_patterns)


def _is_account_confirmation(text: str, has_done_prompt: bool) -> bool:
    lowered = text.lower()
    strong_patterns = [
        "created the account",
        "created an account",
        "created account",
        "i registered",
        "just registered",
        "registered and signed",
        "i finished",
        "ok i finished",
        "okay i finished",
        "i signed up",
        "ok i signed up",
        "signed up",
        "account is created",
        "account has been created",
        "set up the account",
        "setup the account",
        "signed everything",
    ]
    if any(pattern in lowered for pattern in strong_patterns):
        return True
    if has_done_prompt and re.search(r"\b(done|ok done|okay done|all done)\b", lowered):
        return True
    return False


def _extract_trial_class_choice(text: str) -> tuple[str | None, str | None]:
    cleaned = _clean_text(text)
    if not cleaned:
        return None, None

    class_match = re.search(r"\b(Cubs|Seals(?:\s+\d+\+)?|Beach Lions)\b", cleaned, re.IGNORECASE)
    class_name = class_match.group(1) if class_match else None

    when_patterns = [
        r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+[A-Z][a-z]+\s+\d{1,2}(?:st|nd|rd|th)?(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:AM|PM)(?:\s*-\s*\d{1,2}(?::\d{2})?\s*(?:AM|PM))?)?",
        r"\bthis\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)(?:(?:,\s*|\s+)\d{1,2}/\d{1,2})?(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:AM|PM)(?:\s*-\s*\d{1,2}(?::\d{2})?\s*(?:AM|PM))?)?",
        r"\b(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)'?s class(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:AM|PM))?",
        r"\b\d{1,2}/\d{1,2}(?:\s+at\s+\d{1,2}(?::\d{2})?\s*(?:AM|PM)(?:\s*-\s*\d{1,2}(?::\d{2})?\s*(?:AM|PM))?)?",
        r"\bSaturday\s+\d{1,2}:\d{2}\s*(?:AM|PM)\s*-\s*\d{1,2}:\d{2}\s*(?:AM|PM)",
    ]
    class_when = None
    for pattern in when_patterns:
        match = re.search(pattern, cleaned, re.IGNORECASE)
        if match:
            class_when = match.group(0)
            break

    if "free trial" not in cleaned.lower() and "roster" not in cleaned.lower() and "all set" not in cleaned.lower():
        if class_name is None or class_when is None:
            return None, None

    return class_name, class_when


def sync_salesmessage_to_unified(
    db_path: str,
    youth_inbox_id: int,
    cutoff_date: str = "2026-03-01",
) -> dict[str, int]:
    families = 0
    leads = 0
    outreach = 0
    now = _utc_now()
    with get_conn(db_path) as conn:
        conversations = conn.execute(
            """
            SELECT id, contact_id, inbox_id, last_message_at, raw_json
            FROM conversations
            WHERE coalesce(inbox_id, 0) = ?
              AND coalesce(last_message_at, '') >= ?
            ORDER BY coalesce(last_message_at, '') DESC
            """,
            (youth_inbox_id, cutoff_date),
        ).fetchall()
        for conv_row in conversations:
            conv = json.loads(conv_row["raw_json"])
            contact = conv.get("contact") if isinstance(conv.get("contact"), dict) else {}
            contact_id = contact.get("id") or conv_row["contact_id"]
            if contact_id is None:
                contact_id = conv_row["id"]

            family_key = f"salesmessage:{contact_id}"
            contact_name = contact.get("full_name") or (
                f"{contact.get('first_name', '')} {contact.get('last_name', '')}".strip() or None
            )
            contact_phone = contact.get("formatted_number") or contact.get("number")
            contact_email = contact.get("email")

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
                    family_status=excluded.family_status,
                    source_ref=excluded.source_ref,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    family_key,
                    contact_name,
                    contact_phone,
                    contact_email,
                    "active",
                    "salesmessage",
                    str(contact_id),
                    _json({"conversation_id": conv_row["id"], "contact": contact}),
                    now,
                    now,
                ),
            )
            families += 1

            tags: list[str] = []
            for tag in contact.get("tags", []) if isinstance(contact.get("tags"), list) else []:
                if isinstance(tag, dict) and isinstance(tag.get("name"), str):
                    tags.append(tag["name"].strip().lower())

            recent_message = (conv.get("recent_message") or {}).get("body", "")
            recent_body = _clean_text(recent_message).lower()
            trial_status = "unknown"
            if "youth" in " ".join(tags) or "free trial" in recent_body:
                trial_status = "invited"
            if "confirmed class" in tags or "confirmed" in recent_body or "see you" in recent_body:
                trial_status = "confirmed"
            if any(word in recent_body for word in ["reschedule", "can't", "cannot", "won't", "not coming"]):
                trial_status = "declined"

            lead_key = f"salesmessage:conversation:{conv_row['id']}"
            conn.execute(
                """
                INSERT INTO youth_trial_leads (
                    lead_key, family_key, inbox_id, contact_name, contact_phone, trial_status, account_created,
                    added_to_class, trial_class_name, trial_class_when, last_interaction_at,
                    source_system, source_ref, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lead_key) DO UPDATE SET
                    family_key=excluded.family_key,
                    inbox_id=excluded.inbox_id,
                    contact_name=excluded.contact_name,
                    contact_phone=excluded.contact_phone,
                    trial_status=excluded.trial_status,
                    account_created=CASE
                        WHEN youth_trial_leads.account_created = 1 THEN 1
                        ELSE excluded.account_created
                    END,
                    added_to_class=CASE
                        WHEN youth_trial_leads.added_to_class = 1 THEN 1
                        ELSE excluded.added_to_class
                    END,
                    trial_class_name=excluded.trial_class_name,
                    trial_class_when=excluded.trial_class_when,
                    last_interaction_at=excluded.last_interaction_at,
                    metadata_json=excluded.metadata_json,
                    updated_at=excluded.updated_at
                """,
                (
                    lead_key,
                    family_key,
                    int(conv_row["inbox_id"] or 0),
                    contact_name,
                    contact_phone,
                    trial_status,
                    0,
                    0,
                    None,
                    None,
                    conv_row["last_message_at"],
                    "salesmessage",
                    str(conv_row["id"]),
                    _json({"tags": tags, "recent_message": recent_message, "inbox_id": conv_row["inbox_id"]}),
                    now,
                    now,
                ),
            )
            leads += 1

        stale_rows = conn.execute(
            """
            SELECT lead_key
            FROM youth_trial_leads
            WHERE source_system = 'salesmessage'
              AND (
                coalesce(inbox_id, 0) != ?
                OR coalesce(last_interaction_at, '') < ?
              )
            """,
            (youth_inbox_id, cutoff_date),
        ).fetchall()
        stale_keys = [row["lead_key"] for row in stale_rows]
        if stale_keys:
            placeholders = ",".join("?" for _ in stale_keys)
            conn.execute(
                f"DELETE FROM youth_trial_leads WHERE lead_key IN ({placeholders})",
                stale_keys,
            )

    return {
        "families_upserted": families,
        "trial_leads_upserted": leads,
        "outreach_rows_processed": outreach,
    }


def sync_daysmart_to_unified(
    client: DaysmartClient,
    db_path: str,
    max_pages: int = 25,
    page_size: int = 200,
) -> dict[str, int]:
    families = 0
    children = 0
    attendance = 0
    customers = 0
    memberships = 0
    class_registrations = 0
    product_name_cache: dict[int, str | None] = {}
    event_cache: dict[int, tuple[str | None, str | None]] = {}
    with get_conn(db_path) as conn:
        first_data, customer_last_page = client.list_customers(page_number=1, page_size=page_size)
        if not first_data:
            start_page = 1
            customer_last_page = 0
        else:
            # Fresh hosted databases need the full customer directory to match older parent accounts.
            start_page = 1
        for page in range(start_page, customer_last_page + 1):
            data = first_data if page == 1 else client.list_customers(page_number=page, page_size=page_size)[0]
            if not data:
                continue
            for row in data:
                row_id = str(row.get("id"))
                attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
                _upsert_daysmart_customer(db_path, row, conn=conn)
                customers += 1

                household_id = attrs.get("household_id") or attrs.get("family_id") or row_id
                family_key = f"daysmart:family:{household_id}"

                first = attrs.get("first_name") or ""
                last = attrs.get("last_name") or ""
                full_name = attrs.get("full_name") or f"{first} {last}".strip() or f"Customer {row_id}"

                _upsert_family(
                    db_path,
                    family_key=family_key,
                    source_system="daysmart",
                    source_ref=str(household_id),
                    primary_contact_name=attrs.get("parent_name") or full_name,
                    primary_contact_phone=attrs.get("phone") or attrs.get("formatted_number"),
                    primary_contact_email=attrs.get("email"),
                    family_status="active" if not attrs.get("disabled") else "inactive",
                    metadata=attrs,
                    conn=conn,
                )
                families += 1

                child_key = f"daysmart:child:{row_id}"
                _upsert_child(
                    db_path,
                    child_key=child_key,
                    family_key=family_key,
                    child_name=full_name,
                    program_track=attrs.get("program") or attrs.get("group") or attrs.get("level"),
                    started_at=attrs.get("created_at") or attrs.get("created"),
                    is_active=not bool(attrs.get("disabled") or attrs.get("deleted_at")),
                    source_system="daysmart",
                    source_ref=row_id,
                    metadata=attrs,
                    conn=conn,
                )
                children += 1

        first_data, membership_last_page = client.list_memberships(page_number=1, page_size=page_size)
        if not first_data:
            start_page = 1
            membership_last_page = 0
        else:
            start_page = 1
        for page in range(start_page, membership_last_page + 1):
            data = first_data if page == 1 else client.list_memberships(page_number=page, page_size=page_size)[0]
            if not data:
                continue
            for row in data:
                attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
                product_id = _as_int(attrs.get("prod_id"))
                product_name = None
                if product_id is not None:
                    if product_id not in product_name_cache:
                        try:
                            product = client.get_product(product_id)
                            product_attrs = (
                                product.get("attributes") if isinstance(product.get("attributes"), dict) else {}
                            )
                            product_name_cache[product_id] = product_attrs.get("name")
                        except DaysmartApiError:
                            product_name_cache[product_id] = None
                    product_name = product_name_cache.get(product_id)
                _upsert_daysmart_membership(db_path, row, product_name=product_name, conn=conn)
                memberships += 1

        first_data, attendance_last_page = client.list_checkin_events(page_number=1, page_size=page_size)
        if first_data:
            start_page = max(1, attendance_last_page - max_pages + 1)
        else:
            start_page = 1
            attendance_last_page = 0
        for page in range(start_page, attendance_last_page + 1):
            data = first_data if page == 1 and start_page == 1 else client.list_checkin_events(
                page_number=page, page_size=page_size
            )[0]
            if not data:
                continue
            for row in data:
                row_id = str(row.get("id"))
                attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
                customer_id = attrs.get("customer_id") or attrs.get("contact_id") or attrs.get("member_id")
                if customer_id is None:
                    continue
                child_key = f"daysmart:child:{customer_id}"

                event_at = attrs.get("datetime") or attrs.get("created_at")
                if not isinstance(event_at, str) or not event_at.strip():
                    continue

                conn.execute(
                    """
                    INSERT INTO youth_attendance_events (
                        child_key, event_at, attendance_status, source_system,
                        source_event_id, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_system, source_event_id) DO UPDATE SET
                        child_key=excluded.child_key,
                        event_at=excluded.event_at,
                        attendance_status=excluded.attendance_status,
                        metadata_json=excluded.metadata_json
                    """,
                    (
                        child_key,
                        event_at,
                        attrs.get("status") or "checked_in",
                        "daysmart",
                        row_id,
                        _json(attrs),
                        _utc_now(),
                    ),
                )
                attendance += 1

        first_data, registration_last_page = client.list_registrations(page_number=1, page_size=page_size)
        if not first_data:
            start_page = 1
            registration_last_page = 0
        else:
            start_page = _find_cutoff_start_page(
                client.list_registrations,
                last_page=registration_last_page,
                page_size=page_size,
                cutoff=DAYSMART_HISTORY_CUTOFF,
                date_fields=("created_at", "created", "updated_at"),
            )
        for page in range(start_page, registration_last_page + 1):
            data = first_data if page == 1 and start_page == 1 else client.list_registrations(
                page_number=page, page_size=page_size
            )[0]
            if not data:
                continue
            for row in data:
                _upsert_daysmart_class_registration(
                    db_path,
                    source_type="registration",
                    row=row,
                    conn=conn,
                )
                class_registrations += 1

        first_data, event_registration_last_page = client.list_event_registrations(
            page_number=1,
            page_size=page_size,
        )
        if not first_data:
            start_page = 1
            event_registration_last_page = 0
        else:
            start_page = _find_cutoff_start_page(
                client.list_event_registrations,
                last_page=event_registration_last_page,
                page_size=page_size,
                cutoff=DAYSMART_HISTORY_CUTOFF,
                date_fields=("updated_at", "created_at", "date", "start"),
            )
        for page in range(start_page, event_registration_last_page + 1):
            data = first_data if page == 1 and start_page == 1 else client.list_event_registrations(
                page_number=page, page_size=page_size
            )[0]
            if not data:
                continue
            for row in data:
                attrs = row.get("attributes") if isinstance(row.get("attributes"), dict) else {}
                event_id = _as_int(attrs.get("event_id"))
                event_name = None
                event_start = None
                if event_id is not None:
                    cached = event_cache.get(event_id)
                    if cached is None:
                        try:
                            payload = client._get(f"/api/v1/events/{event_id}")
                            event = payload.get("data") if isinstance(payload, dict) else None
                            event_attrs = (
                                event.get("attributes")
                                if isinstance(event, dict) and isinstance(event.get("attributes"), dict)
                                else {}
                            )
                            event_name = event_attrs.get("desc") or event_attrs.get("name")
                            if not event_name and event_attrs.get("hteam_id"):
                                try:
                                    team_payload = client._get(f"/api/v1/teams/{int(event_attrs['hteam_id'])}")
                                    team = team_payload.get("data") if isinstance(team_payload, dict) else None
                                    team_attrs = (
                                        team.get("attributes")
                                        if isinstance(team, dict) and isinstance(team.get("attributes"), dict)
                                        else {}
                                    )
                                    event_name = team_attrs.get("name") or event_name
                                except DaysmartApiError:
                                    pass
                            cached = (
                                event_name,
                                event_attrs.get("start"),
                            )
                        except DaysmartApiError:
                            cached = (None, None)
                        event_cache[event_id] = cached
                    event_name, event_start = cached
                _upsert_daysmart_class_registration(
                    db_path,
                    source_type="event_registration",
                    row=row,
                    event_name=event_name,
                    event_start=event_start,
                    conn=conn,
                )
                class_registrations += 1

    return {
        "families_upserted": families,
        "children_upserted": children,
        "daysmart_customers_upserted": customers,
        "daysmart_memberships_upserted": memberships,
        "attendance_rows_processed": attendance,
        "daysmart_class_registrations_upserted": class_registrations,
    }


def recompute_risk_alerts(
    db_path: str,
    inactivity_days: int = 14,
    outreach_days: int = 30,
) -> dict[str, int]:
    now = dt.datetime.now(dt.timezone.utc)
    inactivity_cutoff = now - dt.timedelta(days=inactivity_days)
    outreach_cutoff = now - dt.timedelta(days=outreach_days)

    triggers: dict[tuple[str, str | None, str | None], dict[str, Any]] = {}

    with get_conn(db_path) as conn:
        child_rows = conn.execute(
            """
            SELECT c.child_key, c.family_key, c.child_name, c.is_active, max(a.event_at) AS last_event_at
            FROM youth_children c
            LEFT JOIN youth_attendance_events a ON a.child_key = c.child_key
            GROUP BY c.child_key, c.family_key, c.child_name, c.is_active
            """
        ).fetchall()

    for row in child_rows:
        if int(row["is_active"] or 0) != 1:
            continue
        last_event = _as_dt(row["last_event_at"])
        if last_event is None or last_event < inactivity_cutoff:
            rule = "inactivity_14d"
            triggers[(rule, row["family_key"], row["child_key"])] = {
                "severity": "high" if last_event is None else "medium",
                "details": {
                    "child_name": row["child_name"],
                    "last_event_at": row["last_event_at"],
                    "inactivity_days_threshold": inactivity_days,
                },
            }

    with get_conn(db_path) as conn:
        family_rows = conn.execute(
            """
            SELECT f.family_key, f.primary_contact_name, max(o.outreach_at) AS last_outreach_at
            FROM youth_families f
            LEFT JOIN youth_family_outreach o ON o.family_key = f.family_key
            GROUP BY f.family_key, f.primary_contact_name
            """
        ).fetchall()

    for row in family_rows:
        last_outreach = _as_dt(row["last_outreach_at"])
        if last_outreach is None or last_outreach < outreach_cutoff:
            rule = "no_outreach_30d"
            triggers[(rule, row["family_key"], None)] = {
                "severity": "medium",
                "details": {
                    "primary_contact_name": row["primary_contact_name"],
                    "last_outreach_at": row["last_outreach_at"],
                    "outreach_days_threshold": outreach_days,
                },
            }

    with get_conn(db_path) as conn:
        lead_rows = conn.execute(
            """
            SELECT lead_key, family_key, contact_name, trial_status, account_created,
                   added_to_class, last_interaction_at
            FROM youth_trial_leads
            """
        ).fetchall()

    for row in lead_rows:
        trial_status = (row["trial_status"] or "").lower()
        if trial_status not in {"confirmed", "invited", "scheduled"}:
            continue
        if int(row["account_created"] or 0) == 1 and int(row["added_to_class"] or 0) == 1:
            continue
        rule = "trial_followup_missing_setup"
        triggers[(rule, row["family_key"], None)] = {
            "severity": "high" if trial_status == "confirmed" else "medium",
            "details": {
                "lead_key": row["lead_key"],
                "contact_name": row["contact_name"],
                "trial_status": row["trial_status"],
                "account_created": int(row["account_created"] or 0),
                "added_to_class": int(row["added_to_class"] or 0),
                "last_interaction_at": row["last_interaction_at"],
            },
        }

    opened = 0
    with get_conn(db_path) as conn:
        for (rule_code, family_key, child_key), payload in triggers.items():
            now_iso = _utc_now()
            conn.execute(
                """
                INSERT INTO youth_risk_alerts (
                    family_key, child_key, scope_key, rule_code, severity,
                    status, details_json, last_triggered_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?)
                ON CONFLICT(rule_code, scope_key) DO UPDATE SET
                    family_key=excluded.family_key,
                    child_key=excluded.child_key,
                    severity=excluded.severity,
                    status='open',
                    details_json=excluded.details_json,
                    last_triggered_at=excluded.last_triggered_at,
                    updated_at=excluded.updated_at
                """,
                (
                    family_key,
                    child_key,
                    _scope_key(family_key, child_key),
                    rule_code,
                    payload["severity"],
                    _json(payload["details"]),
                    now_iso,
                    now_iso,
                    now_iso,
                ),
            )
            opened += 1

        active = conn.execute(
            "SELECT alert_id, rule_code, family_key, child_key FROM youth_risk_alerts WHERE status = 'open'"
        ).fetchall()

        active_keys = {
            (row["rule_code"], row["family_key"], row["child_key"]): row["alert_id"]
            for row in active
        }
        trigger_keys = set(triggers.keys())

        resolved_ids = [
            alert_id for key, alert_id in active_keys.items() if key not in trigger_keys
        ]
        if resolved_ids:
            placeholders = ",".join("?" for _ in resolved_ids)
            conn.execute(
                f"UPDATE youth_risk_alerts SET status='resolved', updated_at=? WHERE alert_id IN ({placeholders})",
                (_utc_now(), *resolved_ids),
            )

    return {
        "open_alerts": len(triggers),
        "rules_triggered": len({k[0] for k in triggers.keys()}),
    }
