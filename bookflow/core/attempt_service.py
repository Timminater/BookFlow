"""Durable request_process attempts and automatic candidate fallback."""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Never, Protocol

from bookflow.core.attempt_policy import FailureCode, FailureDisposition, decide_attempt_failure
from bookflow.core.request_workflow_service import RequestWorkflowService

if TYPE_CHECKING:
    from collections.abc import Mapping
    from os import PathLike


class DownloadAdapter(Protocol):
    """Minimum idempotent external download-client boundary."""

    def add(self, candidate: Mapping[str, Any], *, idempotency_key: str) -> str: ...


class AttemptServiceError(RuntimeError):
    """Raised when an request_process cannot safely start or finish an attempt."""


class CandidateRetryNotFoundError(AttemptServiceError):
    """Raised when the selected release candidate does not exist."""


class CandidateRetryConflictError(AttemptServiceError):
    """Raised when a selected release cannot safely be retried."""


@dataclass(frozen=True, slots=True)
class CandidateRetryClaim:
    """State required to requeue, or restore, one canonical failed attempt."""

    candidate_id: int
    request_process_id: int
    request_attempt_id: int
    task_id: str
    retry_count: int
    failure_code: str | None
    failure_disposition: str | None
    error_message: str | None
    completed_at: str | None


def _raise_attempt(message: str) -> Never:
    raise AttemptServiceError(message)


def _raise_candidate_not_found(message: str) -> Never:
    raise CandidateRetryNotFoundError(message)


def _raise_candidate_conflict(message: str) -> Never:
    raise CandidateRetryConflictError(message)


def _required_request_attempt_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise AttemptServiceError("Attempt insert returned no id")
    return cursor.lastrowid


class AttemptService:
    """Persist attempts around external side effects and choose deterministic fallbacks."""

    def __init__(self, db_path: str | PathLike[str]) -> None:
        self._db_path = os.fspath(db_path)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    @staticmethod
    def _next_candidate(
        conn: sqlite3.Connection,
        *,
        request_process_id: int,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT release_candidates.*
            FROM release_candidates
            WHERE release_candidates.request_process_id = ?
              AND (
                  release_candidates.selected_at IS NOT NULL
                  OR (
                      release_candidates.accepted = 1
                      AND NOT EXISTS (
                          SELECT 1 FROM blocked_releases
                          WHERE blocked_releases.fingerprint IN (
                                release_candidates.fingerprint,
                                json_extract(release_candidates.normalized_json, '$.source_reference_fingerprint')
                            )
                            AND blocked_releases.unblocked_at IS NULL
                            AND (
                                blocked_releases.scope = 'global'
                                OR blocked_releases.request_process_id = release_candidates.request_process_id
                                OR (
                                    blocked_releases.reason = 'incorrect_library_file'
                                    AND EXISTS (
                                        SELECT 1 FROM request_processes original
                                        JOIN request_processes current
                                          ON current.book_id=original.book_id
                                         AND current.id=release_candidates.request_process_id
                                        WHERE original.id=blocked_releases.request_process_id
                                    )
                                )
                            )
                      )
                  )
              )
              AND NOT EXISTS (
                  SELECT 1 FROM request_attempts
                  WHERE request_attempts.candidate_id = release_candidates.id
                    AND request_attempts.status IN (
                        'queued', 'submitted', 'downloading', 'importing', 'completed', 'failed'
                    )
              )
            ORDER BY release_candidates.selected_at IS NULL,
                     release_candidates.selected_at DESC,
                     release_candidates.total_score DESC,
                     COALESCE(release_candidates.seeders, 0) DESC,
                     release_candidates.fingerprint ASC
            LIMIT 1
            """,
            (request_process_id,),
        ).fetchone()

    def start_next_candidate(
        self,
        request_process_id: int,
        *,
        adapter_name: str,
        adapter: DownloadAdapter,
    ) -> dict[str, Any] | None:
        """Claim the next candidate before performing the idempotent client add."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            request_process = conn.execute(
                "SELECT * FROM request_processes WHERE id = ?",
                (request_process_id,),
            ).fetchone()
            if request_process is None:
                _raise_attempt("Request not found")
            if request_process["active_request_attempt_id"] is not None:
                active = conn.execute(
                    "SELECT * FROM request_attempts WHERE id = ?",
                    (request_process["active_request_attempt_id"],),
                ).fetchone()
                conn.commit()
                return dict(active) if active is not None else None
            retry = conn.execute(
                """
                SELECT request_attempts.id AS request_attempt_id, release_candidates.*
                FROM request_attempts
                JOIN release_candidates
                  ON release_candidates.id = request_attempts.candidate_id
                WHERE request_attempts.request_process_id = ?
                  AND request_attempts.status = 'retry_scheduled'
                ORDER BY request_attempts.id
                LIMIT 1
                """,
                (request_process_id,),
            ).fetchone()
            candidate = retry or self._next_candidate(conn, request_process_id=request_process_id)
            if candidate is None:
                conn.execute(
                    """
                    UPDATE request_processes
                    SET status = 'needs_attention', error_code = 'no_candidates_remaining',
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (datetime.now(UTC).isoformat(timespec="seconds"), request_process_id),
                )
                conn.commit()
                return None
            if retry is not None:
                request_attempt_id = int(retry["request_attempt_id"])
                conn.execute(
                    """
                    UPDATE request_attempts
                    SET status = 'queued', client_type = ?, failure_code = NULL,
                        failure_disposition = NULL, error_message = NULL,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        adapter_name,
                        datetime.now(UTC).isoformat(timespec="seconds"),
                        request_attempt_id,
                    ),
                )
            else:
                attempt_number = conn.execute(
                    "SELECT COUNT(*) + 1 FROM request_attempts WHERE request_process_id = ?",
                    (request_process_id,),
                ).fetchone()[0]
                cursor = conn.execute(
                    """
                    INSERT INTO request_attempts (
                        request_process_id, candidate_id, client_type, status, attempt_number
                    )
                    VALUES (?, ?, ?, 'queued', ?)
                    """,
                    (request_process_id, candidate["id"], adapter_name, attempt_number),
                )
                request_attempt_id = _required_request_attempt_id(cursor)
            conn.execute(
                """
                UPDATE request_processes
                SET status = 'downloading', active_request_attempt_id = ?, error_code = NULL,
                    error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (
                    request_attempt_id,
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    request_process_id,
                ),
            )
            conn.commit()
            candidate_payload = dict(candidate)
        except Exception:
            conn.rollback()
            conn.close()
            raise

        idempotency_key = f"request_process:{request_process_id}:attempt:{request_attempt_id}"
        try:
            external_job_id = adapter.add(
                candidate_payload,
                idempotency_key=idempotency_key,
            )
        except ConnectionError, OSError, TimeoutError:
            self.record_failure(
                request_attempt_id,
                FailureCode.CLIENT_UNAVAILABLE,
                error_message="Download client unavailable",
            )
            raise

        try:
            conn.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC).isoformat(timespec="seconds")
            conn.execute(
                """
                UPDATE request_attempts
                SET status = 'submitted', external_job_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (external_job_id, now, request_attempt_id),
            )
            row = conn.execute(
                "SELECT * FROM request_attempts WHERE id = ?",
                (request_attempt_id,),
            ).fetchone()
            conn.commit()
            if row is None:
                _raise_attempt("Attempt disappeared after client submission")
            return dict(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def record_failure(
        self,
        request_attempt_id: int,
        code: FailureCode,
        *,
        error_message: str | None = None,
    ) -> FailureDisposition:
        """Persist failure classification and unblock the next safe action."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            attempt = conn.execute(
                """
                SELECT request_attempts.*, release_candidates.fingerprint
                FROM request_attempts
                JOIN release_candidates
                  ON release_candidates.id = request_attempts.candidate_id
                WHERE request_attempts.id = ?
                """,
                (request_attempt_id,),
            ).fetchone()
            if attempt is None:
                _raise_attempt("Attempt not found")
            decision = decide_attempt_failure(
                code,
                retries_already_scheduled=int(attempt["retry_count"]),
            )
            now = datetime.now(UTC).isoformat(timespec="seconds")
            retry_count = int(attempt["retry_count"])
            attempt_status = "failed"
            request_process_status = "needs_attention"
            if decision.disposition is FailureDisposition.RETRY_CANDIDATE:
                attempt_status = "retry_scheduled"
                request_process_status = "retry_scheduled"
                retry_count += 1
            elif decision.disposition is FailureDisposition.BLOCK_CANDIDATE:
                request_process_status = "searching"
                conn.execute(
                    """
                    INSERT INTO blocked_releases (
                        fingerprint, scope, request_process_id, request_attempt_id, reason
                    )
                    VALUES (?, 'request_process', ?, ?, ?)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        attempt["fingerprint"],
                        attempt["request_process_id"],
                        request_attempt_id,
                        code.value,
                    ),
                )
            conn.execute(
                """
                UPDATE request_attempts
                SET status = ?, retry_count = ?, failure_code = ?,
                    failure_disposition = ?, error_message = ?, updated_at = ?,
                    completed_at = CASE WHEN ? = 'failed' THEN ? ELSE completed_at END
                WHERE id = ?
                """,
                (
                    attempt_status,
                    retry_count,
                    code.value,
                    decision.disposition.value,
                    (error_message or "")[:500] or None,
                    now,
                    attempt_status,
                    now,
                    request_attempt_id,
                ),
            )
            conn.execute(
                """
                UPDATE request_processes
                SET status = ?, active_request_attempt_id = NULL, error_code = ?,
                    error_message = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    request_process_status,
                    code.value,
                    (error_message or "")[:500] or None,
                    now,
                    attempt["request_process_id"],
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        else:
            return decision.disposition
        finally:
            conn.close()

    def claim_selected_candidate_retry(self, candidate_id: int) -> CandidateRetryClaim:
        """Atomically reopen a selected failed attempt before its legacy task is requeued."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            candidate = conn.execute(
                """
                SELECT release_candidates.*, request_processes.status AS process_status,
                       request_processes.active_request_attempt_id
                FROM release_candidates
                JOIN request_processes
                  ON request_processes.id = release_candidates.request_process_id
                WHERE release_candidates.id = ?
                """,
                (candidate_id,),
            ).fetchone()
            if candidate is None:
                _raise_candidate_not_found("Candidate not found")
            if candidate["selected_by"] is None or candidate["selected_at"] is None:
                _raise_candidate_conflict("Candidate is not selected")
            if candidate["process_status"] != "needs_attention":
                _raise_candidate_conflict("Request process is not awaiting attention")
            if candidate["active_request_attempt_id"] is not None:
                _raise_candidate_conflict("Request process already has an active attempt")

            attempt = conn.execute(
                """
                SELECT * FROM request_attempts
                WHERE candidate_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (candidate_id,),
            ).fetchone()
            if attempt is None:
                _raise_candidate_conflict("Candidate has no previous attempt")
            if attempt["status"] != "failed":
                _raise_candidate_conflict("Latest candidate attempt is not failed")
            source = str(candidate["source"])
            external_job_id = str(attempt["external_job_id"] or "")
            prefix = f"{source}:"
            if not external_job_id.startswith(prefix) or not external_job_id.removeprefix(prefix):
                _raise_candidate_conflict("Candidate has no retryable download task")

            claim = CandidateRetryClaim(
                candidate_id=candidate_id,
                request_process_id=int(candidate["request_process_id"]),
                request_attempt_id=int(attempt["id"]),
                task_id=external_job_id.removeprefix(prefix),
                retry_count=int(attempt["retry_count"]),
                failure_code=attempt["failure_code"],
                failure_disposition=attempt["failure_disposition"],
                error_message=attempt["error_message"],
                completed_at=attempt["completed_at"],
            )
            now = datetime.now(UTC).isoformat(timespec="seconds")
            updated = conn.execute(
                """
                UPDATE request_attempts
                SET status = 'submitted', retry_count = retry_count + 1,
                    failure_code = NULL, failure_disposition = NULL, error_message = NULL,
                    completed_at = NULL, updated_at = ?
                WHERE id = ? AND status = 'failed'
                """,
                (now, claim.request_attempt_id),
            )
            if updated.rowcount != 1:
                _raise_candidate_conflict("Candidate retry is already in progress")
            conn.execute(
                """
                UPDATE request_processes
                SET status = 'downloading', active_request_attempt_id = ?,
                    error_code = NULL, error_message = NULL, updated_at = ?
                WHERE id = ?
                """,
                (claim.request_attempt_id, now, claim.request_process_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        else:
            return claim
        finally:
            conn.close()

    def rollback_selected_candidate_retry(self, claim: CandidateRetryClaim) -> None:
        """Restore a claimed retry when the legacy queue rejects the requeue."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            now = datetime.now(UTC).isoformat(timespec="seconds")
            restored = conn.execute(
                """
                UPDATE request_attempts
                SET status = 'failed', retry_count = ?, failure_code = ?,
                    failure_disposition = ?, error_message = ?, completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'submitted' AND retry_count = ?
                """,
                (
                    claim.retry_count,
                    claim.failure_code,
                    claim.failure_disposition,
                    claim.error_message,
                    claim.completed_at,
                    now,
                    claim.request_attempt_id,
                    claim.retry_count + 1,
                ),
            )
            if restored.rowcount == 1:
                conn.execute(
                    """
                    UPDATE request_processes
                    SET status = 'needs_attention', active_request_attempt_id = NULL,
                        error_code = ?, error_message = ?, updated_at = ?
                    WHERE id = ? AND active_request_attempt_id = ?
                    """,
                    (
                        claim.failure_code,
                        claim.error_message,
                        now,
                        claim.request_process_id,
                        claim.request_attempt_id,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def complete_import(self, request_attempt_id: int, *, file_id: int) -> list[int]:
        """Complete an imported attempt and fulfil every recipient."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            attempt = conn.execute(
                "SELECT * FROM request_attempts WHERE id = ?",
                (request_attempt_id,),
            ).fetchone()
            if attempt is None:
                _raise_attempt("Attempt not found")
            now = datetime.now(UTC).isoformat(timespec="seconds")
            conn.execute(
                """
                UPDATE request_attempts
                SET status = 'completed', imported_file_id = ?, updated_at = ?, completed_at = ?
                WHERE id = ?
                """,
                (file_id, now, now, request_attempt_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise
        else:
            conn.close()
        return RequestWorkflowService(self._db_path).complete_request_process(
            attempt["request_process_id"],
            file_id=file_id,
        )

    def serialize_attempt(self, request_attempt_id: int) -> str:
        """Return an audit-safe representation without external payload secrets."""
        conn = self._connect()
        try:
            row = conn.execute(
                """
                SELECT id, request_process_id, candidate_id, client_type, status, attempt_number,
                       retry_count, failure_code, failure_disposition, error_message,
                       started_at, updated_at, completed_at
                FROM request_attempts WHERE id = ?
                """,
                (request_attempt_id,),
            ).fetchone()
            if row is None:
                raise AttemptServiceError("Attempt not found")
            return json.dumps(dict(row), sort_keys=True, default=str)
        finally:
            conn.close()
