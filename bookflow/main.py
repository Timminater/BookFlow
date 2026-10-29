"""Flask app - routes, WebSocket handlers, and middleware."""

import binascii
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import wraps
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, NoReturn, cast

from flask import Flask, abort, jsonify, request, send_file, send_from_directory, session
from flask_cors import CORS
from flask_socketio import SocketIO, emit
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.wrappers import Response

from bookflow.api.websocket import ws_manager
from bookflow.config.env import (
    BUILD_VERSION,
    CONFIG_DIR,
    CWA_DB_PATH,
    DISABLE_LOCAL_AUTH,
    FLASK_HOST,
    FLASK_PORT,
    HIDE_LOCAL_AUTH,
    OIDC_AUTO_REDIRECT,
    RELEASE_VERSION,
    SESSION_COOKIE_NAME,
    SESSION_COOKIE_SECURE_ENV,
    TMP_DIR,
    _is_config_dir_writable,
    string_to_bool,
)
from bookflow.config.security import _migrate_security_settings
from bookflow.config.settings import _SUPPORTED_BOOK_LANGUAGE
from bookflow.core.activity_view_state_service import ActivityViewStateService
from bookflow.core.audiobook_retirement import (
    discard_late_audiobook_artifacts,
    retire_audiobooks,
)
from bookflow.core.auth_modes import (
    AUTH_MODE_UNAVAILABLE,
    get_auth_check_admin_status,
    is_settings_or_onboarding_path,
    load_active_auth_mode,
    requires_admin_for_settings_access,
)
from bookflow.core.backup_routes import register_backup_routes
from bookflow.core.backup_service import BackupService, apply_staged_restore
from bookflow.core.bestseller_service import BestsellerSuggestionService
from bookflow.core.catalog_service import CatalogService
from bookflow.core.config import config as app_config
from bookflow.core.cwa_user_sync import upsert_cwa_user
from bookflow.core.download_history_service import DownloadHistoryService
from bookflow.core.durable_jobs import DurableJobService
from bookflow.core.external_user_linking import upsert_external_user
from bookflow.core.file_store import FileStore
from bookflow.core.github_issue_routes import register_github_issue_routes
from bookflow.core.github_issue_service import GitHubIssueService, valid_repository
from bookflow.core.http_security import (
    TrustedProxyMiddleware,
    mutation_request_is_same_origin,
    parse_trusted_proxy_cidrs,
)
from bookflow.core.image_cache import get_image_cache
from bookflow.core.kepub_converter import KepubCatalogService, KepubConverter, KepubifyBinary
from bookflow.core.kobo_pairing import KoboPairingService
from bookflow.core.kobo_pairing_routes import register_kobo_pairing_routes
from bookflow.core.kobo_routes import register_kobo_routes
from bookflow.core.kobo_services import KoboSyncService, TokenService
from bookflow.core.kobo_settings_routes import register_kobo_settings_routes
from bookflow.core.legacy_file_reconciliation import reconcile_legacy_download_files
from bookflow.core.library_routes import register_library_routes
from bookflow.core.library_service import LibraryService
from bookflow.core.logger import setup_logger
from bookflow.core.metadata_governance import MetadataGovernanceService
from bookflow.core.metadata_routes import register_metadata_routes
from bookflow.core.metrics import RequestMetrics
from bookflow.core.models import TERMINAL_QUEUE_STATUSES, QueueStatus
from bookflow.core.monitor_runtime import MONITOR_SCAN_JOB, MonitorRuntime
from bookflow.core.notifications import (
    NotificationContext,
    NotificationEvent,
    notify_admin,
    notify_user,
)
from bookflow.core.path_security import resolve_library_file
from bookflow.core.prefix_middleware import PrefixMiddleware
from bookflow.core.request_helpers import (
    emit_ws_event,
    get_session_db_user_id,
    normalize_optional_text,
    normalize_positive_int,
)
from bookflow.core.request_runtime import RequestRuntime
from bookflow.core.requests_service import sync_delivery_states_from_queue_status
from bookflow.core.user_db import UserDB
from bookflow.core.utils import normalize_base_path
from bookflow.core.v1_api import V1Actor, register_v1_api
from bookflow.core.web_push import VapidWebPushTransport, WebPushService
from bookflow.core.web_push_events import enqueue_admin_web_push
from bookflow.core.web_push_routes import register_web_push_routes
from bookflow.download import orchestrator as backend
from bookflow.release_sources import Release, SourceUnavailableError, get_source_display_name

if TYPE_CHECKING:
    from bookflow.metadata_providers import BookMetadata, MetadataProvider

logger = setup_logger(__name__)
FLASK_SECRET_KEY_MIN_BYTES = 32
_OPERATIONAL_ERRORS = (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error)
_IMPORT_OPERATIONAL_ERRORS = (ImportError, *_OPERATIONAL_ERRORS)


def _is_debug_enabled() -> bool:
    debug_value = app_config.get("DEBUG", False)
    if isinstance(debug_value, str):
        return string_to_bool(debug_value)
    return bool(debug_value)


def _raise_runtime_error(message: str) -> NoReturn:
    raise RuntimeError(message)


def _resolve_library_file_for_serving(raw_path: str) -> Path:
    """Resolve one persisted file below the configured library storage roots."""
    book_root = normalize_optional_text(app_config.get("DESTINATION", "/books")) or "/books"
    roots = (Path(book_root),)
    return resolve_library_file(raw_path, allowed_roots=roots)


# Project root is the repository root above the package directory.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_DIST = PROJECT_ROOT / "frontend-dist"

BASE_PATH = normalize_base_path(normalize_optional_text(app_config.get("URL_BASE", "")))

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0  # Disable caching
app.config["APPLICATION_ROOT"] = BASE_PATH or "/"

try:
    trusted_proxy_networks = parse_trusted_proxy_cidrs(os.environ.get("TRUSTED_PROXY_CIDRS", ""))
    trusted_proxy_hops = int(os.environ.get("TRUSTED_PROXY_HOPS", "1"))
except ValueError as exc:
    raise RuntimeError(f"Invalid trusted proxy configuration: {exc}") from exc
if not 1 <= trusted_proxy_hops <= 5:
    raise RuntimeError("TRUSTED_PROXY_HOPS must be between 1 and 5")

wsgi_app = cast(Any, app.wsgi_app)
_PUBLIC_OBSERVABILITY_PATHS = {
    "/api/health",
    "/api/health/live",
    "/api/health/ready",
    "/api/metrics",
}
if BASE_PATH:
    wsgi_app = cast(
        Any,
        PrefixMiddleware(wsgi_app, BASE_PATH, bypass_paths=_PUBLIC_OBSERVABILITY_PATHS),
    )
if trusted_proxy_networks:
    wsgi_app = cast(
        Any,
        ProxyFix(
            wsgi_app,
            x_for=trusted_proxy_hops,
            x_proto=trusted_proxy_hops,
            x_host=trusted_proxy_hops,
            x_port=trusted_proxy_hops,
            x_prefix=trusted_proxy_hops,
        ),
    )
wsgi_app = cast(Any, TrustedProxyMiddleware(wsgi_app, trusted_proxy_networks))
app.wsgi_app = wsgi_app

# Socket.IO async mode.
# We run this app under Gunicorn with a gevent websocket worker (even when DEBUG=true),
# so Socket.IO should always use gevent here.
async_mode = "gevent"
configured_socketio_origins = [
    origin.strip()
    for origin in os.environ.get("SOCKETIO_CORS_ALLOWED_ORIGINS", "").split(",")
    if origin.strip()
]
socketio_cors_allowed_origins = configured_socketio_origins or (
    ["http://localhost:5173", "http://127.0.0.1:5173"] if _is_debug_enabled() else None
)

# Initialize Flask-SocketIO with reverse proxy support
socketio_path = f"{BASE_PATH}/socket.io" if BASE_PATH else "/socket.io"
socketio_init_kwargs: dict[str, Any] = {
    "cors_allowed_origins": socketio_cors_allowed_origins,
    "async_mode": async_mode,
    "logger": False,
    "engineio_logger": False,
    # Reverse proxy / Traefik compatibility settings
    "path": socketio_path,
    "ping_timeout": 60,
    "ping_interval": 25,
    # Allow both websocket and polling for better compatibility
    "transports": ["websocket", "polling"],
    # Enable CORS for all origins (you can restrict this in production)
    "allow_upgrades": True,
    # Important for proxies that buffer
    "http_compression": True,
}
socketio = SocketIO(app, **socketio_init_kwargs)
request_metrics = RequestMetrics()

# Initialize WebSocket manager
ws_manager.init_app(app, socketio)
ws_manager.set_queue_status_fn(backend.queue_status)
logger.info("Flask-SocketIO initialized with async_mode='%s'", async_mode)
logger.info("Socket.IO CORS allowed origins: %s", socketio_cors_allowed_origins)

# Ensure all plugins are loaded before starting the download coordinator.
# This prevents a race condition where the download loop could try to process
# a queued task before its handler (e.g., prowlarr) is registered.
try:
    import_module("bookflow.metadata_providers")
    import_module("bookflow.release_sources")
    logger.debug("Plugin modules loaded successfully")
except ImportError as e:
    logger.warning("Failed to import plugin modules: %s", e)

# Migrate legacy security settings if needed
_migrate_security_settings()

# Initialize user database and register multi-user routes
# If CONFIG_DIR doesn't exist or is read-only, multi-user features will be disabled
try:
    if apply_staged_restore(Path(os.environ.get("CONFIG_DIR", "/config"))):
        logger.warning("Applied a validated staged BookFlow restore before database startup")
except OSError, RuntimeError, ValueError, json.JSONDecodeError:
    logger.exception("A staged BookFlow restore could not be applied; existing data was preserved")
_user_db_path = str(Path(os.environ.get("CONFIG_DIR", "/config")) / "users.db")
user_db: UserDB | None = None
download_history_service: DownloadHistoryService | None = None
activity_view_state_service: ActivityViewStateService | None = None
library_service: LibraryService | None = None
request_runtime: RequestRuntime | None = None
github_issue_service: GitHubIssueService | None = None
manual_kobo_fulfilment: Callable[[int, int, int], None] | None = None


def _apply_manual_kobo_fulfilment(user_id: int, book_id: int, file_id: int) -> None:
    callback = manual_kobo_fulfilment
    if callback is not None:
        callback(user_id, book_id, file_id)


def resolve_v1_actor() -> V1Actor | None:
    """Map the current authenticated session to the versioned API principal."""
    actor_user_id = get_session_db_user_id(session)
    if actor_user_id is None:
        return None
    return V1Actor(
        user_id=actor_user_id,
        is_admin=bool(session.get("is_admin", False)),
    )


try:
    user_db = UserDB(_user_db_path)
    user_db.initialize()
    try:
        retire_audiobooks(
            database_path=Path(_user_db_path),
            config_dir=Path(os.environ.get("CONFIG_DIR", "/config")),
            staging_root=TMP_DIR,
            library_root=Path(
                normalize_optional_text(app_config.get("DESTINATION", "/books")) or "/books"
            ),
            cancel_download=backend.cancel_download,
        )
        app_config.refresh()
    except OSError, RuntimeError, TypeError, ValueError, sqlite3.Error:
        # The migration's write guards still prevent new audiobook rows. Keep
        # the service available while surfacing the failed audit for repair.
        logger.exception("Audiobook retirement did not complete; no unsafe path was deleted")
    try:
        legacy_file_summary = reconcile_legacy_download_files(
            database_path=Path(_user_db_path),
            library_roots=[
                Path(normalize_optional_text(app_config.get("DESTINATION", "/books")) or "/books")
            ],
            supported_formats=app_config.get("SUPPORTED_FORMATS", ()),
        )
        logger.info("Legacy file reconciliation completed: %s", legacy_file_summary)
    except OSError, RuntimeError, TypeError, ValueError, sqlite3.Error:
        logger.exception("Legacy file reconciliation failed; quarantined rows were preserved")
    download_history_service = DownloadHistoryService(_user_db_path)
    activity_view_state_service = ActivityViewStateService(_user_db_path)
    library_service = LibraryService(_user_db_path)
    import_module("bookflow.config.users_settings")
    from bookflow.core.admin_routes import register_admin_routes
    from bookflow.core.oidc_routes import register_oidc_routes
    from bookflow.core.self_user_routes import register_self_user_routes

    register_oidc_routes(app, user_db)
    register_admin_routes(app, user_db)
    register_self_user_routes(app, user_db)

    def _retry_selected_candidate(candidate_id: int) -> dict[str, int]:
        if request_runtime is None:
            raise RuntimeError("Download retry service is unavailable")
        return request_runtime.retry_selected_candidate(candidate_id)

    register_v1_api(
        app,
        database_path=_user_db_path,
        resolve_actor=resolve_v1_actor,
        resolve_file_path=_resolve_library_file_for_serving,
        library_root=Path(
            normalize_optional_text(app_config.get("DESTINATION", "/books")) or "/books"
        ),
        kobo_fulfilment=_apply_manual_kobo_fulfilment,
        retry_candidate=_retry_selected_candidate,
    )
    # Catalog bytes are intentionally removed only after the database commit.
    # Resume any durable cleanup outbox entries at startup; individual failures
    # stay pending and are exposed through the admin status endpoint.
    try:
        from bookflow.core.catalog_deletion_service import CatalogDeletionService

        CatalogDeletionService(_user_db_path, _resolve_library_file_for_serving).recover_pending()
    except OSError, RuntimeError, ValueError, sqlite3.Error:
        logger.exception("Catalog deletion cleanup recovery failed; pending entries were retained")
    github_issue_token = os.environ.get("GITHUB_ISSUE_TOKEN", "").strip()
    github_issue_repository = os.environ.get(
        "GITHUB_ISSUE_REPOSITORY", "Timminater/BookFlow"
    ).strip()
    if github_issue_token and valid_repository(github_issue_repository):
        github_issue_service = GitHubIssueService(
            token=github_issue_token,
            repository=github_issue_repository,
        )
        register_github_issue_routes(
            app,
            database_path=_user_db_path,
            resolve_actor=resolve_v1_actor,
            service=github_issue_service,
            build_version=BUILD_VERSION,
            release_version=RELEASE_VERSION,
        )
    elif github_issue_token:
        logger.warning(
            "GitHub issue reporting disabled: GITHUB_ISSUE_REPOSITORY must be OWNER/REPO"
        )
    metadata_governance_service = MetadataGovernanceService(_user_db_path)
    register_metadata_routes(
        app,
        resolve_actor=resolve_v1_actor,
        service=metadata_governance_service,
    )
    vapid_private_key = normalize_optional_text(os.environ.get("VAPID_PRIVATE_KEY", ""))
    vapid_subject = normalize_optional_text(os.environ.get("VAPID_SUBJECT", ""))
    web_push_transport = (
        VapidWebPushTransport(private_key=vapid_private_key, subject=vapid_subject)
        if vapid_private_key is not None and vapid_subject is not None
        else None
    )
    web_push_service = WebPushService(_user_db_path, transport=web_push_transport)
    register_web_push_routes(
        app,
        resolve_actor=resolve_v1_actor,
        service=web_push_service,
        vapid_public_key=normalize_optional_text(os.environ.get("VAPID_PUBLIC_KEY", "")),
    )
    backup_service = BackupService(
        database_path=Path(_user_db_path),
        config_dir=Path(os.environ.get("CONFIG_DIR", "/config")),
        backup_dir=Path(
            os.environ.get(
                "BACKUP_DIR",
                str(Path(os.environ.get("CONFIG_DIR", "/config")) / "backups"),
            )
        ),
        encryption_key=normalize_optional_text(os.environ.get("BACKUP_ENCRYPTION_KEY", "")),
    )
    register_backup_routes(
        app,
        resolve_actor=resolve_v1_actor,
        service=backup_service,
    )
    try:
        request_runtime = RequestRuntime(
            _user_db_path,
            queue_release=backend.queue_release,
            retry_download=backend.retry_download,
            library_root=Path(
                normalize_optional_text(app_config.get("DESTINATION", "/books")) or "/books"
            ),
        )
        platform_jobs = DurableJobService(_user_db_path)

        def _bestseller_check_interval() -> timedelta:
            """Read the live administrator interval without risking runtime startup."""
            try:
                hours = int(str(app_config.get("BESTSELLER_CHECK_INTERVAL_HOURS", 24)))
            except TypeError, ValueError:
                hours = 24
            return timedelta(hours=max(1, min(168, hours)))

        def _schedule_monitor_push(user_id: int, payload: dict[str, Any]) -> None:
            platform_jobs.enqueue(
                job_key=f"web-push:monitor:{payload['monitor_id']}:{payload['book_id']}",
                job_type="send_web_push",
                payload={
                    "user_id": user_id,
                    "event_type": "monitor.discovery",
                    "payload": payload,
                },
            )

        def _deliver_web_push(payload: dict[str, Any]) -> None:
            user_id = payload.get("user_id")
            event_type = payload.get("event_type")
            event_payload = payload.get("payload")
            if (
                not isinstance(user_id, int)
                or not isinstance(event_type, str)
                or not isinstance(event_payload, dict)
            ):
                _raise_runtime_error("Invalid web-push job")
            web_push_service.deliver(
                user_id=user_id,
                event_type=event_type,
                payload=event_payload,
            )

        monitor_runtime = MonitorRuntime(
            _user_db_path,
            notify=_schedule_monitor_push,
        )
        from bookflow.metadata_providers import (
            MetadataProvider,
            get_configured_provider,
            get_enabled_providers,
            get_provider,
            get_provider_kwargs,
        )

        configured_provider = get_configured_provider()
        bestseller_providers: list[MetadataProvider] = []
        configured_name = (
            str(configured_provider.name).casefold() if configured_provider is not None else None
        )
        if configured_provider is not None:
            bestseller_providers.append(configured_provider)
        enabled_provider_names = set(get_enabled_providers())
        for provider_name in ("googlebooks", "openlibrary", "hardcover"):
            if provider_name == configured_name or provider_name not in enabled_provider_names:
                continue
            bestseller_providers.append(
                get_provider(provider_name, **get_provider_kwargs(provider_name))
            )

        bestseller_service = BestsellerSuggestionService(
            _user_db_path,
            providers=bestseller_providers,
            cover_fetcher=get_image_cache().fetch_and_cache,
            check_interval=_bestseller_check_interval(),
            defer_enrichment=True,
            source_metadata_fallback=True,
        )

        def _sync_bestseller60(payload: dict[str, Any]) -> None:
            bestseller_service.set_check_interval(_bestseller_check_interval())
            bestseller_service.sync_if_due(
                worker_id="durable-bestseller60",
                force=True,
            )
            if payload.get("trigger") == "admin":
                bestseller_service.retry_incomplete_enrichment()

        def _enrich_bestseller_metadata(payload: dict[str, Any]) -> None:
            isbns = payload.get("isbns")
            if not isinstance(isbns, list) or not all(isinstance(value, str) for value in isbns):
                _raise_runtime_error("Invalid Bestseller metadata enrichment job")
            bestseller_service.enrich_isbns(isbns)

        def _create_daily_backup(_payload: dict[str, Any]) -> None:
            try:
                backup_service.create()
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                enqueue_admin_web_push(
                    _user_db_path,
                    event_type="backup.error",
                    payload={
                        "title": "Scheduled backup failed",
                        "body": "BookFlow could not create its daily backup.",
                        "url": "/admin/operations",
                    },
                    event_key=f"scheduled-backup:{datetime.now(UTC):%Y%m%d}:{type(exc).__name__}",
                )
                raise

        def _schedule_external_jobs(now: datetime) -> int:
            scheduled = monitor_runtime.schedule_due(now)
            bestseller_service.set_check_interval(_bestseller_check_interval())
            if bool(app_config.get("BESTSELLER_ENABLED", True)) and bestseller_service.is_check_due(
                now
            ):
                platform_jobs.enqueue(
                    job_key=f"bestseller60-check:{now.strftime('%Y%m%d')}",
                    job_type="sync_bestseller60",
                    payload={},
                    run_at=now,
                    max_attempts=3,
                )
                scheduled += 1
            if backup_service.daily_backup_due(now):
                platform_jobs.enqueue(
                    job_key=f"daily-backup:{now.strftime('%Y%m%d')}",
                    job_type="create_daily_backup",
                    payload={},
                    run_at=now,
                    max_attempts=3,
                )
                scheduled += 1
            for isbns, due_key in bestseller_service.due_enrichment_batches(now=now):
                digest = hashlib.sha256(f"{due_key}|{'|'.join(isbns)}".encode()).hexdigest()[:20]
                platform_jobs.enqueue(
                    job_key=f"bestseller-metadata:{digest}",
                    job_type="enrich_bestseller_metadata",
                    payload={"isbns": isbns},
                    run_at=now,
                    max_attempts=3,
                )
                scheduled += 1
            return scheduled

        request_runtime.set_durable_job_handler(MONITOR_SCAN_JOB, monitor_runtime.process)
        request_runtime.set_durable_job_handler("send_web_push", _deliver_web_push)
        request_runtime.set_durable_job_handler("sync_bestseller60", _sync_bestseller60)
        request_runtime.set_durable_job_handler(
            "enrich_bestseller_metadata", _enrich_bestseller_metadata
        )
        request_runtime.set_durable_job_handler("create_daily_backup", _create_daily_backup)
        request_runtime.set_external_job_scheduler(_schedule_external_jobs)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.warning("RequestProcess runtime unavailable: %s", exc)
except (sqlite3.OperationalError, OSError) as e:
    logger.warning(
        "User database initialization failed: %s. Multi-user authentication features will be disabled. Ensure CONFIG_DIR (%s) exists and is writable.",
        e,
        os.environ.get("CONFIG_DIR", "/config"),
    )
    user_db = None
    download_history_service = None
    activity_view_state_service = None
    library_service = None
    request_runtime = None

# Start download coordinator
backend.start()

# Rate limiting for login attempts
# Map usernames to their failed-attempt counters and lockout timestamps.
failed_login_attempts: dict[str, dict[str, Any]] = {}
MAX_LOGIN_ATTEMPTS = 10
LOCKOUT_DURATION_MINUTES = 30
LOGIN_ATTEMPT_WARNING_THRESHOLD = 5


def cleanup_old_lockouts() -> None:
    """Remove expired lockout entries from persistent and fallback state."""
    current_time = datetime.now(UTC)
    if user_db is not None:
        user_db.clear_expired_login_rate_limits(now=current_time)

    expired_users = []
    for username in list(failed_login_attempts):
        lockout_until = _get_lockout_until(username, repair_if_locked=True)
        if lockout_until is not None and lockout_until < current_time:
            expired_users.append(username)
    for username in expired_users:
        logger.info("Lockout expired for user: %s", username)
        del failed_login_attempts[username]


def _get_lockout_until(
    username: str,
    source_ip: str = "unknown",
    *,
    repair_if_locked: bool = False,
) -> datetime | None:
    """Return a valid lockout timestamp for the user when one exists.

    When a user has already crossed the lockout threshold but the timestamp is
    missing or malformed, optionally repair the state to keep the lockout in
    force rather than silently letting the user through.
    """
    lockout_state = None
    if user_db is not None:
        persisted_state = user_db.get_login_rate_limit(username=username, source_ip=source_ip)
        if persisted_state is not None:
            persisted_until = persisted_state.get("locked_until")
            lockout_state = {
                "count": persisted_state["failure_count"],
                "lockout_until": (
                    datetime.fromisoformat(persisted_until) if persisted_until else None
                ),
            }
            failed_login_attempts[username] = lockout_state

    if lockout_state is None:
        lockout_state = failed_login_attempts.get(username)
    if lockout_state is None:
        return None

    lockout_until = lockout_state.get("lockout_until")
    if isinstance(lockout_until, datetime):
        return lockout_until

    attempt_count = lockout_state.get("count")
    if repair_if_locked and isinstance(attempt_count, int) and attempt_count >= MAX_LOGIN_ATTEMPTS:
        repaired_lockout_until = datetime.now(UTC) + timedelta(minutes=LOCKOUT_DURATION_MINUTES)
        lockout_state["lockout_until"] = repaired_lockout_until
        logger.warning("Repaired missing lockout timestamp for locked account '%s'", username)
        return repaired_lockout_until

    if lockout_until is not None:
        logger.warning("Ignoring invalid lockout timestamp for user '%s'", username)

    return None


def is_account_locked(username: str, source_ip: str = "unknown") -> bool:
    """Check if an account is currently locked due to failed login attempts."""
    cleanup_old_lockouts()

    lockout_until = _get_lockout_until(
        username,
        source_ip,
        repair_if_locked=True,
    )
    return lockout_until is not None and datetime.now(UTC) < lockout_until


def record_failed_login(username: str, ip_address: str) -> bool:
    """Record a failed login attempt and lock account if threshold is reached.

    Returns True if account is now locked, False otherwise.
    """
    if user_db is not None:
        state = user_db.record_login_failure(
            username=username,
            source_ip=ip_address,
            max_failures=MAX_LOGIN_ATTEMPTS,
            lockout_duration=timedelta(minutes=LOCKOUT_DURATION_MINUTES),
        )
        count = int(state["failure_count"])
        lockout_until_raw = state.get("locked_until")
        lockout_until = datetime.fromisoformat(lockout_until_raw) if lockout_until_raw else None
        failed_login_attempts[username] = {
            "count": count,
            "lockout_until": lockout_until,
        }
    else:
        if username not in failed_login_attempts:
            failed_login_attempts[username] = {"count": 0}
        failed_login_attempts[username]["count"] += 1
        count = failed_login_attempts[username]["count"]
        lockout_until = None

    logger.warning(
        "Failed login attempt %s/%s for user '%s' from IP %s",
        count,
        MAX_LOGIN_ATTEMPTS,
        username,
        ip_address,
    )

    if count >= MAX_LOGIN_ATTEMPTS:
        if lockout_until is None:
            lockout_until = datetime.now(UTC) + timedelta(minutes=LOCKOUT_DURATION_MINUTES)
            failed_login_attempts[username]["lockout_until"] = lockout_until
        logger.warning(
            "Account locked for user '%s' until %s due to %s failed login attempts",
            username,
            lockout_until.strftime("%Y-%m-%d %H:%M:%S"),
            count,
        )
        return True

    return False


def clear_failed_logins(username: str, source_ip: str = "unknown") -> None:
    """Clear failed login attempts for a user after successful login."""
    if user_db is not None:
        user_db.clear_login_rate_limit(username=username, source_ip=source_ip)
    if username in failed_login_attempts:
        del failed_login_attempts[username]
        logger.debug("Cleared failed login attempts for user: %s", username)


def get_client_ip() -> str:
    """Extract client IP address from request, handling reverse proxy forwarding."""
    ip_address = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
    # X-Forwarded-For can contain multiple IPs, take the first one
    if "," in ip_address:
        ip_address = ip_address.split(",")[0].strip()
    return ip_address


def get_auth_mode() -> str:
    """Determine which authentication mode is active.

    Uses configured AUTH_METHOD plus runtime prerequisites.
    Returns "none" when config is invalid or unavailable.
    """
    return load_active_auth_mode(CWA_DB_PATH, user_db=user_db)


_AUDIOBOOK_CATEGORY_RANGE = (3030, 3049)
_AUDIOBOOK_FORMAT_HINTS = frozenset(
    {
        "m4b",
        "mp3",
        "m4a",
        "flac",
        "ogg",
        "wma",
        "aac",
        "wav",
        "opus",
    }
)


def _contains_audiobook_format_hint(value: Any) -> bool:
    if not isinstance(value, str):
        return False

    normalized = value.strip().lower()
    if not normalized:
        return False

    tokens = [token for token in re.split(r"[^a-z0-9]+", normalized) if token]
    return any(token in _AUDIOBOOK_FORMAT_HINTS for token in tokens)


def normalize_content_type(value: Any) -> str:
    """Normalize queue metadata without request-policy semantics."""
    return "audiobook" if str(value or "").strip().lower() == "audiobook" else "ebook"


def normalize_source(value: Any) -> str:
    """Normalize a release-source identifier."""
    return str(value or "").strip().lower()


def _resolve_release_content_type(data: dict[str, Any], source: Any) -> tuple[str, bool]:
    """Resolve release content type for policy checks and queue payload normalization."""
    extra = data.get("extra")
    if not isinstance(extra, dict):
        extra = {}

    explicit_content_type = data.get("content_type")
    if explicit_content_type is None:
        explicit_content_type = extra.get("content_type")
    if explicit_content_type is not None:
        return normalize_content_type(explicit_content_type), False

    categories = extra.get("categories")
    if isinstance(categories, list):
        min_cat, max_cat = _AUDIOBOOK_CATEGORY_RANGE
        for raw_category in categories:
            try:
                category_id = int(raw_category)
            except TypeError, ValueError:
                continue
            if min_cat <= category_id <= max_cat:
                return "audiobook", True

    candidates: list[Any] = [
        data.get("format"),
        extra.get("format"),
        extra.get("formats_display"),
        data.get("title"),
    ]
    formats = extra.get("formats")
    if isinstance(formats, list):
        candidates.extend(formats)
    else:
        candidates.append(formats)

    if any(_contains_audiobook_format_hint(candidate) for candidate in candidates):
        return "audiobook", True

    return "ebook", False


def _require_download_capability() -> tuple[Response, int] | None:
    """Require Library Capability for non-admin release queueing."""
    if get_auth_mode() == "none" or session.get("is_admin", False):
        return None
    if user_db is None:
        return jsonify({"error": "User database unavailable"}), 503
    db_user_id = normalize_positive_int(session.get("db_user_id"))
    if db_user_id is None:
        return jsonify({"error": "User identity is unavailable"}), 403
    user = user_db.get_user(user_id=db_user_id)
    if user is None:
        return jsonify({"error": "User identity is unavailable"}), 403
    if user.get("library_capability") != "download-capable":
        return jsonify({"error": "Download capability required"}), 403
    return None


def _resolve_download_user_context(
    db_user_id: Any,
    username: Any,
    on_behalf_of_user_id: Any,
) -> tuple[Any, Any, tuple[Response, int] | None]:
    """Resolve download queue user context, including optional admin on-behalf overrides."""
    if on_behalf_of_user_id in (None, ""):
        return db_user_id, username, None

    if not session.get("is_admin", False):
        return db_user_id, username, (jsonify({"error": "Admin required"}), 403)

    if user_db is None:
        return db_user_id, username, (jsonify({"error": "User database unavailable"}), 503)

    try:
        target_user_id = int(on_behalf_of_user_id)
    except TypeError, ValueError:
        return db_user_id, username, (jsonify({"error": "Invalid on_behalf_of_user_id"}), 400)

    if target_user_id <= 0:
        return db_user_id, username, (jsonify({"error": "Invalid on_behalf_of_user_id"}), 400)

    target_user = user_db.get_user(user_id=target_user_id)
    if not target_user:
        return db_user_id, username, (jsonify({"error": "User not found"}), 404)

    return target_user["id"], target_user["username"], None


def _emit_request_updates(rows: list[dict[str, Any]]) -> None:
    """Defer request update emission until the runtime hook is available."""
    _emit_request_update_events(rows)


def _resolve_auth_mode_for_routes() -> str:
    """Resolve auth mode lazily so tests and runtime patches still take effect."""
    return get_auth_mode()


def _queue_release_for_routes(*args: Any, **kwargs: Any) -> Any:
    """Queue a release via the current backend instance."""
    return backend.queue_release(*args, **kwargs)


def _queue_status_for_routes(user_id: int | None = None) -> dict[str, dict[str, Any]]:
    """Read queue status via the current backend instance."""
    return backend.queue_status(user_id=user_id)


def _resolve_metadata_book_for_library(
    provider_name: str, provider_book_id: str
) -> dict[str, Any] | None:
    """Fetch book metadata from a provider for Add-to-Library (#04 sub-decision 13).

    Returns the BookMetadata as a dict, with the cover URL transformed to the
    local proxy form when caching is enabled — mirrors /api/metadata/book.
    Returns None when the provider is unavailable or the book isn't found,
    which the library route translates into a 503. Lookup is synchronous so
    the ``books`` row is fully populated before the user's library link is
    inserted (ADR 0001: snapshot-at-add is final).
    """
    try:
        from dataclasses import asdict

        prov = _resolve_metadata_provider(provider_name)
        book = prov.get_book(provider_book_id)
        if not book:
            return None
        book_dict = asdict(book)
        from bookflow.core.utils import transform_cover_url

        if book_dict.get("cover_url"):
            cache_id = f"{provider_name}_{provider_book_id}"
            book_dict["cover_url"] = transform_cover_url(book_dict["cover_url"], cache_id)

        authors = book_dict.get("authors") or []
        primary_author = authors[0] if isinstance(authors, list) and authors else None
        book_dict["author"] = primary_author
        raw_payload = dict(book_dict)
        raw_payload.pop("author", None)
        book_dict["metadata_json"] = raw_payload
    except (OSError, RuntimeError, TypeError, ValueError, sqlite3.Error) as exc:
        logger.warning(
            "Library metadata fetch for %s/%s failed: %s", provider_name, provider_book_id, exc
        )
        return None
    else:
        return book_dict


if user_db is not None:
    try:
        from bookflow.core.activity_routes import register_activity_routes
        from bookflow.core.request_routes import register_request_routes

        register_request_routes(
            app,
            user_db,
            resolve_auth_mode=_resolve_auth_mode_for_routes,
            queue_release=_queue_release_for_routes,
            ws_manager=ws_manager,
        )
        if download_history_service is not None and activity_view_state_service is not None:
            register_activity_routes(
                app,
                user_db,
                activity_view_state_service=activity_view_state_service,
                download_history_service=download_history_service,
                resolve_auth_mode=_resolve_auth_mode_for_routes,
                queue_status=_queue_status_for_routes,
                sync_request_delivery_states=sync_delivery_states_from_queue_status,
                emit_request_updates=_emit_request_updates,
                ws_manager=ws_manager,
            )
        if library_service is not None and download_history_service is not None:
            register_library_routes(
                app,
                user_db,
                library_service=library_service,
                download_history_service=download_history_service,
                resolve_auth_mode=_resolve_auth_mode_for_routes,
                resolve_metadata_book=_resolve_metadata_book_for_library,
                resolve_file_path=_resolve_library_file_for_serving,
                resolve_client_ip=get_client_ip,
            )
    except _IMPORT_OPERATIONAL_ERRORS as e:
        logger.warning("Failed to register request routes: %s", e)


# Enable CORS in development mode for local frontend development
if _is_debug_enabled():
    CORS(
        app,
        resources={
            r"/*": {
                "origins": ["http://localhost:5173", "http://127.0.0.1:5173"],
                "supports_credentials": True,
                "allow_headers": ["Content-Type", "Authorization", "X-BookFlow-CSRF"],
                "methods": ["GET", "POST", "PUT", "DELETE", "OPTIONS"],
            }
        },
    )


# Custom log filter to exclude routine status endpoint polling and WebSocket noise
class LogNoiseFilter(logging.Filter):
    """Filter out routine status endpoint requests and WebSocket upgrade errors to reduce log noise.

    WebSocket upgrade errors are benign - Flask-SocketIO automatically falls back to polling transport.
    The error occurs because Werkzeug's built-in server doesn't fully support WebSocket upgrades.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Return whether a log record should be emitted."""
        message = record.getMessage() if hasattr(record, "getMessage") else str(record.msg)

        # Exclude GET /api/status requests (polling noise)
        if "GET /api/status" in message:
            return False

        # Exclude WebSocket upgrade errors (benign - falls back to polling)
        if "write() before start_response" in message:
            return False

        # Exclude the Error on request line that precedes WebSocket errors
        if record.levelno == logging.ERROR:
            if "Error on request:" in message:
                return False
            # Filter WebSocket-related AssertionError tracebacks
            if hasattr(record, "exc_info") and record.exc_info:
                exc_type, exc_value = record.exc_info[0], record.exc_info[1]
                if (
                    exc_type
                    and exc_type.__name__ == "AssertionError"
                    and exc_value
                    and "write() before start_response" in str(exc_value)
                ):
                    return False

        return True


# Flask logger
app.logger.handlers = logger.handlers
app.logger.setLevel(logger.level)
# Also handle Werkzeug's logger
werkzeug_logger = logging.getLogger("werkzeug")
werkzeug_logger.handlers = logger.handlers
werkzeug_logger.setLevel(logger.level)
# Add filter to suppress routine status endpoint polling logs and WebSocket upgrade errors
werkzeug_logger.addFilter(LogNoiseFilter())

# Set up authentication defaults
SESSION_COOKIE_SECURE = string_to_bool(SESSION_COOKIE_SECURE_ENV)


def _write_secret_atomically(secret_path: Path, secret: bytes) -> None:
    """Publish a secret with owner-only permissions and no partial file window."""
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret_path.parent.chmod(0o700)
    temporary_path = secret_path.with_name(f".{secret_path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(secret)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o600)
        temporary_path.replace(secret_path)
        secret_path.chmod(0o600)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def _load_or_create_secret_file(name: str, *, size: int = 64) -> bytes:
    secret_path = CONFIG_DIR / name
    try:
        if secret_path.exists():
            secret = secret_path.read_bytes()
            if len(secret) >= FLASK_SECRET_KEY_MIN_BYTES:
                return secret
            logger.warning("Invalid persisted secret at %s; regenerating.", secret_path)
    except OSError as exc:
        logger.warning("Failed to read persisted secret at %s: %s", secret_path, exc)

    secret = os.urandom(size)
    _write_secret_atomically(secret_path, secret)
    return secret


def _load_or_create_secret_key() -> bytes:
    """Load a persisted Flask secret key from config, or create one."""
    try:
        return _load_or_create_secret_file(".flask_secret")
    except OSError as exc:
        logger.warning(
            "Failed to persist Flask secret key. Sessions may reset on restart: %s",
            exc,
        )
        return os.urandom(64)


app.config.update(
    SECRET_KEY=_load_or_create_secret_key(),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=SESSION_COOKIE_SECURE,
    SESSION_COOKIE_NAME=SESSION_COOKIE_NAME,
    PERMANENT_SESSION_LIFETIME=604800,  # 7 days in seconds
)

logger.info(
    "Session cookie secure setting: %s (from env: %s)",
    SESSION_COOKIE_SECURE,
    SESSION_COOKIE_SECURE_ENV,
)
logger.info("Session cookie name: %s", SESSION_COOKIE_NAME)

if user_db is not None:
    try:
        kobo_pepper = _load_or_create_secret_file(".kobo_pepper")
        kobo_token_service = TokenService(_user_db_path, pepper=kobo_pepper)
        kobo_sync_service = KoboSyncService(_user_db_path, cursor_secret=kobo_pepper)
        kobo_token_service.initialize()
        kobo_roots = tuple(
            dict.fromkeys(
                Path(root)
                for root in (
                    normalize_optional_text(app_config.get("DESTINATION", "/books")) or "/books",
                )
                if root
            )
        )
        kepub_converter = None
        kepub_binary = normalize_optional_text(os.environ.get("KEPUBIFY_BINARY", ""))
        kepub_version = normalize_optional_text(os.environ.get("KEPUBIFY_VERSION", ""))
        kepub_sha256 = normalize_optional_text(os.environ.get("KEPUBIFY_SHA256", ""))
        kepub_cache_directory = normalize_optional_text(os.environ.get("KEPUB_CACHE_DIR", ""))
        kepub_metadata_directory = Path(
            normalize_optional_text(
                os.environ.get("KEPUBIFY_METADATA_DIR", "/usr/local/share/bookflow")
            )
            or "/usr/local/share/bookflow"
        )
        if kepub_version is None:
            with suppress(OSError):
                kepub_version = normalize_optional_text(
                    (kepub_metadata_directory / "kepubify.version").read_text(encoding="utf-8")
                )
        if kepub_sha256 is None:
            with suppress(OSError):
                kepub_sha256 = normalize_optional_text(
                    (kepub_metadata_directory / "kepubify.sha256").read_text(encoding="ascii")
                )
        if (
            kepub_binary is not None
            and kepub_version is not None
            and kepub_sha256 is not None
            and kepub_cache_directory is not None
        ):
            try:
                kepub_service = KepubCatalogService(
                    KepubConverter(
                        KepubifyBinary(
                            path=Path(kepub_binary),
                            version=kepub_version,
                            sha256=kepub_sha256,
                        ),
                        Path(kepub_cache_directory),
                    ),
                    FileStore(
                        Path(
                            normalize_optional_text(app_config.get("DESTINATION", "/books"))
                            or "/books"
                        )
                    ),
                    CatalogService(_user_db_path),
                )
                kepub_converter = kepub_service.convert_entitled_for_user
            except OSError, RuntimeError, ValueError:
                logger.exception("KEPUB conversion is unavailable")

        def export_kobo_file(user_id: int, original: dict[str, Any]) -> dict[str, Any]:
            if kepub_converter is None or original["file_format"].lower() != "epub":
                return original
            derived_id = kepub_converter(
                user_id=user_id, book_id=original["book_id"], source_file_id=original["id"]
            )
            derived = CatalogService(_user_db_path).get_entitled_book_file(
                user_id=user_id, book_id=original["book_id"], file_id=derived_id
            )
            if derived is None:
                raise RuntimeError("Converted Kobo file is unavailable")
            return derived

        register_kobo_routes(
            app,
            token_service=kobo_token_service,
            sync_service=kobo_sync_service,
            allowed_file_roots=kobo_roots,
            cover_fetcher=lambda cache_id, url: get_image_cache().fetch_and_cache(cache_id, url),
            sync_audit_recorder=user_db.record_kobo_sync,
            file_exporter=export_kobo_file if kepub_converter is not None else None,
        )
        register_kobo_settings_routes(
            app,
            db_path=_user_db_path,
            resolve_actor=resolve_v1_actor,
            token_service=kobo_token_service,
            sync_service=kobo_sync_service,
            kepub_converter=kepub_converter,
        )
        pairing_service = KoboPairingService(
            _user_db_path,
            secret=kobo_pepper,
            token_service=kobo_token_service,
        )
        register_kobo_pairing_routes(
            app,
            resolve_actor=resolve_v1_actor,
            service=pairing_service,
            patcher_cache=Path(os.environ.get("CONFIG_DIR", "/config")) / "kobopatcher",
            bundled_patcher_dir=Path("/opt/bookflow/kobo-patcher"),
            config_root=Path(os.environ.get("CONFIG_DIR", "/config")),
            release_api_url=normalize_optional_text(
                os.environ.get(
                    "KOBO_PATCHER_RELEASE_API_URL",
                    "https://api.github.com/repos/Timminater/BookFlow/releases/tags/"
                    f"v{RELEASE_VERSION.removeprefix('v')}",
                )
            ),
            release_version=RELEASE_VERSION,
        )

        def _add_fulfilled_book_to_kobo(user_id: int, book_id: int, file_id: int) -> None:
            # The request flag is explicit Kobo intent, so it may enable the
            # user's isolated shelf before adding the just-granted entitlement.
            kobo_sync_service.enable_user(user_id=user_id)
            kobo_sync_service.set_book(
                user_id=user_id,
                book_id=book_id,
                file_id=file_id,
                source="request",
            )

        manual_kobo_fulfilment = _add_fulfilled_book_to_kobo
        if request_runtime is not None:
            request_runtime.set_kobo_fulfilment(_add_fulfilled_book_to_kobo)
    except OSError, sqlite3.Error, ValueError:
        logger.exception("Kobo services unavailable")

if request_runtime is not None:
    # Start only after optional Kobo fulfilment has been composed, so an
    # immediately recovered job cannot miss an explicit add-to-Kobo intent.
    request_runtime.start()


@app.before_request
def enforce_auth_availability() -> Response | tuple[Response, int] | None:
    """Fail closed when a configured authentication provider is unavailable."""
    if get_auth_mode() != AUTH_MODE_UNAVAILABLE:
        return None
    if request.path in _PUBLIC_OBSERVABILITY_PATHS:
        return None
    if not request.path.startswith(("/api", f"{BASE_PATH}/api" if BASE_PATH else "/api")):
        return None
    return (
        jsonify(
            {
                "error": "Authentication provider unavailable",
                "code": "auth_unavailable",
            }
        ),
        503,
    )


@app.before_request
def enforce_browser_mutation_origin() -> Response | tuple[Response, int] | None:
    """Reject cross-site state changes made with ambient browser credentials."""
    if request.method in {"GET", "HEAD", "OPTIONS", "TRACE"}:
        return None
    # Reader requests authenticate with the URL token, and pairing exchanges
    # with a signed one-time code/session. Neither uses ambient cookies.
    # Keep browser provenance checks on account/settings/code-creation routes.
    if (
        not request.headers.get("Origin")
        and not request.headers.get("Sec-Fetch-Site")
        and (
            request.blueprint == "kobo_mvp"
            or request.endpoint
            in {"kobo_pairing.claim", "kobo_pairing.complete", "kobo_pairing.revoke_session"}
        )
    ):
        return None
    if app.config.get("TESTING") and not app.config.get("ENFORCE_MUTATION_ORIGIN"):
        return None

    additional_origins = tuple(configured_socketio_origins)
    if _is_debug_enabled():
        additional_origins += ("http://localhost:5173", "http://127.0.0.1:5173")
    if mutation_request_is_same_origin(
        origin=request.headers.get("Origin"),
        request_origin=request.host_url.rstrip("/"),
        sec_fetch_site=request.headers.get("Sec-Fetch-Site"),
        csrf_header=request.headers.get("X-BookFlow-CSRF"),
        additional_origins=additional_origins,
    ):
        return None
    logger.warning(
        "Rejected cross-site mutation: method=%s path=%s origin=%s",
        request.method,
        request.path,
        request.headers.get("Origin", ""),
    )
    return (
        jsonify(
            {
                "error": "Cross-site mutation rejected",
                "code": "csrf_origin_rejected",
            }
        ),
        403,
    )


@app.before_request
def proxy_auth_middleware() -> Response | tuple[Response, int] | None:
    """Middleware to handle proxy authentication.

    When AUTH_METHOD is set to "proxy", this middleware automatically
    authenticates users based on headers set by the reverse proxy.
    """
    auth_mode = get_auth_mode()

    # Only run for proxy auth mode
    if auth_mode != "proxy":
        return None

    if not request.environ.get("bookflow.trusted_proxy_peer") and not app.config.get("TESTING"):
        logger.error("Rejected proxy authentication from an untrusted direct peer")
        return (
            jsonify(
                {
                    "error": "Proxy authentication requires a trusted proxy",
                    "code": "untrusted_proxy_peer",
                }
            ),
            503,
        )

    # Skip for public endpoints that don't need auth
    if request.path in _PUBLIC_OBSERVABILITY_PATHS:
        return None

    def get_proxy_header(header_name: str) -> str | None:
        """Resolve proxy auth values from headers with WSGI env fallbacks."""
        value = request.headers.get(header_name)
        if value:
            return value

        env_key = f"HTTP_{header_name.upper().replace('-', '_')}"
        value = request.environ.get(env_key)
        if value:
            return value

        # Some proxies set authenticated username in REMOTE_USER (not as a header).
        if header_name.lower().replace("_", "-") == "remote-user":
            return request.environ.get("REMOTE_USER")

        return None

    try:
        user_header = (
            normalize_optional_text(app_config.get("PROXY_AUTH_USER_HEADER", "X-Auth-User"))
            or "X-Auth-User"
        )

        # Extract username from proxy header
        username = get_proxy_header(user_header)

        if not username:
            if request.path.startswith("/api/auth/"):
                return None

            logger.warning("Proxy auth enabled but no username found in header '%s'", user_header)
            return jsonify({"error": "Authentication required. Proxy header not set."}), 401

        # Resolve admin role for proxy sessions.
        # If an admin group is configured, derive from groups header.
        # Otherwise preserve the existing DB role for known users and default
        # first-time users to the non-privileged user role.
        admin_group_header = (
            normalize_optional_text(
                app_config.get("PROXY_AUTH_ADMIN_GROUP_HEADER", "X-Auth-Groups")
            )
            or "X-Auth-Groups"
        )
        admin_group_name = (
            normalize_optional_text(app_config.get("PROXY_AUTH_ADMIN_GROUP_NAME", "")) or ""
        )
        is_admin = False

        if admin_group_name:
            groups_header = get_proxy_header(admin_group_header) or ""
            user_groups_delimiter = "," if "," in groups_header else "|"
            user_groups = [
                g.strip() for g in groups_header.split(user_groups_delimiter) if g.strip()
            ]
            is_admin = admin_group_name in user_groups
        elif user_db is not None:
            existing_db_user = user_db.get_user(username=username)
            if existing_db_user:
                is_admin = existing_db_user.get("role") == "admin"

        # Create or update session
        previous_username = session.get("user_id")
        if previous_username and previous_username != username:
            # Header identity changed mid-session; force reprovision for the new user.
            session.pop("db_user_id", None)

        session["user_id"] = username
        session["is_admin"] = is_admin

        # Provision proxy-authenticated users into users.db for multi-user features.
        # Re-provision when db_user_id is missing/stale/mismatched to avoid broken
        # sessions after DB resets or auth-mode transitions.
        if user_db is not None:
            raw_db_user_id = session.get("db_user_id")
            session_db_user = None

            if raw_db_user_id is not None:
                try:
                    session_db_user = user_db.get_user(user_id=int(raw_db_user_id))
                except TypeError, ValueError:
                    session_db_user = None

            needs_db_user_sync = (
                raw_db_user_id is None
                or session_db_user is None
                or session_db_user.get("auth_source") != "proxy"
                or (
                    session_db_user.get("username") != username
                    and not str(session_db_user.get("username", "")).startswith(
                        f"{username}__proxy"
                    )
                )
            )

            if needs_db_user_sync:
                role = "admin" if is_admin else "user"
                db_user, _ = upsert_external_user(
                    user_db,
                    auth_source="proxy",
                    username=username,
                    role=role,
                    collision_strategy="alias",
                    alias_suffix="__proxy",
                    context="proxy_request",
                )
                if db_user is None:
                    _raise_runtime_error("Unexpected proxy user sync result: no user returned")

                session["db_user_id"] = db_user["id"]

        session.permanent = False
    except _OPERATIONAL_ERRORS:
        logger.exception("Proxy auth middleware error")
        return jsonify({"error": "Authentication error"}), 500
    else:
        return None


@app.before_request
def validate_audited_web_session() -> Response | tuple[Response, int] | None:
    """Enforce revocation for newly issued local, CWA, and OIDC browser sessions."""
    if get_auth_mode() not in {"builtin", "cwa", "oidc"} or user_db is None:
        return None
    session_key = session.get("audit_session_key")
    db_user_id = normalize_positive_int(session.get("db_user_id"))
    if not isinstance(session_key, str) or not session_key or db_user_id is None:
        # Existing signed sessions remain valid during the migration window.
        return None
    try:
        if user_db.validate_web_session(user_id=db_user_id, session_key=session_key):
            return None
    except _OPERATIONAL_ERRORS:
        logger.exception("Failed to validate audited web session")
        return jsonify({"error": "Authentication service unavailable"}), 503

    session.clear()
    if request.path.startswith("/api"):
        return jsonify({"error": "Session expired or revoked"}), 401
    return None


@app.after_request
def set_security_headers(response: Response) -> Response:
    """Add baseline security headers to every response."""
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline' blob:; "
        "img-src 'self' data: blob:; connect-src 'self' ws: wss:; "
        "worker-src 'self' blob:; frame-src 'self' blob:; object-src 'none'",
    )
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Embedder-Policy", "credentialless")
    return response


def login_required(
    f: Callable[..., Response | tuple[Response, int]],
) -> Callable[..., Response | tuple[Response, int]]:
    """Require authentication for a Flask route."""

    @wraps(f)
    def decorated_function(*args: object, **kwargs: object) -> Response | tuple[Response, int]:
        auth_mode = get_auth_mode()

        if auth_mode == AUTH_MODE_UNAVAILABLE:
            return (
                jsonify(
                    {
                        "error": "Authentication provider unavailable",
                        "code": "auth_unavailable",
                    }
                ),
                503,
            )

        # If no authentication is configured, allow access
        if auth_mode == "none":
            return f(*args, **kwargs)

        # If CWA mode and database disappeared after startup, return error
        if auth_mode == "cwa" and CWA_DB_PATH and not CWA_DB_PATH.exists():
            logger.error("CWA database at %s is no longer accessible", CWA_DB_PATH)
            return jsonify({"error": "Internal Server Error"}), 500

        # Check if user has a valid session
        if "user_id" not in session:
            return jsonify({"error": "Unauthorized"}), 401

        # Check admin access for settings/onboarding endpoints.
        if is_settings_or_onboarding_path(request.path):
            try:
                if requires_admin_for_settings_access(request.path, {}) and not session.get(
                    "is_admin", False
                ):
                    return jsonify({"error": "Admin access required"}), 403

            except RuntimeError, TypeError, ValueError:
                logger.exception("Admin access check error")
                return jsonify({"error": "Internal Server Error"}), 500

        return f(*args, **kwargs)

    return decorated_function


_BASE_TAG = '<base href="/" data-bookflow-base />'


def _base_href() -> str:
    if not BASE_PATH:
        return "/"
    return f"{BASE_PATH}/"


def _serve_index_html() -> Response:
    """Serve index.html with an adjusted base tag for subpath deployments."""
    index_path = FRONTEND_DIST / "index.html"
    try:
        with index_path.open(encoding="utf-8") as handle:
            html = handle.read()
    except OSError:
        return send_from_directory(FRONTEND_DIST, "index.html")

    if BASE_PATH and _BASE_TAG in html:
        html = html.replace(_BASE_TAG, f'<base href="{_base_href()}" data-bookflow-base />', 1)

    return Response(html, mimetype="text/html")


# Serve frontend static files
@app.route("/assets/<path:filename>")
def serve_frontend_assets(filename: str) -> Response:
    """Serve static assets from the built frontend."""
    return send_from_directory(FRONTEND_DIST / "assets", filename)


@app.route("/")
def index() -> Response:
    """Serve the React frontend application.

    Authentication is handled by the React app itself.
    """
    return _serve_index_html()


@app.route("/theme-init.js")
def theme_init_js() -> Response:
    """Serve the blocking theme-init script."""
    return send_from_directory(FRONTEND_DIST, "theme-init.js", mimetype="application/javascript")


@app.route("/service-worker.js")
def service_worker_js() -> Response:
    """Serve the PWA worker as JavaScript instead of the SPA fallback."""
    response = send_from_directory(
        FRONTEND_DIST,
        "service-worker.js",
        mimetype="application/javascript",
    )
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Service-Worker-Allowed"] = _base_href()
    return response


@app.route("/manifest.webmanifest")
def web_manifest() -> Response:
    """Serve install metadata with the web-manifest media type."""
    return send_from_directory(
        FRONTEND_DIST,
        "manifest.webmanifest",
        mimetype="application/manifest+json",
    )


@app.route("/offline.html")
def offline_page() -> Response:
    """Serve the non-personal offline fallback."""
    return send_from_directory(FRONTEND_DIST, "offline.html", mimetype="text/html")


@app.route("/icon-<int:size>.png")
@app.route("/icon-maskable-<int:size>.png")
def pwa_icon(size: int) -> Response:
    """Serve only the generated PWA icon sizes."""
    filename = (
        f"icon-maskable-{size}.png"
        if request.path.rsplit("/", 1)[-1].startswith("icon-maskable-")
        else f"icon-{size}.png"
    )
    if filename not in {"icon-192.png", "icon-512.png", "icon-maskable-512.png"}:
        abort(404)
    return send_from_directory(FRONTEND_DIST, filename, mimetype="image/png")


@app.route("/logo.png")
def logo() -> Response:
    """Serve logo from built frontend assets."""
    return send_from_directory(FRONTEND_DIST, "logo.png", mimetype="image/png")


@app.route("/favicon.ico")
@app.route("/favico<path:_>")
def favicon(_: Any = None) -> Response:
    """Serve favicon from built frontend assets."""
    return send_from_directory(FRONTEND_DIST, "favicon.ico", mimetype="image/vnd.microsoft.icon")


if _is_debug_enabled():
    import subprocess

    def _stop_gui() -> None:
        return None

    if app_config.get("USING_EXTERNAL_BYPASSER", False):
        pass
    else:
        from bookflow.bypass.internal_bypasser import _cleanup_orphan_processes

        def _stop_gui() -> None:
            _cleanup_orphan_processes()

    @app.route("/api/debug", methods=["GET"])
    @login_required
    def debug() -> Response | tuple[Response, int]:
        """Run `/app/genDebug.sh`, generate a debug zip, and return it.

        The file is written to `/tmp/bookflow-debug.zip` before being returned.
        """
        try:
            logger.info("Debug endpoint called, stopping GUI and generating debug info...")
            _stop_gui()
            time.sleep(1)
            result = subprocess.run(
                ["/app/genDebug.sh"], capture_output=True, text=True, check=True
            )
            if result.returncode != 0:
                _raise_runtime_error(f"Debug script failed: {result.stderr}")
            logger.info("Debug script executed: %s", result.stdout)
            debug_file_path = result.stdout.strip().split("\n")[-1]
            if not Path(debug_file_path).exists():
                logger.error("Debug zip file not found at: %s", debug_file_path)
                return jsonify({"error": "Failed to generate debug information"}), 500

            logger.info("Sending debug file: %s", debug_file_path)
            return send_file(
                debug_file_path,
                mimetype="application/zip",
                download_name=Path(debug_file_path).name,
                as_attachment=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error_trace(f"Debug script error: {e}, stdout: {e.stdout}, stderr: {e.stderr}")
            return jsonify({"error": f"Debug script failed: {e.stderr}"}), 500
        except _OPERATIONAL_ERRORS as e:
            logger.error_trace(f"Debug endpoint error: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/restart", methods=["POST"])
    @login_required
    def restart() -> Response | tuple[Response, int]:
        """Restart the application."""
        if get_auth_mode() != "none" and not session.get("is_admin", False):
            return jsonify({"error": "Admin access required"}), 403
        os._exit(0)


def _serialize_release(release: Release) -> dict:
    """Serialize a release for the frontend, normalizing preview URLs."""
    from dataclasses import asdict

    from bookflow.core.utils import transform_cover_url

    result = asdict(release)
    extra = result.get("extra")
    if isinstance(extra, dict):
        preview = extra.get("preview")
        if isinstance(preview, str) and preview:
            extra = dict(extra)
            extra["preview"] = transform_cover_url(preview, release.source_id)
            result["extra"] = extra

    return result


@app.route("/api/releases/download", methods=["POST"])
@login_required
def api_download_release() -> Response | tuple[Response, int]:
    """Queue a release for download.

    This endpoint is used when downloading from the ReleaseModal where the
    frontend already has all the release data from the search results.

    Request Body (JSON):
        source (str): Release source (e.g., "direct_download")
        source_id (str): ID within the source (e.g., AA MD5 hash)
        title (str): Book title
        format (str, optional): File format
        size (str, optional): Human-readable size
        extra (dict, optional): Additional metadata

    Returns:
        flask.Response: JSON status object indicating success or failure.

    """
    try:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "No data provided"}), 400

        if "source_id" not in data:
            return jsonify({"error": "source_id is required"}), 400
        if "source" not in data:
            return jsonify({"error": "source is required"}), 400

        capability_gate = _require_download_capability()
        if capability_gate is not None:
            return capability_gate

        source = data["source"]
        resolved_content_type, inferred_content_type = _resolve_release_content_type(data, source)
        if resolved_content_type != "ebook":
            return jsonify({"error": "Audiobooks are no longer supported"}), 400

        release_payload = data
        if inferred_content_type and data.get("content_type") is None:
            release_payload = dict(data)
            release_payload["content_type"] = resolved_content_type

        priority = data.get("priority", 0)
        # Per-user download overrides
        db_user_id = session.get("db_user_id")
        _username = session.get("user_id")
        db_user_id, _username, on_behalf_error = _resolve_download_user_context(
            db_user_id,
            _username,
            data.get("on_behalf_of_user_id"),
        )
        if on_behalf_error:
            return on_behalf_error
        raw_library_book_id = data.get("library_book_id")
        library_book_id = normalize_positive_int(raw_library_book_id)
        if raw_library_book_id is not None and library_book_id is None:
            return jsonify({"error": "library_book_id must be a positive integer"}), 400
        if (
            get_auth_mode() != "none"
            and not session.get("is_admin", False)
            and library_book_id is None
        ):
            return jsonify({"error": "library_book_id is required"}), 400
        if library_book_id is not None:
            if (
                library_service is None
                or db_user_id is None
                or not library_service.is_in_library(user_id=db_user_id, book_id=library_book_id)
            ):
                return jsonify({"error": "Book is not in your library"}), 403
            release_payload = dict(release_payload)
            release_payload["library_book_id"] = library_book_id
        success, error_msg = backend.queue_release(
            release_payload,
            priority,
            user_id=db_user_id,
            username=_username,
        )

        if success:
            return jsonify({"status": "queued", "priority": priority})
        return jsonify({"error": error_msg or "Failed to queue release"}), 500
    except _OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Release download error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/config", methods=["GET"])
@login_required
def api_config() -> Response | tuple[Response, int]:
    """Get application configuration for frontend.

    Uses the dynamic config singleton to ensure settings changes
    are reflected without requiring a container restart.
    """
    try:
        from bookflow.config.env import _is_config_dir_writable
        from bookflow.core.onboarding import is_onboarding_complete as _get_onboarding_complete
        from bookflow.metadata_providers import (
            get_provider_default_sort,
            get_provider_search_fields,
            get_provider_sort_options,
        )

        db_user_id = get_session_db_user_id(session)

        default_release_source = app_config.get(
            "DEFAULT_RELEASE_SOURCE",
            "",
            user_id=db_user_id,
        )
        configured_metadata_provider = normalize_optional_text(
            app_config.get(
                "METADATA_PROVIDER",
                "",
                user_id=db_user_id,
            )
        )
        metadata_ui_provider = configured_metadata_provider

        config = {
            "debug": app_config.get("DEBUG", False),
            "build_version": BUILD_VERSION,
            "release_version": RELEASE_VERSION,
            "book_languages": _SUPPORTED_BOOK_LANGUAGE,
            "default_language": app_config.BOOK_LANGUAGE,
            "default_ui_language": app_config.get("DEFAULT_UI_LANGUAGE", "en"),
            "supported_formats": app_config.SUPPORTED_FORMATS,
            "metadata_sort_options": get_provider_sort_options(metadata_ui_provider),
            "metadata_search_fields": get_provider_search_fields(metadata_ui_provider),
            "default_release_source": default_release_source,
            "show_release_source_links": app_config.get("SHOW_RELEASE_SOURCE_LINKS", True),
            "library_auto_find_releases": app_config.get("LIBRARY_AUTO_FIND_RELEASES", True),
            "hardcover_auto_remove_on_download": app_config.get(
                "HARDCOVER_AUTO_REMOVE_ON_DOWNLOAD", True
            ),
            "settings_enabled": _is_config_dir_writable(),
            "onboarding_complete": _get_onboarding_complete(),
            "issue_reporting_enabled": github_issue_service is not None,
            # Default sort orders
            "metadata_default_sort": get_provider_default_sort(metadata_ui_provider),
        }
        return jsonify(config)
    except _IMPORT_OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Config error: {e}")
        return jsonify({"error": str(e)}), 500


@app.before_request
def begin_request_observation() -> None:
    """Record a monotonic request start without storing paths or query values."""
    request.environ["bookflow.request_started"] = time.monotonic()


@app.after_request
def finish_request_observation(response: Response) -> Response:
    """Publish bounded request metrics and a correlation identifier."""
    started = request.environ.get("bookflow.request_started")
    duration = time.monotonic() - started if isinstance(started, float) else 0.0
    request_metrics.observe(
        method=request.method,
        status=response.status_code,
        duration_seconds=duration,
    )
    response.headers.setdefault(
        "X-Correlation-Id",
        request.headers.get("X-Correlation-Id") or uuid.uuid4().hex,
    )
    return response


@app.get("/api/health")
@app.get("/api/health/live")
def api_health() -> Response:
    """Liveness endpoint for container orchestration.

    No authentication required.
    """
    response: dict[str, object] = {"status": "ok"}

    # Report degraded features
    if not backend.WEBSOCKET_AVAILABLE:
        response["degraded"] = {"websocket": "WebSocket unavailable - real-time updates disabled"}

    return jsonify(response)


@app.get("/api/health/ready")
def api_health_ready() -> Response | tuple[Response, int]:
    """Readiness checks authentication, SQLite and configured storage."""
    checks: dict[str, str] = {}
    checks["authentication"] = "unavailable" if get_auth_mode() == AUTH_MODE_UNAVAILABLE else "ok"
    try:
        with sqlite3.connect(_user_db_path, timeout=2) as connection:
            checks["database"] = str(connection.execute("PRAGMA quick_check(1)").fetchone()[0])
    except OSError, sqlite3.Error:
        checks["database"] = "unavailable"

    destination = Path(normalize_optional_text(app_config.get("DESTINATION", "/books")) or "/books")
    checks["storage"] = (
        "ok"
        if destination.is_dir() and os.access(destination, os.R_OK | os.W_OK)
        else "unavailable"
    )
    ready = all(value == "ok" for value in checks.values())
    return jsonify(
        {"status": "ok" if ready else "not_ready", "checks": checks}
    ), 200 if ready else 503


@app.get("/api/metrics")
def api_metrics() -> Response:
    """Expose non-sensitive aggregate application metrics."""
    return app.response_class(
        request_metrics.render(),
        content_type="text/plain; version=0.0.4; charset=utf-8",
    )


def _resolve_status_scope(*, require_authenticated: bool = True) -> tuple[bool, int | None, bool]:
    """Resolve queue-status visibility from session state.

    Returns:
        (is_admin, db_user_id, can_access_status)

    """
    auth_mode = get_auth_mode()
    if auth_mode == AUTH_MODE_UNAVAILABLE:
        return False, None, False
    if auth_mode == "none":
        return True, None, True

    if require_authenticated and "user_id" not in session:
        return False, None, False

    is_admin = bool(session.get("is_admin", False))
    if is_admin:
        return True, None, True

    db_user_id = get_session_db_user_id(session)

    if db_user_id is None:
        return False, None, False

    return False, db_user_id, True


def _queue_status_to_final_activity_status(status: QueueStatus) -> str | None:
    return status.value if status in TERMINAL_QUEUE_STATUSES else None


def _queue_status_to_notification_event(status: QueueStatus) -> NotificationEvent | None:
    if status == QueueStatus.COMPLETE:
        return NotificationEvent.DOWNLOAD_COMPLETE
    if status == QueueStatus.ERROR:
        return NotificationEvent.DOWNLOAD_FAILED
    return None


def _notify_admin_for_terminal_download_status(
    *, task_id: str, status: QueueStatus, task: Any
) -> None:
    event = _queue_status_to_notification_event(status)
    if event is None:
        return

    request_id = normalize_positive_int(getattr(task, "request_id", None))
    book_id = normalize_positive_int(getattr(task, "library_book_id", None))
    request_context: dict[str, Any] | None = None
    if user_db is not None and (request_id is not None or book_id is not None):
        try:
            request_context = user_db.get_request_notification_context(
                request_id=request_id,
                book_id=book_id,
            )
        except _OPERATIONAL_ERRORS as exc:
            logger.warning(
                "Failed to resolve requesters for download notification %s: %s",
                task_id,
                exc,
            )

    requesters = request_context.get("requesters", []) if request_context else []
    if not isinstance(requesters, (list, tuple)):
        requesters = []
    title = request_context.get("title") if request_context else None
    author = request_context.get("author") if request_context else None
    cover_url = request_context.get("cover_url") if request_context else None
    resolved_book_id = (
        normalize_positive_int(request_context.get("book_id")) if request_context else book_id
    )
    content_type = normalize_optional_text(getattr(task, "content_type", None))
    context = NotificationContext(
        event=event,
        title=str(title or getattr(task, "title", "Unknown title") or "Unknown title"),
        author=str(author or getattr(task, "author", "Unknown author") or "Unknown author"),
        username=normalize_optional_text(getattr(task, "username", None)),
        content_type=normalize_content_type(content_type) if content_type is not None else None,
        format=normalize_optional_text(getattr(task, "format", None)),
        source=normalize_source(getattr(task, "source", None)),
        error_message=(
            normalize_optional_text(getattr(task, "status_message", None))
            if event == NotificationEvent.DOWNLOAD_FAILED
            else None
        ),
        book_id=resolved_book_id,
        requesters=tuple(str(requester) for requester in requesters),
        cover_url=str(cover_url) if cover_url else None,
    )
    try:
        notify_admin(event, context)
    except (RuntimeError, TypeError, ValueError) as exc:
        logger.warning(
            "Failed to trigger admin notification for download %s (%s): %s",
            task_id,
            status.value,
            exc,
        )


def _emit_activity_update_for_task(*, payload: dict[str, Any], task: Any) -> None:
    owner_user_id = normalize_positive_int(getattr(task, "user_id", None))
    emit_ws_event(
        ws_manager,
        event_name="activity_update",
        room="admins",
        payload=payload,
    )
    if owner_user_id is None:
        return
    emit_ws_event(
        ws_manager,
        event_name="activity_update",
        room=f"user_{owner_user_id}",
        payload=payload,
    )


def _build_download_file_rows(task: Any) -> list[dict[str, Any]]:
    """Build per-file row dicts for :meth:`finalize_download_files`.

    Reads ``task.library_paths`` (populated by the folder output handler
    from ``transfer_book_files``, or defaulted to ``[download_path]`` by
    the orchestrator for single-path output handlers) and derives the
    per-file ``format`` (from the path extension) and ``size`` (from the
    on-disk file size when stat succeeds) for each row. Returns an empty
    list when the transfer produced no paths — the sentinel is still
    cleared and no file rows are inserted.
    """
    raw_paths = getattr(task, "library_paths", None)
    if not raw_paths:
        download_path = normalize_optional_text(getattr(task, "download_path", None))
        if download_path is None:
            return []
        raw_paths = [download_path]

    rows: list[dict[str, Any]] = []
    for raw_path in raw_paths:
        normalized_path = normalize_optional_text(raw_path)
        if normalized_path is None:
            continue
        file_format: str | None = None
        file_size: str | None = None
        try:
            path_obj = Path(normalized_path)
            suffix = path_obj.suffix.lstrip(".")
            if suffix:
                file_format = suffix.lower()
            try:
                file_size = str(path_obj.stat().st_size)
            except OSError:
                file_size = None
        except TypeError, ValueError:
            pass
        rows.append(
            {
                "download_path": normalized_path,
                "format": file_format,
                "size": file_size,
            }
        )
    return rows


def _record_download_queued(task_id: str, task: Any) -> None:
    """Persist initial download record when a task enters the queue."""
    if download_history_service is None:
        return

    owner_user_id = normalize_positive_int(getattr(task, "user_id", None))
    request_id = normalize_positive_int(getattr(task, "request_id", None))
    origin = "requested" if request_id else "direct"

    source_name = normalize_source(getattr(task, "source", None))
    source_display = get_source_display_name(source_name)

    try:
        download_history_service.record_download(
            task_id=task_id,
            user_id=owner_user_id,
            username=normalize_optional_text(getattr(task, "username", None)),
            request_id=request_id,
            source=source_name,
            source_display_name=source_display,
            title=str(getattr(task, "title", "Unknown title") or "Unknown title"),
            author=normalize_optional_text(getattr(task, "author", None)),
            file_format=normalize_optional_text(getattr(task, "format", None)),
            size=normalize_optional_text(getattr(task, "size", None)),
            preview=normalize_optional_text(getattr(task, "preview", None)),
            content_type=normalize_optional_text(getattr(task, "content_type", None)),
            origin=origin,
            book_id=normalize_positive_int(getattr(task, "library_book_id", None)),
            retry_payload=backend.serialize_task_for_retry(task),
        )
    except _OPERATIONAL_ERRORS as exc:
        logger.warning("Failed to record download at queue time for task %s: %s", task_id, exc)
        return

    if activity_view_state_service is None:
        return

    try:
        cleared_view_state = 0
        cleared_view_state += activity_view_state_service.clear_item_for_all_viewers(
            item_type="download",
            item_key=f"download:{task_id}",
        )
        if request_id is not None:
            cleared_view_state += activity_view_state_service.clear_item_for_all_viewers(
                item_type="request",
                item_key=f"request:{request_id}",
            )
        if cleared_view_state > 0:
            _emit_activity_update_for_task(
                task=task,
                payload={
                    "kind": "activity_reset",
                    "task_id": task_id,
                },
            )
    except _OPERATIONAL_ERRORS as exc:
        logger.warning("Failed to reset activity viewer state for task %s: %s", task_id, exc)


def _record_download_terminal_snapshot(task_id: str, status: QueueStatus, task: Any) -> None:
    if normalize_content_type(getattr(task, "content_type", None)) == "audiobook":
        destination = Path(
            normalize_optional_text(app_config.get("DESTINATION", "/books")) or "/books"
        )
        deleted = discard_late_audiobook_artifacts(
            task,
            allowed_roots=(destination, TMP_DIR),
        )
        logger.warning(
            "Discarded retired audiobook callback for task %s (%d exact artifact(s) deleted)",
            task_id,
            len(deleted),
        )
        return

    _notify_admin_for_terminal_download_status(task_id=task_id, status=status, task=task)

    final_status = _queue_status_to_final_activity_status(status)
    if final_status is None:
        return

    # Canonical request_processes share the legacy coordinator but own their
    # persisted attempt/import/fulfilment lifecycle.
    if request_runtime is not None:
        try:
            request_runtime.handle_terminal(task, status=final_status)
        except _OPERATIONAL_ERRORS as exc:
            logger.warning("Failed to reconcile request_process task %s: %s", task_id, exc)

    finalized_download = False
    fulfilled_requests: list[dict[str, Any]] = []
    if download_history_service is not None:
        try:
            file_rows = _build_download_file_rows(task)
            fulfilled_requests = download_history_service.finalize_download_files(
                task_id=task_id,
                final_status=final_status,
                status_message=normalize_optional_text(getattr(task, "status_message", None)),
                file_rows=file_rows,
                retry_payload=backend.serialize_task_for_retry(task),
            )
            finalized_download = True
        except _OPERATIONAL_ERRORS as exc:
            logger.warning("Failed to finalize download history for task %s: %s", task_id, exc)

    if finalized_download:
        book_id = normalize_positive_int(getattr(task, "library_book_id", None))
        if (
            final_status == QueueStatus.COMPLETE.value
            and book_id is not None
            and user_db is not None
        ):
            try:
                _emit_request_update_events(fulfilled_requests)
                book = user_db.get_book_notification_context(book_id)
                if book:
                    for fulfilled in fulfilled_requests:
                        notify_user(
                            user_db,
                            fulfilled["user_id"],
                            NotificationEvent.REQUEST_FULFILLED,
                            NotificationContext(
                                event=NotificationEvent.REQUEST_FULFILLED,
                                title=book["title"],
                                author=book["author"],
                                book_id=book_id,
                            ),
                        )
            except _OPERATIONAL_ERRORS as exc:
                logger.warning("Failed to fulfil Requests for Book %s: %s", book_id, exc)
        _emit_activity_update_for_task(
            task=task,
            payload={
                "kind": "download_terminal",
                "task_id": task_id,
                "status": final_status,
            },
        )


def _task_owned_by_actor(
    task: Any, *, actor_user_id: int | None, actor_username: str | None
) -> bool:
    raw_task_user_id = getattr(task, "user_id", None)
    try:
        task_user_id = int(raw_task_user_id) if raw_task_user_id is not None else None
    except TypeError, ValueError:
        task_user_id = None

    if actor_user_id is not None and task_user_id is not None:
        return task_user_id == actor_user_id

    task_username = getattr(task, "username", None)
    if isinstance(task_username, str) and task_username.strip() and isinstance(actor_username, str):
        return task_username.strip() == actor_username.strip()

    return False


def _download_row_owned_by_actor(
    row: dict[str, Any],
    *,
    actor_user_id: int | None,
    actor_username: str | None,
) -> bool:
    owner_user_id = normalize_positive_int(row.get("user_id"))
    if actor_user_id is not None and owner_user_id is not None:
        return owner_user_id == actor_user_id

    row_username = normalize_optional_text(row.get("username"))
    if row_username is not None and isinstance(actor_username, str):
        return row_username == actor_username.strip()

    return False


def _resolve_queue_actor() -> tuple[bool, int | None, str | None, Response | None]:
    is_admin, db_user_id, can_access_status = _resolve_status_scope()
    actor_username = session.get("user_id")
    normalized_actor_username = actor_username if isinstance(actor_username, str) else None

    if not is_admin and (not can_access_status or db_user_id is None):
        return (
            is_admin,
            db_user_id,
            normalized_actor_username,
            jsonify({"error": "User identity unavailable", "code": "user_identity_unavailable"}),
        )

    return is_admin, db_user_id, normalized_actor_username, None


def _queue_task_visible_to_actor(
    task_id: str,
    *,
    is_admin: bool,
    actor_user_id: int | None,
    actor_username: str | None,
) -> bool:
    if is_admin:
        return True

    task = backend.book_queue.get_task(task_id)
    if task is None:
        return False

    return _task_owned_by_actor(
        task,
        actor_user_id=actor_user_id,
        actor_username=actor_username,
    )


backend.book_queue.set_queue_hook(_record_download_queued)
backend.book_queue.set_terminal_status_hook(_record_download_terminal_snapshot)


def _emit_request_update_events(updated_requests: list[dict[str, Any]]) -> None:
    """Broadcast request_update events for rows changed by delivery-state sync."""
    if not updated_requests or ws_manager is None:
        return

    for updated in updated_requests:
        payload = {
            "request_id": updated["id"],
            "status": updated["status"],
            "delivery_state": updated.get("delivery_state"),
            "title": (updated.get("book_data") or {}).get("title") or "Unknown title",
        }
        emit_ws_event(
            ws_manager,
            event_name="request_update",
            room=f"user_{updated['user_id']}",
            payload=payload,
        )
        emit_ws_event(
            ws_manager,
            event_name="request_update",
            room="admins",
            payload=payload,
        )


@app.route("/api/status", methods=["GET"])
@login_required
def api_status() -> Response | tuple[Response, int]:
    """Get current download queue status.

    Returns:
        flask.Response: JSON object with queue status.

    """
    try:
        is_admin, db_user_id, can_access_status = _resolve_status_scope()
        if not can_access_status:
            return jsonify({})

        user_id = None if is_admin else db_user_id
        status = backend.queue_status(user_id=user_id)
        if user_db is not None:
            updated_requests = sync_delivery_states_from_queue_status(
                user_db,
                queue_status=status,
                user_id=user_id,
            )
            _emit_request_update_events(updated_requests)
        return jsonify(status)
    except _OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Status error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/covers/<cover_id>", methods=["GET"])
@login_required
def api_cover(cover_id: str) -> Response | tuple[Response, int]:
    """Serve a cached book cover image.

    This endpoint proxies and caches cover images from external sources.
    Images are cached to disk for faster subsequent requests.

    Path Parameters:
        cover_id (str): Cover identifier (book ID or composite key for universal mode)

    Query Parameters:
        url (str): Base64-encoded original image URL (required on first request)

    Returns:
        flask.Response: Binary image data with appropriate Content-Type, or 404.

    """
    try:
        import base64

        from bookflow.config.env import is_covers_cache_enabled

        # Check if caching is enabled
        if not is_covers_cache_enabled():
            return jsonify({"error": "Cover caching is disabled"}), 404

        cache = get_image_cache()

        # Try to get from cache first
        cached = cache.get(cover_id)
        if cached:
            image_data, content_type = cached
            response = app.response_class(response=image_data, status=200, mimetype=content_type)
            response.headers["Cache-Control"] = "public, max-age=86400"
            response.headers["X-Cache"] = "HIT"
            return response

        # Cache miss - get URL from query parameter
        encoded_url = request.args.get("url")
        if not encoded_url:
            return jsonify({"error": "Cover URL not provided"}), 404

        try:
            original_url = base64.urlsafe_b64decode(encoded_url).decode()
        except (binascii.Error, UnicodeDecodeError) as e:
            logger.warning("Failed to decode cover URL: %s", e)
            return jsonify({"error": "Invalid cover URL encoding"}), 400

        # Fetch and cache the image
        result = cache.fetch_and_cache(cover_id, original_url)
        if not result:
            return jsonify({"error": "Failed to fetch cover image"}), 404

        image_data, content_type = result
        response = app.response_class(response=image_data, status=200, mimetype=content_type)
        response.headers["Cache-Control"] = "public, max-age=86400"
        response.headers["X-Cache"] = "MISS"
    except _IMPORT_OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Cover fetch error: {e}")
        return jsonify({"error": str(e)}), 500
    else:
        return response


@app.route("/api/download/<path:book_id>/cancel", methods=["DELETE"])
@login_required
def api_cancel_download(book_id: str) -> Response | tuple[Response, int]:
    """Cancel a download.

    Path Parameters:
        book_id (str): Book identifier to cancel

    Returns:
        flask.Response: JSON status indicating success or failure.

    """
    try:
        task = backend.book_queue.get_task(book_id)
        if task is None:
            return jsonify({"error": "Failed to cancel download or book not found"}), 404

        is_admin, db_user_id, can_access_status = _resolve_status_scope()
        if not is_admin:
            if not can_access_status or db_user_id is None:
                return jsonify(
                    {"error": "User identity unavailable", "code": "user_identity_unavailable"}
                ), 403

            actor_username = session.get("user_id")
            normalized_actor_username = actor_username if isinstance(actor_username, str) else None
            if not _task_owned_by_actor(
                task,
                actor_user_id=db_user_id,
                actor_username=normalized_actor_username,
            ):
                return jsonify({"error": "Forbidden", "code": "download_not_owned"}), 403

            if getattr(task, "request_id", None) is not None:
                return jsonify(
                    {"error": "Forbidden", "code": "requested_download_cancel_forbidden"}
                ), 403

        success = backend.cancel_download(book_id)
        if success:
            return jsonify({"status": "cancelled", "book_id": book_id})
        return jsonify({"error": "Failed to cancel download or book not found"}), 404
    except _OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Cancel download error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/download/<path:book_id>/retry", methods=["POST"])
@login_required
def api_retry_download(book_id: str) -> Response | tuple[Response, int]:
    """Retry a failed download."""
    try:
        task = backend.book_queue.get_task(book_id)
        history_row = None
        if task is None and download_history_service is not None:
            history_row = download_history_service.get_by_task_id(book_id)
        if task is None and history_row is None:
            return jsonify({"error": "Download not found"}), 404

        is_admin, db_user_id, can_access_status = _resolve_status_scope()
        actor_username = session.get("user_id")
        normalized_actor_username = actor_username if isinstance(actor_username, str) else None
        if not is_admin:
            if not can_access_status or db_user_id is None:
                return jsonify(
                    {"error": "User identity unavailable", "code": "user_identity_unavailable"}
                ), 403

            if task is not None:
                if not _task_owned_by_actor(
                    task,
                    actor_user_id=db_user_id,
                    actor_username=normalized_actor_username,
                ):
                    return jsonify({"error": "Forbidden", "code": "download_not_owned"}), 403
            elif history_row is None or not _download_row_owned_by_actor(
                history_row,
                actor_user_id=db_user_id,
                actor_username=normalized_actor_username,
            ):
                return jsonify({"error": "Forbidden", "code": "download_not_owned"}), 403

        if task is not None:
            task_status = backend.book_queue.get_task_status(book_id)
            if getattr(
                task, "request_id", None
            ) is not None and not backend.can_retry_download_task(task, task_status):
                return jsonify(
                    {"error": "Forbidden", "code": "requested_download_retry_forbidden"}
                ), 403
            success, error = backend.retry_download(book_id)
        else:
            if history_row is None:
                logger.error("Download history row disappeared while retrying task %s", book_id)
                return jsonify({"error": "Download history not found"}), 404
            request_id = normalize_positive_int(history_row.get("request_id"))
            retry_payload = history_row.get("retry_payload")
            final_status = history_row.get("final_status")
            if request_id is not None:
                history_service = download_history_service
                if history_service is None:
                    logger.error(
                        "Download history service unavailable while retrying task %s", book_id
                    )
                    return jsonify({"error": "Download history unavailable"}), 500
                if not history_service.is_retry_available(history_row):
                    return jsonify(
                        {"error": "Forbidden", "code": "requested_download_retry_forbidden"}
                    ), 403
            success, error = backend.retry_persisted_download(
                retry_payload,
                final_status=final_status,
            )

        if success:
            return jsonify({"status": "queued", "book_id": book_id})

        if error == "Download not found":
            return jsonify({"error": error}), 404

        return jsonify({"error": error or "Download cannot be retried"}), 409
    except _OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Retry download error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/queue/<path:book_id>/priority", methods=["PUT"])
@login_required
def api_set_priority(book_id: str) -> Response | tuple[Response, int]:
    """Set priority for a queued book.

    Path Parameters:
        book_id (str): Book identifier

    Request Body:
        priority (int): New priority level (lower number = higher priority)

    Returns:
        flask.Response: JSON status indicating success or failure.

    """
    try:
        data = request.get_json(silent=True)
        if not data or "priority" not in data:
            return jsonify({"error": "Priority not provided"}), 400

        priority = int(data["priority"])

        is_admin, db_user_id, actor_username, identity_error = _resolve_queue_actor()
        if identity_error is not None:
            return identity_error, 403

        task = backend.book_queue.get_task(book_id)
        if task is None:
            return jsonify({"error": "Failed to update priority or book not found"}), 404

        if not is_admin and not _task_owned_by_actor(
            task,
            actor_user_id=db_user_id,
            actor_username=actor_username,
        ):
            return jsonify({"error": "Forbidden", "code": "download_not_owned"}), 403

        success = backend.set_book_priority(book_id, priority)

        if success:
            return jsonify({"status": "updated", "book_id": book_id, "priority": priority})
        return jsonify({"error": "Failed to update priority or book not found"}), 404
    except ValueError:
        return jsonify({"error": "Invalid priority value"}), 400
    except _OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Set priority error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/queue/reorder", methods=["POST"])
@login_required
def api_reorder_queue() -> Response | tuple[Response, int]:
    """Bulk reorder queue by setting new priorities.

    Request Body:
        book_priorities (dict): Mapping of book_id to new priority

    Returns:
        flask.Response: JSON status indicating success or failure.

    """
    try:
        data = request.get_json(silent=True)
        if not data or "book_priorities" not in data:
            return jsonify({"error": "book_priorities not provided"}), 400

        book_priorities = data["book_priorities"]
        if not isinstance(book_priorities, dict):
            return jsonify({"error": "book_priorities must be a dictionary"}), 400

        # Validate all priorities are integers
        for book_id, priority in book_priorities.items():
            if not isinstance(priority, int):
                return jsonify({"error": f"Invalid priority for book {book_id}"}), 400

        is_admin, db_user_id, actor_username, identity_error = _resolve_queue_actor()
        if identity_error is not None:
            return identity_error, 403

        if not is_admin:
            owned_book_priorities = {}
            for book_id in book_priorities:
                task = backend.book_queue.get_task(str(book_id))
                if task is None:
                    continue
                if not _task_owned_by_actor(
                    task, actor_user_id=db_user_id, actor_username=actor_username
                ):
                    return jsonify({"error": "Forbidden", "code": "download_not_owned"}), 403
                owned_book_priorities[book_id] = book_priorities[book_id]
            book_priorities = owned_book_priorities

        success = backend.reorder_queue(book_priorities)

        if success:
            return jsonify({"status": "reordered", "updated_count": len(book_priorities)})
        return jsonify({"error": "Failed to reorder queue"}), 500
    except _OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Reorder queue error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/queue/order", methods=["GET"])
@login_required
def api_queue_order() -> Response | tuple[Response, int]:
    """Get current queue order for display.

    Returns:
        flask.Response: JSON array of queued books with their order and priorities.

    """
    try:
        queue_order = backend.get_queue_order()
        is_admin, db_user_id, actor_username, identity_error = _resolve_queue_actor()
        if identity_error is not None:
            return identity_error, 403
        if not is_admin:
            queue_order = [
                item
                for item in queue_order
                if _queue_task_visible_to_actor(
                    str(item.get("id", "")),
                    is_admin=False,
                    actor_user_id=db_user_id,
                    actor_username=actor_username,
                )
            ]
        return jsonify({"queue": queue_order})
    except _OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Queue order error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/downloads/active", methods=["GET"])
@login_required
def api_active_downloads() -> Response | tuple[Response, int]:
    """Get list of currently active downloads.

    Returns:
        flask.Response: JSON array of active download book IDs.

    """
    try:
        active_downloads = backend.get_active_downloads()
        is_admin, db_user_id, actor_username, identity_error = _resolve_queue_actor()
        if identity_error is not None:
            return identity_error, 403
        if not is_admin:
            active_downloads = [
                task_id
                for task_id in active_downloads
                if _queue_task_visible_to_actor(
                    task_id,
                    is_admin=False,
                    actor_user_id=db_user_id,
                    actor_username=actor_username,
                )
            ]
        return jsonify({"active_downloads": active_downloads})
    except _OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Active downloads error: {e}")
        return jsonify({"error": str(e)}), 500


@app.errorhandler(404)
def not_found_error(error: Exception) -> Response | tuple[Response, int]:
    """Handle 404 (Not Found) errors.

    Args:
        error (HTTPException): The 404 error raised by Flask.

    Returns:
        flask.Response: JSON error message with 404 status.

    """
    logger.warning("404 error: %s : %s", request.url, error)
    return jsonify({"error": "Resource not found"}), 404


@app.errorhandler(500)
def internal_error(error: Exception) -> Response | tuple[Response, int]:
    """Handle 500 (Internal Server) errors.

    Args:
        error (HTTPException): The 500 error raised by Flask.

    Returns:
        flask.Response: JSON error message with 500 status.

    """
    logger.error_trace(f"500 error: {error}")
    return jsonify({"error": "Internal server error"}), 500


def _failed_login_response(username: str, ip_address: str) -> tuple[Response, int]:
    """Handle a failed login attempt by recording it and returning the appropriate response."""
    is_now_locked = record_failed_login(username, ip_address)
    if user_db is not None:
        db_user = user_db.get_user(username=username)
        user_db.record_auth_event(
            user_id=int(db_user["id"]) if db_user is not None else None,
            username=username,
            auth_source=get_auth_mode(),
            event_type="login_failed",
            outcome="blocked" if is_now_locked else "failed",
            source_ip=ip_address,
            user_agent=request.headers.get("User-Agent"),
        )

    if is_now_locked:
        return jsonify(
            {
                "error": f"Account locked due to {MAX_LOGIN_ATTEMPTS} failed login attempts. Try again in {LOCKOUT_DURATION_MINUTES} minutes."
            }
        ), 429

    attempts_remaining = MAX_LOGIN_ATTEMPTS - failed_login_attempts[username]["count"]
    if attempts_remaining <= LOGIN_ATTEMPT_WARNING_THRESHOLD:
        return jsonify(
            {"error": f"Invalid username or password. {attempts_remaining} attempts remaining."}
        ), 401

    return jsonify({"error": "Invalid username or password."}), 401


def _start_audited_web_session(
    *,
    user_id: int,
    username: str,
    auth_source: str,
    source_ip: str,
) -> None:
    """Attach a revocable server-side audit record to the signed Flask session."""
    if user_db is None:
        return
    session["audit_session_key"] = user_db.create_web_session(
        user_id=user_id,
        auth_source=auth_source,
        source_ip=source_ip,
        user_agent=request.headers.get("User-Agent"),
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    user_db.record_auth_event(
        user_id=user_id,
        username=username,
        auth_source=auth_source,
        event_type="login",
        outcome="success",
        source_ip=source_ip,
        user_agent=request.headers.get("User-Agent"),
    )


@app.route("/api/auth/login", methods=["POST"])
def api_login() -> Response | tuple[Response, int]:
    """Login endpoint that validates credentials and creates a session.

    Supports both built-in credentials and CWA database authentication.
    Includes rate limiting: 10 failed attempts = 30 minute lockout.

    Request Body:
        username (str): Username
        password (str): Password
        remember_me (bool): Whether to extend session duration

    Returns:
        flask.Response: JSON with success status or error message.

    """
    try:
        ip_address = get_client_ip()
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "No data provided"}), 400

        auth_mode = get_auth_mode()
        if auth_mode == "proxy":
            return jsonify({"error": "Proxy authentication is enabled"}), 401

        if auth_mode in ("builtin", "oidc") and DISABLE_LOCAL_AUTH:
            return jsonify({"error": "Local authentication is disabled"}), 403

        if auth_mode == "oidc" and HIDE_LOCAL_AUTH:
            return jsonify({"error": "Local authentication is disabled"}), 403

        username = data.get("username", "").strip()
        password = data.get("password", "")
        remember_me = data.get("remember_me", False)

        if not username or not password:
            return jsonify({"error": "Username and password are required"}), 400

        # Check if account is locked due to failed login attempts
        if is_account_locked(username, ip_address):
            if user_db is not None:
                db_user = user_db.get_user(username=username)
                user_db.record_auth_event(
                    user_id=int(db_user["id"]) if db_user is not None else None,
                    username=username,
                    auth_source=auth_mode,
                    event_type="login_failed",
                    outcome="blocked",
                    source_ip=ip_address,
                    user_agent=request.headers.get("User-Agent"),
                )
            lockout_until = _get_lockout_until(
                username,
                ip_address,
                repair_if_locked=True,
            )
            if lockout_until is None:
                logger.error("Locked account '%s' is missing a lockout timestamp", username)
                return jsonify(
                    {
                        "error": f"Account temporarily locked due to multiple failed login attempts. Try again in {LOCKOUT_DURATION_MINUTES} minutes."
                    }
                ), 429
            remaining_time = (lockout_until - datetime.now(UTC)).total_seconds() / 60
            logger.warning(
                "Login attempt blocked for locked account '%s' from IP %s", username, ip_address
            )
            return jsonify(
                {
                    "error": f"Account temporarily locked due to multiple failed login attempts. Try again in {int(remaining_time)} minutes."
                }
            ), 429

        # If no authentication is configured, authentication always succeeds
        if auth_mode == "none":
            session["user_id"] = username
            session.permanent = remember_me
            clear_failed_logins(username, ip_address)
            logger.info(
                "Login successful for user '%s' from IP %s (no auth configured)",
                username,
                ip_address,
            )
            return jsonify({"success": True})

        # Password authentication (builtin and OIDC modes)
        # OIDC mode also allows password login as a fallback so admins don't get locked out
        if auth_mode in ("builtin", "oidc"):
            if user_db is None:
                logger.error("User database not available for %s auth", auth_mode)
                return jsonify({"error": "Authentication service unavailable"}), 503
            try:
                db_user = user_db.get_user(username=username)

                if not db_user:
                    return _failed_login_response(username, ip_address)

                # Authenticate against DB user
                if db_user:
                    if not db_user.get("password_hash") or not check_password_hash(
                        db_user["password_hash"], password
                    ):
                        return _failed_login_response(username, ip_address)

                    is_admin = db_user["role"] == "admin"
                    session["user_id"] = username
                    session["db_user_id"] = db_user["id"]
                    session["is_admin"] = is_admin
                    session.permanent = remember_me
                    _start_audited_web_session(
                        user_id=int(db_user["id"]),
                        username=username,
                        auth_source=auth_mode,
                        source_ip=ip_address,
                    )
                    if not str(db_user["password_hash"]).startswith("scrypt:"):
                        user_db.update_user(
                            db_user["id"],
                            password_hash=generate_password_hash(password),
                        )
                        logger.info("Upgraded password hash for user '%s'", username)
                    clear_failed_logins(username, ip_address)
                    logger.info(
                        "Login successful for user '%s' from IP %s (%s auth, is_admin=%s, remember_me=%s)",
                        username,
                        ip_address,
                        auth_mode,
                        is_admin,
                        remember_me,
                    )
                    return jsonify({"success": True})

                return _failed_login_response(username, ip_address)

            except _OPERATIONAL_ERRORS as e:
                logger.error_trace(f"Built-in auth error: {e}")
                return jsonify({"error": "Authentication system error"}), 500

        # CWA database authentication mode
        if auth_mode == "cwa":
            # Verify database still exists (it was validated at startup)
            if not CWA_DB_PATH or not CWA_DB_PATH.exists():
                logger.error("CWA database at %s is no longer accessible", CWA_DB_PATH)
                return jsonify({"error": "Database configuration error"}), 500

            try:
                db_path = os.fspath(CWA_DB_PATH)
                db_uri = f"file:{db_path}?mode=ro&immutable=1"
                conn = sqlite3.connect(db_uri, uri=True)
                cur = conn.cursor()
                cur.execute("SELECT password, role, email FROM user WHERE name = ?", (username,))
                row = cur.fetchone()
                conn.close()

                # Check if user exists and password is correct
                if not row or not row[0] or not check_password_hash(row[0], password):
                    return _failed_login_response(username, ip_address)

                # Check if user has admin role (ROLE_ADMIN = 1, bit flag)
                user_role = row[1] if row[1] is not None else 0
                is_admin = (user_role & 1) == 1
                cwa_email = row[2] or None

                db_user_id = None
                if user_db is not None:
                    role = "admin" if is_admin else "user"
                    db_user, _ = upsert_cwa_user(
                        user_db,
                        cwa_username=username,
                        cwa_email=cwa_email,
                        role=role,
                        context="cwa_login",
                    )
                    db_user_id = db_user["id"]

                # Successful authentication - create session and clear failed attempts
                session["user_id"] = username
                session["is_admin"] = is_admin
                if db_user_id is not None:
                    session["db_user_id"] = db_user_id
                session.permanent = remember_me
                if db_user_id is not None:
                    _start_audited_web_session(
                        user_id=int(db_user_id),
                        username=username,
                        auth_source="cwa",
                        source_ip=ip_address,
                    )
                clear_failed_logins(username, ip_address)
                logger.info(
                    "Login successful for user '%s' from IP %s (CWA auth, is_admin=%s, remember_me=%s)",
                    username,
                    ip_address,
                    is_admin,
                    remember_me,
                )
                return jsonify({"success": True})

            except _OPERATIONAL_ERRORS as e:
                logger.error_trace(f"CWA database error during login: {e}")
                return jsonify({"error": "Authentication system error"}), 500

        # Should not reach here, but handle gracefully
        return jsonify({"error": "Unknown authentication mode"}), 500

    except _OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Login error: {e}")
        return jsonify({"error": "Login failed"}), 500


@app.route("/api/auth/logout", methods=["POST"])
def api_logout() -> Response | tuple[Response, int]:
    """Logout endpoint that clears the session.

    For proxy auth, returns the logout URL if configured.

    Returns:
        flask.Response: JSON with success status and optional logout_url.

    """
    try:
        auth_mode = get_auth_mode()
        ip_address = get_client_ip()
        username = session.get("user_id", "unknown")
        db_user_id = normalize_positive_int(session.get("db_user_id"))
        audit_session_key = session.get("audit_session_key")
        if user_db is not None and db_user_id is not None:
            persisted_user = user_db.get_user(user_id=db_user_id)
            persisted_user_id = db_user_id if persisted_user is not None else None
            if (
                persisted_user_id is not None
                and isinstance(audit_session_key, str)
                and audit_session_key
            ):
                user_db.revoke_web_session(
                    user_id=persisted_user_id,
                    session_key=audit_session_key,
                )
            user_db.record_auth_event(
                user_id=persisted_user_id,
                username=str(username),
                auth_source=auth_mode,
                event_type="logout",
                outcome="success",
                source_ip=ip_address,
                user_agent=request.headers.get("User-Agent"),
            )
        session.clear()
        logger.info("Logout successful for user '%s' from IP %s", username, ip_address)

        # For proxy auth, include logout URL if configured
        if auth_mode == "proxy":
            logout_url = app_config.get("PROXY_AUTH_LOGOUT_URL", "")
            if logout_url:
                return jsonify({"success": True, "logout_url": logout_url})

        return jsonify({"success": True})
    except _OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Logout error: {e}")
        return jsonify({"error": "Logout failed"}), 500


@app.route("/api/auth/check", methods=["GET"])
def api_auth_check() -> Response | tuple[Response, int]:
    """Check if user has a valid session.

    Returns:
        flask.Response: JSON with authentication status, whether auth is required,
        which auth mode is active, and whether user has admin privileges.

    """
    try:
        auth_mode = get_auth_mode()

        # If no authentication is configured, access is allowed (full admin)
        if auth_mode == "none":
            return jsonify(
                {
                    "authenticated": True,
                    "auth_required": False,
                    "auth_mode": "none",
                    "is_admin": True,
                    "library_capability": None,
                }
            )

        # Check if user has a valid session
        is_authenticated = "user_id" in session

        is_admin = get_auth_check_admin_status(auth_mode, {}, session)

        display_name = None
        library_capability = None
        if is_authenticated and session.get("db_user_id") and user_db is not None:
            try:
                db_user = user_db.get_user(user_id=session["db_user_id"])
                if db_user:
                    display_name = db_user.get("display_name") or None
                    library_capability = db_user["library_capability"]
            except (sqlite3.Error, TypeError, ValueError) as exc:
                logger.debug("Could not load display name for session user: %s", exc)

        response_data = {
            "authenticated": is_authenticated,
            "auth_required": True,
            "auth_mode": auth_mode,
            "is_admin": is_admin if is_authenticated else False,
            "username": session.get("user_id") if is_authenticated else None,
            "display_name": display_name,
            "library_capability": library_capability,
        }

        # Add logout URL for proxy auth if configured
        if auth_mode == "proxy" and app_config.get("PROXY_AUTH_USER_HEADER", ""):
            logout_url = app_config.get("PROXY_AUTH_LOGOUT_URL", "")
            if logout_url:
                response_data["logout_url"] = logout_url

        if auth_mode in ("builtin", "oidc") and DISABLE_LOCAL_AUTH:
            response_data["hide_local_auth"] = True

        # Add custom OIDC button label and SSO enforcement flags if configured
        if auth_mode == "oidc":
            oidc_button_label = app_config.get("OIDC_BUTTON_LABEL", "")
            if oidc_button_label:
                response_data["oidc_button_label"] = oidc_button_label
            if HIDE_LOCAL_AUTH:
                response_data["hide_local_auth"] = True
            if OIDC_AUTO_REDIRECT:
                response_data["oidc_auto_redirect"] = True

        return jsonify(response_data)
    except _OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Auth check error: {e}")
        return jsonify(
            {
                "authenticated": False,
                "auth_required": True,
                "auth_mode": "unknown",
                "is_admin": False,
                "library_capability": None,
            }
        )


@app.route("/api/metadata/providers", methods=["GET"])
@login_required
def api_metadata_providers() -> Response | tuple[Response, int]:
    """Get list of available metadata providers.

    Returns:
        flask.Response: JSON with list of providers and their status.

    """
    try:
        from bookflow.metadata_providers import (
            get_configured_provider_name,
            get_provider,
            get_provider_kwargs,
            list_providers,
        )

        app_config.refresh()
        db_user_id = get_session_db_user_id(session)

        configured_metadata_provider = get_configured_provider_name(
            content_type="ebook",
            user_id=db_user_id,
            fallback_to_main=True,
        )
        configured_combined_metadata_provider = get_configured_provider_name(
            content_type="combined",
            user_id=db_user_id,
            fallback_to_main=False,
        )
        providers = []
        for info in list_providers():
            enabled_key = f"{info['name'].upper()}_ENABLED"
            provider_info = {
                "name": info["name"],
                "display_name": info["display_name"],
                "requires_auth": info["requires_auth"],
                "enabled": app_config.get(enabled_key, False) is True,
                "available": False,
            }

            try:
                kwargs = get_provider_kwargs(info["name"])
                provider = get_provider(info["name"], **kwargs)
                provider_info["available"] = provider.is_available()
            except _OPERATIONAL_ERRORS as exc:
                logger.debug(
                    "Metadata provider %s availability check failed: %s",
                    info["name"],
                    exc,
                )

            providers.append(provider_info)

        return jsonify(
            {
                "providers": providers,
                "configured_provider": configured_metadata_provider or None,
                "configured_provider_combined": configured_combined_metadata_provider or None,
            }
        )
    except _IMPORT_OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Metadata providers error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/metadata/config", methods=["GET"])
@login_required
def api_metadata_config() -> Response | tuple[Response, int]:
    """Return provider-specific metadata search config for the active session."""
    try:
        from bookflow.metadata_providers import (
            get_configured_provider_name,
            get_provider,
            get_provider_capabilities,
            get_provider_default_sort,
            get_provider_kwargs,
            get_provider_search_fields,
            get_provider_sort_options,
            is_provider_registered,
        )

        app_config.refresh()
        content_type = request.args.get("content_type", "ebook").strip()
        if content_type.casefold() != "ebook":
            return jsonify({"error": "Audiobooks are no longer supported"}), 400
        provider_name = request.args.get("provider", "").strip()

        db_user_id = get_session_db_user_id(session)

        if not provider_name:
            provider_name = get_configured_provider_name(
                content_type=content_type,
                user_id=db_user_id,
                fallback_to_main=True,
            )

        if not provider_name:
            return jsonify(
                {
                    "provider": None,
                    "display_name": None,
                    "enabled": False,
                    "available": False,
                    "search_fields": [],
                    "capabilities": [],
                    "sort_options": [{"value": "relevance", "label": "Most relevant"}],
                    "default_sort": "relevance",
                }
            )

        if not is_provider_registered(provider_name):
            return jsonify({"error": f"Unknown metadata provider: {provider_name}"}), 400

        kwargs = get_provider_kwargs(provider_name)
        provider = get_provider(provider_name, **kwargs)
        enabled_key = f"{provider_name.upper()}_ENABLED"
        provider_enabled = app_config.get(enabled_key, False) is True
        provider_available = provider.is_available()

        return jsonify(
            {
                "provider": provider_name,
                "display_name": provider.display_name,
                "enabled": provider_enabled,
                "available": provider_available,
                "search_fields": get_provider_search_fields(provider_name),
                "capabilities": get_provider_capabilities(provider_name),
                "sort_options": get_provider_sort_options(provider_name),
                "default_sort": get_provider_default_sort(provider_name, user_id=db_user_id),
            }
        )
    except _IMPORT_OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Metadata config error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/metadata/search", methods=["GET"])
@login_required
def api_metadata_search() -> Response | tuple[Response, int]:
    """Search for books using the configured metadata provider.

    Query Parameters:
        query (str): Search query (required)
        limit (int): Maximum number of results (default: 40, max: 100)
        sort (str): Sort order - relevance, popularity, rating, newest, oldest (default: relevance)
        [dynamic fields]: Provider-specific search fields passed as query params

    Returns:
        flask.Response: JSON with list of books from metadata provider.

    """
    try:
        from dataclasses import asdict

        from bookflow.metadata_providers import (
            CheckboxSearchField,
            MetadataSearchOptions,
            NumberSearchField,
            SortOrder,
            get_configured_provider,
            get_provider,
            get_provider_kwargs,
            is_provider_enabled,
            is_provider_registered,
        )

        query = request.args.get("query", "").strip()
        content_type = request.args.get("content_type", "ebook").strip()
        if content_type.casefold() != "ebook":
            return jsonify({"error": "Audiobooks are no longer supported"}), 400
        provider_name = request.args.get("provider", "").strip()

        try:
            limit = min(int(request.args.get("limit", 40)), 100)
        except ValueError:
            limit = 40

        try:
            page = max(1, int(request.args.get("page", 1)))
        except ValueError:
            page = 1

        # Parse sort parameter
        sort_value = request.args.get("sort", "relevance").lower()
        try:
            sort_order = SortOrder(sort_value)
        except ValueError:
            sort_order = SortOrder.RELEVANCE

        db_user_id = get_session_db_user_id(session)

        if provider_name:
            if not is_provider_registered(provider_name):
                return jsonify(
                    {
                        "error": f"Unknown metadata provider: {provider_name}",
                        "message": f"Unknown metadata provider: {provider_name}",
                    }
                ), 400
            if not is_provider_enabled(provider_name):
                return jsonify(
                    {
                        "error": f"Metadata provider '{provider_name}' is not enabled",
                        "message": f"{provider_name} is not enabled. Enable it in Settings first.",
                    }
                ), 503

            kwargs = get_provider_kwargs(provider_name)
            provider = get_provider(provider_name, **kwargs)
        else:
            provider = get_configured_provider(content_type=content_type, user_id=db_user_id)

        if not provider:
            return jsonify(
                {
                    "error": "No metadata provider configured",
                    "message": "No metadata provider configured. Enable one in Settings.",
                }
            ), 503

        if not provider.is_available():
            return jsonify(
                {
                    "error": f"Metadata provider '{provider.name}' is not available",
                    "message": f"{provider.display_name} is not available. Check configuration in Settings.",
                }
            ), 503

        # Extract custom search field values from query params
        fields: dict[str, Any] = {}
        for search_field in provider.search_fields:
            value = request.args.get(search_field.key)
            if value is not None:
                # Strip string values to handle whitespace-only input
                value = value.strip()
                if value != "":
                    # Parse value based on field type
                    if isinstance(search_field, CheckboxSearchField):
                        fields[search_field.key] = value.lower() in ("true", "1", "yes", "on")
                    elif isinstance(search_field, NumberSearchField):
                        with suppress(ValueError):
                            fields[search_field.key] = int(value)
                    else:
                        fields[search_field.key] = value

        # Require either a query or at least one field value
        if not query and not fields:
            return jsonify({"error": "Either 'query' or search field values are required"}), 400

        options = MetadataSearchOptions(
            query=query, limit=limit, page=page, sort=sort_order, fields=fields
        )
        search_result = provider.search_paginated(options)

        # Convert BookMetadata objects to dicts
        books_data = [asdict(book) for book in search_result.books]
        if library_service is not None:
            metadata_states = library_service.get_metadata_library_states(
                book_keys=[
                    (str(book["provider"]), str(book["provider_id"]))
                    for book in books_data
                    if book.get("provider") and book.get("provider_id")
                ],
                user_id=db_user_id,
            )
            for book in books_data:
                state = metadata_states.get(
                    (str(book.get("provider", "")), str(book.get("provider_id", "")))
                )
                if state is not None:
                    book.update(state)

        # Transform cover_url to local proxy URLs when caching is enabled
        from bookflow.core.utils import transform_cover_url

        for book_dict in books_data:
            if book_dict.get("cover_url"):
                cache_id = f"{book_dict['provider']}_{book_dict['provider_id']}"
                book_dict["cover_url"] = transform_cover_url(book_dict["cover_url"], cache_id)

        response_data = {
            "books": books_data,
            "provider": provider.name,
            "query": query,
            "page": search_result.page,
            "total_found": search_result.total_found,
            "has_more": search_result.has_more,
        }
        if search_result.source_url:
            response_data["source_url"] = search_result.source_url
        if search_result.source_title:
            response_data["source_title"] = search_result.source_title
        return jsonify(response_data)
    except _IMPORT_OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Metadata search error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/metadata/field-options", methods=["GET"])
@login_required
def api_metadata_field_options() -> Response | tuple[Response, int]:
    """Return dynamic search-field options for a metadata provider."""
    try:
        from bookflow.metadata_providers import (
            get_configured_provider,
            get_provider,
            get_provider_kwargs,
            is_provider_registered,
        )

        field_key = request.args.get("field", "").strip()
        provider_name = request.args.get("provider", "").strip()
        content_type = request.args.get("content_type", "ebook").strip()
        if content_type.casefold() != "ebook":
            return jsonify({"error": "Audiobooks are no longer supported"}), 400
        query_text = request.args.get("query", "").strip()

        if not field_key:
            return jsonify({"options": []})

        db_user_id = get_session_db_user_id(session)

        provider = None
        if provider_name:
            if not is_provider_registered(provider_name):
                return jsonify({"options": []})
            kwargs = get_provider_kwargs(provider_name)
            provider = get_provider(provider_name, **kwargs)
        else:
            provider = get_configured_provider(content_type=content_type, user_id=db_user_id)

        if not provider or not provider.is_available():
            return jsonify({"options": []})

        options = provider.get_search_field_options(field_key, query=query_text or None)
        return jsonify({"options": options})
    except _OPERATIONAL_ERRORS as e:
        logger.warning("Metadata field options endpoint error: %s", e)
        return jsonify({"options": []})


def _resolve_metadata_provider(provider_name: str) -> MetadataProvider:
    """Validate, instantiate and return a ready metadata provider.

    Raises appropriate HTTP-friendly exceptions on failure.
    """
    from bookflow.metadata_providers import (
        get_provider,
        get_provider_kwargs,
        is_provider_registered,
    )

    if not is_provider_registered(provider_name):
        msg = f"Unknown metadata provider: {provider_name}"
        raise ValueError(msg)

    kwargs = get_provider_kwargs(provider_name)
    prov = get_provider(provider_name, **kwargs)

    if not prov.is_available():
        msg = f"Provider '{provider_name}' is not available"
        raise RuntimeError(msg)

    return prov


@app.route("/api/metadata/book/<provider>/<book_id>", methods=["GET"])
@login_required
def api_metadata_book(provider: str, book_id: str) -> Response | tuple[Response, int]:
    """Get detailed book information from a metadata provider.

    Path Parameters:
        provider (str): Provider name (e.g., "hardcover", "openlibrary")
        book_id (str): Book ID in the provider's system

    Returns:
        flask.Response: JSON with book details.

    """
    try:
        from dataclasses import asdict

        prov = _resolve_metadata_provider(provider)

        book = prov.get_book(book_id)
        if not book:
            return jsonify({"error": "Book not found"}), 404

        book_dict = asdict(book)

        # Transform cover_url to local proxy URL when caching is enabled
        from bookflow.core.utils import transform_cover_url

        if book_dict.get("cover_url"):
            cache_id = f"{provider}_{book_id}"
            book_dict["cover_url"] = transform_cover_url(book_dict["cover_url"], cache_id)

        return jsonify(book_dict)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 503
    except (OSError, TypeError, sqlite3.Error) as e:
        logger.error_trace(f"Metadata book error: {e}")
        return jsonify({"error": str(e)}), 500


def _handle_target_errors(
    fallback_message: str,
) -> Callable[
    [Callable[..., Response | tuple[Response, int]]], Callable[..., Response | tuple[Response, int]]
]:
    """Wrap a metadata-target route with standard error handling."""

    def decorator(
        fn: Callable[..., Response | tuple[Response, int]],
    ) -> Callable[..., Response | tuple[Response, int]]:
        @wraps(fn)
        def wrapper(*args: object, **kwargs: object) -> Response | tuple[Response, int]:
            try:
                return fn(*args, **kwargs)
            except (NotImplementedError, ValueError) as e:
                return jsonify({"error": str(e)}), 400
            except RuntimeError as e:
                return jsonify({"error": str(e)}), 502
            except (OSError, TypeError, sqlite3.Error) as e:
                logger.error_trace(f"{fallback_message}: {e}")
                return jsonify({"error": fallback_message}), 500

        return wrapper

    return decorator


@app.route("/api/metadata/book/<provider>/<book_id>/targets", methods=["GET"])
@login_required
@_handle_target_errors("Failed to load book targets")
def api_metadata_book_targets(provider: str, book_id: str) -> Response | tuple[Response, int]:
    """Get provider-managed list/status targets for a specific book."""
    prov = _resolve_metadata_provider(provider)
    return jsonify({"options": prov.get_book_targets(book_id)})


@app.route("/api/metadata/book/<provider>/targets/batch", methods=["POST"])
@login_required
@_handle_target_errors("Failed to load book targets")
def api_metadata_book_targets_batch(provider: str) -> Response | tuple[Response, int]:
    """Get provider-managed list/status targets for multiple books."""
    prov = _resolve_metadata_provider(provider)

    payload = request.get_json(silent=True) or {}
    raw_ids = payload.get("book_ids", []) if isinstance(payload, dict) else []
    if not isinstance(raw_ids, list) or not raw_ids:
        return jsonify({"error": "book_ids must be a non-empty array"}), 400

    book_ids = [str(bid) for bid in raw_ids[:50]]

    return jsonify({"results": prov.get_book_targets_batch(book_ids)})


@app.route("/api/metadata/book/<provider>/<book_id>/targets", methods=["PUT"])
@login_required
@_handle_target_errors("Failed to update book targets")
def api_metadata_book_targets_update(
    provider: str, book_id: str
) -> Response | tuple[Response, int]:
    """Set whether a book belongs to a provider-managed list or shelf."""
    prov = _resolve_metadata_provider(provider)

    payload = request.get_json(silent=True) or {}
    target = str(payload.get("target", "")).strip() if isinstance(payload, dict) else ""
    selected = payload.get("selected") if isinstance(payload, dict) else None
    if not target:
        return jsonify({"error": "target is required"}), 400
    if not isinstance(selected, bool):
        return jsonify({"error": "selected must be a boolean"}), 400

    result = prov.set_book_target_state(book_id, target, selected=selected)
    response: dict = {
        "success": True,
        "changed": bool(result.get("changed", True)),
        "selected": selected,
    }
    deselected = result.get("deselected_target")
    if isinstance(deselected, str) and deselected:
        response["deselected_target"] = deselected
    return jsonify(response)


@app.route("/api/releases", methods=["GET"])
@login_required
def api_releases() -> Response | tuple[Response, int]:
    """Search for downloadable releases of a book.

    This endpoint resolves the persisted library Book and searches available release sources.

    Query Parameters:
        library_book_id (int): Canonical library Book ID (required)
        source (str): Release source to search (optional, default: all)

    Returns:
        flask.Response: JSON with list of available releases.

    """
    try:
        from dataclasses import asdict

        from bookflow.core.search_plan import build_release_search_plan
        from bookflow.metadata_providers import (
            BookMetadata,
        )
        from bookflow.release_sources import (
            get_source,
            list_available_sources,
            serialize_column_config,
        )

        def _search_source_releases(
            source_name: str, search_book: BookMetadata
        ) -> tuple[Any | None, list[Any], str | None]:
            """Search one source and return any error message instead of raising."""
            try:
                source = get_source(source_name)

                plan = build_release_search_plan(
                    search_book,
                    languages=languages,
                    manual_query=manual_query,
                    indexers=indexers,
                )

                if plan.manual_query:
                    planned_query = plan.manual_query
                    planned_query_type = "manual"
                elif not expand_search and plan.isbn_candidates:
                    planned_query = plan.isbn_candidates[0]
                    planned_query_type = "isbn"
                else:
                    planned_query = plan.primary_query
                    planned_query_type = "title_author"

                logger.debug(
                    "Searching %s: %s='%s' (title='%s', authors=%s, expand=%s, content_type=%s)",
                    source_name,
                    planned_query_type,
                    planned_query,
                    search_book.title,
                    search_book.authors,
                    expand_search,
                    content_type,
                )

                releases = source.search(
                    search_book, plan, expand_search=expand_search, content_type=content_type
                )
            except ValueError:
                return None, [], f"Unknown source: {source_name}"
            except (SourceUnavailableError, *_OPERATIONAL_ERRORS) as e:
                logger.warning("Release search failed for source %s: %s", source_name, e)
                return None, [], f"{source_name}: {e!s}"
            else:
                return source, releases, None

        capability_gate = _require_download_capability()
        if capability_gate is not None:
            return capability_gate

        library_book_id = normalize_positive_int(request.args.get("library_book_id"))
        if library_book_id is None:
            return jsonify({"error": "library_book_id is required"}), 400
        allowed_params = {
            "library_book_id",
            "source",
            "expand_search",
            "languages",
            "content_type",
            "manual_query",
            "indexers",
        }
        if set(request.args) - allowed_params:
            return jsonify({"error": "Legacy release search parameters are not supported"}), 400
        if library_service is None:
            return jsonify({"error": "Library is unavailable"}), 503

        actor_user_id = get_session_db_user_id(session)
        is_admin = bool(session.get("is_admin", False))
        if not is_admin and (
            actor_user_id is None
            or not library_service.is_in_library(user_id=actor_user_id, book_id=library_book_id)
        ):
            return jsonify({"error": "Book is not in your library"}), 403

        library_book = library_service.get_book(library_book_id)
        if library_book is None:
            return jsonify({"error": "Book not found"}), 404

        # Release searches use only the canonical Book already tracked by the library.
        # Provider, source-browse, and query parameters are never an alternate entry point.
        provider = str(library_book["metadata_provider"])
        book_id = str(library_book["provider_book_id"])
        source_filter = request.args.get("source", "").strip()
        expand_search = request.args.get("expand_search", "").lower() == "true"
        # Accept language codes for filtering (comma-separated)
        languages_param = request.args.get("languages", "").strip()
        languages = (
            [lang.strip() for lang in languages_param.split(",") if lang.strip()]
            if languages_param
            else None
        )
        content_type = request.args.get("content_type", "ebook").strip()
        if content_type.casefold() != "ebook":
            return jsonify({"error": "Audiobooks are no longer supported"}), 400

        manual_query = request.args.get("manual_query", "").strip()
        if manual_query and not is_admin:
            return jsonify({"error": "Manual release queries require administrator access"}), 403

        # Accept indexer names for Prowlarr filtering (comma-separated)
        indexers_param = request.args.get("indexers", "").strip()
        indexers = (
            [idx.strip() for idx in indexers_param.split(",") if idx.strip()]
            if indexers_param
            else None
        )
        book = BookMetadata(
            provider=provider,
            provider_id=book_id,
            title=str(library_book["title"]),
            authors=[str(library_book["author"])] if library_book.get("author") else [],
            isbn_13=library_book.get("isbn_13"),
            cover_url=library_book.get("cover_url"),
            publish_year=library_book.get("publish_year"),
            language=library_book.get("language"),
            series_name=library_book.get("series_name"),
            series_position=library_book.get("series_position"),
            search_title=str(library_book["title"]),
            search_author=library_book.get("author"),
        )
        # Determine which release sources to search
        if source_filter:
            sources_to_search = [source_filter]
        else:
            # Search only enabled sources
            sources_to_search = [src["name"] for src in list_available_sources() if src["enabled"]]

        # Search each source for releases
        all_releases = []
        errors = []
        source_instances = {}  # Keep source instances for column config

        for source_name in sources_to_search:
            source, releases, error = _search_source_releases(source_name, book)
            if source is not None:
                source_instances[source_name] = source
                all_releases.extend(releases)
            if error is not None:
                errors.append(error)

        # Convert Release objects to dicts
        releases_data = [_serialize_release(release) for release in all_releases]
        if library_service is not None:
            task_ids = [backend.derive_release_task_id(release) for release in releases_data]
            states = library_service.get_release_library_states(
                task_ids=task_ids, user_id=get_session_db_user_id(session)
            )
            for release, task_id in zip(releases_data, task_ids, strict=True):
                release.update(states.get(task_id, {}))

        # Get column config from the first source searched
        # Reuse the same instance to get any dynamic data (e.g., online_servers for IRC)
        column_config = None
        if sources_to_search and sources_to_search[0] in source_instances:
            try:
                first_source = source_instances[sources_to_search[0]]
                column_config = serialize_column_config(first_source.get_column_config())
            except _OPERATIONAL_ERRORS as e:
                logger.warning("Failed to get column config: %s", e)

        # Convert book to dict and transform cover_url
        book_dict = asdict(book)
        from bookflow.core.utils import transform_cover_url

        if book_dict.get("cover_url"):
            cache_id = f"{provider}_{book_id}"
            book_dict["cover_url"] = transform_cover_url(book_dict["cover_url"], cache_id)

        search_info = {}
        for source_name, source_instance in source_instances.items():
            if hasattr(source_instance, "last_search_type") and source_instance.last_search_type:
                search_info[source_name] = {"search_type": source_instance.last_search_type}

        response = {
            "releases": releases_data,
            "book": book_dict,
            "sources_searched": sources_to_search,
            "column_config": column_config,
            "search_info": search_info,
        }

        if errors:
            response["errors"] = errors

        # If no releases found and there were errors, return 503 with the first
        # source failure message for the release modal.
        if not releases_data and errors:
            # Use the first error message (typically the most relevant)
            error_message = errors[0]
            # Strip the source prefix if present (e.g., "direct_download: message" -> "message")
            if ": " in error_message:
                error_message = error_message.split(": ", 1)[1]
            return jsonify({"error": error_message}), 503

        return jsonify(response)
    except SourceUnavailableError as e:
        logger.warning("Release search unavailable: %s", e)
        return jsonify({"error": str(e)}), 503
    except _IMPORT_OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Releases search error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/release-sources", methods=["GET"])
@login_required
def api_release_sources() -> Response | tuple[Response, int]:
    """Get available release sources from the plugin registry.

    Returns:
        flask.Response: JSON list of available release sources.

    """
    try:
        from bookflow.release_sources import list_available_sources

        sources = list_available_sources()
        return jsonify(sources)
    except _IMPORT_OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Release sources error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings", methods=["GET"])
@login_required
def api_settings_get_all() -> Response | tuple[Response, int]:
    """Get all settings tabs with their fields and current values.

    Returns:
        flask.Response: JSON with all settings tabs.

    """
    try:
        import_module("bookflow.config.notifications_settings")
        import_module("bookflow.config.security")

        # Ensure settings are registered by importing settings modules
        # This triggers the @register_settings decorators
        import_module("bookflow.config.settings")
        import_module("bookflow.config.users_settings")
        from bookflow.core.settings_registry import serialize_all_settings

        data = serialize_all_settings(include_values=True)
        return jsonify(data)
    except _IMPORT_OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Settings get error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/<tab_name>", methods=["GET"])
@login_required
def api_settings_get_tab(tab_name: str) -> Response | tuple[Response, int]:
    """Get settings for a specific tab.

    Path Parameters:
        tab_name (str): Settings tab name (e.g., "general", "hardcover")

    Returns:
        flask.Response: JSON with tab settings and values.

    """
    try:
        import_module("bookflow.config.notifications_settings")
        import_module("bookflow.config.security")

        # Ensure settings are registered
        import_module("bookflow.config.settings")
        import_module("bookflow.config.users_settings")
        from bookflow.core.settings_registry import (
            get_settings_tab,
            serialize_tab,
        )

        tab = get_settings_tab(tab_name)
        if not tab:
            return jsonify({"error": f"Unknown settings tab: {tab_name}"}), 404

        return jsonify(serialize_tab(tab, include_values=True))
    except _IMPORT_OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Settings get tab error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/<tab_name>", methods=["PUT"])
@login_required
def api_settings_update_tab(tab_name: str) -> Response | tuple[Response, int]:
    """Update settings for a specific tab.

    Path Parameters:
        tab_name (str): Settings tab name

    Request Body:
        JSON object with setting keys and values to update.

    Returns:
        flask.Response: JSON with update result.

    """
    try:
        import_module("bookflow.config.notifications_settings")
        import_module("bookflow.config.security")

        # Ensure settings are registered
        import_module("bookflow.config.settings")
        import_module("bookflow.config.users_settings")
        from bookflow.core.settings_registry import (
            get_settings_tab,
            update_settings,
        )

        tab = get_settings_tab(tab_name)
        if not tab:
            return jsonify({"error": f"Unknown settings tab: {tab_name}"}), 404

        values = request.get_json(silent=True)
        if values is None or not isinstance(values, dict):
            return jsonify({"error": "Request body must be a JSON object"}), 400

        # If no values to update, return success with empty updated list
        if not values:
            return jsonify({"success": True, "message": "No changes to save", "updated": []})

        result = update_settings(tab_name, values)

        if result["success"]:
            return jsonify(result)
        return jsonify(result), 400
    except _IMPORT_OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Settings update error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/<tab_name>/action/<action_key>", methods=["POST"])
@login_required
def api_settings_execute_action(tab_name: str, action_key: str) -> Response | tuple[Response, int]:
    """Execute a settings action (e.g., test connection).

    Path Parameters:
        tab_name (str): Settings tab name
        action_key (str): Action key to execute

    Request Body (optional):
        JSON object with current form values (unsaved)

    Returns:
        flask.Response: JSON with action result.

    """
    try:
        import_module("bookflow.config.notifications_settings")
        import_module("bookflow.config.security")

        # Ensure settings are registered
        import_module("bookflow.config.settings")
        import_module("bookflow.config.users_settings")
        from bookflow.core.settings_registry import execute_action

        # Get current form values if provided (for testing with unsaved values)
        current_values = request.get_json(silent=True) or {}

        result = execute_action(tab_name, action_key, current_values)

        if result["success"]:
            return jsonify(result)
        return jsonify(result), 400
    except _IMPORT_OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Settings action error: {e}")
        return jsonify({"error": str(e)}), 500


# =============================================================================
# Onboarding API
# =============================================================================


@app.route("/api/onboarding", methods=["GET"])
@login_required
def api_onboarding_get() -> Response | tuple[Response, int]:
    """Get onboarding configuration including steps, fields, and current values.

    Returns:
        flask.Response: JSON with onboarding steps and values.

    """
    try:
        # Ensure settings are registered
        import_module("bookflow.config.settings")
        from bookflow.core.onboarding import get_onboarding_config

        config = get_onboarding_config()
        return jsonify(config)
    except _IMPORT_OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Onboarding get error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/onboarding", methods=["POST"])
@login_required
def api_onboarding_save() -> Response | tuple[Response, int]:
    """Save onboarding settings and mark as complete.

    Request Body:
        JSON object with all onboarding field values

    Returns:
        flask.Response: JSON with success/error status.

    """
    try:
        # Ensure settings are registered
        import_module("bookflow.config.settings")
        from bookflow.core.onboarding import save_onboarding_settings

        data = request.get_json(silent=True)
        if not data:
            return jsonify({"success": False, "message": "No data provided"}), 400

        result = save_onboarding_settings(data)

        if result["success"]:
            return jsonify(result)
        return jsonify(result), 400
    except _IMPORT_OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Onboarding save error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/onboarding/skip", methods=["POST"])
@login_required
def api_onboarding_skip() -> Response | tuple[Response, int]:
    """Skip onboarding and mark as complete without saving any settings.

    Returns:
        flask.Response: JSON with success status.

    """
    try:
        from bookflow.core.onboarding import mark_onboarding_complete

        mark_onboarding_complete()
        return jsonify({"success": True, "message": "Onboarding skipped"})
    except _IMPORT_OPERATIONAL_ERRORS as e:
        logger.error_trace(f"Onboarding skip error: {e}")
        return jsonify({"error": str(e)}), 500


# Catch-all route for React Router (must be last)
# This handles client-side routing by serving index.html for any unmatched routes
@app.route("/<path:path>")
def catch_all(path: str) -> Response | tuple[Response, int]:
    """Serve the React app for any route not matched by API endpoints.

    This allows React Router to handle client-side routing.
    Authentication is handled by the React app itself.
    """
    # If the request is for an API endpoint or static file, let it 404
    if path.startswith(("api/", "assets/")):
        return jsonify({"error": "Resource not found"}), 404
    # Otherwise serve the React app
    return _serve_index_html()


def _get_request_sid() -> str | None:
    """Return the Socket.IO session id for the active request when available."""
    sid = getattr(request, "sid", None)
    return sid if isinstance(sid, str) and sid else None


# WebSocket event handlers
@socketio.on("connect")
def handle_connect() -> bool | None:
    """Handle client connection."""
    auth_mode = get_auth_mode()
    if auth_mode == AUTH_MODE_UNAVAILABLE:
        logger.warning("Rejected WebSocket connection while authentication is unavailable")
        return False
    if auth_mode != "none" and "user_id" not in session:
        logger.warning("Rejected unauthenticated WebSocket connection")
        return False

    is_admin, db_user_id, can_access_status = _resolve_status_scope()
    if not can_access_status:
        logger.warning("Rejected WebSocket connection without an authorized status scope")
        return False

    logger.info("WebSocket client connected")

    # Track the connection (triggers warmup callbacks on first connect)
    ws_manager.client_connected()

    # Join appropriate room based on authenticated user session
    sid = _get_request_sid()
    if sid is None:
        logger.warning("Socket.IO connect event missing sid")
        return None
    ws_manager.join_user_room(sid, is_admin=is_admin, db_user_id=db_user_id)

    # Send initial status to the newly connected client (filtered)
    try:
        user_id = None if is_admin else db_user_id
        status = backend.queue_status(user_id=user_id)
        emit("status_update", status)
    except _OPERATIONAL_ERRORS:
        logger.exception("Error sending initial status")


@socketio.on("disconnect")
def handle_disconnect() -> None:
    """Handle client disconnection."""
    logger.info("WebSocket client disconnected")

    # Leave room
    sid = _get_request_sid()
    if sid is not None:
        ws_manager.leave_user_room(sid)

    # Track the disconnection
    ws_manager.client_disconnected()


@socketio.on("request_status")
def handle_status_request() -> None:
    """Handle manual status request from client."""
    try:
        is_admin, db_user_id, can_access_status = _resolve_status_scope()
        sid = _get_request_sid()
        if sid is None:
            logger.warning("Socket.IO request_status event missing sid")
            emit("status_update", {})
            return
        ws_manager.sync_user_room(sid, is_admin=is_admin, db_user_id=db_user_id)

        if not can_access_status:
            emit("status_update", {})
            return

        user_id = None if is_admin else db_user_id
        status = backend.queue_status(user_id=user_id)
        emit("status_update", status)
    except _OPERATIONAL_ERRORS:
        logger.exception("Error handling status request")
        emit("error", {"message": "Failed to get status"})


logger.log_resource_usage()

# Warn if config directory is not writable (settings won't persist)
if not _is_config_dir_writable():
    logger.warning(
        "Config directory %s is not writable. Settings will not persist. Mount a config volume to enable settings persistence (see docs for details).",
        CONFIG_DIR,
    )

if __name__ == "__main__":
    debug_enabled = _is_debug_enabled()
    logger.info(
        "Starting Flask application with WebSocket support on %s:%s (debug=%s)",
        FLASK_HOST,
        FLASK_PORT,
        debug_enabled,
    )
    socketio.run(
        app,
        host=FLASK_HOST,
        port=FLASK_PORT,
        debug=debug_enabled,
        allow_unsafe_werkzeug=True,  # For development only
    )
