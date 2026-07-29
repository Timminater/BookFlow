"""Production composition for durable request_process jobs.

This module is deliberately the only bridge between the new persisted
request_process domain and the pre-existing release/download coordinator.  The
domain services stay synchronous and testable; this wrapper supplies the
release-source and coordinator adapters plus a small, stoppable worker.
"""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bookflow.core.attempt_policy import DEFAULT_RETRY_DELAYS, FailureCode, FailureDisposition
from bookflow.core.attempt_service import AttemptService, DownloadAdapter
from bookflow.core.catalog_service import CatalogService
from bookflow.core.catalog_storage_lease import (
    CatalogStorageLeaseError,
)
from bookflow.core.durable_jobs import DurableJobService
from bookflow.core.file_store import FileStore
from bookflow.core.release_candidates import ReleaseCandidate, ReleaseProfile
from bookflow.core.release_profile_service import release_profile_from_snapshot
from bookflow.core.request_scheduler import RequestScheduler
from bookflow.core.search_plan import build_release_search_plan
from bookflow.core.search_service import SearchService
from bookflow.core.web_push_events import enqueue_web_push
from bookflow.metadata_providers import BookMetadata
from bookflow.release_sources import Release, get_source, list_available_sources

QueueRelease = Callable[..., tuple[bool, str | None]]
KoboFulfilment = Callable[[int, int, int], None]


def encode_source_reference(source: str, source_id: str) -> str:
    """Encode a source-native identifier without storing a download URL.

    The value is opaque to API consumers, URL-safe, and constrained to the
    character set accepted by ``SearchService``.  It is not a credential or a
    signed authorization token; the selected source remains authoritative when
    it resolves the identifier during download.
    """
    if not source or not source_id:
        raise ValueError("source and source_id are required")
    encoded = base64.urlsafe_b64encode(source_id.encode("utf-8")).rstrip(b"=").decode("ascii")
    reference = f"source:{source}:{encoded}"
    if len(reference) > 256:
        raise ValueError("source_id reference is too long")
    return reference


def decode_source_reference(reference: object, *, expected_source: str) -> str:
    """Decode and validate one opaque source-id reference."""
    if not isinstance(reference, str):
        raise TypeError("candidate has no source reference")
    prefix = f"source:{expected_source}:"
    if not reference.startswith(prefix):
        raise ValueError("candidate source reference does not match its source")
    encoded = reference.removeprefix(prefix)
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("invalid source reference") from exc


class LegacyQueueAdapter(DownloadAdapter):
    """Submit an accepted persisted candidate through ``backend.queue_release``."""

    def __init__(self, database_path: str, queue_release: QueueRelease) -> None:
        self._database_path = database_path
        self._queue_release = queue_release

    def add(self, candidate: Mapping[str, Any], *, idempotency_key: str) -> str:
        source = str(candidate["source"])
        source_id = decode_source_reference(
            candidate.get("payload_reference"), expected_source=source
        )
        context = self._load_context(int(candidate["request_process_id"]))
        if str(context["content_type"]).casefold() != "ebook":
            raise ValueError("Audiobooks are no longer supported")
        payload: dict[str, Any] = {
            "source": source,
            "source_id": source_id,
            "title": candidate["title"],
            "author": candidate.get("author"),
            "format": candidate.get("file_format"),
            "content_type": context["content_type"],
            "library_book_id": context["book_id"],
            "_request_id": context["request_id"],
            # Kept for audit/idempotency; the legacy coordinator ignores unknown keys.
            "request_process_idempotency_key": idempotency_key,
        }
        success, error = self._queue_release(
            payload,
            0,
            user_id=context["user_id"],
            username=context["username"],
        )
        if not success:
            raise ConnectionError(error or "legacy download coordinator rejected candidate")
        return f"{source}:{source_id}"

    def _load_context(self, request_process_id: int) -> sqlite3.Row:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute(
                """
                SELECT a.book_id, a.content_type, r.request_id, r.user_id, u.username
                FROM request_processes a
                LEFT JOIN request_process_recipients r ON r.request_process_id = a.id
                LEFT JOIN users u ON u.id = r.user_id
                WHERE a.id = ?
                ORDER BY r.created_at, r.request_id
                LIMIT 1
                """,
                (request_process_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            raise ValueError("Request not found")
        return row


class RequestRuntime:
    """Run durable request_process work and reconcile coordinator terminal events."""

    def __init__(
        self,
        database_path: str,
        *,
        queue_release: QueueRelease,
        library_root: Path,
        worker_id: str | None = None,
        poll_interval: float = 2.0,
        kobo_fulfilment: KoboFulfilment | None = None,
    ) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        self._database_path = database_path
        self._queue_release = queue_release
        self._jobs = DurableJobService(database_path)
        self._search = SearchService(database_path)
        self._attempts = AttemptService(database_path)
        self._catalog = CatalogService(database_path)
        self._files = FileStore(library_root)
        self._adapter = LegacyQueueAdapter(database_path, queue_release)
        self._scheduler = RequestScheduler(
            database_path,
            jobs=self._jobs,
            worker_id=worker_id or f"request_process-{uuid.uuid4().hex}",
            search_request=self.search_request,
            start_attempt=self.start_attempt,
        )
        self._poll_interval = poll_interval
        self._kobo_fulfilment = kobo_fulfilment
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()

    def set_kobo_fulfilment(self, callback: KoboFulfilment | None) -> None:
        self._kobo_fulfilment = callback

    def set_durable_job_handler(
        self,
        job_type: str,
        callback: Callable[[dict[str, Any]], Any] | None,
    ) -> None:
        self._scheduler.set_job_handler(job_type, callback)

    def set_external_job_scheduler(self, callback: Callable[[datetime], int] | None) -> None:
        self._scheduler.set_external_scheduler(callback)

    def start(self) -> None:
        """Start one worker and immediately repair expired durable-job leases."""
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_event.clear()
            self._scheduler.recover_and_schedule()
            self._thread = threading.Thread(
                target=self._worker_loop,
                daemon=True,
                name="RequestScheduler",
            )
            self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def search_request(
        self, request_process_id: int, *, search_run_id: int | None = None
    ) -> Mapping[str, Any]:
        """Search each currently-enabled release source for one request_process."""
        request_process, book, profile = self._load_search_context(request_process_id)
        if str(request_process["content_type"]).casefold() != "ebook":
            raise ValueError("Audiobooks are no longer supported")
        metadata = BookMetadata(
            provider="catalog",
            provider_id=str(book["id"]),
            title=str(book["title"]),
            authors=[str(book["author"])] if book["author"] else [],
            publish_year=book["publish_year"],
            language=book["language"],
        )
        plan = build_release_search_plan(
            metadata, languages=[book["language"]] if book["language"] else None
        )
        sources: dict[str, Callable[[], list[ReleaseCandidate]]] = {}
        for item in list_available_sources():
            name = str(item["name"])
            if not item.get("enabled") or request_process["content_type"] not in item.get(
                "supported_content_types", []
            ):
                continue
            sources[name] = self._source_callable(
                name, metadata, plan, str(request_process["content_type"])
            )
        if not sources:
            self._mark_needs_attention(request_process_id, "no_enabled_sources")
            return {"accepted_count": 0}
        return self._search.run_search(
            request_process_id, profile=profile, sources=sources, search_run_id=search_run_id
        )

    def start_attempt(self, request_process_id: int) -> Any:
        return self._attempts.start_next_candidate(
            request_process_id,
            adapter_name="legacy_queue",
            adapter=self._adapter,
        )

    def handle_terminal(self, task: Any, *, status: str) -> None:
        """Translate a legacy terminal task into canonical attempt state.

        A task without a matching canonical attempt remains a legacy task and
        is intentionally ignored here.
        """
        attempt = self._find_attempt(task)
        if attempt is None:
            return
        request_attempt_id = int(attempt["id"])
        if status == "complete":
            try:
                file_id = self._import_task_file(
                    task, request_process_id=int(attempt["request_process_id"])
                )
                self._attempts.complete_import(request_attempt_id, file_id=file_id)
                self._enqueue_book_available(int(attempt["request_process_id"]))
                self._apply_kobo_intent(int(attempt["request_process_id"]), file_id)
            except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                self._record_terminal_failure(request_attempt_id, FailureCode.CORRUPT, str(exc))
            return
        message = str(
            getattr(task, "last_error_message", None) or getattr(task, "status_message", None) or ""
        )
        code = classify_terminal_failure(task) if status == "error" else FailureCode.UNKNOWN
        self._record_terminal_failure(request_attempt_id, code, message)

    def _record_terminal_failure(
        self, request_attempt_id: int, code: FailureCode, message: str
    ) -> None:
        disposition = self._attempts.record_failure(request_attempt_id, code, error_message=message)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT request_process_id, retry_count FROM request_attempts WHERE id = ?",
                (request_attempt_id,),
            ).fetchone()
        if row is None:
            return
        request_process_id = int(row["request_process_id"])
        self._enqueue_request_process_error(
            request_process_id, request_attempt_id=request_attempt_id, code=code
        )
        if disposition is FailureDisposition.RETRY_CANDIDATE:
            retry_count = int(row["retry_count"])
            delay = DEFAULT_RETRY_DELAYS[min(retry_count - 1, len(DEFAULT_RETRY_DELAYS) - 1)]
            self._scheduler.enqueue_attempt(
                request_process_id,
                retry=True,
                retry_number=retry_count,
                run_at=datetime.now(UTC) + delay,
            )
        elif disposition is FailureDisposition.BLOCK_CANDIDATE:
            self._scheduler.enqueue_attempt(request_process_id)

    def _enqueue_book_available(self, request_process_id: int) -> None:
        """Notify every fulfilled recipient after the file transaction commits."""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT r.user_id, a.book_id, b.title FROM request_process_recipients r
                    JOIN request_processes a ON a.id = r.request_process_id JOIN books b ON b.id = a.book_id
                    WHERE r.request_process_id = ?""",
                (request_process_id,),
            ).fetchall()
        for row in rows:
            enqueue_web_push(
                self._database_path,
                user_id=int(row["user_id"]),
                event_type="book.available",
                payload={
                    "title": str(row["title"]),
                    "book_id": int(row["book_id"]),
                    "url": f"/catalog/{int(row['book_id'])}",
                },
                event_key=f"request_process-complete:{request_process_id}",
            )

    def _enqueue_request_process_error(
        self, request_process_id: int, *, request_attempt_id: int, code: FailureCode
    ) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT r.user_id, a.book_id, b.title FROM request_process_recipients r
                    JOIN request_processes a ON a.id = r.request_process_id JOIN books b ON b.id = a.book_id
                    WHERE r.request_process_id = ?""",
                (request_process_id,),
            ).fetchall()
        for row in rows:
            enqueue_web_push(
                self._database_path,
                user_id=int(row["user_id"]),
                event_type="request.error",
                payload={
                    "title": str(row["title"]),
                    "book_id": int(row["book_id"]),
                    "code": code.value,
                    "url": f"/catalog/{int(row['book_id'])}",
                },
                event_key=f"attempt-error:{request_attempt_id}:{code.value}",
            )

    def _source_callable(
        self, source_name: str, book: BookMetadata, plan: Any, content_type: str
    ) -> Callable[[], list[ReleaseCandidate]]:
        def search() -> list[ReleaseCandidate]:
            releases = get_source(source_name).search(book, plan, content_type=content_type)
            return [self._candidate_from_release(release) for release in releases]

        return search

    @staticmethod
    def _candidate_from_release(release: Release) -> ReleaseCandidate:
        return ReleaseCandidate(
            source=release.source,
            title=release.title,
            download_url=release.download_url,
            indexer=release.indexer,
            author=release.extra.get("author") if isinstance(release.extra, dict) else None,
            language=release.language,
            format=release.format,
            protocol=_candidate_protocol(release.protocol),
            size_bytes=release.size_bytes,
            seeders=release.seeders,
            publish_year=release.extra.get("year") if isinstance(release.extra, dict) else None,
            metadata={
                "payload_reference": encode_source_reference(release.source, release.source_id),
            },
        )

    def _load_search_context(
        self, request_process_id: int
    ) -> tuple[sqlite3.Row, sqlite3.Row, ReleaseProfile]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT a.*, b.id, b.title, b.author, b.language, b.publish_year
                FROM request_processes a JOIN books b ON b.id = a.book_id WHERE a.id = ?
                """,
                (request_process_id,),
            ).fetchone()
        if row is None:
            raise ValueError("Request not found")
        snapshot = json.loads(str(row["profile_snapshot_json"]))
        if not isinstance(snapshot, dict):
            snapshot = {}
        return (
            row,
            row,
            release_profile_from_snapshot(
                snapshot,
                title=str(row["title"]),
                author=row["author"],
                language=row["language"],
                publish_year=row["publish_year"],
            ),
        )

    def _find_attempt(self, task: Any) -> sqlite3.Row | None:
        task_id = getattr(task, "task_id", None)
        source = getattr(task, "source", None)
        if not isinstance(task_id, str) or not isinstance(source, str):
            return None
        with self._connect() as conn:
            return conn.execute(
                """SELECT * FROM request_attempts
                WHERE client_type = 'legacy_queue' AND external_job_id = ?
                  AND status IN ('submitted', 'downloading', 'importing')""",
                (f"{source}:{task_id}",),
            ).fetchone()

    def _import_task_file(self, task: Any, *, request_process_id: int) -> int:
        paths = list(getattr(task, "library_paths", []) or [])
        if not paths and getattr(task, "download_path", None):
            paths = [task.download_path]
        if not paths:
            raise ValueError("completed download did not provide an imported file path")
        with self._connect() as conn:
            row = conn.execute(
                "SELECT book_id, content_type FROM request_processes WHERE id = ?",
                (request_process_id,),
            ).fetchone()
            if row is not None:
                deleting = conn.execute(
                    """SELECT 1 FROM catalog_deletion_operations
                       WHERE book_id = ? AND state IN ('deleting', 'cleanup_pending')""",
                    (row["book_id"],),
                ).fetchone()
                if deleting is not None:
                    raise ValueError("Book is being deleted")
        if row is None:
            raise ValueError("Request not found")
        book_id = int(row["book_id"])
        try:
            source_path = Path(paths[0])
            record = self._catalog.publish_and_register_imported_file(
                book_id=book_id,
                file_store=self._files,
                source_path=source_path,
                source_roots=(self._files.storage_root,),
                release_source=str(getattr(task, "source", "")),
                content_type=str(row["content_type"]),
            )
        except CatalogStorageLeaseError as exc:
            raise ValueError("Catalog storage is busy") from exc
        return int(record["id"])

    def _apply_kobo_intent(self, request_process_id: int, file_id: int) -> None:
        callback = self._kobo_fulfilment
        if callback is None:
            return
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT r.user_id, a.book_id, r.add_to_kobo_when_available
                FROM request_process_recipients r JOIN request_processes a ON a.id = r.request_process_id
                WHERE r.request_process_id = ?""",
                (request_process_id,),
            ).fetchall()
        for row in rows:
            if row["add_to_kobo_when_available"]:
                try:
                    callback(int(row["user_id"]), int(row["book_id"]), file_id)
                except OSError, RuntimeError, ValueError, sqlite3.Error:
                    # A Kobo preference must never roll back a completed request_process.
                    continue

    def _mark_needs_attention(self, request_process_id: int, code: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE request_processes SET status = 'needs_attention', error_code = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (code, request_process_id),
            )

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            with suppress(OSError, RuntimeError, ValueError, sqlite3.Error):
                self._scheduler.run_once()
            self._stop_event.wait(self._poll_interval)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn


def _candidate_protocol(protocol: object) -> str:
    normalized = str(protocol or "direct").casefold()
    return {
        "http": "direct",
        "https": "direct",
        "dcc": "direct",
        "nzb": "usenet",
    }.get(normalized, normalized)


def classify_terminal_failure(task: Any) -> FailureCode:
    """Classify coordinator errors into durable fallback policy buckets."""
    details = " ".join(
        str(value or "")
        for value in (
            getattr(task, "last_error_type", None),
            getattr(task, "last_error_message", None),
            getattr(task, "status_message", None),
        )
    ).casefold()
    rules: tuple[tuple[tuple[str, ...], FailureCode], ...] = (
        (("password protected", "encrypted archive"), FailureCode.PASSWORD_PROTECTED),
        (("executable payload", "unsafe executable"), FailureCode.EXECUTABLE_CONTENT),
        (("wrong content", "unexpected content"), FailureCode.WRONG_CONTENT),
        (("not found", "404"), FailureCode.NOT_FOUND),
        (("corrupt", "crc", "bad zip", "invalid archive"), FailureCode.CORRUPT),
        (("timeout", "timed out"), FailureCode.TIMEOUT),
        (("unauthorized", "forbidden", "credential", "401", "403"), FailureCode.CREDENTIALS),
        (("no space", "disk full", "permission denied", "storage"), FailureCode.STORAGE),
        (("not configured", "configuration"), FailureCode.CONFIGURATION),
        (("network", "connection", "dns", "socket"), FailureCode.NETWORK),
    )
    for needles, code in rules:
        if any(needle in details for needle in needles):
            return code
    return FailureCode.UNKNOWN
