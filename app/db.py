from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:  # pragma: no cover - optional until Postgres is installed
    psycopg = None
    dict_row = None


SQLITE_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER,
    contact_name TEXT,
    contact_number TEXT,
    owner_id INTEGER,
    inbox_id INTEGER,
    started_at TEXT,
    closed_at TEXT,
    last_message_at TEXT,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL,
    body TEXT,
    status TEXT,
    message_type TEXT,
    source TEXT,
    created_at TEXT,
    sent_at TEXT,
    received_at TEXT,
    user_id INTEGER,
    contact_id INTEGER,
    raw_json TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_last_message_at ON conversations(last_message_at);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    body,
    content='messages',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, body) VALUES (new.id, coalesce(new.body, ''));
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, body) VALUES('delete', old.id, old.body);
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, body) VALUES('delete', old.id, old.body);
    INSERT INTO messages_fts(rowid, body) VALUES (new.id, coalesce(new.body, ''));
END;

CREATE TABLE IF NOT EXISTS youth_families (
    family_key TEXT PRIMARY KEY,
    primary_contact_name TEXT,
    primary_contact_phone TEXT,
    primary_contact_email TEXT,
    family_status TEXT,
    source_system TEXT NOT NULL,
    source_ref TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS youth_children (
    child_key TEXT PRIMARY KEY,
    family_key TEXT NOT NULL,
    child_name TEXT NOT NULL,
    program_track TEXT,
    started_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    source_system TEXT NOT NULL,
    source_ref TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (family_key) REFERENCES youth_families(family_key)
);

CREATE TABLE IF NOT EXISTS youth_attendance_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_key TEXT NOT NULL,
    event_at TEXT NOT NULL,
    attendance_status TEXT NOT NULL,
    source_system TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_system, source_event_id),
    FOREIGN KEY (child_key) REFERENCES youth_children(child_key)
);

CREATE TABLE IF NOT EXISTS youth_coach_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_key TEXT NOT NULL,
    class_at TEXT,
    note_text TEXT NOT NULL,
    tone_label TEXT,
    source_system TEXT NOT NULL,
    source_note_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_system, source_note_id),
    FOREIGN KEY (child_key) REFERENCES youth_children(child_key)
);

CREATE TABLE IF NOT EXISTS youth_family_outreach (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_key TEXT NOT NULL,
    outreach_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    summary_text TEXT,
    sentiment_hint TEXT,
    source_system TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_system, source_message_id),
    FOREIGN KEY (family_key) REFERENCES youth_families(family_key)
);

CREATE TABLE IF NOT EXISTS youth_trial_leads (
    lead_key TEXT PRIMARY KEY,
    family_key TEXT NOT NULL,
    inbox_id INTEGER,
    contact_name TEXT,
    contact_phone TEXT,
    trial_status TEXT NOT NULL,
    account_created INTEGER NOT NULL DEFAULT 0,
    added_to_class INTEGER NOT NULL DEFAULT 0,
    trial_class_name TEXT,
    trial_class_when TEXT,
    last_interaction_at TEXT,
    source_system TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (family_key) REFERENCES youth_families(family_key)
);

CREATE TABLE IF NOT EXISTS youth_risk_alerts (
    alert_id INTEGER PRIMARY KEY AUTOINCREMENT,
    family_key TEXT,
    child_key TEXT,
    scope_key TEXT NOT NULL,
    rule_code TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    details_json TEXT NOT NULL,
    last_triggered_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(rule_code, scope_key)
);

CREATE INDEX IF NOT EXISTS idx_youth_children_family_key ON youth_children(family_key);
CREATE INDEX IF NOT EXISTS idx_youth_attendance_child_event_at ON youth_attendance_events(child_key, event_at);
CREATE INDEX IF NOT EXISTS idx_youth_outreach_family_outreach_at ON youth_family_outreach(family_key, outreach_at);
CREATE INDEX IF NOT EXISTS idx_youth_trial_leads_family_key ON youth_trial_leads(family_key);
CREATE INDEX IF NOT EXISTS idx_youth_risk_status ON youth_risk_alerts(status, severity);

CREATE TABLE IF NOT EXISTS daysmart_customers (
    customer_id INTEGER PRIMARY KEY,
    full_name TEXT,
    email TEXT,
    phone_day TEXT,
    phone_mobile TEXT,
    phone_night TEXT,
    phone_emergency TEXT,
    normalized_name TEXT,
    normalized_email TEXT,
    normalized_phone_day TEXT,
    normalized_phone_mobile TEXT,
    normalized_phone_night TEXT,
    normalized_phone_emergency TEXT,
    child_ids_json TEXT NOT NULL,
    family_ids_json TEXT NOT NULL,
    api_url TEXT,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daysmart_memberships (
    membership_id INTEGER PRIMARY KEY,
    bill_customer_id INTEGER,
    product_id INTEGER,
    product_name TEXT,
    created_at TEXT,
    expires_at TEXT,
    auto_renew INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daysmart_customer_memberships (
    customer_id INTEGER NOT NULL,
    membership_id INTEGER NOT NULL,
    PRIMARY KEY (customer_id, membership_id)
);

CREATE TABLE IF NOT EXISTS daysmart_class_registrations (
    source_type TEXT NOT NULL,
    registration_id INTEGER NOT NULL,
    customer_id INTEGER NOT NULL,
    team_or_event_id INTEGER,
    created_at TEXT,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_type, registration_id)
);

CREATE INDEX IF NOT EXISTS idx_daysmart_customer_phone_day ON daysmart_customers(normalized_phone_day);
CREATE INDEX IF NOT EXISTS idx_daysmart_customer_phone_mobile ON daysmart_customers(normalized_phone_mobile);
CREATE INDEX IF NOT EXISTS idx_daysmart_customer_email ON daysmart_customers(normalized_email);
CREATE INDEX IF NOT EXISTS idx_daysmart_customer_name ON daysmart_customers(normalized_name);
CREATE INDEX IF NOT EXISTS idx_daysmart_memberships_bill_customer ON daysmart_memberships(bill_customer_id);
CREATE INDEX IF NOT EXISTS idx_daysmart_class_registrations_customer ON daysmart_class_registrations(customer_id);
"""

POSTGRES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS conversations (
    id BIGINT PRIMARY KEY,
    contact_id BIGINT,
    contact_name TEXT,
    contact_number TEXT,
    owner_id BIGINT,
    inbox_id BIGINT,
    started_at TEXT,
    closed_at TEXT,
    last_message_at TEXT,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id BIGINT PRIMARY KEY,
    conversation_id BIGINT NOT NULL,
    body TEXT,
    status TEXT,
    message_type TEXT,
    source TEXT,
    created_at TEXT,
    sent_at TEXT,
    received_at TEXT,
    user_id BIGINT,
    contact_id BIGINT,
    raw_json TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_last_message_at ON conversations(last_message_at);
CREATE INDEX IF NOT EXISTS idx_messages_body_search ON messages USING GIN (to_tsvector('english', coalesce(body, '')));

CREATE TABLE IF NOT EXISTS youth_families (
    family_key TEXT PRIMARY KEY,
    primary_contact_name TEXT,
    primary_contact_phone TEXT,
    primary_contact_email TEXT,
    family_status TEXT,
    source_system TEXT NOT NULL,
    source_ref TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS youth_children (
    child_key TEXT PRIMARY KEY,
    family_key TEXT NOT NULL,
    child_name TEXT NOT NULL,
    program_track TEXT,
    started_at TEXT,
    is_active INTEGER NOT NULL DEFAULT 1,
    source_system TEXT NOT NULL,
    source_ref TEXT,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (family_key) REFERENCES youth_families(family_key)
);

CREATE TABLE IF NOT EXISTS youth_attendance_events (
    id BIGSERIAL PRIMARY KEY,
    child_key TEXT NOT NULL,
    event_at TEXT NOT NULL,
    attendance_status TEXT NOT NULL,
    source_system TEXT NOT NULL,
    source_event_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_system, source_event_id),
    FOREIGN KEY (child_key) REFERENCES youth_children(child_key)
);

CREATE TABLE IF NOT EXISTS youth_coach_notes (
    id BIGSERIAL PRIMARY KEY,
    child_key TEXT NOT NULL,
    class_at TEXT,
    note_text TEXT NOT NULL,
    tone_label TEXT,
    source_system TEXT NOT NULL,
    source_note_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_system, source_note_id),
    FOREIGN KEY (child_key) REFERENCES youth_children(child_key)
);

CREATE TABLE IF NOT EXISTS youth_family_outreach (
    id BIGSERIAL PRIMARY KEY,
    family_key TEXT NOT NULL,
    outreach_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    summary_text TEXT,
    sentiment_hint TEXT,
    source_system TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_system, source_message_id),
    FOREIGN KEY (family_key) REFERENCES youth_families(family_key)
);

CREATE TABLE IF NOT EXISTS youth_trial_leads (
    lead_key TEXT PRIMARY KEY,
    family_key TEXT NOT NULL,
    inbox_id BIGINT,
    contact_name TEXT,
    contact_phone TEXT,
    trial_status TEXT NOT NULL,
    account_created INTEGER NOT NULL DEFAULT 0,
    added_to_class INTEGER NOT NULL DEFAULT 0,
    trial_class_name TEXT,
    trial_class_when TEXT,
    last_interaction_at TEXT,
    source_system TEXT NOT NULL,
    source_ref TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (family_key) REFERENCES youth_families(family_key)
);

CREATE TABLE IF NOT EXISTS youth_risk_alerts (
    alert_id BIGSERIAL PRIMARY KEY,
    family_key TEXT,
    child_key TEXT,
    scope_key TEXT NOT NULL,
    rule_code TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    details_json TEXT NOT NULL,
    last_triggered_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(rule_code, scope_key)
);

CREATE INDEX IF NOT EXISTS idx_youth_children_family_key ON youth_children(family_key);
CREATE INDEX IF NOT EXISTS idx_youth_attendance_child_event_at ON youth_attendance_events(child_key, event_at);
CREATE INDEX IF NOT EXISTS idx_youth_outreach_family_outreach_at ON youth_family_outreach(family_key, outreach_at);
CREATE INDEX IF NOT EXISTS idx_youth_trial_leads_family_key ON youth_trial_leads(family_key);
CREATE INDEX IF NOT EXISTS idx_youth_risk_status ON youth_risk_alerts(status, severity);

CREATE TABLE IF NOT EXISTS daysmart_customers (
    customer_id BIGINT PRIMARY KEY,
    full_name TEXT,
    email TEXT,
    phone_day TEXT,
    phone_mobile TEXT,
    phone_night TEXT,
    phone_emergency TEXT,
    normalized_name TEXT,
    normalized_email TEXT,
    normalized_phone_day TEXT,
    normalized_phone_mobile TEXT,
    normalized_phone_night TEXT,
    normalized_phone_emergency TEXT,
    child_ids_json TEXT NOT NULL,
    family_ids_json TEXT NOT NULL,
    api_url TEXT,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daysmart_memberships (
    membership_id BIGINT PRIMARY KEY,
    bill_customer_id BIGINT,
    product_id BIGINT,
    product_name TEXT,
    created_at TEXT,
    expires_at TEXT,
    auto_renew INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS daysmart_customer_memberships (
    customer_id BIGINT NOT NULL,
    membership_id BIGINT NOT NULL,
    PRIMARY KEY (customer_id, membership_id)
);

CREATE TABLE IF NOT EXISTS daysmart_class_registrations (
    source_type TEXT NOT NULL,
    registration_id BIGINT NOT NULL,
    customer_id BIGINT NOT NULL,
    team_or_event_id BIGINT,
    created_at TEXT,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (source_type, registration_id)
);

CREATE INDEX IF NOT EXISTS idx_daysmart_customer_phone_day ON daysmart_customers(normalized_phone_day);
CREATE INDEX IF NOT EXISTS idx_daysmart_customer_phone_mobile ON daysmart_customers(normalized_phone_mobile);
CREATE INDEX IF NOT EXISTS idx_daysmart_customer_email ON daysmart_customers(normalized_email);
CREATE INDEX IF NOT EXISTS idx_daysmart_customer_name ON daysmart_customers(normalized_name);
CREATE INDEX IF NOT EXISTS idx_daysmart_memberships_bill_customer ON daysmart_memberships(bill_customer_id);
CREATE INDEX IF NOT EXISTS idx_daysmart_class_registrations_customer ON daysmart_class_registrations(customer_id);
"""


def is_postgres_url(database_url: str) -> bool:
    return database_url.startswith("postgres://") or database_url.startswith("postgresql://")


class PostgresCompatConnection:
    def __init__(self, conn: Any):
        self._conn = conn

    def _translate_sql(self, sql: str) -> str:
        return sql.replace("?", "%s")

    def execute(self, sql: str, params: Any | None = None) -> Any:
        cur = self._conn.cursor()
        if params is None:
            cur.execute(self._translate_sql(sql))
        else:
            cur.execute(self._translate_sql(sql), params)
        return cur

    def executescript(self, script: str) -> None:
        for statement in _split_sql_statements(script):
            self.execute(statement)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


@contextmanager
def get_conn(database_url: str) -> Iterator[Any]:
    if is_postgres_url(database_url):
        if psycopg is None or dict_row is None:
            raise RuntimeError("psycopg is required for Postgres DATABASE_URL")
        raw_conn = psycopg.connect(database_url, row_factory=dict_row)
        conn: Any = PostgresCompatConnection(raw_conn)
    else:
        raw_conn = sqlite3.connect(database_url)
        raw_conn.row_factory = sqlite3.Row
        conn = raw_conn
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _split_sql_statements(script: str) -> list[str]:
    statements = []
    for part in script.split(";"):
        statement = part.strip()
        if statement:
            statements.append(statement)
    return statements


def _get_column_names(conn: Any, database_url: str, table_name: str) -> set[str]:
    if is_postgres_url(database_url):
        rows = conn.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ?
            """,
            (table_name,),
        ).fetchall()
        return {str(row["column_name"]) for row in rows}
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def init_db(database_url: str) -> None:
    schema_sql = POSTGRES_SCHEMA_SQL if is_postgres_url(database_url) else SQLITE_SCHEMA_SQL
    with get_conn(database_url) as conn:
        if is_postgres_url(database_url):
            conn.executescript(schema_sql)
        else:
            conn.executescript(schema_sql)
        columns = _get_column_names(conn, database_url, "youth_trial_leads")
        if "inbox_id" not in columns:
            conn.execute("ALTER TABLE youth_trial_leads ADD COLUMN inbox_id BIGINT")
        if "added_to_class" not in columns:
            conn.execute(
                "ALTER TABLE youth_trial_leads ADD COLUMN added_to_class INTEGER NOT NULL DEFAULT 0"
            )
        if "trial_class_name" not in columns:
            conn.execute("ALTER TABLE youth_trial_leads ADD COLUMN trial_class_name TEXT")
        if "trial_class_when" not in columns:
            conn.execute("ALTER TABLE youth_trial_leads ADD COLUMN trial_class_when TEXT")
