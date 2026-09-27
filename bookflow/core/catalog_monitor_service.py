"""Global catalog, monitor persistence and personal reading-progress services.

These services intentionally only persist state. Provider-specific discovery and
notifications run in durable jobs owned by the host runtime; keeping the data
layer deterministic makes retries idempotent.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any

from bookflow.core.book_groups import book_group_key_sql
from bookflow.metadata_providers import get_provider_monitoring_capabilities


class CatalogMonitorError(ValueError):
    """A safe validation or authorization failure for catalog operations."""


def _now() -> datetime:
    return datetime.now(UTC)


def _serialize_search_profile(value: Any) -> str:
    """Validate the bounded provider/request_process profile stored with a monitor."""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise CatalogMonitorError("search_profile must be an object")
    unknown = set(value) - {"fields", "language", "limit", "request_process_profile"}
    if unknown:
        raise CatalogMonitorError(
            f"Unsupported search_profile fields: {', '.join(sorted(unknown))}"
        )
    fields = value.get("fields", {})
    if not isinstance(fields, dict) or any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(field_value, str)
        or len(field_value) > 500
        for key, field_value in fields.items()
    ):
        raise CatalogMonitorError("search_profile.fields must contain short string values")
    language = value.get("language")
    if language is not None and (not isinstance(language, str) or len(language) > 32):
        raise CatalogMonitorError("search_profile.language must be a short string")
    limit = value.get("limit")
    if limit is not None and (
        isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 100
    ):
        raise CatalogMonitorError("search_profile.limit must be between 1 and 100")
    request_process_profile = value.get("request_process_profile", {})
    if not isinstance(request_process_profile, dict):
        raise CatalogMonitorError("search_profile.request_process_profile must be an object")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise CatalogMonitorError("search_profile must be JSON serializable") from exc
    if len(encoded.encode("utf-8")) > 16_384:
        raise CatalogMonitorError("search_profile is too large")
    return encoded


class CatalogMonitorService:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @staticmethod
    def _page(value: int, *, maximum: int = 100) -> int:
        return min(maximum, max(1, value))

    @staticmethod
    def _monitor(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["type"] = item.pop("monitor_type")
        item["add_to_kobo"] = bool(item["add_to_kobo"])
        try:
            profile = json.loads(str(item.pop("search_profile_json", "{}") or "{}"))
        except json.JSONDecodeError:
            profile = {}
        item["search_profile"] = profile if isinstance(profile, dict) else {}
        return item

    def catalog_books(
        self,
        *,
        user_id: int | None = None,
        query: str | None,
        author_id: int | None,
        series_id: int | None,
        offset: int,
        limit: int,
        sort: str = "title",
        file_format: str | None = None,
    ) -> dict[str, Any]:
        if sort not in {"title", "author", "recent", "year"}:
            raise CatalogMonitorError("Invalid catalog sort")
        # A catalog card represents a book a reader can actually obtain.  A
        # metadata/request row without an imported file remains addressable by
        # its detail/resolve APIs, but must not create a duplicate in browse.
        clauses: list[str] = [
            "EXISTS (SELECT 1 FROM book_files catalog_bf WHERE catalog_bf.book_id = b.id AND catalog_bf.import_status = 'imported')",
        ]
        params: list[Any] = []
        joins = ""
        if query and query.strip():
            # FTS query syntax should not leak into the API. Quote each token
            # as a phrase, allowing ordinary search text without operators.
            terms = [token.replace('"', '""') for token in query.split() if token]
            if terms:
                joins += " INNER JOIN books_fts ON books_fts.rowid = b.id"
                clauses.append("books_fts MATCH ?")
                params.append(" AND ".join(f'"{term}"*' for term in terms))
        if author_id is not None:
            joins += " INNER JOIN book_catalog_authors bca ON bca.book_id = b.id"
            clauses.append("bca.author_id = ?")
            params.append(author_id)
        if series_id is not None:
            joins += " INNER JOIN book_catalog_series bcs ON bcs.book_id = b.id"
            clauses.append("bcs.series_id = ?")
            params.append(series_id)
        if file_format and file_format.strip():
            clauses.append(
                """EXISTS (
                    SELECT 1 FROM book_files bf
                    WHERE bf.book_id = b.id AND bf.import_status = 'imported'
                      AND LOWER(bf.file_format) = LOWER(?)
                )"""
            )
            params.append(file_format.strip())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        order = {
            "title": "title COLLATE NOCASE, id",
            "author": "author COLLATE NOCASE, title COLLATE NOCASE, id",
            "recent": "created_at DESC, id DESC",
            "year": "publish_year IS NULL, publish_year DESC, title COLLATE NOCASE, id",
        }[sort]
        group_key = book_group_key_sql("matching")
        ranked_group_key = book_group_key_sql("ranked")
        library_group_key = book_group_key_sql("library_book")
        with self._connect() as conn:
            # joins/order are selected from fixed literals above, never request text.
            cte = f"""
                WITH matching AS (
                    SELECT DISTINCT b.* FROM books b{joins}{where}
                ),
                ranked AS (
                    SELECT matching.*,
                           ROW_NUMBER() OVER (
                               PARTITION BY {group_key}
                               ORDER BY matching.id ASC
                           ) AS catalog_rank
                    FROM matching
                )
            """  # noqa: S608 - joins and predicates are fixed SQL fragments.
            total = int(
                conn.execute(
                    f"{cte} SELECT COUNT(*) FROM ranked WHERE catalog_rank = 1",  # noqa: S608
                    params,
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""{cte}
                    SELECT ranked.*, EXISTS (
                        SELECT 1 FROM user_library ul
                        JOIN books library_book ON library_book.id = ul.book_id
                        WHERE ul.user_id = ? AND {library_group_key} = {ranked_group_key}
                    ) AS in_library, COALESCE((
                        SELECT json_group_array(DISTINCT bf.file_format)
                        FROM book_files bf
                        WHERE bf.book_id = ranked.id AND bf.import_status = 'imported'
                    ), '[]') AS formats_json
                    FROM ranked WHERE catalog_rank = 1
                    ORDER BY {order} LIMIT ? OFFSET ?""",  # noqa: S608
                [*params, user_id or 0, self._page(limit), max(0, offset)],
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item.pop("catalog_rank", None)
            item["in_library"] = bool(item["in_library"])
            item["formats"] = json.loads(item.pop("formats_json"))
            items.append(item)
        return {
            "items": items,
            "total": total,
            "offset": max(0, offset),
            "limit": self._page(limit),
        }

    def recent(self, *, user_id: int | None = None, offset: int, limit: int) -> dict[str, Any]:
        group_key = book_group_key_sql("matching")
        ranked_group_key = book_group_key_sql("ranked")
        library_group_key = book_group_key_sql("library_book")
        cte = f"""
            WITH matching AS (
                SELECT b.*, COALESCE(MAX(imported_bf.imported_at), b.created_at) AS added_at
                FROM books b
                JOIN book_files imported_bf ON imported_bf.book_id = b.id
                    AND imported_bf.import_status = 'imported'
                GROUP BY b.id
            ),
            ranked AS (
                SELECT matching.*,
                       ROW_NUMBER() OVER (
                           PARTITION BY {group_key}
                           ORDER BY matching.added_at DESC, matching.id ASC
                       ) AS catalog_rank
                FROM matching
            )
        """  # noqa: S608 - the interpolated expression is generated internally.
        with self._connect() as conn:
            total = int(
                conn.execute(
                    f"{cte} SELECT COUNT(*) FROM ranked WHERE catalog_rank = 1"  # noqa: S608
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""{cte}
                   SELECT ranked.*, EXISTS (
                       SELECT 1 FROM user_library ul
                       JOIN books library_book ON library_book.id = ul.book_id
                       WHERE ul.user_id = ? AND {library_group_key} = {ranked_group_key}
                   ) AS in_library, COALESCE((
                       SELECT json_group_array(DISTINCT bf.file_format)
                       FROM book_files bf
                       WHERE bf.book_id = ranked.id AND bf.import_status = 'imported'
                   ), '[]') AS formats_json
                   FROM ranked WHERE catalog_rank = 1
                   ORDER BY added_at DESC, id DESC LIMIT ? OFFSET ?""",  # noqa: S608
                (user_id or 0, self._page(limit), max(0, offset)),
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item.pop("catalog_rank", None)
            item["in_library"] = bool(item["in_library"])
            item["formats"] = json.loads(item.pop("formats_json"))
            items.append(item)
        return {
            "items": items,
            "total": total,
            "offset": max(0, offset),
            "limit": self._page(limit),
        }

    def catalog_entities(
        self, *, entity: str, query: str | None, offset: int, limit: int
    ) -> dict[str, Any]:
        if entity not in {"authors", "series"}:
            raise CatalogMonitorError("Invalid catalog entity")
        table = "catalog_authors" if entity == "authors" else "catalog_series"
        relation = "book_catalog_authors" if entity == "authors" else "book_catalog_series"
        clause, parameters = "", []
        if query and query.strip():
            clause, parameters = (
                "e.name LIKE ? ESCAPE '\\'",
                [f"%{query.strip().replace('%', '\\%').replace('_', '\\_')}%"],
            )
        with self._connect() as conn:
            availability = (
                f"EXISTS (SELECT 1 FROM {relation} available_r "  # noqa: S608
                f"JOIN book_files available_bf ON available_bf.book_id = available_r.book_id "
                f"WHERE available_r.{('author_id' if entity == 'authors' else 'series_id')} = e.id "
                "AND available_bf.import_status = 'imported')"
            )
            predicate = f"{availability}" if not clause else f"{clause} AND {availability}"
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} e WHERE {predicate}",  # noqa: S608
                    parameters,
                ).fetchone()[0]
            )
            rows = conn.execute(
                f"""SELECT e.*, COUNT(DISTINCT r.book_id) AS book_count FROM {table} e
                    JOIN {relation} r ON r.{("author_id" if entity == "authors" else "series_id")} = e.id
                    JOIN book_files visible_bf ON visible_bf.book_id = r.book_id
                      AND visible_bf.import_status = 'imported'
                    WHERE {predicate} GROUP BY e.id ORDER BY e.name COLLATE NOCASE, e.id LIMIT ? OFFSET ?""",  # noqa: S608
                [*parameters, self._page(limit), max(0, offset)],
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "offset": max(0, offset),
            "limit": self._page(limit),
        }

    def create_monitor(
        self,
        *,
        user_id: int,
        payload: dict[str, Any],
        is_admin: bool,
        library_capability: str | None,
    ) -> dict[str, Any]:
        monitor_type = str(payload.get("type") or "").strip().lower()
        provider = str(payload.get("provider") or "").strip().lower()
        query = str(payload.get("query") or "").strip()
        action = str(payload.get("action") or "notify").strip().lower()
        if (
            monitor_type not in {"author", "series"}
            or not provider
            or not query
            or len(query) > 500
        ):
            raise CatalogMonitorError("type, provider and query are required")
        if monitor_type not in get_provider_monitoring_capabilities(provider):
            raise CatalogMonitorError(f"{provider} does not support {monitor_type} monitoring")
        if action not in {"notify", "request", "auto_acquire"}:
            raise CatalogMonitorError("Invalid monitor action")
        if action == "auto_acquire" and not (is_admin or library_capability == "download-capable"):
            raise PermissionError("auto_acquire requires download capability")
        interval = payload.get("scan_interval_seconds", 86400)
        if (
            isinstance(interval, bool)
            or not isinstance(interval, int)
            or interval < 3600
            or interval > 604800
        ):
            raise CatalogMonitorError("scan_interval_seconds must be between 3600 and 604800")
        provider_id = str(payload.get("provider_id") or "").strip() or None
        if provider_id is not None and len(provider_id) > 500:
            raise CatalogMonitorError("provider_id is too long")
        search_profile_json = _serialize_search_profile(payload.get("search_profile"))
        with self._connect() as conn:
            cursor = conn.execute(
                """INSERT INTO monitors (user_id, monitor_type, provider, provider_id, query, action, add_to_kobo,
                   scan_interval_seconds, next_scan_at, search_profile_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?)""",
                (
                    user_id,
                    monitor_type,
                    provider,
                    provider_id,
                    query,
                    action,
                    int(bool(payload.get("add_to_kobo", False))),
                    interval,
                    search_profile_json,
                ),
            )
            row = conn.execute(
                "SELECT * FROM monitors WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._monitor(row)

    def list_monitors(self, *, user_id: int, offset: int, limit: int) -> dict[str, Any]:
        with self._connect() as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM monitors WHERE user_id = ?", (user_id,)
                ).fetchone()[0]
            )
            rows = conn.execute(
                "SELECT * FROM monitors WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (user_id, self._page(limit), max(0, offset)),
            ).fetchall()
        return {
            "items": [self._monitor(row) for row in rows],
            "total": total,
            "offset": max(0, offset),
            "limit": self._page(limit),
        }

    def update_monitor(
        self,
        *,
        user_id: int,
        monitor_id: int,
        payload: dict[str, Any],
        is_admin: bool,
        library_capability: str | None,
    ) -> dict[str, Any] | None:
        allowed = {
            "status",
            "action",
            "add_to_kobo",
            "provider_id",
            "query",
            "scan_interval_seconds",
            "search_profile",
        }
        updates = {key: payload[key] for key in allowed if key in payload}
        if not updates:
            raise CatalogMonitorError("No mutable monitor fields supplied")
        if updates.get("status") not in {None, "active", "paused"} or updates.get("action") not in {
            None,
            "notify",
            "request",
            "auto_acquire",
        }:
            raise CatalogMonitorError("Invalid monitor value")
        if updates.get("action") == "auto_acquire" and not (
            is_admin or library_capability == "download-capable"
        ):
            raise PermissionError("auto_acquire requires download capability")
        if "query" in updates:
            query = str(updates["query"] or "").strip()
            if not query or len(query) > 500:
                raise CatalogMonitorError("query must be a non-empty short string")
            updates["query"] = query
        if "provider_id" in updates:
            provider_id = str(updates["provider_id"] or "").strip() or None
            if provider_id is not None and len(provider_id) > 500:
                raise CatalogMonitorError("provider_id is too long")
            updates["provider_id"] = provider_id
        if "scan_interval_seconds" in updates:
            interval = updates["scan_interval_seconds"]
            if (
                isinstance(interval, bool)
                or not isinstance(interval, int)
                or interval < 3600
                or interval > 604800
            ):
                raise CatalogMonitorError("scan_interval_seconds must be between 3600 and 604800")
        if "search_profile" in updates:
            updates["search_profile_json"] = _serialize_search_profile(
                updates.pop("search_profile")
            )
        fields = ", ".join(f"{key} = ?" for key in updates)
        with self._connect() as conn:
            conn.execute(
                f"UPDATE monitors SET {fields}, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ?",  # noqa: S608
                [*updates.values(), monitor_id, user_id],
            )
            row = conn.execute(
                "SELECT * FROM monitors WHERE id = ? AND user_id = ?", (monitor_id, user_id)
            ).fetchone()
        return self._monitor(row) if row else None

    def delete_monitor(self, *, user_id: int, monitor_id: int) -> bool:
        with self._connect() as conn:
            return (
                conn.execute(
                    "DELETE FROM monitors WHERE id = ? AND user_id = ?", (monitor_id, user_id)
                ).rowcount
                == 1
            )

    def schedule_scan(self, *, user_id: int, monitor_id: int) -> bool:
        with self._connect() as conn:
            return (
                conn.execute(
                    "UPDATE monitors SET next_scan_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND user_id = ? AND status = 'active'",
                    (monitor_id, user_id),
                ).rowcount
                == 1
            )

    def list_discoveries(
        self, *, user_id: int, monitor_id: int, offset: int, limit: int
    ) -> dict[str, Any] | None:
        with self._connect() as conn:
            owned = conn.execute(
                "SELECT 1 FROM monitors WHERE id = ? AND user_id = ?", (monitor_id, user_id)
            ).fetchone()
            if owned is None:
                return None
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM monitor_discoveries WHERE monitor_id = ?", (monitor_id,)
                ).fetchone()[0]
            )
            rows = conn.execute(
                "SELECT * FROM monitor_discoveries WHERE monitor_id = ? ORDER BY discovered_at DESC, id DESC LIMIT ? OFFSET ?",
                (monitor_id, self._page(limit), max(0, offset)),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "offset": max(0, offset),
            "limit": self._page(limit),
        }

    def set_discovery_state(
        self, *, user_id: int, monitor_id: int, discovery_id: int, state: str
    ) -> dict[str, Any] | None:
        """Let a monitor owner explicitly ignore, or restore, a discovery."""
        if state not in {"ignored", "discovered"}:
            raise CatalogMonitorError("Discovery state must be ignored or discovered")
        with self._connect() as conn:
            cursor = conn.execute(
                """UPDATE monitor_discoveries SET state = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND monitor_id = ? AND EXISTS (
                       SELECT 1 FROM monitors WHERE id = ? AND user_id = ?
                   )""",
                (state, discovery_id, monitor_id, monitor_id, user_id),
            )
            if cursor.rowcount != 1:
                return None
            row = conn.execute(
                "SELECT * FROM monitor_discoveries WHERE id = ?", (discovery_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_progress(self, *, user_id: int, book_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            if not self._has_reading_entitlement(conn, user_id=user_id, book_id=book_id):
                return None
            row = conn.execute(
                "SELECT * FROM user_reading_progress WHERE user_id = ? AND book_id = ?",
                (user_id, book_id),
            ).fetchone()
        return dict(row) if row else None

    def list_active_progress(self, *, user_id: int, offset: int, limit: int) -> dict[str, Any]:
        with self._connect() as conn:
            entitlement = """EXISTS (SELECT 1 FROM user_library ul WHERE ul.user_id = rp.user_id AND ul.book_id = rp.book_id)
                AND EXISTS (SELECT 1 FROM book_files bf JOIN user_file_entitlements ufe ON ufe.file_id = bf.id
                    WHERE bf.book_id = rp.book_id AND ufe.user_id = rp.user_id AND bf.import_status = 'imported')"""
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM user_reading_progress rp WHERE rp.user_id = ? AND rp.active = 1 AND {entitlement}",  # noqa: S608
                    (user_id,),
                ).fetchone()[0]
            )
            rows = conn.execute(
                """SELECT rp.*, b.title, b.author, b.cover_url FROM user_reading_progress rp
                JOIN books b ON b.id = rp.book_id WHERE rp.user_id = ? AND rp.active = 1
                AND EXISTS (SELECT 1 FROM user_library ul WHERE ul.user_id = rp.user_id AND ul.book_id = rp.book_id)
                AND EXISTS (SELECT 1 FROM book_files bf JOIN user_file_entitlements ufe ON ufe.file_id = bf.id
                    WHERE bf.book_id = rp.book_id AND ufe.user_id = rp.user_id AND bf.import_status = 'imported')
                ORDER BY rp.updated_at DESC, rp.book_id DESC LIMIT ? OFFSET ?""",
                (user_id, self._page(limit), max(0, offset)),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "offset": max(0, offset),
            "limit": self._page(limit),
        }

    def set_progress(
        self,
        *,
        user_id: int,
        book_id: int,
        progress: float,
        source: str,
        reset: bool = False,
        require_kobo: bool = False,
        file_id: int | None = None,
        locator: Any = None,
        client_updated_at: str | None = None,
    ) -> dict[str, Any]:
        if (
            source not in {"web", "kobo"}
            or isinstance(progress, bool)
            or not isinstance(progress, (int, float))
            or not 0 <= float(progress) <= 100
        ):
            raise CatalogMonitorError("progress must be a number from 0 to 100")
        reader_update = file_id is not None or locator is not None or client_updated_at is not None
        serialized_locator: str | None = None
        normalized_reader_updated_at = ""
        if reader_update:
            if isinstance(file_id, bool) or not isinstance(file_id, int) or file_id < 1:
                raise CatalogMonitorError("file_id must be a positive integer")
            if locator is None:
                raise CatalogMonitorError("locator is required for reader progress")
            try:
                serialized_locator = json.dumps(locator, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError) as exc:
                raise CatalogMonitorError("locator must be JSON serializable") from exc
            if len(serialized_locator.encode("utf-8")) > 8192:
                raise CatalogMonitorError("locator must be at most 8192 bytes")
            normalized_reader_updated_at = self._normalize_reader_updated_at(client_updated_at)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if require_kobo:
                allowed = conn.execute(
                    """SELECT 1 FROM user_kobo_books ukb JOIN book_files bf ON bf.id = ukb.file_id
                    JOIN user_file_entitlements ufe ON ufe.file_id = bf.id
                    WHERE ukb.user_id = ? AND ukb.book_id = ? AND ukb.desired_state = 'active'
                    AND ufe.user_id = ? AND bf.import_status = 'imported'""",
                    (user_id, book_id, user_id),
                ).fetchone()
            else:
                allowed = self._has_reading_entitlement(conn, user_id=user_id, book_id=book_id)
            if not allowed:
                conn.rollback()
                raise PermissionError("Book is not available to this user")
            if reader_update:
                reader_file_allowed = conn.execute(
                    """SELECT 1 FROM user_library ul
                    JOIN book_files bf ON bf.book_id = ul.book_id
                    JOIN user_file_entitlements ufe ON ufe.file_id = bf.id
                    WHERE ul.user_id = ? AND ul.book_id = ? AND bf.id = ?
                      AND ufe.user_id = ul.user_id AND bf.import_status = 'imported'""",
                    (user_id, book_id, file_id),
                ).fetchone()
                if reader_file_allowed is None:
                    conn.rollback()
                    raise PermissionError("File is not available to this user")
            old = conn.execute(
                """SELECT progress, revision, reader_updated_at
                FROM user_reading_progress WHERE user_id = ? AND book_id = ?""",
                (user_id, book_id),
            ).fetchone()
            if reader_update and old is not None and old["reader_updated_at"] is not None:
                existing_reader_updated_at = self._normalize_reader_updated_at(
                    str(old["reader_updated_at"])
                )
                if existing_reader_updated_at > normalized_reader_updated_at:
                    conn.rollback()
                    current = conn.execute(
                        "SELECT * FROM user_reading_progress WHERE user_id = ? AND book_id = ?",
                        (user_id, book_id),
                    ).fetchone()
                    return dict(current)
            if (
                old is not None
                and float(progress) < float(old["progress"])
                and not reset
                and not reader_update
            ):
                conn.rollback()
                raise CatalogMonitorError("Progress may only decrease after an explicit reset")
            revision = int(old["revision"]) + 1 if old else 1
            active = int(float(progress) < 100)
            if reader_update:
                conn.execute(
                    """INSERT INTO user_reading_progress(
                        user_id, book_id, progress, active, source, revision, updated_at,
                        file_id, locator_json, reader_updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)
                    ON CONFLICT(user_id,book_id) DO UPDATE SET
                        progress=excluded.progress, active=excluded.active, source=excluded.source,
                        revision=excluded.revision, updated_at=CURRENT_TIMESTAMP,
                        file_id=excluded.file_id, locator_json=excluded.locator_json,
                        reader_updated_at=excluded.reader_updated_at""",
                    (
                        user_id,
                        book_id,
                        float(progress),
                        active,
                        source,
                        revision,
                        file_id,
                        serialized_locator,
                        normalized_reader_updated_at,
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO user_reading_progress(user_id, book_id, progress, active, source, revision, updated_at, reader_updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')) ON CONFLICT(user_id,book_id) DO UPDATE SET
                    progress=excluded.progress, active=excluded.active, source=excluded.source, revision=excluded.revision, updated_at=CURRENT_TIMESTAMP,
                    locator_json=NULL, file_id=NULL, reader_updated_at=excluded.reader_updated_at""",
                    (user_id, book_id, float(progress), active, source, revision),
                )
            self._append_kobo_progress_change(
                conn,
                user_id=user_id,
                book_id=book_id,
                progress=float(progress),
                active=active,
                source=source,
            )
            row = conn.execute(
                "SELECT * FROM user_reading_progress WHERE user_id = ? AND book_id = ?",
                (user_id, book_id),
            ).fetchone()
            conn.commit()
        return dict(row)

    @staticmethod
    def _normalize_reader_updated_at(value: str | None) -> str:
        """Normalize the client read timestamp used for offline last-write-wins."""
        if not isinstance(value, str) or not value.strip():
            raise CatalogMonitorError("client_updated_at must be an ISO-8601 timestamp")
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise CatalogMonitorError("client_updated_at must be an ISO-8601 timestamp") from exc
        if parsed.tzinfo is None:
            raise CatalogMonitorError("client_updated_at must include a timezone")
        return parsed.astimezone(UTC).isoformat(timespec="microseconds")

    def clear_progress(self, *, user_id: int, book_id: int) -> bool:
        """Explicitly reset state and publish an inactive Kobo delta when paired."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not self._has_reading_entitlement(conn, user_id=user_id, book_id=book_id):
                conn.rollback()
                raise PermissionError("Book is not available to this user")
            old = conn.execute(
                "SELECT revision FROM user_reading_progress WHERE user_id = ? AND book_id = ?",
                (user_id, book_id),
            ).fetchone()
            if old is None:
                conn.rollback()
                return False
            revision = int(old["revision"]) + 1
            conn.execute(
                """UPDATE user_reading_progress SET progress = 0, active = 0, source = 'web',
                   revision = ?, updated_at = CURRENT_TIMESTAMP, locator_json = NULL, file_id = NULL,
                   reader_updated_at = strftime('%Y-%m-%dT%H:%M:%f+00:00', 'now')
                   WHERE user_id = ? AND book_id = ?""",
                (revision, user_id, book_id),
            )
            self._append_kobo_progress_change(
                conn, user_id=user_id, book_id=book_id, progress=0.0, active=0, source="web"
            )
            conn.commit()
            return True

    @staticmethod
    def _has_reading_entitlement(conn: sqlite3.Connection, *, user_id: int, book_id: int) -> bool:
        return (
            conn.execute(
                """SELECT 1 FROM user_library ul WHERE ul.user_id = ? AND ul.book_id = ?
               AND EXISTS (SELECT 1 FROM book_files bf JOIN user_file_entitlements ufe ON ufe.file_id = bf.id
                   WHERE bf.book_id = ul.book_id AND ufe.user_id = ul.user_id AND bf.import_status = 'imported')""",
                (user_id, book_id),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _append_kobo_progress_change(
        conn: sqlite3.Connection,
        *,
        user_id: int,
        book_id: int,
        progress: float,
        active: int,
        source: str,
    ) -> None:
        kobo = conn.execute(
            "SELECT current_revision FROM user_kobo_settings WHERE user_id = ? AND enabled = 1",
            (user_id,),
        ).fetchone()
        kobo_book = conn.execute(
            """SELECT 1 FROM user_kobo_books ukb JOIN book_files bf ON bf.id = ukb.file_id
                JOIN user_file_entitlements ufe ON ufe.file_id = bf.id WHERE ukb.user_id = ? AND ukb.book_id = ?
                AND ukb.desired_state = 'active' AND ufe.user_id = ? AND bf.import_status = 'imported'""",
            (user_id, book_id, user_id),
        ).fetchone()
        if kobo is None or kobo_book is None:
            return
        kobo_revision = int(kobo["current_revision"]) + 1
        conn.execute(
            """INSERT INTO user_kobo_progress_changes(user_id, revision, book_id, progress, active, source)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, kobo_revision, book_id, progress, active, source),
        )
        conn.execute(
            "UPDATE user_kobo_settings SET current_revision = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ?",
            (kobo_revision, user_id),
        )
