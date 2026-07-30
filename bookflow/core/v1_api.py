"""Versioned Book/Request API, isolated from legacy route registration.

``register_v1_api`` is intentionally an explicit integration seam: main.py can
wire its existing session/auth implementation without this module importing it.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import sqlite3
import uuid
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flask import Blueprint, Flask, Response, g, jsonify, request, send_file

from bookflow.core.bestseller_service import BestsellerSuggestionService
from bookflow.core.book_groups import canonical_book_id, equivalent_book_ids
from bookflow.core.book_matcher import normalize_isbn
from bookflow.core.catalog_deletion_service import (
    CatalogDeletionConflictError,
    CatalogDeletionError,
    CatalogDeletionNotFoundError,
    CatalogDeletionService,
)
from bookflow.core.catalog_monitor_service import CatalogMonitorError, CatalogMonitorService
from bookflow.core.durable_jobs import DurableJobService
from bookflow.core.library_service import LibraryService
from bookflow.core.path_security import PathSecurityError
from bookflow.core.release_candidates import ReleaseCandidate, evaluate_candidate
from bookflow.core.release_profile_service import (
    ReleaseProfileError,
    ReleaseProfileService,
    profile_snapshot_from_row,
    release_profile_from_snapshot,
)
from bookflow.core.request_workflow_service import (
    RequestWorkflowError,
    RequestWorkflowService,
)
from bookflow.core.search_service import SearchService, SearchServiceError
from bookflow.core.web_push_events import enqueue_web_push

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@dataclass(frozen=True, slots=True)
class V1Actor:
    """Resolved authenticated principal supplied by the host application."""

    user_id: int
    is_admin: bool = False


def register_v1_api(
    app: Flask,
    *,
    database_path: str | Path,
    resolve_actor: Callable[[], V1Actor | None],
    default_profile: Callable[[], dict[str, Any]] | None = None,
    resolve_file_path: Callable[[str], Path] | None = None,
) -> None:
    """Register the standalone ``/api/v1`` Blueprint on an application.

    The host must call this once after its authentication/session setup, e.g.
    ``register_v1_api(app, database_path=CWA_DB_PATH, resolve_actor=actor)``.
    """
    del default_profile
    db_path = str(database_path)
    request_service = RequestWorkflowService(db_path)
    search_service = SearchService(db_path)
    profile_service = ReleaseProfileService(db_path)
    job_service = DurableJobService(db_path)
    catalog_service = CatalogMonitorService(db_path)
    library_service = LibraryService(db_path)
    catalog_deletion_service = CatalogDeletionService(db_path, resolve_file_path)
    suggestion_service = BestsellerSuggestionService(db_path)
    blueprint = Blueprint("v1", __name__, url_prefix="/api/v1")

    @blueprint.before_request
    def _correlation_id() -> None:
        g.v1_correlation_id = request.headers.get("X-Correlation-Id") or uuid.uuid4().hex

    def error(code: str, message: str, status: int, details: Any = None) -> tuple[Response, int]:
        body: dict[str, Any] = {
            "error": {
                "code": code,
                "message": message,
                "details": details,
                "correlation_id": g.v1_correlation_id,
            }
        }
        response = jsonify(body)
        response.headers["X-Correlation-Id"] = g.v1_correlation_id
        return response, status

    def require_actor(*, admin: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(handler: Callable[..., Any]) -> Callable[..., Any]:
            @wraps(handler)
            def wrapped(*args: Any, **kwargs: Any) -> Any:
                actor = resolve_actor()
                if actor is None:
                    return error("unauthorized", "Authentication required", 401)
                if admin and not actor.is_admin:
                    return error("forbidden", "Administrator access required", 403)
                g.v1_actor = actor
                return handler(*args, **kwargs)

            return wrapped

        return decorator

    def connect() -> sqlite3.Connection:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def reader_content_type(file_format: object, declared_mime_type: object) -> str:
        """Return a safe inline MIME type for the reader's supported assets."""
        normalized_format = str(file_format or "").strip().casefold().lstrip(".")
        known = {
            "epub": "application/epub+zip",
            "kepub": "application/epub+zip",
            "kepub.epub": "application/epub+zip",
            "pdf": "application/pdf",
            "cbz": "application/vnd.comicbook+zip",
            "fb2": "application/x-fictionbook+xml",
            "mobi": "application/x-mobipocket-ebook",
            "azw3": "application/vnd.amazon.ebook",
        }
        if normalized_format in known:
            return known[normalized_format]
        if isinstance(declared_mime_type, str) and declared_mime_type.strip():
            return declared_mime_type.strip()
        guessed, _encoding = mimetypes.guess_type(f"book.{normalized_format}")
        return guessed or "application/octet-stream"

    def reader_mode(file_format: object) -> str | None:
        normalized_format = str(file_format or "").strip().casefold().lstrip(".")
        if normalized_format in {"epub", "kepub", "kepub.epub", "mobi", "azw3", "fb2"}:
            return "reflowable"
        if normalized_format in {"pdf", "cbz"}:
            return "fixed"
        return None

    def serialize_reader_progress(progress: dict[str, Any]) -> dict[str, Any]:
        """Expose the stored locator as JSON rather than a database encoding."""
        serialized = dict(progress)
        raw_locator = serialized.pop("locator_json", None)
        if raw_locator is not None:
            try:
                serialized["locator"] = json.loads(str(raw_locator))
            except json.JSONDecodeError:
                serialized["locator"] = None
        raw_progress = serialized.get("progress")
        if isinstance(raw_progress, (int, float)) and not isinstance(raw_progress, bool):
            serialized["progression"] = float(raw_progress) / 100
        if "active" in serialized:
            serialized["completed"] = not bool(serialized["active"])
        return serialized

    def enqueue_request_status(request_row: dict[str, Any]) -> None:
        """Queue personal request status after its durable DB transition."""
        try:
            user_id = int(request_row["user_id"])
            book_id = int(request_row["book_id"])
            request_id = int(request_row["id"])
            status = str(request_row["status"])
        except KeyError, TypeError, ValueError:
            return
        with connect() as conn:
            book = conn.execute("SELECT title FROM books WHERE id = ?", (book_id,)).fetchone()
        if book is None:
            return
        enqueue_web_push(
            db_path,
            user_id=user_id,
            event_type="request.status",
            payload={
                "title": str(book["title"]),
                "book_id": book_id,
                "status": status,
                "url": f"/catalog/{book_id}",
            },
            event_key=f"request:{request_id}:{status}",
        )

    def page(*, default_limit: int = 50) -> tuple[int, int]:
        try:
            offset = max(0, int(request.args.get("offset", "0")))
            limit = min(100, max(1, int(request.args.get("limit", str(default_limit)))))
        except ValueError:
            raise ValueError("offset and limit must be integers") from None
        return offset, limit

    def paged(sql: str, parameters: tuple[Any, ...] = ()) -> dict[str, Any]:
        offset, limit = page()
        with connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM ({sql})", parameters).fetchone()[0])  # noqa: S608
            rows = conn.execute(f"{sql} LIMIT ? OFFSET ?", (*parameters, limit, offset)).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    @blueprint.get("/openapi.json")
    def openapi() -> Response:
        spec: dict[str, Any] = {
            "openapi": "3.1.0",
            "info": {"title": "BookFlow API", "version": "v1"},
            "components": {
                "securitySchemes": {
                    "sessionCookie": {
                        "type": "apiKey",
                        "in": "cookie",
                        "name": "session",
                    },
                },
                "schemas": {
                    "PagedResponse": {
                        "type": "object",
                        "required": ["items", "total", "offset", "limit"],
                        "properties": {
                            "items": {"type": "array", "items": {}},
                            "total": {"type": "integer", "minimum": 0},
                            "offset": {"type": "integer", "minimum": 0},
                            "limit": {"type": "integer", "minimum": 1},
                        },
                    },
                    "BulkLibraryRequest": {
                        "type": "object",
                        "required": ["book_ids"],
                        "properties": {
                            "book_ids": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": 100,
                                "items": {"type": "integer", "minimum": 1},
                            }
                        },
                    },
                    "BulkLibraryResult": {
                        "type": "object",
                        "required": ["book_id", "status"],
                        "properties": {
                            "book_id": {"type": "integer", "minimum": 1},
                            "canonical_book_id": {"type": "integer", "minimum": 1},
                            "status": {
                                "type": "string",
                                "enum": [
                                    "added",
                                    "already_in_library",
                                    "unavailable",
                                    "not_found",
                                ],
                            },
                        },
                    },
                    "Error": {
                        "type": "object",
                        "required": ["error"],
                        "properties": {
                            "error": {
                                "type": "object",
                                "required": ["code"],
                                "properties": {
                                    "code": {"type": "string"},
                                    "message": {"type": "string"},
                                    "correlation_id": {"type": "string"},
                                },
                            }
                        },
                    },
                },
            },
            "paths": {
                "/api/v1/library/books": {
                    "get": {"summary": "List books on the personal library shelf"}
                },
                "/api/v1/library/books/bulk": {
                    "post": {
                        "summary": "Add available catalog books to the personal library",
                        "requestBody": {
                            "required": True,
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/BulkLibraryRequest"}
                                }
                            },
                        },
                        "responses": {
                            "200": {
                                "description": "Per-book outcomes and aggregate counts",
                                "content": {
                                    "application/json": {
                                        "schema": {
                                            "type": "object",
                                            "required": ["results", "summary"],
                                            "properties": {
                                                "results": {
                                                    "type": "array",
                                                    "items": {
                                                        "$ref": "#/components/schemas/BulkLibraryResult"
                                                    },
                                                },
                                                "summary": {"type": "object"},
                                            },
                                        }
                                    }
                                },
                            }
                        },
                    }
                },
                "/api/v1/library/books/{bookId}": {"get": {"summary": "Get an entitled book"}},
                "/api/v1/requests": {"post": {"summary": "Create a request"}},
                "/api/v1/requests/{requestId}/cancel": {
                    "post": {"summary": "Cancel an owned request"}
                },
                "/api/v1/users/me/requests": {
                    "get": {"summary": "List the current user's recent requests"}
                },
                "/api/v1/admin/requests": {"get": {"summary": "List requests"}},
                "/api/v1/admin/request-processes": {"get": {"summary": "List request processes"}},
                "/api/v1/admin/request-processes/{requestProcessId}/search-now": {
                    "post": {"summary": "Schedule a request search"}
                },
                "/api/v1/admin/request-processes/{requestProcessId}/cancel": {
                    "post": {"summary": "Cancel a request process and its active requests"}
                },
                "/api/v1/admin/maintenance": {
                    "get": {"summary": "Inspect database and migration status"}
                },
                "/api/v1/users/me/kobo-settings": {
                    "get": {"summary": "Get personal Kobo settings"},
                    "put": {"summary": "Update personal Kobo settings"},
                },
                "/api/v1/users/me/kobo-token": {"post": {"summary": "Issue a Kobo token"}},
                "/api/v1/users/me/kobo-token/{tokenId}/rotate": {
                    "post": {"summary": "Rotate a Kobo token"}
                },
                "/api/v1/users/me/kobo-token/{tokenId}": {
                    "delete": {"summary": "Revoke a Kobo device token"}
                },
                "/api/v1/users/me/kobo-books/{bookId}": {
                    "put": {"summary": "Add or update a Kobo shelf book"},
                    "delete": {"summary": "Remove a Kobo shelf book"},
                },
                "/api/v1/catalog/recent": {"get": {"summary": "Recent shared catalog books"}},
                "/api/v1/catalog/books": {"get": {"summary": "Search shared catalog"}},
                "/api/v1/search/books": {"get": {"summary": "Autocomplete local books"}},
                "/api/v1/catalog/books/{bookId}": {
                    "get": {"summary": "View global catalog metadata"}
                },
                "/api/v1/catalog/presence": {
                    "post": {"summary": "Match external results against the local catalog"}
                },
                "/api/v1/suggestions": {
                    "get": {"summary": "List current Dutch bestseller suggestions"}
                },
                "/api/v1/suggestions/{listKey}": {
                    "get": {"summary": "Browse one Dutch bestseller list"}
                },
                "/api/v1/admin/bestseller/status": {
                    "get": {"summary": "Inspect Dutch bestseller sync status"}
                },
                "/api/v1/admin/bestseller/sync": {
                    "post": {"summary": "Schedule a Dutch bestseller sync"}
                },
                "/api/v1/catalog/authors": {"get": {"summary": "Browse canonical authors"}},
                "/api/v1/catalog/series": {"get": {"summary": "Browse canonical series"}},
                "/api/v1/monitors": {
                    "get": {"summary": "List monitors"},
                    "post": {"summary": "Create monitor"},
                },
                "/api/v1/monitors/{monitorId}": {
                    "patch": {"summary": "Update an owned monitor"},
                    "delete": {"summary": "Delete an owned monitor"},
                },
                "/api/v1/monitors/{monitorId}/scan": {
                    "post": {"summary": "Schedule a durable monitor scan"}
                },
                "/api/v1/monitors/{monitorId}/discoveries": {
                    "get": {"summary": "List monitor discoveries"}
                },
                "/api/v1/monitors/{monitorId}/discoveries/{discoveryId}": {
                    "patch": {"summary": "Ignore or restore an owned monitor discovery"}
                },
                "/api/v1/users/me/reading-progress/{bookId}": {
                    "get": {"summary": "Get reading progress"},
                    "patch": {"summary": "Update reading progress"},
                    "delete": {"summary": "Clear reading progress"},
                },
                "/api/v1/users/me/reading-progress": {
                    "get": {"summary": "List active, entitled reading progress"}
                },
                "/api/v1/users/me/kobo-pairing-code": {
                    "post": {"summary": "Create a one-time Kobo pairing code"}
                },
                "/api/kobo/pair/claim": {"post": {"summary": "Claim a one-time pairing code"}},
                "/api/kobo/pair/complete": {
                    "post": {"summary": "Complete pairing and issue a Kobo token"}
                },
                "/api/kobo/pair/session": {
                    "delete": {"summary": "Revoke a failed pairing session"}
                },
                "/api/v1/kobo/patcher/manifest": {
                    "get": {"summary": "Get compatible patcher release metadata"}
                },
                "/api/v1/kobo/patcher/download": {
                    "get": {"summary": "Download the compatible Windows patcher"}
                },
                "/api/kobo/{token}/v1/library/{bookId}/state": {
                    "get": {"summary": "Read Kobo reading state"},
                    "put": {"summary": "Update Kobo reading state"},
                },
                "/api/kobo/{token}/v1/initialization": {
                    "get": {"summary": "Initialize a paired Kobo device"}
                },
                "/api/kobo/{token}/v1/library/sync": {
                    "get": {"summary": "Synchronize entitled Kobo library changes"}
                },
                "/api/kobo/{token}/v1/books/{bookId}": {
                    "get": {"summary": "Read entitled Kobo book metadata"},
                    "delete": {"summary": "Remove a book from the personal Kobo shelf"},
                },
                "/api/kobo/{token}/v1/files/{fileId}/download": {
                    "get": {"summary": "Download an entitled Kobo book file"}
                },
                "/api/kobo/{token}/v1/books/{bookId}/cover": {
                    "get": {"summary": "Read an entitled Kobo book cover"}
                },
                "/api/kobo/{token}/v1/{compatibilityRoute}": {
                    "get": {"summary": "Kobo compatibility response"}
                },
                "/api/v1/users/me/push/vapid-key": {"get": {"summary": "Get the public VAPID key"}},
                "/api/v1/users/me/push/subscriptions": {
                    "get": {"summary": "List push devices"},
                    "post": {"summary": "Register a push device"},
                },
                "/api/v1/users/me/push/subscriptions/{subscriptionId}": {
                    "delete": {"summary": "Remove a push device"}
                },
                "/api/v1/users/me/push/rules": {
                    "get": {"summary": "List personal notification rules"}
                },
                "/api/v1/users/me/push/rules/{eventType}": {
                    "put": {"summary": "Create or update a notification rule"},
                    "delete": {"summary": "Reset a notification rule to its default"},
                },
                "/api/v1/users/me/push/test": {"post": {"summary": "Send a personal push test"}},
                "/api/v1/catalog/books/{bookId}/metadata-reports": {
                    "post": {"summary": "Report incorrect catalog metadata"}
                },
                "/api/v1/issue-reports": {
                    "post": {"summary": "Submit an in-app problem report to GitHub"}
                },
                "/api/v1/metadata-reports": {"get": {"summary": "List own metadata reports"}},
                "/api/v1/admin/metadata-reports": {"get": {"summary": "Review metadata reports"}},
                "/api/v1/admin/catalog/books/{bookId}": {
                    "get": {"summary": "Inspect catalog deletion"},
                    "delete": {"summary": "Delete a shared catalog book"},
                },
                "/api/v1/admin/metadata-reports/{reportId}/reject": {
                    "post": {"summary": "Reject a metadata report"}
                },
                "/api/v1/admin/books/{bookId}/metadata/preview": {
                    "post": {"summary": "Preview an administrative metadata revision"}
                },
                "/api/v1/admin/books/{bookId}/metadata/apply": {
                    "post": {"summary": "Apply an administrative metadata revision"}
                },
                "/api/v1/admin/books/{bookId}/metadata/history": {
                    "get": {"summary": "List metadata revision history"}
                },
                "/api/v1/admin/books/{bookId}/metadata/revert/{revisionId}": {
                    "post": {"summary": "Revert a metadata revision"}
                },
                "/api/v1/admin/backups": {
                    "get": {"summary": "List backups"},
                    "post": {"summary": "Create a consistent backup"},
                },
                "/api/v1/admin/backups/{backupId}/verify": {
                    "post": {"summary": "Verify a backup checksum and schema"}
                },
                "/api/v1/admin/backups/{backupId}/stage-restore": {
                    "post": {"summary": "Validate and stage a restore for restart"}
                },
                "/api/v1/admin/backups/{backupId}/download": {
                    "get": {"summary": "Download a backup"}
                },
                "/api/v1/admin/backups/{backupId}": {"delete": {"summary": "Delete a backup"}},
            },
        }
        integer_parameters = {
            "bookId",
            "fileId",
            "monitorId",
            "discoveryId",
            "subscriptionId",
            "reportId",
            "revisionId",
            "requestProcessId",
            "requestId",
            "tokenId",
        }
        anonymous_paths = {
            "/api/kobo/pair/claim",
            "/api/kobo/pair/complete",
            "/api/kobo/pair/session",
        }
        for path, path_item in spec["paths"].items():
            placeholders = [
                segment[1:-1]
                for segment in path.split("/")
                if segment.startswith("{") and segment.endswith("}")
            ]
            if placeholders:
                path_item["parameters"] = [
                    {
                        "name": name,
                        "in": "path",
                        "required": True,
                        **(
                            {"description": "Sensitive Kobo device token; never log this value"}
                            if name == "token"
                            else {}
                        ),
                        "schema": {"type": "integer" if name in integer_parameters else "string"},
                    }
                    for name in placeholders
                ]
            for method, operation in path_item.items():
                if method == "parameters" or not isinstance(operation, dict):
                    continue
                operation.setdefault(
                    "responses",
                    {
                        "200": {"description": "Success"},
                        "4XX": {
                            "description": "Request error",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Error"}
                                }
                            },
                        },
                    },
                )
                if path.startswith("/api/kobo/{token}/"):
                    # Kobo authenticates with the opaque token in the endpoint
                    # path, so no standard OpenAPI security scheme accurately
                    # represents this legacy device contract.
                    operation["security"] = []
                elif path not in anonymous_paths:
                    operation["security"] = [{"sessionCookie": []}]
        return jsonify(spec)

    @blueprint.get("/release-profiles")
    @require_actor()
    def release_profiles() -> Response:
        return jsonify({"items": profile_service.list(active_only=True)})

    @blueprint.get("/admin/catalog/books/<int:book_id>")
    @require_actor(admin=True)
    def admin_catalog_delete_plan(book_id: int) -> Response | tuple[Response, int]:
        plan = catalog_deletion_service.plan(book_id)
        if plan is None:
            return error("not_found", "Catalog book not found", 404)
        return jsonify({"plan": plan.serialize(), "plan_token": plan.token()})

    @blueprint.delete("/admin/catalog/books/<int:book_id>")
    @require_actor(admin=True)
    def admin_delete_catalog_book(book_id: int) -> Response | tuple[Response, int]:
        payload = request.get_json(silent=True) or {}
        plan_token = payload.get("plan_token")
        if not isinstance(plan_token, str) or len(plan_token) != 64:
            return error("invalid_plan", "A deletion plan token is required", 400)
        try:
            result = catalog_deletion_service.delete(
                book_id, actor_user_id=g.v1_actor.user_id, plan_token=plan_token
            )
        except CatalogDeletionConflictError as exc:
            return error("catalog_delete_blocked", str(exc), 409)
        except CatalogDeletionError:
            return error("catalog_delete_failed", "Catalog deletion failed", 500)
        return jsonify(result)

    @blueprint.post("/admin/catalog/deletions/<operation_id>/retry-cleanup")
    @require_actor(admin=True)
    def admin_retry_catalog_cleanup(operation_id: str) -> Response | tuple[Response, int]:
        try:
            return jsonify(catalog_deletion_service.retry_cleanup(operation_id))
        except ValueError as exc:
            return error("invalid_operation", str(exc), 400)
        except CatalogDeletionNotFoundError as exc:
            return error("not_found", str(exc), 404)
        except CatalogDeletionConflictError as exc:
            return error("cleanup_not_retryable", str(exc), 409)
        except CatalogDeletionError:
            return error("cleanup_failed", "Catalog cleanup failed", 500)

    @blueprint.get("/admin/catalog/deletions")
    @require_actor(admin=True)
    def admin_pending_catalog_deletions() -> Response:
        return jsonify({"items": catalog_deletion_service.pending_operations()})

    @blueprint.get("/admin/catalog/deletions/<operation_id>")
    @require_actor(admin=True)
    def admin_catalog_deletion_status(operation_id: str) -> Response | tuple[Response, int]:
        try:
            return jsonify(catalog_deletion_service.operation(operation_id))
        except ValueError as exc:
            return error("invalid_operation", str(exc), 400)
        except CatalogDeletionNotFoundError as exc:
            return error("not_found", str(exc), 404)

    @blueprint.get("/admin/release-profiles")
    @require_actor(admin=True)
    def admin_release_profiles() -> Response:
        return jsonify({"items": profile_service.list()})

    @blueprint.post("/admin/release-profiles")
    @require_actor(admin=True)
    def create_release_profile() -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("name"), str):
            return error("invalid_profile", "name is required", 400)
        try:
            created = profile_service.create(
                name=payload["name"],
                config=payload.get("config"),
                is_default=bool(payload.get("is_default", False)),
                is_active=bool(payload.get("is_active", True)),
                created_by=actor.user_id,
            )
        except (ReleaseProfileError, ValueError) as exc:
            return error("invalid_profile", str(exc), 400)
        return jsonify({"profile": created}), 201

    @blueprint.patch("/admin/release-profiles/<int:profile_id>")
    @require_actor(admin=True)
    def patch_release_profile(profile_id: int) -> Response | tuple[Response, int]:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error("invalid_profile", "body must be an object", 400)
        try:
            updated = profile_service.update(
                profile_id,
                name=payload.get("name"),
                config=payload.get("config"),
                is_default=payload.get("is_default"),
                is_active=payload.get("is_active"),
            )
        except ReleaseProfileError as exc:
            return error(
                "profile_not_found" if "not found" in str(exc).lower() else "invalid_profile",
                str(exc),
                404 if "not found" in str(exc).lower() else 400,
            )
        return jsonify({"profile": updated})

    @blueprint.delete("/admin/release-profiles/<int:profile_id>")
    @require_actor(admin=True)
    def delete_release_profile(profile_id: int) -> Response | tuple[Response, int]:
        try:
            profile_service.delete(profile_id)
        except ReleaseProfileError as exc:
            return error(
                "profile_not_found" if "not found" in str(exc).lower() else "invalid_profile",
                str(exc),
                404 if "not found" in str(exc).lower() else 409,
            )
        return jsonify({"profile_id": profile_id, "deleted": True})

    @blueprint.post("/admin/release-profiles/preview")
    @require_actor(admin=True)
    def preview_release_profile() -> Response | tuple[Response, int]:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("candidate"), dict):
            return error("invalid_profile_preview", "candidate is required", 400)
        try:
            profile_id = payload.get("profile_id")
            snapshot = (
                profile_snapshot_from_row(profile_service.get(int(profile_id)))
                if profile_id is not None
                else dict(payload.get("config") or {})
            )
            candidate = ReleaseCandidate(**payload["candidate"])
            profile = release_profile_from_snapshot(
                snapshot,
                title=str(payload.get("title") or candidate.title),
                author=payload.get("author"),
                language=payload.get("language"),
                publish_year=payload.get("publish_year"),
            )
            evaluation = evaluate_candidate(candidate, profile)
        except (ReleaseProfileError, TypeError, ValueError) as exc:
            return error("invalid_profile_preview", str(exc), 400)
        return jsonify(
            {
                "evaluation": {
                    "fingerprint": evaluation.fingerprint,
                    "automatic_eligible": evaluation.accepted,
                    "total_score": evaluation.total_score,
                    "components": evaluation.components,
                    "reject_reasons": list(evaluation.reject_reasons),
                }
            }
        )

    def _library_capability(user_id: int) -> str | None:
        with connect() as conn:
            row = conn.execute(
                "SELECT library_capability FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return str(row["library_capability"]) if row else None

    @blueprint.get("/catalog/recent")
    @require_actor()
    def catalog_recent() -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        try:
            offset, limit = page()
            return jsonify(
                catalog_service.recent(user_id=actor.user_id, offset=offset, limit=limit)
            )
        except ValueError as exc:
            return error("invalid_pagination", str(exc), 400)

    @blueprint.get("/suggestions")
    @require_actor()
    def suggestions_dashboard() -> Response:
        actor: V1Actor = g.v1_actor
        return jsonify(suggestion_service.dashboard(user_id=actor.user_id, per_list_limit=10))

    @blueprint.get("/suggestions/<string:list_key>")
    @require_actor()
    def suggestions_list(list_key: str) -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        try:
            offset, limit = page()
            return jsonify(
                suggestion_service.list_suggestions(
                    list_key=list_key,
                    user_id=actor.user_id,
                    offset=offset,
                    limit=limit,
                )
            )
        except ValueError as exc:
            return error("invalid_suggestion_list", str(exc), 400)

    @blueprint.get("/admin/bestseller/status")
    @require_actor(admin=True)
    def bestseller_status() -> Response:
        return jsonify(suggestion_service.status())

    @blueprint.post("/admin/bestseller/sync")
    @require_actor(admin=True)
    def schedule_bestseller_sync() -> tuple[Response, int]:
        """Queue a forced sync for the existing durable worker handler.

        The handler still obtains the Bestseller service's own SQLite lease,
        preventing this explicit request from overlapping an automatic check
        or a second BookFlow process.
        """
        job = job_service.enqueue(
            job_key=f"bestseller60-manual:{uuid.uuid4().hex}",
            job_type="sync_bestseller60",
            payload={"trigger": "admin"},
        )
        return jsonify({"scheduled": True, "job_id": job.id, "job_key": job.job_key}), 202

    @blueprint.get("/catalog/books")
    @require_actor()
    def catalog_books() -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        try:
            offset, limit = page()
            author = request.args.get("author_id")
            series = request.args.get("series_id")
            author_id = int(author) if author else None
            series_id = int(series) if series else None
            if author_id is not None and author_id < 1 or series_id is not None and series_id < 1:
                raise ValueError("author_id and series_id must be positive integers")  # noqa: TRY301
            return jsonify(
                catalog_service.catalog_books(
                    user_id=actor.user_id,
                    query=request.args.get("q"),
                    author_id=author_id,
                    series_id=series_id,
                    offset=offset,
                    limit=limit,
                    sort=request.args.get("sort", "title"),
                    file_format=request.args.get("format"),
                )
            )
        except (ValueError, CatalogMonitorError) as exc:
            return error("invalid_catalog_query", str(exc), 400)

    @blueprint.get("/search/books")
    @require_actor()
    def search_books() -> Response | tuple[Response, int]:
        """Return a small, local-only cross-catalog autocomplete result set.

        This intentionally does not reuse the catalog endpoint: catalog cards require
        an imported file, whereas a search result may be metadata or a suggestion.
        """
        actor: V1Actor = g.v1_actor
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify({"items": [], "limit": 8})
        try:
            limit = min(12, max(1, int(request.args.get("limit", "8"))))
        except ValueError:
            return error("invalid_search_query", "limit must be an integer", 400)
        terms = [token.replace('"', '""') for token in query.split() if token]
        if not terms:
            return jsonify({"items": [], "limit": limit})
        fts_query = " AND ".join(f'"{term}"*' for term in terms)
        with connect() as conn:
            has_bestseller_entries = (
                conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'bestseller_entries'"
                ).fetchone()
                is not None
            )
            trending_sql = (
                "EXISTS (SELECT 1 FROM bestseller_entries be WHERE be.book_id = b.id)"
                if has_bestseller_entries
                else "0"
            )
            rows = conn.execute(
                f"""
                SELECT b.id, b.title, b.author, b.cover_url, b.publish_year, b.isbn_13,
                       COALESCE(b.catalog_visible, 1) AS catalog_visible,
                       EXISTS (SELECT 1 FROM book_files bf WHERE bf.book_id = b.id
                               AND bf.import_status = 'imported') AS has_imported_file,
                       EXISTS (SELECT 1 FROM user_library ul WHERE ul.user_id = ?
                               AND ul.book_id = b.id) AS in_library,
                       EXISTS (SELECT 1 FROM requests r WHERE r.user_id = ?
                               AND r.book_id = b.id) AS requested,
                       {trending_sql} AS is_trending
                FROM books_fts
                JOIN books b ON b.id = books_fts.rowid
                WHERE books_fts MATCH ?
                ORDER BY bm25(books_fts), b.id
                LIMIT ?
                """,  # noqa: S608 - only the constant trending expression is interpolated.
                (actor.user_id, actor.user_id, fts_query, limit),
            ).fetchall()
        return jsonify(
            {
                "items": [
                    {
                        **dict(row),
                        "catalog_visible": bool(row["catalog_visible"]),
                        "has_imported_file": bool(row["has_imported_file"]),
                        "in_library": bool(row["in_library"]),
                        "requested": bool(row["requested"]),
                        "is_trending": bool(row["is_trending"]),
                    }
                    for row in rows
                ],
                "limit": limit,
            }
        )

    @blueprint.post("/library/books/bulk")
    @require_actor()
    def bulk_add_library_books() -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        payload = request.get_json(silent=True)
        raw_book_ids = payload.get("book_ids") if isinstance(payload, dict) else None
        if not isinstance(raw_book_ids, list) or not 1 <= len(raw_book_ids) <= 100:
            return error(
                "invalid_book_ids",
                "book_ids must be a list containing between 1 and 100 positive integers",
                400,
            )

        book_ids: list[int] = []
        seen: set[int] = set()
        for raw_book_id in raw_book_ids:
            if isinstance(raw_book_id, bool) or not isinstance(raw_book_id, int) or raw_book_id < 1:
                return error(
                    "invalid_book_ids",
                    "book_ids must contain only positive integers",
                    400,
                )
            if raw_book_id not in seen:
                seen.add(raw_book_id)
                book_ids.append(raw_book_id)

        results = library_service.add_many_to_library(user_id=actor.user_id, book_ids=book_ids)
        summary = {
            "submitted": len(book_ids),
            "added": sum(item["status"] == "added" for item in results),
            "already_in_library": sum(item["status"] == "already_in_library" for item in results),
            "failed": sum(item["status"] in {"unavailable", "not_found"} for item in results),
        }
        return jsonify({"results": results, "summary": summary})

    @blueprint.post("/catalog/presence")
    @require_actor()
    def catalog_presence() -> Response | tuple[Response, int]:
        """Resolve external-result identities against the complete local Book set."""
        payload = request.get_json(silent=True)
        candidates = payload.get("candidates") if isinstance(payload, dict) else None
        if not isinstance(candidates, list) or len(candidates) > 50:
            return error("invalid_candidates", "candidates must be a list of at most 50 items", 400)
        present: list[int] = []
        with connect() as conn:
            for index, candidate in enumerate(candidates):
                if not isinstance(candidate, dict):
                    return error("invalid_candidates", "Each candidate must be an object", 400)
                provider = str(candidate.get("provider") or "").strip().casefold()
                provider_book_id = str(candidate.get("provider_book_id") or "").strip()
                raw_isbn = str(candidate.get("isbn") or "").strip()
                normalized = normalize_isbn(raw_isbn) if raw_isbn else None
                isbn_values = tuple(
                    value
                    for value in (
                        normalized.isbn13 if normalized else None,
                        normalized.isbn10 if normalized else None,
                    )
                    if value is not None
                )
                found = None
                if provider and provider_book_id:
                    found = conn.execute(
                        """
                        SELECT 1 FROM books b
                        WHERE lower(b.metadata_provider) = ? AND b.provider_book_id = ?
                          AND EXISTS (
                              SELECT 1 FROM book_files bf
                              WHERE bf.book_id = b.id AND bf.import_status = 'imported'
                          )
                        UNION ALL
                        SELECT 1 FROM book_provider_identifiers bpi
                        JOIN books b ON b.id = bpi.book_id
                        WHERE lower(bpi.provider) = ? AND bpi.provider_book_id = ?
                          AND EXISTS (
                              SELECT 1 FROM book_files bf
                              WHERE bf.book_id = b.id AND bf.import_status = 'imported'
                          )
                        LIMIT 1
                        """,
                        (provider, provider_book_id, provider, provider_book_id),
                    ).fetchone()
                if found is None and isbn_values:
                    placeholders = ",".join("?" for _ in isbn_values)
                    found = conn.execute(
                        f"""
                        SELECT 1 FROM book_identifiers bi
                        JOIN books b ON b.id = bi.book_id
                        WHERE bi.identifier_type IN ('isbn10', 'isbn13')
                          AND bi.identifier IN ({placeholders})
                          AND EXISTS (
                              SELECT 1 FROM book_files bf
                              WHERE bf.book_id = b.id AND bf.import_status = 'imported'
                          )
                        UNION ALL
                        SELECT 1 FROM books b
                        WHERE REPLACE(REPLACE(TRIM(COALESCE(b.isbn_13, '')), '-', ''), ' ', '')
                              IN ({placeholders})
                          AND EXISTS (
                              SELECT 1 FROM book_files bf
                              WHERE bf.book_id = b.id AND bf.import_status = 'imported'
                          )
                        LIMIT 1
                        """,  # noqa: S608 - only the placeholder count is generated.
                        (*isbn_values, *isbn_values),
                    ).fetchone()
                if found is not None:
                    present.append(index)
        return jsonify({"present": present})

    @blueprint.get("/catalog/authors")
    @require_actor()
    def catalog_authors() -> Response | tuple[Response, int]:
        try:
            offset, limit = page()
            return jsonify(
                catalog_service.catalog_entities(
                    entity="authors", query=request.args.get("q"), offset=offset, limit=limit
                )
            )
        except ValueError as exc:
            return error("invalid_pagination", str(exc), 400)

    def _catalog_book_detail(book_id: int) -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        with connect() as conn:
            book_ids = equivalent_book_ids(conn, book_id)
            if not book_ids:
                return error("book_not_found", "Book not found", 404)
            representative_id = canonical_book_id(conn, book_ids)
            book = conn.execute("SELECT * FROM books WHERE id = ?", (representative_id,)).fetchone()
            placeholders = ",".join("?" for _ in book_ids)
            in_library = (
                conn.execute(
                    f"""
                    SELECT 1 FROM user_library
                    WHERE user_id = ? AND book_id IN ({placeholders})
                    LIMIT 1
                    """,  # noqa: S608 - only the placeholder count is generated.
                    (actor.user_id, *book_ids),
                ).fetchone()
                is not None
            )
            entitled = (
                conn.execute(
                    f"""
                SELECT 1 FROM book_files bf
                JOIN user_file_entitlements ufe ON ufe.file_id = bf.id
                WHERE ufe.user_id = ? AND bf.book_id IN ({placeholders})
                  AND bf.import_status = 'imported'
                """,  # noqa: S608 - only the placeholder count is generated.
                    (actor.user_id, *book_ids),
                ).fetchone()
                is not None
            )
            request_row = conn.execute(
                f"""
                SELECT id, status, add_to_kobo_when_available, created_at, updated_at
                FROM requests WHERE user_id = ? AND book_id IN ({placeholders})
                ORDER BY id DESC LIMIT 1
                """,  # noqa: S608 - only the placeholder count is generated.
                (actor.user_id, *book_ids),
            ).fetchone()
            library_row = conn.execute(
                f"""
                SELECT added_at FROM user_library
                WHERE user_id = ? AND book_id IN ({placeholders})
                ORDER BY added_at ASC LIMIT 1
                """,  # noqa: S608 - only the placeholder count is generated.
                (actor.user_id, *book_ids),
            ).fetchone()
            progress_row = conn.execute(
                f"""
                SELECT progress, active, source, updated_at
                FROM user_reading_progress
                WHERE user_id = ? AND book_id IN ({placeholders})
                ORDER BY updated_at DESC LIMIT 1
                """,  # noqa: S608 - only the placeholder count is generated.
                (actor.user_id, *book_ids),
            ).fetchone()
            entitled_files = conn.execute(
                f"""
                SELECT DISTINCT bf.file_format
                FROM book_files bf
                JOIN user_file_entitlements ufe ON ufe.file_id = bf.id
                WHERE bf.book_id IN ({placeholders}) AND ufe.user_id = ?
                  AND bf.import_status = 'imported'
                ORDER BY bf.file_format COLLATE NOCASE
                """,  # noqa: S608 - only the placeholder count is generated.
                (*book_ids, actor.user_id),
            ).fetchall()
            entitled_file_count = conn.execute(
                f"""
                SELECT COUNT(*) AS file_count
                FROM book_files bf
                JOIN user_file_entitlements ufe ON ufe.file_id = bf.id
                WHERE bf.book_id IN ({placeholders}) AND ufe.user_id = ?
                  AND bf.import_status = 'imported'
                """,  # noqa: S608 - only the placeholder count is generated.
                (*book_ids, actor.user_id),
            ).fetchone()
            imported_files = conn.execute(
                f"""
                SELECT DISTINCT file_format FROM book_files
                WHERE book_id IN ({placeholders}) AND import_status = 'imported'
                ORDER BY file_format COLLATE NOCASE
                """,  # noqa: S608 - only the placeholder count is generated.
                book_ids,
            ).fetchall()
            kobo_row = conn.execute(
                f"""
                SELECT desired_state FROM user_kobo_books
                WHERE user_id = ? AND book_id IN ({placeholders})
                ORDER BY revision DESC LIMIT 1
                """,  # noqa: S608 - only the placeholder count is generated.
                (actor.user_id, *book_ids),
            ).fetchone()
        item = dict(book)
        try:
            item["metadata"] = json.loads(str(item.get("metadata_json") or "{}"))
        except json.JSONDecodeError:
            item["metadata"] = {}
        return jsonify(
            {
                "book": item,
                "in_library": in_library,
                "entitled": entitled,
                # Delivery is a file fact, not a placeholder derived from an
                # absent request/request_process row.  Clients can distinguish a
                # globally available import from their own entitlement.
                "delivery_state": "available" if imported_files else None,
                "available_formats": [file["file_format"] for file in imported_files],
                "request": dict(request_row) if request_row else None,
                "personal_status": {
                    "library_added_at": library_row["added_at"] if library_row else None,
                    "reading_progress": dict(progress_row) if progress_row else None,
                    "available_formats": [file["file_format"] for file in entitled_files],
                    "file_count": int(entitled_file_count["file_count"]),
                    "kobo_state": kobo_row["desired_state"] if kobo_row else None,
                },
            }
        )

    @blueprint.get("/catalog/books/isbn/<string:raw_isbn>")
    @require_actor()
    def catalog_book_by_isbn(raw_isbn: str) -> Response | tuple[Response, int]:
        normalized = normalize_isbn(raw_isbn)
        if normalized is None:
            return error("invalid_isbn", "ISBN is invalid", 400)
        identifiers = tuple(
            value for value in (normalized.isbn13, normalized.isbn10) if value is not None
        )
        placeholders = ",".join("?" for _ in identifiers)
        with connect() as conn:
            rows = conn.execute(
                f"""
                SELECT DISTINCT book_id FROM book_identifiers
                WHERE identifier_type IN ('isbn10', 'isbn13')
                  AND identifier IN ({placeholders})
                ORDER BY book_id
                """,  # noqa: S608 - placeholders are generated, not user input.
                identifiers,
            ).fetchall()
            if not rows and normalized.isbn13 is not None:
                rows = conn.execute(
                    "SELECT id AS book_id FROM books WHERE isbn_13 = ? ORDER BY id",
                    (normalized.isbn13,),
                ).fetchall()
        if not rows:
            return error("book_not_found", "Book not found", 404)
        book_ids = sorted({int(row["book_id"]) for row in rows})
        with connect() as conn:
            representative_id = canonical_book_id(conn, book_ids)
        return _catalog_book_detail(representative_id)

    @blueprint.get("/catalog/books/<int:book_id>")
    @require_actor()
    def catalog_book_detail(book_id: int) -> Response | tuple[Response, int]:
        return _catalog_book_detail(book_id)

    @blueprint.get("/catalog/series")
    @require_actor()
    def catalog_series() -> Response | tuple[Response, int]:
        try:
            offset, limit = page()
            return jsonify(
                catalog_service.catalog_entities(
                    entity="series", query=request.args.get("q"), offset=offset, limit=limit
                )
            )
        except ValueError as exc:
            return error("invalid_pagination", str(exc), 400)

    @blueprint.get("/monitors")
    @require_actor()
    def list_monitors() -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        try:
            offset, limit = page()
            return jsonify(
                catalog_service.list_monitors(user_id=actor.user_id, offset=offset, limit=limit)
            )
        except ValueError as exc:
            return error("invalid_pagination", str(exc), 400)

    @blueprint.post("/monitors")
    @require_actor()
    def create_monitor() -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error("invalid_monitor", "Request body must be an object", 400)
        try:
            monitor = catalog_service.create_monitor(
                user_id=actor.user_id,
                payload=payload,
                is_admin=actor.is_admin,
                library_capability=_library_capability(actor.user_id),
            )
        except PermissionError as exc:
            return error("forbidden", str(exc), 403)
        except CatalogMonitorError as exc:
            return error("invalid_monitor", str(exc), 400)
        job_service.enqueue(
            job_key=f"scan-monitor:{monitor['id']}",
            job_type="scan_monitor",
            payload={"monitor_id": monitor["id"]},
        )
        return jsonify({"monitor": monitor}), 201

    @blueprint.patch("/monitors/<int:monitor_id>")
    @require_actor()
    def update_monitor(monitor_id: int) -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error("invalid_monitor", "Request body must be an object", 400)
        try:
            monitor = catalog_service.update_monitor(
                user_id=actor.user_id,
                monitor_id=monitor_id,
                payload=payload,
                is_admin=actor.is_admin,
                library_capability=_library_capability(actor.user_id),
            )
        except PermissionError as exc:
            return error("forbidden", str(exc), 403)
        except CatalogMonitorError as exc:
            return error("invalid_monitor", str(exc), 400)
        if monitor is None:
            return error("monitor_not_found", "Monitor not found", 404)
        return jsonify({"monitor": monitor})

    @blueprint.delete("/monitors/<int:monitor_id>")
    @require_actor()
    def delete_monitor(monitor_id: int) -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        if not catalog_service.delete_monitor(user_id=actor.user_id, monitor_id=monitor_id):
            return error("monitor_not_found", "Monitor not found", 404)
        return jsonify({"monitor_id": monitor_id, "deleted": True})

    @blueprint.post("/monitors/<int:monitor_id>/scan")
    @require_actor()
    def scan_monitor(monitor_id: int) -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        if not catalog_service.schedule_scan(user_id=actor.user_id, monitor_id=monitor_id):
            return error("monitor_not_found", "Active monitor not found", 404)
        job_service.enqueue(
            job_key=f"scan-monitor:{monitor_id}:{uuid.uuid4().hex}",
            job_type="scan_monitor",
            payload={"monitor_id": monitor_id},
        )
        return jsonify({"monitor_id": monitor_id, "scheduled": True}), 202

    @blueprint.get("/monitors/<int:monitor_id>/discoveries")
    @require_actor()
    def list_monitor_discoveries(monitor_id: int) -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        try:
            offset, limit = page()
            discoveries = catalog_service.list_discoveries(
                user_id=actor.user_id, monitor_id=monitor_id, offset=offset, limit=limit
            )
        except ValueError as exc:
            return error("invalid_pagination", str(exc), 400)
        if discoveries is None:
            return error("monitor_not_found", "Monitor not found", 404)
        return jsonify(discoveries)

    @blueprint.patch("/monitors/<int:monitor_id>/discoveries/<int:discovery_id>")
    @require_actor()
    def update_monitor_discovery(
        monitor_id: int, discovery_id: int
    ) -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        payload = request.get_json(silent=True)
        state = payload.get("state") if isinstance(payload, dict) else None
        if not isinstance(state, str):
            return error("invalid_discovery", "state is required", 400)
        try:
            discovery = catalog_service.set_discovery_state(
                user_id=actor.user_id,
                monitor_id=monitor_id,
                discovery_id=discovery_id,
                state=state,
            )
        except CatalogMonitorError as exc:
            return error("invalid_discovery", str(exc), 400)
        if discovery is None:
            return error("discovery_not_found", "Monitor discovery not found", 404)
        return jsonify({"discovery": discovery})

    @blueprint.get("/users/me/reading-progress")
    @require_actor()
    def active_reading_progress() -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        try:
            offset, limit = page()
            result = catalog_service.list_active_progress(
                user_id=actor.user_id, offset=offset, limit=limit
            )
            result["items"] = [serialize_reader_progress(item) for item in result["items"]]
            return jsonify(result)
        except ValueError as exc:
            return error("invalid_pagination", str(exc), 400)

    @blueprint.get("/users/me/reading-progress/<int:book_id>")
    @require_actor()
    def get_reading_progress(book_id: int) -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        progress = catalog_service.get_progress(user_id=actor.user_id, book_id=book_id)
        if progress is None:
            return error("progress_not_found", "Reading progress not found", 404)
        return jsonify({"progress": serialize_reader_progress(progress)})

    @blueprint.route("/users/me/reading-progress/<int:book_id>", methods=["PATCH", "PUT"])
    @require_actor()
    def patch_reading_progress(book_id: int) -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return error("invalid_progress", "Request body must be an object", 400)
        raw_progress = payload.get("progress")
        raw_progression = payload.get("progression")
        if raw_progression is not None:
            if isinstance(raw_progression, bool) or not isinstance(raw_progression, (int, float)):
                return error("invalid_progress", "progression must be a number", 400)
            if not 0 <= float(raw_progression) <= 1:
                return error("invalid_progress", "progression must be from 0 to 1", 400)
            progress = float(raw_progression) * 100
        elif not isinstance(raw_progress, bool) and isinstance(raw_progress, (int, float)):
            progress = float(raw_progress)
        else:
            return error("invalid_progress", "progress or progression must be a number", 400)
        reset = payload.get("reset", False)
        if not isinstance(reset, bool):
            return error("invalid_progress", "reset must be a boolean", 400)
        completed = payload.get("completed")
        if completed is not None:
            if not isinstance(completed, bool):
                return error("invalid_progress", "completed must be a boolean", 400)
            if completed and progress != 100:
                return error("invalid_progress", "completed progress must be 1", 400)

        reader_keys = {"file_id", "locator", "client_updated_at"}
        reader_update = any(key in payload for key in reader_keys)
        if reader_update and not reader_keys.issubset(payload):
            return error(
                "invalid_progress",
                "file_id, locator and client_updated_at are required for reader progress",
                400,
            )
        try:
            updated = catalog_service.set_progress(
                user_id=actor.user_id,
                book_id=book_id,
                progress=progress,
                source="web",
                reset=reset,
                file_id=payload.get("file_id") if reader_update else None,
                locator=payload.get("locator") if reader_update else None,
                client_updated_at=payload.get("client_updated_at") if reader_update else None,
            )
        except PermissionError as exc:
            return error("book_not_found", str(exc), 404)
        except CatalogMonitorError as exc:
            return error("invalid_progress", str(exc), 409)
        return jsonify({"progress": serialize_reader_progress(updated)})

    @blueprint.delete("/users/me/reading-progress/<int:book_id>")
    @require_actor()
    def delete_reading_progress(book_id: int) -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        try:
            deleted = catalog_service.clear_progress(user_id=actor.user_id, book_id=book_id)
        except PermissionError as exc:
            return error("book_not_found", str(exc), 404)
        if not deleted:
            return error("progress_not_found", "Reading progress not found", 404)
        return jsonify({"book_id": book_id, "reset": True})

    @blueprint.get("/users/me/requests")
    @require_actor()
    def current_user_requests() -> Response | tuple[Response, int]:
        """List the signed-in reader's requests with dashboard card metadata."""
        actor: V1Actor = g.v1_actor
        try:
            offset, limit = page(default_limit=10)
        except ValueError as exc:
            return error("invalid_pagination", str(exc), 400)

        with connect() as conn:
            total = int(
                conn.execute(
                    "SELECT COUNT(*) FROM requests WHERE user_id = ?", (actor.user_id,)
                ).fetchone()[0]
            )
            rows = conn.execute(
                """
                SELECT requests.id, requests.book_id, books.title, books.author, books.cover_url,
                       requests.request_process_id, requests.content_type, requests.status,
                       requests.add_to_kobo_when_available,
                       requests.profile_id, requests.profile_name,
                       COALESCE(
                           request_processes.status,
                           CASE WHEN requests.status = 'fulfilled' THEN 'available' END
                       ) AS delivery_state,
                       requests.created_at, requests.updated_at
                FROM requests
                JOIN books ON books.id = requests.book_id
                LEFT JOIN request_processes ON request_processes.id = requests.request_process_id
                WHERE requests.user_id = ?
                ORDER BY requests.created_at DESC, requests.id DESC
                LIMIT ? OFFSET ?
                """,
                (actor.user_id, limit, offset),
            ).fetchall()

        return jsonify(
            {
                "items": [dict(row) for row in rows],
                "total": total,
                "offset": offset,
                "limit": limit,
            }
        )

    @blueprint.get("/library/books")
    @require_actor()
    def library_books() -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        try:
            offset, limit = page()
            sort = request.args.get("sort", "recent")
            file_state = request.args.get("file_state", "all")
            shelf = request.args.get("shelf", "")
            file_format = (request.args.get("format") or "").strip()
            if sort not in {"recent", "title", "author", "year"}:
                return error("invalid_library_query", "Invalid library sort", 400)
            if file_state not in {"all", "with-files", "needs-files"}:
                return error("invalid_library_query", "Invalid file_state filter", 400)
            if shelf not in {"", "kobo"}:
                return error("invalid_library_query", "Invalid library shelf", 400)
            clauses = [
                """EXISTS (SELECT 1 FROM user_library ul
                    WHERE ul.book_id = books.id AND ul.user_id = ?)"""
            ]
            where_params: list[Any] = [actor.user_id]
            if shelf == "kobo":
                clauses.append(
                    """EXISTS (SELECT 1 FROM user_kobo_books shelf_ukb
                        WHERE shelf_ukb.book_id = books.id
                          AND shelf_ukb.user_id = ?
                          AND shelf_ukb.desired_state = 'active')"""
                )
                where_params.append(actor.user_id)
            joins = ""
            query = (request.args.get("q") or "").strip()
            if query:
                terms = [token.replace('"', '""') for token in query.split() if token]
                if terms:
                    joins = " INNER JOIN books_fts ON books_fts.rowid = books.id"
                    clauses.append("books_fts MATCH ?")
                    where_params.append(" AND ".join(f'"{term}"*' for term in terms))
            entitled_file = """EXISTS (
                SELECT 1 FROM book_files filter_bf
                JOIN user_file_entitlements filter_ufe ON filter_ufe.file_id = filter_bf.id
                WHERE filter_bf.book_id = books.id AND filter_ufe.user_id = ?
                  AND filter_bf.import_status = 'imported'"""
            if file_format:
                entitled_file += " AND LOWER(filter_bf.file_format) = LOWER(?)"
            entitled_file += ")"
            if file_state != "all" or file_format:
                clauses.append(
                    entitled_file if file_state != "needs-files" else f"NOT {entitled_file}"
                )
                where_params.append(actor.user_id)
                if file_format:
                    where_params.append(file_format)
            order = {
                "recent": "library_added_at IS NULL, library_added_at DESC, books.id DESC",
                "title": "books.title COLLATE NOCASE, books.id",
                "author": "books.author COLLATE NOCASE, books.title COLLATE NOCASE, books.id",
                "year": "books.publish_year IS NULL, books.publish_year DESC, books.title COLLATE NOCASE, books.id",
            }[sort]
            where_sql = " AND ".join(clauses)
            with connect() as conn:
                total = int(
                    conn.execute(
                        f"SELECT COUNT(DISTINCT books.id) FROM books{joins} WHERE {where_sql}",  # noqa: S608
                        where_params,
                    ).fetchone()[0]
                )
                rows = conn.execute(
                    f"""
                    SELECT books.id, books.title, books.author, books.language,
                           books.publish_year, books.cover_url,
                           (
                               SELECT ul.added_at FROM user_library ul
                               WHERE ul.book_id = books.id AND ul.user_id = ?
                           ) AS library_added_at,
                           COALESCE((
                               SELECT json_group_array(DISTINCT bf.file_format)
                               FROM book_files bf
                               JOIN user_file_entitlements ufe ON ufe.file_id = bf.id
                               WHERE bf.book_id = books.id AND ufe.user_id = ?
                                 AND bf.import_status = 'imported'
                           ), '[]') AS formats_json,
                           (
                               SELECT r.status FROM requests r
                               WHERE r.book_id = books.id AND r.user_id = ?
                               ORDER BY r.created_at DESC, r.id DESC LIMIT 1
                           ) AS request_status,
                           (
                               SELECT a.status FROM requests r
                               JOIN request_processes a ON a.id = r.request_process_id
                               WHERE r.book_id = books.id AND r.user_id = ?
                               ORDER BY r.created_at DESC, r.id DESC LIMIT 1
                           ) AS request_process_status,
                           (
                               SELECT ukb.desired_state FROM user_kobo_books ukb
                               WHERE ukb.book_id = books.id AND ukb.user_id = ?
                           ) AS kobo_state
                    FROM books{joins}
                    WHERE {where_sql}
                    ORDER BY {order}
                    LIMIT ? OFFSET ?
                    """,  # noqa: S608
                    [
                        actor.user_id,
                        actor.user_id,
                        actor.user_id,
                        actor.user_id,
                        actor.user_id,
                        *where_params,
                        limit,
                        offset,
                    ],
                ).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                item["formats"] = json.loads(item.pop("formats_json"))
                items.append(item)
            return jsonify({"items": items, "total": total, "offset": offset, "limit": limit})
        except ValueError as exc:
            return error("invalid_library_query", str(exc), 400)

    @blueprint.get("/library/books/<int:book_id>")
    @require_actor()
    def library_book_detail(book_id: int) -> Response | tuple[Response, int]:
        actor: V1Actor = g.v1_actor
        with connect() as conn:
            book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
            allowed = (
                actor.is_admin
                or conn.execute(
                    """
                    SELECT 1 FROM user_library WHERE user_id = ? AND book_id = ?
                    UNION ALL
                    SELECT 1 FROM requests WHERE user_id = ? AND book_id = ?
                    UNION ALL
                    SELECT 1 FROM user_file_entitlements
                    JOIN book_files ON book_files.id = file_id
                    WHERE user_id = ? AND book_files.book_id = ?
                      AND book_files.import_status = 'imported'
                    """,
                    (
                        actor.user_id,
                        book_id,
                        actor.user_id,
                        book_id,
                        actor.user_id,
                        book_id,
                    ),
                ).fetchone()
            )
            if book is None or not allowed:
                return error("book_not_found", "Book not found", 404)
            files = conn.execute(
                """
                SELECT book_files.id, book_files.file_format, book_files.size_bytes,
                       book_files.mime_type, book_files.imported_at,
                       user_file_entitlements.granted_at
                FROM book_files
                JOIN user_file_entitlements ON user_file_entitlements.file_id = book_files.id
                WHERE book_files.book_id = ? AND user_file_entitlements.user_id = ?
                  AND book_files.import_status = 'imported'
                ORDER BY book_files.file_format, book_files.id
                """,
                (book_id, actor.user_id),
            ).fetchall()
            request_row = conn.execute(
                """
                SELECT requests.*, request_processes.status AS request_process_status,
                       request_processes.monitoring_enabled, request_processes.next_search_at
                FROM requests
                LEFT JOIN request_processes ON request_processes.id = requests.request_process_id
                WHERE requests.user_id = ? AND requests.book_id = ?
                ORDER BY requests.created_at DESC, requests.id DESC LIMIT 1
                """,
                (actor.user_id, book_id),
            ).fetchone()
            kobo_row = conn.execute(
                "SELECT * FROM user_kobo_books WHERE user_id = ? AND book_id = ?",
                (actor.user_id, book_id),
            ).fetchone()
        serialized_files = []
        for file in files:
            serialized_file = dict(file)
            mode = reader_mode(serialized_file.get("file_format"))
            serialized_file["browser_reader"] = mode
            serialized_file["content_url"] = (
                f"/api/v1/library/books/{book_id}/files/{serialized_file['id']}/content"
                if mode is not None
                else None
            )
            serialized_file["download_url"] = (
                f"/api/v1/library/books/{book_id}/files/{serialized_file['id']}/download"
            )
            serialized_files.append(serialized_file)
        return jsonify(
            {
                "book": dict(book),
                "files": serialized_files,
                "entitled": bool(files),
                "request": dict(request_row) if request_row else None,
                "kobo": dict(kobo_row) if kobo_row else None,
            }
        )

    @blueprint.get("/library/books/<int:book_id>/files")
    @require_actor()
    def library_files(book_id: int) -> Response | tuple[Response, int]:
        return library_book_detail(book_id)

    @blueprint.route(
        "/library/books/<int:book_id>/files/<int:file_id>/content", methods=["GET", "HEAD"]
    )
    @require_actor()
    def library_reader_content(book_id: int, file_id: int) -> Response | tuple[Response, int]:
        """Stream one entitled imported file for the browser reader.

        The query deliberately requires both Book-level library membership and
        this exact file's entitlement.  A file id from another Book, another
        user, or a removed library entry is indistinguishable from a missing
        resource to avoid leaking the shared storage catalog.
        """
        actor: V1Actor = g.v1_actor
        with connect() as conn:
            file_row = conn.execute(
                """SELECT bf.storage_path, bf.file_format, bf.mime_type
                FROM user_library ul
                JOIN book_files bf ON bf.book_id = ul.book_id
                JOIN user_file_entitlements ufe ON ufe.file_id = bf.id
                WHERE ul.user_id = ? AND ul.book_id = ? AND bf.id = ?
                  AND ufe.user_id = ul.user_id AND bf.import_status = 'imported'""",
                (actor.user_id, book_id, file_id),
            ).fetchone()
        if file_row is None:
            return error("file_not_found", "File not found", 404)
        if reader_mode(file_row["file_format"]) is None:
            return error("file_not_found", "File not found", 404)
        if resolve_file_path is None:
            return error("file_unavailable", "Reader file service is unavailable", 503)
        try:
            path = resolve_file_path(str(file_row["storage_path"]))
        except OSError, ValueError, PathSecurityError:
            return error("file_unavailable", "File is unavailable", 404)
        if not path.is_file():
            return error("file_unavailable", "File is unavailable", 404)
        return send_file(
            path,
            mimetype=reader_content_type(file_row["file_format"], file_row["mime_type"]),
            as_attachment=False,
            download_name=path.name,
            conditional=True,
            etag=True,
        )

    @blueprint.route(
        "/library/books/<int:book_id>/files/<int:file_id>/download", methods=["GET", "HEAD"]
    )
    @require_actor()
    def library_file_download(book_id: int, file_id: int) -> Response | tuple[Response, int]:
        """Download one entitled imported file, including non-reader formats."""
        actor: V1Actor = g.v1_actor
        with connect() as conn:
            file_row = conn.execute(
                """SELECT bf.storage_path, bf.file_format, bf.mime_type
                FROM user_library ul
                JOIN book_files bf ON bf.book_id = ul.book_id
                JOIN user_file_entitlements ufe ON ufe.file_id = bf.id
                WHERE ul.user_id = ? AND ul.book_id = ? AND bf.id = ?
                  AND ufe.user_id = ul.user_id AND bf.import_status = 'imported'""",
                (actor.user_id, book_id, file_id),
            ).fetchone()
        if file_row is None:
            return error("file_not_found", "File not found", 404)
        if resolve_file_path is None:
            return error("file_unavailable", "File service is unavailable", 503)
        try:
            path = resolve_file_path(str(file_row["storage_path"]))
        except OSError, ValueError, PathSecurityError:
            return error("file_unavailable", "File is unavailable", 404)
        if not path.is_file():
            return error("file_unavailable", "File is unavailable", 404)
        return send_file(
            path,
            mimetype=reader_content_type(file_row["file_format"], file_row["mime_type"]),
            as_attachment=True,
            download_name=path.name,
            conditional=True,
            etag=True,
        )

    @blueprint.post("/requests")
    @require_actor()
    def create_request() -> tuple[Response, int] | Response:
        actor: V1Actor = g.v1_actor
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("book_id"), int):
            return error("invalid_request", "book_id must be an integer", 400)
        idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
        if len(idempotency_key) > 128:
            return error("invalid_idempotency_key", "Idempotency-Key is too long", 400)
        request_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        if idempotency_key:
            with connect() as conn:
                replay = conn.execute(
                    """
                    SELECT request_hash, response_status, response_json
                    FROM api_idempotency_keys
                    WHERE actor_user_id = ? AND method = 'POST'
                      AND route = '/api/v1/requests' AND idempotency_key = ?
                    """,
                    (actor.user_id, idempotency_key),
                ).fetchone()
            if replay is not None:
                if replay["request_hash"] != request_hash:
                    return error(
                        "idempotency_conflict",
                        "Idempotency-Key was already used with a different request",
                        409,
                    )
                response = jsonify(json.loads(replay["response_json"]))
                response.headers["X-Correlation-Id"] = g.v1_correlation_id
                return response, int(replay["response_status"])
        profile_id = payload.get("profile_id")
        if isinstance(profile_id, bool) or not isinstance(profile_id, int):
            return error("invalid_profile", "profile_id must be an integer", 400)
        try:
            created = request_service.create_request(
                user_id=actor.user_id,
                book_id=payload["book_id"],
                profile_id=profile_id,
                content_type=str(payload.get("content_type", "ebook")),
                note=payload.get("note"),
                add_to_kobo_when_available=bool(payload.get("add_to_kobo_when_available", False)),
                reject_if_available=True,
            )
        except RequestWorkflowError as exc:
            return error(exc.code, str(exc), exc.status_code)
        except (TypeError, ValueError) as exc:
            return error("invalid_request", str(exc), 400)
        enqueue_request_status(created)
        if created.get("status") == "approved" and created.get("request_process_id") is not None:
            job_service.enqueue(
                job_key=f"search-request:{created['request_process_id']}",
                job_type="search_request",
                payload={"request_process_id": created["request_process_id"]},
            )
        status = 202 if created["status"] != "fulfilled" else 200
        response_body = {"request": created, "idempotency_key": idempotency_key or None}
        if idempotency_key:
            with connect() as conn:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO api_idempotency_keys (
                        actor_user_id, method, route, idempotency_key, request_hash,
                        response_status, response_json
                    )
                    VALUES (?, 'POST', '/api/v1/requests', ?, ?, ?, ?)
                    """,
                    (
                        actor.user_id,
                        idempotency_key,
                        request_hash,
                        status,
                        json.dumps(response_body, sort_keys=True, default=str),
                    ),
                )
        response = jsonify(response_body)
        response.headers["X-Correlation-Id"] = g.v1_correlation_id
        return response, status

    @blueprint.post("/requests/<int:request_id>/cancel")
    @require_actor()
    def cancel_request(request_id: int) -> tuple[Response, int] | Response:
        actor: V1Actor = g.v1_actor
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM requests WHERE id = ? AND user_id = ?", (request_id, actor.user_id)
            ).fetchone()
            if row is None:
                return error("request_not_found", "Request not found", 404)
            if row["status"] == "cancelled":
                return jsonify({"request": dict(row)}), 200
            if row["status"] not in {"pending_approval", "approved"}:
                return error("invalid_request_transition", "Request cannot be cancelled", 409)
            conn.execute(
                "UPDATE requests SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (request_id,),
            )
            request_process_id = row["request_process_id"]
            if request_process_id is not None:
                conn.execute(
                    "DELETE FROM request_process_recipients WHERE request_id = ?",
                    (request_id,),
                )
                recipient_count = conn.execute(
                    "SELECT COUNT(*) FROM request_process_recipients WHERE request_process_id = ?",
                    (request_process_id,),
                ).fetchone()[0]
                if recipient_count == 0:
                    conn.execute(
                        """
                        UPDATE request_processes
                        SET status = 'cancelled', monitoring_enabled = 0,
                            active_request_attempt_id = NULL, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ? AND status NOT IN ('completed', 'cancelled', 'failed')
                        """,
                        (request_process_id,),
                    )
            cancelled = conn.execute(
                "SELECT * FROM requests WHERE id = ?", (request_id,)
            ).fetchone()
        enqueue_request_status(dict(cancelled))
        return jsonify({"request": dict(cancelled)}), 202

    @blueprint.get("/admin/requests")
    @require_actor(admin=True)
    def admin_requests() -> Response | tuple[Response, int]:
        status = request.args.get("status")
        sql = """
            SELECT requests.*, books.title AS book_title, books.author AS book_author,
                   users.username AS requester_name
            FROM requests
            JOIN books ON books.id = requests.book_id
            LEFT JOIN users ON users.id = requests.user_id
        """
        parameters: tuple[Any, ...] = ()
        if status:
            sql += " WHERE requests.status = ?"
            parameters = (status,)
        sql += " ORDER BY requests.created_at DESC, requests.id DESC"
        try:
            return jsonify(paged(sql, parameters))
        except ValueError as exc:
            return error("invalid_pagination", str(exc), 400)

    @blueprint.post("/admin/requests/<int:request_id>/approve")
    @require_actor(admin=True)
    def approve_request(request_id: int) -> tuple[Response, int] | Response:
        actor: V1Actor = g.v1_actor
        try:
            approved, request_process, _created = request_service.approve_request(
                request_id,
                reviewer_user_id=actor.user_id,
            )
        except RequestWorkflowError as exc:
            return error(exc.code, str(exc), exc.status_code)
        if request_process is not None:
            job_service.enqueue(
                job_key=f"search-request:{request_process['id']}",
                job_type="search_request",
                payload={"request_process_id": request_process["id"]},
            )
        enqueue_request_status(approved)
        return jsonify({"request": approved, "request_process": request_process}), 202

    @blueprint.post("/admin/requests/<int:request_id>/reject")
    @require_actor(admin=True)
    def reject_request(request_id: int) -> tuple[Response, int] | Response:
        actor: V1Actor = g.v1_actor
        payload = request.get_json(silent=True) or {}
        with connect() as conn:
            row = conn.execute("SELECT status FROM requests WHERE id = ?", (request_id,)).fetchone()
            if row is None:
                return error("request_not_found", "Request not found", 404)
            if row["status"] == "rejected":
                return jsonify({"request_id": request_id, "status": "rejected"}), 200
            if row["status"] != "pending_approval":
                return error("invalid_request_transition", "Request cannot be rejected", 409)
            conn.execute(
                """
                UPDATE requests SET status = 'rejected', admin_note = ?, reviewed_by = ?,
                    reviewed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (payload.get("admin_note"), actor.user_id, request_id),
            )
            updated = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        enqueue_request_status(dict(updated))
        return jsonify({"request_id": request_id, "status": "rejected"}), 202

    @blueprint.get("/admin/request-processes")
    @require_actor(admin=True)
    def admin_request_processes() -> Response | tuple[Response, int]:
        try:
            return jsonify(
                paged("SELECT * FROM request_processes ORDER BY updated_at DESC, id DESC")
            )
        except ValueError as exc:
            return error("invalid_pagination", str(exc), 400)

    @blueprint.get("/admin/request-processes/<int:request_process_id>/candidates")
    @require_actor(admin=True)
    def candidates(request_process_id: int) -> Response | tuple[Response, int]:
        try:
            search_run_id = request.args.get("search_run_id", type=int)
            parameters: tuple[Any, ...] = (request_process_id, search_run_id, search_run_id)
            result = paged(
                """
                SELECT release_candidates.*,
                       EXISTS (
                           SELECT 1
                           FROM blocked_releases
                           WHERE blocked_releases.fingerprint = release_candidates.fingerprint
                             AND blocked_releases.unblocked_at IS NULL
                             AND (
                                 blocked_releases.scope = 'global'
                                 OR blocked_releases.request_process_id = release_candidates.request_process_id
                             )
                       ) AS blocked
                FROM release_candidates
                WHERE request_process_id = ?
                  AND (? IS NULL OR search_run_id = ?)
                ORDER BY total_score DESC, id
                """,
                parameters,
            )
            for item in result["items"]:
                item["score_components"] = json.loads(item.pop("score_components_json"))
                item["reject_reasons"] = json.loads(item.pop("reject_reasons_json"))
                item["selected_by_admin"] = int(item.get("selected_by") is not None)
            return jsonify(result)
        except ValueError as exc:
            return error("invalid_pagination", str(exc), 400)

    @blueprint.get("/admin/request-processes/<int:request_process_id>/search-runs")
    @require_actor(admin=True)
    def search_runs(request_process_id: int) -> Response | tuple[Response, int]:
        try:
            return jsonify(
                paged(
                    "SELECT * FROM request_search_runs WHERE request_process_id=? ORDER BY id DESC",
                    (request_process_id,),
                )
            )
        except ValueError as exc:
            return error("invalid_pagination", str(exc), 400)

    @blueprint.get(
        "/admin/request-processes/<int:request_process_id>/search-runs/<int:search_run_id>"
    )
    @require_actor(admin=True)
    def search_run(request_process_id: int, search_run_id: int) -> Response | tuple[Response, int]:
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM request_search_runs WHERE id=? AND request_process_id=?",
                (search_run_id, request_process_id),
            ).fetchone()
        if row is None:
            return error("search_run_not_found", "Search run not found", 404)
        return jsonify({"search_run": dict(row)})

    @blueprint.get("/admin/request-processes/<int:request_process_id>/attempts")
    @require_actor(admin=True)
    def attempts(request_process_id: int) -> Response | tuple[Response, int]:
        try:
            return jsonify(
                paged(
                    "SELECT * FROM request_attempts WHERE request_process_id = ? ORDER BY id DESC",
                    (request_process_id,),
                )
            )
        except ValueError as exc:
            return error("invalid_pagination", str(exc), 400)

    @blueprint.get("/admin/blocklist")
    @require_actor(admin=True)
    def blocklist() -> Response | tuple[Response, int]:
        try:
            return jsonify(
                paged("SELECT * FROM blocked_releases WHERE unblocked_at IS NULL ORDER BY id DESC")
            )
        except ValueError as exc:
            return error("invalid_pagination", str(exc), 400)

    @blueprint.post("/admin/candidates/<int:candidate_id>/select")
    @require_actor(admin=True)
    def select_candidate(candidate_id: int) -> tuple[Response, int] | Response:
        actor: V1Actor = g.v1_actor
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return error("invalid_selection", "body must be an object", 400)
        force = bool(payload.get("force", False))
        reason = str(payload.get("reason") or "").strip()
        with connect() as conn:
            candidate = conn.execute(
                "SELECT * FROM release_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate is None:
                return error("candidate_not_found", "Candidate not found", 404)
            if not bool(candidate["accepted"]) and not force:
                return error(
                    "candidate_not_eligible",
                    "Candidate is not eligible for automatic selection; use force with a reason",
                    409,
                )
            if force and not reason:
                return error(
                    "force_reason_required",
                    "A reason is required when forcing a rejected candidate",
                    400,
                )
            conn.execute(
                """
                UPDATE release_candidates
                SET selected_by = CASE WHEN id = ? THEN ? ELSE NULL END,
                    selected_at = CASE WHEN id = ? THEN CURRENT_TIMESTAMP ELSE NULL END,
                    selection_override_json = CASE WHEN id = ? THEN ? ELSE NULL END
                WHERE request_process_id = ?
                """,
                (
                    candidate_id,
                    actor.user_id,
                    candidate_id,
                    candidate_id,
                    json.dumps({"forced": force, "reason": reason or None}, sort_keys=True),
                    candidate["request_process_id"],
                ),
            )
            conn.execute(
                """INSERT INTO candidate_selection_audits(candidate_id, request_process_id, actor_user_id, forced, reason,
                   original_accepted, original_reject_reasons_json) VALUES(?,?,?,?,?,?,?)""",
                (
                    candidate_id,
                    candidate["request_process_id"],
                    actor.user_id,
                    int(force),
                    reason or None,
                    candidate["accepted"],
                    candidate["reject_reasons_json"],
                ),
            )
        job_service.enqueue(
            job_key=f"start-attempt:{candidate['request_process_id']}:candidate:{candidate_id}",
            job_type="start_attempt",
            payload={
                "request_process_id": candidate["request_process_id"],
                "candidate_id": candidate_id,
            },
        )
        return jsonify({"candidate_id": candidate_id, "selected": True, "forced": force}), 202

    @blueprint.post("/admin/candidates/<int:candidate_id>/block")
    @require_actor(admin=True)
    def block_candidate(candidate_id: int) -> tuple[Response, int] | Response:
        payload = request.get_json(silent=True) or {}
        with connect() as conn:
            candidate = conn.execute(
                "SELECT request_process_id, fingerprint FROM release_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
            if candidate is None:
                return error("candidate_not_found", "Candidate not found", 404)
            conn.execute(
                """
                INSERT INTO blocked_releases (fingerprint, scope, request_process_id, reason)
                VALUES (?, 'request_process', ?, ?)
                ON CONFLICT DO NOTHING
                """,
                (
                    candidate["fingerprint"],
                    candidate["request_process_id"],
                    str(payload.get("reason") or "admin_block")[:200],
                ),
            )
        return jsonify({"candidate_id": candidate_id, "blocked": True}), 202

    @blueprint.delete("/admin/blocklist/<int:block_id>")
    @require_actor(admin=True)
    def unblock_candidate(block_id: int) -> tuple[Response, int] | Response:
        with connect() as conn:
            updated = conn.execute(
                """
                UPDATE blocked_releases
                SET unblocked_at = CURRENT_TIMESTAMP
                WHERE id = ? AND unblocked_at IS NULL
                """,
                (block_id,),
            ).rowcount
        if updated != 1:
            return error("block_not_found", "Active block not found", 404)
        return jsonify({"block_id": block_id, "blocked": False}), 200

    @blueprint.post("/admin/request-processes/<int:request_process_id>/search-now")
    @require_actor(admin=True)
    def search_now(request_process_id: int) -> tuple[Response, int] | Response:
        with connect() as conn:
            updated = conn.execute(
                """UPDATE request_processes SET status = 'queued', next_search_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP WHERE id = ?
                   AND status NOT IN ('completed', 'cancelled', 'failed')""",
                (request_process_id,),
            ).rowcount
        if updated != 1:
            return error("request_process_conflict", "Request is not searchable", 409)
        job_service.enqueue(
            job_key=f"search-request:{request_process_id}:{uuid.uuid4().hex}",
            job_type="search_request",
            payload={"request_process_id": request_process_id},
        )
        return jsonify({"request_process_id": request_process_id, "scheduled": True}), 202

    @blueprint.post("/admin/request-processes/<int:request_process_id>/interactive-search")
    @require_actor(admin=True)
    def interactive_search(request_process_id: int) -> tuple[Response, int] | Response:
        """A fresh persisted run; results include rejected candidates for review."""
        try:
            run = search_service.create_queued_run(request_process_id)
        except SearchServiceError as exc:
            return error("request_process_conflict", str(exc), 409)
        job_service.enqueue(
            job_key=f"search-request:{request_process_id}:interactive:{uuid.uuid4().hex}",
            job_type="search_request",
            payload={
                "request_process_id": request_process_id,
                "search_run_id": run["id"],
                "interactive": True,
            },
        )
        return jsonify(
            {
                "request_process_id": request_process_id,
                "search_run": run,
                "scheduled": True,
                "interactive": True,
            }
        ), 202

    @blueprint.patch("/admin/request-processes/<int:request_process_id>/monitor")
    @require_actor(admin=True)
    def monitor(request_process_id: int) -> tuple[Response, int] | Response:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            return error("invalid_request", "enabled must be a boolean", 400)
        with connect() as conn:
            updated = conn.execute(
                "UPDATE request_processes SET monitoring_enabled = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(payload["enabled"]), request_process_id),
            ).rowcount
        if updated != 1:
            return error("request_process_not_found", "Request not found", 404)
        return jsonify(
            {"request_process_id": request_process_id, "monitoring_enabled": payload["enabled"]}
        ), 202

    @blueprint.post("/admin/request-processes/<int:request_process_id>/cancel")
    @require_actor(admin=True)
    def cancel_request_process(request_process_id: int) -> tuple[Response, int] | Response:
        cancelled_requests: list[dict[str, Any]] = []
        with connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM requests
                WHERE request_process_id = ? AND status IN ('pending_approval', 'approved')
                """,
                (request_process_id,),
            ).fetchall()
            updated = conn.execute(
                """
                UPDATE request_processes
                SET status = 'cancelled', monitoring_enabled = 0,
                    active_request_attempt_id = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status NOT IN ('completed', 'cancelled', 'failed')
                """,
                (request_process_id,),
            ).rowcount
            if updated == 1:
                conn.execute(
                    """
                    UPDATE requests
                    SET status = 'cancelled', updated_at = CURRENT_TIMESTAMP
                    WHERE request_process_id = ? AND status IN ('pending_approval', 'approved')
                    """,
                    (request_process_id,),
                )
                cancelled_requests = [{**dict(row), "status": "cancelled"} for row in rows]
        if updated != 1:
            return error("request_process_conflict", "Request cannot be cancelled", 409)
        for cancelled_request in cancelled_requests:
            enqueue_request_status(cancelled_request)
        return jsonify(
            {
                "request_process_id": request_process_id,
                "status": "cancelled",
                "request_ids": [int(row["id"]) for row in cancelled_requests],
            }
        ), 202

    @blueprint.get("/admin/maintenance")
    @require_actor(admin=True)
    def maintenance() -> tuple[Response, int] | Response:
        with connect() as conn:
            integrity = str(conn.execute("PRAGMA quick_check").fetchone()[0])
            migrations = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT version, name, status, started_at, completed_at, error_message
                    FROM schema_migrations ORDER BY version
                    """
                ).fetchall()
            ]
            pending_jobs = int(
                conn.execute(
                    "SELECT COUNT(*) FROM background_jobs WHERE status IN ('queued', 'running')"
                ).fetchone()[0]
            )
        status = (
            200
            if integrity == "ok" and all(row["status"] == "completed" for row in migrations)
            else 503
        )
        return (
            jsonify(
                {
                    "database_integrity": integrity,
                    "migrations": migrations,
                    "pending_jobs": pending_jobs,
                }
            ),
            status,
        )

    app.register_blueprint(blueprint)
