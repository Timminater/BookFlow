"""BookFlow content for the reader's Discover tab, in the Kobo Store's list shape.

Nickel fills Discover from ``featured_lists`` (the rows) and ``featured_list``
(the books in one row). The paging fields and the ``{"Book": ...}`` item wrapper
match responses observed from storeapi.kobo.com in November 2026. Nothing is
forwarded to Kobo: every row comes from BookFlow's own library, catalog and
Bestseller 60 data, and every request is scoped to the token's user.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from bookflow.core.bestseller_service import LIST_DEFINITIONS, BestsellerSuggestionService
from bookflow.core.catalog_monitor_service import CatalogMonitorService

_LIST_PREFIX = "bf00d15c-0000-4000-8000-"
MAX_PAGE_SIZE = 100


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


def parse_list_uuid(value: str) -> DiscoverList:
    canonical = str(UUID(value))
    if not canonical.startswith(_LIST_PREFIX):
        raise ValueError("Unknown Discover list")
    found = _LISTS.get(int(canonical[-12:], 16))
    if found is None:
        raise ValueError("Unknown Discover list")
    return found


@dataclass(frozen=True)
class DiscoverPage:
    books: list[dict[str, Any]]
    total: int


class KoboDiscoverService:
    """Read-only Discover rows for one user; it never changes library or sync state."""

    def __init__(
        self,
        db_path: str,
        *,
        catalog: CatalogMonitorService | None = None,
        suggestions: BestsellerSuggestionService | None = None,
    ) -> None:
        self._db_path = db_path
        self._catalog = catalog or CatalogMonitorService(db_path)
        self._suggestions = suggestions or BestsellerSuggestionService(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def featured_lists(self, *, user_id: int) -> list[DiscoverList]:
        """Return the non-empty rows, in display order."""
        rows: list[DiscoverList] = []
        if self.page(user_id=user_id, list_id=_LIBRARY.identifier, offset=0, limit=1).total:
            rows.append(_LIBRARY)
        if self.page(user_id=user_id, list_id=_RECENT.identifier, offset=0, limit=1).total:
            rows.append(_RECENT)
        dashboard = self._suggestions.dashboard(user_id=user_id, per_list_limit=1)
        totals = {str(item["key"]): int(item["total"]) for item in dashboard["lists"]}
        rows.extend(
            row for row in _BESTSELLERS if totals.get(row.key.removeprefix("bestseller:"), 0)
        )
        return rows

    def page(self, *, user_id: int, list_id: str, offset: int, limit: int) -> DiscoverPage:
        """Return one page of a row; raises ValueError for an unknown row."""
        row = parse_list_uuid(list_id)
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

    def cover_url(self, *, book_id: int) -> str | None:
        """Return a cover only for a book that Discover may show to any signed-in user."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT b.cover_url FROM books b
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
                """,
                (book_id,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        cover = row["cover_url"]
        return cover if isinstance(cover, str) and cover.strip() else None
