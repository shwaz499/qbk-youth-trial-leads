from __future__ import annotations

import datetime as dt
import json
from typing import Any

from .db import get_conn
from .salesmessage import SalesmessageClient


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


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


def upsert_conversation(db_path: str, conv: dict[str, Any]) -> None:
    with get_conn(db_path) as conn:
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


def upsert_messages(db_path: str, messages: list[dict[str, Any]]) -> int:
    if not messages:
        return 0
    with get_conn(db_path) as conn:
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


def sync_conversations(
    client: SalesmessageClient,
    db_path: str,
    filters: list[str],
    conv_page_size: int = 100,
    message_page_size: int = 100,
) -> dict[str, int]:
    conversation_count = 0
    message_count = 0

    seen_ids: set[int] = set()
    for filter_name in filters:
        offset = 0
        while True:
            conversations = client.list_conversations(
                filter_name=filter_name,
                limit=conv_page_size,
                offset=offset,
            )
            if not conversations:
                break

            for conv in conversations:
                conv_id = conv.get("id")
                if not isinstance(conv_id, int) or conv_id in seen_ids:
                    continue
                seen_ids.add(conv_id)

                upsert_conversation(db_path, conv)
                conversation_count += 1

                page = 1
                while True:
                    batch, meta = client.get_messages_paginated(
                        conversation_id=conv_id,
                        per_page=message_page_size,
                        page=page,
                    )
                    if not batch:
                        break
                    message_count += upsert_messages(db_path, batch)

                    last_page = meta.get("last_page")
                    current_page = meta.get("current_page", page)
                    if isinstance(last_page, int) and isinstance(current_page, int):
                        if current_page >= last_page:
                            break
                    if len(batch) < message_page_size:
                        break
                    page += 1

            if len(conversations) < conv_page_size:
                break
            offset += conv_page_size

    return {
        "conversations_synced": conversation_count,
        "messages_synced": message_count,
    }
