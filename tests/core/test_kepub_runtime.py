"""On-demand KEPUB registration, sharing, entitlement, and Kobo wiring tests."""

from __future__ import annotations

import hashlib
import sqlite3
import zipfile
from pathlib import Path

from flask import Flask

from bookflow.core.catalog_service import CatalogService
from bookflow.core.file_store import FileStore
from bookflow.core.kepub_converter import (
    EPUB_MIME_TYPE,
    KepubCatalogService,
    KepubConversionResult,
    kepub_options_hash,
)
from bookflow.core.kobo_services import KoboSyncService, TokenService
from bookflow.core.kobo_settings_routes import register_kobo_settings_routes
from bookflow.core.user_db import UserDB
from bookflow.core.v1_api import V1Actor


def _write_epub(path: Path, chapter: str) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", EPUB_MIME_TYPE)
        archive.writestr("META-INF/container.xml", "<container />")
        archive.writestr("OEBPS/content.opf", "<package />")
        archive.writestr("OEBPS/chapter.xhtml", chapter)


class _FakeConverter:
    """A converter double that keeps runtime service tests independent of kepubify."""

    converter_version = "kepubify test"

    def __init__(self, cache_directory: Path) -> None:
        self.cache_directory = cache_directory
        self.cache_directory.mkdir()
        self.calls = 0

    def convert(
        self,
        source_path: Path,
        *,
        options: dict[str, str | int | bool] | None = None,
        expected_source_sha256: str | None = None,
    ) -> KepubConversionResult:
        del options
        self.calls += 1
        source_sha256 = hashlib.sha256(source_path.read_bytes()).hexdigest()
        assert expected_source_sha256 == source_sha256
        options_hash = kepub_options_hash()
        cache_key = hashlib.sha256(
            f"{source_sha256}\0kepubify test\0{options_hash}".encode()
        ).hexdigest()
        output_path = self.cache_directory / f"{cache_key}.kepub.epub"
        _write_epub(output_path, "converted kepub")
        return KepubConversionResult(
            output_path=output_path,
            source_sha256=source_sha256,
            converter_version=self.converter_version,
            options_hash=options_hash,
            cache_key=cache_key,
            cache_hit=False,
        )


def _runtime_fixture(tmp_path: Path) -> dict[str, object]:
    db_path = tmp_path / "users.db"
    users = UserDB(str(db_path))
    users.initialize()
    first_user = users.create_user(username="first-reader")
    second_user = users.create_user(username="second-reader")
    storage = tmp_path / "storage"
    storage.mkdir()
    source_path = tmp_path / "source.epub"
    _write_epub(source_path, "source")
    catalog = CatalogService(str(db_path))
    published_source = FileStore(storage).publish(source_path, source_roots=(tmp_path,))
    conn = users._connect()
    try:
        book_id = conn.execute(
            """
            INSERT INTO books (metadata_provider, provider_book_id, title, author)
            VALUES ('test', 'kepub-runtime', 'Runtime KEPUB', 'BookFlow')
            """
        ).lastrowid
        assert book_id is not None
        conn.commit()
    finally:
        conn.close()
    source = catalog.register_imported_file(book_id=int(book_id), published_file=published_source)
    for user in (first_user, second_user):
        catalog.grant_file_entitlement(user_id=int(user["id"]), file_id=int(source["id"]))
    fake_converter = _FakeConverter(tmp_path / "cache")
    service = KepubCatalogService(fake_converter, FileStore(storage), catalog)  # type: ignore[arg-type]
    tokens = TokenService(str(db_path), pepper="kepub-test-token-pepper")
    sync = KoboSyncService(str(db_path), cursor_secret="kepub-test-cursor-secret")
    tokens.initialize()
    return {
        "catalog": catalog,
        "db_path": db_path,
        "first_user": first_user,
        "second_user": second_user,
        "book_id": int(book_id),
        "source_id": int(source["id"]),
        "converter": fake_converter,
        "service": service,
        "sync": sync,
        "tokens": tokens,
    }


def test_on_demand_kepub_is_db_cached_shared_and_entitled_for_each_user(tmp_path: Path) -> None:
    fixture = _runtime_fixture(tmp_path)
    service: KepubCatalogService = fixture["service"]  # type: ignore[assignment]
    converter: _FakeConverter = fixture["converter"]  # type: ignore[assignment]
    first = service.convert_entitled_for_user(
        user_id=int(fixture["first_user"]["id"]),
        book_id=int(fixture["book_id"]),
        source_file_id=int(fixture["source_id"]),
    )
    second = service.convert_entitled_for_user(
        user_id=int(fixture["second_user"]["id"]),
        book_id=int(fixture["book_id"]),
        source_file_id=int(fixture["source_id"]),
    )

    assert first == second
    assert converter.calls == 1
    conn = sqlite3.connect(fixture["db_path"])
    try:
        cache_rows = conn.execute("SELECT output_file_id FROM kepub_conversion_cache").fetchall()
        entitlement_rows = conn.execute(
            "SELECT user_id FROM user_file_entitlements WHERE file_id = ? ORDER BY user_id",
            (first,),
        ).fetchall()
        source_rows = conn.execute(
            "SELECT id FROM book_files WHERE file_format = 'epub'"
        ).fetchall()
    finally:
        conn.close()
    assert cache_rows == [(first,)]
    assert [row[0] for row in entitlement_rows] == [
        fixture["first_user"]["id"],
        fixture["second_user"]["id"],
    ]
    assert source_rows == [(fixture["source_id"],)]


def test_kobo_kepub_callback_replaces_file_id_and_reports_conversion_errors(tmp_path: Path) -> None:
    fixture = _runtime_fixture(tmp_path)
    service: KepubCatalogService = fixture["service"]  # type: ignore[assignment]
    user_id = int(fixture["first_user"]["id"])
    book_id = int(fixture["book_id"])
    source_id = int(fixture["source_id"])
    derived_id = service.convert_entitled_for_user(
        user_id=user_id, book_id=book_id, source_file_id=source_id
    )
    app = Flask(__name__)
    app.config["TESTING"] = True
    actor = {"value": V1Actor(user_id)}
    calls: list[tuple[int, int, int]] = []

    def convert(*, user_id: int, book_id: int, source_file_id: int) -> int:
        calls.append((user_id, book_id, source_file_id))
        return derived_id

    register_kobo_settings_routes(
        app,
        db_path=str(fixture["db_path"]),
        resolve_actor=lambda: actor["value"],
        token_service=fixture["tokens"],  # type: ignore[arg-type]
        sync_service=fixture["sync"],  # type: ignore[arg-type]
        kepub_converter=convert,
    )
    client = app.test_client()
    added = client.put(
        f"/api/v1/users/me/kobo-books/{book_id}",
        json={"file_id": source_id, "preferred_format": "kepub"},
    )
    conn = sqlite3.connect(fixture["db_path"])
    try:
        stored_file_id = conn.execute(
            "SELECT file_id FROM user_kobo_books WHERE user_id = ? AND book_id = ?",
            (user_id, book_id),
        ).fetchone()[0]
    finally:
        conn.close()
    assert added.status_code == 202
    assert calls == [(user_id, book_id, source_id)]
    assert stored_file_id == derived_id

    failing_app = Flask(__name__)
    failing_app.config["TESTING"] = True
    register_kobo_settings_routes(
        failing_app,
        db_path=str(fixture["db_path"]),
        resolve_actor=lambda: actor["value"],
        token_service=fixture["tokens"],  # type: ignore[arg-type]
        sync_service=fixture["sync"],  # type: ignore[arg-type]
        kepub_converter=lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    failed = failing_app.test_client().put(
        f"/api/v1/users/me/kobo-books/{book_id}",
        json={"file_id": source_id, "preferred_format": "kepub"},
    )
    assert failed.status_code == 503
    assert failed.json["error"]["code"] == "kepub_conversion_failed"
