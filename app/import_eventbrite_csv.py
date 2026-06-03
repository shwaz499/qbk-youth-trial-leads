from __future__ import annotations

import csv
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

from .config import get_settings

EVENTBRITE_ALLOWED_EVENTS = {
    "Free Youth Indoor Beach Volleyball Class (Ages 10-17)",
    "Free Youth Indoor Beach Volleyball Class (Ages 10-12)",
    "Free Youth Indoor Beach Volleyball Class (Ages 13-17)",
    "Free Youth Indoor Beach Volleyball Class (Ages 6-9)",
}


def _clean(value: str | None) -> str:
    return str(value or "").strip()


def _full_name(first: str | None, last: str | None) -> str:
    return " ".join(part for part in (_clean(first), _clean(last)) if part).strip()


def _load_existing_by_order(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    rows = conn.execute(
        """
        SELECT lead_key, family_key, source_ref, contact_phone
        FROM youth_trial_leads
        WHERE source_system = 'gmail_eventbrite'
          AND source_ref IS NOT NULL
        """
    ).fetchall()
    return {str(row["source_ref"]): row for row in rows if row["source_ref"]}


def import_csv(path: str | Path) -> dict[str, int]:
    settings = get_settings()
    csv_path = Path(path).expanduser().resolve()

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        rows = [row for row in csv.DictReader(f) if _clean(row.get("Event name")) in EVENTBRITE_ALLOWED_EVENTS]

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        order_id = _clean(row.get("Order ID"))
        if order_id:
            grouped[order_id].append(row)

    conn = sqlite3.connect(settings.database_url, timeout=30)
    conn.row_factory = sqlite3.Row
    updated = 0
    phone_filled = 0
    skipped_missing_order = 0
    skipped_existing_phone = 0
    skipped_missing_phone = 0
    try:
        existing_by_order = _load_existing_by_order(conn)
        for order_id, order_rows in grouped.items():
            existing = existing_by_order.get(order_id)
            if existing is None:
                skipped_missing_order += 1
                continue
            existing_phone = _clean(existing["contact_phone"])
            if existing_phone:
                skipped_existing_phone += 1
                continue

            first = order_rows[0]
            parent_phone = _clean(first.get("Phone number"))
            if not parent_phone:
                skipped_missing_phone += 1
                continue

            conn.execute(
                """
                UPDATE youth_trial_leads
                SET contact_phone = ?
                WHERE lead_key = ?
                  AND coalesce(contact_phone, '') = ''
                """,
                (parent_phone, existing["lead_key"]),
            )
            conn.execute(
                """
                UPDATE youth_families
                SET primary_contact_phone = ?
                WHERE family_key = ?
                  AND coalesce(primary_contact_phone, '') = ''
                """,
                (parent_phone, existing["family_key"]),
            )
            updated += 1
            phone_filled += 1
        conn.commit()
    finally:
        conn.close()

    return {
        "csv_rows": len(rows),
        "orders": len(grouped),
        "updated": updated,
        "phone_filled": phone_filled,
        "skipped_missing_order": skipped_missing_order,
        "skipped_existing_phone": skipped_existing_phone,
        "skipped_missing_phone": skipped_missing_phone,
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m app.import_eventbrite_csv <eventbrite-attendees.csv>", file=sys.stderr)
        return 1
    print(json.dumps(import_csv(argv[1]), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
