from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Any, Iterator


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
    event_name TEXT,
    event_start TEXT,
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

CREATE TABLE IF NOT EXISTS adult_trial_email_notifications (
    notification_key TEXT PRIMARY KEY,
    class_start_at TEXT NOT NULL,
    class_label TEXT NOT NULL,
    recipient_email TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS adult_trial_schedule_events (
    event_id INTEGER PRIMARY KEY,
    starts_at TEXT NOT NULL,
    team_name TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS youth_trial_email_notifications (
    notification_key TEXT PRIMARY KEY,
    class_day TEXT NOT NULL,
    class_label TEXT NOT NULL,
    recipient_email TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    sent_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS youth_trial_schedule_events (
    event_id INTEGER PRIMARY KEY,
    starts_at TEXT NOT NULL,
    team_name TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

@contextmanager
def get_conn(database_url: str) -> Iterator[Any]:
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


def _get_column_names(conn: Any, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"]) for row in rows}


def init_db(database_url: str) -> None:
    with get_conn(database_url) as conn:
        conn.executescript(SQLITE_SCHEMA_SQL)
        columns = _get_column_names(conn, "youth_trial_leads")
        if "inbox_id" not in columns:
            conn.execute("ALTER TABLE youth_trial_leads ADD COLUMN inbox_id INTEGER")
        if "added_to_class" not in columns:
            conn.execute(
                "ALTER TABLE youth_trial_leads ADD COLUMN added_to_class INTEGER NOT NULL DEFAULT 0"
            )
        if "trial_class_name" not in columns:
            conn.execute("ALTER TABLE youth_trial_leads ADD COLUMN trial_class_name TEXT")
        if "trial_class_when" not in columns:
            conn.execute("ALTER TABLE youth_trial_leads ADD COLUMN trial_class_when TEXT")
        registration_columns = _get_column_names(conn, "daysmart_class_registrations")
        if "event_name" not in registration_columns:
            conn.execute("ALTER TABLE daysmart_class_registrations ADD COLUMN event_name TEXT")
        if "event_start" not in registration_columns:
            conn.execute("ALTER TABLE daysmart_class_registrations ADD COLUMN event_start TEXT")
