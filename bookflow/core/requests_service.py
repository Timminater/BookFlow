"""Validation shared by the Book-level Request API."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if TYPE_CHECKING:
    import sqlite3

MAX_REQUEST_NOTE_LENGTH = 1000
MAX_OPEN_REQUESTS_PER_USER = 10
MAX_REQUESTS_PER_USER_PER_DAY = 10


class RequestServiceError(ValueError):
    """Structured API validation error."""

    def __init__(self, message: str, *, status_code: int = 400, code: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


def _request_day_bounds_utc(now: datetime | None = None) -> tuple[str, str]:
    """Return the server-local calendar day as UTC SQLite boundaries."""
    timezone_name = os.environ.get("TZ", "UTC")
    try:
        timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        timezone = ZoneInfo("UTC")
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    local = current.astimezone(timezone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return (
        start.astimezone(UTC).isoformat(timespec="seconds"),
        end.astimezone(UTC).isoformat(timespec="seconds"),
    )


def enforce_request_limits(conn: sqlite3.Connection, *, user_id: int) -> None:
    """Enforce aggregate per-user limits across current and legacy request APIs."""
    open_count = int(
        conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM requests
                 WHERE user_id = ? AND status IN ('pending_approval', 'approved'))
              + (SELECT COUNT(*) FROM download_requests
                 WHERE user_id = ? AND status = 'pending'
                   AND NOT EXISTS (
                       SELECT 1 FROM requests
                       WHERE requests.legacy_request_id = download_requests.id
                   ))
            """,
            (user_id, user_id),
        ).fetchone()[0]
    )
    if open_count >= MAX_OPEN_REQUESTS_PER_USER:
        raise RequestServiceError(
            f"A user can have at most {MAX_OPEN_REQUESTS_PER_USER} open requests",
            status_code=429,
            code="open_request_limit_reached",
        )

    day_start, day_end = _request_day_bounds_utc()
    daily_count = int(
        conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM requests
                 WHERE user_id = ?
                   AND datetime(created_at) >= datetime(?)
                   AND datetime(created_at) < datetime(?))
              + (SELECT COUNT(*) FROM download_requests
                 WHERE user_id = ?
                   AND datetime(created_at) >= datetime(?)
                   AND datetime(created_at) < datetime(?)
                   AND NOT EXISTS (
                       SELECT 1 FROM requests
                       WHERE requests.legacy_request_id = download_requests.id
                   ))
            """,
            (user_id, day_start, day_end, user_id, day_start, day_end),
        ).fetchone()[0]
    )
    if daily_count >= MAX_REQUESTS_PER_USER_PER_DAY:
        raise RequestServiceError(
            f"A user can submit at most {MAX_REQUESTS_PER_USER_PER_DAY} requests per day",
            status_code=429,
            code="daily_request_limit_reached",
        )


def normalize_note(note: object) -> str | None:
    """Validate a requester or administrator note."""
    if note is None:
        return None
    if not isinstance(note, str):
        raise RequestServiceError("note must be a string")
    normalized = note.strip()
    if len(normalized) > MAX_REQUEST_NOTE_LENGTH:
        raise RequestServiceError(f"note must be <= {MAX_REQUEST_NOTE_LENGTH} characters")
    return normalized or None


def sync_delivery_states_from_queue_status(
    _user_db: Any, *, queue_status: dict[str, dict[str, Any]], user_id: int | None = None
) -> list[dict[str, Any]]:
    """Retain Activity's callback seam until its Book-level rewrite.

    Requests are availability signals, not download progress, so queue state
    never changes them. File finalization is the only fulfilment transition.
    """
    del queue_status, user_id
    return []
