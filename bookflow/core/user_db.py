"""SQLite user database for multi-user support."""

import base64
import hashlib
import ipaddress
import json
import os
import secrets
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from bookflow.core.activity_view_state_service import user_viewer_scope
from bookflow.core.auth_modes import AUTH_SOURCE_BUILTIN, AUTH_SOURCE_SET
from bookflow.core.bestseller_schema import BESTSELLER60_MIGRATION_SQL
from bookflow.core.book_matcher import normalize_isbn
from bookflow.core.db_migrations import Migration, SQLiteMigrator
from bookflow.core.durable_jobs import BACKGROUND_JOBS_TABLE_SQL
from bookflow.core.kepub_cache_schema import KEPUB_CONVERSION_CACHE_MIGRATION_SQL
from bookflow.core.kobo_db import KOB0_DB_SQL, KOBO_AUTH_RATE_LIMIT_SQL, KOBO_READING_STATES_SQL
from bookflow.core.logger import setup_logger
from bookflow.core.metadata_history_schema import METADATA_FIELD_HISTORY_MIGRATION_SQL
from bookflow.core.platform_features_schema import PLATFORM_FEATURES_MIGRATION_SQL
from bookflow.core.request_validation import (
    normalize_request_status,
    validate_status_transition,
)
from bookflow.core.requests_service import enforce_request_limits

logger = setup_logger(__name__)

_CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    email         TEXT,
    display_name  TEXT,
    password_hash TEXT,
    oidc_subject  TEXT UNIQUE,
    auth_source   TEXT NOT NULL DEFAULT 'builtin',
    role          TEXT NOT NULL DEFAULT 'user',
    is_active     INTEGER NOT NULL DEFAULT 1,
    library_capability TEXT NOT NULL DEFAULT 'download-capable',
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_preferences (
    user_id                  INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    notifications_enabled    INTEGER NOT NULL DEFAULT 0,
    notification_transport   TEXT,
    notification_destination TEXT
);

CREATE TABLE IF NOT EXISTS download_requests (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id        INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    status         TEXT NOT NULL DEFAULT 'pending',
    note           TEXT,
    admin_note     TEXT,
    reviewed_by    INTEGER REFERENCES users(id),
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reviewed_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_download_requests_user_status_created_at
ON download_requests (user_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_download_requests_status_created_at
ON download_requests (status, created_at DESC);

CREATE TABLE IF NOT EXISTS legacy_download_requests_review (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source_table       TEXT NOT NULL,
    source_row_id      TEXT,
    source_schema_json TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    reason             TEXT NOT NULL,
    quarantined_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS download_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    username TEXT,
    request_id INTEGER,
    source TEXT NOT NULL,
    source_display_name TEXT,
    title TEXT NOT NULL,
    author TEXT,
    format TEXT,
    size TEXT,
    preview TEXT,
    content_type TEXT,
    origin TEXT NOT NULL DEFAULT 'direct',
    final_status TEXT NOT NULL,
    status_message TEXT,
    download_path TEXT,
    retry_payload TEXT,
    queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    terminal_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_download_history_user_status
ON download_history (user_id, final_status, terminal_at DESC);

CREATE INDEX IF NOT EXISTS idx_download_history_recent
ON download_history (user_id, terminal_at DESC, id DESC);

-- One row = one file under #13's schema (b); files sharing a task_id belong
-- to the same release. The index is non-unique because a release can span
-- multiple file rows. Fresh databases get it here; upgrades get it via
-- _migrate_download_history_task_id_nonunique().
CREATE INDEX IF NOT EXISTS idx_download_history_task_id
ON download_history (task_id);

-- download_history.book_id and idx_download_history_book_id are added by
-- _migrate_download_history_book_id() so the column exists before the index
-- is created on upgraded pre-library databases. Fresh databases get both via
-- the CREATE TABLE / CREATE INDEX calls inside that migration.

CREATE TABLE IF NOT EXISTS activity_view_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    viewer_scope TEXT NOT NULL,
    item_type TEXT NOT NULL,
    item_key TEXT NOT NULL,
    dismissed_at TIMESTAMP,
    cleared_at TIMESTAMP,
    UNIQUE(viewer_scope, item_type, item_key)
);

CREATE INDEX IF NOT EXISTS idx_activity_view_state_history
ON activity_view_state (viewer_scope, dismissed_at DESC, id DESC)
WHERE dismissed_at IS NOT NULL AND cleared_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_activity_view_state_hidden
ON activity_view_state (viewer_scope, item_type, item_key)
WHERE dismissed_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS books (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    metadata_provider  TEXT    NOT NULL,
    provider_book_id   TEXT    NOT NULL,
    title              TEXT    NOT NULL,
    author             TEXT,
    subtitle           TEXT,
    publish_year       INTEGER,
    isbn_13            TEXT,
    cover_url          TEXT,
    series_name        TEXT,
    series_position    REAL,
    language           TEXT,
    metadata_json      TEXT    NOT NULL DEFAULT '{}',
    created_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (metadata_provider, provider_book_id)
);

CREATE INDEX IF NOT EXISTS idx_books_provider
ON books (metadata_provider, provider_book_id);

CREATE TABLE IF NOT EXISTS user_library (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id    INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    added_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, book_id)
);

CREATE INDEX IF NOT EXISTS idx_user_library_book_id_added_at
ON user_library (book_id, added_at DESC);

CREATE TABLE IF NOT EXISTS user_downloads (
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    history_id  INTEGER NOT NULL REFERENCES download_history(id) ON DELETE CASCADE,
    added_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, history_id)
);

CREATE INDEX IF NOT EXISTS idx_user_downloads_history
ON user_downloads (history_id);
"""

_MIGRATION_001_DEFINITION = (
    _CREATE_TABLES_SQL
    + """
legacy request preservation v1;
legacy request id reservation v1;
auth_source backfill v1;
library_capability backfill v1;
download_history queued_at/retry_payload/book_id/task_id migrations v1;
"""
)
_MIGRATION_001_CHECKSUM = hashlib.sha256(_MIGRATION_001_DEFINITION.encode()).hexdigest()

_MIGRATION_002_DEFINITION = """
CREATE TABLE auth_login_rate_limits (
    username      TEXT NOT NULL,
    source_ip     TEXT NOT NULL,
    failure_count INTEGER NOT NULL CHECK (failure_count > 0),
    locked_until  TIMESTAMP,
    updated_at    TIMESTAMP NOT NULL,
    PRIMARY KEY (username, source_ip)
);

CREATE INDEX idx_auth_login_rate_limits_locked_until
ON auth_login_rate_limits (locked_until)
WHERE locked_until IS NOT NULL;
"""
_MIGRATION_002_CHECKSUM = hashlib.sha256(_MIGRATION_002_DEFINITION.encode()).hexdigest()

_MIGRATION_003_DEFINITION = """
CREATE TABLE book_provider_identifiers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id          INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    provider         TEXT NOT NULL CHECK (length(trim(provider)) > 0),
    provider_book_id TEXT NOT NULL CHECK (length(trim(provider_book_id)) > 0),
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (provider, provider_book_id),
    UNIQUE (book_id, provider, provider_book_id)
);

CREATE INDEX idx_book_provider_identifiers_book_id
ON book_provider_identifiers (book_id);

CREATE TABLE book_identifiers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id         INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    identifier_type TEXT NOT NULL CHECK (length(trim(identifier_type)) > 0),
    identifier      TEXT NOT NULL CHECK (length(trim(identifier)) > 0),
    source          TEXT NOT NULL CHECK (length(trim(source)) > 0),
    confidence      REAL NOT NULL DEFAULT 1.0 CHECK (confidence >= 0.0 AND confidence <= 1.0),
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        identifier_type NOT IN ('isbn10', 'isbn13')
        OR (
            (identifier_type = 'isbn10' AND length(identifier) = 10
             AND identifier NOT GLOB '*[^0-9X]*')
            OR (identifier_type = 'isbn13' AND length(identifier) = 13
                AND identifier NOT GLOB '*[^0-9]*')
        )
    ),
    UNIQUE (book_id, identifier_type, identifier, source)
);

CREATE INDEX idx_book_identifiers_lookup
ON book_identifiers (identifier_type, identifier);

CREATE TABLE book_identifier_conflicts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    identifier_type  TEXT NOT NULL,
    identifier       TEXT NOT NULL,
    existing_book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    proposed_book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    source           TEXT NOT NULL,
    created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (existing_book_id <> proposed_book_id),
    UNIQUE (identifier_type, identifier, existing_book_id, proposed_book_id)
);

CREATE TABLE book_match_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    matched_book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    method          TEXT NOT NULL CHECK (method IN ('isbn13', 'isbn10_to_isbn13',
                                                     'provider_identifier', 'metadata',
                                                     'created')),
    confidence      REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    source          TEXT NOT NULL,
    details_json    TEXT NOT NULL DEFAULT '{}',
    created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_book_match_audit_book_id_created_at
ON book_match_audit (matched_book_id, created_at DESC);

CREATE TABLE book_files (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id              INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    storage_path         TEXT NOT NULL UNIQUE CHECK (length(trim(storage_path)) > 0),
    file_format          TEXT NOT NULL CHECK (length(trim(file_format)) > 0),
    size_bytes           INTEGER NOT NULL CHECK (size_bytes >= 0),
    sha256               TEXT NOT NULL UNIQUE CHECK (
        length(sha256) = 64
        AND sha256 = lower(sha256)
        AND sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    mime_type            TEXT,
    release_source       TEXT,
    import_status        TEXT NOT NULL CHECK (import_status IN ('imported', 'quarantined')),
    import_attempt       INTEGER NOT NULL DEFAULT 1 CHECK (import_attempt > 0),
    derived_from_file_id INTEGER REFERENCES book_files(id) ON DELETE RESTRICT,
    imported_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    created_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (derived_from_file_id IS NULL OR derived_from_file_id <> id)
);

CREATE INDEX idx_book_files_book_id_format
ON book_files (book_id, file_format);

CREATE TABLE user_file_entitlements (
    user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    file_id      INTEGER NOT NULL REFERENCES book_files(id) ON DELETE CASCADE,
    request_id   INTEGER,
    grant_source TEXT NOT NULL DEFAULT 'direct' CHECK (grant_source IN ('direct', 'request', 'migration')),
    granted_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, file_id)
);

CREATE INDEX idx_user_file_entitlements_file_id
ON user_file_entitlements (file_id);

CREATE TABLE legacy_book_files_review (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    history_id     INTEGER NOT NULL UNIQUE,
    book_id        INTEGER REFERENCES books(id) ON DELETE SET NULL,
    payload_json   TEXT NOT NULL,
    reason         TEXT NOT NULL,
    quarantined_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

"""
_MIGRATION_003_CHECKSUM = hashlib.sha256(_MIGRATION_003_DEFINITION.encode()).hexdigest()

_MIGRATION_004_DEFINITION = """
CREATE TABLE acquisitions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id               INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    content_type          TEXT NOT NULL CHECK (content_type IN ('ebook', 'audiobook')),
    profile_hash          TEXT NOT NULL CHECK (length(profile_hash) = 64),
    profile_snapshot_json TEXT NOT NULL,
    status                TEXT NOT NULL CHECK (
        status IN ('queued', 'searching', 'downloading', 'importing', 'retry_scheduled',
                   'waiting_for_release', 'needs_attention', 'completed', 'cancelled', 'failed')
    ),
    monitoring_enabled    INTEGER NOT NULL DEFAULT 1 CHECK (monitoring_enabled IN (0, 1)),
    next_search_at        TIMESTAMP,
    last_search_at        TIMESTAMP,
    active_attempt_id     INTEGER,
    error_code            TEXT,
    error_message         TEXT,
    created_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at            TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at          TIMESTAMP
);

CREATE UNIQUE INDEX idx_acquisitions_one_active_profile
ON acquisitions (book_id, content_type, profile_hash)
WHERE status IN ('queued', 'searching', 'downloading', 'importing', 'retry_scheduled',
                 'waiting_for_release', 'needs_attention');

CREATE INDEX idx_acquisitions_due
ON acquisitions (monitoring_enabled, next_search_at)
WHERE monitoring_enabled = 1
  AND status IN ('queued', 'waiting_for_release', 'retry_scheduled');

CREATE TABLE requests (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    legacy_request_id          INTEGER UNIQUE,
    user_id                    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id                    INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    acquisition_id             INTEGER REFERENCES acquisitions(id) ON DELETE SET NULL,
    content_type               TEXT NOT NULL DEFAULT 'ebook'
                               CHECK (content_type IN ('ebook', 'audiobook')),
    profile_hash               TEXT NOT NULL CHECK (length(profile_hash) = 64),
    profile_snapshot_json      TEXT NOT NULL,
    status                     TEXT NOT NULL CHECK (
        status IN ('pending_approval', 'approved', 'fulfilled', 'rejected', 'cancelled')
    ),
    note                       TEXT,
    admin_note                 TEXT,
    reviewed_by                INTEGER REFERENCES users(id) ON DELETE SET NULL,
    fulfilled_file_id          INTEGER REFERENCES book_files(id) ON DELETE SET NULL,
    add_to_kobo_when_available INTEGER NOT NULL DEFAULT 0
                               CHECK (add_to_kobo_when_available IN (0, 1)),
    created_at                 TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at                 TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    reviewed_at                TIMESTAMP,
    fulfilled_at               TIMESTAMP
);

CREATE UNIQUE INDEX idx_requests_one_active_profile
ON requests (user_id, book_id, profile_hash)
WHERE status IN ('pending_approval', 'approved');

CREATE INDEX idx_requests_user_status_created
ON requests (user_id, status, created_at DESC);

CREATE INDEX idx_requests_acquisition
ON requests (acquisition_id);

CREATE TABLE acquisition_recipients (
    acquisition_id             INTEGER NOT NULL REFERENCES acquisitions(id) ON DELETE CASCADE,
    user_id                    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    request_id                 INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
    add_to_kobo_when_available INTEGER NOT NULL DEFAULT 0
                               CHECK (add_to_kobo_when_available IN (0, 1)),
    created_at                 TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (acquisition_id, user_id, request_id),
    UNIQUE (request_id)
);

CREATE INDEX idx_acquisition_recipients_user
ON acquisition_recipients (user_id, acquisition_id);

INSERT OR IGNORE INTO requests (
    legacy_request_id, user_id, book_id, content_type, profile_hash,
    profile_snapshot_json, status, note, admin_note, reviewed_by,
    created_at, updated_at, reviewed_at, fulfilled_at
)
SELECT
    id, user_id, book_id, 'ebook',
    '44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',
    '{}',
    CASE status
        WHEN 'fulfilled' THEN 'fulfilled'
        WHEN 'rejected' THEN 'rejected'
        WHEN 'cancelled' THEN 'cancelled'
        ELSE 'pending_approval'
    END,
    note, admin_note, reviewed_by, created_at, COALESCE(reviewed_at, created_at),
    reviewed_at, CASE WHEN status = 'fulfilled' THEN reviewed_at ELSE NULL END
FROM download_requests;
"""
_MIGRATION_004_CHECKSUM = hashlib.sha256(_MIGRATION_004_DEFINITION.encode()).hexdigest()

_MIGRATION_005_DEFINITION = """
CREATE TABLE acquisition_search_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    acquisition_id  INTEGER NOT NULL REFERENCES acquisitions(id) ON DELETE CASCADE,
    status          TEXT NOT NULL CHECK (status IN ('running', 'completed', 'partial', 'failed')),
    source_count    INTEGER NOT NULL DEFAULT 0 CHECK (source_count >= 0),
    candidate_count INTEGER NOT NULL DEFAULT 0 CHECK (candidate_count >= 0),
    errors_json     TEXT NOT NULL DEFAULT '{}',
    started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMP
);

CREATE INDEX idx_acquisition_search_runs_acquisition
ON acquisition_search_runs (acquisition_id, started_at DESC);

CREATE TABLE release_candidates (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    acquisition_id      INTEGER NOT NULL REFERENCES acquisitions(id) ON DELETE CASCADE,
    search_run_id       INTEGER NOT NULL REFERENCES acquisition_search_runs(id) ON DELETE CASCADE,
    fingerprint         TEXT NOT NULL,
    source              TEXT NOT NULL,
    indexer             TEXT,
    title               TEXT NOT NULL,
    author              TEXT,
    isbn                TEXT,
    language            TEXT,
    file_format         TEXT,
    protocol            TEXT,
    size_bytes          INTEGER CHECK (size_bytes IS NULL OR size_bytes >= 0),
    seeders             INTEGER CHECK (seeders IS NULL OR seeders >= 0),
    publish_year        INTEGER,
    accepted            INTEGER NOT NULL CHECK (accepted IN (0, 1)),
    total_score         INTEGER NOT NULL,
    score_components_json TEXT NOT NULL,
    reject_reasons_json TEXT NOT NULL,
    normalized_json     TEXT NOT NULL,
    payload_reference   TEXT,
    created_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (acquisition_id, fingerprint)
);

CREATE INDEX idx_release_candidates_rank
ON release_candidates (acquisition_id, accepted, total_score DESC, id);

CREATE TABLE acquisition_attempts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    acquisition_id      INTEGER NOT NULL REFERENCES acquisitions(id) ON DELETE CASCADE,
    candidate_id        INTEGER NOT NULL REFERENCES release_candidates(id) ON DELETE RESTRICT,
    client_type         TEXT,
    external_job_id     TEXT,
    status              TEXT NOT NULL CHECK (
        status IN ('queued', 'submitted', 'downloading', 'importing', 'completed',
                   'retry_scheduled', 'failed', 'cancelled')
    ),
    attempt_number      INTEGER NOT NULL CHECK (attempt_number > 0),
    retry_count         INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
    failure_code        TEXT,
    failure_disposition TEXT,
    error_message       TEXT,
    completed_path      TEXT,
    started_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at          TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at        TIMESTAMP,
    UNIQUE (client_type, external_job_id)
);

CREATE INDEX idx_acquisition_attempts_acquisition
ON acquisition_attempts (acquisition_id, started_at DESC);

CREATE UNIQUE INDEX idx_acquisition_attempts_one_active
ON acquisition_attempts (acquisition_id)
WHERE status IN ('queued', 'submitted', 'downloading', 'importing', 'retry_scheduled');

CREATE TABLE blocked_releases (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint    TEXT NOT NULL,
    scope          TEXT NOT NULL DEFAULT 'global' CHECK (scope IN ('global', 'acquisition')),
    acquisition_id INTEGER REFERENCES acquisitions(id) ON DELETE CASCADE,
    attempt_id     INTEGER REFERENCES acquisition_attempts(id) ON DELETE SET NULL,
    reason         TEXT NOT NULL,
    blocked_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    blocked_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    unblocked_by   INTEGER REFERENCES users(id) ON DELETE SET NULL,
    unblocked_at   TIMESTAMP,
    CHECK (
        (scope = 'global' AND acquisition_id IS NULL)
        OR (scope = 'acquisition' AND acquisition_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX idx_blocked_releases_active
ON blocked_releases (fingerprint, scope, COALESCE(acquisition_id, 0))
WHERE unblocked_at IS NULL;
"""
_MIGRATION_005_CHECKSUM = hashlib.sha256(_MIGRATION_005_DEFINITION.encode()).hexdigest()
_MIGRATION_006_CHECKSUM = hashlib.sha256(BACKGROUND_JOBS_TABLE_SQL.encode()).hexdigest()
_MIGRATION_007_DEFINITION = """
CREATE TABLE api_idempotency_keys (
    actor_user_id  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    method         TEXT NOT NULL,
    route          TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash   TEXT NOT NULL CHECK (length(request_hash) = 64),
    response_status INTEGER NOT NULL,
    response_json  TEXT NOT NULL,
    created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (actor_user_id, method, route, idempotency_key)
);

CREATE INDEX idx_api_idempotency_created
ON api_idempotency_keys (created_at);
"""
_MIGRATION_007_CHECKSUM = hashlib.sha256(_MIGRATION_007_DEFINITION.encode()).hexdigest()
_MIGRATION_008_CHECKSUM = hashlib.sha256(KOB0_DB_SQL.encode()).hexdigest()
_MIGRATION_009_CHECKSUM = hashlib.sha256(KEPUB_CONVERSION_CACHE_MIGRATION_SQL.encode()).hexdigest()
_MIGRATION_010_CHECKSUM = hashlib.sha256(KOBO_AUTH_RATE_LIMIT_SQL.encode()).hexdigest()

# Keep this migration intentionally independent from the pairing/push/metadata
# migrations.  The numbered migration is shared by every installation, so new
# domains must be additive and never alter an earlier migration checksum.
_MIGRATION_011_DEFINITION = """
CREATE TABLE catalog_authors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE catalog_series (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    normalized_name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE book_catalog_authors (
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    author_id INTEGER NOT NULL REFERENCES catalog_authors(id) ON DELETE CASCADE,
    position INTEGER NOT NULL DEFAULT 0 CHECK(position >= 0),
    PRIMARY KEY(book_id, author_id),
    UNIQUE(book_id, position)
);
CREATE TABLE book_catalog_series (
    book_id INTEGER PRIMARY KEY REFERENCES books(id) ON DELETE CASCADE,
    series_id INTEGER NOT NULL REFERENCES catalog_series(id) ON DELETE CASCADE,
    position REAL
);
CREATE INDEX idx_book_catalog_authors_author ON book_catalog_authors(author_id, book_id);
CREATE INDEX idx_book_catalog_series_series ON book_catalog_series(series_id, book_id);
CREATE TRIGGER books_catalog_after_insert AFTER INSERT ON books WHEN trim(COALESCE(new.author, '')) <> '' BEGIN
  INSERT OR IGNORE INTO catalog_authors(name, normalized_name) VALUES(trim(new.author), lower(trim(new.author)));
  INSERT OR REPLACE INTO book_catalog_authors(book_id, author_id, position)
  SELECT new.id, id, 0 FROM catalog_authors WHERE normalized_name = lower(trim(new.author));
END;
CREATE TRIGGER books_catalog_author_after_update AFTER UPDATE OF author ON books BEGIN
  DELETE FROM book_catalog_authors WHERE book_id = new.id;
  INSERT OR IGNORE INTO catalog_authors(name, normalized_name)
  SELECT trim(new.author), lower(trim(new.author)) WHERE trim(COALESCE(new.author, '')) <> '';
  INSERT OR REPLACE INTO book_catalog_authors(book_id, author_id, position)
  SELECT new.id, id, 0 FROM catalog_authors WHERE normalized_name = lower(trim(new.author))
  AND trim(COALESCE(new.author, '')) <> '';
END;
CREATE TRIGGER books_catalog_series_after_insert AFTER INSERT ON books WHEN trim(COALESCE(new.series_name, '')) <> '' BEGIN
  INSERT OR IGNORE INTO catalog_series(name, normalized_name) VALUES(trim(new.series_name), lower(trim(new.series_name)));
  INSERT OR REPLACE INTO book_catalog_series(book_id, series_id, position)
  SELECT new.id, id, new.series_position FROM catalog_series WHERE normalized_name = lower(trim(new.series_name));
END;
CREATE TRIGGER books_catalog_series_after_update AFTER UPDATE OF series_name, series_position ON books BEGIN
  DELETE FROM book_catalog_series WHERE book_id = new.id;
  INSERT OR IGNORE INTO catalog_series(name, normalized_name)
  SELECT trim(new.series_name), lower(trim(new.series_name)) WHERE trim(COALESCE(new.series_name, '')) <> '';
  INSERT OR REPLACE INTO book_catalog_series(book_id, series_id, position)
  SELECT new.id, id, new.series_position FROM catalog_series WHERE normalized_name = lower(trim(new.series_name))
  AND trim(COALESCE(new.series_name, '')) <> '';
END;

CREATE VIRTUAL TABLE books_fts USING fts5(
    book_id UNINDEXED, title, subtitle, author, series_name, isbn_13, description,
    tokenize = 'unicode61 remove_diacritics 2'
);
CREATE TRIGGER books_fts_after_insert AFTER INSERT ON books BEGIN
  INSERT INTO books_fts(rowid, book_id, title, subtitle, author, series_name, isbn_13, description)
  VALUES (new.id, new.id, new.title, new.subtitle, new.author, new.series_name, new.isbn_13,
          COALESCE(json_extract(new.metadata_json, '$.description'), ''));
END;
CREATE TRIGGER books_fts_after_delete AFTER DELETE ON books BEGIN
  DELETE FROM books_fts WHERE rowid = old.id;
END;
CREATE TRIGGER books_fts_after_update AFTER UPDATE OF title, subtitle, author, series_name, isbn_13, metadata_json ON books BEGIN
  DELETE FROM books_fts WHERE rowid = old.id;
  INSERT INTO books_fts(rowid, book_id, title, subtitle, author, series_name, isbn_13, description)
  VALUES (new.id, new.id, new.title, new.subtitle, new.author, new.series_name, new.isbn_13,
          COALESCE(json_extract(new.metadata_json, '$.description'), ''));
END;
INSERT INTO books_fts(rowid, book_id, title, subtitle, author, series_name, isbn_13, description)
SELECT id, id, title, subtitle, author, series_name, isbn_13,
       COALESCE(json_extract(metadata_json, '$.description'), '') FROM books;

CREATE TABLE monitors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    monitor_type TEXT NOT NULL CHECK(monitor_type IN ('author', 'series')),
    provider TEXT NOT NULL,
    provider_id TEXT,
    query TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'notify' CHECK(action IN ('notify','request','auto_acquire')),
    add_to_kobo INTEGER NOT NULL DEFAULT 0 CHECK(add_to_kobo IN (0,1)),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','paused')),
    scan_interval_seconds INTEGER NOT NULL DEFAULT 86400 CHECK(scan_interval_seconds >= 3600),
    next_scan_at TIMESTAMP,
    last_scanned_at TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_monitors_due ON monitors(status, next_scan_at);
CREATE INDEX idx_monitors_user ON monitors(user_id, created_at DESC);
CREATE TABLE monitor_discoveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    monitor_id INTEGER NOT NULL REFERENCES monitors(id) ON DELETE CASCADE,
    provider TEXT NOT NULL,
    provider_book_id TEXT NOT NULL,
    book_id INTEGER REFERENCES books(id) ON DELETE SET NULL,
    state TEXT NOT NULL DEFAULT 'discovered' CHECK(state IN ('discovered','present','requested','ignored','future')),
    payload_json TEXT NOT NULL DEFAULT '{}',
    discovered_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(monitor_id, provider, provider_book_id)
);
CREATE INDEX idx_monitor_discoveries_monitor_state ON monitor_discoveries(monitor_id, state, discovered_at DESC);

CREATE TABLE user_reading_progress (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    progress REAL NOT NULL CHECK(progress >= 0 AND progress <= 100),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    source TEXT NOT NULL CHECK(source IN ('web','kobo')),
    revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id, book_id)
);
CREATE INDEX idx_user_reading_progress_active ON user_reading_progress(user_id, active, updated_at DESC);
CREATE TABLE user_kobo_progress_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK(revision > 0),
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    progress REAL NOT NULL CHECK(progress >= 0 AND progress <= 100),
    active INTEGER NOT NULL CHECK(active IN (0,1)),
    source TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id, revision)
);
CREATE INDEX idx_user_kobo_progress_changes_sync ON user_kobo_progress_changes(user_id, revision ASC);
CREATE TRIGGER prevent_user_kobo_progress_changes_update BEFORE UPDATE ON user_kobo_progress_changes BEGIN
  SELECT RAISE(ABORT, 'user_kobo_progress_changes is append-only');
END;
CREATE TRIGGER prevent_user_kobo_progress_changes_delete BEFORE DELETE ON user_kobo_progress_changes BEGIN
  SELECT RAISE(ABORT, 'user_kobo_progress_changes is append-only');
END;
"""
_MIGRATION_011_CHECKSUM = hashlib.sha256(_MIGRATION_011_DEFINITION.encode()).hexdigest()
_MIGRATION_012_CHECKSUM = hashlib.sha256(PLATFORM_FEATURES_MIGRATION_SQL.encode()).hexdigest()
_MIGRATION_013_CHECKSUM = hashlib.sha256(BESTSELLER60_MIGRATION_SQL.encode()).hexdigest()
_MIGRATION_014_DEFINITION = "user-preferred-interface-language-v1"
_MIGRATION_014_CHECKSUM = hashlib.sha256(_MIGRATION_014_DEFINITION.encode()).hexdigest()
_MIGRATION_015_DEFINITION = """
CREATE TABLE kobo_pairing_rate_limits (
    code_identity_hash TEXT NOT NULL,
    source_ip TEXT NOT NULL,
    window_started TEXT NOT NULL,
    request_count INTEGER NOT NULL CHECK(request_count > 0),
    blocked_until TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(code_identity_hash, source_ip)
);
"""
_MIGRATION_015_CHECKSUM = hashlib.sha256(_MIGRATION_015_DEFINITION.encode()).hexdigest()
_MIGRATION_016_CHECKSUM = hashlib.sha256(METADATA_FIELD_HISTORY_MIGRATION_SQL.encode()).hexdigest()
_MIGRATION_017_DEFINITION = """
ALTER TABLE monitors ADD COLUMN search_profile_json TEXT NOT NULL DEFAULT '{}';
"""
_MIGRATION_017_CHECKSUM = hashlib.sha256(_MIGRATION_017_DEFINITION.encode()).hexdigest()
_MIGRATION_018_DEFINITION = """
CREATE TABLE bestseller_source_metadata_attempts (
    isbn_13 TEXT PRIMARY KEY CHECK (
        length(isbn_13) = 13 AND isbn_13 NOT GLOB '*[^0-9]*'
    ),
    attempted_at TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('claimed', 'found', 'missing', 'error')),
    source_path TEXT NOT NULL,
    last_error TEXT
);
"""
_MIGRATION_018_CHECKSUM = hashlib.sha256(_MIGRATION_018_DEFINITION.encode()).hexdigest()

# Keep content type on the imported artifact itself.  Requests are explicitly
# content-type scoped, so inferring availability only from a Book can otherwise
# let an EPUB fulfil an audiobook request (or vice versa).
_MIGRATION_019_DEFINITION = """
ALTER TABLE book_files ADD COLUMN content_type TEXT NOT NULL DEFAULT 'ebook'
    CHECK (content_type IN ('ebook', 'audiobook'));

-- Existing audio formats are mechanically identifiable.  Unknown historical
-- formats stay ebook deliberately: a conservative classification cannot make
-- an audiobook request appear fulfilled by the wrong artifact.
UPDATE book_files
SET content_type = CASE
    WHEN lower(trim(file_format)) IN (
        'aac', 'flac', 'm4a', 'm4b', 'mp3', 'ogg', 'opus', 'wav', 'wma'
    ) THEN 'audiobook'
    ELSE 'ebook'
END;

DROP INDEX IF EXISTS idx_requests_one_active_profile;
CREATE UNIQUE INDEX idx_requests_one_active_profile
ON requests (user_id, book_id, content_type, profile_hash)
WHERE status IN ('pending_approval', 'approved');
"""
_MIGRATION_019_CHECKSUM = hashlib.sha256(_MIGRATION_019_DEFINITION.encode()).hexdigest()
_MIGRATION_020_DEFINITION = """
-- A Kobo credential identifies an account, not a physical reader. Keep exactly
-- one active credential while retaining revoked rows for audit history.
UPDATE kobo_tokens
SET revoked_at = CURRENT_TIMESTAMP
WHERE revoked_at IS NULL
  AND id NOT IN (
      SELECT MAX(id) FROM kobo_tokens WHERE revoked_at IS NULL GROUP BY user_id
  );

CREATE UNIQUE INDEX idx_kobo_tokens_one_active_per_user
ON kobo_tokens(user_id)
WHERE revoked_at IS NULL;

CREATE TABLE kobo_sync_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_id INTEGER REFERENCES kobo_tokens(id) ON DELETE SET NULL,
    source_ip TEXT NOT NULL,
    user_agent TEXT,
    status TEXT NOT NULL CHECK(status IN ('completed', 'failed')),
    changes_sent INTEGER NOT NULL DEFAULT 0 CHECK(changes_sent >= 0),
    high_water_revision INTEGER,
    has_more INTEGER NOT NULL DEFAULT 0 CHECK(has_more IN (0, 1)),
    duration_ms INTEGER NOT NULL DEFAULT 0 CHECK(duration_ms >= 0),
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_kobo_sync_events_user_recent
ON kobo_sync_events(user_id, created_at DESC, id DESC);
"""
_MIGRATION_020_CHECKSUM = hashlib.sha256(_MIGRATION_020_DEFINITION.encode()).hexdigest()
_MIGRATION_021_DEFINITION = """
CREATE TABLE user_auth_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    username TEXT NOT NULL,
    auth_source TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('login', 'login_failed', 'logout')),
    outcome TEXT NOT NULL CHECK(outcome IN ('success', 'failed', 'blocked')),
    source_ip TEXT NOT NULL,
    user_agent TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_user_auth_events_user_recent
ON user_auth_events(user_id, created_at DESC, id DESC);
CREATE INDEX idx_user_auth_events_username_recent
ON user_auth_events(username, created_at DESC, id DESC);

CREATE TABLE user_web_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    session_key_hash TEXT UNIQUE NOT NULL,
    auth_source TEXT NOT NULL,
    source_ip TEXT NOT NULL,
    user_agent TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE INDEX idx_user_web_sessions_user_recent
ON user_web_sessions(user_id, created_at DESC, id DESC);
"""
_MIGRATION_021_CHECKSUM = hashlib.sha256(_MIGRATION_021_DEFINITION.encode()).hexdigest()
_MIGRATION_022_DEFINITION = """
CREATE TABLE user_file_download_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    book_id INTEGER REFERENCES books(id) ON DELETE SET NULL,
    history_id INTEGER REFERENCES download_history(id) ON DELETE SET NULL,
    source_ip TEXT NOT NULL,
    user_agent TEXT,
    file_name TEXT NOT NULL,
    file_format TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_user_file_download_events_user_recent
ON user_file_download_events(user_id, created_at DESC, id DESC);
"""
_MIGRATION_022_CHECKSUM = hashlib.sha256(_MIGRATION_022_DEFINITION.encode()).hexdigest()
_MIGRATION_023_DEFINITION = """
CREATE TABLE audiobook_retirement_runs (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    started_at TEXT,
    completed_at TEXT,
    backup_path TEXT,
    manifest_path TEXT,
    summary_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT
);
INSERT OR IGNORE INTO audiobook_retirement_runs (id, status) VALUES (1, 'pending');

CREATE TRIGGER reject_audiobook_request_insert
BEFORE INSERT ON requests
WHEN lower(trim(new.content_type)) <> 'ebook'
BEGIN
    SELECT RAISE(ABORT, 'only ebook requests are supported');
END;
CREATE TRIGGER reject_audiobook_request_update
BEFORE UPDATE OF content_type ON requests
WHEN lower(trim(new.content_type)) <> 'ebook'
BEGIN
    SELECT RAISE(ABORT, 'only ebook requests are supported');
END;
CREATE TRIGGER reject_audiobook_acquisition_insert
BEFORE INSERT ON acquisitions
WHEN lower(trim(new.content_type)) <> 'ebook'
BEGIN
    SELECT RAISE(ABORT, 'only ebook acquisitions are supported');
END;
CREATE TRIGGER reject_audiobook_acquisition_update
BEFORE UPDATE OF content_type ON acquisitions
WHEN lower(trim(new.content_type)) <> 'ebook'
BEGIN
    SELECT RAISE(ABORT, 'only ebook acquisitions are supported');
END;
CREATE TRIGGER reject_audiobook_file_insert
BEFORE INSERT ON book_files
WHEN lower(trim(new.content_type)) <> 'ebook'
BEGIN
    SELECT RAISE(ABORT, 'only ebook files are supported');
END;
CREATE TRIGGER reject_audiobook_file_update
BEFORE UPDATE OF content_type ON book_files
WHEN lower(trim(new.content_type)) <> 'ebook'
BEGIN
    SELECT RAISE(ABORT, 'only ebook files are supported');
END;
CREATE TRIGGER reject_audiobook_history_insert
BEFORE INSERT ON download_history
WHEN lower(trim(COALESCE(new.content_type, 'ebook'))) <> 'ebook'
BEGIN
    SELECT RAISE(ABORT, 'only ebook downloads are supported');
END;
CREATE TRIGGER reject_audiobook_history_update
BEFORE UPDATE OF content_type ON download_history
WHEN lower(trim(COALESCE(new.content_type, 'ebook'))) <> 'ebook'
BEGIN
    SELECT RAISE(ABORT, 'only ebook downloads are supported');
END;
"""
_MIGRATION_023_CHECKSUM = hashlib.sha256(_MIGRATION_023_DEFINITION.encode()).hexdigest()

# Named profiles are deliberately separate from request snapshots: editing a
# profile only changes future requests, never a running acquisition.
_MIGRATION_024_DEFINITION = """
CREATE TABLE release_profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK(length(trim(name)) > 0),
    config_json TEXT NOT NULL,
    is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0, 1)),
    is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
    created_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX idx_release_profiles_one_default ON release_profiles(is_default) WHERE is_default = 1;
INSERT INTO release_profiles(name, config_json, is_default, is_active)
VALUES ('Default', '{"allow_archives":false,"allow_bundles":false,"allowed_formats":["epub"],"allowed_indexers":[],"allowed_protocols":["torrent","usenet","direct"],"indexer_priorities":{},"max_size_bytes":null,"min_seeders":0,"minimum_automatic_score":0,"negative_terms":[],"positive_terms":[]}', 1, 1);

ALTER TABLE requests ADD COLUMN profile_id INTEGER REFERENCES release_profiles(id) ON DELETE SET NULL;
ALTER TABLE requests ADD COLUMN profile_name TEXT;
ALTER TABLE acquisitions ADD COLUMN profile_id INTEGER REFERENCES release_profiles(id) ON DELETE SET NULL;
ALTER TABLE acquisitions ADD COLUMN profile_name TEXT;

UPDATE requests SET profile_id = 1, profile_name = 'Default' WHERE profile_id IS NULL;
UPDATE acquisitions SET profile_id = 1, profile_name = 'Default' WHERE profile_id IS NULL;

DROP INDEX IF EXISTS idx_acquisitions_one_active_profile;
CREATE UNIQUE INDEX idx_acquisitions_one_active_book_content
ON acquisitions (book_id, content_type)
WHERE status IN ('queued', 'searching', 'downloading', 'importing', 'retry_scheduled',
                 'waiting_for_release', 'needs_attention');

ALTER TABLE release_candidates ADD COLUMN selected_by INTEGER REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE release_candidates ADD COLUMN selected_at TEXT;
ALTER TABLE release_candidates ADD COLUMN selection_override_json TEXT;
CREATE TABLE candidate_selection_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL REFERENCES release_candidates(id) ON DELETE CASCADE,
    acquisition_id INTEGER NOT NULL REFERENCES acquisitions(id) ON DELETE CASCADE,
    actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    forced INTEGER NOT NULL DEFAULT 0 CHECK(forced IN (0,1)),
    reason TEXT,
    original_accepted INTEGER NOT NULL,
    original_reject_reasons_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""
_MIGRATION_024_CHECKSUM = hashlib.sha256(_MIGRATION_024_DEFINITION.encode()).hexdigest()

# Upgrade compatibility only: v1-v24 used the technical acquisition name.
# v25 renames every live relation/column/value without rebuilding data.
_MIGRATION_025_DEFINITION = """
ALTER TABLE acquisitions RENAME TO request_processes;
ALTER TABLE requests RENAME COLUMN acquisition_id TO request_process_id;
ALTER TABLE acquisition_recipients RENAME TO request_process_recipients;
ALTER TABLE acquisition_search_runs RENAME TO request_search_runs;
ALTER TABLE acquisition_attempts RENAME TO request_attempts;
ALTER TABLE request_process_recipients RENAME COLUMN acquisition_id TO request_process_id;
ALTER TABLE request_search_runs RENAME COLUMN acquisition_id TO request_process_id;
ALTER TABLE request_attempts RENAME COLUMN acquisition_id TO request_process_id;
ALTER TABLE request_processes RENAME COLUMN active_attempt_id TO active_request_attempt_id;
ALTER TABLE release_candidates RENAME COLUMN acquisition_id TO request_process_id;
ALTER TABLE blocked_releases RENAME COLUMN acquisition_id TO request_process_id;
ALTER TABLE blocked_releases RENAME COLUMN attempt_id TO request_attempt_id;
ALTER TABLE candidate_selection_audits RENAME COLUMN acquisition_id TO request_process_id;
DROP TRIGGER IF EXISTS reject_audiobook_acquisition_insert;
DROP TRIGGER IF EXISTS reject_audiobook_acquisition_update;
CREATE TRIGGER reject_audiobook_request_process_insert
BEFORE INSERT ON request_processes
WHEN lower(trim(new.content_type)) <> 'ebook'
BEGIN
    SELECT RAISE(ABORT, 'only ebook request processes are supported');
END;
CREATE TRIGGER reject_audiobook_request_process_update
BEFORE UPDATE OF content_type ON request_processes
WHEN lower(trim(new.content_type)) <> 'ebook'
BEGIN
    SELECT RAISE(ABORT, 'only ebook request processes are supported');
END;
ALTER TABLE blocked_releases RENAME TO legacy_blocked_releases_v25;
CREATE TABLE blocked_releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT 'global' CHECK(scope IN ('global', 'request_process')),
    request_process_id INTEGER REFERENCES request_processes(id) ON DELETE CASCADE,
    request_attempt_id INTEGER REFERENCES request_attempts(id) ON DELETE SET NULL,
    reason TEXT NOT NULL,
    blocked_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    blocked_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    unblocked_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    unblocked_at TIMESTAMP,
    CHECK((scope = 'global' AND request_process_id IS NULL) OR (scope = 'request_process' AND request_process_id IS NOT NULL))
);
INSERT INTO blocked_releases(id,fingerprint,scope,request_process_id,request_attempt_id,reason,blocked_by,blocked_at,unblocked_by,unblocked_at)
SELECT id,fingerprint,
       CASE WHEN scope = 'acquisition' THEN 'request_process' ELSE scope END,
       request_process_id,request_attempt_id,reason,blocked_by,blocked_at,unblocked_by,unblocked_at
FROM legacy_blocked_releases_v25;
DROP TABLE legacy_blocked_releases_v25;

DROP INDEX IF EXISTS idx_acquisitions_one_active_profile;
DROP INDEX IF EXISTS idx_acquisitions_due;
DROP INDEX IF EXISTS idx_requests_acquisition;
DROP INDEX IF EXISTS idx_acquisition_recipients_user;
DROP INDEX IF EXISTS idx_acquisition_search_runs_acquisition;
DROP INDEX IF EXISTS idx_release_candidates_rank;
DROP INDEX IF EXISTS idx_acquisition_attempts_acquisition;
DROP INDEX IF EXISTS idx_acquisition_attempts_one_active;
CREATE UNIQUE INDEX idx_request_processes_one_active_book_content
ON request_processes (book_id, content_type)
WHERE status IN ('queued', 'searching', 'downloading', 'importing', 'retry_scheduled',
                 'waiting_for_release', 'needs_attention');
CREATE INDEX idx_request_processes_due ON request_processes (monitoring_enabled, next_search_at)
WHERE monitoring_enabled = 1 AND status IN ('queued', 'waiting_for_release', 'retry_scheduled');
CREATE INDEX idx_requests_request_process ON requests(request_process_id);
CREATE INDEX idx_request_process_recipients_user ON request_process_recipients(user_id, request_process_id);
CREATE INDEX idx_request_search_runs_process ON request_search_runs(request_process_id, started_at DESC);
CREATE INDEX idx_release_candidates_rank ON release_candidates(request_process_id, accepted, total_score DESC, id);
CREATE INDEX idx_request_attempts_process ON request_attempts(request_process_id, started_at DESC);
CREATE UNIQUE INDEX idx_request_attempts_one_active ON request_attempts(request_process_id)
WHERE status IN ('queued', 'submitted', 'downloading', 'importing', 'retry_scheduled');
CREATE UNIQUE INDEX idx_blocked_releases_active ON blocked_releases(fingerprint, scope, COALESCE(request_process_id, 0)) WHERE unblocked_at IS NULL;
"""
_MIGRATION_025_CHECKSUM = hashlib.sha256(_MIGRATION_025_DEFINITION.encode()).hexdigest()

# Reader locations are tied to the imported file that rendered them.  The
# existing ``user_reading_progress`` row remains book-scoped for Kobo sync and
# the Continue Reading shelf; these additive fields let the web reader restore
# an EPUB CFI or fixed-layout page without inventing a second progress model.
_MIGRATION_026_DEFINITION = """
ALTER TABLE user_reading_progress ADD COLUMN file_id
    INTEGER REFERENCES book_files(id) ON DELETE SET NULL;
ALTER TABLE user_reading_progress ADD COLUMN locator_json TEXT
    CHECK(locator_json IS NULL OR json_valid(locator_json));
ALTER TABLE user_reading_progress ADD COLUMN reader_updated_at TEXT;
CREATE INDEX idx_user_reading_progress_file
ON user_reading_progress(user_id, file_id)
WHERE file_id IS NOT NULL;
"""
_MIGRATION_026_CHECKSUM = hashlib.sha256(_MIGRATION_026_DEFINITION.encode()).hexdigest()

_MIGRATION_027_DEFINITION = """
CREATE TABLE catalog_deletion_operations (
    id TEXT PRIMARY KEY,
    book_id INTEGER NOT NULL,
    plan_token TEXT NOT NULL UNIQUE,
    actor_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK(state IN ('deleting','deleted','cleanup_pending','cleanup_complete','failed')),
    plan_json TEXT NOT NULL CHECK(json_valid(plan_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);
CREATE TABLE catalog_deletion_cleanup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL REFERENCES catalog_deletion_operations(id) ON DELETE CASCADE,
    storage_path TEXT NOT NULL,
    expected_sha256 TEXT NOT NULL,
    expected_size_bytes INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','complete','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(operation_id, storage_path)
);
CREATE TABLE catalog_deletion_audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL REFERENCES catalog_deletion_operations(id) ON DELETE RESTRICT,
    actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TRIGGER prevent_catalog_deletion_audit_update BEFORE UPDATE ON catalog_deletion_audit_events BEGIN SELECT RAISE(ABORT, 'catalog deletion audit is append-only'); END;
CREATE TRIGGER prevent_catalog_deletion_audit_delete BEFORE DELETE ON catalog_deletion_audit_events BEGIN SELECT RAISE(ABORT, 'catalog deletion audit is append-only'); END;
"""
_MIGRATION_027_CHECKSUM = hashlib.sha256(_MIGRATION_027_DEFINITION.encode()).hexdigest()

_MIGRATION_028_DEFINITION = """
CREATE TABLE catalog_storage_leases (
    lease_key TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
"""
_MIGRATION_028_CHECKSUM = hashlib.sha256(_MIGRATION_028_DEFINITION.encode()).hexdigest()

# SQLite cannot alter a foreign-key action in place.  Rebuild the parent table
# under deferred FK enforcement so historical operations remain addressable
# after their administrator account is removed.
_MIGRATION_029_DEFINITION = """
CREATE TABLE catalog_deletion_operations_rebuilt (
    id TEXT PRIMARY KEY,
    book_id INTEGER NOT NULL,
    plan_token TEXT NOT NULL UNIQUE,
    actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    state TEXT NOT NULL CHECK(state IN ('deleting','deleted','cleanup_pending','cleanup_complete','failed')),
    plan_json TEXT NOT NULL CHECK(json_valid(plan_json)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);
INSERT INTO catalog_deletion_operations_rebuilt
SELECT id, book_id, plan_token, actor_user_id, state, plan_json, created_at, completed_at
FROM catalog_deletion_operations;
DROP TABLE catalog_deletion_operations;
ALTER TABLE catalog_deletion_operations_rebuilt RENAME TO catalog_deletion_operations;
DROP TRIGGER prevent_catalog_deletion_audit_update;
CREATE TRIGGER prevent_catalog_deletion_audit_update
BEFORE UPDATE ON catalog_deletion_audit_events
WHEN NOT (
    OLD.actor_user_id IS NOT NULL
    AND NEW.actor_user_id IS NULL
    AND NEW.id = OLD.id
    AND NEW.operation_id = OLD.operation_id
    AND NEW.event_type = OLD.event_type
    AND NEW.payload_json = OLD.payload_json
    AND NEW.created_at = OLD.created_at
)
BEGIN
    SELECT RAISE(ABORT, 'catalog deletion audit is append-only');
END;
"""
_MIGRATION_029_CHECKSUM = hashlib.sha256(_MIGRATION_029_DEFINITION.encode()).hexdigest()

_MIGRATION_030_DEFINITION = """
ALTER TABLE users ADD COLUMN auto_approve_requests INTEGER NOT NULL DEFAULT 0
CHECK (auto_approve_requests IN (0, 1));
ALTER TABLE requests ADD COLUMN approval_source TEXT
CHECK (approval_source IN ('admin', 'user_policy'));
"""
_MIGRATION_030_CHECKSUM = hashlib.sha256(_MIGRATION_030_DEFINITION.encode()).hexdigest()
_MIGRATION_031_CHECKSUM = hashlib.sha256(KOBO_READING_STATES_SQL.encode()).hexdigest()


def _require_loaded_user(user: dict[str, Any] | None) -> dict[str, Any]:
    """Return a loaded user row or raise when the DB insert result is inconsistent."""
    if user is None:
        msg = "Failed to load newly created user"
        raise RuntimeError(msg)
    return user


def _require_recorded_login_failure(
    rate_limit: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the just-written login failure record or raise on an inconsistent insert."""
    if rate_limit is None:
        raise RuntimeError("Failed to load recorded login failure")
    return rate_limit


def _execute_sql_script(conn: sqlite3.Connection, script: str) -> None:
    """Execute a SQL script without sqlite3.executescript's implicit commit."""
    statement = ""
    for line in script.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            if statement.strip():
                conn.execute(statement)
            statement = ""
    if statement.strip():
        raise sqlite3.OperationalError("Incomplete SQL statement in migration")


def _json_safe_sqlite_value(value: Any) -> Any:
    """Encode all SQLite storage classes losslessly into JSON-compatible values."""
    if isinstance(value, bytes):
        return {"type": "blob", "base64": base64.b64encode(value).decode("ascii")}
    return value


def get_users_db_path(config_dir: str | None = None) -> str:
    """Return the configured users database path."""
    root = config_dir or os.environ.get("CONFIG_DIR", "/config")
    return str(Path(root) / "users.db")


def sync_builtin_admin_user(
    username: str,
    password_hash: str,
    db_path: str | None = None,
) -> None:
    """Ensure a local admin user exists for configured builtin credentials."""
    normalized_username = (username or "").strip()
    normalized_hash = password_hash or ""
    if not normalized_username or not normalized_hash:
        return

    user_db = UserDB(db_path or get_users_db_path())
    user_db.initialize()

    existing = user_db.get_user(username=normalized_username)
    if existing:
        existing_auth_source = (
            str(existing.get("auth_source") or AUTH_SOURCE_BUILTIN).strip().lower()
        )
        if existing_auth_source != AUTH_SOURCE_BUILTIN:
            logger.warning(
                "Skipped builtin admin sync for username '%s' because it belongs to auth_source='%s'",
                normalized_username,
                existing_auth_source,
            )
            return
        updates: dict[str, Any] = {}
        if existing.get("password_hash") != normalized_hash:
            updates["password_hash"] = normalized_hash
        if existing.get("role") != "admin":
            updates["role"] = "admin"
        if existing.get("auth_source") != AUTH_SOURCE_BUILTIN:
            updates["auth_source"] = AUTH_SOURCE_BUILTIN
        if updates:
            user_db.update_user(existing["id"], **updates)
            logger.info("Updated local admin user '%s' from builtin settings", normalized_username)
        return

    user_db.create_user(
        username=normalized_username,
        password_hash=normalized_hash,
        auth_source=AUTH_SOURCE_BUILTIN,
        role="admin",
        # Credential migration can run while the settings registry itself is
        # still loading. Avoid recursively loading the full config merely to
        # choose a UI default for this legacy admin.
        preferred_language="en",
    )
    logger.info("Created local admin user '%s' from builtin settings", normalized_username)


class UserDB:
    """Thread-safe SQLite user database."""

    _VALID_AUTH_SOURCES: ClassVar[frozenset[str]] = frozenset(AUTH_SOURCE_SET)
    _VALID_LIBRARY_CAPABILITIES: ClassVar[frozenset[str]] = frozenset(
        {"download-capable", "request-only"}
    )

    def __init__(self, db_path: str) -> None:
        """Initialize the user database wrapper for the given SQLite path."""
        self._db_path = db_path
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def initialize(self) -> None:
        """Bring the database to the latest supported schema version."""
        with self._lock:
            conn = self._connect()
            try:
                migrator = SQLiteMigrator(
                    conn,
                    self._db_path,
                    (
                        Migration(
                            version=1,
                            name="canonical-library-baseline",
                            checksum=_MIGRATION_001_CHECKSUM,
                            apply=self._apply_migration_001,
                        ),
                        Migration(
                            version=2,
                            name="persistent-auth-login-rate-limits",
                            checksum=_MIGRATION_002_CHECKSUM,
                            apply=self._apply_migration_002,
                        ),
                        Migration(
                            version=3,
                            name="canonical-book-files-and-entitlements",
                            checksum=_MIGRATION_003_CHECKSUM,
                            apply=self._apply_migration_003,
                        ),
                        Migration(
                            version=4,
                            name="request-acquisition-core",
                            checksum=_MIGRATION_004_CHECKSUM,
                            apply=self._apply_migration_004,
                        ),
                        Migration(
                            version=5,
                            name="search-candidates-attempts-blocklist",
                            checksum=_MIGRATION_005_CHECKSUM,
                            apply=self._apply_migration_005,
                        ),
                        Migration(
                            version=6,
                            name="durable-background-jobs",
                            checksum=_MIGRATION_006_CHECKSUM,
                            apply=self._apply_migration_006,
                        ),
                        Migration(
                            version=7,
                            name="api-idempotency-keys",
                            checksum=_MIGRATION_007_CHECKSUM,
                            apply=self._apply_migration_007,
                        ),
                        Migration(
                            version=8,
                            name="personal-kobo-sync",
                            checksum=_MIGRATION_008_CHECKSUM,
                            apply=self._apply_migration_008,
                            # v1.2.9 accidentally added reading states to migration 8.
                            # Preserve those fresh installs' journal; migration 31
                            # reconciles both released definitions idempotently.
                            historical_checksums=(
                                "e211e37e7cf67f74aa97266a1d9f1f28ab13c61f79585a0a3971ee9f01b1f9f0",
                            ),
                        ),
                        Migration(
                            version=9,
                            name="kepub-conversion-cache",
                            checksum=_MIGRATION_009_CHECKSUM,
                            apply=self._apply_migration_009,
                        ),
                        Migration(
                            version=10,
                            name="kobo-authentication-rate-limits",
                            checksum=_MIGRATION_010_CHECKSUM,
                            apply=self._apply_migration_010,
                        ),
                        Migration(
                            version=11,
                            name="catalog-monitoring-and-reading-progress",
                            checksum=_MIGRATION_011_CHECKSUM,
                            apply=self._apply_migration_011,
                        ),
                        Migration(
                            version=12,
                            name="pairing-push-metadata-and-backups",
                            checksum=_MIGRATION_012_CHECKSUM,
                            apply=self._apply_migration_012,
                        ),
                        Migration(
                            version=13,
                            name="bestseller60-suggestions",
                            checksum=_MIGRATION_013_CHECKSUM,
                            apply=self._apply_migration_013,
                        ),
                        Migration(
                            version=14,
                            name="user-preferred-interface-language",
                            checksum=_MIGRATION_014_CHECKSUM,
                            apply=self._apply_migration_014,
                        ),
                        Migration(
                            version=15,
                            name="kobo-pairing-rate-limits",
                            checksum=_MIGRATION_015_CHECKSUM,
                            apply=self._apply_migration_015,
                        ),
                        Migration(
                            version=16,
                            name="append-only-metadata-field-provenance",
                            checksum=_MIGRATION_016_CHECKSUM,
                            apply=self._apply_migration_016,
                        ),
                        Migration(
                            version=17,
                            name="monitor-search-profiles",
                            checksum=_MIGRATION_017_CHECKSUM,
                            apply=self._apply_migration_017,
                        ),
                        Migration(
                            version=18,
                            name="bestseller-source-metadata-attempts",
                            checksum=_MIGRATION_018_CHECKSUM,
                            apply=self._apply_migration_018,
                        ),
                        Migration(
                            version=19,
                            name="request-content-type-isolation",
                            checksum=_MIGRATION_019_CHECKSUM,
                            apply=self._apply_migration_019,
                        ),
                        Migration(
                            version=20,
                            name="single-kobo-token-and-sync-audit",
                            checksum=_MIGRATION_020_CHECKSUM,
                            apply=self._apply_migration_020,
                        ),
                        Migration(
                            version=21,
                            name="user-authentication-and-session-audit",
                            checksum=_MIGRATION_021_CHECKSUM,
                            apply=self._apply_migration_021,
                        ),
                        Migration(
                            version=22,
                            name="user-file-download-audit",
                            checksum=_MIGRATION_022_CHECKSUM,
                            apply=self._apply_migration_022,
                        ),
                        Migration(
                            version=23,
                            name="retire-audiobook-content",
                            checksum=_MIGRATION_023_CHECKSUM,
                            apply=self._apply_migration_023,
                        ),
                        Migration(
                            version=24,
                            name="named-release-profiles-and-bookwide-acquisitions",
                            checksum=_MIGRATION_024_CHECKSUM,
                            apply=self._apply_migration_024,
                        ),
                        Migration(
                            version=25,
                            name="rename-acquisition-to-request-process",
                            checksum=_MIGRATION_025_CHECKSUM,
                            apply=self._apply_migration_025,
                        ),
                        Migration(
                            version=26,
                            name="reader-file-locators",
                            checksum=_MIGRATION_026_CHECKSUM,
                            apply=self._apply_migration_026,
                        ),
                        Migration(
                            version=27,
                            name="durable-catalog-deletion",
                            checksum=_MIGRATION_027_CHECKSUM,
                            apply=self._apply_migration_027,
                        ),
                        Migration(
                            version=28,
                            name="catalog-storage-leases",
                            checksum=_MIGRATION_028_CHECKSUM,
                            apply=self._apply_migration_028,
                        ),
                        Migration(
                            version=29,
                            name="catalog-deletion-actor-retention",
                            checksum=_MIGRATION_029_CHECKSUM,
                            apply=self._apply_migration_029,
                        ),
                        Migration(
                            version=30,
                            name="per-user-request-auto-approval",
                            checksum=_MIGRATION_030_CHECKSUM,
                            apply=self._apply_migration_030,
                        ),
                        Migration(
                            version=31,
                            name="kobo-device-reading-states",
                            checksum=_MIGRATION_031_CHECKSUM,
                            apply=self._apply_migration_031,
                        ),
                    ),
                )
                migrator.migrate()
                # WAL mode must be changed outside an open transaction.
                conn.execute("PRAGMA journal_mode=WAL")
            finally:
                conn.close()

    def _apply_migration_001(self, conn: sqlite3.Connection) -> None:
        """Create the canonical baseline while preserving all legacy data."""
        self._preserve_legacy_requests(conn)
        _execute_sql_script(conn, _CREATE_TABLES_SQL)
        self._reserve_legacy_request_ids(conn)
        self._migrate_auth_source_column(conn)
        self._migrate_library_capability_column(conn)
        self._migrate_download_history_queued_at(conn)
        self._migrate_download_history_retry_payload(conn)
        self._migrate_download_history_book_id(conn)
        self._migrate_download_history_task_id_nonunique(conn)

    @staticmethod
    def _apply_migration_002(conn: sqlite3.Connection) -> None:
        """Persist authentication rate-limit state independently of process memory."""
        _execute_sql_script(conn, _MIGRATION_002_DEFINITION)

    @staticmethod
    def _apply_migration_003(conn: sqlite3.Connection) -> None:
        """Add canonical catalog/file relations without changing legacy tables."""
        _execute_sql_script(conn, _MIGRATION_003_DEFINITION)
        UserDB._backfill_catalog_phase_one(conn)

    @staticmethod
    def _apply_migration_004(conn: sqlite3.Connection) -> None:
        """Create durable Request and shared Acquisition lifecycles."""
        _execute_sql_script(conn, _MIGRATION_004_DEFINITION)

    @staticmethod
    def _apply_migration_005(conn: sqlite3.Connection) -> None:
        """Create persisted searches, candidates, attempts, and release blocks."""
        _execute_sql_script(conn, _MIGRATION_005_DEFINITION)

    @staticmethod
    def _apply_migration_006(conn: sqlite3.Connection) -> None:
        """Create the lease-backed durable job queue."""
        _execute_sql_script(conn, BACKGROUND_JOBS_TABLE_SQL)

    @staticmethod
    def _apply_migration_007(conn: sqlite3.Connection) -> None:
        """Persist API idempotency responses across process restarts."""
        _execute_sql_script(conn, _MIGRATION_007_DEFINITION)

    @staticmethod
    def _apply_migration_008(conn: sqlite3.Connection) -> None:
        """Create personal Kobo settings, tokens, shelf, and append-only changes."""
        _execute_sql_script(conn, KOB0_DB_SQL)

    @staticmethod
    def _apply_migration_009(conn: sqlite3.Connection) -> None:
        """Create the shared KEPUB conversion cache index."""
        _execute_sql_script(conn, KEPUB_CONVERSION_CACHE_MIGRATION_SQL)

    @staticmethod
    def _apply_migration_010(conn: sqlite3.Connection) -> None:
        """Rate-limit both valid and unknown Kobo token identities by source IP."""
        _execute_sql_script(conn, KOBO_AUTH_RATE_LIMIT_SQL)

    @staticmethod
    def _apply_migration_011(conn: sqlite3.Connection) -> None:
        """Create global catalog search, monitor state and reading progress."""
        _execute_sql_script(conn, _MIGRATION_011_DEFINITION)
        # Preserve the existing snapshot as canonical browse relations.  Full
        # ordered author/provider reconciliation remains a metadata concern.
        rows = conn.execute("SELECT id, author, series_name, series_position FROM books").fetchall()
        for row in rows:
            if row["author"] and str(row["author"]).strip():
                name = str(row["author"]).strip()
                normalized = " ".join(name.casefold().split())
                conn.execute(
                    "INSERT OR IGNORE INTO catalog_authors(name, normalized_name) VALUES (?, ?)",
                    (name, normalized),
                )
                author_id = conn.execute(
                    "SELECT id FROM catalog_authors WHERE normalized_name = ?", (normalized,)
                ).fetchone()[0]
                conn.execute(
                    "INSERT OR IGNORE INTO book_catalog_authors(book_id, author_id, position) VALUES (?, ?, 0)",
                    (row["id"], author_id),
                )
            if row["series_name"] and str(row["series_name"]).strip():
                name = str(row["series_name"]).strip()
                normalized = " ".join(name.casefold().split())
                conn.execute(
                    "INSERT OR IGNORE INTO catalog_series(name, normalized_name) VALUES (?, ?)",
                    (name, normalized),
                )
                series_id = conn.execute(
                    "SELECT id FROM catalog_series WHERE normalized_name = ?", (normalized,)
                ).fetchone()[0]
                conn.execute(
                    "INSERT OR IGNORE INTO book_catalog_series(book_id, series_id, position) VALUES (?, ?, ?)",
                    (row["id"], series_id, row["series_position"]),
                )

    @staticmethod
    def _apply_migration_012(conn: sqlite3.Connection) -> None:
        """Add pairing, push, metadata governance and backup management."""
        token_columns = {
            str(row["name"]) for row in conn.execute("PRAGMA table_info(kobo_tokens)").fetchall()
        }
        if "device_label" not in token_columns:
            conn.execute("ALTER TABLE kobo_tokens ADD COLUMN device_label TEXT")
        _execute_sql_script(conn, PLATFORM_FEATURES_MIGRATION_SQL)

    @staticmethod
    def _apply_migration_013(conn: sqlite3.Connection) -> None:
        """Add persistent Bestseller 60 suggestions and catalog visibility."""
        _execute_sql_script(conn, BESTSELLER60_MIGRATION_SQL)

    @staticmethod
    def _apply_migration_014(conn: sqlite3.Connection) -> None:
        """Persist the interface language chosen by each user."""
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(user_preferences)").fetchall()
        }
        if "preferred_language" not in columns:
            conn.execute(
                "ALTER TABLE user_preferences ADD COLUMN preferred_language "
                "TEXT NOT NULL DEFAULT 'en' CHECK(preferred_language IN ('en', 'nl'))"
            )

    @staticmethod
    def _apply_migration_015(conn: sqlite3.Connection) -> None:
        """Rate-limit anonymous pairing-code guesses by code identity and IP."""
        _execute_sql_script(conn, _MIGRATION_015_DEFINITION)

    @staticmethod
    def _apply_migration_016(conn: sqlite3.Connection) -> None:
        """Persist immutable field-level metadata provenance events."""
        _execute_sql_script(conn, METADATA_FIELD_HISTORY_MIGRATION_SQL)

    @staticmethod
    def _apply_migration_017(conn: sqlite3.Connection) -> None:
        """Persist provider and acquisition search profiles per monitor."""
        _execute_sql_script(conn, _MIGRATION_017_DEFINITION)

    @staticmethod
    def _apply_migration_018(conn: sqlite3.Connection) -> None:
        """Allow exactly one source-site metadata fallback per trending ISBN."""
        _execute_sql_script(conn, _MIGRATION_018_DEFINITION)

    @staticmethod
    def _apply_migration_019(conn: sqlite3.Connection) -> None:
        """Keep active requests and imported files isolated by content type."""
        _execute_sql_script(conn, _MIGRATION_019_DEFINITION)

    @staticmethod
    def _apply_migration_020(conn: sqlite3.Connection) -> None:
        """Enforce one Kobo credential per account and persist sync audit events."""
        _execute_sql_script(conn, _MIGRATION_020_DEFINITION)

    @staticmethod
    def _apply_migration_021(conn: sqlite3.Connection) -> None:
        """Add privacy-bounded authentication and browser-session audit storage."""
        _execute_sql_script(conn, _MIGRATION_021_DEFINITION)

    @staticmethod
    def _apply_migration_022(conn: sqlite3.Connection) -> None:
        """Persist successful file deliveries for account-sharing detection."""
        _execute_sql_script(conn, _MIGRATION_022_DEFINITION)

    @staticmethod
    def _apply_migration_023(conn: sqlite3.Connection) -> None:
        """Persist one-time retirement state and reject future audiobook writes."""
        _execute_sql_script(conn, _MIGRATION_023_DEFINITION)

    @staticmethod
    def _apply_migration_024(conn: sqlite3.Connection) -> None:
        """Introduce immutable named profile snapshots and one active acquisition per Book."""
        _execute_sql_script(conn, _MIGRATION_024_DEFINITION)
        # Old library memberships without an imported artifact were wish-list
        # rows.  Move them into the request lifecycle before removing the
        # invalid membership; users await approval, admins retain their former
        # direct intent as an approved shared acquisition.
        profile = conn.execute("SELECT * FROM release_profiles WHERE is_default = 1").fetchone()
        if profile is None:
            raise RuntimeError("Default release profile missing")
        profile_json = str(profile["config_json"])
        profile_hash = hashlib.sha256(profile_json.encode()).hexdigest()
        rows = conn.execute(
            """SELECT ul.user_id, ul.book_id, u.role FROM user_library ul JOIN users u ON u.id=ul.user_id
               WHERE NOT EXISTS (SELECT 1 FROM book_files bf WHERE bf.book_id=ul.book_id AND bf.import_status='imported')"""
        ).fetchall()
        for row in rows:
            status = "approved" if row["role"] == "admin" else "pending_approval"
            conn.execute(
                """INSERT OR IGNORE INTO requests (user_id, book_id, content_type, profile_id, profile_name,
                   profile_hash, profile_snapshot_json, status)
                   VALUES (?, ?, 'ebook', ?, ?, ?, ?, ?)""",
                (
                    row["user_id"],
                    row["book_id"],
                    profile["id"],
                    profile["name"],
                    profile_hash,
                    profile_json,
                    status,
                ),
            )
            if status == "approved":
                acquisition = conn.execute(
                    "SELECT id FROM acquisitions WHERE book_id=? AND content_type='ebook' AND status IN ('queued','searching','downloading','importing','retry_scheduled','waiting_for_release','needs_attention')",
                    (row["book_id"],),
                ).fetchone()
                if acquisition is None:
                    cursor = conn.execute(
                        """INSERT INTO acquisitions(book_id, content_type, profile_id, profile_name, profile_hash, profile_snapshot_json,
                           status, monitoring_enabled, next_search_at) VALUES (?, 'ebook', ?, ?, ?, ?, 'queued', 1, CURRENT_TIMESTAMP)""",
                        (
                            row["book_id"],
                            profile["id"],
                            profile["name"],
                            profile_hash,
                            profile_json,
                        ),
                    )
                    acquisition_id = cursor.lastrowid
                else:
                    acquisition_id = acquisition["id"]
                request_row = conn.execute(
                    "SELECT id FROM requests WHERE user_id=? AND book_id=? AND status='approved' ORDER BY id DESC LIMIT 1",
                    (row["user_id"], row["book_id"]),
                ).fetchone()
                if request_row is not None:
                    conn.execute(
                        "UPDATE requests SET acquisition_id=? WHERE id=?",
                        (acquisition_id, request_row["id"]),
                    )
                    conn.execute(
                        "INSERT OR IGNORE INTO acquisition_recipients(acquisition_id,user_id,request_id) VALUES(?,?,?)",
                        (acquisition_id, row["user_id"], request_row["id"]),
                    )
            conn.execute(
                "DELETE FROM user_library WHERE user_id=? AND book_id=?",
                (row["user_id"], row["book_id"]),
            )

    @staticmethod
    def _apply_migration_025(conn: sqlite3.Connection) -> None:
        """Apply the one-way v24 terminology upgrade."""
        _execute_sql_script(conn, _MIGRATION_025_DEFINITION)

    @staticmethod
    def _apply_migration_026(conn: sqlite3.Connection) -> None:
        """Attach web-reader locations to the existing book progress rows."""
        _execute_sql_script(conn, _MIGRATION_026_DEFINITION)

    @staticmethod
    def _apply_migration_027(conn: sqlite3.Connection) -> None:
        _execute_sql_script(conn, _MIGRATION_027_DEFINITION)

    @staticmethod
    def _apply_migration_028(conn: sqlite3.Connection) -> None:
        _execute_sql_script(conn, _MIGRATION_028_DEFINITION)

    @staticmethod
    def _apply_migration_029(conn: sqlite3.Connection) -> None:
        _execute_sql_script(conn, _MIGRATION_029_DEFINITION)

    @staticmethod
    def _apply_migration_030(conn: sqlite3.Connection) -> None:
        """Add the per-user request policy and its approval audit source."""
        _execute_sql_script(conn, _MIGRATION_030_DEFINITION)

    @staticmethod
    def _apply_migration_031(conn: sqlite3.Connection) -> None:
        _execute_sql_script(conn, KOBO_READING_STATES_SQL)

    @staticmethod
    def _backfill_catalog_phase_one(conn: sqlite3.Connection) -> None:
        """Backfill only mechanically certain catalog links; quarantine old files.

        Existing file paths cannot be trusted without configured storage roots,
        so this migration does not hash or import them.  They remain available
        through legacy history while an administrator reviews the explicit
        quarantine report.
        """
        books = conn.execute(
            """
            SELECT id, metadata_provider, provider_book_id, isbn_13
            FROM books
            ORDER BY id ASC
            """
        ).fetchall()
        known_identifiers: dict[tuple[str, str], int] = {}
        for book in books:
            book_id = int(book["id"])
            provider = str(book["metadata_provider"] or "").strip()
            provider_book_id = str(book["provider_book_id"] or "").strip()
            if provider and provider_book_id:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO book_provider_identifiers (
                        book_id, provider, provider_book_id
                    ) VALUES (?, ?, ?)
                    """,
                    (book_id, provider, provider_book_id),
                )

            isbn = normalize_isbn(str(book["isbn_13"] or ""))
            if isbn is None or isbn.isbn13 is None:
                continue
            existing_book_id = known_identifiers.get(("isbn13", isbn.isbn13))
            if existing_book_id is not None and existing_book_id != book_id:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO book_identifier_conflicts (
                        identifier_type, identifier, existing_book_id, proposed_book_id, source
                    ) VALUES ('isbn13', ?, ?, ?, 'legacy_books_backfill')
                    """,
                    (isbn.isbn13, existing_book_id, book_id),
                )
            else:
                known_identifiers[("isbn13", isbn.isbn13)] = book_id
            conn.execute(
                """
                INSERT OR IGNORE INTO book_identifiers (
                    book_id, identifier_type, identifier, source, confidence
                ) VALUES (?, 'isbn13', ?, 'legacy_books_backfill', 1.0)
                """,
                (book_id, isbn.isbn13),
            )

        history_rows = conn.execute(
            """
            SELECT id, book_id, task_id, download_path, format, final_status, terminal_at
            FROM download_history
            WHERE book_id IS NOT NULL AND download_path IS NOT NULL
            ORDER BY id ASC
            """
        ).fetchall()
        for history in history_rows:
            payload = {
                "task_id": history["task_id"],
                "download_path": history["download_path"],
                "format": history["format"],
                "final_status": history["final_status"],
                "terminal_at": history["terminal_at"],
            }
            conn.execute(
                """
                INSERT OR IGNORE INTO legacy_book_files_review (
                    history_id, book_id, payload_json, reason
                ) VALUES (?, ?, ?, 'legacy_path_requires_configured_root_review')
                """,
                (
                    int(history["id"]),
                    int(history["book_id"]),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )

    @staticmethod
    def _normalize_rate_limit_username(username: str) -> str:
        normalized = username.strip().casefold()
        if not normalized:
            raise ValueError("username is required")
        return normalized

    @staticmethod
    def _normalize_rate_limit_source_ip(source_ip: str) -> str:
        normalized = source_ip.strip().lower()
        if not normalized:
            raise ValueError("source_ip is required")
        return normalized

    def get_login_rate_limit(self, *, username: str, source_ip: str) -> dict[str, Any] | None:
        """Return the persisted login-failure state for one normalized identity and IP."""
        normalized_username = self._normalize_rate_limit_username(username)
        normalized_source_ip = self._normalize_rate_limit_source_ip(source_ip)
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT username, source_ip, failure_count, locked_until, updated_at
                FROM auth_login_rate_limits
                WHERE username = ? AND source_ip = ?
                """,
                (normalized_username, normalized_source_ip),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def record_login_failure(
        self,
        *,
        username: str,
        source_ip: str,
        max_failures: int,
        lockout_duration: timedelta,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Atomically record a failed login and apply a lock at the configured threshold."""
        if max_failures < 1:
            raise ValueError("max_failures must be positive")
        if lockout_duration.total_seconds() <= 0:
            raise ValueError("lockout_duration must be positive")

        normalized_username = self._normalize_rate_limit_username(username)
        normalized_source_ip = self._normalize_rate_limit_source_ip(source_ip)
        effective_now = now or datetime.now(UTC)
        if effective_now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        updated_at = effective_now.astimezone(UTC).isoformat(timespec="seconds")
        locked_until = (
            (effective_now + lockout_duration).astimezone(UTC).isoformat(timespec="seconds")
        )

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    INSERT INTO auth_login_rate_limits (
                        username, source_ip, failure_count, locked_until, updated_at
                    )
                    VALUES (?, ?, 1, CASE WHEN ? = 1 THEN ? ELSE NULL END, ?)
                    ON CONFLICT(username, source_ip) DO UPDATE SET
                        failure_count = auth_login_rate_limits.failure_count + 1,
                        locked_until = CASE
                            WHEN auth_login_rate_limits.failure_count + 1 >= ?
                            THEN ?
                            ELSE auth_login_rate_limits.locked_until
                        END,
                        updated_at = excluded.updated_at
                    """,
                    (
                        normalized_username,
                        normalized_source_ip,
                        max_failures,
                        locked_until,
                        updated_at,
                        max_failures,
                        locked_until,
                    ),
                )
                row = conn.execute(
                    """
                    SELECT username, source_ip, failure_count, locked_until, updated_at
                    FROM auth_login_rate_limits
                    WHERE username = ? AND source_ip = ?
                    """,
                    (normalized_username, normalized_source_ip),
                ).fetchone()
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            else:
                return _require_recorded_login_failure(dict(row) if row else None)
            finally:
                conn.close()

    def clear_login_rate_limit(self, *, username: str, source_ip: str) -> bool:
        """Clear one identity/IP rate-limit record after a successful authentication."""
        normalized_username = self._normalize_rate_limit_username(username)
        normalized_source_ip = self._normalize_rate_limit_source_ip(source_ip)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "DELETE FROM auth_login_rate_limits WHERE username = ? AND source_ip = ?",
                    (normalized_username, normalized_source_ip),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            else:
                return cursor.rowcount > 0
            finally:
                conn.close()

    def clear_expired_login_rate_limits(self, *, now: datetime | None = None) -> int:
        """Delete rate-limit records whose lockout period has expired."""
        effective_now = now or datetime.now(UTC)
        if effective_now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        cutoff = effective_now.astimezone(UTC).isoformat(timespec="seconds")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    "DELETE FROM auth_login_rate_limits WHERE locked_until IS NOT NULL AND locked_until <= ?",
                    (cutoff,),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            else:
                return cursor.rowcount
            finally:
                conn.close()

    def _migrate_auth_source_column(self, conn: sqlite3.Connection) -> None:
        """Ensure users.auth_source exists and backfill historical rows."""
        columns = conn.execute("PRAGMA table_info(users)").fetchall()
        column_names = {str(col["name"]) for col in columns}

        if "auth_source" not in column_names:
            conn.execute("ALTER TABLE users ADD COLUMN auth_source TEXT NOT NULL DEFAULT 'builtin'")

        # Backfill OIDC-origin users created before auth_source existed.
        conn.execute("UPDATE users SET auth_source = 'oidc' WHERE oidc_subject IS NOT NULL")
        # Defensive cleanup for any legacy null/blank values.
        conn.execute(
            "UPDATE users SET auth_source = 'builtin' WHERE auth_source IS NULL OR auth_source = ''"
        )

    def _migrate_library_capability_column(self, conn: sqlite3.Connection) -> None:
        """Ensure each user has one explicit Library Capability."""
        columns = conn.execute("PRAGMA table_info(users)").fetchall()
        column_names = {str(col["name"]) for col in columns}
        if "library_capability" not in column_names:
            conn.execute(
                "ALTER TABLE users ADD COLUMN library_capability TEXT "
                "NOT NULL DEFAULT 'download-capable'"
            )
        conn.execute(
            "UPDATE users SET library_capability = 'download-capable' "
            "WHERE library_capability NOT IN ('download-capable', 'request-only') "
            "OR library_capability IS NULL"
        )

    def _preserve_legacy_requests(self, conn: sqlite3.Connection) -> None:
        """Quarantine incompatible request rows before installing the canonical table."""
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'download_requests'"
        ).fetchone()
        if table is None:
            return
        columns = conn.execute("PRAGMA table_info(download_requests)").fetchall()
        expected = {
            "id",
            "user_id",
            "book_id",
            "status",
            "note",
            "admin_note",
            "reviewed_by",
            "created_at",
            "reviewed_at",
        }
        column_by_name = {str(column["name"]): column for column in columns}
        foreign_keys = {
            (str(row["from"]), str(row["table"]), str(row["on_delete"]).upper())
            for row in conn.execute("PRAGMA foreign_key_list(download_requests)").fetchall()
        }
        has_canonical_shape = (
            set(column_by_name) == expected
            and int(column_by_name["id"]["pk"]) == 1
            and int(column_by_name["user_id"]["notnull"]) == 1
            and int(column_by_name["book_id"]["notnull"]) == 1
            and int(column_by_name["status"]["notnull"]) == 1
            and ("user_id", "users", "CASCADE") in foreign_keys
            and ("book_id", "books", "CASCADE") in foreign_keys
        )
        if has_canonical_shape:
            return

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS legacy_download_requests_review (
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                source_table       TEXT NOT NULL,
                source_row_id      TEXT,
                source_schema_json TEXT NOT NULL,
                payload_json       TEXT NOT NULL,
                reason             TEXT NOT NULL,
                quarantined_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        column_names = list(column_by_name)
        schema_json = json.dumps(
            [dict(column) for column in columns],
            sort_keys=True,
            separators=(",", ":"),
        )
        try:
            legacy_rows = conn.execute("SELECT rowid, * FROM download_requests").fetchall()
            rowid_offset = 1
        except sqlite3.OperationalError:
            legacy_rows = conn.execute("SELECT * FROM download_requests").fetchall()
            rowid_offset = 0

        for ordinal, row in enumerate(legacy_rows, start=1):
            payload = {
                column_name: _json_safe_sqlite_value(row[index + rowid_offset])
                for index, column_name in enumerate(column_names)
            }
            source_row_id = payload.get("id")
            if source_row_id is None and rowid_offset:
                source_row_id = row[0]
            if source_row_id is None:
                source_row_id = ordinal
            conn.execute(
                """
                INSERT INTO legacy_download_requests_review (
                    source_table,
                    source_row_id,
                    source_schema_json,
                    payload_json,
                    reason
                )
                VALUES ('download_requests', ?, ?, ?, ?)
                """,
                (
                    str(source_row_id),
                    schema_json,
                    json.dumps(payload, sort_keys=True, separators=(",", ":")),
                    "incompatible_schema_requires_manual_review",
                ),
            )
        logger.warning(
            "Preserved %d incompatible legacy download request(s) for manual review",
            len(legacy_rows),
        )
        conn.execute("DROP TABLE download_requests")

    def _reserve_legacy_request_ids(self, conn: sqlite3.Connection) -> None:
        """Prevent legacy audit references from aliasing newly created requests."""
        legacy_max = conn.execute(
            """
            SELECT MAX(CAST(source_row_id AS INTEGER))
            FROM legacy_download_requests_review
            WHERE source_row_id <> '' AND source_row_id NOT GLOB '*[^0-9]*'
            """
        ).fetchone()[0]
        history_max = conn.execute("SELECT MAX(request_id) FROM download_history").fetchone()[0]
        activity_max = conn.execute(
            """
            SELECT MAX(CAST(SUBSTR(item_key, 9) AS INTEGER))
            FROM activity_view_state
            WHERE item_type = 'request'
              AND item_key GLOB 'request:[0-9]*'
              AND SUBSTR(item_key, 9) NOT GLOB '*[^0-9]*'
            """
        ).fetchone()[0]
        reserved_max = max(
            (int(value) for value in (legacy_max, history_max, activity_max) if value is not None),
            default=0,
        )
        if reserved_max == 0:
            return
        conn.execute("DELETE FROM sqlite_sequence WHERE name = 'download_requests'")
        conn.execute(
            "INSERT INTO sqlite_sequence (name, seq) VALUES ('download_requests', ?)",
            (reserved_max,),
        )

    def _migrate_download_history_queued_at(self, conn: sqlite3.Connection) -> None:
        """Ensure download_history.queued_at exists for queue-time recording."""
        columns = conn.execute("PRAGMA table_info(download_history)").fetchall()
        column_names = {str(col["name"]) for col in columns}
        if "queued_at" not in column_names:
            conn.execute("ALTER TABLE download_history ADD COLUMN queued_at TIMESTAMP")
            conn.execute(
                "UPDATE download_history SET queued_at = CURRENT_TIMESTAMP WHERE queued_at IS NULL"
            )

    def _migrate_download_history_retry_payload(self, conn: sqlite3.Connection) -> None:
        """Ensure download_history.retry_payload exists for restart-safe retries."""
        columns = conn.execute("PRAGMA table_info(download_history)").fetchall()
        column_names = {str(col["name"]) for col in columns}
        if "retry_payload" not in column_names:
            conn.execute("ALTER TABLE download_history ADD COLUMN retry_payload TEXT")

    def _migrate_download_history_book_id(self, conn: sqlite3.Connection) -> None:
        """Ensure download_history.book_id exists to link Files to Books.

        Fresh databases get the column via ``_CREATE_TABLES_SQL``; this migration
        covers upgrades of pre-library ``users.db`` files. The library always
        starts from a fresh database (no legacy ``download_history`` rows to
        backfill), so this only adds the nullable column — book rows are minted
        on Add-to-Library going forward and legacy rows keep ``book_id IS NULL``.
        """
        columns = conn.execute("PRAGMA table_info(download_history)").fetchall()
        column_names = {str(col["name"]) for col in columns}
        if "book_id" not in column_names:
            conn.execute(
                "ALTER TABLE download_history ADD COLUMN book_id "
                "INTEGER REFERENCES books(id) ON DELETE SET NULL"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_download_history_book_id "
            "ON download_history (book_id, final_status)"
        )

    def _migrate_download_history_task_id_nonunique(self, conn: sqlite3.Connection) -> None:
        """Drop the legacy UNIQUE constraint on ``download_history.task_id``.

        Per #13's resolution (schema shape (b)), ``download_history`` is one
        row = one file; the files of a single release share a non-unique
        ``task_id``. SQLite cannot drop a UNIQUE constraint in place, so when
        the legacy constraint is detected the table is rebuilt via the
        copy-to-temp / drop / recreate / copy-back idiom. Legacy rows keep
        their existing values (single-file releases trivially form
        well-formed one-member groups). Fresh databases get the non-unique
        column directly from ``_CREATE_TABLES_SQL`` and skip the rebuild.
        """
        indexes = conn.execute("PRAGMA index_list(download_history)").fetchall()
        has_unique_task_id = any(
            str(idx["name"]) == "sqlite_autoindex_download_history_1" and bool(idx["unique"])
            for idx in indexes
        )
        if not has_unique_task_id:
            # Fresh DB (no auto-index) or already migrated. Ensure the
            # non-unique index exists for both paths.
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_download_history_task_id "
                "ON download_history (task_id)"
            )
            return

        _execute_sql_script(
            conn,
            """
            CREATE TABLE download_history_migration AS
                SELECT
                    id,
                    task_id,
                    user_id,
                    username,
                    request_id,
                    source,
                    source_display_name,
                    title,
                    author,
                    format,
                    size,
                    preview,
                    content_type,
                    origin,
                    final_status,
                    status_message,
                    download_path,
                    retry_payload,
                    queued_at,
                    terminal_at,
                    book_id
                FROM download_history;

            CREATE TEMP TABLE user_downloads_migration AS
                SELECT user_id, history_id, added_at
                FROM user_downloads;

            DROP TABLE download_history;

            CREATE TABLE download_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                username TEXT,
                request_id INTEGER,
                source TEXT NOT NULL,
                source_display_name TEXT,
                title TEXT NOT NULL,
                author TEXT,
                format TEXT,
                size TEXT,
                preview TEXT,
                content_type TEXT,
                origin TEXT NOT NULL DEFAULT 'direct',
                final_status TEXT NOT NULL,
                status_message TEXT,
                download_path TEXT,
                retry_payload TEXT,
                queued_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                terminal_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                book_id INTEGER REFERENCES books(id) ON DELETE SET NULL
            );

            INSERT INTO download_history (
                id,
                task_id,
                user_id,
                username,
                request_id,
                source,
                source_display_name,
                title,
                author,
                format,
                size,
                preview,
                content_type,
                origin,
                final_status,
                status_message,
                download_path,
                retry_payload,
                queued_at,
                terminal_at,
                book_id
            )
            SELECT
                id,
                task_id,
                user_id,
                username,
                request_id,
                source,
                source_display_name,
                title,
                author,
                format,
                size,
                preview,
                content_type,
                origin,
                final_status,
                status_message,
                download_path,
                retry_payload,
                queued_at,
                terminal_at,
                book_id
            FROM download_history_migration;

            INSERT INTO user_downloads (user_id, history_id, added_at)
                SELECT user_id, history_id, added_at
                FROM user_downloads_migration;

            DROP TABLE download_history_migration;
            DROP TABLE user_downloads_migration;

            CREATE INDEX idx_download_history_user_status
                ON download_history (user_id, final_status, terminal_at DESC);
            CREATE INDEX idx_download_history_recent
                ON download_history (user_id, terminal_at DESC, id DESC);
            CREATE INDEX idx_download_history_task_id
                ON download_history (task_id);
            CREATE INDEX idx_download_history_book_id
                ON download_history (book_id, final_status);
            """,
        )

    def create_user(
        self,
        username: str,
        email: str | None = None,
        display_name: str | None = None,
        password_hash: str | None = None,
        oidc_subject: str | None = None,
        auth_source: str = "builtin",
        role: str = "user",
        library_capability: str = "download-capable",
        auto_approve_requests: bool = False,
        preferred_language: str | None = None,
    ) -> dict[str, Any]:
        """Create a new user. Raises ValueError if username or oidc_subject already exists."""
        if auth_source not in self._VALID_AUTH_SOURCES:
            msg = f"Invalid auth_source: {auth_source}"
            raise ValueError(msg)
        if library_capability not in self._VALID_LIBRARY_CAPABILITIES:
            msg = f"Invalid library_capability: {library_capability}"
            raise ValueError(msg)
        if not isinstance(auto_approve_requests, bool):
            msg = "auto_approve_requests must be a boolean"
            raise TypeError(msg)
        if preferred_language is None:
            from bookflow.core.config import config

            configured_language = config.get("DEFAULT_UI_LANGUAGE", "en")
            preferred_language = (
                configured_language
                if isinstance(configured_language, str) and configured_language in {"en", "nl"}
                else "en"
            )
        if preferred_language not in {"en", "nl"}:
            msg = f"Invalid preferred_language: {preferred_language}"
            raise ValueError(msg)
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """INSERT INTO users (
                           username, email, display_name, password_hash, oidc_subject, auth_source, role,
                           library_capability, auto_approve_requests
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        username,
                        email,
                        display_name,
                        password_hash,
                        oidc_subject,
                        auth_source,
                        role,
                        library_capability,
                        int(auto_approve_requests),
                    ),
                )
                user_id = cursor.lastrowid
                if not isinstance(user_id, int):
                    msg = "Failed to create user"
                    raise TypeError(msg)
                conn.execute(
                    "INSERT INTO user_preferences (user_id, preferred_language) VALUES (?, ?)",
                    (user_id, preferred_language),
                )
                conn.commit()
                created_user = self._get_user_by_id(conn, user_id)
                return _require_loaded_user(created_user)
            except sqlite3.IntegrityError as e:
                msg = f"User already exists: {e}"
                raise ValueError(msg) from e
            finally:
                conn.close()

    def get_user(
        self,
        user_id: int | None = None,
        username: str | None = None,
        oidc_subject: str | None = None,
    ) -> dict[str, Any] | None:
        """Get a user by id, username, or oidc_subject. Returns None if not found."""
        conn = self._connect()
        try:
            if user_id is not None:
                return self._get_user_by_id(conn, user_id)
            if username is not None:
                row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
            elif oidc_subject is not None:
                row = conn.execute(
                    "SELECT * FROM users WHERE oidc_subject = ?", (oidc_subject,)
                ).fetchone()
            else:
                return None
            return dict(row) if row else None
        finally:
            conn.close()

    def _get_user_by_id(self, conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None

    _ALLOWED_UPDATE_COLUMNS: ClassVar[frozenset[str]] = frozenset(
        {
            "username",
            "email",
            "display_name",
            "password_hash",
            "oidc_subject",
            "auth_source",
            "role",
            "is_active",
            "library_capability",
            "auto_approve_requests",
        }
    )
    _USER_UPDATE_STATEMENTS: ClassVar[dict[str, str]] = {
        "username": "UPDATE users SET username = ? WHERE id = ?",
        "email": "UPDATE users SET email = ? WHERE id = ?",
        "display_name": "UPDATE users SET display_name = ? WHERE id = ?",
        "password_hash": "UPDATE users SET password_hash = ? WHERE id = ?",
        "oidc_subject": "UPDATE users SET oidc_subject = ? WHERE id = ?",
        "auth_source": "UPDATE users SET auth_source = ? WHERE id = ?",
        "role": "UPDATE users SET role = ? WHERE id = ?",
        "is_active": "UPDATE users SET is_active = ? WHERE id = ?",
        "library_capability": "UPDATE users SET library_capability = ? WHERE id = ?",
        "auto_approve_requests": "UPDATE users SET auto_approve_requests = ? WHERE id = ?",
    }

    def update_user(self, user_id: int, **kwargs: object) -> None:
        """Update user fields. Raises ValueError if user not found or invalid column."""
        if not kwargs:
            return
        for k in kwargs:
            if k not in self._ALLOWED_UPDATE_COLUMNS:
                msg = f"Invalid column: {k}"
                raise ValueError(msg)
        if "auth_source" in kwargs and kwargs["auth_source"] not in self._VALID_AUTH_SOURCES:
            msg = f"Invalid auth_source: {kwargs['auth_source']}"
            raise ValueError(msg)
        if (
            "library_capability" in kwargs
            and kwargs["library_capability"] not in self._VALID_LIBRARY_CAPABILITIES
        ):
            msg = f"Invalid library_capability: {kwargs['library_capability']}"
            raise ValueError(msg)
        if "auto_approve_requests" in kwargs and not isinstance(
            kwargs["auto_approve_requests"], bool
        ):
            msg = "auto_approve_requests must be a boolean"
            raise ValueError(msg)
        with self._lock:
            conn = self._connect()
            try:
                # Verify user exists
                if not self._get_user_by_id(conn, user_id):
                    msg = f"User {user_id} not found"
                    raise ValueError(msg)
                for column, value in kwargs.items():
                    conn.execute(self._USER_UPDATE_STATEMENTS[column], (value, user_id))
                conn.commit()
            finally:
                conn.close()

    def delete_user(self, user_id: int) -> None:
        """Delete a user and their settings."""
        with self._lock:
            conn = self._connect()
            try:
                request_rows = conn.execute(
                    "SELECT id FROM download_requests WHERE user_id = ?",
                    (user_id,),
                ).fetchall()
                request_item_keys = [f"request:{row['id']}" for row in request_rows]
                if request_item_keys:
                    conn.executemany(
                        "DELETE FROM activity_view_state WHERE item_type = 'request' AND item_key = ?",
                        [(item_key,) for item_key in request_item_keys],
                    )
                conn.execute(
                    "DELETE FROM activity_view_state WHERE viewer_scope = ?",
                    (user_viewer_scope(user_id),),
                )
                conn.execute(
                    "UPDATE download_requests SET reviewed_by = NULL WHERE reviewed_by = ?",
                    (user_id,),
                )
                conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
                conn.commit()
            finally:
                conn.close()

    def list_users(self) -> list[dict[str, Any]]:
        """List all users."""
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def record_auth_event(
        self,
        *,
        username: str,
        auth_source: str,
        event_type: str,
        outcome: str,
        source_ip: str,
        user_agent: str | None,
        user_id: int | None = None,
    ) -> None:
        """Record a bounded authentication event without credentials or cookie values."""
        if event_type not in {"login", "login_failed", "logout"}:
            raise ValueError("Invalid authentication event type")
        if outcome not in {"success", "failed", "blocked"}:
            raise ValueError("Invalid authentication outcome")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO user_auth_events (
                        user_id, username, auth_source, event_type, outcome,
                        source_ip, user_agent
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        str(username or "unknown")[:255],
                        str(auth_source or "unknown")[:32],
                        event_type,
                        outcome,
                        str(source_ip or "unknown")[:64],
                        str(user_agent or "")[:512] or None,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def create_web_session(
        self,
        *,
        user_id: int,
        auth_source: str,
        source_ip: str,
        user_agent: str | None,
        expires_at: datetime,
    ) -> str:
        """Create an independently revocable browser session and return its opaque key."""
        session_key = secrets.token_urlsafe(32)
        session_key_hash = hashlib.sha256(session_key.encode()).hexdigest()
        normalized_expiry = expires_at.astimezone(UTC).isoformat()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO user_web_sessions (
                        user_id, session_key_hash, auth_source, source_ip,
                        user_agent, expires_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        session_key_hash,
                        str(auth_source or "unknown")[:32],
                        str(source_ip or "unknown")[:64],
                        str(user_agent or "")[:512] or None,
                        normalized_expiry,
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return session_key

    def validate_web_session(self, *, user_id: int, session_key: str) -> bool:
        """Validate and occasionally touch one browser session."""
        session_key_hash = hashlib.sha256(session_key.encode()).hexdigest()
        now = datetime.now(UTC)
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT id, last_seen_at, expires_at, revoked_at
                    FROM user_web_sessions
                    WHERE user_id = ? AND session_key_hash = ?
                    """,
                    (user_id, session_key_hash),
                ).fetchone()
                if row is None or row["revoked_at"] is not None:
                    return False
                expires_at = datetime.fromisoformat(str(row["expires_at"]))
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=UTC)
                if expires_at <= now:
                    return False
                last_seen_at = datetime.fromisoformat(str(row["last_seen_at"]))
                if last_seen_at.tzinfo is None:
                    last_seen_at = last_seen_at.replace(tzinfo=UTC)
                if now - last_seen_at >= timedelta(minutes=5):
                    conn.execute(
                        "UPDATE user_web_sessions SET last_seen_at = ? WHERE id = ?",
                        (now.isoformat(), row["id"]),
                    )
                    conn.commit()
                return True
            finally:
                conn.close()

    def revoke_web_session(
        self,
        *,
        user_id: int,
        session_key: str | None = None,
        session_id: int | None = None,
    ) -> bool:
        """Revoke a browser session by opaque key or admin-visible row id."""
        if (session_key is None) == (session_id is None):
            raise ValueError("Provide exactly one session identifier")
        session_key_hash = (
            hashlib.sha256(session_key.encode()).hexdigest() if session_key is not None else None
        )
        with self._lock:
            conn = self._connect()
            try:
                if session_key_hash is not None:
                    cursor = conn.execute(
                        """
                        UPDATE user_web_sessions
                        SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP)
                        WHERE user_id = ? AND session_key_hash = ? AND revoked_at IS NULL
                        """,
                        (user_id, session_key_hash),
                    )
                else:
                    cursor = conn.execute(
                        """
                        UPDATE user_web_sessions
                        SET revoked_at = COALESCE(revoked_at, CURRENT_TIMESTAMP)
                        WHERE user_id = ? AND id = ? AND revoked_at IS NULL
                        """,
                        (user_id, int(session_id or 0)),
                    )
                conn.commit()
                return cursor.rowcount > 0
            finally:
                conn.close()

    def record_kobo_sync(
        self,
        *,
        user_id: int,
        token_id: int | None,
        source_ip: str,
        user_agent: str | None,
        status: str,
        changes_sent: int = 0,
        high_water_revision: int | None = None,
        has_more: bool = False,
        duration_ms: int = 0,
        error_code: str | None = None,
    ) -> None:
        """Persist one authenticated Kobo library-sync request without credentials."""
        if status not in {"completed", "failed"}:
            raise ValueError("Invalid Kobo sync status")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO kobo_sync_events (
                        user_id, token_id, source_ip, user_agent, status,
                        changes_sent, high_water_revision, has_more,
                        duration_ms, error_code
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        token_id,
                        str(source_ip or "unknown")[:64],
                        str(user_agent or "")[:512] or None,
                        status,
                        max(0, int(changes_sent)),
                        high_water_revision,
                        int(has_more),
                        max(0, int(duration_ms)),
                        str(error_code or "")[:64] or None,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def record_file_download(
        self,
        *,
        user_id: int,
        book_id: int,
        history_id: int | None,
        source_ip: str,
        user_agent: str | None,
        file_name: str,
        file_format: str | None,
    ) -> None:
        """Persist one successful authenticated file-delivery request."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO user_file_download_events (
                        user_id, book_id, history_id, source_ip, user_agent,
                        file_name, file_format
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        user_id,
                        book_id,
                        history_id,
                        str(source_ip or "unknown")[:64],
                        str(user_agent or "")[:512] or None,
                        str(file_name or "download")[:512],
                        str(file_format or "")[:32] or None,
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _network_key(value: str) -> str:
        """Normalize addresses for switching detection; collapse IPv6 privacy addresses."""
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            return value.strip().casefold() or "unknown"
        if address.version == 6:
            return str(ipaddress.ip_network(f"{address}/64", strict=False))
        return str(address)

    @classmethod
    def _ip_switch_risk(cls, rows: list[sqlite3.Row]) -> dict[str, Any]:
        """Calculate the explainable three-switch/48-hour account-sharing signal."""
        transition_streak = 0
        max_transition_streak = 0
        previous_network: str | None = None
        last_transition_at: datetime | None = None
        last_event_at: datetime | None = None
        for row in rows:
            created_at = datetime.fromisoformat(str(row["created_at"]))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=UTC)
            last_event_at = created_at
            network = cls._network_key(str(row["source_ip"]))
            if previous_network is None:
                previous_network = network
                continue
            if network == previous_network:
                continue
            if last_transition_at is not None and created_at - last_transition_at <= timedelta(
                hours=48
            ):
                transition_streak += 1
            else:
                transition_streak = 1
            max_transition_streak = max(max_transition_streak, transition_streak)
            last_transition_at = created_at
            previous_network = network

        now = datetime.now(UTC)
        return {
            "ip_switch_streak": transition_streak,
            "max_ip_switch_streak": max_transition_streak,
            "suspicious": bool(
                transition_streak >= 3
                and last_transition_at is not None
                and now - last_transition_at <= timedelta(hours=48)
            ),
            "last_event_at": last_event_at.isoformat() if last_event_at else None,
        }

    def get_admin_user_audit(self, user_id: int, *, limit: int = 50) -> dict[str, Any]:
        """Return bounded account audit data with explainable network risk signals."""
        normalized_limit = min(max(int(limit), 1), 200)
        conn = self._connect()
        try:
            user = conn.execute(
                "SELECT id, username FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if user is None:
                raise ValueError("User not found")
            token = conn.execute(
                """
                SELECT id, token_prefix, device_label, created_at, last_used_at, revoked_at
                FROM kobo_tokens WHERE user_id = ?
                ORDER BY (revoked_at IS NULL) DESC, created_at DESC, id DESC LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            recent = conn.execute(
                """
                SELECT id, token_id, source_ip, user_agent, status, changes_sent,
                       high_water_revision, has_more, duration_ms, error_code, created_at
                FROM kobo_sync_events
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (user_id, normalized_limit),
            ).fetchall()
            chronological = conn.execute(
                """
                SELECT source_ip, created_at FROM kobo_sync_events
                WHERE user_id = ? AND status = 'completed'
                  AND created_at >= datetime('now', '-14 days')
                ORDER BY created_at ASC, id ASC
                """,
                (user_id,),
            ).fetchall()
            file_downloads = conn.execute(
                """
                SELECT id, book_id, history_id, source_ip, user_agent,
                       file_name, file_format, created_at
                FROM user_file_download_events
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (user_id, normalized_limit),
            ).fetchall()
            chronological_downloads = conn.execute(
                """
                SELECT source_ip, created_at FROM user_file_download_events
                WHERE user_id = ?
                  AND created_at >= datetime('now', '-14 days')
                ORDER BY created_at ASC, id ASC
                """,
                (user_id,),
            ).fetchall()
            auth_events = conn.execute(
                """
                SELECT id, auth_source, event_type, outcome, source_ip,
                       user_agent, created_at
                FROM user_auth_events
                WHERE user_id = ? OR (user_id IS NULL AND username = ?)
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (user_id, user["username"], normalized_limit),
            ).fetchall()
            web_sessions = conn.execute(
                """
                SELECT id, auth_source, source_ip, user_agent, created_at,
                       last_seen_at, expires_at, revoked_at
                FROM user_web_sessions
                WHERE user_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ?
                """,
                (user_id, normalized_limit),
            ).fetchall()
            failed_login_count = conn.execute(
                """
                SELECT COUNT(*) FROM user_auth_events
                WHERE (user_id = ? OR (user_id IS NULL AND username = ?))
                  AND event_type = 'login_failed'
                  AND created_at >= datetime('now', '-48 hours')
                """,
                (user_id, user["username"]),
            ).fetchone()[0]
            history = conn.execute(
                """
                SELECT id, title, author, format, content_type, origin,
                       final_status, source_display_name, terminal_at
                FROM download_history
                WHERE user_id = ?
                ORDER BY terminal_at DESC, id DESC LIMIT ?
                """,
                (user_id, normalized_limit),
            ).fetchall()
        finally:
            conn.close()

        now = datetime.now(UTC)
        kobo_risk = self._ip_switch_risk(chronological)
        download_risk = self._ip_switch_risk(chronological_downloads)
        serialized_sessions = []
        for row in web_sessions:
            item = dict(row)
            expiry = datetime.fromisoformat(str(row["expires_at"]))
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            item["is_active"] = row["revoked_at"] is None and expiry > now
            serialized_sessions.append(item)

        ip_summary: dict[str, dict[str, Any]] = {}
        for source, rows in (
            ("login", auth_events),
            ("download", file_downloads),
            ("kobo", recent),
        ):
            for row in rows:
                address = str(row["source_ip"])
                entry = ip_summary.setdefault(
                    address,
                    {
                        "source_ip": address,
                        "last_seen_at": str(row["created_at"]),
                        "event_count": 0,
                        "sources": [],
                    },
                )
                entry["event_count"] += 1
                if source not in entry["sources"]:
                    entry["sources"].append(source)
                if str(row["created_at"]) > entry["last_seen_at"]:
                    entry["last_seen_at"] = str(row["created_at"])

        return {
            "authentication": {
                "recent_events": [dict(row) for row in auth_events],
                "sessions": serialized_sessions,
                "failed_login_count_48h": int(failed_login_count),
            },
            "history": [dict(row) for row in history],
            "downloads": {
                "recent_downloads": [dict(row) for row in file_downloads],
                "download_count": len(chronological_downloads),
                "ip_switch_streak": download_risk["ip_switch_streak"],
                "max_ip_switch_streak": download_risk["max_ip_switch_streak"],
                "suspicious": download_risk["suspicious"],
                "last_download_at": download_risk["last_event_at"],
            },
            "ip_addresses": sorted(
                ip_summary.values(),
                key=lambda item: item["last_seen_at"],
                reverse=True,
            ),
            "kobo": {
                "token": dict(token) if token is not None else None,
                "recent_syncs": [dict(row) for row in recent],
                "sync_count": len(chronological),
                "ip_switch_streak": kobo_risk["ip_switch_streak"],
                "max_ip_switch_streak": kobo_risk["max_ip_switch_streak"],
                "suspicious": kobo_risk["suspicious"],
                "last_sync_at": kobo_risk["last_event_at"],
            },
        }

    def has_admin_with_password(self) -> bool:
        """Return True when at least one admin user with a password hash exists."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM users WHERE role = 'admin'"
                " AND password_hash IS NOT NULL AND password_hash != ''"
                " LIMIT 1",
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def get_personal_preferences(self, user_id: int) -> dict[str, Any]:
        """Return the explicit personal preferences for one user."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT notifications_enabled, notification_transport, notification_destination, "
                "preferred_language "
                "FROM user_preferences WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            if row:
                return dict(row)
            return {
                "notifications_enabled": False,
                "notification_transport": None,
                "notification_destination": None,
                "preferred_language": "en",
            }
        finally:
            conn.close()

    def update_personal_preferences(self, user_id: int, **preferences: object) -> None:
        """Update only the supported personal preference fields."""
        allowed = {
            "notifications_enabled",
            "notification_transport",
            "notification_destination",
            "preferred_language",
        }
        if not set(preferences).issubset(allowed):
            msg = "Invalid personal preference"
            raise ValueError(msg)
        if not preferences:
            return
        if "preferred_language" in preferences and preferences["preferred_language"] not in {
            "en",
            "nl",
        }:
            msg = "Invalid preferred language"
            raise ValueError(msg)
        with self._lock:
            conn = self._connect()
            try:
                if not self._get_user_by_id(conn, user_id):
                    msg = f"User {user_id} not found"
                    raise ValueError(msg)
                existing = self.get_personal_preferences(user_id)
                existing.update(preferences)
                conn.execute(
                    """INSERT INTO user_preferences (
                        user_id, notifications_enabled, notification_transport,
                        notification_destination, preferred_language
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        notifications_enabled = excluded.notifications_enabled,
                        notification_transport = excluded.notification_transport,
                        notification_destination = excluded.notification_destination,
                        preferred_language = excluded.preferred_language""",
                    (
                        user_id,
                        bool(existing["notifications_enabled"]),
                        existing["notification_transport"],
                        existing["notification_destination"],
                        existing["preferred_language"],
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def create_library_request(
        self, *, user_id: int, book_id: int, note: str | None = None
    ) -> dict[str, Any]:
        """Create one pending request for a member's Book without title matching.

        Membership, global File availability, and duplicate detection share the
        insert transaction so a Request cannot race a completed File into
        existence.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                book = conn.execute(
                    "SELECT id, title, author FROM books WHERE id = ?", (book_id,)
                ).fetchone()
                if book is None:
                    raise ValueError("Book not found")
                member = conn.execute(
                    "SELECT 1 FROM user_library WHERE user_id = ? AND book_id = ?",
                    (user_id, book_id),
                ).fetchone()
                if member is None:
                    raise ValueError("Book is not in the user's library")
                has_files = conn.execute(
                    """
                    SELECT 1 FROM download_history
                    WHERE book_id = ? AND final_status = 'complete' AND download_path IS NOT NULL
                    LIMIT 1
                    """,
                    (book_id,),
                ).fetchone()
                if has_files is not None:
                    raise ValueError("Book already has completed Files")
                duplicate = conn.execute(
                    """
                    SELECT 1 FROM download_requests
                    WHERE user_id = ? AND book_id = ? AND status = 'pending'
                    LIMIT 1
                    """,
                    (user_id, book_id),
                ).fetchone()
                if duplicate is not None:
                    raise ValueError("Duplicate pending Request exists for this Book")
                enforce_request_limits(conn, user_id=user_id)
                cursor = conn.execute(
                    "INSERT INTO download_requests (user_id, book_id, note) VALUES (?, ?, ?)",
                    (user_id, book_id, note),
                )
                created = dict(
                    conn.execute(
                        "SELECT * FROM download_requests WHERE id = ?", (cursor.lastrowid,)
                    ).fetchone()
                )
                conn.commit()
                return created
            finally:
                conn.close()

    def fulfil_pending_book_requests(
        self, *, book_id: int, reviewed_by: int | None = None
    ) -> list[dict[str, Any]]:
        """Fulfil and link every pending Request for a Book with its Files."""
        with self._lock:
            conn = self._connect()
            try:
                pending = conn.execute(
                    "SELECT * FROM download_requests WHERE book_id = ? AND status = 'pending'",
                    (book_id,),
                ).fetchall()
                file_rows = conn.execute(
                    """
                    SELECT id FROM download_history
                    WHERE book_id = ? AND final_status = 'complete' AND download_path IS NOT NULL
                    """,
                    (book_id,),
                ).fetchall()
                if not pending or not file_rows:
                    return []
                now = datetime.now(UTC).isoformat(timespec="seconds")
                conn.executemany(
                    "INSERT OR IGNORE INTO user_downloads (user_id, history_id, added_at) VALUES (?, ?, ?)",
                    [
                        (int(request["user_id"]), int(file_row["id"]), now)
                        for request in pending
                        for file_row in file_rows
                    ],
                )
                request_ids = [int(request["id"]) for request in pending]
                conn.execute(
                    """
                    UPDATE download_requests
                    SET status = 'fulfilled', reviewed_by = COALESCE(?, reviewed_by),
                        reviewed_at = ?
                    WHERE book_id = ? AND status = 'pending'
                    """,
                    (reviewed_by, now, book_id),
                )
                rows = conn.execute(
                    "SELECT * FROM download_requests WHERE id IN (SELECT value FROM json_each(?))",
                    (json.dumps(request_ids),),
                ).fetchall()
                conn.commit()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def list_pending_book_requests(self, book_id: int) -> list[dict[str, Any]]:
        """Return pending Requests identified by one canonical Book."""
        conn = self._connect()
        try:
            rows = conn.execute(
                """
                SELECT * FROM download_requests
                WHERE book_id = ? AND status = 'pending'
                ORDER BY created_at ASC, id ASC
                """,
                (book_id,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def get_request(self, request_id: int) -> dict[str, Any] | None:
        """Get a request row by ID."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM download_requests WHERE id = ?",
                (request_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_book_notification_context(self, book_id: int) -> dict[str, Any] | None:
        """Return the small canonical Book projection used by notifications."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT id, title, author, cover_url FROM books WHERE id = ?", (book_id,)
            ).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def get_request_notification_context(
        self, *, request_id: int | None = None, book_id: int | None = None
    ) -> dict[str, Any] | None:
        """Return the Book and requester projection needed by admin notifications.

        New request-process downloads carry one canonical ``requests.id`` in
        their task snapshot.  The legacy admin fulfilment route does not carry
        a request id, so its pending requesters are resolved by Book instead.
        """
        conn = self._connect()
        try:
            row = None
            requester_rows: list[Any] = []
            if request_id is not None:
                row = conn.execute(
                    """
                    SELECT requests.book_id, books.id, books.title, books.author, books.cover_url,
                           requests.request_process_id, requests.user_id
                    FROM requests
                    JOIN books ON books.id = requests.book_id
                    WHERE requests.id = ?
                    """,
                    (request_id,),
                ).fetchone()
                if row is not None and row["request_process_id"] is not None:
                    requester_rows = conn.execute(
                        """
                        SELECT DISTINCT users.username
                        FROM request_process_recipients
                        JOIN users ON users.id = request_process_recipients.user_id
                        WHERE request_process_recipients.request_process_id = ?
                        ORDER BY request_process_recipients.created_at,
                                 request_process_recipients.request_id
                        """,
                        (row["request_process_id"],),
                    ).fetchall()
                if row is not None and not requester_rows:
                    requester_rows = conn.execute(
                        "SELECT username FROM users WHERE id = ?", (row["user_id"],)
                    ).fetchall()

                if row is None:
                    row = conn.execute(
                        """
                        SELECT download_requests.book_id, books.id, books.title,
                               books.author, books.cover_url, download_requests.user_id
                        FROM download_requests
                        JOIN books ON books.id = download_requests.book_id
                        WHERE download_requests.id = ?
                        """,
                        (request_id,),
                    ).fetchone()
                    if row is not None:
                        requester_rows = conn.execute(
                            "SELECT username FROM users WHERE id = ?", (row["user_id"],)
                        ).fetchall()

            if row is None and book_id is not None:
                row = conn.execute(
                    "SELECT id, title, author, cover_url FROM books WHERE id = ?",
                    (book_id,),
                ).fetchone()
                if row is not None:
                    requester_rows = conn.execute(
                        """
                        SELECT DISTINCT users.username
                        FROM requests
                        JOIN users ON users.id = requests.user_id
                        WHERE requests.book_id = ?
                          AND requests.status IN ('pending_approval', 'approved')
                        ORDER BY requests.created_at, requests.id
                        """,
                        (book_id,),
                    ).fetchall()
                    legacy_rows = conn.execute(
                        """
                        SELECT DISTINCT users.username
                        FROM download_requests
                        JOIN users ON users.id = download_requests.user_id
                        WHERE download_requests.book_id = ?
                          AND download_requests.status = 'pending'
                        ORDER BY download_requests.created_at, download_requests.id
                        """,
                        (book_id,),
                    ).fetchall()
                    requester_rows.extend(legacy_rows)

            if row is None:
                return None

            requesters: list[str] = []
            for requester_row in requester_rows:
                username = str(requester_row["username"] or "").strip()
                if username and username not in requesters:
                    requesters.append(username)

            return {
                "book_id": int(row["id"]),
                "title": row["title"],
                "author": row["author"],
                "cover_url": row["cover_url"],
                "requesters": requesters,
            }
        finally:
            conn.close()

    def list_requests(
        self,
        *,
        user_id: int | None = None,
        status: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        include_book_details: bool = False,
    ) -> list[dict[str, Any]]:
        """List requests with optional user/status filters."""
        where_clauses: list[str] = []
        params: list[Any] = []

        if user_id is not None:
            where_clauses.append("user_id = ?")
            params.append(user_id)

        if status is not None:
            where_clauses.append("status = ?")
            params.append(normalize_request_status(status))

        query = "SELECT * FROM download_requests"
        if include_book_details:
            query = """
                SELECT download_requests.*, books.title AS book_title,
                       books.author AS book_author, books.cover_url AS book_cover_url
                FROM download_requests
                LEFT JOIN books ON books.id = download_requests.book_id
            """
        if where_clauses:
            query += " WHERE " + " AND ".join(where_clauses)
        query += " ORDER BY created_at DESC, id DESC"

        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
            if offset:
                query += " OFFSET ?"
                params.append(offset)
        elif offset:
            query += " LIMIT -1 OFFSET ?"
            params.append(offset)

        conn = self._connect()
        try:
            rows = conn.execute(query, params).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    _ALLOWED_REQUEST_UPDATE_COLUMNS: ClassVar[frozenset[str]] = frozenset(
        {
            "status",
            "note",
            "admin_note",
            "reviewed_by",
            "reviewed_at",
        }
    )
    _REQUEST_UPDATE_STATEMENTS: ClassVar[dict[str, str]] = {
        "status": "UPDATE download_requests SET status = ? WHERE id = ?",
        "note": "UPDATE download_requests SET note = ? WHERE id = ?",
        "admin_note": "UPDATE download_requests SET admin_note = ? WHERE id = ?",
        "reviewed_by": "UPDATE download_requests SET reviewed_by = ? WHERE id = ?",
        "reviewed_at": "UPDATE download_requests SET reviewed_at = ? WHERE id = ?",
    }

    def update_request(
        self,
        request_id: int,
        expected_current_status: str | None = None,
        **kwargs: object,
    ) -> dict[str, Any]:
        """Update request fields and return the updated record."""
        if not kwargs:
            request = self.get_request(request_id)
            if request is None:
                msg = f"Request {request_id} not found"
                raise ValueError(msg)
            if expected_current_status is not None:
                normalized_expected_status = normalize_request_status(expected_current_status)
                if request["status"] != normalized_expected_status:
                    msg = "Request state changed before update"
                    raise ValueError(msg)
            return request

        for key in kwargs:
            if key not in self._ALLOWED_REQUEST_UPDATE_COLUMNS:
                msg = f"Invalid request column: {key}"
                raise ValueError(msg)

        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT * FROM download_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
                current = dict(row) if row else None
                if current is None:
                    msg = f"Request {request_id} not found"
                    raise ValueError(msg)

                if expected_current_status is not None:
                    normalized_expected_status = normalize_request_status(expected_current_status)
                    if current["status"] != normalized_expected_status:
                        msg = "Request state changed before update"
                        raise ValueError(msg)

                updates = dict(kwargs)

                if "status" in updates:
                    _, normalized_status = validate_status_transition(
                        current["status"],
                        updates["status"],
                    )
                    updates["status"] = normalized_status

                for column, value in updates.items():
                    conn.execute(self._REQUEST_UPDATE_STATEMENTS[column], (value, request_id))
                conn.commit()

                updated_row = conn.execute(
                    "SELECT * FROM download_requests WHERE id = ?",
                    (request_id,),
                ).fetchone()
                parsed = dict(updated_row) if updated_row else None
                if parsed is None:
                    msg = f"Request {request_id} not found after update"
                    raise ValueError(msg)
                return parsed
            finally:
                conn.close()
