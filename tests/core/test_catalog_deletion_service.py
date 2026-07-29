from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from flask import Flask

from bookflow.core.catalog_deletion_service import CatalogDeletionError, CatalogDeletionService
from bookflow.core.user_db import UserDB
from bookflow.core.v1_api import V1Actor, register_v1_api


def _database(tmp_path: Path) -> tuple[Path, int, int]:
    path = tmp_path / "catalog.sqlite"
    db = UserDB(str(path))
    db.initialize()
    actor_id = int(db.create_user(username="admin")["id"])
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            "INSERT INTO books(metadata_provider, provider_book_id, title, isbn_13) VALUES('test', 'one', 'Book', '9780306406157')"
        )
        assert cursor.lastrowid is not None
        book_id = int(cursor.lastrowid)
    return path, book_id, actor_id


def _file(
    conn: sqlite3.Connection, book_id: int, path: Path, suffix: str, parent: int | None = None
) -> int:
    digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else (suffix * 64)[:64]
    size = path.stat().st_size if path.exists() else 1
    cursor = conn.execute(
        """INSERT INTO book_files(book_id, storage_path, file_format, size_bytes, sha256, import_status, derived_from_file_id)
           VALUES(?, ?, 'epub', ?, ?, 'imported', ?)""",
        (book_id, str(path), size, digest, parent),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def test_delete_removes_equivalent_books_and_files_after_commit(tmp_path: Path) -> None:
    path, book_id, actor_id = _database(tmp_path)
    root = tmp_path / "files"
    root.mkdir()
    first = root / "first.epub"
    second = root / "second.epub"
    first.write_text("one")
    second.write_text("two")
    with sqlite3.connect(path) as conn:
        source = _file(conn, book_id, first, "a")
        _file(conn, book_id, second, "b", source)
    service = CatalogDeletionService(
        str(path),
        lambda stored: Path(stored).resolve().relative_to(root.resolve()) and Path(stored),
    )

    result = service.delete(
        book_id, actor_user_id=actor_id, plan_token=service.plan(book_id).token()
    )

    assert result["status"] == "deleted"
    assert not first.exists()
    assert not second.exists()
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM book_files").fetchone()[0] == 0


def test_delete_blocks_active_request_process(tmp_path: Path) -> None:
    path, book_id, actor_id = _database(tmp_path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO request_processes(book_id, content_type, profile_hash, profile_snapshot_json, status)
               VALUES(?, 'ebook', ?, '{}', 'downloading')""",
            (book_id, "a" * 64),
        )

    service = CatalogDeletionService(str(path), None)
    with pytest.raises(CatalogDeletionError, match="blocked"):
        service.delete(book_id, actor_user_id=actor_id, plan_token=service.plan(book_id).token())


def test_repeated_delete_is_idempotent(tmp_path: Path) -> None:
    path, book_id, actor_id = _database(tmp_path)
    service = CatalogDeletionService(str(path), None)

    token = service.plan(book_id).token()
    assert service.delete(book_id, actor_user_id=actor_id, plan_token=token)["status"] == "deleted"
    assert (
        service.delete(book_id, actor_user_id=actor_id, plan_token=token)["status"]
        == "already_deleted"
    )


def test_failed_cleanup_is_durable_and_retries_after_restart(tmp_path: Path) -> None:
    path, book_id, actor_id = _database(tmp_path)
    stored = tmp_path / "book.epub"
    stored.write_bytes(b"catalog bytes")
    with sqlite3.connect(path) as conn:
        _file(conn, book_id, stored, "a")
    failing = CatalogDeletionService(
        str(path), lambda _stored: (_ for _ in ()).throw(OSError("offline"))
    )
    result = failing.delete(
        book_id, actor_user_id=actor_id, plan_token=failing.plan(book_id).token()
    )

    assert result["cleanup"]["pending"] == 1
    restarted = CatalogDeletionService(str(path), lambda _stored: stored)
    retried = restarted.retry_cleanup(result["operation_id"])

    assert retried["pending"] == 0
    assert not stored.exists()
    with sqlite3.connect(path) as conn:
        assert (
            conn.execute(
                "SELECT state FROM catalog_deletion_operations WHERE id=?",
                (result["operation_id"],),
            ).fetchone()[0]
            == "cleanup_complete"
        )


def test_delete_removes_three_level_derived_chain_children_first(tmp_path: Path) -> None:
    path, book_id, actor_id = _database(tmp_path)
    root = tmp_path / "files"
    root.mkdir()
    paths = [root / f"{name}.epub" for name in ("one", "two", "three")]
    for item in paths:
        item.write_text(item.name)
    with sqlite3.connect(path) as conn:
        first = _file(conn, book_id, paths[0], "a")
        second = _file(conn, book_id, paths[1], "b", first)
        _file(conn, book_id, paths[2], "c", second)
    service = CatalogDeletionService(str(path), lambda stored: Path(stored))
    token = service.plan(book_id).token()

    assert service.delete(book_id, actor_user_id=actor_id, plan_token=token)["status"] == "deleted"
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM book_files").fetchone()[0] == 0


def test_verified_cleanup_refuses_symlink_without_touching_target(tmp_path: Path) -> None:
    path, _book_id, _actor_id = _database(tmp_path)
    outside = tmp_path / "outside.epub"
    outside.write_bytes(b"outside bytes")
    link = tmp_path / "link.epub"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable for this Windows test account")
    service = CatalogDeletionService(str(path), lambda _stored: link)

    with pytest.raises(ValueError, match="symbolic link"):
        service._remove_verified_file(
            "ignored", hashlib.sha256(outside.read_bytes()).hexdigest(), outside.stat().st_size
        )
    assert outside.exists()


def test_deletion_operation_keeps_audit_when_actor_is_removed(tmp_path: Path) -> None:
    path, book_id, actor_id = _database(tmp_path)
    operation_id = "a" * 32
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """INSERT INTO catalog_deletion_operations(id, book_id, plan_token, actor_user_id, state, plan_json)
               VALUES(?,?,?,?, 'cleanup_pending', '{}')""",
            (operation_id, book_id, "b" * 64, actor_id),
        )
        conn.execute(
            """INSERT INTO catalog_deletion_audit_events(operation_id, actor_user_id, event_type, payload_json)
               VALUES(?,?, 'db_committed', '{}')""",
            (operation_id, actor_id),
        )
        conn.execute("DELETE FROM users WHERE id=?", (actor_id,))
        operation_actor = conn.execute(
            "SELECT actor_user_id FROM catalog_deletion_operations WHERE id=?", (operation_id,)
        ).fetchone()[0]
        audit_actor = conn.execute(
            "SELECT actor_user_id FROM catalog_deletion_audit_events WHERE operation_id=?",
            (operation_id,),
        ).fetchone()[0]
    assert operation_actor is None
    assert audit_actor is None


def test_durable_cleanup_job_is_claimed_once_across_two_service_instances(tmp_path: Path) -> None:
    path, book_id, actor_id = _database(tmp_path)
    operation_id = "c" * 32
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO catalog_deletion_operations(id, book_id, plan_token, actor_user_id, state, plan_json)
               VALUES(?,?,?,?, 'cleanup_pending', '{}')""",
            (operation_id, book_id, "d" * 64, actor_id),
        )
    first = CatalogDeletionService(str(path), None)
    second = CatalogDeletionService(str(path), None)
    assert first.schedule_pending_cleanup() == 1
    first_result = first.run_scheduled_cleanup(worker_id="worker-one")
    second_result = second.run_scheduled_cleanup(worker_id="worker-two")
    assert first_result["claimed"] == 1
    assert second_result["claimed"] == 0
    assert first.operation(operation_id)["state"] == "cleanup_complete"


def test_recovery_restart_discovers_pending_cleanup_job(tmp_path: Path) -> None:
    path, book_id, actor_id = _database(tmp_path)
    operation_id = "e" * 32
    with sqlite3.connect(path) as conn:
        conn.execute(
            """INSERT INTO catalog_deletion_operations(id, book_id, plan_token, actor_user_id, state, plan_json)
               VALUES(?,?,?,?, 'cleanup_pending', '{}')""",
            (operation_id, book_id, "f" * 64, actor_id),
        )
    restarted = CatalogDeletionService(str(path), None)
    restarted.recover_pending()
    assert restarted.operation(operation_id)["state"] == "cleanup_complete"


def test_v1_catalog_delete_is_admin_only_and_does_not_require_membership(tmp_path: Path) -> None:
    path, book_id, _actor_id = _database(tmp_path)
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_v1_api(
        app, database_path=path, resolve_actor=lambda: V1Actor(user_id=1, is_admin=False)
    )

    assert app.test_client().delete(f"/api/v1/admin/catalog/books/{book_id}").status_code == 403

    admin_app = Flask(__name__)
    admin_app.config["TESTING"] = True
    register_v1_api(
        admin_app, database_path=path, resolve_actor=lambda: V1Actor(user_id=1, is_admin=True)
    )
    plan = admin_app.test_client().get(f"/api/v1/admin/catalog/books/{book_id}").json
    response = admin_app.test_client().delete(
        f"/api/v1/admin/catalog/books/{book_id}", json={"plan_token": plan["plan_token"]}
    )

    assert response.status_code == 200
    assert response.json["status"] == "deleted"
