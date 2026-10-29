"""Tests for SQLite user and canonical Book-request persistence."""

import json
import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bookflow.core.user_db import UserDB


@pytest.fixture
def db_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield os.path.join(tmpdir, "bookflow.db")


@pytest.fixture
def user_db(db_path):
    db = UserDB(db_path)
    db.initialize()
    return db


def add_book(user_db, user):
    conn = user_db._connect()
    try:
        cursor = conn.execute(
            "INSERT INTO books (metadata_provider, provider_book_id, title) VALUES (?, ?, ?)",
            ("test", f"book-{user['id']}", "Test Book"),
        )
        book_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO user_library (user_id, book_id) VALUES (?, ?)", (user["id"], book_id)
        )
        conn.commit()
        return book_id
    finally:
        conn.close()


def test_request_notification_context_includes_pending_requesters_and_cover(user_db):
    conn = user_db._connect()
    try:
        first_id = int(
            conn.execute("INSERT INTO users (username) VALUES (?)", ("requester-one",)).lastrowid
        )
        second_id = int(
            conn.execute("INSERT INTO users (username) VALUES (?)", ("requester-two",)).lastrowid
        )
        book_id = int(
            conn.execute(
                "INSERT INTO books (metadata_provider, provider_book_id, title, author, cover_url) "
                "VALUES (?, ?, ?, ?, ?)",
                ("test", "notification-book", "Notification Book", "Author", "https://cover"),
            ).lastrowid
        )
        first_request_id = int(
            conn.execute(
                "INSERT INTO download_requests (user_id, book_id) VALUES (?, ?)",
                (first_id, book_id),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO download_requests (user_id, book_id) VALUES (?, ?)",
            (second_id, book_id),
        )
        conn.commit()
    finally:
        conn.close()

    by_request = user_db.get_request_notification_context(request_id=first_request_id)
    by_book = user_db.get_request_notification_context(book_id=book_id)

    assert by_request == {
        "book_id": book_id,
        "title": "Notification Book",
        "author": "Author",
        "cover_url": "https://cover",
        "requesters": ["requester-one"],
    }
    assert by_book is not None
    assert by_book["requesters"] == ["requester-one", "requester-two"]


def test_request_notification_context_resolves_shared_request_process(user_db):
    conn = user_db._connect()
    try:
        user_ids = [
            int(
                conn.execute(
                    "INSERT INTO users (username) VALUES (?)", (f"process-requester-{index}",)
                ).lastrowid
            )
            for index in (1, 2)
        ]
        book_id = int(
            conn.execute(
                "INSERT INTO books (metadata_provider, provider_book_id, title, author, cover_url) "
                "VALUES (?, ?, ?, ?, ?)",
                ("test", "process-book", "Process Book", "Author", "https://cover/process.jpg"),
            ).lastrowid
        )
        process_id = int(
            conn.execute(
                "INSERT INTO request_processes "
                "(book_id, content_type, profile_hash, profile_snapshot_json, status) "
                "VALUES (?, 'ebook', ?, '{}', 'downloading')",
                (book_id, "a" * 64),
            ).lastrowid
        )
        request_ids = [
            int(
                conn.execute(
                    "INSERT INTO requests "
                    "(user_id, book_id, request_process_id, profile_hash, "
                    "profile_snapshot_json, status) VALUES (?, ?, ?, ?, '{}', 'approved')",
                    (user_id, book_id, process_id, f"{index:064d}"),
                ).lastrowid
            )
            for index, user_id in enumerate(user_ids, start=1)
        ]
        conn.executemany(
            "INSERT INTO request_process_recipients (request_process_id, user_id, request_id) "
            "VALUES (?, ?, ?)",
            [
                (process_id, user_id, request_id)
                for user_id, request_id in zip(user_ids, request_ids, strict=True)
            ],
        )
        conn.commit()
    finally:
        conn.close()

    context = user_db.get_request_notification_context(request_id=request_ids[0])

    assert context is not None
    assert context["book_id"] == book_id
    assert context["requesters"] == ["process-requester-1", "process-requester-2"]
    assert context["cover_url"] == "https://cover/process.jpg"


def test_initialize_creates_canonical_request_schema(user_db, db_path):
    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(download_requests)")}
    migration = conn.execute(
        "SELECT version, name, status, completed_at FROM schema_migrations"
    ).fetchone()
    conn.close()
    assert columns == {
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
    assert migration[:3] == (1, "canonical-library-baseline", "completed")
    assert migration[3] is not None


def test_initialize_applies_persistent_login_rate_limit_migration(user_db, db_path):
    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(auth_login_rate_limits)")}
    migration = conn.execute(
        "SELECT name, status FROM schema_migrations WHERE version = 2"
    ).fetchone()
    conn.close()

    assert columns == {"username", "source_ip", "failure_count", "locked_until", "updated_at"}
    assert migration == ("persistent-auth-login-rate-limits", "completed")


def test_initialize_applies_preferred_language_migration_with_english_default(user_db, db_path):
    conn = sqlite3.connect(db_path)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(user_preferences)")}
    migration = conn.execute(
        "SELECT name, status FROM schema_migrations WHERE version = 14"
    ).fetchone()
    conn.close()

    assert "preferred_language" in columns
    assert migration == ("user-preferred-interface-language", "completed")
    user = user_db.create_user(username="english-default")
    assert user_db.get_personal_preferences(user["id"])["preferred_language"] == "en"


def test_create_user_uses_configured_interface_language_default(monkeypatch, user_db):
    from bookflow.core.config import config

    original_get = config.get
    monkeypatch.setattr(
        config,
        "get",
        lambda key, default=None: (
            "nl" if key == "DEFAULT_UI_LANGUAGE" else original_get(key, default)
        ),
    )

    configured = user_db.create_user(username="dutch-default")
    explicit = user_db.create_user(username="explicit-english", preferred_language="en")

    assert user_db.get_personal_preferences(configured["id"])["preferred_language"] == "nl"
    assert user_db.get_personal_preferences(explicit["id"])["preferred_language"] == "en"


def test_create_user_rejects_an_unsupported_preferred_language(user_db):
    with pytest.raises(ValueError, match="Invalid preferred_language: de"):
        user_db.create_user(username="unsupported-language", preferred_language="de")


def test_login_rate_limit_persists_across_user_db_reinitialization(db_path):
    now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    first = UserDB(db_path)
    first.initialize()
    initial = first.record_login_failure(
        username="Alice",
        source_ip="203.0.113.8",
        max_failures=2,
        lockout_duration=timedelta(minutes=30),
        now=now,
    )
    assert initial["failure_count"] == 1
    assert initial["locked_until"] is None

    restarted = UserDB(db_path)
    restarted.initialize()
    assert restarted.get_login_rate_limit(username=" alice ", source_ip="203.0.113.8") == initial
    locked = restarted.record_login_failure(
        username=" alice ",
        source_ip=" 203.0.113.8 ",
        max_failures=2,
        lockout_duration=timedelta(minutes=30),
        now=now + timedelta(minutes=1),
    )

    assert locked["username"] == "alice"
    assert locked["source_ip"] == "203.0.113.8"
    assert locked["failure_count"] == 2
    assert locked["locked_until"] == "2026-07-27T12:31:00+00:00"


def test_login_rate_limits_are_isolated_by_username_and_source_ip(user_db):
    now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    same_identity = user_db.record_login_failure(
        username="Alice",
        source_ip="198.51.100.10",
        max_failures=3,
        lockout_duration=timedelta(minutes=30),
        now=now,
    )
    same_identity = user_db.record_login_failure(
        username=" ALICE ",
        source_ip=" 198.51.100.10 ",
        max_failures=3,
        lockout_duration=timedelta(minutes=30),
        now=now + timedelta(seconds=1),
    )
    other_ip = user_db.record_login_failure(
        username="alice",
        source_ip="198.51.100.11",
        max_failures=3,
        lockout_duration=timedelta(minutes=30),
        now=now,
    )
    other_user = user_db.record_login_failure(
        username="bob",
        source_ip="198.51.100.10",
        max_failures=3,
        lockout_duration=timedelta(minutes=30),
        now=now,
    )

    assert same_identity["failure_count"] == 2
    assert other_ip["failure_count"] == 1
    assert other_user["failure_count"] == 1


def test_login_rate_limit_clear_and_expired_cleanup(user_db):
    now = datetime(2026, 7, 27, 12, 0, tzinfo=UTC)
    user_db.record_login_failure(
        username="alice",
        source_ip="192.0.2.1",
        max_failures=1,
        lockout_duration=timedelta(minutes=30),
        now=now,
    )
    user_db.record_login_failure(
        username="bob",
        source_ip="192.0.2.2",
        max_failures=2,
        lockout_duration=timedelta(minutes=30),
        now=now,
    )

    assert user_db.clear_login_rate_limit(username="bob", source_ip="192.0.2.2") is True
    assert user_db.clear_login_rate_limit(username="bob", source_ip="192.0.2.2") is False
    assert user_db.clear_expired_login_rate_limits(now=now + timedelta(minutes=29)) == 0
    assert user_db.clear_expired_login_rate_limits(now=now + timedelta(minutes=30)) == 1
    assert user_db.get_login_rate_limit(username="alice", source_ip="192.0.2.1") is None


def test_initialize_quarantines_legacy_requests_without_affecting_other_tables(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            oidc_subject TEXT UNIQUE,
            role TEXT NOT NULL DEFAULT 'user'
        );
        CREATE TABLE download_requests (id INTEGER PRIMARY KEY, user_id INTEGER, content_type TEXT, book_data TEXT);
    """)
    conn.execute("INSERT INTO users (id, username) VALUES (1, 'legacy')")
    conn.execute("INSERT INTO download_requests VALUES (1, 1, 'ebook', '{}')")
    conn.commit()
    conn.close()

    UserDB(db_path).initialize()

    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM download_requests").fetchone()[0] == 0
    assert conn.execute("SELECT username FROM users WHERE id = 1").fetchone() == ("legacy",)
    quarantined = conn.execute(
        """
        SELECT source_row_id, payload_json, reason
        FROM legacy_download_requests_review
        """
    ).fetchone()
    assert quarantined[0] == "1"
    assert json.loads(quarantined[1]) == {
        "book_data": "{}",
        "content_type": "ebook",
        "id": 1,
        "user_id": 1,
    }
    assert quarantined[2] == "incompatible_schema_requires_manual_review"
    assert {row[1] for row in conn.execute("PRAGMA table_info(download_requests)")} == {
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
    conn.execute(
        "INSERT INTO books (metadata_provider, provider_book_id, title) VALUES ('test', '1', 'Book')"
    )
    conn.execute("INSERT INTO download_requests (user_id, book_id) VALUES (1, 1)")
    assert conn.execute("SELECT id FROM download_requests").fetchone()[0] > 1
    conn.close()

    backup_root = Path(f"{db_path}.migration-backups")
    manifests = list(backup_root.glob("*.manifest.json"))
    backups = list(backup_root.glob("*.sqlite3"))
    assert len(manifests) == 1
    assert len(backups) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["backup_path"] == str(backups[0])
    backup_conn = sqlite3.connect(backups[0])
    try:
        assert backup_conn.execute("SELECT book_data FROM download_requests").fetchone() == ("{}",)
        assert (
            backup_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    finally:
        backup_conn.close()

    UserDB(db_path).initialize()
    assert len(list(backup_root.glob("*.manifest.json"))) == 1


def test_initialize_preserves_history_and_links_when_removing_unique_task_id(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username TEXT UNIQUE NOT NULL,
            oidc_subject TEXT UNIQUE,
            role TEXT NOT NULL DEFAULT 'user'
        );
        CREATE TABLE download_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL UNIQUE,
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
            terminal_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE user_downloads (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            history_id INTEGER NOT NULL REFERENCES download_history(id) ON DELETE CASCADE,
            added_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, history_id)
        );
        INSERT INTO users (id, username) VALUES (1, 'legacy');
        INSERT INTO download_history (
            id, task_id, user_id, username, request_id, source, title, format,
            final_status, download_path, terminal_at
        )
        VALUES (
            4, 'shared-task', 1, 'legacy', 23, 'test', 'Book', 'epub',
            'complete', '/books/book.epub', '2026-01-02 03:04:05'
        );
        INSERT INTO user_downloads (user_id, history_id) VALUES (1, 4);
    """)
    conn.commit()
    conn.close()

    UserDB(db_path).initialize()

    conn = sqlite3.connect(db_path)
    migrated = conn.execute(
        """
        SELECT task_id, request_id, retry_payload, queued_at, terminal_at, download_path
        FROM download_history
        WHERE id = 4
        """
    ).fetchone()
    assert migrated[:3] == ("shared-task", 23, None)
    assert migrated[3] is not None
    assert migrated[4:] == ("2026-01-02 03:04:05", "/books/book.epub")
    assert conn.execute("SELECT * FROM user_downloads").fetchone()[:2] == (1, 4)
    conn.execute(
        """
        INSERT INTO download_history (task_id, source, title, final_status)
        VALUES ('shared-task', 'test', 'Second file', 'complete')
        """
    )
    conn.execute(
        "INSERT INTO books (metadata_provider, provider_book_id, title) VALUES ('test', '2', 'Book')"
    )
    request_cursor = conn.execute("INSERT INTO download_requests (user_id, book_id) VALUES (1, 1)")
    assert request_cursor.lastrowid > 23
    conn.commit()
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM download_history WHERE task_id = 'shared-task'"
        ).fetchone()[0]
        == 2
    )
    conn.close()


def test_book_request_lifecycle(user_db):
    member = user_db.create_user(username="member", library_capability="request-only")
    admin = user_db.create_user(username="admin", role="admin")
    book_id = add_book(user_db, member)

    created = user_db.create_library_request(
        user_id=member["id"], book_id=book_id, note="Please add"
    )
    assert created["book_id"] == book_id
    assert created["status"] == "pending"
    assert user_db.list_pending_book_requests(book_id) == [created]
    assert user_db.list_requests(user_id=member["id"], status="pending") == [created]

    rejected = user_db.update_request(
        created["id"],
        expected_current_status="pending",
        status="rejected",
        admin_note="No",
        reviewed_by=admin["id"],
    )
    assert rejected["status"] == "rejected"
    assert rejected["admin_note"] == "No"
    assert user_db.get_request(created["id"]) == rejected


def test_fulfil_pending_book_requests_grants_completed_files(user_db):
    member = user_db.create_user(username="member")
    admin = user_db.create_user(username="admin")
    book_id = add_book(user_db, member)
    request = user_db.create_library_request(user_id=member["id"], book_id=book_id)
    conn = user_db._connect()
    try:
        conn.execute(
            "INSERT INTO download_history (task_id, source, title, final_status, download_path, book_id) VALUES (?, ?, ?, ?, ?, ?)",
            ("task", "test", "Test Book", "complete", "/books/test.epub", book_id),
        )
        conn.commit()
    finally:
        conn.close()

    fulfilled = user_db.fulfil_pending_book_requests(book_id=book_id, reviewed_by=admin["id"])
    assert fulfilled[0]["id"] == request["id"]
    assert fulfilled[0]["status"] == "fulfilled"


def test_delete_user_cascades_requests_and_clears_reviewer(user_db):
    member = user_db.create_user(username="member")
    admin = user_db.create_user(username="admin")
    book_id = add_book(user_db, member)
    request = user_db.create_library_request(user_id=member["id"], book_id=book_id)
    user_db.update_request(request["id"], reviewed_by=admin["id"])
    user_db.delete_user(admin["id"])
    assert user_db.get_request(request["id"])["reviewed_by"] is None
    user_db.delete_user(member["id"])
    assert user_db.get_request(request["id"]) is None
