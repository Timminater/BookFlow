from __future__ import annotations

import sqlite3

import pytest

from bookflow.core.db_migrations import Migration, SQLiteMigrator
from bookflow.core.request_workflow_service import (
    RequestWorkflowError,
    RequestWorkflowService,
)
from bookflow.core.requests_service import RequestServiceError
from bookflow.core.user_db import _MIGRATION_019_CHECKSUM, UserDB


def _seed(db_path):
    db = UserDB(str(db_path))
    db.initialize()
    admin = db.create_user(username="admin", role="admin")
    alice = db.create_user(username="alice", role="user")
    bob = db.create_user(username="bob", role="user")
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        """
        INSERT INTO books (metadata_provider, provider_book_id, title)
        VALUES ('test', 'dune', 'Dune')
        """
    )
    book_id = cursor.lastrowid
    conn.commit()
    conn.close()
    assert book_id is not None
    return db, admin, alice, bob, book_id


def _insert_file(
    db_path,
    *,
    book_id,
    suffix="a",
    file_format="epub",
    content_type="ebook",
):
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        """
        INSERT INTO book_files (
            book_id, storage_path, file_format, size_bytes, sha256, mime_type,
            content_type, import_status
        )
        VALUES (?, ?, ?, 4, ?, ?, ?, 'imported')
        """,
        (
            book_id,
            f"/books/{suffix}.{file_format}",
            file_format,
            suffix * 64,
            "audio/mpeg" if content_type == "audiobook" else "application/epub+zip",
            content_type,
        ),
    )
    file_id = cursor.lastrowid
    conn.commit()
    conn.close()
    assert file_id is not None
    return file_id


def _insert_books(db_path, *, count: int) -> list[int]:
    conn = sqlite3.connect(db_path)
    book_ids: list[int] = []
    for index in range(count):
        cursor = conn.execute(
            """
            INSERT INTO books (metadata_provider, provider_book_id, title)
            VALUES ('limit-test', ?, ?)
            """,
            (f"book-{index}", f"Book {index}"),
        )
        assert cursor.lastrowid is not None
        book_ids.append(cursor.lastrowid)
    conn.commit()
    conn.close()
    return book_ids


def test_request_process_schema_is_present_after_migrations(tmp_path):
    db_path = tmp_path / "users.db"
    db, *_ = _seed(db_path)

    conn = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    journal = conn.execute(
        "SELECT version, name, status FROM schema_migrations ORDER BY version"
    ).fetchall()
    conn.close()

    assert {"requests", "request_processes", "request_process_recipients"} <= tables
    # The immutable v4 journal entry records its historical migration name;
    # current runtime tables intentionally use request-process terminology.
    assert any(version == 4 and status == "completed" for version, _name, status in journal)
    assert db is not None


def test_content_type_migration_backfills_files_and_rebuilds_active_request_identity(tmp_path):
    db_path = tmp_path / "pre-content-type.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE book_files (id INTEGER PRIMARY KEY, file_format TEXT NOT NULL);
        INSERT INTO book_files (id, file_format) VALUES (1, 'epub'), (2, 'M4B'), (3, 'zip');
        CREATE TABLE requests (
            id INTEGER PRIMARY KEY,
            user_id INTEGER NOT NULL,
            book_id INTEGER NOT NULL,
            content_type TEXT NOT NULL,
            profile_hash TEXT NOT NULL,
            status TEXT NOT NULL
        );
        CREATE UNIQUE INDEX idx_requests_one_active_profile
        ON requests (user_id, book_id, profile_hash)
        WHERE status IN ('pending_approval', 'approved');
        INSERT INTO requests VALUES (1, 7, 8, 'ebook', 'profile', 'pending_approval');
        """
    )
    migration = Migration(
        version=1,
        name="request-content-type-isolation",
        checksum=_MIGRATION_019_CHECKSUM,
        apply=UserDB._apply_migration_019,
    )
    SQLiteMigrator(conn, str(db_path), (migration,)).migrate()

    file_types = conn.execute("SELECT id, content_type FROM book_files ORDER BY id").fetchall()
    conn.execute(
        "INSERT INTO requests VALUES (2, 7, 8, 'audiobook', 'profile', 'pending_approval')"
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO requests VALUES (3, 7, 8, 'ebook', 'profile', 'pending_approval')"
        )
    conn.close()

    assert file_types == [(1, "ebook"), (2, "audiobook"), (3, "ebook")]


def test_duplicate_request_and_approval_are_idempotent(tmp_path):
    db_path = tmp_path / "users.db"
    _db, admin, alice, _bob, book_id = _seed(db_path)
    service = RequestWorkflowService(db_path)

    first = service.create_request(
        user_id=alice["id"], book_id=book_id, profile={"formats": ["epub"]}
    )
    duplicate = service.create_request(
        user_id=alice["id"],
        book_id=book_id,
        profile={"formats": ["epub"]},
    )
    approved, request_process, created = service.approve_request(
        first["id"],
        reviewer_user_id=admin["id"],
    )
    replayed, replayed_request_process, replay_created = service.approve_request(
        first["id"],
        reviewer_user_id=admin["id"],
    )

    assert duplicate["id"] == first["id"]
    assert approved["status"] == replayed["status"] == "approved"
    assert request_process is not None and replayed_request_process is not None
    assert request_process["id"] == replayed_request_process["id"]
    assert created is True
    assert replay_created is False


def test_user_cannot_exceed_ten_open_requests(tmp_path):
    db_path = tmp_path / "users.db"
    _db, _admin, alice, bob, _book_id = _seed(db_path)
    book_ids = _insert_books(db_path, count=11)
    service = RequestWorkflowService(db_path)
    profile = {"formats": ["epub"]}

    created = [
        service.create_request(user_id=alice["id"], book_id=book_id, profile=profile)
        for book_id in book_ids[:10]
    ]

    replay = service.create_request(user_id=alice["id"], book_id=book_ids[0], profile=profile)
    assert replay["id"] == created[0]["id"]
    with pytest.raises(RequestWorkflowError) as error:
        service.create_request(user_id=alice["id"], book_id=book_ids[10], profile=profile)

    assert error.value.code == "open_request_limit_reached"
    assert error.value.status_code == 429
    assert (
        service.create_request(user_id=bob["id"], book_id=book_ids[10], profile=profile)["status"]
        == "pending_approval"
    )


def test_user_cannot_submit_more_than_ten_requests_per_local_day(tmp_path):
    db_path = tmp_path / "users.db"
    db, _admin, alice, _bob, _book_id = _seed(db_path)
    book_ids = _insert_books(db_path, count=12)
    service = RequestWorkflowService(db_path)
    profile = {"formats": ["epub"]}

    for book_id in book_ids[:10]:
        service.create_request(user_id=alice["id"], book_id=book_id, profile=profile)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE requests SET status = 'cancelled' WHERE user_id = ?",
            (alice["id"],),
        )

    with pytest.raises(RequestWorkflowError) as error:
        service.create_request(user_id=alice["id"], book_id=book_ids[10], profile=profile)
    assert error.value.code == "daily_request_limit_reached"
    assert error.value.status_code == 429

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO user_library (user_id, book_id) VALUES (?, ?)",
            (alice["id"], book_ids[11]),
        )
    with pytest.raises(RequestServiceError) as legacy_error:
        db.create_library_request(user_id=alice["id"], book_id=book_ids[11])
    assert legacy_error.value.code == "daily_request_limit_reached"
    assert legacy_error.value.status_code == 429


def test_daily_request_limit_resets_on_the_next_local_day(tmp_path):
    db_path = tmp_path / "users.db"
    _db, _admin, alice, _bob, _book_id = _seed(db_path)
    book_ids = _insert_books(db_path, count=11)
    service = RequestWorkflowService(db_path)
    profile = {"formats": ["epub"]}

    for book_id in book_ids[:10]:
        service.create_request(user_id=alice["id"], book_id=book_id, profile=profile)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE requests
            SET status = 'cancelled', created_at = '2000-01-01 12:00:00'
            WHERE user_id = ?
            """,
            (alice["id"],),
        )

    created = service.create_request(user_id=alice["id"], book_id=book_ids[10], profile=profile)
    assert created["status"] == "pending_approval"


def test_legacy_rows_mirrored_into_canonical_requests_are_not_counted_twice(tmp_path):
    db_path = tmp_path / "users.db"
    _db, _admin, alice, _bob, _book_id = _seed(db_path)
    book_ids = _insert_books(db_path, count=6)
    service = RequestWorkflowService(db_path)
    profile = {"formats": ["epub"]}

    with sqlite3.connect(db_path) as conn:
        for book_id in book_ids[:5]:
            cursor = conn.execute(
                "INSERT INTO download_requests (user_id, book_id) VALUES (?, ?)",
                (alice["id"], book_id),
            )
            assert cursor.lastrowid is not None
            conn.execute(
                """
                INSERT INTO requests (
                    legacy_request_id, user_id, book_id, content_type,
                    profile_hash, profile_snapshot_json, status
                ) VALUES (?, ?, ?, 'ebook', ?, '{}', 'pending_approval')
                """,
                (cursor.lastrowid, alice["id"], book_id, "a" * 64),
            )

    created = service.create_request(user_id=alice["id"], book_id=book_ids[5], profile=profile)
    assert created["status"] == "pending_approval"


def test_user_policy_approves_only_the_requesters_own_request(tmp_path):
    db_path = tmp_path / "users.db"
    db, _admin, alice, bob, book_id = _seed(db_path)
    service = RequestWorkflowService(db_path)
    profile = {"formats": ["epub"]}
    bob_request = service.create_request(user_id=bob["id"], book_id=book_id, profile=profile)
    db.update_user(alice["id"], auto_approve_requests=True)

    alice_request = service.create_request(
        user_id=alice["id"],
        book_id=book_id,
        profile=profile,
    )

    assert alice_request["status"] == "approved"
    assert alice_request["approval_source"] == "user_policy"
    assert alice_request["reviewed_by"] == alice["id"]
    assert alice_request["request_process_id"] is not None
    conn = sqlite3.connect(db_path)
    persisted_bob = conn.execute(
        "SELECT status FROM requests WHERE id = ?", (bob_request["id"],)
    ).fetchone()
    conn.close()
    assert persisted_bob is not None
    assert persisted_bob[0] == "pending_approval"
    with pytest.raises(RequestWorkflowError, match="Admin access required"):
        service.approve_request(bob_request["id"], reviewer_user_id=alice["id"])


def test_disabling_user_policy_is_not_retroactive(tmp_path):
    db_path = tmp_path / "users.db"
    db, _admin, alice, _bob, book_id = _seed(db_path)
    db.update_user(alice["id"], auto_approve_requests=True)
    service = RequestWorkflowService(db_path)
    approved = service.create_request(
        user_id=alice["id"], book_id=book_id, profile={"formats": ["epub"]}
    )

    db.update_user(alice["id"], auto_approve_requests=False)
    conn = sqlite3.connect(db_path)
    persisted = conn.execute(
        "SELECT status, approval_source FROM requests WHERE id = ?", (approved["id"],)
    ).fetchone()
    conn.close()
    assert persisted is not None

    assert persisted == ("approved", "user_policy")


def test_audiobook_request_is_rejected(tmp_path):
    db_path = tmp_path / "users.db"
    _db, _admin, alice, _bob, book_id = _seed(db_path)
    service = RequestWorkflowService(db_path)
    profile = {"language": "en"}

    ebook = service.create_request(
        user_id=alice["id"],
        book_id=book_id,
        profile=profile,
        content_type="ebook",
    )
    with pytest.raises(RequestWorkflowError, match="no longer supported") as error:
        service.create_request(
            user_id=alice["id"],
            book_id=book_id,
            profile=profile,
            content_type="audiobook",
        )

    assert ebook["content_type"] == "ebook"
    assert error.value.code == "invalid_content_type"


def test_two_requesters_join_one_active_request_process_and_complete_atomically(tmp_path):
    db_path = tmp_path / "users.db"
    _db, admin, alice, bob, book_id = _seed(db_path)
    service = RequestWorkflowService(db_path)
    profile = {"formats": ["epub"], "language": "en"}
    alice_request = service.create_request(user_id=alice["id"], book_id=book_id, profile=profile)
    bob_request = service.create_request(user_id=bob["id"], book_id=book_id, profile=profile)

    _, first_request_process, first_created = service.approve_request(
        alice_request["id"],
        reviewer_user_id=admin["id"],
    )
    _, second_request_process, second_created = service.approve_request(
        bob_request["id"],
        reviewer_user_id=admin["id"],
    )
    assert first_request_process is not None and second_request_process is not None
    assert first_request_process["id"] == second_request_process["id"]
    assert first_created is True and second_created is False

    file_id = _insert_file(db_path, book_id=book_id)
    fulfilled = service.complete_request_process(first_request_process["id"], file_id=file_id)

    conn = sqlite3.connect(db_path)
    statuses = conn.execute("SELECT status FROM requests ORDER BY user_id").fetchall()
    entitlements = conn.execute(
        "SELECT user_id, file_id FROM user_file_entitlements ORDER BY user_id"
    ).fetchall()
    conn.close()
    assert sorted(fulfilled) == sorted([alice_request["id"], bob_request["id"]])
    assert statuses == [("fulfilled",), ("fulfilled",)]
    assert entitlements == [(alice["id"], file_id), (bob["id"], file_id)]


def test_existing_entitled_file_fulfils_without_request_process(tmp_path):
    db_path = tmp_path / "users.db"
    _db, _admin, alice, _bob, book_id = _seed(db_path)
    file_id = _insert_file(db_path, book_id=book_id, suffix="b")
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        INSERT INTO user_file_entitlements (user_id, file_id, grant_source)
        VALUES (?, ?, 'direct')
        """,
        (alice["id"], file_id),
    )
    conn.commit()
    conn.close()

    request_row = RequestWorkflowService(db_path).create_request(
        user_id=alice["id"],
        book_id=book_id,
        profile={"formats": ["epub"]},
    )

    assert request_row["status"] == "fulfilled"
    assert request_row["fulfilled_file_id"] == file_id
    assert request_row["request_process_id"] is None


def test_approval_after_import_grants_file_and_library_without_request_process(tmp_path):
    db_path = tmp_path / "users.db"
    _db, admin, alice, _bob, book_id = _seed(db_path)
    service = RequestWorkflowService(db_path)
    request_row = service.create_request(
        user_id=alice["id"],
        book_id=book_id,
        profile={"formats": ["epub"]},
    )
    file_id = _insert_file(db_path, book_id=book_id, suffix="e")

    approved, request_process, created = service.approve_request(
        request_row["id"],
        reviewer_user_id=admin["id"],
    )

    assert approved["status"] == "fulfilled"
    assert approved["fulfilled_file_id"] == file_id
    assert request_process is None
    assert created is False
    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT 1 FROM user_file_entitlements WHERE user_id = ? AND file_id = ?",
        (alice["id"], file_id),
    ).fetchone()
    assert conn.execute(
        "SELECT 1 FROM user_library WHERE user_id = ? AND book_id = ?",
        (alice["id"], book_id),
    ).fetchone()
    conn.close()


def test_audio_file_insert_is_blocked_by_database_guard(tmp_path):
    db_path = tmp_path / "users.db"
    _db, _admin, _alice, _bob, book_id = _seed(db_path)
    with pytest.raises(sqlite3.IntegrityError, match="only ebook files"):
        _insert_file(
            db_path,
            book_id=book_id,
            suffix="c",
            file_format="m4b",
            content_type="audiobook",
        )


def test_audio_request_process_cannot_be_created(tmp_path):
    db_path = tmp_path / "users.db"
    _db, _admin, alice, _bob, book_id = _seed(db_path)
    service = RequestWorkflowService(db_path)
    with pytest.raises(RequestWorkflowError, match="no longer supported"):
        service.create_request(
            user_id=alice["id"],
            book_id=book_id,
            profile={"formats": ["m4b"]},
            content_type="audiobook",
        )
