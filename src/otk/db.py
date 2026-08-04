"""SQLite schema and connection handling.

Plain stdlib `sqlite3` rather than an ORM: the schema is small and fixed, and
this keeps the dependency surface of a service that faces the internet as
narrow as possible. Every query in the codebase is parameterised.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id            TEXT PRIMARY KEY,
    slug          TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL,
    contact_email TEXT,
    odoo_url      TEXT,
    notes         TEXT NOT NULL DEFAULT '',
    active        INTEGER NOT NULL DEFAULT 1,
    ticket_seq    INTEGER NOT NULL DEFAULT 0,
    -- Minimum ticket priority that alerts the operator, per client. Set by the
    -- operator only: it is never read or written through the client API, so a
    -- client marking everything urgent cannot decide what reaches a phone.
    -- NULL means "use the deployment default".
    notify_priority TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_keys (
    id           TEXT PRIMARY KEY,
    client_id    TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    secret_hash  TEXT NOT NULL,
    scopes       TEXT NOT NULL DEFAULT '[]',
    created_at   TEXT NOT NULL,
    expires_at   TEXT,
    last_used_at TEXT,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_keys_client ON api_keys(client_id);

-- Short-lived, single-use credentials handed to a browser so it can upload a
-- screenshot directly without ever seeing the client's real API key.
CREATE TABLE IF NOT EXISTS upload_tokens (
    id            TEXT PRIMARY KEY,
    client_id     TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    api_key_id    TEXT REFERENCES api_keys(id) ON DELETE SET NULL,
    secret_hash   TEXT NOT NULL,
    reporter_json TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL,
    used_at       TEXT,
    ticket_id     TEXT
);
CREATE INDEX IF NOT EXISTS idx_upload_tokens_expiry ON upload_tokens(expires_at);

CREATE TABLE IF NOT EXISTS reporters (
    id            TEXT PRIMARY KEY,
    client_id     TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    identity_key  TEXT NOT NULL,
    odoo_uid      INTEGER,
    login         TEXT,
    email         TEXT,
    name          TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    UNIQUE(client_id, identity_key)
);

CREATE TABLE IF NOT EXISTS tickets (
    id               TEXT PRIMARY KEY,
    ref              TEXT NOT NULL UNIQUE,
    client_id        TEXT NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    reporter_id      TEXT REFERENCES reporters(id) ON DELETE SET NULL,
    reporter_name    TEXT NOT NULL,
    reporter_email   TEXT,
    title            TEXT NOT NULL,
    description      TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'new',
    priority         TEXT NOT NULL DEFAULT 'normal',
    category         TEXT NOT NULL DEFAULT 'other',
    source           TEXT NOT NULL DEFAULT 'api',
    context_json     TEXT NOT NULL DEFAULT '{}',
    tags_json        TEXT NOT NULL DEFAULT '[]',
    assignee         TEXT,
    external_ref     TEXT,
    idempotency_key  TEXT,
    unread           INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    first_response_at TEXT,
    resolved_at      TEXT,
    closed_at        TEXT,
    -- Set when a retention sweep strips the files but keeps the ticket, so the
    -- UI can say "3 files purged" instead of silently showing nothing.
    purged_at        TEXT,
    purged_files     INTEGER NOT NULL DEFAULT 0,
    purged_bytes     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tickets_client_created ON tickets(client_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status);
-- Ascending (client, updated_at, id) backs the sync feed's keyset scan; the
-- id is part of the key so tickets sharing a timestamp still order totally.
CREATE INDEX IF NOT EXISTS idx_tickets_sync ON tickets(client_id, updated_at, id);
-- Partial unique index: retrying a create with the same Idempotency-Key
-- returns the original ticket instead of duplicating it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_idempotency
    ON tickets(client_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS comments (
    id              TEXT PRIMARY KEY,
    ticket_id       TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    author_type     TEXT NOT NULL,
    author_name     TEXT NOT NULL,
    body            TEXT NOT NULL,
    visibility      TEXT NOT NULL DEFAULT 'public',
    idempotency_key TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_ticket ON comments(ticket_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_comments_idempotency
    ON comments(ticket_id, idempotency_key) WHERE idempotency_key IS NOT NULL;

CREATE TABLE IF NOT EXISTS attachments (
    id           TEXT PRIMARY KEY,
    ticket_id    TEXT NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    comment_id   TEXT REFERENCES comments(id) ON DELETE SET NULL,
    role         TEXT NOT NULL DEFAULT 'attachment',
    filename     TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size_bytes   INTEGER NOT NULL,
    sha256       TEXT NOT NULL,
    width        INTEGER,
    height       INTEGER,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attachments_ticket ON attachments(ticket_id, created_at);
CREATE INDEX IF NOT EXISTS idx_attachments_sha ON attachments(sha256);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id  TEXT,
    client_id  TEXT,
    actor      TEXT NOT NULL,
    kind       TEXT NOT NULL,
    data_json  TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_ticket ON events(ticket_id, created_at);

-- The operator side. Separate from `clients` entirely: these accounts sign in
-- to the web UI and can see every client's data, so they share no code path
-- with API keys.
CREATE TABLE IF NOT EXISTS operators (
    id            TEXT PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    last_login_at TEXT
);

-- Server-side sessions rather than stateless signed cookies, so signing out
-- (or revoking a stolen laptop's session) takes effect immediately.
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    operator_id  TEXT NOT NULL REFERENCES operators(id) ON DELETE CASCADE,
    secret_hash  TEXT NOT NULL,
    csrf_token   TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    user_agent   TEXT,
    revoked_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_operator ON sessions(operator_id);

CREATE TABLE IF NOT EXISTS rate_limits (
    bucket     TEXT PRIMARY KEY,
    tokens     REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=15.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    # WAL lets the TUI read while the API writes without either blocking.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a database created by an older build up to the current schema.

    `CREATE TABLE IF NOT EXISTS` silently leaves existing tables alone, so
    columns added after a table first shipped need an explicit ALTER.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(comments)")}
    if columns and "idempotency_key" not in columns:
        conn.execute("ALTER TABLE comments ADD COLUMN idempotency_key TEXT")

    client_columns = {row["name"] for row in conn.execute("PRAGMA table_info(clients)")}
    if client_columns and "notify_priority" not in client_columns:
        conn.execute("ALTER TABLE clients ADD COLUMN notify_priority TEXT")

    ticket_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tickets)")}
    if ticket_columns:
        for name, ddl in (
            ("purged_at", "TEXT"),
            ("purged_files", "INTEGER NOT NULL DEFAULT 0"),
            ("purged_bytes", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in ticket_columns:
                conn.execute(f"ALTER TABLE tickets ADD COLUMN {name} {ddl}")


def initialize(conn: sqlite3.Connection) -> None:
    _migrate(conn)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO schema_meta(key, value) VALUES('version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(SCHEMA_VERSION),),
    )


def open_database(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    initialize(conn)
    return conn
