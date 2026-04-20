from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from openai import OpenAI

from .db import get_conn


def _extract_output_text(completion: Any) -> str:
    text = getattr(completion, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text

    output = getattr(completion, "output", None)
    if not isinstance(output, list):
        return ""

    chunks: list[str] = []
    for item in output:
        content = getattr(item, "content", None)
        if not isinstance(content, list):
            continue
        for part in content:
            part_text = getattr(part, "text", None)
            if isinstance(part_text, str) and part_text:
                chunks.append(part_text)
    return "\n".join(chunks).strip()


def search_messages(
    db_path: str,
    query: str,
    limit: int = 25,
    conversation_id: int | None = None,
) -> list[dict[str, Any]]:
    sql = """
    SELECT
      m.id,
      m.conversation_id,
      m.body,
      m.created_at,
      c.contact_name,
      c.contact_number
    FROM messages_fts f
    JOIN messages m ON m.id = f.rowid
    LEFT JOIN conversations c ON c.id = m.conversation_id
    WHERE messages_fts MATCH ?
    """
    params: list[Any] = [query]
    if conversation_id is not None:
        sql += " AND m.conversation_id = ?"
        params.append(conversation_id)

    sql += " ORDER BY bm25(messages_fts) LIMIT ?"
    params.append(limit)

    with get_conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_recent_messages(
    db_path: str,
    limit: int = 50,
    conversation_id: int | None = None,
) -> list[dict[str, Any]]:
    sql = """
    SELECT
      m.id,
      m.conversation_id,
      m.body,
      m.created_at,
      c.contact_name,
      c.contact_number
    FROM messages m
    LEFT JOIN conversations c ON c.id = m.conversation_id
    WHERE coalesce(m.body, '') <> ''
    """
    params: list[Any] = []
    if conversation_id is not None:
        sql += " AND m.conversation_id = ?"
        params.append(conversation_id)

    sql += " ORDER BY coalesce(m.created_at, '') DESC LIMIT ?"
    params.append(limit)

    with get_conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def answer_with_llm(
    *,
    api_key: str,
    model: str,
    question: str,
    context_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    client = OpenAI(api_key=api_key)

    snippets = []
    for row in context_rows:
        snippets.append(
            {
                "message_id": row.get("id"),
                "conversation_id": row.get("conversation_id"),
                "contact_name": row.get("contact_name"),
                "contact_number": row.get("contact_number"),
                "created_at": row.get("created_at"),
                "body": row.get("body"),
            }
        )

    prompt = {
        "question": question,
        "context_messages": snippets,
        "instructions": [
            "Only use the context messages for claims.",
            "If evidence is insufficient, say so clearly.",
            "Return JSON with keys: answer, insights, uncertainties, citations.",
            "Each citation should include message_id and conversation_id.",
        ],
    }

    completion = client.responses.create(
        model=model,
        input=[
            {
                "role": "system",
                "content": "You are a sales conversation analyst. Be concise and evidence-based.",
            },
            {"role": "user", "content": json.dumps(prompt, ensure_ascii=True)},
        ],
    )

    text = _extract_output_text(completion)
    if not text:
        return {
            "answer": "The model returned no text output.",
            "insights": [],
            "uncertainties": ["No answer text in model response."],
            "citations": [],
        }
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    return {
        "answer": text,
        "insights": [],
        "uncertainties": ["Model output was not valid JSON; returning raw answer text."],
        "citations": [],
    }


def answer_locally(question: str, context_rows: list[dict[str, Any]]) -> dict[str, Any]:
    keyword_buckets = {
        "price": ["price", "cost", "expensive", "afford", "budget", "discount", "payment"],
        "schedule": ["time", "schedule", "day", "hours", "when", "available", "class time"],
        "location": ["where", "location", "address", "parking", "distance", "near"],
        "commitment": ["contract", "cancel", "commitment", "month", "term", "locked in"],
        "logistics": ["bring", "wear", "equipment", "what do i need", "arrive"],
    }

    bodies: list[str] = []
    for row in context_rows:
        body = row.get("body")
        if isinstance(body, str) and body.strip():
            bodies.append(body.strip())

    lowered = [b.lower() for b in bodies]
    objection_counts: Counter[str] = Counter()
    cited_by_bucket: dict[str, dict[str, Any]] = {}
    for row, body in zip(context_rows, lowered):
        for bucket, keywords in keyword_buckets.items():
            if any(k in body for k in keywords):
                objection_counts[bucket] += 1
                if bucket not in cited_by_bucket:
                    cited_by_bucket[bucket] = row

    question_like = [b for b in bodies if "?" in b]
    recurring_questions = Counter(
        re.sub(r"\s+", " ", q.strip()).lower() for q in question_like if len(q) > 6
    ).most_common(3)

    top_objections = objection_counts.most_common(3)
    insights: list[str] = []
    citations: list[dict[str, Any]] = []

    if top_objections:
        insights.append(
            "Top objection themes: "
            + ", ".join(f"{name} ({count})" for name, count in top_objections)
            + "."
        )
        for name, _ in top_objections:
            row = cited_by_bucket.get(name)
            if row:
                citations.append(
                    {
                        "message_id": row.get("id"),
                        "conversation_id": row.get("conversation_id"),
                    }
                )

    if recurring_questions:
        insights.append(
            "Most repeated question-style messages: "
            + "; ".join(f"\"{q}\" ({count})" for q, count in recurring_questions)
            + "."
        )

    if not insights:
        insights.append("Not enough repeated patterns found in current context messages.")

    return {
        "answer": (
            f"Local analysis of {len(bodies)} messages for: {question}. "
            + " ".join(insights)
        ),
        "insights": insights,
        "uncertainties": [
            "This answer used local heuristic analysis (no LLM).",
            "Date filtering is not yet applied unless reflected in retrieved context.",
        ],
        "citations": citations,
    }
