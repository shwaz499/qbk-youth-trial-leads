from __future__ import annotations

import datetime as dt
import json
import sqlite3
from typing import Any, Callable

from .db import get_conn
from .salesmessage import SalesmessageApiError, SalesmessageClient


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_salesmessage_timestamp(value: str | None) -> dt.datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip().replace("Z", "+00:00")
    for candidate in (cleaned, cleaned.replace(" ", "T")):
        try:
            parsed = dt.datetime.fromisoformat(candidate)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.astimezone(dt.timezone.utc)
        except ValueError:
            continue
    return None


def _default_min_last_message_at(
    conn: sqlite3.Connection,
    explicit_value: str | None,
) -> dt.datetime | None:
    if explicit_value:
        return _parse_salesmessage_timestamp(explicit_value)
    row = conn.execute("SELECT MAX(last_message_at) AS ts FROM conversations").fetchone()
    latest = row["ts"] if row else None
    return _parse_salesmessage_timestamp(latest)


def _participant_name(conv: dict[str, Any]) -> str | None:
    participants = conv.get("participants")
    if not isinstance(participants, list) or not participants:
        return conv.get("name")
    first = participants[0]
    if not isinstance(first, dict):
        return conv.get("name")
    full_name = first.get("full_name")
    if isinstance(full_name, str) and full_name.strip():
        return full_name
    first_name = first.get("first_name") or ""
    last_name = first.get("last_name") or ""
    candidate = f"{first_name} {last_name}".strip()
    return candidate or conv.get("name")


def _participant_number(conv: dict[str, Any]) -> str | None:
    participants = conv.get("participants")
    if isinstance(participants, list) and participants:
        first = participants[0]
        if isinstance(first, dict):
            number = first.get("number") or first.get("formatted_number")
            if isinstance(number, str):
                return number
    return None


def _upsert_conversation(conn: sqlite3.Connection, conv: dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO conversations (
            id, contact_id, contact_name, contact_number, owner_id, inbox_id,
            started_at, closed_at, last_message_at, raw_json, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            contact_id=excluded.contact_id,
            contact_name=excluded.contact_name,
            contact_number=excluded.contact_number,
            owner_id=excluded.owner_id,
            inbox_id=excluded.inbox_id,
            started_at=excluded.started_at,
            closed_at=excluded.closed_at,
            last_message_at=excluded.last_message_at,
            raw_json=excluded.raw_json,
            updated_at=excluded.updated_at
        """,
        (
            conv.get("id"),
            conv.get("contact_id"),
            _participant_name(conv),
            _participant_number(conv),
            ((conv.get("owner") or {}).get("id") if isinstance(conv.get("owner"), dict) else None),
            conv.get("inbox_id"),
            conv.get("started_at"),
            conv.get("closed_at"),
            conv.get("last_message_at"),
            json.dumps(conv, ensure_ascii=True),
            _utc_now(),
        ),
    )


def _upsert_messages(conn: sqlite3.Connection, messages: list[dict[str, Any]]) -> int:
    if not messages:
        return 0
    for msg in messages:
        contact = msg.get("contact") if isinstance(msg.get("contact"), dict) else {}
        conn.execute(
            """
            INSERT INTO messages (
                id, conversation_id, body, status, message_type, source, created_at,
                sent_at, received_at, user_id, contact_id, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                body=excluded.body,
                status=excluded.status,
                message_type=excluded.message_type,
                source=excluded.source,
                created_at=excluded.created_at,
                sent_at=excluded.sent_at,
                received_at=excluded.received_at,
                user_id=excluded.user_id,
                contact_id=excluded.contact_id,
                raw_json=excluded.raw_json
            """,
            (
                msg.get("id"),
                msg.get("conversation_id"),
                msg.get("body"),
                msg.get("status"),
                msg.get("type"),
                msg.get("source"),
                msg.get("created_at"),
                msg.get("sent_at"),
                msg.get("received_at"),
                msg.get("user_id"),
                contact.get("id"),
                json.dumps(msg, ensure_ascii=True),
            ),
        )
    return len(messages)


def _load_existing_conversation_state(
    conn: sqlite3.Connection,
) -> tuple[dict[int, str | None], dict[int, int]]:
    last_message_at_by_conversation = {
        int(row["id"]): row["last_message_at"]
        for row in conn.execute("SELECT id, last_message_at FROM conversations").fetchall()
        if row["id"] is not None
    }
    latest_message_id_by_conversation = {
        int(row["conversation_id"]): int(row["latest_message_id"])
        for row in conn.execute(
            """
            SELECT conversation_id, MAX(id) AS latest_message_id
            FROM messages
            GROUP BY conversation_id
            """
        ).fetchall()
        if row["conversation_id"] is not None and row["latest_message_id"] is not None
    }
    return last_message_at_by_conversation, latest_message_id_by_conversation


def _existing_message_ids(conn: sqlite3.Connection, message_ids: list[int]) -> set[int]:
    if not message_ids:
        return set()
    placeholders = ",".join("?" for _ in message_ids)
    rows = conn.execute(
        f"SELECT id FROM messages WHERE id IN ({placeholders})",
        message_ids,
    ).fetchall()
    return {int(row["id"]) for row in rows if row["id"] is not None}


def _should_fetch_messages(
    remote_last_message_at: str | None,
    known_last_message_at: str | None,
    known_latest_message_id: int | None,
) -> bool:
    if known_latest_message_id is None:
        return True
    return remote_last_message_at != known_last_message_at


def sync_conversations(
    client: SalesmessageClient,
    db_path: str,
    filters: list[str],
    conv_page_size: int = 100,
    message_page_size: int = 100,
    max_message_pages_per_conversation: int = 2,
    target_inbox_ids: set[int] | None = None,
    min_last_message_at: str | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, int]:
    conversation_count = 0
    message_count = 0
    conversations_unchanged = 0
    message_pages_skipped = 0
    conversations_failed = 0
    conversations_filtered_out = 0
    conversations_before_cutoff = 0

    def report_progress(
        current_filter: str,
        offset_value: int,
        conversation_id: int | None = None,
        phase: str | None = None,
        message_page: int | None = None,
    ) -> None:
        if progress_callback is None:
            return
        progress_callback(
            {
                "salesmessage_progress": {
                    "filter": current_filter,
                    "offset": offset_value,
                    "conversation_id": conversation_id,
                    "phase": phase,
                    "message_page": message_page,
                    "conversations_synced": conversation_count,
                    "messages_synced": message_count,
                    "conversations_unchanged": conversations_unchanged,
                    "message_pages_skipped": message_pages_skipped,
                    "conversations_failed": conversations_failed,
                    "conversations_filtered_out": conversations_filtered_out,
                    "conversations_before_cutoff": conversations_before_cutoff,
                }
            }
        )

    with get_conn(db_path) as conn:
        cutoff_dt = _default_min_last_message_at(conn, min_last_message_at)
        known_last_message_at, known_latest_message_id = _load_existing_conversation_state(conn)

        seen_ids: set[int] = set()
        for filter_name in filters:
            offset = 0
            while True:
                conversations = client.list_conversations(
                    filter_name=filter_name,
                    limit=conv_page_size,
                    offset=offset,
                    inbox_id=(next(iter(target_inbox_ids)) if target_inbox_ids and len(target_inbox_ids) == 1 else None),
                )
                if not conversations:
                    break

                page_has_eligible_conversation = False
                for conv in conversations:
                    conv_id = conv.get("id")
                    if not isinstance(conv_id, int) or conv_id in seen_ids:
                        continue
                    seen_ids.add(conv_id)

                    remote_last_message_at = _parse_salesmessage_timestamp(conv.get("last_message_at"))
                    if cutoff_dt and remote_last_message_at and remote_last_message_at < cutoff_dt:
                        conversations_before_cutoff += 1
                        report_progress(filter_name, offset, conv_id)
                        continue
                    page_has_eligible_conversation = True

                    existing_last_message_at = known_last_message_at.get(conv_id)
                    existing_latest_message_id = known_latest_message_id.get(conv_id)

                    _upsert_conversation(conn, conv)
                    conversation_count += 1

                    inbox_id = conv.get("inbox_id")
                    if target_inbox_ids and inbox_id not in target_inbox_ids:
                        conversations_filtered_out += 1
                        report_progress(filter_name, offset, conv_id)
                        continue

                    if not _should_fetch_messages(
                        remote_last_message_at=conv.get("last_message_at"),
                        known_last_message_at=existing_last_message_at,
                        known_latest_message_id=existing_latest_message_id,
                    ):
                        conversations_unchanged += 1
                        report_progress(filter_name, offset, conv_id)
                        continue

                    page = 1
                    while True:
                        report_progress(
                            filter_name,
                            offset,
                            conv_id,
                            phase="fetching_messages",
                            message_page=page,
                        )
                        try:
                            batch, meta = client.get_messages_paginated(
                                conversation_id=conv_id,
                                per_page=message_page_size,
                                page=page,
                            )
                        except SalesmessageApiError:
                            conversations_failed += 1
                            break
                        if not batch:
                            break

                        batch_ids = [int(msg["id"]) for msg in batch if isinstance(msg.get("id"), int)]
                        existing_ids = _existing_message_ids(conn, batch_ids)
                        new_messages = [
                            msg
                            for msg in batch
                            if isinstance(msg.get("id"), int) and int(msg["id"]) not in existing_ids
                        ]
                        message_count += _upsert_messages(conn, new_messages)

                        if batch_ids:
                            known_latest_message_id[conv_id] = max(batch_ids)
                        known_last_message_at[conv_id] = conv.get("last_message_at")

                        if existing_ids:
                            message_pages_skipped += 1
                            break

                        last_page = meta.get("last_page")
                        current_page = meta.get("current_page", page)
                        if isinstance(last_page, int) and isinstance(current_page, int):
                            if current_page >= last_page:
                                break
                        if page >= max_message_pages_per_conversation:
                            break
                        if len(batch) < message_page_size:
                            break
                        page += 1

                    report_progress(filter_name, offset, conv_id)

                if len(conversations) < conv_page_size:
                    break
                if cutoff_dt and not page_has_eligible_conversation:
                    break
                offset += conv_page_size
                report_progress(filter_name, offset, None)

    return {
        "conversations_synced": conversation_count,
        "messages_synced": message_count,
        "conversations_unchanged": conversations_unchanged,
        "message_pages_skipped": message_pages_skipped,
        "conversations_failed": conversations_failed,
        "conversations_filtered_out": conversations_filtered_out,
        "conversations_before_cutoff": conversations_before_cutoff,
    }
