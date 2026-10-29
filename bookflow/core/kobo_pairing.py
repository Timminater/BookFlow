"""Short-lived, self-describing pairing codes for the Windows Kobo patcher."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from bookflow.core.kobo_services import TokenService


class PairingError(RuntimeError):
    """Safe pairing failure."""


class PairingRateLimitError(PairingError):
    """Raised when an anonymous pairing-code guess exceeds its budget."""


# Keep the explicit Kobo-prefixed name used by API consumers during the
# initial rollout while the shorter internal name remains the implementation.
KoboPairingRateLimitError = PairingRateLimitError


@dataclass(frozen=True, slots=True)
class PairingClaim:
    session: str
    origin: str
    expires_at: str


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


class KoboPairingService:
    """Issue, claim, complete, and revoke one-time patcher pairing sessions."""

    PROTOCOL_VERSION = 1

    def __init__(
        self,
        db_path: str,
        *,
        secret: str | bytes,
        token_service: TokenService,
        lifetime: timedelta = timedelta(minutes=10),
        max_claims_per_window: int = 5,
        claim_window: timedelta = timedelta(minutes=1),
    ) -> None:
        self._db_path = db_path
        self._secret = secret.encode() if isinstance(secret, str) else secret
        self._token_service = token_service
        self._lifetime = lifetime
        self._max_claims_per_window = max_claims_per_window
        self._claim_window = claim_window
        self._lock = threading.Lock()
        if len(self._secret) < 32:
            raise ValueError("pairing secret must contain at least 32 bytes")
        if max_claims_per_window < 1 or claim_window <= timedelta():
            raise ValueError("pairing rate-limit settings must be positive")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _validate_origin(origin: str) -> str:
        normalized = origin.rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise PairingError("Invalid BookFlow origin")
        if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise PairingError("BookFlow origin must not include a path or credentials")
        if parsed.scheme == "http":
            hostname = parsed.hostname or ""
            allowed = hostname.casefold() == "localhost"
            try:
                address = ipaddress.ip_address(hostname)
                allowed = allowed or address.is_private or address.is_loopback
            except ValueError:
                pass
            if not allowed:
                raise PairingError("Public BookFlow origins require HTTPS")
        return normalized

    def create_code(self, *, user_id: int, origin: str) -> dict[str, Any]:
        normalized_origin = self._validate_origin(origin)
        pair_id = secrets.token_urlsafe(18)
        nonce = secrets.token_urlsafe(24)
        expires = _utcnow() + self._lifetime
        payload = {
            "origin": normalized_origin,
            "pairing_id": pair_id,
            "nonce": nonce,
            "exp": int(expires.timestamp()),
            "protocol_version": self.PROTOCOL_VERSION,
        }
        encoded = _b64encode(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )
        signature = _b64encode(
            hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
        )
        nonce_hash = hashlib.sha256(nonce.encode("ascii")).hexdigest()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO kobo_pairing_codes
                    (id, user_id, nonce_hash, origin, expires_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (pair_id, user_id, nonce_hash, normalized_origin, _iso(expires)),
            )
        return {
            "code": f"SM1.{encoded}.{signature}",
            "expires_at": _iso(expires),
            "protocol_version": self.PROTOCOL_VERSION,
        }

    def _decode(self, code: str) -> dict[str, Any]:
        try:
            prefix, encoded, supplied_signature = code.strip().split(".", 2)
            expected_signature = _b64encode(
                hmac.new(self._secret, encoded.encode("ascii"), hashlib.sha256).digest()
            )
            if prefix != "SM1" or not hmac.compare_digest(supplied_signature, expected_signature):
                raise PairingError("Invalid pairing code")
            payload = json.loads(_b64decode(encoded))
            if (
                not isinstance(payload, dict)
                or int(payload.get("protocol_version", 0)) != self.PROTOCOL_VERSION
            ):
                raise PairingError("Unsupported pairing protocol")
            if int(payload["exp"]) <= int(_utcnow().timestamp()):
                raise PairingError("Pairing code expired")
            payload["origin"] = self._validate_origin(str(payload["origin"]))
        except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
            raise PairingError("Invalid pairing code") from exc
        else:
            return payload

    def claim(self, *, code: str, source_ip: str = "local") -> PairingClaim:
        normalized_ip = source_ip.strip()
        if not normalized_ip:
            raise ValueError("source_ip is required")
        # Hash the submitted representation before parsing it so malformed and
        # valid codes receive the exact same per-code/IP brute-force budget.
        identity_hash = hashlib.sha256(code.strip().encode("utf-8")).hexdigest()
        with self._lock, self._connect() as conn:
            if self._consume_claim_rate_limit(
                conn, code_identity_hash=identity_hash, source_ip=normalized_ip
            ):
                raise PairingRateLimitError("Too many pairing attempts")
        payload = self._decode(code)
        nonce_hash = hashlib.sha256(str(payload["nonce"]).encode("ascii")).hexdigest()
        session = secrets.token_urlsafe(32)
        session_hash = hashlib.sha256(session.encode("ascii")).hexdigest()
        expires = _utcnow() + self._lifetime
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE kobo_pairing_codes
                SET claimed_at = ?, session_hash = ?, session_expires_at = ?
                WHERE id = ? AND nonce_hash = ? AND consumed_at IS NULL
                  AND claimed_at IS NULL AND expires_at > ?
                """,
                (
                    _iso(_utcnow()),
                    session_hash,
                    _iso(expires),
                    str(payload["pairing_id"]),
                    nonce_hash,
                    _iso(_utcnow()),
                ),
            )
            if cursor.rowcount != 1:
                raise PairingError("Pairing code was already used or expired")
        return PairingClaim(
            session=session, origin=str(payload["origin"]), expires_at=_iso(expires)
        )

    def _consume_claim_rate_limit(
        self, conn: sqlite3.Connection, *, code_identity_hash: str, source_ip: str
    ) -> bool:
        """Consume both the code/IP and the aggregate-IP guess budgets."""
        # The sentinel cannot clash with a SHA-256 hex digest.  Keeping both
        # counters in the same table avoids a second migration while ensuring
        # a client cannot evade the IP budget by changing one character of a
        # malformed pairing code for every request.
        return self._consume_claim_rate_limit_row(
            conn, code_identity_hash=code_identity_hash, source_ip=source_ip
        ) or self._consume_claim_rate_limit_row(
            conn, code_identity_hash="__all__", source_ip=source_ip
        )

    def _consume_claim_rate_limit_row(
        self, conn: sqlite3.Connection, *, code_identity_hash: str, source_ip: str
    ) -> bool:
        now = _utcnow()
        row = conn.execute(
            """SELECT window_started, request_count, blocked_until FROM kobo_pairing_rate_limits
               WHERE code_identity_hash = ? AND source_ip = ?""",
            (code_identity_hash, source_ip),
        ).fetchone()
        if (
            row is not None
            and row["blocked_until"] is not None
            and datetime.fromisoformat(str(row["blocked_until"])) > now
        ):
            return True
        if (
            row is None
            or now - datetime.fromisoformat(str(row["window_started"])) >= self._claim_window
        ):
            window_started, request_count = now, 1
        else:
            window_started = datetime.fromisoformat(str(row["window_started"]))
            request_count = int(row["request_count"]) + 1
        blocked_until = (
            now + self._claim_window if request_count > self._max_claims_per_window else None
        )
        conn.execute(
            """INSERT INTO kobo_pairing_rate_limits
               (code_identity_hash, source_ip, window_started, request_count, blocked_until, updated_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(code_identity_hash, source_ip) DO UPDATE SET
                 window_started=excluded.window_started, request_count=excluded.request_count,
                 blocked_until=excluded.blocked_until, updated_at=excluded.updated_at""",
            (
                code_identity_hash,
                source_ip,
                _iso(window_started),
                request_count,
                _iso(blocked_until) if blocked_until else None,
                _iso(now),
            ),
        )
        return blocked_until is not None

    def complete(self, *, session: str, device_label: str | None) -> dict[str, Any]:
        session_hash = hashlib.sha256(session.encode("ascii")).hexdigest()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, user_id, origin FROM kobo_pairing_codes
                WHERE session_hash = ? AND consumed_at IS NULL
                  AND session_expires_at > ?
                """,
                (session_hash, _iso(_utcnow())),
            ).fetchone()
            if row is None:
                raise PairingError("Pairing session expired or invalid")
            issued = self._token_service.issue_token(
                user_id=int(row["user_id"]), device_label=device_label
            )
            cursor = conn.execute(
                """
                UPDATE kobo_pairing_codes SET consumed_at = ?, token_id = ?
                WHERE id = ? AND consumed_at IS NULL
                """,
                (_iso(_utcnow()), issued.token_id, str(row["id"])),
            )
            if cursor.rowcount != 1:
                self._token_service.revoke_token(
                    user_id=int(row["user_id"]), token_id=issued.token_id
                )
                raise PairingError("Pairing session was already completed")
            # Completing the explicitly confirmed wizard is opt-in to sync,
            # including for an account that has never opened Kobo settings.
            conn.execute(
                """INSERT INTO user_kobo_settings(user_id, enabled) VALUES (?, 1)
                   ON CONFLICT(user_id) DO UPDATE SET enabled=1""",
                (int(row["user_id"]),),
            )
        return {
            "token_id": issued.token_id,
            "api_endpoint": f"{str(row['origin'])}/api/kobo/{issued.token}",
            "protocol_version": self.PROTOCOL_VERSION,
            "shown_once": True,
        }

    def revoke_session(self, *, session: str) -> bool:
        session_hash = hashlib.sha256(session.encode("ascii")).hexdigest()
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT user_id, token_id FROM kobo_pairing_codes WHERE session_hash = ?",
                (session_hash,),
            ).fetchone()
            if row is None:
                return False
            if row["token_id"] is not None:
                self._token_service.revoke_token(
                    user_id=int(row["user_id"]), token_id=int(row["token_id"])
                )
            conn.execute(
                """
                UPDATE kobo_pairing_codes
                SET consumed_at = COALESCE(consumed_at, ?), session_hash = NULL
                WHERE session_hash = ?
                """,
                (_iso(_utcnow()), session_hash),
            )
            return True
