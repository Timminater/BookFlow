"""BookFlow content for the reader's Discover tab, in the Kobo Store's list shape.

Nickel fills Discover from ``featured_lists`` (the rows) and ``featured_list``
(the books in one row), and opens a book through the store's product pages. The
paging fields, the ``{"Book": ...}`` item wrapper and the product fields match
responses observed from storeapi.kobo.com in November 2026. Nothing is
forwarded to Kobo: every row comes from BookFlow's own library, catalog and
Bestseller 60 data, and every request is scoped to the token's user.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from bookflow.core.bestseller_service import LIST_DEFINITIONS, BestsellerSuggestionService
from bookflow.core.book_groups import equivalent_book_ids
from bookflow.core.catalog_monitor_service import CatalogMonitorService
from bookflow.core.durable_jobs import DurableJobService
from bookflow.core.library_service import LibraryService
from bookflow.core.release_profile_service import ReleaseProfileService
from bookflow.core.request_workflow_service import RequestWorkflowError, RequestWorkflowService
from bookflow.core.web_push_events import enqueue_web_push

_LIST_PREFIX = "bf00d15c-0000-4000-8000-"
MAX_PAGE_SIZE = 100

# A Discover book is one any signed-in user may see: it has an imported file
# (catalog) or belongs to the active Bestseller 60 snapshot.
_VISIBLE_BOOK_SQL = """
    SELECT b.* FROM books b
    WHERE b.id = ? AND (
        EXISTS (
            SELECT 1 FROM book_files bf
            WHERE bf.book_id = b.id AND bf.import_status = 'imported'
        )
        OR EXISTS (
            SELECT 1 FROM bestseller_entries e
            JOIN bestseller_snapshots s ON s.id = e.snapshot_id
            WHERE e.book_id = b.id AND s.is_active = 1 AND s.status = 'completed'
        )
    )
"""


class DiscoverError(ValueError):
    """A Discover request that names an unknown row or an unavailable book."""


@dataclass(frozen=True)
class DiscoverList:
    number: int
    key: str
    name: str

    @property
    def identifier(self) -> str:
        return f"{_LIST_PREFIX}{self.number:012x}"


_LIBRARY = DiscoverList(1, "library", "Nog niet op je Kobo")
_RECENT = DiscoverList(2, "recent", "Nieuw in BookFlow")
_BESTSELLERS = tuple(
    DiscoverList(16 + index, f"bestseller:{definition.key}", definition.label)
    for index, definition in enumerate(LIST_DEFINITIONS)
)
_LISTS = {item.number: item for item in (_LIBRARY, _RECENT, *_BESTSELLERS)}


@dataclass(frozen=True)
class DiscoverPage:
    books: list[dict[str, Any]]
    total: int


class KoboDiscoverService:
    """Discover rows and product pages for one user.

    Only ``claim`` and ``request`` change state, and only for the requesting
    user, exactly like the web catalog's add and request actions.
    """

    def __init__(
        self,
        db_path: str,
        *,
        catalog: CatalogMonitorService | None = None,
        suggestions: BestsellerSuggestionService | None = None,
        library: LibraryService | None = None,
    ) -> None:
        self._db_path = db_path
        self._catalog = catalog or CatalogMonitorService(db_path)
        self._suggestions = suggestions or BestsellerSuggestionService(db_path)
        self._library = library or LibraryService(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def featured_lists(self, *, user_id: int) -> list[DiscoverList]:
        """Return the non-empty rows, in display order."""
        rows: list[DiscoverList] = []
        if self._library_page(user_id=user_id, offset=0, limit=1).total:
            rows.append(_LIBRARY)
        if self._catalog.recent(user_id=user_id, offset=0, limit=1)["total"]:
            rows.append(_RECENT)
        dashboard = self._suggestions.dashboard(user_id=user_id, per_list_limit=1)
        totals = {str(item["key"]): int(item["total"]) for item in dashboard["lists"]}
        rows.extend(
            row for row in _BESTSELLERS if totals.get(row.key.removeprefix("bestseller:"), 0)
        )
        return rows

    def _resolve_list(self, *, user_id: int, list_id: str) -> DiscoverList:
        try:
            canonical = str(UUID(list_id))
        except ValueError:
            raise DiscoverError("Unknown Discover list") from None
        if canonical.startswith(_LIST_PREFIX):
            found = _LISTS.get(int(canonical[-12:], 16))
            if found is None:
                raise DiscoverError("Unknown Discover list")
            return found
        # The reader also asks for Kobo Store lists it remembers, such as
        # "Top 50 Books". Show the closest BookFlow row instead of an error.
        rows = self.featured_lists(user_id=user_id)
        bestsellers = [row for row in rows if row in _BESTSELLERS]
        return bestsellers[0] if bestsellers else _RECENT

    def page(self, *, user_id: int, list_id: str, offset: int, limit: int) -> DiscoverPage:
        """Return one page of a row; raises DiscoverError for an unknown BookFlow row."""
        row = self._resolve_list(user_id=user_id, list_id=list_id)
        if row is _LIBRARY:
            return self._library_page(user_id=user_id, offset=offset, limit=limit)
        if row is _RECENT:
            result = self._catalog.recent(user_id=user_id, offset=offset, limit=limit)
        else:
            result = self._suggestions.list_suggestions(
                list_key=row.key.removeprefix("bestseller:"),
                user_id=user_id,
                offset=offset,
                limit=limit,
            )
        return DiscoverPage(books=list(result["items"]), total=int(result["total"]))

    def search(self, *, user_id: int, query: str, offset: int, limit: int) -> DiscoverPage:
        """Search the catalog by title or author, as the web catalog search does."""
        if not query.strip():
            return DiscoverPage(books=[], total=0)
        result = self._catalog.catalog_books(
            user_id=user_id,
            query=query.strip(),
            author_id=None,
            series_id=None,
            offset=offset,
            limit=limit,
        )
        return DiscoverPage(books=list(result["items"]), total=int(result["total"]))

    def more_by_author(self, *, user_id: int, book_id: int, limit: int) -> DiscoverPage:
        """Other catalog books by the same author, for the product page."""
        book = self.book(book_id=book_id)
        author = str(book.get("author") or "").strip() if book else ""
        if not author:
            return DiscoverPage(books=[], total=0)
        found = self.search(user_id=user_id, query=author, offset=0, limit=limit + 1).books
        others = [item for item in found if int(item["id"]) != book_id][:limit]
        return DiscoverPage(books=others, total=len(others))

    def _library_page(self, *, user_id: int, offset: int, limit: int) -> DiscoverPage:
        # Library books with a readable file that the user has not sent to the reader.
        where = """
            FROM user_library ul
            JOIN books b ON b.id = ul.book_id
            WHERE ul.user_id = ?
              AND EXISTS (
                  SELECT 1 FROM user_file_entitlements ufe
                  JOIN book_files bf ON bf.id = ufe.file_id
                  WHERE ufe.user_id = ul.user_id AND bf.book_id = b.id
                    AND bf.import_status = 'imported'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM user_kobo_books ukb
                  WHERE ukb.user_id = ul.user_id AND ukb.book_id = b.id
                    AND ukb.desired_state = 'active'
              )
        """
        conn = self._connect()
        try:
            total = int(conn.execute(f"SELECT COUNT(*) {where}", (user_id,)).fetchone()[0])
            rows = conn.execute(
                f"SELECT b.* {where} ORDER BY ul.added_at DESC, b.id DESC LIMIT ? OFFSET ?",
                (user_id, limit, offset),
            ).fetchall()
        finally:
            conn.close()
        return DiscoverPage(books=[dict(row) for row in rows], total=total)

    def book(self, *, book_id: int) -> dict[str, Any] | None:
        """Return a book only when Discover may show it to any signed-in user."""
        conn = self._connect()
        try:
            row = conn.execute(_VISIBLE_BOOK_SQL, (book_id,)).fetchone()
        finally:
            conn.close()
        return dict(row) if row is not None else None

    def cover_url(self, *, book_id: int) -> str | None:
        book = self.book(book_id=book_id)
        cover = book.get("cover_url") if book else None
        return cover if isinstance(cover, str) and cover.strip() else None

    def claim(self, *, user_id: int, book_id: int) -> tuple[int, int]:
        """Add a catalog book to the user's library and pick the file to send to the reader.

        Returns ``(book_id, file_id)`` of an entitled EPUB/KEPUB file. Raises
        DiscoverError for a book without such a file; a fileless Bestseller 60
        book must be requested on the website instead.
        """
        try:
            self._library.add_to_library(user_id=user_id, book_id=book_id)
        except ValueError:
            raise DiscoverError("This book has no file to read yet") from None
        conn = self._connect()
        try:
            candidates = equivalent_book_ids(conn, book_id) or [book_id]
            marks = ",".join("?" for _ in candidates)
            row = conn.execute(
                f"""
                SELECT bf.book_id, bf.id FROM book_files bf
                JOIN user_file_entitlements ufe ON ufe.file_id = bf.id AND ufe.user_id = ?
                WHERE bf.book_id IN ({marks}) AND bf.import_status = 'imported'
                  AND lower(bf.file_format) IN ('epub', 'kepub')
                ORDER BY lower(bf.file_format) = 'epub' DESC, bf.id
                LIMIT 1
                """,  # noqa: S608 - only placeholders are interpolated
                (user_id, *candidates),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            raise DiscoverError("This book has no EPUB for the reader")
        return int(row["book_id"]), int(row["id"])

    def request(self, *, user_id: int, book_id: int) -> dict[str, Any]:
        """Request a book that has no file yet, as ``POST /api/v1/requests`` does.

        Uses the default active release profile and asks for the book to be added
        to the reader once a file is imported. Admins and auto-approved users start
        the search at once; other requests wait for an admin, as on the website.
        """
        profiles = ReleaseProfileService(self._db_path).list(active_only=True)
        if not profiles:
            raise DiscoverError("No release profile is configured")
        try:
            created = RequestWorkflowService(self._db_path).create_request(
                user_id=user_id,
                book_id=book_id,
                profile_id=int(profiles[0]["id"]),
                note="Aangevraagd vanaf de Kobo",
                add_to_kobo_when_available=True,
                reject_if_available=True,
            )
        except RequestWorkflowError as exc:
            raise DiscoverError(str(exc)) from exc
        conn = self._connect()
        try:
            title = conn.execute("SELECT title FROM books WHERE id = ?", (book_id,)).fetchone()
        finally:
            conn.close()
        if title is not None:
            enqueue_web_push(
                self._db_path,
                user_id=user_id,
                event_type="request.status",
                payload={
                    "title": str(title["title"]),
                    "book_id": book_id,
                    "status": str(created["status"]),
                    "url": f"/catalog/{book_id}",
                },
                event_key=f"request:{created['id']}:{created['status']}",
            )
        if created.get("status") == "approved" and created.get("request_process_id") is not None:
            DurableJobService(self._db_path).enqueue(
                job_key=f"search-request:{created['request_process_id']}",
                job_type="search_request",
                payload={"request_process_id": created["request_process_id"]},
            )
        return created
