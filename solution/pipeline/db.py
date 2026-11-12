"""
Database layer — SQLite via stdlib sqlite3.
All PII fields (customer_name, phone) are stored as HMAC-SHA256 hashes or
one-way encrypted values so plaintext never lands on disk.
Phone is stored both as a hash (for event-linking) and masked (for display).
"""

import hashlib
import hmac
import os
import sqlite3
from pathlib import Path

# Secret used for HMAC-based PII hashing.  In production this would come from
# a secrets manager.  For the assignment we derive it from an env var with a
# safe fallback so tests are deterministic.
_PII_SECRET = os.environ.get("PII_SECRET", "agentic-universe-pii-secret-key").encode()


def hash_pii(value: str) -> str:
    """HMAC-SHA256 of a PII value.  One-way; we can still match if we hash the
    same input (e.g. canonical phone) consistently."""
    return hmac.new(_PII_SECRET, value.encode(), hashlib.sha256).hexdigest()


def mask_phone(canonical: str) -> str:
    """'+91XXXXXXXXXX' → '+91******XXXX'"""
    return canonical[:3] + "******" + canonical[-4:]


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


DDL = """
-- ── Ingestion tracking ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ingested_drops (
    drop_path   TEXT PRIMARY KEY,
    ingested_at TEXT NOT NULL          -- ISO-8601
);

-- ── Lead files ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS ingested_batches (
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    PRIMARY KEY (workflow_id, batch_id)
);

-- ── Leads ────────────────────────────────────────────────────────────────────
-- PII: customer_name stored as HMAC hash; phone stored as HMAC hash.
-- phone_masked kept for display (last 4 digits visible, not reversible to full).
CREATE TABLE IF NOT EXISTS leads (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id         TEXT NOT NULL,
    phone_hash          TEXT NOT NULL,   -- HMAC(canonical_phone)
    phone_masked        TEXT NOT NULL,   -- +91******XXXX
    external_ref        TEXT NOT NULL,   -- policy_number / order_id
    batch_id            TEXT NOT NULL,
    customer_name_hash  TEXT NOT NULL,   -- HMAC(customer_name)

    -- optional fields (not PII)
    policy_expiry       TEXT,
    premium_amount      REAL,
    preferred_language  TEXT,
    product             TEXT,
    order_date          TEXT,

    UNIQUE (workflow_id, phone_hash)
);

-- ── Rejections ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rejections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    UNIQUE (workflow_id, batch_id, row_number)
);

-- ── Data quality counters (single row, upserted) ─────────────────────────────
CREATE TABLE IF NOT EXISTS data_quality (
    id                          INTEGER PRIMARY KEY CHECK (id = 1),
    lead_rows_total             INTEGER NOT NULL DEFAULT 0,
    lead_rows_rejected          INTEGER NOT NULL DEFAULT 0,
    duplicate_lead_rows_merged  INTEGER NOT NULL DEFAULT 0,
    field_warnings              INTEGER NOT NULL DEFAULT 0,
    events_total                INTEGER NOT NULL DEFAULT 0,
    malformed_events            INTEGER NOT NULL DEFAULT 0,
    duplicate_events_dropped    INTEGER NOT NULL DEFAULT 0,
    orphan_events               INTEGER NOT NULL DEFAULT 0,
    unknown_disposition_events  INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO data_quality (id) VALUES (1);

-- ── Rejection reason tallies ──────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rejection_reasons (
    reason  TEXT PRIMARY KEY,
    count   INTEGER NOT NULL DEFAULT 0
);

-- ── Interaction ledger (append-only) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT    NOT NULL UNIQUE,   -- dedup key
    event_type      TEXT    NOT NULL,
    workflow_id     TEXT,
    call_id         TEXT    NOT NULL,
    occurred_at     TEXT    NOT NULL,          -- stored as ISO-8601 UTC
    lead_id         INTEGER REFERENCES leads(id),  -- NULL = orphan
    payload         TEXT    NOT NULL,          -- JSON blob
    is_orphan       INTEGER NOT NULL DEFAULT 0,
    drop_sequence   INTEGER NOT NULL DEFAULT 0 -- order drops were ingested
);

CREATE INDEX IF NOT EXISTS idx_events_call_id    ON events(call_id);
CREATE INDEX IF NOT EXISTS idx_events_lead_id    ON events(lead_id);
CREATE INDEX IF NOT EXISTS idx_events_event_id   ON events(event_id);
CREATE INDEX IF NOT EXISTS idx_leads_wf_phone    ON leads(workflow_id, phone_hash);
"""


def init_db(db_path: str | Path) -> sqlite3.Connection:
    conn = get_connection(db_path)
    conn.executescript(DDL)
    conn.commit()
    return conn

