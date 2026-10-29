"""Parallel provider search with persisted, explainable candidate results."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Never

from bookflow.core.release_candidates import (
    CandidateEvaluation,
    ReleaseCandidate,
    ReleaseProfile,
    evaluate_candidate,
)

if TYPE_CHECKING:
    from os import PathLike

SearchSource = Callable[[], list[ReleaseCandidate]]
_OPAQUE_REFERENCE = re.compile(r"^[A-Za-z0-9:_-]{1,256}$")
# Browser-challenge fallbacks regularly need just over 30 seconds to start a
# browser and solve the challenge. Keep the aggregate search window long enough
# for those sources while still bounding interactive and scheduled searches.
DEFAULT_SEARCH_TIMEOUT_SECONDS = 60.0


class SearchServiceError(RuntimeError):
    """Raised when a persisted search cannot be started safely."""


def _raise_search(message: str) -> Never:
    raise SearchServiceError(message)


def _required_search_run_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise SearchServiceError("Search run insert returned no id")
    return cursor.lastrowid


class SearchService:
    """Run independent sources concurrently and persist normalized results."""

    def __init__(self, db_path: str | PathLike[str]) -> None:
        self._db_path = str(Path(db_path))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def create_queued_run(self, request_process_id: int) -> dict[str, Any]:
        """Reserve an interactive run before its durable worker starts.

        Returning this durable identifier lets callers poll one immutable
        search operation instead of accidentally rendering stale candidates.
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            process = conn.execute(
                "SELECT status FROM request_processes WHERE id = ?", (request_process_id,)
            ).fetchone()
            if process is None or process["status"] in {"completed", "cancelled", "failed"}:
                _raise_search("Request process is not searchable")
            cursor = conn.execute(
                "INSERT INTO request_search_runs(request_process_id, status, source_count) VALUES (?, 'running', 0)",
                (request_process_id,),
            )
            run_id = _required_search_run_id(cursor)
            row = conn.execute(
                "SELECT * FROM request_search_runs WHERE id = ?", (run_id,)
            ).fetchone()
            conn.commit()
            if row is None:
                _raise_search("Search run disappeared")
            return dict(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _safe_payload_reference(candidate: ReleaseCandidate) -> str | None:
        value = candidate.metadata.get("payload_reference")
        if not isinstance(value, str) or not _OPAQUE_REFERENCE.fullmatch(value):
            return None
        return value

    @staticmethod
    def _normalized_candidate(candidate: ReleaseCandidate) -> str:
        """Serialize only non-secret fields; URLs and arbitrary metadata are excluded."""
        return json.dumps(
            {
                "author": candidate.author,
                "files": list(candidate.files),
                "format": candidate.format,
                "indexer": candidate.indexer,
                "isbn": candidate.isbn,
                "is_archive": candidate.is_archive,
                "is_bundle": candidate.is_bundle,
                "language": candidate.language,
                "protocol": candidate.protocol,
                "publish_year": candidate.publish_year,
                "seeders": candidate.seeders,
                "size_bytes": candidate.size_bytes,
                "source": candidate.source,
                "title": candidate.title,
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    def run_search(
        self,
        request_process_id: int,
        *,
        profile: ReleaseProfile,
        sources: Mapping[str, SearchSource],
        timeout_seconds: float = DEFAULT_SEARCH_TIMEOUT_SECONDS,
        search_run_id: int | None = None,
    ) -> dict[str, Any]:
        """Run a search now; one broken source cannot discard other results."""
        if not sources:
            raise ValueError("At least one search source is required")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

        started_at = datetime.now(UTC).isoformat(timespec="seconds")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            request_process = conn.execute(
                "SELECT id, status FROM request_processes WHERE id = ?",
                (request_process_id,),
            ).fetchone()
            if request_process is None:
                _raise_search("Request not found")
            if request_process["status"] in {"completed", "cancelled", "failed"}:
                _raise_search("Completed or cancelled request cannot be searched")
            conn.execute(
                """
                UPDATE request_processes
                SET status = 'searching', last_search_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (started_at, started_at, request_process_id),
            )
            if search_run_id is None:
                cursor = conn.execute(
                    """INSERT INTO request_search_runs(request_process_id, status, source_count, started_at)
                       VALUES (?, 'running', ?, ?)""",
                    (request_process_id, len(sources), started_at),
                )
                search_run_id = _required_search_run_id(cursor)
            else:
                claimed = conn.execute(
                    """UPDATE request_search_runs SET status='running', source_count=?, started_at=?
                       WHERE id=? AND request_process_id=? AND status='running'""",
                    (len(sources), started_at, search_run_id, request_process_id),
                ).rowcount
                if claimed != 1:
                    _raise_search("Search run is no longer active")
            blocked = frozenset(
                row["fingerprint"]
                for row in conn.execute(
                    """
                    SELECT fingerprint FROM blocked_releases
                    WHERE unblocked_at IS NULL
                      AND (scope = 'global' OR request_process_id = ?)
                    """,
                    (request_process_id,),
                ).fetchall()
            )
            conn.commit()
        except Exception:
            conn.rollback()
            conn.close()
            raise

        executor = ThreadPoolExecutor(max_workers=min(len(sources), 8))
        futures: dict[Future[list[ReleaseCandidate]], str] = {
            executor.submit(source): name for name, source in sources.items()
        }
        done, not_done = wait(futures, timeout=timeout_seconds)
        errors: dict[str, str] = {}
        candidates: list[ReleaseCandidate] = []
        for future in done:
            source_name = futures[future]
            try:
                results = future.result()
            except Exception as exc:  # noqa: BLE001 - provider failures are isolated
                errors[source_name] = type(exc).__name__
            else:
                candidates.extend(results)
        for future in not_done:
            errors[futures[future]] = "Timeout"
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

        evaluated_by_fingerprint: dict[str, tuple[ReleaseCandidate, CandidateEvaluation]] = {}
        for candidate in candidates:
            evaluation = evaluate_candidate(
                candidate,
                profile,
                blocked_fingerprints=blocked,
            )
            existing = evaluated_by_fingerprint.get(evaluation.fingerprint)
            if existing is None or (
                evaluation.total_score,
                candidate.seeders or 0,
                candidate.source,
            ) > (
                existing[1].total_score,
                existing[0].seeders or 0,
                existing[0].source,
            ):
                evaluated_by_fingerprint[evaluation.fingerprint] = (candidate, evaluation)
        evaluated_candidates = list(evaluated_by_fingerprint.values())

        completed_at = datetime.now(UTC).isoformat(timespec="seconds")
        accepted_count = sum(evaluation.accepted for _, evaluation in evaluated_candidates)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for candidate, evaluation in evaluated_candidates:
                conn.execute(
                    """
                    INSERT INTO release_candidates (
                        request_process_id, search_run_id, fingerprint, source, indexer, title,
                        author, isbn, language, file_format, protocol, size_bytes, seeders,
                        publish_year, accepted, total_score, score_components_json,
                        reject_reasons_json, normalized_json, payload_reference
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(request_process_id, fingerprint) DO UPDATE SET
                        search_run_id = excluded.search_run_id,
                        accepted = excluded.accepted,
                        total_score = excluded.total_score,
                        score_components_json = excluded.score_components_json,
                        reject_reasons_json = excluded.reject_reasons_json,
                        normalized_json = excluded.normalized_json,
                        payload_reference = excluded.payload_reference
                    """,
                    (
                        request_process_id,
                        search_run_id,
                        evaluation.fingerprint,
                        candidate.source,
                        candidate.indexer,
                        candidate.title,
                        candidate.author,
                        candidate.isbn,
                        candidate.language,
                        candidate.format,
                        candidate.protocol,
                        candidate.size_bytes,
                        candidate.seeders,
                        candidate.publish_year,
                        int(evaluation.accepted),
                        evaluation.total_score,
                        json.dumps(evaluation.components, sort_keys=True),
                        json.dumps(evaluation.reject_reasons),
                        self._normalized_candidate(candidate),
                        self._safe_payload_reference(candidate),
                    ),
                )

            if errors and not evaluated_candidates:
                run_status = "failed"
                request_process_status = "needs_attention"
            elif errors:
                run_status = "partial"
                request_process_status = "searching" if accepted_count else "waiting_for_release"
            else:
                run_status = "completed"
                request_process_status = "searching" if accepted_count else "waiting_for_release"
            conn.execute(
                """
                UPDATE request_search_runs
                SET status = ?, candidate_count = ?, errors_json = ?, completed_at = ?
                WHERE id = ?
                """,
                (
                    run_status,
                    len(evaluated_candidates),
                    json.dumps(errors, sort_keys=True),
                    completed_at,
                    search_run_id,
                ),
            )
            conn.execute(
                """
                UPDATE request_processes
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (request_process_status, completed_at, request_process_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return {
            "search_run_id": search_run_id,
            "status": run_status,
            "candidate_count": len(evaluated_candidates),
            "accepted_count": accepted_count,
            "errors": errors,
        }
