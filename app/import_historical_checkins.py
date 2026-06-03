from __future__ import annotations

import csv
import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import get_settings
from .db import get_conn

LOCAL_TZ = ZoneInfo("America/New_York")


def _parse_visit_ts(value: str | None) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    for fmt in ("%m/%d/%Y %I:%M%p", "%m/%d/%Y %I:%M %p"):
        try:
            parsed = dt.datetime.strptime(cleaned, fmt)
            return parsed.replace(tzinfo=LOCAL_TZ)
        except ValueError:
            continue
    return None


def _iter_rows(path: Path):
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            customer_id = (row.get("Customer ID #") or "").strip()
            first_name = (row.get("First Name") or "").strip()
            last_name = (row.get("Last Name") or "").strip()
            visit_raw = (row.get("Date of Visit") or "").strip()
            if not customer_id.isdigit():
                continue
            if not first_name and not last_name:
                continue
            visit_at = _parse_visit_ts(visit_raw)
            if visit_at is None:
                continue
            yield {
                "customer_id": int(customer_id),
                "child_key": f"daysmart:child:{int(customer_id)}",
                "visit_at": visit_at.astimezone(dt.timezone.utc).isoformat(),
                "source_event_id": f"historical_csv:{int(customer_id)}:{visit_at.astimezone(dt.timezone.utc).isoformat()}",
                "metadata": {
                    "location": (row.get("Location") or "").strip() or "QBK SPORTS",
                    "customer_id": int(customer_id),
                    "first_name": first_name,
                    "last_name": last_name,
                    "date_of_visit": visit_raw,
                    "source_file": path.name,
                },
            }


def import_csvs(paths: list[Path]) -> dict[str, int]:
    settings = get_settings()
    inserted = 0
    updated = 0
    seen = 0
    with get_conn(settings.database_url) as conn:
        for path in paths:
            for row in _iter_rows(path):
                seen += 1
                before = conn.total_changes
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
                        row["child_key"],
                        row["visit_at"],
                        "checked_in",
                        "daysmart_historical_csv",
                        row["source_event_id"],
                        json.dumps(row["metadata"], separators=(",", ":"), sort_keys=True),
                        dt.datetime.now(dt.timezone.utc).isoformat(),
                    ),
                )
                delta = conn.total_changes - before
                if delta > 0:
                    inserted += 1
        conn.commit()
    return {"rows_seen": seen, "rows_upserted": inserted, "rows_updated": updated}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python -m app.import_historical_checkins <csv> [<csv> ...]", file=sys.stderr)
        return 1
    paths = [Path(arg).expanduser().resolve() for arg in argv[1:]]
    result = import_csvs(paths)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
