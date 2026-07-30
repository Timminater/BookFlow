"""Contract and ownership tests for the isolated v1 API Blueprint."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from flask import Flask

from bookflow.core.user_db import UserDB
from bookflow.core.v1_api import V1Actor, register_v1_api


@pytest.fixture
def client(tmp_path: Path):
    db_path = tmp_path / "v1.sqlite"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, is_active INTEGER, role TEXT);
            CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, author TEXT, language TEXT,
                publish_year INTEGER, cover_url TEXT, isbn_13 TEXT, catalog_visible INTEGER);
            CREATE TABLE book_identifiers (
                book_id INTEGER, identifier_type TEXT, identifier TEXT
            );
            CREATE TABLE book_files (id INTEGER PRIMARY KEY, book_id INTEGER, file_format TEXT, size_bytes INTEGER,
                mime_type TEXT, imported_at TEXT, import_status TEXT,
                content_type TEXT NOT NULL DEFAULT 'ebook');
            CREATE TABLE user_file_entitlements (
                user_id INTEGER, file_id INTEGER, granted_at TEXT,
                grant_source TEXT NOT NULL DEFAULT 'direct',
                UNIQUE (user_id, file_id)
            );
            CREATE TABLE user_library (
                user_id INTEGER, book_id INTEGER, added_at TEXT,
                UNIQUE (user_id, book_id)
            );
            CREATE TABLE user_kobo_books (
                user_id INTEGER, book_id INTEGER, desired_state TEXT, revision INTEGER
            );
            CREATE TABLE user_reading_progress (user_id INTEGER, book_id INTEGER, progress REAL,
                active INTEGER, source TEXT, updated_at TEXT);
            CREATE TABLE requests (id INTEGER PRIMARY KEY, user_id INTEGER, book_id INTEGER, request_process_id INTEGER,
                content_type TEXT, profile_hash TEXT, profile_snapshot_json TEXT, status TEXT, note TEXT,
                add_to_kobo_when_available INTEGER, updated_at TEXT, created_at TEXT);
            CREATE TABLE request_processes (id INTEGER PRIMARY KEY, status TEXT, monitoring_enabled INTEGER,
                next_search_at TEXT, updated_at TEXT);
            CREATE TABLE release_candidates (id INTEGER PRIMARY KEY, request_process_id INTEGER, total_score INTEGER);
            CREATE TABLE request_attempts (id INTEGER PRIMARY KEY, request_process_id INTEGER);
            CREATE TABLE blocked_releases (id INTEGER PRIMARY KEY, unblocked_at TEXT);
            INSERT INTO users VALUES (1, 'reader-one', 1, 'user'),
                (2, 'reader-two', 1, 'user'), (3, 'admin', 1, 'admin');
            INSERT INTO books VALUES (10, 'Allowed', 'Author', 'en', 2020, NULL, NULL, 1),
                (11, 'Private', 'Other', 'en', 2021, NULL, NULL, 1),
                (12, 'Entitled only', 'Other', 'en', 2022, NULL, NULL, 1),
                (13, 'Requested only', 'Other', 'en', 2023, NULL, NULL, 1),
                (14, 'Trending only', 'Other', 'en', 2024, NULL, NULL, 0);
            INSERT INTO book_files VALUES (100, 10, 'epub', 42, 'application/epub+zip', '2026-01-01', 'imported', 'ebook');
            INSERT INTO book_files VALUES (101, 11, 'epub', 43, 'application/epub+zip', '2026-01-01', 'imported', 'ebook');
            INSERT INTO book_files VALUES (102, 12, 'epub', 44, 'application/epub+zip', '2026-01-01', 'imported', 'ebook');
            INSERT INTO user_file_entitlements VALUES (1, 100, '2026-01-01', 'direct');
            INSERT INTO user_file_entitlements VALUES (2, 101, '2026-01-01', 'direct');
            INSERT INTO user_file_entitlements VALUES (1, 102, '2026-01-01', 'direct');
            INSERT INTO user_library VALUES (1, 10, '2026-01-02');
            INSERT INTO user_kobo_books VALUES (1, 10, 'active', 1);
            INSERT INTO user_reading_progress VALUES (1, 10, 42.5, 1, 'web', '2026-01-03');
            INSERT INTO requests VALUES (50, 1, 10, NULL, 'ebook', 'x', '{}', 'pending_approval', NULL, 0, '2026-01-01', '2026-01-01');
            INSERT INTO requests VALUES (51, 1, 13, NULL, 'ebook', 'x', '{}', 'pending_approval', NULL, 0, '2026-01-01', '2026-01-01');
            INSERT INTO request_processes VALUES (70, 'waiting_for_release', 1, NULL, '2026-01-01');
            """
        )
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["V1_TEST_DATABASE"] = str(db_path)
    app.config["V1_TEST_DB_PATH"] = db_path
    actor = V1Actor(1)
    register_v1_api(app, database_path=db_path, resolve_actor=lambda: actor)
    return app.test_client()


def test_library_contract_and_idor_protection(client) -> None:
    library = client.get("/api/v1/library/books")
    assert library.status_code == 200
    assert [item["id"] for item in library.json["items"]] == [10]

    allowed = client.get("/api/v1/library/books/10")
    assert allowed.status_code == 200
    assert allowed.json["entitled"] is True
    assert allowed.json["files"][0]["file_format"] == "epub"

    entitled_detail = client.get("/api/v1/library/books/12")
    assert entitled_detail.status_code == 200

    requested_detail = client.get("/api/v1/library/books/13")
    assert requested_detail.status_code == 200

    denied = client.get("/api/v1/library/books/11")
    assert denied.status_code == 404
    assert denied.json["error"]["code"] == "book_not_found"
    assert denied.json["error"]["correlation_id"]

    catalog = client.get("/api/v1/catalog/books")
    assert catalog.status_code == 200
    membership = {item["id"]: item["in_library"] for item in catalog.json["items"]}
    assert membership[10] is True
    assert membership[11] is False


def test_trending_only_book_detail_does_not_promote_catalog_visibility(client) -> None:
    before = client.get("/api/v1/catalog/books")
    assert before.status_code == 200
    assert 14 not in {item["id"] for item in before.json["items"]}

    detail = client.get("/api/v1/catalog/books/14")
    assert detail.status_code == 200
    assert detail.json["book"]["id"] == 14

    after = client.get("/api/v1/catalog/books")
    assert after.status_code == 200
    assert 14 not in {item["id"] for item in after.json["items"]}


def test_local_search_includes_hidden_metadata_and_keeps_catalog_unchanged(client) -> None:
    """Autocomplete is FTS-only and never turns a suggestion into a catalog card."""
    with sqlite3.connect(client.application.config["V1_TEST_DATABASE"]) as conn:
        conn.execute(
            "CREATE VIRTUAL TABLE books_fts USING fts5(book_id UNINDEXED, title, author, isbn_13)"
        )
        conn.executemany(
            "INSERT INTO books_fts(rowid, book_id, title, author, isbn_13) VALUES (?, ?, ?, ?, ?)",
            [
                (10, 10, "Dune", "Frank Herbert", "9780441172719"),
                (13, 13, "Dune Messiah", "Frank Herbert", "9780441172696"),
                (14, 14, "Dúné Trending", "Other", "9780000000014"),
            ],
        )
        conn.execute(
            "UPDATE books SET title = 'Dúné Trending', isbn_13 = '9780000000014' WHERE id = 14"
        )

    response = client.get("/api/v1/search/books?q=Dune&limit=2")
    assert response.status_code == 200
    assert response.json["limit"] == 2
    assert [item["id"] for item in response.json["items"]] == [10, 14]

    hidden = client.get("/api/v1/search/books?q=Trending")
    assert hidden.status_code == 200
    assert hidden.json["items"][0]["id"] == 14
    assert hidden.json["items"][0]["catalog_visible"] is False
    assert hidden.json["items"][0]["has_imported_file"] is False
    assert 14 not in {item["id"] for item in client.get("/api/v1/catalog/books").json["items"]}


def test_local_search_requires_authentication_and_validates_limit(tmp_path: Path) -> None:
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_v1_api(
        app, database_path=tmp_path / "unauthenticated.sqlite", resolve_actor=lambda: None
    )
    assert app.test_client().get("/api/v1/search/books?q=dune").status_code == 401


def test_admin_requester_names_are_complete_and_private(client) -> None:
    """Only the admin list identifies every requester, including safe orphan fallback."""
    db_path = client.application.config["V1_TEST_DB_PATH"]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO requests VALUES (52, 2, 10, NULL, 'ebook', 'x', '{}', "
            "'pending_approval', NULL, 0, '2026-01-02', '2026-01-02')"
        )
        # Historic data can outlive a deleted/anonymised user; it must not vanish from admin.
        conn.execute(
            "INSERT INTO requests VALUES (53, 99, 10, NULL, 'ebook', 'x', '{}', "
            "'pending_approval', NULL, 0, '2026-01-03', '2026-01-03')"
        )

    admin_app = Flask(__name__)
    admin_app.config["TESTING"] = True
    register_v1_api(
        admin_app,
        database_path=db_path,
        resolve_actor=lambda: V1Actor(3, is_admin=True),
    )
    admin_response = admin_app.test_client().get("/api/v1/admin/requests?limit=100")

    assert admin_response.status_code == 200
    requester_names = {item["id"]: item["requester_name"] for item in admin_response.json["items"]}
    assert requester_names[50] == "reader-one"
    assert requester_names[52] == "reader-two"
    assert requester_names[53] is None

    # The same non-admin actor cannot obtain the admin-only requester response.
    assert client.get("/api/v1/admin/requests").status_code == 403


def test_bulk_add_library_books_is_idempotent_and_reports_partial_results(client) -> None:
    response = client.post(
        "/api/v1/library/books/bulk",
        json={"book_ids": [11, 11, 10, 13, 999]},
    )

    assert response.status_code == 200
    assert response.json["summary"] == {
        "submitted": 4,
        "added": 1,
        "already_in_library": 1,
        "failed": 2,
    }
    assert [(item["book_id"], item["status"]) for item in response.json["results"]] == [
        (11, "added"),
        (10, "already_in_library"),
        (13, "unavailable"),
        (999, "not_found"),
    ]

    second = client.post("/api/v1/library/books/bulk", json={"book_ids": [11]})
    assert second.status_code == 200
    assert second.json["results"][0]["status"] == "already_in_library"


def test_bulk_add_library_books_requires_authentication(tmp_path: Path) -> None:
    app = Flask(__name__)
    app.config["TESTING"] = True
    register_v1_api(
        app,
        database_path=tmp_path / "unauthenticated.sqlite",
        resolve_actor=lambda: None,
    )

    response = app.test_client().post(
        "/api/v1/library/books/bulk",
        json={"book_ids": [10]},
    )

    assert response.status_code == 401
    assert response.json["error"]["code"] == "unauthorized"


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        {"book_ids": []},
        {"book_ids": [0]},
        {"book_ids": [True]},
        {"book_ids": ["10"]},
        {"book_ids": list(range(1, 102))},
    ],
)
def test_bulk_add_library_books_validates_payload(client, payload) -> None:
    response = client.post("/api/v1/library/books/bulk", json=payload)

    assert response.status_code == 400
    assert response.json["error"]["code"] == "invalid_book_ids"


def test_library_kobo_shelf_only_returns_active_personal_kobo_books(client) -> None:
    shelf = client.get("/api/v1/library/books?shelf=kobo")
    assert shelf.status_code == 200
    assert [book["id"] for book in shelf.json["items"]] == [10]
    assert shelf.json["items"][0]["kobo_state"] == "active"

    invalid = client.get("/api/v1/library/books?shelf=shared")
    assert invalid.status_code == 400
    assert invalid.json["error"]["code"] == "invalid_library_query"


def test_catalog_presence_checks_complete_local_set_by_provider_and_isbn(tmp_path: Path) -> None:
    db_path = tmp_path / "catalog-presence.sqlite"
    db = UserDB(str(db_path))
    db.initialize()
    user = db.create_user(username="reader")
    with sqlite3.connect(db_path) as conn:
        direct_provider = conn.execute(
            """
            INSERT INTO books (metadata_provider, provider_book_id, title)
            VALUES ('hardcover', 'direct-provider', 'Direct provider match')
            """
        ).lastrowid
        identifier_provider = conn.execute(
            """
            INSERT INTO books (metadata_provider, provider_book_id, title)
            VALUES ('legacy', 'legacy-primary', 'Provider identifier match')
            """
        ).lastrowid
        identifier_isbn = conn.execute(
            """
            INSERT INTO books (metadata_provider, provider_book_id, title)
            VALUES ('legacy', 'isbn-primary', 'ISBN identifier match')
            """
        ).lastrowid
        legacy_isbn = conn.execute(
            """
            INSERT INTO books (metadata_provider, provider_book_id, title, isbn_13)
            VALUES ('legacy', 'isbn-column', 'Legacy ISBN column match', '9780306406157')
            """
        ).lastrowid
        metadata_only = conn.execute(
            """
            INSERT INTO books (metadata_provider, provider_book_id, title)
            VALUES ('hardcover', 'metadata-only', 'Not in downloaded catalog')
            """
        ).lastrowid
        conn.execute(
            """
            INSERT INTO book_provider_identifiers (book_id, provider, provider_book_id)
            VALUES (?, 'openlibrary', 'alternate-provider')
            """,
            (identifier_provider,),
        )
        conn.execute(
            """
            INSERT INTO book_identifiers (book_id, identifier_type, identifier, source, confidence)
            VALUES (?, 'isbn13', '9789026175220', 'test', 1.0)
            """,
            (identifier_isbn,),
        )
        conn.executemany(
            """
            INSERT INTO book_files (
                book_id, storage_path, file_format, size_bytes, sha256,
                import_status, content_type
            ) VALUES (?, ?, 'epub', 1, ?, 'imported', 'ebook')
            """,
            [
                (book_id, str(tmp_path / f"{book_id}.epub"), f"{book_id:064x}")
                for book_id in (
                    direct_provider,
                    identifier_provider,
                    identifier_isbn,
                    legacy_isbn,
                )
            ],
        )
        assert all(
            value is not None
            for value in (
                direct_provider,
                identifier_provider,
                identifier_isbn,
                legacy_isbn,
                metadata_only,
            )
        )

    app = Flask(__name__)
    app.config["TESTING"] = True
    register_v1_api(
        app,
        database_path=db_path,
        resolve_actor=lambda: V1Actor(user["id"]),
    )

    response = app.test_client().post(
        "/api/v1/catalog/presence",
        json={
            "candidates": [
                {"provider": "HardCover", "provider_book_id": "direct-provider"},
                {"provider": "openlibrary", "provider_book_id": "alternate-provider"},
                {"isbn": "978-902-617-5220"},
                {"isbn": "978-0-306-40615-7"},
                {"provider": "hardcover", "provider_book_id": "metadata-only"},
                {"provider": "missing", "provider_book_id": "not-here", "isbn": "9781526622433"},
            ]
        },
    )

    assert response.status_code == 200
    assert response.json == {"present": [0, 1, 2, 3]}


def test_catalog_book_detail_includes_actor_scoped_personal_status(client) -> None:
    response = client.get("/api/v1/catalog/books/10")

    assert response.status_code == 200
    assert response.json["request"] == {
        "id": 50,
        "status": "pending_approval",
        "add_to_kobo_when_available": 0,
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
    }
    assert response.json["personal_status"] == {
        "library_added_at": "2026-01-02",
        "reading_progress": {
            "progress": 42.5,
            "active": 1,
            "source": "web",
            "updated_at": "2026-01-03",
        },
        "available_formats": ["epub"],
        "file_count": 1,
        "kobo_state": "active",
    }


def test_catalog_book_detail_personal_status_isolated_and_empty(tmp_path: Path) -> None:
    db_path = tmp_path / "empty-personal-status.sqlite"
    db = UserDB(str(db_path))
    db.initialize()
    owner = db.create_user(username="owner")
    other = db.create_user(username="other")
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO books (metadata_provider, provider_book_id, title)
            VALUES ('test', 'personal-status', 'Personal status')
            """
        )
        book_id = int(cursor.lastrowid)
        conn.execute(
            "INSERT INTO user_library (user_id, book_id, added_at) VALUES (?, ?, '2026-02-01')",
            (owner["id"], book_id),
        )
        conn.execute(
            """
            INSERT INTO user_reading_progress (user_id, book_id, progress, active, source, revision, updated_at)
            VALUES (?, ?, 18.0, 1, 'web', 1, '2026-02-02')
            """,
            (owner["id"], book_id),
        )
        file_id = conn.execute(
            """
            INSERT INTO book_files (book_id, storage_path, file_format, size_bytes, sha256, import_status)
            VALUES (?, 'book.epub', 'epub', 1,
                    '0000000000000000000000000000000000000000000000000000000000000000', 'imported')
            """,
            (book_id,),
        ).lastrowid
        conn.execute(
            "INSERT INTO user_file_entitlements (user_id, file_id) VALUES (?, ?)",
            (owner["id"], file_id),
        )
        conn.execute(
            """
            INSERT INTO user_kobo_books (user_id, book_id, desired_state, source, revision)
            VALUES (?, ?, 'active', 'web', 1)
            """,
            (owner["id"], book_id),
        )

    app = Flask(__name__)
    app.config["TESTING"] = True
    register_v1_api(
        app,
        database_path=db_path,
        resolve_actor=lambda: V1Actor(other["id"]),
    )
    response = app.test_client().get(f"/api/v1/catalog/books/{book_id}")

    assert response.status_code == 200
    assert response.json["personal_status"] == {
        "library_added_at": None,
        "reading_progress": None,
        "available_formats": [],
        "file_count": 0,
        "kobo_state": None,
    }


def test_request_cancel_is_owner_scoped_and_terminal_conflicts(client) -> None:
    cancelled = client.post("/api/v1/requests/50/cancel", headers={"Idempotency-Key": "cancel-50"})
    assert cancelled.status_code == 202
    assert cancelled.json["request"]["status"] == "cancelled"

    replay = client.post("/api/v1/requests/50/cancel")
    assert replay.status_code == 200
    assert replay.json["request"]["status"] == "cancelled"

    missing = client.post("/api/v1/requests/999/cancel")
    assert missing.status_code == 404
    assert missing.json["error"]["code"] == "request_not_found"


def test_admin_request_process_cancel_also_cancels_active_requests(tmp_path: Path) -> None:
    db_path = tmp_path / "admin-cancel.sqlite"
    db = UserDB(str(db_path))
    db.initialize()
    admin = db.create_user(username="admin", role="admin")
    reader = db.create_user(username="reader")
    with sqlite3.connect(db_path) as conn:
        book_id = conn.execute(
            """
            INSERT INTO books (metadata_provider, provider_book_id, title)
            VALUES ('test', 'cancel-me', 'Cancel Me')
            """
        ).lastrowid
        profile_id = conn.execute(
            "SELECT id FROM release_profiles WHERE is_default = 1"
        ).fetchone()[0]
    assert book_id is not None
    service_client = Flask(__name__)
    service_client.config["TESTING"] = True
    register_v1_api(
        service_client,
        database_path=db_path,
        resolve_actor=lambda: V1Actor(reader["id"]),
    )
    created = service_client.test_client().post(
        "/api/v1/requests",
        json={"book_id": book_id, "profile_id": profile_id},
    )
    request_id = created.json["request"]["id"]

    admin_app = Flask(__name__)
    admin_app.config["TESTING"] = True
    register_v1_api(
        admin_app,
        database_path=db_path,
        resolve_actor=lambda: V1Actor(admin["id"], is_admin=True),
    )
    approved = admin_app.test_client().post(f"/api/v1/admin/requests/{request_id}/approve")
    process_id = approved.json["request_process"]["id"]

    cancelled = admin_app.test_client().post(f"/api/v1/admin/request-processes/{process_id}/cancel")

    assert cancelled.status_code == 202
    assert cancelled.json == {
        "request_process_id": process_id,
        "status": "cancelled",
        "request_ids": [request_id],
    }
    with sqlite3.connect(db_path) as conn:
        assert (
            conn.execute("SELECT status FROM requests WHERE id = ?", (request_id,)).fetchone()[0]
            == "cancelled"
        )


def test_openapi_and_admin_access_use_consistent_contract(client) -> None:
    spec = client.get("/api/v1/openapi.json")
    assert spec.status_code == 200
    assert spec.json["openapi"] == "3.1.0"
    assert "/api/kobo/{token}/v1/initialization" in spec.json["paths"]
    assert "/api/kobo/{token}/v1/library/sync" in spec.json["paths"]
    for path_item in spec.json["paths"].values():
        for method, operation in path_item.items():
            if method != "parameters":
                assert operation["responses"]

    denied = client.get("/api/v1/admin/request-processes")
    assert denied.status_code == 403
    assert denied.json["error"] == {
        "code": "forbidden",
        "message": "Administrator access required",
        "details": None,
        "correlation_id": denied.json["error"]["correlation_id"],
    }


def test_create_request_idempotency_survives_app_reregistration(tmp_path: Path) -> None:
    db_path = tmp_path / "users.db"
    db = UserDB(str(db_path))
    db.initialize()
    user = db.create_user(username="reader")
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO books (metadata_provider, provider_book_id, title)
            VALUES ('test', 'dune', 'Dune')
            """
        )
        book_id = cursor.lastrowid
        profile_id = conn.execute(
            "SELECT id FROM release_profiles WHERE is_default = 1"
        ).fetchone()[0]
    assert book_id is not None

    def make_client():
        app = Flask(__name__)
        app.config["TESTING"] = True
        register_v1_api(
            app,
            database_path=db_path,
            resolve_actor=lambda: V1Actor(user["id"]),
        )
        return app.test_client()

    payload = {"book_id": book_id, "profile_id": profile_id}
    first = make_client().post(
        "/api/v1/requests",
        json=payload,
        headers={"Idempotency-Key": "request-dune"},
    )
    replay = make_client().post(
        "/api/v1/requests",
        json=payload,
        headers={"Idempotency-Key": "request-dune"},
    )
    conflict = make_client().post(
        "/api/v1/requests",
        json={"book_id": book_id, "profile_id": profile_id, "note": "different payload"},
        headers={"Idempotency-Key": "request-dune"},
    )
    missing_profile = make_client().post("/api/v1/requests", json={"book_id": book_id})

    assert first.status_code == replay.status_code == 202
    assert first.json == replay.json
    assert conflict.status_code == 409
    assert conflict.json["error"]["code"] == "idempotency_conflict"
    assert missing_profile.status_code == 400
    assert missing_profile.json["error"]["code"] == "invalid_profile"


def test_catalog_book_can_be_resolved_by_any_linked_isbn(tmp_path: Path) -> None:
    db_path = tmp_path / "isbn-catalog.sqlite"
    db = UserDB(str(db_path))
    db.initialize()
    user = db.create_user(username="reader")
    with sqlite3.connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO books (metadata_provider, provider_book_id, title, isbn_13)
            VALUES ('test', 'edition-1', 'ISBN Book', '9789026175220')
            """
        )
        book_id = int(cursor.lastrowid)
        conn.executemany(
            """
            INSERT INTO book_identifiers (
                book_id, identifier_type, identifier, source, confidence
            ) VALUES (?, ?, ?, 'test', 1.0)
            """,
            (
                (book_id, "isbn13", "9789026175220"),
                (book_id, "isbn13", "9789026175237"),
            ),
        )

    app = Flask(__name__)
    app.config["TESTING"] = True
    register_v1_api(
        app,
        database_path=db_path,
        resolve_actor=lambda: V1Actor(user["id"]),
    )
    client = app.test_client()

    primary = client.get("/api/v1/catalog/books/isbn/9789026175220")
    alternate = client.get("/api/v1/catalog/books/isbn/9789026175237")

    assert primary.status_code == alternate.status_code == 200
    assert primary.json["book"]["id"] == alternate.json["book"]["id"] == book_id


def test_catalog_isbn_groups_legacy_duplicates_and_aggregates_availability(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "duplicate-isbn.sqlite"
    db = UserDB(str(db_path))
    db.initialize()
    user = db.create_user(username="reader")
    with sqlite3.connect(db_path) as conn:
        first_id = int(
            conn.execute(
                """
                INSERT INTO books (metadata_provider, provider_book_id, title, isbn_13)
                VALUES ('one', 'edition-1', 'Canonical title', '9789026175220')
                """
            ).lastrowid
        )
        duplicate_id = int(
            conn.execute(
                """
                INSERT INTO books (metadata_provider, provider_book_id, title, isbn_13)
                VALUES ('two', 'edition-2', 'Duplicate title', '9789026175220')
                """
            ).lastrowid
        )
        for candidate_id, source in ((first_id, "one"), (duplicate_id, "two")):
            conn.execute(
                """
                INSERT INTO book_identifiers (
                    book_id, identifier_type, identifier, source, confidence
                ) VALUES (?, 'isbn13', '9789026175220', ?, 1.0)
                """,
                (candidate_id, source),
            )
        first_file = int(
            conn.execute(
                """
                INSERT INTO book_files (
                    book_id, storage_path, file_format, size_bytes, sha256, import_status
                ) VALUES (?, ?, 'epub', 1, ?, 'imported')
                """,
                (first_id, str(tmp_path / "one.epub"), "a" * 64),
            ).lastrowid
        )
        duplicate_file = int(
            conn.execute(
                """
                INSERT INTO book_files (
                    book_id, storage_path, file_format, size_bytes, sha256, import_status
                ) VALUES (?, ?, 'pdf', 1, ?, 'imported')
                """,
                (duplicate_id, str(tmp_path / "two.pdf"), "b" * 64),
            ).lastrowid
        )
        conn.execute(
            "INSERT INTO user_library(user_id, book_id) VALUES (?, ?)",
            (user["id"], duplicate_id),
        )
        conn.executemany(
            "INSERT INTO user_file_entitlements(user_id, file_id) VALUES (?, ?)",
            ((user["id"], first_file), (user["id"], duplicate_file)),
        )

    app = Flask(__name__)
    app.config["TESTING"] = True
    register_v1_api(
        app,
        database_path=db_path,
        resolve_actor=lambda: V1Actor(user["id"]),
    )
    response = app.test_client().get("/api/v1/catalog/books/isbn/9789026175220")

    assert response.status_code == 200
    assert response.json["book"]["id"] == first_id
    assert response.json["in_library"] is True
    assert response.json["delivery_state"] == "available"
    assert response.json["available_formats"] == ["epub", "pdf"]
    assert response.json["personal_status"]["available_formats"] == ["epub", "pdf"]
    assert response.json["personal_status"]["file_count"] == 2
