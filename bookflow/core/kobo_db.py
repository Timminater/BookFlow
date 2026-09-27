"""Additive SQLite schema for the isolated Kobo MVP domain."""

KOB0_DB_SQL = """
CREATE TABLE IF NOT EXISTS user_kobo_settings (
    user_id          INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    enabled          INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    current_revision INTEGER NOT NULL DEFAULT 0 CHECK (current_revision >= 0),
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS kobo_tokens (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_prefix  TEXT NOT NULL UNIQUE,
    token_hash    TEXT NOT NULL UNIQUE,
    device_label  TEXT,
    created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_used_at  TIMESTAMP,
    revoked_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kobo_tokens_user_active
ON kobo_tokens (user_id, created_at DESC)
WHERE revoked_at IS NULL;

CREATE TABLE IF NOT EXISTS kobo_token_rate_limits (
    token_id       INTEGER NOT NULL REFERENCES kobo_tokens(id) ON DELETE CASCADE,
    source_ip      TEXT NOT NULL,
    window_started TIMESTAMP NOT NULL,
    request_count  INTEGER NOT NULL CHECK (request_count > 0),
    blocked_until  TIMESTAMP,
    updated_at     TIMESTAMP NOT NULL,
    PRIMARY KEY (token_id, source_ip)
);

CREATE TABLE IF NOT EXISTS user_kobo_books (
    user_id          INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id          INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    file_id          INTEGER REFERENCES book_files(id) ON DELETE SET NULL,
    desired_state    TEXT NOT NULL CHECK (desired_state IN ('active', 'deleted')),
    preferred_format TEXT,
    source           TEXT NOT NULL,
    request_id       INTEGER,
    revision         INTEGER NOT NULL CHECK (revision > 0),
    updated_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, book_id)
);

CREATE INDEX IF NOT EXISTS idx_user_kobo_books_active
ON user_kobo_books (user_id, desired_state, revision DESC);

CREATE TABLE IF NOT EXISTS user_kobo_changes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    revision     INTEGER NOT NULL CHECK (revision > 0),
    change_type  TEXT NOT NULL CHECK (change_type IN ('upsert', 'delete')),
    book_id      INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    file_id      INTEGER REFERENCES book_files(id) ON DELETE SET NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_user_kobo_changes_sync
ON user_kobo_changes (user_id, revision ASC);

CREATE TRIGGER IF NOT EXISTS prevent_user_kobo_changes_update
BEFORE UPDATE ON user_kobo_changes
BEGIN
    SELECT RAISE(ABORT, 'user_kobo_changes is append-only');
END;

CREATE TRIGGER IF NOT EXISTS prevent_user_kobo_changes_delete
BEFORE DELETE ON user_kobo_changes
BEGIN
    SELECT RAISE(ABORT, 'user_kobo_changes is append-only');
END;
"""

KOBO_AUTH_RATE_LIMIT_SQL = """
CREATE TABLE IF NOT EXISTS kobo_auth_rate_limits (
    token_identity_hash TEXT NOT NULL,
    source_ip           TEXT NOT NULL,
    window_started      TIMESTAMP NOT NULL,
    request_count       INTEGER NOT NULL CHECK (request_count > 0),
    blocked_until       TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL,
    PRIMARY KEY (token_identity_hash, source_ip)
);
"""

KOBO_READING_STATES_SQL = """
CREATE TABLE IF NOT EXISTS kobo_reading_states (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    state_json TEXT NOT NULL,
    PRIMARY KEY (user_id, book_id)
);
"""
