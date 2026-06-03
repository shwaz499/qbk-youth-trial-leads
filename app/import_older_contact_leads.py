from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .config import get_settings
from .youth_kpis import _normalize_email, _normalize_name, _normalize_phone

YOUTH_LABEL_TOKENS = (
    "youth trial class",
    "seals trial class",
    "grades 6+ trial class",
)


def _parse_created_at(value: str | None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            parsed = dt.datetime.strptime(cleaned, fmt).replace(tzinfo=dt.timezone.utc)
            return parsed.isoformat()
        except ValueError:
            continue
    return None


def _full_name(row: dict[str, str]) -> str:
    parts = [str(row.get("First Name") or "").strip(), str(row.get("Last Name") or "").strip()]
    return " ".join(part for part in parts if part).strip()


def _row_is_youth_trial(row: dict[str, str]) -> bool:
    labels = (row.get("Labels") or "").lower()
    return any(token in labels for token in YOUTH_LABEL_TOKENS)


def _load_existing_identities(conn: sqlite3.Connection) -> tuple[set[str], set[str], set[str]]:
    phones: set[str] = set()
    emails: set[str] = set()
    names: set[str] = set()
    rows = conn.execute(
        """
        SELECT contact_name, contact_phone, metadata_json
        FROM youth_trial_leads
        """
    ).fetchall()
    for row in rows:
        metadata: dict[str, Any] = {}
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except json.JSONDecodeError:
            metadata = {}
        contact = metadata.get("contact") if isinstance(metadata, dict) else {}
        if not isinstance(contact, dict):
            contact = {}
        phone = _normalize_phone(row["contact_phone"] or contact.get("phone") or contact.get("number"))
        email = _normalize_email(contact.get("email"))
        name = _normalize_name(row["contact_name"] or contact.get("full_name"))
        if phone:
            phones.add(phone)
        if email:
            emails.add(email)
        if name:
            names.add(name)
    return phones, emails, names


def _make_identity(phone: str | None, email: str | None, name: str | None) -> str | None:
    if phone:
        return f"phone:{phone}"
    if email:
        return f"email:{email}"
    if name:
        return f"name:{name}"
    return None


def _make_family_key(identity: str) -> str:
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()
    return f"legacy_csv:family:{digest[:16]}"


def _make_source_ref(identity: str) -> str:
    digest = hashlib.sha1(f"legacy_csv:{identity}".encode("utf-8")).hexdigest()
    return str(int(digest[:15], 16))


def import_csv(path: Path) -> dict[str, int]:
    settings = get_settings()
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    conn = sqlite3.connect(settings.database_url, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        existing_phones, existing_emails, existing_names = _load_existing_identities(conn)
        earliest_existing = conn.execute(
            """
            WITH first_message_times AS (
                SELECT conversation_id, MIN(coalesce(created_at, sent_at, received_at, '')) AS first_message_at
                FROM messages
                GROUP BY conversation_id
            )
            SELECT MIN(coalesce(fmt.first_message_at, c.started_at, t.last_interaction_at, c.last_message_at))
            FROM youth_trial_leads t
            LEFT JOIN conversations c ON c.id = CAST(t.source_ref AS INTEGER)
            LEFT JOIN first_message_times fmt ON fmt.conversation_id = c.id
            """
        ).fetchone()[0]

        imported = 0
        skipped_existing = 0
        skipped_not_youth = 0
        skipped_not_older = 0
        seen_identities: dict[str, dict[str, Any]] = {}

        with path.open(newline="", encoding="utf-8-sig") as handle:
            for row in csv.DictReader(handle):
                if not _row_is_youth_trial(row):
                    skipped_not_youth += 1
                    continue

                created_at = _parse_created_at(row.get("Created At (UTC+0)"))
                if not created_at:
                    continue
                if earliest_existing and created_at >= str(earliest_existing):
                    skipped_not_older += 1
                    continue

                full_name = _full_name(row)
                phone = _normalize_phone(row.get("Phone 1") or row.get("Phone 2") or row.get("Phone 3"))
                email = _normalize_email(row.get("Email 1"))
                name = _normalize_name(full_name)
                identity = _make_identity(phone, email, name)
                if identity is None:
                    continue

                if (phone and phone in existing_phones) or (email and email in existing_emails) or (name and name in existing_names):
                    skipped_existing += 1
                    continue

                existing_row = seen_identities.get(identity)
                if existing_row is None or created_at < existing_row["created_at"]:
                    seen_identities[identity] = {
                        "row": row,
                        "created_at": created_at,
                        "full_name": full_name or (email or phone or "Unknown lead"),
                        "phone": row.get("Phone 1") or row.get("Phone 2") or row.get("Phone 3") or "",
                        "email": row.get("Email 1") or "",
                        "identity": identity,
                    }

        for payload in seen_identities.values():
            row = payload["row"]
            family_key = _make_family_key(payload["identity"])
            source_ref = _make_source_ref(payload["identity"])
            lead_key = f"legacy_csv:lead:{source_ref}"
            full_name = payload["full_name"]
            phone_raw = payload["phone"]
            email_raw = payload["email"]
            created_at = payload["created_at"]
            metadata = {
                "contact": {
                    "full_name": full_name,
                    "email": email_raw,
                    "phone": phone_raw,
                },
                "legacy_contact_csv": {
                    "source_file": path.name,
                    "labels": row.get("Labels") or "",
                    "created_at_utc": row.get("Created At (UTC+0)") or "",
                    "source": row.get("Source") or "",
                    "how_did_you_hear_about_us": row.get("How Did You Hear About Us?") or "",
                    "additional_details": row.get("Enter in any additional details") or "",
                    "raw_row": row,
                },
            }
            conn.execute(
                """
                INSERT OR IGNORE INTO youth_families (
                    family_key, primary_contact_name, primary_contact_phone, primary_contact_email,
                    family_status, source_system, source_ref, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    family_key,
                    full_name,
                    phone_raw,
                    email_raw,
                    "legacy_import",
                    "legacy_csv",
                    source_ref,
                    json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO youth_trial_leads (
                    lead_key, family_key, contact_name, contact_phone, trial_status,
                    account_created, waiver_completed, last_interaction_at, source_system, source_ref,
                    metadata_json, created_at, updated_at, inbox_id, added_to_class, trial_class_name, trial_class_when
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lead_key,
                    family_key,
                    full_name,
                    phone_raw,
                    "legacy_csv",
                    0,
                    0,
                    created_at,
                    "legacy_csv",
                    source_ref,
                    json.dumps(metadata, separators=(",", ":"), sort_keys=True),
                    created_at,
                    now,
                    None,
                    0,
                    None,
                    None,
                ),
            )
            imported += 1

        conn.commit()
    finally:
        conn.close()

    return {
        "imported": imported,
        "skipped_existing": skipped_existing,
        "skipped_not_youth": skipped_not_youth,
        "skipped_not_older": skipped_not_older,
        "deduped_candidates": len(seen_identities),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m app.import_older_contact_leads <csv>", file=sys.stderr)
        return 1
    result = import_csv(Path(argv[1]).expanduser().resolve())
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
