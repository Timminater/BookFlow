"""Transactional Request and shared RequestProcess persistence."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Never

from bookflow.core.release_profile_service import (
    ReleaseProfileError,
    ReleaseProfileService,
    profile_snapshot_from_row,
    snapshot_hash,
)
from bookflow.core.request_state import (
    RequestEvent,
    RequestProcessStatus,
    RequestStatus,
    StateTransitionError,
    profile_hash,
    profile_snapshot,
    transition_request,
)
from bookflow.core.requests_service import RequestServiceError, enforce_request_limits

if TYPE_CHECKING:
    from pathlib import Path

ACTIVE_REQUEST_STATUSES = (
    RequestStatus.PENDING_APPROVAL.value,
    RequestStatus.APPROVED.value,
)
ACTIVE_REQUEST_PROCESS_STATUSES = tuple(
    status.value
    for status in RequestProcessStatus
    if status
    not in {
        RequestProcessStatus.COMPLETED,
        RequestProcessStatus.CANCELLED,
        RequestProcessStatus.FAILED,
    }
)


class RequestWorkflowError(ValueError):
    """Domain error with an HTTP-compatible status and stable code."""

    def __init__(self, message: str, *, code: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _raise_domain(message: str, *, code: str, status_code: int) -> Never:
    raise RequestWorkflowError(message, code=code, status_code=status_code)


def _required_lastrowid(cursor: sqlite3.Cursor, entity: str) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError(f"{entity} insert returned no id")
    return cursor.lastrowid


class RequestWorkflowService:
    """Enforce request/request_process invariants inside short SQLite transactions."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @staticmethod
    def _load_required(
        conn: sqlite3.Connection,
        table: str,
        record_id: int,
    ) -> sqlite3.Row:
        queries = {
            "users": "SELECT * FROM users WHERE id = ?",
            "books": "SELECT * FROM books WHERE id = ?",
            "requests": "SELECT * FROM requests WHERE id = ?",
            "request_processes": "SELECT * FROM request_processes WHERE id = ?",
        }
        if table not in queries:
            raise ValueError("Unsupported table")
        row = conn.execute(queries[table], (record_id,)).fetchone()
        if row is None:
            _raise_domain(
                f"{table.removesuffix('s').capitalize()} not found",
                code=f"{table.removesuffix('s')}_not_found",
                status_code=404,
            )
        return row

    @staticmethod
    def _available_entitled_file(
        conn: sqlite3.Connection,
        *,
        user_id: int,
        book_id: int,
        content_type: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT book_files.*
            FROM user_file_entitlements
            JOIN book_files ON book_files.id = user_file_entitlements.file_id
            WHERE user_file_entitlements.user_id = ?
              AND book_files.book_id = ?
              AND book_files.content_type = ?
              AND book_files.import_status = 'imported'
            ORDER BY book_files.id
            LIMIT 1
            """,
            (user_id, book_id, content_type),
        ).fetchone()

    @staticmethod
    def _available_imported_file(
        conn: sqlite3.Connection,
        *,
        book_id: int,
        content_type: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT *
            FROM book_files
            WHERE book_id = ? AND content_type = ? AND import_status = 'imported'
            ORDER BY id
            LIMIT 1
            """,
            (book_id, content_type),
        ).fetchone()

    def create_request(
        self,
        *,
        user_id: int,
        book_id: int,
        profile: dict[str, Any] | None = None,
        profile_id: int | None = None,
        content_type: str = "ebook",
        note: str | None = None,
        add_to_kobo_when_available: bool = False,
        reject_if_available: bool = False,
    ) -> dict[str, Any]:
        """Create one active request or return its idempotent existing row."""
        if profile_id is not None:
            try:
                profile_row = ReleaseProfileService(self._db_path).get(profile_id, active_only=True)
            except ReleaseProfileError as exc:
                _raise_domain(str(exc), code="profile_not_found", status_code=404)
            snapshot = profile_snapshot_from_row(profile_row)
        elif profile is not None:  # compatibility for internal callers/tests
            snapshot = profile_snapshot(profile)
        else:
            _raise_domain("profile_id is required", code="profile_required", status_code=400)
        snapshot_json = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
        fingerprint = (
            snapshot_hash(snapshot) if profile_id is not None else profile_hash(profile or {})
        )
        normalized_content_type = content_type.strip().casefold()
        if normalized_content_type != "ebook":
            raise RequestWorkflowError(
                "Audiobooks are no longer supported",
                code="invalid_content_type",
                status_code=400,
            )

        conn = self._connect()
        auto_approve = False
        approval_source = "admin"
        created: dict[str, Any] | None = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            user = self._load_required(conn, "users", user_id)
            self._load_required(conn, "books", book_id)
            if not bool(user["is_active"]):
                _raise_domain(
                    "Inactive users cannot create requests",
                    code="inactive_user",
                    status_code=403,
                )
            is_admin = str(user["role"]) == "admin"
            auto_approve = is_admin or bool(user["auto_approve_requests"])
            approval_source = "admin" if is_admin else "user_policy"

            if (
                reject_if_available
                and conn.execute(
                    "SELECT 1 FROM book_files WHERE book_id=? AND content_type=? AND import_status='imported' LIMIT 1",
                    (book_id, normalized_content_type),
                ).fetchone()
                is not None
            ):
                _raise_domain(
                    "This book is available; add it to your library instead",
                    code="book_available",
                    status_code=409,
                )

            existing = conn.execute(
                """
                SELECT * FROM requests
                WHERE user_id = ? AND book_id = ? AND content_type = ? AND profile_hash = ?
                  AND status IN (?, ?)
                """,
                (
                    user_id,
                    book_id,
                    normalized_content_type,
                    fingerprint,
                    *ACTIVE_REQUEST_STATUSES,
                ),
            ).fetchone()
            if existing is not None:
                conn.commit()
                return dict(existing)

            try:
                enforce_request_limits(conn, user_id=user_id)
            except RequestServiceError as exc:
                _raise_domain(
                    str(exc),
                    code=exc.code or "request_limit_reached",
                    status_code=exc.status_code,
                )

            entitled_file = self._available_entitled_file(
                conn,
                user_id=user_id,
                book_id=book_id,
                content_type=normalized_content_type,
            )
            status = (
                RequestStatus.FULFILLED.value
                if entitled_file is not None
                else RequestStatus.PENDING_APPROVAL.value
            )
            cursor = conn.execute(
                """
                INSERT INTO requests (
                    user_id, book_id, content_type, profile_id, profile_name, profile_hash, profile_snapshot_json,
                    status, note, add_to_kobo_when_available, fulfilled_file_id,
                    fulfilled_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user_id,
                    book_id,
                    normalized_content_type,
                    profile_id,
                    str(snapshot.get("profile_name")) if profile_id is not None else None,
                    fingerprint,
                    snapshot_json,
                    status,
                    note.strip() if note else None,
                    int(add_to_kobo_when_available),
                    entitled_file["id"] if entitled_file is not None else None,
                    datetime.now(UTC).isoformat(timespec="seconds")
                    if entitled_file is not None
                    else None,
                ),
            )
            request_row = self._load_required(
                conn,
                "requests",
                _required_lastrowid(cursor, "Request"),
            )
            conn.commit()
            created = dict(request_row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        if created is None:
            raise RuntimeError("Request insert did not return a row")
        if auto_approve:
            approved, _request_process, _created = self.approve_request(
                int(created["id"]),
                reviewer_user_id=user_id,
                _requester_policy=approval_source == "user_policy",
            )
            return approved
        return created

    def approve_request(
        self,
        request_id: int,
        *,
        reviewer_user_id: int,
        _requester_policy: bool = False,
    ) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
        """Approve idempotently and create or join exactly one active request_process."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            reviewer = self._load_required(conn, "users", reviewer_user_id)
            request_row = self._load_required(conn, "requests", request_id)
            is_authorized_policy = (
                _requester_policy
                and int(request_row["user_id"]) == reviewer_user_id
                and bool(reviewer["auto_approve_requests"])
                and bool(reviewer["is_active"])
            )
            if not is_authorized_policy and (
                reviewer["role"] != "admin" or not bool(reviewer["is_active"])
            ):
                _raise_domain(
                    "Admin access required",
                    code="admin_required",
                    status_code=403,
                )
            approval_source = "user_policy" if is_authorized_policy else "admin"
            current_status = RequestStatus(request_row["status"])
            if current_status in {RequestStatus.FULFILLED, RequestStatus.APPROVED}:
                request_process = (
                    self._load_required(
                        conn, "request_processes", request_row["request_process_id"]
                    )
                    if request_row["request_process_id"] is not None
                    else None
                )
                conn.commit()
                return dict(request_row), dict(request_process) if request_process else None, False
            try:
                transition_request(current_status, RequestEvent.APPROVE)
            except StateTransitionError as exc:
                raise RequestWorkflowError(
                    str(exc),
                    code="invalid_request_transition",
                    status_code=409,
                ) from exc

            entitled_file = self._available_imported_file(
                conn,
                book_id=request_row["book_id"],
                content_type=request_row["content_type"],
            )
            reviewed_at = datetime.now(UTC).isoformat(timespec="seconds")
            if entitled_file is not None:
                conn.execute(
                    """
                    INSERT INTO user_file_entitlements (user_id, file_id, request_id, grant_source)
                    VALUES (?, ?, ?, 'request')
                    ON CONFLICT(user_id, file_id) DO NOTHING
                    """,
                    (request_row["user_id"], entitled_file["id"], request_id),
                )
                conn.execute(
                    """
                    INSERT INTO user_library (user_id, book_id)
                    VALUES (?, ?)
                    ON CONFLICT(user_id, book_id) DO NOTHING
                    """,
                    (request_row["user_id"], request_row["book_id"]),
                )
                conn.execute(
                    """
                    UPDATE requests
                    SET status = 'fulfilled', fulfilled_file_id = ?, fulfilled_at = ?,
                        reviewed_by = ?, reviewed_at = ?, approval_source = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        entitled_file["id"],
                        reviewed_at,
                        reviewer_user_id,
                        reviewed_at,
                        approval_source,
                        reviewed_at,
                        request_id,
                    ),
                )
                fulfilled = self._load_required(conn, "requests", request_id)
                conn.commit()
                return dict(fulfilled), None, False

            request_process = conn.execute(
                """
                SELECT * FROM request_processes
                WHERE book_id = ? AND content_type = ?
                  AND status IN (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_row["book_id"],
                    request_row["content_type"],
                    *ACTIVE_REQUEST_PROCESS_STATUSES,
                ),
            ).fetchone()
            created = request_process is None
            if request_process is None:
                cursor = conn.execute(
                    """
                    INSERT INTO request_processes (
                        book_id, content_type, profile_id, profile_name, profile_hash, profile_snapshot_json,
                        status, monitoring_enabled, next_search_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, 'queued', 1, ?)
                    """,
                    (
                        request_row["book_id"],
                        request_row["content_type"],
                        request_row["profile_id"],
                        request_row["profile_name"],
                        request_row["profile_hash"],
                        request_row["profile_snapshot_json"],
                        reviewed_at,
                    ),
                )
                request_process = self._load_required(
                    conn,
                    "request_processes",
                    _required_lastrowid(cursor, "RequestProcess"),
                )

            pending = conn.execute(
                """SELECT * FROM requests
                   WHERE book_id=? AND content_type=? AND status='pending_approval'
                     AND (? = 0 OR id = ?)""",
                (
                    request_row["book_id"],
                    request_row["content_type"],
                    int(is_authorized_policy),
                    request_id,
                ),
            ).fetchall()
            for item in pending:
                conn.execute(
                    """INSERT INTO request_process_recipients(request_process_id,user_id,request_id,add_to_kobo_when_available)
                       VALUES(?,?,?,?) ON CONFLICT(request_process_id,user_id,request_id) DO NOTHING""",
                    (
                        request_process["id"],
                        item["user_id"],
                        item["id"],
                        item["add_to_kobo_when_available"],
                    ),
                )
                conn.execute(
                    """UPDATE requests SET status='approved', request_process_id=?, reviewed_by=?,
                       reviewed_at=?, approval_source=?, updated_at=? WHERE id=?""",
                    (
                        request_process["id"],
                        reviewer_user_id,
                        reviewed_at,
                        approval_source,
                        reviewed_at,
                        item["id"],
                    ),
                )
            approved = self._load_required(conn, "requests", request_id)
            conn.commit()
            return dict(approved), dict(request_process), created
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def complete_request_process(
        self,
        request_process_id: int,
        *,
        file_id: int,
    ) -> list[int]:
        """Grant every recipient and fulfil every request atomically."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            request_process = self._load_required(conn, "request_processes", request_process_id)
            file_row = conn.execute(
                "SELECT * FROM book_files WHERE id = ? AND import_status = 'imported'",
                (file_id,),
            ).fetchone()
            if (
                file_row is None
                or file_row["book_id"] != request_process["book_id"]
                or file_row["content_type"] != request_process["content_type"]
            ):
                _raise_domain(
                    "Imported file does not match the requested Book and content type",
                    code="invalid_fulfilment_file",
                    status_code=409,
                )
            recipients = conn.execute(
                "SELECT * FROM request_process_recipients WHERE request_process_id = ?",
                (request_process_id,),
            ).fetchall()
            fulfilled_ids: list[int] = []
            completed_at = datetime.now(UTC).isoformat(timespec="seconds")
            for recipient in recipients:
                conn.execute(
                    """
                    INSERT INTO user_file_entitlements (user_id, file_id, request_id, grant_source)
                    VALUES (?, ?, ?, 'request')
                    ON CONFLICT(user_id, file_id) DO NOTHING
                    """,
                    (recipient["user_id"], file_id, recipient["request_id"]),
                )
                conn.execute(
                    """
                    INSERT INTO user_library (user_id, book_id)
                    VALUES (?, ?)
                    ON CONFLICT(user_id, book_id) DO NOTHING
                    """,
                    (recipient["user_id"], request_process["book_id"]),
                )
                cursor = conn.execute(
                    """
                    UPDATE requests
                    SET status = 'fulfilled', fulfilled_file_id = ?, fulfilled_at = ?,
                        updated_at = ?
                    WHERE id = ? AND status = 'approved'
                    """,
                    (file_id, completed_at, completed_at, recipient["request_id"]),
                )
                if cursor.rowcount:
                    fulfilled_ids.append(recipient["request_id"])

            conn.execute(
                """
                UPDATE request_processes
                SET status = 'completed', monitoring_enabled = 0, completed_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (completed_at, completed_at, request_process_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        else:
            return fulfilled_ids
        finally:
            conn.close()
