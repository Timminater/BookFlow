"""Transactional catalog, central-file and entitlement tests."""

import sqlite3
import uuid
from pathlib import Path

import pytest

from bookflow.core.book_matcher import BookMatchInput
from bookflow.core.catalog_service import CatalogService
from bookflow.core.catalog_storage_lease import CatalogStorageLease, catalog_path_lease_key
from bookflow.core.file_store import FileStore
from bookflow.core.user_db import UserDB


def test_catalog_reuses_one_book_for_isbn_and_attaches_second_provider(tmp_path: Path) -> None:
    db_path = tmp_path / "users.db"
    UserDB(str(db_path)).initialize()
    catalog = CatalogService(str(db_path))

    first = catalog.get_or_create_book(
        BookMatchInput(
            title="Canonical Book",
            author="Ada Lovelace",
            provider="hardcover",
            provider_book_id="one",
            isbn="978-0-306-40615-7",
        )
    )
    second = catalog.get_or_create_book(
        BookMatchInput(
            title="Different provider display name",
            provider="openlibrary",
            provider_book_id="two",
            isbn="0-306-40615-2",
        )
    )

    assert first.created is True
    assert second.created is False
    assert second.book_id == first.book_id
    assert second.match_method == "isbn10_to_isbn13"
    conn = UserDB(str(db_path))._connect()
    try:
        provider_count = conn.execute(
            "SELECT COUNT(*) FROM book_provider_identifiers WHERE book_id = ?", (first.book_id,)
        ).fetchone()[0]
        audit_methods = [
            row[0]
            for row in conn.execute(
                "SELECT method FROM book_match_audit WHERE matched_book_id = ? ORDER BY id",
                (first.book_id,),
            )
        ]
    finally:
        conn.close()
    assert provider_count == 2
    assert audit_methods == ["created", "isbn10_to_isbn13"]


def test_catalog_file_record_and_entitlement_are_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "users.db"
    user_db = UserDB(str(db_path))
    user_db.initialize()
    user = user_db.create_user(username="reader")
    catalog = CatalogService(str(db_path))
    book = catalog.get_or_create_book(
        BookMatchInput(title="A Book", provider="test", provider_book_id="1")
    )
    source_root = tmp_path / "source"
    storage_root = tmp_path / "storage"
    source_root.mkdir()
    storage_root.mkdir()
    source_file = source_root / "book.epub"
    source_file.write_bytes(b"canonical bytes")
    published = FileStore(storage_root).publish(source_file, source_roots=(source_root,))

    first_file = catalog.register_imported_file(book_id=book.book_id, published_file=published)
    second_file = catalog.register_imported_file(book_id=book.book_id, published_file=published)

    assert first_file["id"] == second_file["id"]
    assert catalog.grant_file_entitlement(user_id=user["id"], file_id=first_file["id"]) is True
    assert catalog.grant_file_entitlement(user_id=user["id"], file_id=first_file["id"]) is False


def test_central_publish_register_rejects_deletion_tombstone_before_writing(tmp_path: Path) -> None:
    db_path = tmp_path / "users.db"
    users = UserDB(str(db_path))
    users.initialize()
    actor = users.create_user(username="admin")
    catalog = CatalogService(str(db_path))
    book = catalog.get_or_create_book(
        BookMatchInput(title="A Book", provider="test", provider_book_id="1")
    )
    source_root, storage_root = tmp_path / "source", tmp_path / "storage"
    source_root.mkdir()
    storage_root.mkdir()
    source = source_root / "book.epub"
    source.write_bytes(b"central bytes")
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT INTO catalog_deletion_operations(id, book_id, plan_token, actor_user_id, state, plan_json)
               VALUES(?,?,?,?, 'cleanup_pending', '{}')""",
            (uuid.uuid4().hex, book.book_id, "t" * 64, actor["id"]),
        )
    with pytest.raises(Exception, match="being deleted"):
        catalog.publish_and_register_imported_file(
            book_id=book.book_id,
            file_store=FileStore(storage_root),
            source_path=source,
            source_roots=(source_root,),
        )
    assert list(storage_root.rglob("*.epub")) == []


def test_central_publish_register_does_not_unlink_object_when_lease_is_busy(tmp_path: Path) -> None:
    db_path = tmp_path / "users.db"
    UserDB(str(db_path)).initialize()
    catalog = CatalogService(str(db_path))
    book = catalog.get_or_create_book(
        BookMatchInput(title="A Book", provider="test", provider_book_id="1")
    )
    source_root, storage_root = tmp_path / "source", tmp_path / "storage"
    source_root.mkdir()
    storage_root.mkdir()
    source = source_root / "book.epub"
    source.write_bytes(b"central bytes")
    store = FileStore(storage_root)
    published = store.publish(source, source_roots=(source_root,))
    held = CatalogStorageLease(
        str(db_path), catalog_path_lease_key(str(published.path), published.sha256), "other-worker"
    )
    held.acquire()
    try:
        with pytest.raises(Exception, match="busy"):
            catalog.publish_and_register_imported_file(
                book_id=book.book_id,
                file_store=store,
                source_path=source,
                source_roots=(source_root,),
            )
        assert published.path.exists()
    finally:
        held.release()
