"""Pairing-code validation and the deliberately minimal HTTP pairing client."""

from __future__ import annotations

import base64
import ipaddress
import json
import time
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

# Cloudflare's Browser Integrity Check rejects urllib's default "Python-urllib"
# agent with error 1010, so BookFlow behind Cloudflare never sees the request.
USER_AGENT = "BookFlowKoboPatcher/1"


class PairingError(ValueError):
    """A pairing code or pairing-server exchange is invalid."""


def _b64decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:  # binascii error differs by Python release
        raise PairingError("The pairing code has an invalid payload") from exc


def origin_is_allowed(origin: str) -> bool:
    parsed = urlsplit(origin)
    if parsed.scheme == "https" and parsed.hostname:
        return True
    if parsed.scheme != "http" or not parsed.hostname:
        return False
    host = parsed.hostname.casefold()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _canonical_origin(origin: str) -> str:
    parsed = urlsplit(origin)
    return f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"


@dataclass(frozen=True)
class PairingCode:
    raw: str
    origin: str
    pairing_id: str
    expires_at: int
    protocol_version: int

    @property
    def host(self) -> str:
        return urlsplit(self.origin).netloc

    @property
    def insecure_lan(self) -> bool:
        return self.origin.startswith("http://")


def parse_pairing_code(value: str) -> PairingCode:
    parts = value.strip().split(".")
    if len(parts) != 3 or parts[0] != "SM1" or not all(parts[1:]):
        raise PairingError("Expected a code in the SM1.payload.signature format")
    try:
        payload = json.loads(_b64decode(parts[1]).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PairingError("The pairing code does not contain valid JSON") from exc
    if not isinstance(payload, dict):
        raise PairingError("The pairing code payload must be an object")
    origin = payload.get("origin")
    pairing_id = payload.get("pairing_id", payload.get("nonce"))
    expires_at = payload.get("expires_at", payload.get("exp"))
    protocol_version = payload.get("protocol_version", payload.get("version"))
    if not isinstance(origin, str) or not origin_is_allowed(origin):
        raise PairingError("The pairing server must use HTTPS (or private HTTP)")
    parsed = urlsplit(origin)
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise PairingError("The pairing server origin cannot include a path or query")
    if not isinstance(pairing_id, str) or not pairing_id:
        raise PairingError("The pairing code has no pairing identifier")
    if not isinstance(expires_at, int) or not isinstance(protocol_version, int):
        raise PairingError("The pairing code has invalid protocol metadata")
    return PairingCode(
        value.strip(), _canonical_origin(origin), pairing_id, expires_at, protocol_version
    )


class PairingClient:
    """Never logs secrets; callers should show only concise errors to users."""

    def __init__(self, code: PairingCode, *, timeout: float = 15.0) -> None:
        self.code = code
        self.timeout = timeout

    def _request(
        self, method: str, path: str, data: dict[str, object] | None = None
    ) -> dict[str, object]:
        body = None if data is None else json.dumps(data).encode("utf-8")
        request = Request(
            f"{self.code.origin}{path}",
            body,
            method=method,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:  # nosec B310: origin is user-confirmed
                raw = response.read(65_536)
        except (HTTPError, URLError, OSError) as exc:
            raise PairingError("Could not contact BookFlow; check the code and network") from exc
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PairingError("BookFlow returned an invalid pairing response") from exc
        if not isinstance(result, dict):
            raise PairingError("BookFlow returned an invalid pairing response")
        return result

    def claim(self) -> str:
        if self.code.protocol_version != 1:
            raise PairingError("This pairing code uses an unsupported protocol")
        if self.code.expires_at <= int(time.time()):
            raise PairingError("This pairing code has expired")
        result = self._request("POST", "/api/kobo/pair/claim", {"code": self.code.raw})
        session = result.get("pairing_session", result.get("session_id"))
        if not isinstance(session, str) or not session:
            raise PairingError("BookFlow did not return a pairing session")
        return session

    def complete(self, session_id: str, device_label: str) -> str:
        result = self._request(
            "POST",
            "/api/kobo/pair/complete",
            {"pairing_session": session_id, "device_label": device_label[:120]},
        )
        endpoint = result.get("api_endpoint")
        if not isinstance(endpoint, str) or not endpoint:
            raise PairingError("BookFlow did not return a Kobo endpoint")
        if result.get("protocol_version") != self.code.protocol_version:
            raise PairingError("BookFlow returned an incompatible pairing protocol")
        parsed = urlsplit(endpoint)
        if (
            _canonical_origin(endpoint) != self.code.origin
            or not origin_is_allowed(_canonical_origin(endpoint))
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not parsed.path.startswith("/api/kobo/")
            or not parsed.path.removeprefix("/api/kobo/")
            or "/" in parsed.path.removeprefix("/api/kobo/")
        ):
            raise PairingError("BookFlow returned an invalid Kobo endpoint")
        return endpoint

    def revoke(self, session_id: str, *, attempts: int = 3, backoff_seconds: float = 0.25) -> bool:
        """Revoke a completed pairing token and report whether the server confirmed it.

        A local filesystem failure must not silently leave a usable Kobo token behind.
        Retries are deliberately short: the GUI remains responsive enough to explain a
        failed cleanup while avoiding any logging of the pairing session.
        """
        if attempts < 1:
            raise ValueError("attempts must be at least one")
        for attempt in range(attempts):
            try:
                result = self._request(
                    "DELETE", "/api/kobo/pair/session", {"pairing_session": session_id}
                )
                if result.get("revoked") is True:
                    return True
            except PairingError:
                pass
            if attempt + 1 < attempts:
                time.sleep(backoff_seconds * (2**attempt))
        return False
