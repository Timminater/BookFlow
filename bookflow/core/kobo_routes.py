"""Token-authenticated Kobo device API and legacy BookFlow endpoints."""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import time
from copy import deepcopy
from ipaddress import ip_address
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlsplit

from flask import Blueprint, Response, jsonify, request, send_file

from bookflow.core import kobo_positions, kobo_protocol
from bookflow.core.kobo_services import (
    KoboAuthenticationError,
    KoboCursorError,
    KoboEntitlementError,
    KoboRateLimitError,
)
from bookflow.core.path_security import PathInput, PathSecurityError, resolve_library_file

if TYPE_CHECKING:
    from collections.abc import Callable

    from bookflow.core.kobo_services import KoboSyncService, TokenService

KOB0_CONTRACT_FIXTURES: dict[str, dict[str, Any]] = {
    "initialization": {
        "Version": "1.0",
        "Capabilities": {"sync": True, "metadata": True, "download": True, "delete": True},
    },
    "sync": {"Changes": [], "Cursor": None},
    "compatibility_empty": {},
}

type TLSGuard = Callable[[], bool]
type CoverFetcher = Callable[[str, str], tuple[bytes, str] | None]
type SyncAuditRecorder = Callable[..., None]
type DeviceFileExporter = Callable[[int, dict[str, Any]], dict[str, Any]]
type KoboRouteFailure = tuple[Response, int]
type KoboAuthenticationResult = tuple[int, None] | tuple[None, KoboRouteFailure]


def default_external_tls_guard() -> bool:
    """Allow loopback development; require a secure transport for external peers."""
    remote = request.remote_addr or ""
    try:
        if ip_address(remote).is_loopback:
            return True
    except ValueError:
        return False
    return bool(request.is_secure)


def register_kobo_routes(
    app: Any,
    *,
    token_service: TokenService,
    sync_service: KoboSyncService,
    allowed_file_roots: tuple[PathInput, ...],
    tls_guard: TLSGuard = default_external_tls_guard,
    cover_fetcher: CoverFetcher | None = None,
    sync_audit_recorder: SyncAuditRecorder | None = None,
    file_exporter: DeviceFileExporter | None = None,
) -> Blueprint:
    """Create and register Kobo token routes without touching application bootstrap.

    Registration is explicit so callers can place it behind feature/config gates
    during the broader integration.  Tokens are intentionally never logged or
    returned in error messages.
    """
    blueprint = Blueprint("kobo_mvp", __name__, url_prefix="/api/kobo")

    def device_file(user_id: int, file_id: int) -> tuple[dict[str, Any], Path, Path]:
        original = sync_service.get_entitled_file(user_id=user_id, file_id=file_id)
        source = resolve_library_file(original["storage_path"], allowed_roots=allowed_file_roots)
        exported = file_exporter(user_id, original) if file_exporter else original
        target = resolve_library_file(exported["storage_path"], allowed_roots=allowed_file_roots)
        return exported, source, target

    def device_book(user_id: int, book: dict[str, Any]) -> dict[str, Any]:
        if file_exporter is None:
            return book
        exported, _, _ = device_file(user_id, book["file_id"])
        return {
            **book,
            "file_format": exported["file_format"],
            "size_bytes": exported["size_bytes"],
        }

    def device_state(user_id: int, book_id: int, stored: dict[str, Any] | None) -> dict[str, Any]:
        result = kobo_protocol.reading_state(book_id, stored)
        if stored and stored.get("source") != "kobo" and stored.get("locator_json"):
            book = sync_service.get_book_metadata(user_id=user_id, book_id=book_id)
            if stored.get("file_id") == book["file_id"]:
                _, source, target = device_file(user_id, book["file_id"])
                locator = json.loads(stored["locator_json"])
                bookmark = kobo_positions.to_kobo(source, target, str(locator.get("value", "")))
                if bookmark:
                    result["CurrentBookmark"].update(bookmark)
        return result

    def authenticate(token: str) -> KoboAuthenticationResult:
        if not tls_guard():
            return None, (jsonify({"code": "tls_required", "message": "TLS is required"}), 400)
        try:
            authenticated = token_service.authenticate(
                token=token,
                source_ip=request.remote_addr or "unknown",
            )
        except KoboRateLimitError:
            return None, (jsonify({"code": "rate_limited", "message": "Too many requests"}), 429)
        except KoboAuthenticationError:
            return None, (jsonify({"code": "unauthorized", "message": "Unauthorized"}), 401)
        request.environ["bookflow.kobo_token_id"] = authenticated.token_id
        return authenticated.user_id, None

    def record_sync_audit(
        *,
        user_id: int,
        status: str,
        started: float,
        changes_sent: int = 0,
        high_water_revision: int | None = None,
        has_more: bool = False,
        error_code: str | None = None,
    ) -> None:
        if sync_audit_recorder is None:
            return
        try:
            sync_audit_recorder(
                user_id=user_id,
                token_id=request.environ.get("bookflow.kobo_token_id"),
                source_ip=request.remote_addr or "unknown",
                user_agent=request.headers.get("User-Agent"),
                status=status,
                changes_sent=changes_sent,
                high_water_revision=high_water_revision,
                has_more=has_more,
                duration_ms=round((time.monotonic() - started) * 1000),
                error_code=error_code,
            )
        except OSError, RuntimeError, TypeError, ValueError:
            app.logger.exception("Unable to persist Kobo sync audit event")

    @blueprint.get("/<token>/v1/initialization")
    @blueprint.get("/<token>")
    def initialization(token: str) -> tuple[Response, int] | Response:
        _user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        response = jsonify({"Resources": kobo_protocol.resources(device_endpoint(token))})
        response.headers["x-kobo-apitoken"] = "e30="
        return response

    def device_endpoint(token: str) -> str:
        return f"{request.url_root.rstrip('/')}/api/kobo/{token}"

    @blueprint.route("/<token>/oauth/.well-known/openid-configuration", methods=["GET", "POST"])
    def device_oidc_discovery(token: str) -> tuple[Response, int] | Response:
        _, failure = authenticate(token)
        if failure is not None:
            return failure
        base = device_endpoint(token) + "/oauth"
        return jsonify(
            {
                "issuer": base,
                "authorization_endpoint": base + "/connect/authorize",
                "token_endpoint": base + "/connect/token",
                "userinfo_endpoint": base + "/connect/userinfo",
                "jwks_uri": base + "/jwks",
                "response_types_supported": ["code"],
                "subject_types_supported": ["public"],
                "id_token_signing_alg_values_supported": ["RS256"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"],
            }
        )

    @blueprint.route("/<token>/oauth/connect/token", methods=["GET", "POST"])
    @blueprint.route("/<token>/oauth/token", methods=["GET", "POST"])
    @blueprint.route("/<token>/oauth/connect/authorize", methods=["GET", "POST"])
    @blueprint.route("/<token>/oauth/authorize", methods=["GET", "POST"])
    def device_oauth_token(token: str) -> tuple[Response, int] | Response:
        user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        # Nickel's identity bootstrap is separate from /v1/auth/device.
        # These opaque compatibility tokens grant no BookFlow access; the
        # personal URL token must authenticate every resource independently.
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        return jsonify(
            {
                "access_token": access,
                "refresh_token": refresh,
                "token_type": "Bearer",
                "expires_in": 3600,
                "scope": "",
                "user_id": str(user_id),
                "user_key": "",
                "AccessToken": access,
                "RefreshToken": refresh,
                "TokenType": "Bearer",
            }
        )

    @blueprint.get("/<token>/oauth/jwks")
    @blueprint.get("/<token>/oauth/connect/userinfo")
    def device_oauth_info(token: str) -> tuple[Response, int] | Response:
        user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        return jsonify({"keys": []} if request.path.endswith("/jwks") else {"sub": str(user_id)})

    @blueprint.after_request
    def private_response(response: Response) -> Response:
        response.headers["Cache-Control"] = "private, no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @blueprint.post("/<token>/v1/auth/device")
    @blueprint.post("/<token>/v1/auth/refresh")
    @blueprint.post("/<token>/v1/auth/exchange")
    @blueprint.post("/<token>/v1/user/add-device")
    def device_auth(token: str) -> tuple[Response, int] | Response:
        user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"code": "invalid_request"}), 400
        # These compatibility values do not grant access. Every subsequent
        # request must still authenticate the revocable personal URL token.
        user_key = payload.get("UserKey")
        return jsonify(
            {
                "AccessToken": secrets.token_urlsafe(32),
                "RefreshToken": secrets.token_urlsafe(32),
                "TokenType": "Bearer",
                "TrackingId": kobo_protocol.book_uuid(cast(int, user_id)),
                "UserKey": user_key if isinstance(user_key, str) else str(user_id),
            }
        )

    @blueprint.get("/<token>/v1/library/sync")
    def library_sync(token: str) -> tuple[Response, int] | Response:
        user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        user_id = cast(int, user_id)
        started = time.monotonic()
        try:
            cursor = request.headers.get("X-Kobo-Synctoken")
            # Ignore an official-store token left by the previous endpoint.
            # BookFlow tokens are signed and fail closed if modified.
            cursor = cursor.removeprefix("bf1.") if cursor and cursor.startswith("bf1.") else None
            batch = sync_service.sync(user_id=user_id, cursor=cursor, persistent=True)
        except KoboCursorError:
            record_sync_audit(
                user_id=user_id,
                status="failed",
                started=started,
                error_code="invalid_cursor",
            )
            return jsonify({"code": "invalid_cursor", "message": "Invalid sync cursor"}), 409
        except KoboEntitlementError:
            record_sync_audit(
                user_id=user_id,
                status="failed",
                started=started,
                error_code="forbidden",
            )
            return jsonify({"code": "forbidden", "message": "Kobo is unavailable"}), 403
        payload = []
        latest = {change["book_id"]: change for change in batch.changes}
        for book_id, change in latest.items():
            try:
                book = sync_service.get_book_metadata(user_id=user_id, book_id=book_id)
            except KoboEntitlementError:
                # Only IDs from this user's own journal can produce tombstones.
                payload.append(
                    {
                        "ChangedEntitlement": {
                            "BookEntitlement": kobo_protocol.entitlement(
                                book_id, change["created_at"], removed=True
                            )
                        }
                    }
                )
                continue
            if str(book["file_format"]).lower() not in {"epub", "kepub"}:
                continue
            state = device_state(
                user_id, book_id, sync_service.get_reading_state(user_id=user_id, book_id=book_id)
            )
            if change["type"] == "progress" and book["first_revision"] <= batch.since_revision:
                payload.append({"ChangedReadingState": {"ReadingState": state}})
            else:
                kind = (
                    "NewEntitlement"
                    if book["first_revision"] > batch.since_revision
                    else "ChangedEntitlement"
                )
                payload.append(
                    {
                        kind: {
                            "BookEntitlement": kobo_protocol.entitlement(
                                book_id, book["updated_at"]
                            ),
                            "BookMetadata": kobo_protocol.metadata(
                                device_book(user_id, book), device_endpoint(token)
                            ),
                            "ReadingState": state,
                        }
                    }
                )
        response = jsonify(payload)
        response.headers["X-Kobo-Synctoken"] = f"bf1.{batch.cursor}"
        response.headers["X-Kobo-Sync-Mode"] = "delta"
        if batch.has_more:
            response.headers["X-Kobo-Sync"] = "continue"
        record_sync_audit(
            user_id=user_id,
            status="completed",
            started=started,
            changes_sent=len(payload),
            high_water_revision=batch.high_water,
            has_more=batch.has_more,
        )
        return response

    @blueprint.get("/<token>/v1/library/<book_identifier>/metadata")
    @blueprint.get("/<token>/v1/user/library/books/<book_identifier>")
    def device_metadata(token: str, book_identifier: str) -> tuple[Response, int] | Response:
        user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        try:
            book_id = kobo_protocol.parse_book_uuid(book_identifier)
            book = sync_service.get_book_metadata(user_id=cast(int, user_id), book_id=book_id)
        except ValueError, KoboEntitlementError:
            return jsonify({"code": "not_found"}), 404
        metadata = kobo_protocol.metadata(
            device_book(cast(int, user_id), book), device_endpoint(token)
        )
        if "/v1/user/library/books/" in request.path:
            return jsonify(metadata)
        return jsonify([metadata])

    @blueprint.route("/<token>/v1/library/<book_identifier>/state", methods=["GET", "PUT"])
    def device_reading_state(token: str, book_identifier: str) -> tuple[Response, int] | Response:
        user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        user_id = cast(int, user_id)
        try:
            book_id = kobo_protocol.parse_book_uuid(book_identifier)
            stored = sync_service.get_reading_state(user_id=user_id, book_id=book_id)
        except ValueError, KoboEntitlementError:
            return jsonify({"code": "not_found"}), 404
        if request.method == "GET":
            return jsonify([device_state(user_id, book_id, stored)])
        payload = request.get_json(silent=True)
        try:
            state, progress = kobo_protocol.parse_reading_state(payload, book_identifier, stored)
            locator = None
            source_file_id = None
            location = (state.get("CurrentBookmark") or {}).get("Location")
            if isinstance(location, dict) and location.get("Type") == "KoboSpan":
                book = sync_service.get_book_metadata(user_id=user_id, book_id=book_id)
                _, source, target = device_file(user_id, book["file_id"])
                cfi = kobo_positions.from_kobo(
                    source, target, str(location.get("Source", "")), str(location.get("Value", ""))
                )
                if cfi:
                    locator = {"value": cfi, "progression": progress / 100}
                    source_file_id = book["file_id"]
            # Resolve timestamps atomically with the write, including newer
            # backwards moves and stale offline device submissions.
            stored = sync_service.set_reading_state(
                user_id=user_id,
                book_id=book_id,
                progress=progress,
                device_state=state,
                locator=locator,
                file_id=source_file_id,
            )
        except KeyError, TypeError, ValueError, AttributeError:
            return jsonify({"code": "invalid_reading_state"}), 400
        except KoboEntitlementError:
            return jsonify({"code": "not_found"}), 404
        date = kobo_protocol.timestamp(stored["updated_at"] if stored else None)
        return jsonify(
            {
                "RequestResult": "Success",
                "UpdateResults": [
                    {
                        "EntitlementId": book_identifier,
                        "LastModified": date,
                        "PriorityTimestamp": date,
                        "CurrentBookmarkResult": {"Result": "Success"},
                        "StatusInfoResult": {"Result": "Success"},
                        "StatisticsResult": {"Result": "Success"},
                    }
                ],
            }
        )

    @blueprint.delete("/<token>/v1/library/<book_identifier>")
    def device_delete(token: str, book_identifier: str) -> tuple[Response, int] | Response:
        user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        try:
            book_id = kobo_protocol.parse_book_uuid(book_identifier)
            sync_service.get_book_metadata(user_id=cast(int, user_id), book_id=book_id)
            sync_service.delete_book(user_id=cast(int, user_id), book_id=book_id, source="kobo")
        except ValueError, KoboEntitlementError:
            return jsonify({"code": "not_found"}), 404
        return jsonify({})

    @blueprint.get("/<token>/v1/covers/<book_identifier>/<int:width>/<int:height>/image.jpg")
    @blueprint.get(
        "/<token>/v1/covers/<book_identifier>/<int:width>/<int:height>/<quality>/<grayscale>/image.jpg"
    )
    def device_cover(
        token: str,
        book_identifier: str,
        width: int,
        height: int,
        quality: str = "",
        grayscale: str = "",
    ) -> tuple[Response, int] | Response:
        del width, height, quality, grayscale
        try:
            book_id = kobo_protocol.parse_book_uuid(book_identifier)
        except ValueError:
            return jsonify({"code": "not_found"}), 404
        return book_cover(token, book_id)

    @blueprint.route("/<token>/v1/analytics/gettests", methods=["GET", "POST"])
    @blueprint.post("/<token>/v1/analytics/event")
    @blueprint.get("/<token>/v1/configuration")
    @blueprint.get("/<token>/v1/user/profile")
    @blueprint.get("/<token>/v1/user/platform")
    @blueprint.get("/<token>/v1/user/loyalty/benefits")
    @blueprint.route("/<token>/v1/affiliate", methods=["GET", "POST"])
    @blueprint.get("/<token>/v1/assets")
    @blueprint.route("/<token>/v1/deals", methods=["GET", "POST"])
    @blueprint.get("/<token>/v1/products/books/subscriptions")
    def device_optional_resource(token: str) -> tuple[Response, int] | Response:
        user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        if request.path.endswith("/analytics/gettests"):
            return jsonify(
                {
                    "Result": "Success",
                    "TestKey": request.headers.get("X-Kobo-UserKey", ""),
                    "Tests": {},
                }
            )
        if request.path.endswith("/user/loyalty/benefits"):
            return jsonify({"Benefits": {}})
        if request.path.endswith("/assets"):
            return jsonify({"Assets": []})
        if request.path.endswith("/user/profile"):
            return jsonify({"UserId": f"bookflow-{user_id}", "UserDisplayName": "BookFlow"})
        # Explicit optional store endpoints, as in Calibre-Web's local mode.
        # Unknown paths and unauthorized requests must still fail normally.
        return jsonify({})

    @blueprint.get("/<token>/v1/library/tags")
    def device_tags(token: str) -> tuple[Response, int] | Response:
        _user_id, failure = authenticate(token)
        return failure if failure is not None else jsonify([])

    @blueprint.get("/<token>/v1/books/<int:book_id>")
    def book_metadata(token: str, book_id: int) -> tuple[Response, int] | Response:
        user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        user_id = cast(int, user_id)
        try:
            book = sync_service.get_book_metadata(user_id=user_id, book_id=book_id)
        except KoboEntitlementError:
            return jsonify({"code": "not_found", "message": "Book not found"}), 404
        return jsonify(
            {
                "BookId": book["id"],
                "Title": book["title"],
                "Author": book["author"],
                "Language": book["language"],
                "PublicationYear": book["publish_year"],
                "File": {
                    "Id": book["file_id"],
                    "Format": book["file_format"],
                    "Size": book["size_bytes"],
                    "Checksum": book["sha256"],
                },
                "CoverUrl": f"/api/kobo/{token}/v1/books/{book_id}/cover",
            }
        )

    @blueprint.get("/<token>/v1/library/<int:book_id>/state")
    def reading_state(token: str, book_id: int) -> tuple[Response, int] | Response:
        user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        try:
            state = sync_service.get_reading_state(user_id=cast(int, user_id), book_id=book_id)
        except KoboEntitlementError:
            return jsonify({"code": "not_found", "message": "Book not found"}), 404
        if state is None:
            return jsonify({"BookId": book_id, "Progress": 0, "Active": False, "Revision": 0})
        return jsonify(
            {
                "BookId": book_id,
                "Progress": state["progress"],
                "Active": bool(state["active"]),
                "Revision": state["revision"],
                "UpdatedAt": state["updated_at"],
            }
        )

    @blueprint.put("/<token>/v1/library/<int:book_id>/state")
    def put_reading_state(token: str, book_id: int) -> tuple[Response, int] | Response:
        user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        payload = request.get_json(silent=True)
        raw_progress = (
            payload.get("Progress", payload.get("progress")) if isinstance(payload, dict) else None
        )
        if isinstance(raw_progress, bool) or not isinstance(raw_progress, (int, float)):
            return jsonify(
                {"code": "invalid_progress", "message": "Progress must be a number"}
            ), 400
        try:
            # A device must never be able to lower progress by supplying an
            # undocumented Reset flag.  Resetting is an explicit BookFlow
            # action (the authenticated web API), keeping device sync monotone.
            state = sync_service.set_reading_state(
                user_id=cast(int, user_id), book_id=book_id, progress=float(raw_progress)
            )
        except KoboEntitlementError:
            return jsonify({"code": "not_found", "message": "Book not found"}), 404
        except ValueError as exc:
            return jsonify({"code": "invalid_progress", "message": str(exc)}), 409
        return jsonify(
            {
                "BookId": book_id,
                "Progress": state["progress"],
                "Active": bool(state["active"]),
                "Revision": state["revision"],
                "UpdatedAt": state["updated_at"],
            }
        )

    @blueprint.get("/<token>/v1/files/<int:file_id>/download")
    def file_download(token: str, file_id: int) -> tuple[Response, int] | Response:
        user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        user_id = cast(int, user_id)
        try:
            _, _, path = device_file(user_id, file_id)
        except KoboEntitlementError:
            return jsonify({"code": "not_found", "message": "File not found"}), 404
        except PathSecurityError:
            return jsonify({"code": "file_unavailable", "message": "File is unavailable"}), 404
        return send_file(path, as_attachment=True, download_name=Path(path).name)

    @blueprint.get("/<token>/v1/books/<int:book_id>/cover")
    def book_cover(token: str, book_id: int) -> tuple[Response, int] | Response:
        user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        user_id = cast(int, user_id)
        try:
            book = sync_service.get_book_metadata(user_id=user_id, book_id=book_id)
        except KoboEntitlementError:
            return jsonify({"code": "not_found", "message": "Cover not found"}), 404
        cover_url = book.get("cover_url")
        if not isinstance(cover_url, str) or not cover_url.strip():
            return jsonify({"code": "not_found", "message": "Cover not found"}), 404
        if cover_fetcher is None:
            return jsonify({"CoverUrl": cover_url})
        # Catalog metadata may retain the authenticated browser proxy URL.
        # Resolve its source locally; readers do not have a browser session.
        if cover_url.startswith("/api/covers/"):
            try:
                encoded = parse_qs(urlsplit(cover_url).query).get("url", [""])[0]
                cover_url = base64.b64decode(encoded, altchars=b"-_", validate=True).decode()
            except ValueError, binascii.Error, UnicodeDecodeError:
                return jsonify({"code": "not_found", "message": "Cover not found"}), 404
        if urlsplit(cover_url).scheme not in {"http", "https"}:
            return jsonify({"code": "not_found", "message": "Cover not found"}), 404
        # The fetcher's existing public-address and image checks still apply.
        cover = cover_fetcher(f"kobo-book-{book_id}", cover_url)
        if cover is None:
            return jsonify({"code": "not_found", "message": "Cover not found"}), 404
        image_bytes, content_type = cover
        response = app.response_class(image_bytes, content_type=content_type)
        response.headers["Cache-Control"] = "private, max-age=86400"
        return response

    @blueprint.delete("/<token>/v1/books/<int:book_id>")
    def delete_book(token: str, book_id: int) -> tuple[Response, int] | Response:
        user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        user_id = cast(int, user_id)
        try:
            revision = sync_service.delete_book(user_id=user_id, book_id=book_id)
        except KoboEntitlementError:
            return jsonify({"code": "not_found", "message": "Book not found"}), 404
        return jsonify({"revision": revision})

    @blueprint.get("/<token>/v1/<compatibility_route>")
    def empty_compatibility(
        token: str, compatibility_route: str
    ) -> tuple[Response, int] | Response:
        if compatibility_route not in {"analytics", "recommendations", "store"}:
            return jsonify({"code": "not_found", "message": "Not found"}), 404
        _user_id, failure = authenticate(token)
        if failure is not None:
            return failure
        return jsonify(deepcopy(KOB0_CONTRACT_FIXTURES["compatibility_empty"]))

    app.register_blueprint(blueprint)
    return blueprint
