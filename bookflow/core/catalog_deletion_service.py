"""Safe, administrator-triggered hard deletion of catalog books.

The personal library delete deliberately remains in :mod:`library_service`.
This service is only for removing a shared catalog work and therefore plans the
operation first, blocks live workers, commits database changes before touching
disk, and retains download history through its ``ON DELETE SET NULL`` FK.
"""

from __future__ import annotations

import hashlib
import json

# SQL IN placeholder counts are generated solely from local integer tuples.
# ruff: noqa: S608
import logging
import os
import sqlite3
import stat
import threading
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from bookflow.core.book_groups import canonical_book_id, equivalent_book_ids
from bookflow.core.catalog_storage_lease import (
    CatalogStorageLease,
    CatalogStorageLeaseError,
    catalog_book_lease_key,
    catalog_path_lease_key,
)
from bookflow.core.durable_jobs import DurableJobService, LeaseLostError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)


class CatalogDeletionError(RuntimeError):
    """Raised when deleting a catalog work would violate a safety invariant."""


class CatalogDeletionConflictError(CatalogDeletionError):
    """The displayed plan is stale or the work is still in active use."""


class CatalogDeletionNotFoundError(CatalogDeletionError):
    """The requested durable deletion operation does not exist."""


def _raise_catalog_deletion_conflict(message: str) -> None:
    raise CatalogDeletionConflictError(message)


@dataclass(frozen=True)
class CatalogDeletionPlan:
    canonical_book_id: int
    equivalent_book_ids: tuple[int, ...]
    title: str
    author: str | None
    files: tuple[dict[str, Any], ...]
    memberships: int
    entitlements: int
    reading_progress: int
    requests: int
    active_processes: tuple[int, ...]
    active_downloads: int

    def serialize(self) -> dict[str, Any]:
        return {
            "canonical_book_id": self.canonical_book_id,
            "equivalent_book_ids": list(self.equivalent_book_ids),
            "book": {"id": self.canonical_book_id, "title": self.title, "author": self.author},
            "files": list(self.files),
            "counts": {
                "memberships": self.memberships,
                "entitlements": self.entitlements,
                "reading_progress": self.reading_progress,
                "requests": self.requests,
                "active_processes": len(self.active_processes),
                "active_downloads": self.active_downloads,
            },
        }

    def token(self) -> str:
        """Stable optimistic-concurrency token for the exact displayed plan."""
        encoded = json.dumps(self.serialize(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


class CatalogDeletionService:
    """Own a narrow, transactional catalog hard-delete workflow."""

    _ACTIVE_PROCESS_STATUSES = (
        "queued",
        "searching",
        "downloading",
        "importing",
        "retry_scheduled",
        "waiting_for_release",
        "needs_attention",
    )
    _CLEANUP_JOB_TYPE = "catalog_file_cleanup"

    def __init__(self, database_path: str, resolve_file_path: Callable[[str], Path] | None) -> None:
        self._database_path = database_path
        self._resolve_file_path = resolve_file_path
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._database_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @staticmethod
    def _placeholders(values: tuple[object, ...]) -> str:
        return ",".join("?" for _ in values)

    def plan(
        self, book_id: int, *, conn: sqlite3.Connection | None = None
    ) -> CatalogDeletionPlan | None:
        own_connection = conn is None
        connection = conn or self._connect()
        try:
            ids = tuple(equivalent_book_ids(connection, book_id))
            if not ids:
                return None
            canonical_id = canonical_book_id(connection, list(ids))
            row = connection.execute(
                "SELECT title, author FROM books WHERE id = ?", (canonical_id,)
            ).fetchone()
            if row is None:
                return None
            placeholders = self._placeholders(ids)
            files = [
                dict(item)
                for item in connection.execute(
                    f"SELECT id, book_id, storage_path, file_format, sha256, size_bytes, derived_from_file_id "
                    f"FROM book_files WHERE book_id IN ({placeholders}) ORDER BY id",
                    ids,
                ).fetchall()
            ]
            file_ids = tuple(int(item["id"]) for item in files)

            def count(sql: str, args: tuple[object, ...]) -> int:
                return int(connection.execute(sql, args).fetchone()[0])

            memberships = count(
                f"SELECT COUNT(*) FROM user_library WHERE book_id IN ({placeholders})", ids
            )
            progress = count(
                f"SELECT COUNT(*) FROM user_reading_progress WHERE book_id IN ({placeholders})", ids
            )
            requests = count(
                f"SELECT COUNT(*) FROM requests WHERE book_id IN ({placeholders})", ids
            )
            entitlements = (
                count(
                    f"SELECT COUNT(*) FROM user_file_entitlements WHERE file_id IN ({self._placeholders(file_ids)})",
                    file_ids,
                )
                if file_ids
                else 0
            )
            active_processes = tuple(
                int(item[0])
                for item in connection.execute(
                    f"SELECT id FROM request_processes WHERE book_id IN ({placeholders}) "
                    f"AND status IN ({self._placeholders(self._ACTIVE_PROCESS_STATUSES)})",
                    (*ids, *self._ACTIVE_PROCESS_STATUSES),
                ).fetchall()
            )
            active_downloads = count(
                f"SELECT COUNT(*) FROM download_history WHERE book_id IN ({placeholders}) AND final_status = 'active'",
                ids,
            )
            return CatalogDeletionPlan(
                canonical_id,
                ids,
                str(row["title"]),
                row["author"],
                tuple(files),
                memberships,
                entitlements,
                progress,
                requests,
                active_processes,
                active_downloads,
            )
        finally:
            if own_connection:
                connection.close()

    def delete(self, book_id: int, *, actor_user_id: int, plan_token: str) -> dict[str, Any]:
        """Hard-delete one equivalent catalog group; repeated deletes are safe."""
        lease = CatalogStorageLease(
            self._database_path,
            catalog_book_lease_key(self._database_path, book_id),
            uuid.uuid4().hex,
        )
        try:
            lease.acquire()
        except CatalogStorageLeaseError as exc:
            raise CatalogDeletionConflictError("Catalog storage is busy") from exc
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                plan = self.plan(book_id, conn=conn)
                if plan is None:
                    existing = conn.execute(
                        """SELECT operation.id FROM catalog_deletion_operations AS operation
                           WHERE operation.state != 'cleanup_complete'
                           AND (operation.book_id=? OR EXISTS (
                               SELECT 1 FROM json_each(operation.plan_json, '$.equivalent_book_ids')
                               WHERE value=?
                           )) ORDER BY operation.created_at DESC LIMIT 1""",
                        (book_id, book_id),
                    ).fetchone()
                    conn.commit()
                    if existing is not None:
                        operation = self.operation(str(existing["id"]))
                        lease.release()
                        return {
                            "status": "already_deleted",
                            "operation_id": existing["id"],
                            "operation": operation,
                            "cleanup": {
                                "pending": operation["pending"],
                                "failed": [
                                    item
                                    for item in operation["cleanup"]
                                    if item["state"] == "failed"
                                ],
                            },
                        }
                    lease.release()
                    return {"status": "already_deleted", "cleanup": {"removed": [], "failed": []}}
                if plan.token() != plan_token:
                    _raise_catalog_deletion_conflict(
                        "Catalog deletion plan is stale; inspect and confirm again"
                    )
                if plan.active_processes or plan.active_downloads:
                    _raise_catalog_deletion_conflict(
                        "Catalog deletion is blocked while request processes or downloads are active"
                    )
                operation_id = uuid.uuid4().hex
                conn.execute(
                    """INSERT INTO catalog_deletion_operations(id, book_id, plan_token, actor_user_id, state, plan_json)
                       VALUES(?,?,?,?, 'deleting', ?)""",
                    (
                        operation_id,
                        plan.canonical_book_id,
                        plan_token,
                        actor_user_id,
                        json.dumps(plan.serialize(), sort_keys=True),
                    ),
                )
                file_ids = tuple(int(item["id"]) for item in plan.files)
                if file_ids:
                    placeholders = self._placeholders(file_ids)
                    external_children = conn.execute(
                        f"""SELECT id FROM book_files WHERE derived_from_file_id IN ({placeholders})
                            AND id NOT IN ({placeholders})""",
                        (*file_ids, *file_ids),
                    ).fetchall()
                    if external_children:
                        _raise_catalog_deletion_conflict(
                            "Catalog deletion is blocked by derived files outside the deletion group"
                        )
                    # KEPUB cache references both source and output; remove it first.
                    conn.execute(
                        f"DELETE FROM kepub_conversion_cache WHERE source_file_id IN ({placeholders}) "
                        f"OR output_file_id IN ({placeholders})",
                        (*file_ids, *file_ids),
                    )
                    # RESTRICT on source files requires derived rows to be detached/deleted first.
                    # Repeatedly remove leaves so arbitrarily deep conversion
                    # chains are deleted children-first under RESTRICT.
                    while True:
                        deleted = conn.execute(
                            f"""DELETE FROM book_files WHERE id IN ({placeholders})
                                AND NOT EXISTS (SELECT 1 FROM book_files child
                                                WHERE child.derived_from_file_id = book_files.id)""",
                            file_ids,
                        ).rowcount
                        if deleted == 0:
                            break
                ids = plan.equivalent_book_ids
                conn.execute(f"DELETE FROM books WHERE id IN ({self._placeholders(ids)})", ids)
                for file in plan.files:
                    conn.execute(
                        """INSERT INTO catalog_deletion_cleanup(operation_id, storage_path, expected_sha256, expected_size_bytes, state)
                           VALUES(?,?,?,?, 'pending')""",
                        (operation_id, file["storage_path"], file["sha256"], file["size_bytes"]),
                    )
                conn.execute(
                    "UPDATE catalog_deletion_operations SET state='cleanup_pending' WHERE id=?",
                    (operation_id,),
                )
                # This is intentionally emitted only after every destructive
                # database statement has succeeded, in the same transaction.
                conn.execute(
                    """INSERT INTO catalog_deletion_audit_events(operation_id, actor_user_id, event_type, payload_json)
                       VALUES(?,?, 'db_committed', ?)""",
                    (
                        operation_id,
                        actor_user_id,
                        json.dumps({"files": len(plan.files)}, sort_keys=True),
                    ),
                )
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                lease.release()
                raise CatalogDeletionError("Catalog delete transaction failed") from exc
            except Exception:
                conn.rollback()
                lease.release()
                raise
            finally:
                conn.close()

        lease.release()

        # Run exactly the same durable cleanup path as a later retry.  The
        # committed outbox rows remain the source of truth if this process
        # crashes or file verification fails halfway through this first pass.
        self.schedule_pending_cleanup()
        cleanup = self.retry_cleanup(operation_id)
        return {
            "status": "deleted",
            "operation_id": operation_id,
            "plan": plan.serialize(),
            "cleanup": cleanup,
        }

    def _path_still_referenced(self, path: str) -> bool:
        with self._connect() as conn:
            return (
                conn.execute("SELECT 1 FROM book_files WHERE storage_path = ?", (path,)).fetchone()
                is not None
            )

    @staticmethod
    def _valid_operation_id(operation_id: str) -> bool:
        try:
            return uuid.UUID(hex=operation_id).hex == operation_id
        except TypeError, ValueError, AttributeError:
            return False

    def operation(self, operation_id: str) -> dict[str, Any]:
        """Return durable status and cleanup rows, even after the book is gone."""
        if not self._valid_operation_id(operation_id):
            raise ValueError("Invalid deletion operation ID")
        with self._connect() as conn:
            operation = conn.execute(
                "SELECT * FROM catalog_deletion_operations WHERE id=?", (operation_id,)
            ).fetchone()
            if operation is None:
                raise CatalogDeletionNotFoundError("Catalog deletion operation not found")
            cleanup = conn.execute(
                """SELECT storage_path, state, attempts, error, updated_at
                   FROM catalog_deletion_cleanup WHERE operation_id=? ORDER BY id""",
                (operation_id,),
            ).fetchall()
        item = dict(operation)
        item["plan"] = json.loads(str(item.pop("plan_json")))
        item["cleanup"] = [dict(row) for row in cleanup]
        item["pending"] = sum(row["state"] != "complete" for row in cleanup)
        return item

    def pending_operations(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id FROM catalog_deletion_operations WHERE state='cleanup_pending' ORDER BY created_at"
            ).fetchall()
        return [self.operation(str(row["id"])) for row in rows]

    def recover_pending(self) -> list[dict[str, Any]]:
        """Discover pending outbox rows and recover them through durable job leases."""
        self.schedule_pending_cleanup()
        result = self.run_scheduled_cleanup(worker_id=f"catalog-recovery-{uuid.uuid4().hex}")
        return [*result["completed"], *result["failed"]]

    def schedule_pending_cleanup(self) -> int:
        """Idempotently publish every committed cleanup operation as a durable job."""
        jobs = DurableJobService(self._database_path)
        pending = self.pending_operations()
        for operation in pending:
            operation_id = str(operation["id"])
            jobs.enqueue(
                job_key=f"catalog-file-cleanup:{operation_id}",
                job_type=self._CLEANUP_JOB_TYPE,
                payload={"operation_id": operation_id},
                max_attempts=3,
            )
        return len(pending)

    def run_scheduled_cleanup(self, *, worker_id: str, limit: int = 10) -> dict[str, Any]:
        """Atomically claim cleanup jobs; a failing operation cannot stop another."""
        jobs = DurableJobService(self._database_path)
        claimed = jobs.claim_due(
            worker_id=worker_id, lease_duration=timedelta(minutes=2), limit=limit
        )
        completed: list[dict[str, Any]] = []
        failed: list[dict[str, str]] = []
        for job in claimed:
            if job.job_type != self._CLEANUP_JOB_TYPE:
                continue
            operation_id = str(job.payload.get("operation_id", ""))
            try:
                result = self.retry_cleanup(operation_id)
                if result["pending"] == 0:
                    jobs.complete(job_id=job.id, worker_id=worker_id)
                    completed.append(result)
                else:
                    jobs.fail(job_id=job.id, worker_id=worker_id, error="cleanup still pending")
                    failed.append({"operation_id": operation_id, "error": "cleanup still pending"})
            except (CatalogDeletionError, OSError, ValueError, sqlite3.Error) as exc:
                with suppress(LeaseLostError):
                    jobs.fail(job_id=job.id, worker_id=worker_id, error=str(exc))
                failed.append({"operation_id": operation_id, "error": str(exc)})
        return {"claimed": len(claimed), "completed": completed, "failed": failed}

    def retry_cleanup(self, operation_id: str) -> dict[str, Any]:
        """Retry durable post-commit cleanup; missing files are already clean."""
        operation = self.operation(operation_id)
        if operation["state"] == "cleanup_complete":
            raise CatalogDeletionConflictError("Catalog deletion cleanup is already complete")
        if operation["state"] != "cleanup_pending":
            raise CatalogDeletionConflictError("Catalog deletion cleanup is not retryable")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT cleanup.storage_path, cleanup.expected_sha256, cleanup.expected_size_bytes,
                          operation.book_id
                   FROM catalog_deletion_cleanup AS cleanup
                   JOIN catalog_deletion_operations AS operation ON operation.id=cleanup.operation_id
                   WHERE cleanup.operation_id=? AND cleanup.state != 'complete' ORDER BY cleanup.id""",
                (operation_id,),
            ).fetchall()
        complete = 0
        failed: list[dict[str, str]] = []
        for row in rows:
            path = str(row["storage_path"])
            lease = CatalogStorageLease(
                self._database_path,
                catalog_book_lease_key(self._database_path, int(row["book_id"])),
                uuid.uuid4().hex,
            )
            path_lease = CatalogStorageLease(
                self._database_path,
                catalog_path_lease_key(path, str(row["expected_sha256"])),
                uuid.uuid4().hex,
            )
            try:
                lease.acquire()
                path_lease.acquire()
                self._audit(operation_id, "cleanup_attempt", {"path": path})
                if self._path_still_referenced(path):
                    # Another publisher won while cleanup was pending.  It is
                    # deliberately retained; completion means this deletion no
                    # longer owns the physical bytes, never that it unlinked.
                    with self._connect() as conn:
                        conn.execute(
                            """UPDATE catalog_deletion_cleanup SET state='complete', attempts=attempts+1,
                               error='retained_referenced', updated_at=CURRENT_TIMESTAMP
                               WHERE operation_id=? AND storage_path=?""",
                            (operation_id, path),
                        )
                        conn.commit()
                    self._audit(operation_id, "retained_referenced", {"path": path})
                    complete += 1
                    continue
                self._remove_verified_file(
                    path, str(row["expected_sha256"]), int(row["expected_size_bytes"])
                )
                with self._connect() as conn:
                    conn.execute(
                        """UPDATE catalog_deletion_cleanup SET state='complete', attempts=attempts+1,
                           error=NULL, updated_at=CURRENT_TIMESTAMP WHERE operation_id=? AND storage_path=?""",
                        (operation_id, path),
                    )
                    conn.commit()
                self._audit(operation_id, "cleanup_completed", {"path": path})
                complete += 1
            except (OSError, ValueError) as exc:
                with self._connect() as conn:
                    conn.execute(
                        """UPDATE catalog_deletion_cleanup SET state='failed', attempts=attempts+1,
                           error=?, updated_at=CURRENT_TIMESTAMP WHERE operation_id=? AND storage_path=?""",
                        (str(exc), operation_id, path),
                    )
                    conn.commit()
                failed.append({"path": path, "error": str(exc)})
                self._audit(operation_id, "cleanup_failed", {"path": path, "error": str(exc)})
            except CatalogStorageLeaseError as exc:
                failed.append({"path": path, "error": str(exc)})
            finally:
                path_lease.release()
                lease.release()
        with self._connect() as conn:
            pending = conn.execute(
                "SELECT COUNT(*) FROM catalog_deletion_cleanup WHERE operation_id=? AND state != 'complete'",
                (operation_id,),
            ).fetchone()[0]
            conn.execute(
                "UPDATE catalog_deletion_operations SET state=?, completed_at=CASE WHEN ?=0 THEN CURRENT_TIMESTAMP ELSE completed_at END WHERE id=?",
                ("cleanup_complete" if pending == 0 else "cleanup_pending", pending, operation_id),
            )
            conn.commit()
        if complete:
            self._audit(operation_id, "cleanup_retry", {"completed": complete, "pending": pending})
        if pending == 0:
            self._audit(operation_id, "completed", {"cleanup_items": complete})
        return {
            "operation_id": operation_id,
            "completed": complete,
            "failed": failed,
            "pending": pending,
        }

    def _remove_verified_file(self, path: str, expected_sha256: str, expected_size: int) -> None:
        if self._resolve_file_path is None:
            raise ValueError("Central file cleanup is not configured")
        target = self._resolve_file_path(path)
        try:
            path_stat = os.lstat(target)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(path_stat.st_mode):
            raise ValueError("Cleanup target must not be a symbolic link")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(target, flags)
        except OSError as exc:
            raise ValueError("Cleanup target could not be opened safely") from exc
        try:
            stat_result = os.fstat(descriptor)
            if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_size != expected_size:
                raise ValueError("Cleanup target failed regular-file or size verification")
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != expected_sha256:
                raise ValueError("Cleanup target hash does not match deletion plan")
            # Detect a path replacement after fstat/hash before unlink.
            current_stat = os.lstat(target)
            if (
                stat.S_ISLNK(current_stat.st_mode)
                or current_stat.st_ino != stat_result.st_ino
                or current_stat.st_dev != stat_result.st_dev
            ):
                raise ValueError("Cleanup target changed during verification")
            target.unlink()
        finally:
            os.close(descriptor)

    def _audit(self, operation_id: str, event_type: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            actor = conn.execute(
                "SELECT actor_user_id FROM catalog_deletion_operations WHERE id=?", (operation_id,)
            ).fetchone()
            if actor is None:
                return
            conn.execute(
                "INSERT INTO catalog_deletion_audit_events(operation_id, actor_user_id, event_type, payload_json) VALUES(?,?,?,?)",
                (
                    operation_id,
                    actor["actor_user_id"],
                    event_type,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            conn.commit()
