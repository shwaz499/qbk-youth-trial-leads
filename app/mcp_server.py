from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .analysis import answer_locally, answer_with_llm, get_recent_messages, search_messages
from .config import get_settings
from .db import get_conn, init_db
from .daysmart import DaysmartApiError, DaysmartClient
from .ingest import sync_conversations
from .salesmessage import SalesmessageApiError, SalesmessageClient
from .unified import recompute_risk_alerts, sync_daysmart_to_unified, sync_salesmessage_to_unified

settings = get_settings()
init_db(settings.database_url)

mcp = FastMCP("salesmessage-agent")


def _load_context(
    question: str,
    search_query: str | None,
    conversation_id: int | None,
    max_context_messages: int,
) -> list[dict[str, Any]]:
    query = search_query or question
    context = []
    try:
        context = search_messages(
            db_path=settings.database_url,
            query=query,
            limit=max_context_messages,
            conversation_id=conversation_id,
        )
    except Exception:
        context = []

    if not context:
        context = get_recent_messages(
            db_path=settings.database_url,
            limit=max_context_messages,
            conversation_id=conversation_id,
        )
    return context


@mcp.tool()
def sync_salesmessage(
    filters: list[str] | None = None,
    conversation_page_size: int = 100,
    message_page_size: int = 25,
) -> dict[str, Any]:
    """Sync conversations and messages from Salesmessage into local DB."""
    client = SalesmessageClient(
        token=settings.salesmessage_api_token,
        base_url=settings.salesmessage_base_url,
    )
    try:
        stats = sync_conversations(
            client=client,
            db_path=settings.database_url,
            filters=filters or ["open", "closed", "unread", "assigned", "unassigned"],
            conv_page_size=conversation_page_size,
            message_page_size=message_page_size,
            max_message_pages_per_conversation=1,
        )
    except SalesmessageApiError as exc:
        return {"ok": False, "error": str(exc)}
    unified_stats = sync_salesmessage_to_unified(settings.database_url, settings.youth_inbox_id)
    risk_stats = recompute_risk_alerts(settings.database_url)
    return {"ok": True, **stats, "unified": unified_stats, "risk": risk_stats}


@mcp.tool()
def sync_daysmart(max_pages: int = 25, page_size: int = 200) -> dict[str, Any]:
    """Sync DaySmart customers + check-ins into the unified youth schema."""
    client = DaysmartClient(
        client_id=settings.daysmart_api_client_id,
        client_secret=settings.daysmart_api_secret,
        base_url=settings.daysmart_base_url,
    )
    try:
        stats = sync_daysmart_to_unified(
            client=client,
            db_path=settings.database_url,
            max_pages=max_pages,
            page_size=page_size,
        )
    except DaysmartApiError as exc:
        return {"ok": False, "error": str(exc)}
    risk_stats = recompute_risk_alerts(settings.database_url)
    return {"ok": True, **stats, "risk": risk_stats}


@mcp.tool()
def recompute_risk(inactivity_days: int = 14, outreach_days: int = 30) -> dict[str, Any]:
    """Recompute retention and operational risk alerts in the unified schema."""
    stats = recompute_risk_alerts(
        db_path=settings.database_url,
        inactivity_days=inactivity_days,
        outreach_days=outreach_days,
    )
    return {"ok": True, **stats}


@mcp.tool()
def list_risk_alerts(status: str = "open", limit: int = 200) -> dict[str, Any]:
    """List risk alerts from unified schema."""
    with get_conn(settings.database_url) as conn:
        rows = conn.execute(
            """
            SELECT alert_id, family_key, child_key, rule_code, severity, status,
                   details_json, last_triggered_at, created_at, updated_at
            FROM youth_risk_alerts
            WHERE status = ?
            ORDER BY severity DESC, updated_at DESC
            LIMIT ?
            """,
            (status, limit),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@mcp.tool()
def list_conversations(limit: int = 100, offset: int = 0) -> dict[str, Any]:
    """List conversations already synced into the local database."""
    with get_conn(settings.database_url) as conn:
        rows = conn.execute(
            """
            SELECT id, contact_name, contact_number, started_at, closed_at, last_message_at
            FROM conversations
            ORDER BY coalesce(last_message_at, '') DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@mcp.tool()
def get_conversation_messages(conversation_id: int, limit: int = 200) -> dict[str, Any]:
    """Get messages for one synced conversation."""
    with get_conn(settings.database_url) as conn:
        rows = conn.execute(
            """
            SELECT id, conversation_id, body, status, message_type, source, created_at
            FROM messages
            WHERE conversation_id = ?
            ORDER BY coalesce(created_at, '') ASC
            LIMIT ?
            """,
            (conversation_id, limit),
        ).fetchall()
    return {"items": [dict(r) for r in rows]}


@mcp.tool()
def search_synced_messages(
    query: str,
    conversation_id: int | None = None,
    limit: int = 25,
) -> dict[str, Any]:
    """Full-text search over synced message bodies."""
    try:
        items = search_messages(
            db_path=settings.database_url,
            query=query,
            limit=limit,
            conversation_id=conversation_id,
        )
    except Exception as exc:
        return {"ok": False, "error": f"Search failed: {exc}", "items": []}
    return {"ok": True, "items": items}


@mcp.tool()
def ask_salesmessage(
    question: str,
    search_query: str | None = None,
    conversation_id: int | None = None,
    max_context_messages: int = 30,
) -> dict[str, Any]:
    """Answer a question from synced data, with local fallback when LLM is unavailable."""
    context = _load_context(
        question=question,
        search_query=search_query,
        conversation_id=conversation_id,
        max_context_messages=max_context_messages,
    )

    if not settings.openai_api_key:
        result = answer_locally(question, context)
        result["context_size"] = len(context)
        return result

    try:
        result = answer_with_llm(
            api_key=settings.openai_api_key,
            model=settings.openai_model,
            question=question,
            context_rows=context,
        )
    except Exception as exc:
        result = answer_locally(question, context)
        result.setdefault("uncertainties", []).append(f"LLM unavailable: {exc}")

    result["context_size"] = len(context)
    return result


if __name__ == "__main__":
    mcp.run()
