from __future__ import annotations

import inspect
import json
import sqlite3
import time

from bookflow.core.release_candidates import ReleaseCandidate, ReleaseProfile
from bookflow.core.request_workflow_service import RequestWorkflowService
from bookflow.core.search_service import SearchService
from bookflow.core.user_db import UserDB


def _seed_request_process(db_path):
    db = UserDB(str(db_path))
    db.initialize()
    admin = db.create_user(username="admin", role="admin")
    user = db.create_user(username="reader")
    conn = sqlite3.connect(db_path)
    cursor = conn.execute(
        "INSERT INTO books (metadata_provider, provider_book_id, title) VALUES ('t', '1', 'Dune')"
    )
    book_id = cursor.lastrowid
    conn.commit()
    conn.close()
    assert book_id is not None
    request = RequestWorkflowService(db_path).create_request(
        user_id=user["id"],
        book_id=book_id,
        profile={"formats": ["epub"]},
    )
    _, request_process, _ = RequestWorkflowService(db_path).approve_request(
        request["id"],
        reviewer_user_id=admin["id"],
    )
    assert request_process is not None
    return request_process["id"]


def test_default_search_timeout_covers_browser_challenge_fallback():
    timeout = inspect.signature(SearchService.run_search).parameters["timeout_seconds"].default

    assert timeout == 60.0


def test_parallel_search_isolates_errors_deduplicates_and_redacts_urls(tmp_path):
    db_path = tmp_path / "users.db"
    request_process_id = _seed_request_process(db_path)
    candidate = ReleaseCandidate(
        source="indexer-a",
        indexer="shared",
        guid="guid-1",
        title="Dune retail",
        author="Frank Herbert",
        language="en",
        format="epub",
        protocol="torrent",
        seeders=20,
        download_url="https://example.test/download?token=super-secret",
        metadata={
            "payload_reference": "prowlarr:result-123",
            "secret": "must-not-persist",
        },
    )

    def broken():
        raise ConnectionError("secret-provider-token")

    result = SearchService(db_path).run_search(
        request_process_id,
        profile=ReleaseProfile(
            title="Dune",
            authors=("Frank Herbert",),
            language="en",
            allowed_formats=("epub",),
            allowed_protocols=("torrent",),
        ),
        sources={
            "a": lambda: [candidate],
            "duplicate": lambda: [candidate],
            "broken": broken,
        },
    )

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT * FROM release_candidates").fetchall()
    run = conn.execute(
        "SELECT * FROM request_search_runs WHERE id = ?",
        (result["search_run_id"],),
    ).fetchone()
    conn.close()

    assert result["status"] == "partial"
    assert result["candidate_count"] == result["accepted_count"] == 1
    assert result["errors"] == {"broken": "ConnectionError"}
    assert len(rows) == 1
    assert rows[0]["payload_reference"] == "prowlarr:result-123"
    persisted = json.dumps(dict(rows[0]), sort_keys=True)
    assert "super-secret" not in persisted
    assert "must-not-persist" not in persisted
    assert json.loads(run["errors_json"]) == {"broken": "ConnectionError"}


def test_no_candidates_moves_monitored_request_process_to_waiting(tmp_path):
    db_path = tmp_path / "users.db"
    request_process_id = _seed_request_process(db_path)

    result = SearchService(db_path).run_search(
        request_process_id,
        profile=ReleaseProfile(title="Dune"),
        sources={"empty": list},
    )

    conn = sqlite3.connect(db_path)
    status = conn.execute(
        "SELECT status FROM request_processes WHERE id = ?",
        (request_process_id,),
    ).fetchone()[0]
    conn.close()
    assert result["status"] == "completed"
    assert status == "waiting_for_release"


def test_source_timeout_is_persisted_without_waiting_for_slow_source(tmp_path):
    db_path = tmp_path / "users.db"
    request_process_id = _seed_request_process(db_path)

    def slow():
        time.sleep(0.2)
        return []

    started = time.monotonic()
    result = SearchService(db_path).run_search(
        request_process_id,
        profile=ReleaseProfile(title="Dune"),
        sources={"slow": slow},
        timeout_seconds=0.01,
    )

    assert time.monotonic() - started < 0.15
    assert result["status"] == "failed"
    assert result["errors"] == {"slow": "Timeout"}
