"""Transactional catalog and central-file records for the canonical Book slice."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from bookflow.core.book_matcher import (
    BookMatch,
    BookMatchCandidate,
    BookMatcher,
    BookMatchInput,
    normalize_isbn,
)
from bookflow.core.catalog_storage_lease import (
    CatalogStorageLease,
    catalog_book_lease_key,
    catalog_path_lease_key,
)

_RETIRED_AUDIO_FILE_FORMATS = frozenset(
    {"aac", "flac", "m4a", "m4b", "mp3", "ogg", "opus", "wav", "wma"}
)

if TYPE_CHECKING:
    from bookflow.core.file_store import FileStore, PublishedFile
    from bookflow.core.path_security import PathInput


class CatalogServiceError(RuntimeError):
    """Raised when a catalog invariant cannot be maintained."""


def _raise_book_deleting() -> None:
    raise CatalogServiceError("Book is being deleted")


@dataclass(frozen=True)
class CanonicalBook:
    """Result of a conservative canonical Book get-or-create operation."""

    book_id: int
    match_method: str
    match_confidence: float
    created: bool


class CatalogService:
    """Own canonical Book identifiers, file records and user entitlements."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        self._matcher = BookMatcher()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @staticmethod
    def _now() -> str:
        return datetime.now(UTC).isoformat(timespec="seconds")

    @staticmethod
    def _required_text(value: str, field: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{field} is required")
        return normalized

    @staticmethod
    def _optional_text(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @staticmethod
    def _file_content_type(*, content_type: str | None, file_format: str) -> str:
        """Reject retired audio artifacts and return the sole supported content type."""
        normalized = (content_type or "ebook").strip().casefold()
        if normalized != "ebook" or file_format.casefold() in _RETIRED_AUDIO_FILE_FORMATS:
            raise ValueError("Audiobooks are no longer supported")
        return "ebook"

    def get_or_create_book(self, incoming: BookMatchInput) -> CanonicalBook:
        """Find one unambiguous Book or create a new local canonical row.

        The transaction deliberately uses ``BEGIN IMMEDIATE``: concurrent adds
        can either reuse the same canonical Book or observe a unique conflict,
        never create two provider links for the same provider identifier.
        """
        title = self._required_text(incoming.title, "title")
        provider = self._optional_text(incoming.provider)
        provider_book_id = self._optional_text(incoming.provider_book_id)
        if (provider is None) != (provider_book_id is None):
            raise ValueError("provider and provider_book_id must be supplied together")

        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                candidates = self._load_candidates(conn)
                match = self._matcher.match(incoming, candidates)
                if match is None:
                    book_id = self._insert_book(conn, title=title, incoming=incoming)
                    match = BookMatch(book_id=book_id, method="created", confidence=1.0)
                    created = True
                else:
                    book_id = match.book_id
                    created = False

                self._attach_provider_identifier(
                    conn,
                    book_id=book_id,
                    provider=provider,
                    provider_book_id=provider_book_id,
                )
                self._attach_isbn(conn, book_id=book_id, raw_isbn=incoming.isbn)
                self._record_match(conn, book_id=book_id, match=match, incoming=incoming)
                conn.commit()
                return CanonicalBook(
                    book_id=book_id,
                    match_method=match.method,
                    match_confidence=match.confidence,
                    created=created,
                )
            except sqlite3.Error as exc:
                conn.rollback()
                raise CatalogServiceError("Catalog transaction failed") from exc
            finally:
                conn.close()

    def _load_candidates(self, conn: sqlite3.Connection) -> list[BookMatchCandidate]:
        books = conn.execute(
            "SELECT id, title, author, language, publish_year FROM books ORDER BY id"
        ).fetchall()
        provider_rows = conn.execute(
            "SELECT book_id, provider, provider_book_id FROM book_provider_identifiers"
        ).fetchall()
        identifier_rows = conn.execute(
            "SELECT book_id, identifier_type, identifier FROM book_identifiers"
        ).fetchall()
        providers: dict[int, set[tuple[str, str]]] = {}
        for row in provider_rows:
            providers.setdefault(int(row["book_id"]), set()).add(
                (str(row["provider"]).strip().casefold(), str(row["provider_book_id"]).strip())
            )
        isbn10: dict[int, set[str]] = {}
        isbn13: dict[int, set[str]] = {}
        for row in identifier_rows:
            target = isbn10 if row["identifier_type"] == "isbn10" else isbn13
            if row["identifier_type"] in {"isbn10", "isbn13"}:
                target.setdefault(int(row["book_id"]), set()).add(str(row["identifier"]))
        return [
            BookMatchCandidate(
                book_id=int(row["id"]),
                title=str(row["title"]),
                author=row["author"],
                language=row["language"],
                publish_year=row["publish_year"],
                provider_identifiers=frozenset(providers.get(int(row["id"]), set())),
                isbn10=frozenset(isbn10.get(int(row["id"]), set())),
                isbn13=frozenset(isbn13.get(int(row["id"]), set())),
            )
            for row in books
        ]

    def _insert_book(
        self, conn: sqlite3.Connection, *, title: str, incoming: BookMatchInput
    ) -> int:
        provider = self._optional_text(incoming.provider) or "bookflow"
        provider_book_id = self._optional_text(incoming.provider_book_id) or f"local:{uuid.uuid4()}"
        normalized_isbn = normalize_isbn(incoming.isbn)
        cursor = conn.execute(
            """
            INSERT INTO books (
                metadata_provider, provider_book_id, title, author, publish_year,
                isbn_13, language, metadata_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?, ?)
            """,
            (
                provider,
                provider_book_id,
                title,
                self._optional_text(incoming.author),
                incoming.publish_year,
                normalized_isbn.isbn13 if normalized_isbn else None,
                self._optional_text(incoming.language),
                self._now(),
                self._now(),
            ),
        )
        if cursor.lastrowid is None:
            raise CatalogServiceError("Failed to create canonical Book")
        book_id = int(cursor.lastrowid)
        self._attach_provider_identifier(
            conn,
            book_id=book_id,
            provider=provider,
            provider_book_id=provider_book_id,
        )
        return book_id

    def _attach_provider_identifier(
        self,
        conn: sqlite3.Connection,
        *,
        book_id: int,
        provider: str | None,
        provider_book_id: str | None,
    ) -> None:
        if provider is None or provider_book_id is None:
            return
        normalized_provider = provider.casefold()
        conn.execute(
            """
            INSERT INTO book_provider_identifiers (book_id, provider, provider_book_id)
            VALUES (?, ?, ?)
            ON CONFLICT(provider, provider_book_id) DO NOTHING
            """,
            (book_id, normalized_provider, provider_book_id),
        )
        linked = conn.execute(
            """
            SELECT book_id FROM book_provider_identifiers
            WHERE provider = ? AND provider_book_id = ?
            """,
            (normalized_provider, provider_book_id),
        ).fetchone()
        if linked is None or int(linked["book_id"]) != book_id:
            raise CatalogServiceError("Provider identifier is already linked to another Book")

    def _attach_isbn(self, conn: sqlite3.Connection, *, book_id: int, raw_isbn: str | None) -> None:
        normalized = normalize_isbn(raw_isbn)
        if normalized is None:
            return
        identifiers = [
            ("isbn10", normalized.isbn10),
            ("isbn13", normalized.isbn13),
        ]
        for identifier_type, identifier in identifiers:
            if identifier is None:
                continue
            existing_rows = conn.execute(
                """
                SELECT DISTINCT book_id FROM book_identifiers
                WHERE identifier_type = ? AND identifier = ? AND book_id <> ?
                """,
                (identifier_type, identifier, book_id),
            ).fetchall()
            for existing in existing_rows:
                existing_book_id = int(existing["book_id"])
                conn.execute(
                    """
                    INSERT OR IGNORE INTO book_identifier_conflicts (
                        identifier_type, identifier, existing_book_id, proposed_book_id, source
                    ) VALUES (?, ?, ?, ?, 'catalog_service')
                    """,
                    (identifier_type, identifier, existing_book_id, book_id),
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO book_identifiers (
                    book_id, identifier_type, identifier, source, confidence
                ) VALUES (?, ?, ?, 'catalog_service', 1.0)
                """,
                (book_id, identifier_type, identifier),
            )

    @staticmethod
    def _record_match(
        conn: sqlite3.Connection,
        *,
        book_id: int,
        match: BookMatch,
        incoming: BookMatchInput,
    ) -> None:
        conn.execute(
            """
            INSERT INTO book_match_audit (matched_book_id, method, confidence, source, details_json)
            VALUES (?, ?, ?, 'catalog_service', ?)
            """,
            (
                book_id,
                match.method,
                match.confidence,
                json.dumps(
                    {
                        "provider": incoming.provider,
                        "provider_book_id": incoming.provider_book_id,
                        "isbn": incoming.isbn,
                    },
                    sort_keys=True,
                ),
            ),
        )

    def register_imported_file(
        self,
        *,
        book_id: int,
        published_file: PublishedFile,
        release_source: str | None = None,
        content_type: str | None = None,
        derived_from_file_id: int | None = None,
    ) -> dict[str, Any]:
        """Persist a published central file or return the existing SHA record."""
        normalized_content_type = self._file_content_type(
            content_type=content_type,
            file_format=published_file.file_format,
        )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                deleting = conn.execute(
                    """SELECT 1 FROM catalog_deletion_operations
                       WHERE book_id = ? AND state IN ('deleting', 'cleanup_pending')""",
                    (book_id,),
                ).fetchone()
                if deleting is not None:
                    raise CatalogServiceError("Book is being deleted")
                book = conn.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone()
                if book is None:
                    raise ValueError("Book not found")
                existing = conn.execute(
                    "SELECT * FROM book_files WHERE sha256 = ?", (published_file.sha256,)
                ).fetchone()
                if existing is not None:
                    conn.commit()
                    return dict(existing)
                if derived_from_file_id is not None:
                    parent = conn.execute(
                        "SELECT book_id FROM book_files WHERE id = ?", (derived_from_file_id,)
                    ).fetchone()
                    if parent is None or int(parent["book_id"]) != book_id:
                        raise ValueError("derived_from_file_id must belong to the same Book")
                cursor = conn.execute(
                    """
                    INSERT INTO book_files (
                        book_id, storage_path, file_format, size_bytes, sha256, mime_type,
                        release_source, content_type, import_status, derived_from_file_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'imported', ?)
                    """,
                    (
                        book_id,
                        str(published_file.path),
                        published_file.file_format,
                        published_file.size_bytes,
                        published_file.sha256,
                        published_file.mime_type,
                        self._optional_text(release_source),
                        normalized_content_type,
                        derived_from_file_id,
                    ),
                )
                row = conn.execute(
                    "SELECT * FROM book_files WHERE id = ?", (cursor.lastrowid,)
                ).fetchone()
                if row is not None:
                    # A manual/external import must converge with the normal
                    # request_process path.  Every requester for this Book gets a
                    # usable entitlement; pending rows are no longer allowed
                    # to hide an already available file behind approval.
                    now = self._now()
                    requests = conn.execute(
                        """SELECT * FROM requests WHERE book_id=? AND content_type=?
                           AND status IN ('pending_approval','approved')""",
                        (book_id, normalized_content_type),
                    ).fetchall()
                    for request in requests:
                        conn.execute(
                            """INSERT INTO user_file_entitlements(user_id,file_id,request_id,grant_source)
                               VALUES(?,?,?,'request') ON CONFLICT(user_id,file_id) DO NOTHING""",
                            (request["user_id"], row["id"], request["id"]),
                        )
                        conn.execute(
                            "INSERT INTO user_library(user_id,book_id) VALUES(?,?) ON CONFLICT(user_id,book_id) DO NOTHING",
                            (request["user_id"], book_id),
                        )
                        conn.execute(
                            """UPDATE requests SET status='fulfilled', fulfilled_file_id=?, fulfilled_at=?,
                               reviewed_at=COALESCE(reviewed_at, ?), updated_at=? WHERE id=?""",
                            (row["id"], now, now, now, request["id"]),
                        )
                conn.commit()
                if row is None:
                    raise CatalogServiceError("Failed to load imported file")
                return dict(row)
            except sqlite3.Error as exc:
                conn.rollback()
                raise CatalogServiceError("File-record transaction failed") from exc
            finally:
                conn.close()

    def publish_and_register_imported_file(
        self,
        *,
        book_id: int,
        file_store: FileStore,
        source_path: PathInput,
        source_roots: tuple[PathInput, ...],
        release_source: str | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        """Publish and register one import under the deletion-compatible leases.

        This is deliberately the sole orchestration boundary for callers that
        turn a downloaded path into a catalog file.  Holding both the canonical
        book key and the content-addressed object key closes the window between
        publication, registration and deletion cleanup.
        """
        owner = uuid.uuid4().hex
        book_lease = CatalogStorageLease(
            self._db_path, catalog_book_lease_key(self._db_path, book_id), owner
        )
        object_lease: CatalogStorageLease | None = None
        object_lease_acquired = False
        published: PublishedFile | None = None
        try:
            book_lease.acquire()
            with self._connect() as conn:
                tombstone = conn.execute(
                    "SELECT 1 FROM catalog_deletion_operations WHERE book_id=?", (book_id,)
                ).fetchone()
            if tombstone is not None:
                _raise_book_deleting()
            published = file_store.publish(source_path, source_roots=source_roots)
            object_lease = CatalogStorageLease(
                self._db_path,
                catalog_path_lease_key(str(published.path), published.sha256),
                owner,
            )
            object_lease.acquire()
            object_lease_acquired = True
            # The book lease is still owned, but repeat the durable tombstone
            # check after publication so future alternate callers cannot turn a
            # stale plan into a resurrected catalog record.
            with self._connect() as conn:
                tombstone = conn.execute(
                    "SELECT 1 FROM catalog_deletion_operations WHERE book_id=?", (book_id,)
                ).fetchone()
            if tombstone is not None:
                _raise_book_deleting()
            return self.register_imported_file(
                book_id=book_id,
                published_file=published,
                release_source=release_source,
                content_type=content_type,
            )
        except Exception:
            # Only remove an orphan while the same object key is fenced; a
            # publisher/cleanup racing this path can therefore never lose bytes
            # it has already registered.
            if published is not None and object_lease_acquired:
                with self._connect() as conn:
                    referenced = conn.execute(
                        "SELECT 1 FROM book_files WHERE storage_path=? OR sha256=?",
                        (str(published.path), published.sha256),
                    ).fetchone()
                if referenced is None:
                    published.path.unlink(missing_ok=True)
            raise
        finally:
            if object_lease is not None:
                object_lease.release()
            book_lease.release()

    def register_kepub_conversion(
        self,
        *,
        book_id: int,
        source_file_id: int,
        published_file: PublishedFile,
        converter_version: str,
        options_hash: str,
    ) -> dict[str, Any]:
        """Record one published KEPUB and atomically index its conversion key."""
        if not converter_version.strip():
            raise ValueError("converter_version is required")
        if len(options_hash) != 64 or any(
            character not in "0123456789abcdef" for character in options_hash
        ):
            raise ValueError("options_hash must be a SHA-256 hex digest")
        if published_file.file_format != "kepub":
            raise ValueError("KEPUB conversion must publish with file_format='kepub'")
        registered = self.register_imported_file(
            book_id=book_id,
            published_file=published_file,
            release_source=f"kepubify:{converter_version.strip()}",
            content_type="ebook",
            derived_from_file_id=source_file_id,
        )
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                source = conn.execute(
                    "SELECT book_id, sha256 FROM book_files WHERE id = ?", (source_file_id,)
                ).fetchone()
                if source is None or int(source["book_id"]) != book_id:
                    raise ValueError("source_file_id must belong to the same Book")
                existing = conn.execute(
                    """
                    SELECT bf.*
                    FROM kepub_conversion_cache cache
                    INNER JOIN book_files bf ON bf.id = cache.output_file_id
                    WHERE cache.source_sha256 = ? AND cache.converter_version = ?
                      AND cache.options_hash = ?
                    """,
                    (str(source["sha256"]), converter_version.strip(), options_hash),
                ).fetchone()
                if existing is not None:
                    result = dict(existing)
                else:
                    conn.execute(
                        """
                        INSERT INTO kepub_conversion_cache (
                            source_sha256, converter_version, options_hash, source_file_id,
                            output_file_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            str(source["sha256"]),
                            converter_version.strip(),
                            options_hash,
                            source_file_id,
                            int(registered["id"]),
                            self._now(),
                        ),
                    )
                    result = registered
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise CatalogServiceError("KEPUB conversion-cache transaction failed") from exc
            else:
                return result
            finally:
                conn.close()

    def find_kepub_conversion(
        self, *, source_sha256: str, converter_version: str, options_hash: str
    ) -> dict[str, Any] | None:
        """Return the centrally published KEPUB for an exact conversion key."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT bf.*
                    FROM kepub_conversion_cache cache
                    INNER JOIN book_files bf ON bf.id = cache.output_file_id
                    WHERE cache.source_sha256 = ? AND cache.converter_version = ?
                      AND cache.options_hash = ? AND bf.import_status = 'imported'
                    """,
                    (source_sha256, converter_version, options_hash),
                ).fetchone()
                return dict(row) if row is not None else None
            finally:
                conn.close()

    def get_entitled_book_file(
        self, *, user_id: int, book_id: int, file_id: int
    ) -> dict[str, Any] | None:
        """Return one imported central file only when the user is entitled to it."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT bf.*
                    FROM book_files bf
                    INNER JOIN user_file_entitlements entitlement ON entitlement.file_id = bf.id
                    WHERE bf.id = ? AND bf.book_id = ? AND entitlement.user_id = ?
                      AND bf.import_status = 'imported'
                    """,
                    (file_id, book_id, user_id),
                ).fetchone()
                return dict(row) if row is not None else None
            finally:
                conn.close()

    def grant_file_entitlement(
        self,
        *,
        user_id: int,
        file_id: int,
        request_id: int | None = None,
        grant_source: str = "direct",
    ) -> bool:
        """Idempotently grant one explicit user-to-central-file entitlement."""
        if grant_source not in {"direct", "request", "migration"}:
            raise ValueError("Unsupported entitlement grant_source")
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO user_file_entitlements (
                        user_id, file_id, request_id, grant_source
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (user_id, file_id, request_id, grant_source),
                )
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise CatalogServiceError("Entitlement transaction failed") from exc
            else:
                return cursor.rowcount > 0
            finally:
                conn.close()
