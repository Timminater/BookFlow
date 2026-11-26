"""Retire a wrong library file and queue one replacement for its readers."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing, suppress
from datetime import UTC, datetime

from bookflow.core.book_groups import equivalent_book_ids
from bookflow.core.release_candidates import source_reference_fingerprint
from bookflow.core.release_profile_service import profile_snapshot_from_row, snapshot_hash
from bookflow.core.request_runtime import encode_source_reference
from bookflow.core.request_workflow_service import RequestWorkflowError
from bookflow.core.requests_service import RequestServiceError, enforce_request_limits


def replace_incorrect_file(
    database_path: str, *, user_id: int, book_id: int, file_id: int
) -> dict[str, int | bool]:
    """Commit the report, quarantine and monitored replacement in one write transaction."""
    with closing(sqlite3.connect(database_path, timeout=30)) as conn, conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("BEGIN IMMEDIATE")
        file = conn.execute(
            "SELECT * FROM book_files WHERE id=? AND book_id=? AND content_type='ebook'",
            (file_id, book_id),
        ).fetchone()
        library_books = {
            row[0]
            for row in conn.execute("SELECT book_id FROM user_library WHERE user_id=?", (user_id,))
        }
        if file is None or not library_books.intersection(equivalent_book_ids(conn, book_id)):
            raise RequestWorkflowError(
                "File not found in your library", code="file_not_found", status_code=404
            )
        source_id = int(file["derived_from_file_id"] or file["id"])
        prior = conn.execute(
            """SELECT request_process_id, release_blocked FROM incorrect_file_reports
               WHERE file_id=? AND reported_by=?""",
            (source_id, user_id),
        ).fetchone()
        if prior is not None:
            return {
                "request_process_id": int(prior["request_process_id"]),
                "release_blocked": bool(prior["release_blocked"]),
            }
        if (
            file["import_status"] != "imported"
            or conn.execute(
                "SELECT 1 FROM user_file_entitlements WHERE user_id=? AND file_id=?",
                (user_id, file_id),
            ).fetchone()
            is None
        ):
            raise RequestWorkflowError(
                "File not found in your library", code="file_not_found", status_code=404
            )
        if (
            conn.execute(
                """SELECT 1 FROM request_processes WHERE book_id=? AND content_type='ebook'
               AND status NOT IN ('completed', 'cancelled', 'failed')""",
                (book_id,),
            ).fetchone()
            is not None
        ):
            raise RequestWorkflowError(
                "A download for this book is already in progress",
                code="replacement_in_progress",
                status_code=409,
            )
        try:
            enforce_request_limits(conn, user_id=user_id)
        except RequestServiceError as exc:
            raise RequestWorkflowError(
                str(exc), code=exc.code or "request_limit_reached", status_code=exc.status_code
            ) from exc

        origin = _original_attempt(conn, source_id)
        profile = conn.execute(
            "SELECT * FROM release_profiles WHERE is_active=1 ORDER BY is_default DESC, id LIMIT 1"
        ).fetchone()
        if origin is not None:
            profile_values = (
                origin["profile_id"],
                origin["profile_name"],
                origin["profile_hash"],
                origin["profile_snapshot_json"],
            )
        elif profile is not None:
            snapshot = profile_snapshot_from_row(profile)
            profile_values = (
                profile["id"],
                profile["name"],
                snapshot_hash(snapshot),
                json.dumps(snapshot),
            )
        else:
            raise RequestWorkflowError(
                "Configure an active release profile before requesting a replacement",
                code="profile_required",
                status_code=409,
            )
        now = datetime.now(UTC).isoformat(timespec="seconds")
        process_id = conn.execute(
            """INSERT INTO request_processes
                   (book_id, content_type, profile_id, profile_name, profile_hash, profile_snapshot_json,
                    status, monitoring_enabled, next_search_at)
               VALUES (?, 'ebook', ?, ?, ?, ?, 'queued', 1, ?)""",
            (book_id, *profile_values, now),
        ).lastrowid
        if process_id is None:
            raise RuntimeError("Replacement process insert returned no ID")
        reference = _release_reference(conn, source_id, origin)
        block_identity = source_reference_fingerprint(reference) or (
            origin["fingerprint"] if origin is not None else None
        )
        release_blocked = bool(block_identity)
        conn.execute(
            """INSERT INTO incorrect_file_reports
                   (file_id, reported_by, request_process_id, release_blocked)
               VALUES (?, ?, ?, ?)""",
            (source_id, user_id, process_id, int(release_blocked)),
        )
        if block_identity:
            conn.execute(
                """INSERT INTO blocked_releases
                       (fingerprint, scope, request_process_id, request_attempt_id, reason, blocked_by)
                   VALUES (?, 'request_process', ?, ?, 'incorrect_library_file', ?)""",
                (
                    block_identity,
                    process_id,
                    origin["attempt_id"] if origin is not None else None,
                    user_id,
                ),
            )

        recipients = conn.execute(
            """WITH affected_files AS (
                   SELECT id, book_id, storage_path FROM book_files WHERE id=? OR derived_from_file_id=?
               ), affected_users AS (
                   SELECT e.user_id FROM user_file_entitlements e JOIN affected_files f ON f.id=e.file_id
                   UNION
                   SELECT ud.user_id FROM user_downloads ud
                   JOIN download_history h ON h.id=ud.history_id
                   JOIN affected_files f ON f.book_id=h.book_id AND f.storage_path=h.download_path
               )
               SELECT u.user_id, EXISTS(
                   SELECT 1 FROM user_kobo_books k JOIN affected_files f ON f.id=k.file_id
                   WHERE k.user_id=u.user_id AND k.book_id=f.book_id AND k.desired_state='active'
               ) AS add_to_kobo
               FROM affected_users u ORDER BY u.user_id != ?, u.user_id""",
            (source_id, source_id, user_id),
        ).fetchall()
        for recipient in recipients:
            _queue_recipient(conn, process_id, book_id, profile_values, recipient, now)
            if recipient["add_to_kobo"]:
                _remove_kobo_file(conn, int(recipient["user_id"]), book_id, now)

        conn.execute(
            """DELETE FROM user_downloads WHERE history_id IN (
                   SELECT h.id FROM download_history h JOIN book_files f ON f.storage_path=h.download_path
                   WHERE h.book_id=f.book_id AND (f.id=? OR f.derived_from_file_id=?)
               )""",
            (source_id, source_id),
        )
        conn.execute(
            """DELETE FROM user_file_entitlements
               WHERE file_id IN (SELECT id FROM book_files WHERE id=? OR derived_from_file_id=?)""",
            (source_id, source_id),
        )
        conn.execute(
            "UPDATE book_files SET import_status='quarantined' WHERE id=? OR derived_from_file_id=?",
            (source_id, source_id),
        )
        return {"request_process_id": process_id, "release_blocked": release_blocked}


def _original_attempt(conn: sqlite3.Connection, file_id: int) -> sqlite3.Row | None:
    """Prefer recorded provenance. Old requests can identify one unambiguous attempt."""
    rows = conn.execute(
        """SELECT a.id AS attempt_id, c.fingerprint, c.payload_reference, p.profile_id, p.profile_name,
                  p.profile_hash, p.profile_snapshot_json
           FROM request_attempts a
           JOIN release_candidates c ON c.id=a.candidate_id
           JOIN request_processes p ON p.id=a.request_process_id
           WHERE a.status='completed' AND (
                a.imported_file_id=?
                OR (a.imported_file_id IS NULL AND EXISTS (
                    SELECT 1 FROM requests r WHERE r.request_process_id=p.id AND r.fulfilled_file_id=?
                ))
           )
           ORDER BY a.imported_file_id IS NULL, a.id
           LIMIT 2""",
        (file_id, file_id),
    ).fetchall()
    return rows[0] if len(rows) == 1 else None


def _release_reference(
    conn: sqlite3.Connection, file_id: int, origin: sqlite3.Row | None
) -> str | None:
    if origin is not None and origin["payload_reference"]:
        return str(origin["payload_reference"])
    rows = conn.execute(
        """SELECT DISTINCT h.source, h.task_id
           FROM book_files f JOIN download_history h ON h.book_id=f.book_id
                AND h.download_path=f.storage_path
           WHERE f.id=? AND h.final_status='complete' AND h.source=f.release_source
           LIMIT 2""",
        (file_id,),
    ).fetchall()
    if len(rows) == 1:
        with suppress(ValueError):
            return encode_source_reference(rows[0]["source"], rows[0]["task_id"])
    return None


def _queue_recipient(
    conn: sqlite3.Connection,
    process_id: int,
    book_id: int,
    profile: tuple[str | int | None, ...],
    recipient: sqlite3.Row,
    now: str,
) -> None:
    user_id = recipient["user_id"]
    kobo_intent = recipient["add_to_kobo"]
    pending = conn.execute(
        """SELECT id, add_to_kobo_when_available FROM requests
           WHERE user_id=? AND book_id=? AND content_type='ebook'
             AND profile_hash=? AND status='pending_approval'""",
        (user_id, book_id, profile[2]),
    ).fetchone()
    if pending is None:
        request_id = conn.execute(
            """INSERT INTO requests
                   (user_id, book_id, content_type, profile_id, profile_name, profile_hash,
                    profile_snapshot_json, status, request_process_id, add_to_kobo_when_available,
                    reviewed_at)
               VALUES (?, ?, 'ebook', ?, ?, ?, ?, 'approved', ?, ?, ?)""",
            (user_id, book_id, *profile, process_id, kobo_intent, now),
        ).lastrowid
        if request_id is None:
            raise RuntimeError("Replacement request insert returned no ID")
    else:
        request_id = pending["id"]
        kobo_intent = max(kobo_intent, pending["add_to_kobo_when_available"])
        conn.execute(
            """UPDATE requests SET status='approved', request_process_id=?,
                      add_to_kobo_when_available=?, reviewed_at=?, updated_at=? WHERE id=?""",
            (process_id, kobo_intent, now, now, request_id),
        )
    conn.execute(
        """INSERT INTO request_process_recipients
               (request_process_id, user_id, request_id, add_to_kobo_when_available)
           VALUES (?, ?, ?, ?)""",
        (process_id, user_id, request_id, kobo_intent),
    )


def _remove_kobo_file(conn: sqlite3.Connection, user_id: int, book_id: int, now: str) -> None:
    conn.execute(
        "UPDATE user_kobo_settings SET current_revision=current_revision+1, updated_at=? WHERE user_id=?",
        (now, user_id),
    )
    revision = conn.execute(
        "SELECT current_revision FROM user_kobo_settings WHERE user_id=?", (user_id,)
    ).fetchone()[0]
    conn.execute(
        """UPDATE user_kobo_books SET file_id=NULL, desired_state='deleted', preferred_format=NULL,
                  source='incorrect_file', request_id=NULL, revision=?, updated_at=?
           WHERE user_id=? AND book_id=?""",
        (revision, now, user_id, book_id),
    )
    conn.execute(
        """INSERT INTO user_kobo_changes (user_id, revision, change_type, book_id, created_at)
           VALUES (?, ?, 'delete', ?, ?)""",
        (user_id, revision, book_id, now),
    )
