from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .config import get_settings
from .import_eventbrite_gmail_orders import import_live_eventbrite_messages
from .youth_kpis import build_youth_kpi_dashboard, build_youth_kpi_timeseries


WINDOWS: tuple[tuple[str, int | None, str | None], ...] = (
    ("days_7", 7, None),
    ("days_14", 14, None),
    ("days_30", 30, None),
    ("window_this_year", None, "this_year"),
    ("window_all_time", None, "all_time"),
)

GRANULARITIES = ("month", "week", "quarter", "year")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))


def export_snapshot(output_dir: Path, *, force_refresh: bool = False) -> dict[str, Any]:
    settings = get_settings()
    written: list[str] = []
    try:
        live_gmail_eventbrite: dict[str, Any] = dict(import_live_eventbrite_messages(max_results=100))
    except Exception as exc:
        live_gmail_eventbrite = {
            "source": "live-gmail-eventbrite",
            "fetched": 0,
            "imported": 0,
            "skipped_existing": 0,
            "skipped_similar": 0,
            "skipped_not_matching": 0,
            "errors": 1,
            "detail": str(exc)[:240],
        }

    for key, days, window in WINDOWS:
        dashboard = build_youth_kpi_dashboard(
            settings.database_url,
            youth_inbox_id=settings.youth_inbox_id,
            days=days or 7,
            window=window,
            include_attendance=True,
            force_refresh=force_refresh,
        )
        dashboard["snapshot_mode"] = True
        _write_json(output_dir / f"dashboard__{key}.json", dashboard)
        written.append(f"dashboard__{key}.json")

        for granularity in GRANULARITIES:
            timeseries = build_youth_kpi_timeseries(
                settings.database_url,
                youth_inbox_id=settings.youth_inbox_id,
                days=days or 7,
                window=window,
                granularity=granularity,
                force_refresh=False,
            )
            timeseries["snapshot_mode"] = True
            path = output_dir / f"timeseries__{key}__{granularity}.json"
            _write_json(path, timeseries)
            written.append(path.name)

    manifest = {
        "ok": True,
        "files": written,
        "file_count": len(written),
        "live_gmail_eventbrite": live_gmail_eventbrite,
    }
    _write_json(output_dir / "manifest.json", manifest)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Youth KPI data for Render snapshot mode.")
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().parent / "snapshot"),
        help="Directory where snapshot JSON files should be written.",
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Refresh local live data before exporting. This should only be used locally, not on Render.",
    )
    args = parser.parse_args()
    manifest = export_snapshot(Path(args.output_dir), force_refresh=args.force_refresh)
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
