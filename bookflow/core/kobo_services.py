"""Token, synchronization and entitlement services for Kobo readers."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from bookflow.core.kobo_db import KOB0_DB_SQL, KOBO_AUTH_RATE_LIMIT_SQL

_CURSOR_VERSION = 1
_SYNC_BATCH_SIZE = 100


class KoboError(RuntimeError):
    """Base error for the Kobo domain, safe to map to a non-sensitive response."""


class KoboAuthenticationError(KoboError):
    """Raised for absent, revoked or mismatching Kobo tokens."""


class KoboRateLimitError(KoboError):
    """Raised when a token/IP pair exceeds the persistent request budget."""


class KoboCursorError(KoboError):
    """Raised when a cursor is malformed, forged or belongs to another user."""


class KoboEntitlementError(KoboError):
    """Raised when a user attempts to expose a non-entitled Kobo file."""


def redact_kobo_token(token: str | None) -> str:
    """Return a safe token label for diagnostics without leaking bearer credentials."""
    if not token:
        return "<missing>"
    return f"{token[:6]}…<redacted>"


@dataclass(frozen=True)
class IssuedKoboToken:
    """One-time plaintext token result; only its hash is persisted."""

    token_id: int
    token: str
    prefix: str


@dataclass(frozen=True)
class AuthenticatedKoboToken:
    """Authenticated token identity, never containing the original token string."""

    token_id: int
    user_id: int
    prefix: str


@dataclass(frozen=True)
class KoboSyncBatch:
    """One deterministic user-bound page of append-only Kobo changes."""

    changes: list[dict[str, Any]]
    cursor: str | None
    has_more: bool
    high_water: int
    since_revision: int = 0


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


class _KoboSQLiteService:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def initialize(self) -> None:
        """Install the isolated Kobo schema; callers can invoke this idempotently."""
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript(KOB0_DB_SQL + KOBO_AUTH_RATE_LIMIT_SQL)
            finally:
                conn.close()


class TokenService(_KoboSQLiteService):
    """Issue and authenticate HMAC-protected Kobo bearer tokens."""

    def __init__(
        self,
        db_path: str,
        *,
        pepper: str | bytes,
        max_requests_per_window: int = 600,
        window: timedelta = timedelta(minutes=1),
    ) -> None:
        super().__init__(db_path)
        if isinstance(pepper, str):
            pepper = pepper.encode("utf-8")
        if not pepper:
            raise ValueError("Kobo token pepper must not be empty")
        if max_requests_per_window < 1 or window <= timedelta(0):
            raise ValueError("Kobo rate-limit settings must be positive")
        self._pepper = pepper
        self._max_requests_per_window = max_requests_per_window
        self._window = window

    def _hash_token(self, token: str) -> str:
        return hmac.new(self._pepper, token.encode("ascii"), hashlib.sha256).hexdigest()

    @staticmethod
    def _validate_source_ip(source_ip: str) -> str:
        normalized = source_ip.strip()
        if not normalized:
            raise ValueError("source_ip is required")
        return normalized

    def issue_token(self, *, user_id: int, device_label: str | None = None) -> IssuedKoboToken:
        """Create a 32-random-byte token and return plaintext exactly once."""
        normalized_label = (device_label or "").strip()[:100] or None
        token = secrets.token_urlsafe(32)
        prefix = token[:12]
        token_hash = self._hash_token(token)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    """
                    UPDATE kobo_tokens
                    SET revoked_at = ?
                    WHERE user_id = ? AND revoked_at IS NULL
                    """,
                    (_iso(_now()), user_id),
                )
                cursor = conn.execute(
                    """
                    INSERT INTO kobo_tokens (user_id, token_prefix, token_hash, device_label)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, prefix, token_hash, normalized_label),
                )
                conn.commit()
                if cursor.lastrowid is None:
                    raise KoboError("Unable to issue Kobo token")
                return IssuedKoboToken(token_id=int(cursor.lastrowid), token=token, prefix=prefix)
            except sqlite3.Error as exc:
                conn.rollback()
                raise KoboError("Unable to issue Kobo token") from exc
            finally:
                conn.close()

    def revoke_token(self, *, user_id: int, token_id: int) -> bool:
        """Immediately revoke one user-owned token without exposing its value."""
        with self._lock:
            conn = self._connect()
            try:
                cursor = conn.execute(
                    """
                    UPDATE kobo_tokens SET revoked_at = ?
                    WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                    """,
                    (_iso(_now()), token_id, user_id),
                )
                conn.commit()
                return cursor.rowcount == 1
            finally:
                conn.close()

    def rotate_token(self, *, user_id: int, token_id: int) -> IssuedKoboToken:
        """Revoke an old token then issue a new plaintext token once.

        Device labels are user-facing identity, not credential material, so a
        rotation deliberately carries the label to the replacement token.
        """
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                existing = conn.execute(
                    """SELECT device_label FROM kobo_tokens
                       WHERE id = ? AND user_id = ? AND revoked_at IS NULL""",
                    (token_id, user_id),
                ).fetchone()
                if existing is None:
                    raise KoboAuthenticationError("Kobo token cannot be rotated")
                revoked = conn.execute(
                    """
                    UPDATE kobo_tokens SET revoked_at = ?
                    WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                    """,
                    (_iso(_now()), token_id, user_id),
                )
                if revoked.rowcount != 1:
                    raise KoboAuthenticationError("Kobo token cannot be rotated")
                token = secrets.token_urlsafe(32)
                prefix = token[:12]
                cursor = conn.execute(
                    """
                    INSERT INTO kobo_tokens (user_id, token_prefix, token_hash, device_label)
                    VALUES (?, ?, ?, ?)
                    """,
                    (user_id, prefix, self._hash_token(token), existing["device_label"]),
                )
                conn.commit()
                if cursor.lastrowid is None:
                    raise KoboError("Unable to rotate Kobo token")
                return IssuedKoboToken(token_id=int(cursor.lastrowid), token=token, prefix=prefix)
            except sqlite3.Error:
                conn.rollback()
                raise
            finally:
                conn.close()

    def authenticate(self, *, token: str, source_ip: str) -> AuthenticatedKoboToken:
        """Authenticate with constant-time HMAC comparison and persistent limits."""
        normalized_ip = self._validate_source_ip(source_ip)
        identity_hash = hmac.new(
            self._pepper,
            f"kobo-auth:{token[:12]}".encode(),
            hashlib.sha256,
        ).hexdigest()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT id, user_id, token_prefix, token_hash, revoked_at "
                    "FROM kobo_tokens WHERE token_prefix = ?",
                    (token[:12],),
                ).fetchone()
                valid = (
                    row is not None
                    and row["revoked_at"] is None
                    and len(token) >= 12
                    and token.isascii()
                    and hmac.compare_digest(str(row["token_hash"]), self._hash_token(token))
                )
                rate_limited = self._consume_auth_rate_limit(
                    conn,
                    token_identity_hash=identity_hash,
                    source_ip=normalized_ip,
                    limit=self._max_requests_per_window
                    if valid
                    else min(60, self._max_requests_per_window),
                )
                conn.commit()
                if rate_limited:
                    raise KoboRateLimitError("Kobo token rate limit exceeded")
                if not valid or row is None:
                    raise KoboAuthenticationError("Invalid Kobo token")
                conn.execute(
                    "UPDATE kobo_tokens SET last_used_at = ? WHERE id = ?",
                    (_iso(_now()), int(row["id"])),
                )
                conn.commit()
                return AuthenticatedKoboToken(
                    token_id=int(row["id"]),
                    user_id=int(row["user_id"]),
                    prefix=str(row["token_prefix"]),
                )
            except KoboError:
                conn.rollback()
                raise
            except sqlite3.Error as exc:
                conn.rollback()
                raise KoboError("Kobo token lookup failed") from exc
            finally:
                conn.close()

    def _consume_auth_rate_limit(
        self,
        conn: sqlite3.Connection,
        *,
        token_identity_hash: str,
        source_ip: str,
        limit: int,
    ) -> bool:
        now = _now()
        row = conn.execute(
            """
            SELECT window_started, request_count, blocked_until
            FROM kobo_auth_rate_limits
            WHERE token_identity_hash = ? AND source_ip = ?
            """,
            (token_identity_hash, source_ip),
        ).fetchone()
        if (
            row is not None
            and row["blocked_until"] is not None
            and datetime.fromisoformat(str(row["blocked_until"])) > now
        ):
            return True
        if row is None or now - datetime.fromisoformat(str(row["window_started"])) >= self._window:
            request_count = 1
            window_started = now
        else:
            request_count = int(row["request_count"]) + 1
            window_started = datetime.fromisoformat(str(row["window_started"]))
        blocked_until = now + self._window if request_count > limit else None
        conn.execute(
            """
            INSERT INTO kobo_auth_rate_limits (
                token_identity_hash, source_ip, window_started, request_count,
                blocked_until, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(token_identity_hash, source_ip) DO UPDATE SET
                window_started = excluded.window_started,
                request_count = excluded.request_count,
                blocked_until = excluded.blocked_until,
                updated_at = excluded.updated_at
            """,
            (
                token_identity_hash,
                source_ip,
                _iso(window_started),
                request_count,
                _iso(blocked_until) if blocked_until else None,
                _iso(now),
            ),
        )
        return blocked_until is not None

    def _consume_rate_limit(
        self, conn: sqlite3.Connection, *, token_id: int, source_ip: str
    ) -> bool:
        now = _now()
        row = conn.execute(
            """
            SELECT window_started, request_count, blocked_until
            FROM kobo_token_rate_limits WHERE token_id = ? AND source_ip = ?
            """,
            (token_id, source_ip),
        ).fetchone()
        if row is not None and row["blocked_until"] is not None:
            blocked_until = datetime.fromisoformat(str(row["blocked_until"]))
            if blocked_until > now:
                return True
        if row is None or now - datetime.fromisoformat(str(row["window_started"])) >= self._window:
            request_count = 1
            window_started = now
        else:
            request_count = int(row["request_count"]) + 1
            window_started = datetime.fromisoformat(str(row["window_started"]))
        blocked_until = (
            now + self._window if request_count > self._max_requests_per_window else None
        )
        conn.execute(
            """
            INSERT INTO kobo_token_rate_limits (
                token_id, source_ip, window_started, request_count, blocked_until, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(token_id, source_ip) DO UPDATE SET
                window_started = excluded.window_started,
                request_count = excluded.request_count,
                blocked_until = excluded.blocked_until,
                updated_at = excluded.updated_at
            """,
            (
                token_id,
                source_ip,
                _iso(window_started),
                request_count,
                _iso(blocked_until) if blocked_until else None,
                _iso(now),
            ),
        )
        return blocked_until is not None


class KoboSyncService(_KoboSQLiteService):
    """Manage per-user Kobo desired state and deterministic cursor batches."""

    def __init__(self, db_path: str, *, cursor_secret: str | bytes) -> None:
        super().__init__(db_path)
        if isinstance(cursor_secret, str):
            cursor_secret = cursor_secret.encode("utf-8")
        if not cursor_secret:
            raise ValueError("Kobo cursor secret must not be empty")
        self._cursor_secret = cursor_secret

    def enable_user(self, *, user_id: int, enabled: bool = True) -> None:
        """Create or update the user's isolated Kobo settings record."""
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    """
                    INSERT INTO user_kobo_settings (user_id, enabled, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET enabled = excluded.enabled,
                        updated_at = excluded.updated_at
                    """,
                    (user_id, int(enabled), _iso(_now())),
                )
                conn.commit()
            finally:
                conn.close()

    def set_book(
        self,
        *,
        user_id: int,
        book_id: int,
        file_id: int,
        preferred_format: str | None = None,
        source: str = "manual",
        request_id: int | None = None,
    ) -> int:
        """Upsert an entitled desired book and append a monotone change revision."""
        return self._write_change(
            user_id=user_id,
            book_id=book_id,
            file_id=file_id,
            desired_state="active",
            preferred_format=preferred_format,
            source=source,
            request_id=request_id,
        )

    def delete_book(self, *, user_id: int, book_id: int, source: str = "user") -> int:
        """Persist deletion intent as an append-only change, never remove history."""
        return self._write_change(
            user_id=user_id,
            book_id=book_id,
            file_id=None,
            desired_state="deleted",
            preferred_format=None,
            source=source,
            request_id=None,
        )

    def _write_change(
        self,
        *,
        user_id: int,
        book_id: int,
        file_id: int | None,
        desired_state: str,
        preferred_format: str | None,
        source: str,
        request_id: int | None,
    ) -> int:
        if desired_state == "active" and file_id is None:
            raise ValueError("An active Kobo book requires file_id")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                settings = conn.execute(
                    "SELECT enabled, current_revision FROM user_kobo_settings WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if settings is None or not bool(settings["enabled"]):
                    raise KoboEntitlementError("Kobo is not enabled for this user")
                self._require_book(conn, book_id)
                if file_id is not None:
                    self._require_entitled_file(
                        conn, user_id=user_id, book_id=book_id, file_id=file_id
                    )
                revision = int(settings["current_revision"]) + 1
                now = _iso(_now())
                conn.execute(
                    """
                    INSERT INTO user_kobo_books (
                        user_id, book_id, file_id, desired_state, preferred_format,
                        source, request_id, revision, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(user_id, book_id) DO UPDATE SET
                        file_id = excluded.file_id,
                        desired_state = excluded.desired_state,
                        preferred_format = excluded.preferred_format,
                        source = excluded.source,
                        request_id = excluded.request_id,
                        revision = excluded.revision,
                        updated_at = excluded.updated_at
                    """,
                    (
                        user_id,
                        book_id,
                        file_id,
                        desired_state,
                        preferred_format,
                        source,
                        request_id,
                        revision,
                        now,
                    ),
                )
                change_type = "upsert" if desired_state == "active" else "delete"
                payload = json.dumps(
                    {
                        "desired_state": desired_state,
                        "preferred_format": preferred_format,
                        "source": source,
                    },
                    sort_keys=True,
                )
                conn.execute(
                    """
                    INSERT INTO user_kobo_changes (
                        user_id, revision, change_type, book_id, file_id, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (user_id, revision, change_type, book_id, file_id, payload, now),
                )
                conn.execute(
                    """
                    UPDATE user_kobo_settings
                    SET current_revision = ?, updated_at = ? WHERE user_id = ?
                    """,
                    (revision, now, user_id),
                )
                conn.commit()
            except KoboError:
                conn.rollback()
                raise
            except sqlite3.Error as exc:
                conn.rollback()
                raise KoboError("Unable to persist Kobo change") from exc
            else:
                return revision
            finally:
                conn.close()

    @staticmethod
    def _require_book(conn: sqlite3.Connection, book_id: int) -> None:
        if conn.execute("SELECT 1 FROM books WHERE id = ?", (book_id,)).fetchone() is None:
            raise KoboEntitlementError("Book not found")

    @staticmethod
    def _require_entitled_file(
        conn: sqlite3.Connection, *, user_id: int, book_id: int, file_id: int
    ) -> None:
        row = conn.execute(
            """
            SELECT 1 FROM book_files bf
            INNER JOIN user_file_entitlements ufe ON ufe.file_id = bf.id
            WHERE bf.id = ? AND bf.book_id = ? AND ufe.user_id = ?
              AND bf.import_status = 'imported'
            """,
            (file_id, book_id, user_id),
        ).fetchone()
        if row is None:
            raise KoboEntitlementError("File is not entitled for this Kobo user")

    def sync(
        self, *, user_id: int, cursor: str | None = None, persistent: bool = False
    ) -> KoboSyncBatch:
        """Return a deterministic initial, incremental or resumed batch of 100 changes."""
        with self._lock:
            conn = self._connect()
            try:
                settings = conn.execute(
                    "SELECT enabled, current_revision FROM user_kobo_settings WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if settings is None or not bool(settings["enabled"]):
                    raise KoboEntitlementError("Kobo is not enabled for this user")
                if cursor is None:
                    high_water = int(settings["current_revision"])
                    last_revision = 0
                else:
                    high_water, last_revision = self._decode_cursor(cursor, user_id=user_id)
                    if persistent and last_revision == high_water:
                        high_water = int(settings["current_revision"])
                    if last_revision > high_water:
                        raise KoboCursorError("Sync cursor is ahead of the library")
                # Progress has its own immutable table so older installations
                # retain the strict CHECK constraint on user_kobo_changes.  The
                # revision stream remains one globally ordered Kobo changelog.
                rows = conn.execute(
                    """
                    SELECT revision, change_type, book_id, file_id, payload_json, created_at
                    FROM user_kobo_changes
                    WHERE user_id = ? AND revision > ? AND revision <= ?
                    UNION ALL
                    SELECT revision, 'progress' AS change_type, book_id, NULL AS file_id,
                           json_object('progress', progress, 'active', active, 'source', source) AS payload_json,
                           created_at
                    FROM user_kobo_progress_changes
                    WHERE user_id = ? AND revision > ? AND revision <= ?
                    ORDER BY revision ASC LIMIT ?
                    """,
                    (
                        user_id,
                        last_revision,
                        high_water,
                        user_id,
                        last_revision,
                        high_water,
                        _SYNC_BATCH_SIZE + 1,
                    ),
                ).fetchall()
                has_more = len(rows) > _SYNC_BATCH_SIZE
                selected = rows[:_SYNC_BATCH_SIZE]
                changes = [self._serialize_change(row) for row in selected]
                next_cursor = None
                if persistent or (selected and has_more):
                    next_cursor = self._encode_cursor(
                        user_id=user_id,
                        high_water=high_water,
                        last_revision=int(selected[-1]["revision"]) if has_more else high_water,
                    )
                return KoboSyncBatch(
                    changes=changes,
                    cursor=next_cursor,
                    has_more=has_more,
                    high_water=high_water,
                    since_revision=last_revision,
                )
            finally:
                conn.close()

    @staticmethod
    def _serialize_change(row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(str(row["payload_json"]))
        return {
            "revision": int(row["revision"]),
            "type": str(row["change_type"]),
            "book_id": int(row["book_id"]),
            "file_id": int(row["file_id"]) if row["file_id"] is not None else None,
            "payload": payload,
            "created_at": str(row["created_at"]),
        }

    def _encode_cursor(self, *, user_id: int, high_water: int, last_revision: int) -> str:
        payload = json.dumps(
            {
                "v": _CURSOR_VERSION,
                "user": user_id,
                "high_water": high_water,
                "last_revision": last_revision,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.new(self._cursor_secret, payload, hashlib.sha256).digest()
        return f"{self._base64(payload)}.{self._base64(signature)}"

    def _decode_cursor(self, cursor: str, *, user_id: int) -> tuple[int, int]:
        try:
            raw_payload, raw_signature = cursor.split(".", 1)
            payload = self._unbase64(raw_payload)
            signature = self._unbase64(raw_signature)
            expected_signature = hmac.new(self._cursor_secret, payload, hashlib.sha256).digest()
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
            raise KoboCursorError("Invalid Kobo cursor") from exc
        if not hmac.compare_digest(signature, expected_signature):
            raise KoboCursorError("Invalid Kobo cursor")
        if (
            not isinstance(decoded, dict)
            or decoded.get("v") != _CURSOR_VERSION
            or decoded.get("user") != user_id
            or not isinstance(decoded.get("high_water"), int)
            or not isinstance(decoded.get("last_revision"), int)
        ):
            raise KoboCursorError("Invalid Kobo cursor")
        high_water = int(decoded["high_water"])
        last_revision = int(decoded["last_revision"])
        if high_water < 0 or last_revision < 0 or last_revision > high_water:
            raise KoboCursorError("Invalid Kobo cursor")
        return high_water, last_revision

    @staticmethod
    def _base64(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @staticmethod
    def _unbase64(value: str) -> bytes:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))

    def get_entitled_file(self, *, user_id: int, file_id: int) -> dict[str, Any]:
        """Return only an active, desired, explicitly entitled central file."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT bf.*, b.title, b.author, b.cover_url, b.id AS book_id
                    FROM user_kobo_books ukb
                    INNER JOIN book_files bf ON bf.id = ukb.file_id
                    INNER JOIN user_file_entitlements ufe ON ufe.file_id = bf.id
                    INNER JOIN books b ON b.id = bf.book_id
                    WHERE ukb.user_id = ? AND ukb.file_id = ? AND ukb.desired_state = 'active'
                      AND ufe.user_id = ? AND bf.import_status = 'imported'
                    """,
                    (user_id, file_id, user_id),
                ).fetchone()
                if row is None:
                    raise KoboEntitlementError("Kobo file is unavailable")
                return dict(row)
            finally:
                conn.close()

    def get_reading_state(self, *, user_id: int, book_id: int) -> dict[str, Any] | None:
        """Return personal state only if this exact Kobo user can access the book."""
        with self._lock:
            conn = self._connect()
            try:
                self._require_active_entitled_kobo_book(conn, user_id=user_id, book_id=book_id)
                row = conn.execute(
                    "SELECT * FROM user_reading_progress WHERE user_id = ? AND book_id = ?",
                    (user_id, book_id),
                ).fetchone()
                if row is None:
                    return None
                state = dict(row)
                device_row = conn.execute(
                    "SELECT state_json FROM kobo_reading_states WHERE user_id = ? AND book_id = ?",
                    (user_id, book_id),
                ).fetchone()
                if device_row:
                    state["device_state"] = json.loads(device_row["state_json"])
                return state
            finally:
                conn.close()

    def set_reading_state(
        self,
        *,
        user_id: int,
        book_id: int,
        progress: float,
        reset: bool = False,
        device_state: dict[str, Any] | None = None,
        locator: dict[str, Any] | None = None,
        file_id: int | None = None,
    ) -> dict[str, Any]:
        """Resolve device timestamps atomically and journal accepted reading updates."""
        if (
            isinstance(progress, bool)
            or not isinstance(progress, (int, float))
            or not 0 <= float(progress) <= 100
        ):
            raise ValueError("progress must be a number from 0 to 100")
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                settings = conn.execute(
                    "SELECT enabled, current_revision FROM user_kobo_settings WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                if settings is None or not bool(settings["enabled"]):
                    raise KoboEntitlementError("Kobo is not enabled for this user")  # noqa: TRY301
                self._require_active_entitled_kobo_book(conn, user_id=user_id, book_id=book_id)
                if file_id is not None:
                    self._require_entitled_file(
                        conn, user_id=user_id, book_id=book_id, file_id=file_id
                    )
                old = conn.execute(
                    "SELECT * FROM user_reading_progress WHERE user_id = ? AND book_id = ?",
                    (user_id, book_id),
                ).fetchone()
                observed_at = None
                if device_state is not None:
                    bookmark = device_state.get("CurrentBookmark") or {}
                    raw_time = bookmark.get("LastModified") or device_state.get("LastModified")
                    if raw_time:
                        observed_at = datetime.fromisoformat(str(raw_time))
                        if observed_at.tzinfo is None:
                            observed_at = observed_at.replace(tzinfo=UTC)
                        observed_at = min(observed_at.astimezone(UTC), _now())
                    if old is not None:
                        previous_time = datetime.fromisoformat(
                            str(old["reader_updated_at"] or old["updated_at"])
                        )
                        if previous_time.tzinfo is None:
                            previous_time = previous_time.replace(tzinfo=UTC)
                        stale = observed_at is not None and observed_at <= previous_time
                        legacy_backwards = observed_at is None and progress < float(old["progress"])
                        if stale or legacy_backwards:
                            conn.rollback()
                            return dict(old)
                if (
                    old is not None
                    and float(progress) < float(old["progress"])
                    and not reset
                    and observed_at is None
                ):
                    raise ValueError("Progress may only decrease after an explicit reset")  # noqa: TRY301
                revision = int(settings["current_revision"]) + 1
                active = int(float(progress) < 100)
                local_revision = int(old["revision"]) + 1 if old else 1
                now = _iso(_now())
                conn.execute(
                    """INSERT INTO user_reading_progress(user_id, book_id, progress, active, source, revision, updated_at,
                    reader_updated_at, locator_json, file_id)
                    VALUES (?, ?, ?, ?, 'kobo', ?, ?, ?, ?, ?) ON CONFLICT(user_id, book_id) DO UPDATE SET
                    progress=excluded.progress, active=excluded.active, source='kobo', revision=excluded.revision,
                    updated_at=excluded.updated_at, reader_updated_at=excluded.reader_updated_at,
                    locator_json=excluded.locator_json, file_id=excluded.file_id""",
                    (
                        user_id,
                        book_id,
                        float(progress),
                        active,
                        local_revision,
                        now,
                        _iso(observed_at) if observed_at else now,
                        json.dumps(locator) if locator is not None else None,
                        file_id,
                    ),
                )
                conn.execute(
                    """INSERT INTO user_kobo_progress_changes(user_id, revision, book_id, progress, active, source, created_at)
                    VALUES (?, ?, ?, ?, ?, 'kobo', ?)""",
                    (user_id, revision, book_id, float(progress), active, now),
                )
                conn.execute(
                    "UPDATE user_kobo_settings SET current_revision = ?, updated_at = ? WHERE user_id = ?",
                    (revision, now, user_id),
                )
                if device_state is not None:
                    conn.execute(
                        """INSERT INTO kobo_reading_states(user_id, book_id, state_json)
                           VALUES (?, ?, ?) ON CONFLICT(user_id, book_id)
                           DO UPDATE SET state_json = excluded.state_json""",
                        (user_id, book_id, json.dumps(device_state)),
                    )
                row = conn.execute(
                    "SELECT * FROM user_reading_progress WHERE user_id = ? AND book_id = ?",
                    (user_id, book_id),
                ).fetchone()
                conn.commit()
                return dict(row)
            except KoboEntitlementError, ValueError:
                conn.rollback()
                raise
            except sqlite3.Error as exc:
                conn.rollback()
                raise KoboError("Unable to persist Kobo reading state") from exc
            finally:
                conn.close()

    @staticmethod
    def _require_active_entitled_kobo_book(
        conn: sqlite3.Connection, *, user_id: int, book_id: int
    ) -> None:
        row = conn.execute(
            """SELECT 1 FROM user_kobo_books ukb JOIN book_files bf ON bf.id = ukb.file_id
                JOIN user_file_entitlements ufe ON ufe.file_id = bf.id
                WHERE ukb.user_id = ? AND ukb.book_id = ? AND ukb.desired_state = 'active'
                  AND ufe.user_id = ? AND bf.import_status = 'imported'""",
            (user_id, book_id, user_id),
        ).fetchone()
        if row is None:
            raise KoboEntitlementError("Kobo book is unavailable")

    def get_book_metadata(self, *, user_id: int, book_id: int) -> dict[str, Any]:
        """Return metadata only when the Book is active and entitled for this user."""
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    """
                    SELECT b.id, b.title, b.author, b.cover_url, b.language, b.publish_year,
                           bf.id AS file_id, bf.file_format, bf.size_bytes, bf.sha256,
                           ukb.updated_at, ukb.revision,
                           (SELECT MIN(revision) FROM user_kobo_changes
                            WHERE user_id = ukb.user_id AND book_id = ukb.book_id) AS first_revision
                    FROM user_kobo_books ukb
                    INNER JOIN books b ON b.id = ukb.book_id
                    INNER JOIN book_files bf ON bf.id = ukb.file_id
                    INNER JOIN user_file_entitlements ufe ON ufe.file_id = bf.id
                    WHERE ukb.user_id = ? AND ukb.book_id = ? AND ukb.desired_state = 'active'
                      AND ufe.user_id = ? AND bf.import_status = 'imported'
                    """,
                    (user_id, book_id, user_id),
                ).fetchone()
                if row is None:
                    raise KoboEntitlementError("Kobo book is unavailable")
                return dict(row)
            finally:
                conn.close()
