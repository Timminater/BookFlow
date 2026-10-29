"""Tests for the versioned SQLite migration runner."""

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from bookflow.core.db_migrations import (
    Migration,
    MigrationChecksumError,
    SQLiteMigrator,
    UnsupportedSchemaVersionError,
)
from bookflow.core.kobo_db import KOB0_DB_SQL
from bookflow.core.user_db import UserDB


def test_released_kobo_migration_is_immutable():
    assert hashlib.sha256(KOB0_DB_SQL.encode()).hexdigest() == (
        "5252576f2bc9c0a17d57053c539e09586194e950d2ac7fc02ab390f7bd97b2a8"
    )


@pytest.mark.parametrize("fresh_v129", [False, True])
def test_kobo_reading_states_upgrade_preserves_released_journal(tmp_path, fresh_v129):
    db_path = tmp_path / "upgrade.db"
    database = UserDB(str(db_path))
    database.initialize()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = 31")
        if fresh_v129:
            conn.execute(
                "UPDATE schema_migrations SET checksum = ? WHERE version = 8",
                ("e211e37e7cf67f74aa97266a1d9f1f28ab13c61f79585a0a3971ee9f01b1f9f0",),
            )
        else:
            conn.execute("DROP TABLE kobo_reading_states")
        conn.execute("INSERT INTO users (username, role) VALUES ('existing', 'user')")
        journal = conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall()
    database.initialize()
    database.initialize()
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute(
                "SELECT * FROM schema_migrations WHERE version <= 30 ORDER BY version"
            ).fetchall()
            == journal
        )
        assert conn.execute("SELECT username FROM users").fetchall() == [("existing",)]
        assert conn.execute("SELECT COUNT(*) FROM kobo_reading_states").fetchone() == (0,)
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def checksum(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_migration_creates_preflight_backup_and_is_idempotent(tmp_path):
    db_path = tmp_path / "users.db"
    conn = connect(db_path)
    conn.execute("CREATE TABLE legacy_data (id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("INSERT INTO legacy_data VALUES (1, 'preserve me')")
    conn.commit()

    migration = Migration(
        version=1,
        name="create-current-data",
        checksum=checksum("create-current-data-v1"),
        apply=lambda connection: connection.execute(
            "CREATE TABLE current_data (id INTEGER PRIMARY KEY)"
        ),
    )
    migrator = SQLiteMigrator(conn, str(db_path), (migration,))
    manifest_path = migrator.migrate()

    assert manifest_path is not None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    backup_path = Path(manifest["backup_path"])
    assert backup_path.exists()
    assert manifest["backup_size"] == backup_path.stat().st_size
    assert manifest["backup_sha256"] == hashlib.sha256(backup_path.read_bytes()).hexdigest()
    backup_conn = sqlite3.connect(backup_path)
    try:
        assert backup_conn.execute("SELECT * FROM legacy_data").fetchall() == [(1, "preserve me")]
        assert (
            backup_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'current_data'"
            ).fetchone()
            is None
        )
    finally:
        backup_conn.close()

    row = conn.execute(
        "SELECT version, name, checksum, status, completed_at FROM schema_migrations"
    ).fetchone()
    assert tuple(row[:4]) == (1, migration.name, migration.checksum, "completed")
    assert row["completed_at"] is not None
    assert migrator.migrate() is None
    assert len(list(manifest_path.parent.glob("*.manifest.json"))) == 1
    conn.close()


def test_failed_migration_rolls_back_and_can_be_retried(tmp_path):
    db_path = tmp_path / "users.db"
    conn = connect(db_path)
    migration_checksum = checksum("recoverable-v1")

    def fail_after_schema_change(connection):
        connection.execute("CREATE TABLE partial_change (id INTEGER PRIMARY KEY)")
        raise RuntimeError("injected failure")

    failing = Migration(1, "recoverable", migration_checksum, fail_after_schema_change)
    with pytest.raises(RuntimeError, match="injected failure"):
        SQLiteMigrator(conn, str(db_path), (failing,)).migrate()

    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'partial_change'"
        ).fetchone()
        is None
    )
    failed_row = conn.execute(
        "SELECT status, error_message FROM schema_migrations WHERE version = 1"
    ).fetchone()
    assert failed_row["status"] == "failed"
    assert failed_row["error_message"] == "injected failure"

    successful = Migration(
        1,
        "recoverable",
        migration_checksum,
        lambda connection: connection.execute(
            "CREATE TABLE completed_change (id INTEGER PRIMARY KEY)"
        ),
    )
    SQLiteMigrator(conn, str(db_path), (successful,)).migrate()
    assert (
        conn.execute("SELECT status FROM schema_migrations WHERE version = 1").fetchone()[0]
        == "completed"
    )
    assert (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'completed_change'"
        ).fetchone()
        is not None
    )
    conn.close()


def test_newer_database_version_is_blocked_before_changes(tmp_path):
    db_path = tmp_path / "users.db"
    conn = connect(db_path)
    migration = Migration(
        1,
        "supported",
        checksum("supported-v1"),
        lambda connection: connection.execute("CREATE TABLE supported (id INTEGER)"),
    )
    SQLiteMigrator(conn, str(db_path), (migration,)).migrate()
    conn.execute(
        """
        INSERT INTO schema_migrations (
            version, name, checksum, started_at, completed_at, status
        )
        VALUES (2, 'future', ?, 'now', 'now', 'completed')
        """,
        (checksum("future-v2"),),
    )
    conn.commit()
    manifest_count = len(list((tmp_path / "users.db.migration-backups").glob("*.manifest.json")))

    with pytest.raises(UnsupportedSchemaVersionError, match="newer than this application"):
        SQLiteMigrator(conn, str(db_path), (migration,)).migrate()

    assert (
        len(list((tmp_path / "users.db.migration-backups").glob("*.manifest.json")))
        == manifest_count
    )
    conn.close()


def test_changed_applied_migration_is_blocked(tmp_path):
    db_path = tmp_path / "users.db"
    conn = connect(db_path)
    original = Migration(
        1,
        "immutable",
        checksum("immutable-v1"),
        lambda connection: connection.execute("CREATE TABLE immutable (id INTEGER)"),
    )
    SQLiteMigrator(conn, str(db_path), (original,)).migrate()
    changed = Migration(1, "immutable", checksum("changed-v1"), original.apply)

    with pytest.raises(MigrationChecksumError, match="checksum differs"):
        SQLiteMigrator(conn, str(db_path), (changed,)).migrate()
    conn.close()


def test_request_process_rename_migrates_existing_scoped_blocklist_row(tmp_path):
    """V25 must translate constrained scope values while preserving linked data."""
    db_path = tmp_path / "users.db"
    database = UserDB(str(db_path))
    conn = database._connect()
    try:
        for version in range(1, 25):
            getattr(database, f"_apply_migration_{version:03d}")(conn)
        conn.commit()
        user_id = conn.execute(
            "INSERT INTO users (username, role) VALUES ('admin', 'admin')"
        ).lastrowid
        book_id = conn.execute(
            "INSERT INTO books (metadata_provider, provider_book_id, title) "
            "VALUES ('test', 'rename', 'Rename test')"
        ).lastrowid
        process_id = conn.execute(
            """
            INSERT INTO acquisitions (
                book_id, content_type, profile_hash, profile_snapshot_json, status
            )
            VALUES (?, 'ebook', ?, '{}', 'queued')
            """,
            (book_id, "a" * 64),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO blocked_releases (
                fingerprint, scope, acquisition_id, reason, blocked_by
            )
            VALUES ('candidate', 'acquisition', ?, 'failed', ?)
            """,
            (process_id, user_id),
        )
        conn.commit()

        database._apply_migration_025(conn)
        conn.commit()

        row = conn.execute("SELECT scope, request_process_id FROM blocked_releases").fetchone()
        assert tuple(row) == ("request_process", process_id)
        assert conn.execute(
            "SELECT id FROM request_processes WHERE id = ?", (process_id,)
        ).fetchone()
        assert (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='acquisitions'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()
