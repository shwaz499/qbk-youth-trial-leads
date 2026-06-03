from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

EVENTBRITE_SUBJECTS = {
    "Order Notification for Free Youth Indoor Beach Volleyball Class (Ages 10-17)",
    "Order Notification for Free Youth Indoor Beach Volleyball Class (Ages 10-12)",
    "Order Notification for Free Youth Indoor Beach Volleyball Class (Ages 13-17)",
    "Order Notification for Free Youth Indoor Beach Volleyball Class (Ages 6-9)",
}
DEFAULT_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
UTC = dt.timezone.utc


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(cleaned)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _iter_session_messages(sessions_root: Path) -> list[dict[str, Any]]:
    recovered: dict[str, dict[str, Any]] = {}
    for jsonl_path in sorted(sessions_root.rglob("*.jsonl")):
        try:
            stream = jsonl_path.open()
        except OSError:
            continue
        with stream:
            for line in stream:
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                if payload.get("type") != "mcp_tool_call_end":
                    continue
                invocation = payload.get("invocation")
                if not isinstance(invocation, dict):
                    continue
                if invocation.get("tool") not in {"gmail_batch_read_email", "gmail_read_email"}:
                    continue
                ok = payload.get("result", {}).get("Ok", {})
                contents = ok.get("content")
                if not isinstance(contents, list):
                    continue
                for item in contents:
                    if not isinstance(item, dict) or item.get("type") != "text":
                        continue
                    text = item.get("text")
                    if not isinstance(text, str):
                        continue
                    try:
                        tool_payload = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    responses = tool_payload.get("responses")
                    if not isinstance(responses, list):
                        continue
                    for response in responses:
                        if not isinstance(response, dict):
                            continue
                        subject = str(response.get("subject") or "").strip()
                        if subject not in EVENTBRITE_SUBJECTS:
                            continue
                        message_id = str(response.get("id") or "").strip()
                        if not message_id:
                            continue
                        existing = recovered.get(message_id)
                        if existing is None or len(str(response.get("body") or "")) > len(
                            str(existing.get("body") or "")
                        ):
                            recovered[message_id] = response
    return list(recovered.values())


def recover_messages(sessions_root: Path, since: dt.datetime) -> list[dict[str, Any]]:
    recovered = _iter_session_messages(sessions_root)
    filtered: list[dict[str, Any]] = []
    for message in recovered:
        ts = _parse_ts(message.get("email_ts"))
        if ts is None or ts < since:
            continue
        filtered.append(message)
    filtered.sort(key=lambda item: (_parse_ts(item.get("email_ts")) or since, str(item.get("id") or "")))
    return filtered


def main(argv: list[str]) -> int:
    if len(argv) not in {2, 3}:
        print(
            "usage: python -m app.recover_eventbrite_gmail_from_sessions <output.json> [sessions_root]",
            file=sys.stderr,
        )
        return 1
    output_path = Path(argv[1]).expanduser().resolve()
    sessions_root = Path(argv[2]).expanduser().resolve() if len(argv) == 3 else DEFAULT_SESSIONS_ROOT
    since = dt.datetime(2025, 1, 1, tzinfo=UTC)
    messages = recover_messages(sessions_root, since)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(messages, indent=2))
    summary = {
        "output": str(output_path),
        "sessions_root": str(sessions_root),
        "since": since.isoformat(),
        "count": len(messages),
        "subjects": sorted({str(message.get("subject") or "").strip() for message in messages}),
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
