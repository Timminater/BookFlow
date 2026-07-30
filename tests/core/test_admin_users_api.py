"""Tests for administrator-owned account access controls."""

import os
import tempfile
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from flask import Flask

from bookflow.core.admin_routes import register_admin_routes
from bookflow.core.user_db import UserDB


@pytest.fixture
def user_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db = UserDB(os.path.join(tmpdir, "users.db"))
        db.initialize()
        yield db


@pytest.fixture
def app(user_db):
    test_app = Flask(__name__)
    test_app.config["SECRET_KEY"] = "test-secret"
    test_app.config["TESTING"] = True
    register_admin_routes(test_app, user_db)
    return test_app


def _admin_client(app):
    client = app.test_client()
    with client.session_transaction() as session:
        session["user_id"] = "admin"
        session["is_admin"] = True
    return client


def _create_book(user_db, *, provider_book_id: str) -> int:
    conn = user_db._connect()
    try:
        cursor = conn.execute(
            """
            INSERT INTO books (metadata_provider, provider_book_id, title)
            VALUES ('test', ?, ?)
            """,
            (provider_book_id, f"Book {provider_book_id}"),
        )
        conn.commit()
        return int(cursor.lastrowid)
    finally:
        conn.close()


def test_admin_can_create_regular_user_with_four_character_password(app, user_db):
    user_db.create_user(username="existing-admin", role="admin")

    with patch("bookflow.core.admin_routes.load_active_auth_mode", return_value="builtin"):
        response = _admin_client(app).post(
            "/api/admin/users",
            json={"username": "user", "password": "user", "role": "user"},
        )

    assert response.status_code == 201
    assert response.json["username"] == "user"
    assert response.json["role"] == "user"
    assert response.json["auto_approve_requests"] is False


def test_admin_can_create_read_and_update_request_auto_approval_policy(app, user_db):
    user_db.create_user(username="existing-admin", role="admin")
    with patch("bookflow.core.admin_routes.load_active_auth_mode", return_value="builtin"):
        created = _admin_client(app).post(
            "/api/admin/users",
            json={
                "username": "trusted",
                "password": "user",
                "auto_approve_requests": True,
            },
        )
        updated = _admin_client(app).put(
            f"/api/admin/users/{created.json['id']}",
            json={"auto_approve_requests": False},
        )
        invalid = _admin_client(app).put(
            f"/api/admin/users/{created.json['id']}",
            json={"auto_approve_requests": 1},
        )

    assert created.status_code == 201
    assert created.json["auto_approve_requests"] is True
    assert updated.status_code == 200
    assert updated.json["auto_approve_requests"] is False
    assert invalid.status_code == 400


def test_admin_rejects_password_shorter_than_four_characters(app, user_db):
    user_db.create_user(username="existing-admin", role="admin")

    with patch("bookflow.core.admin_routes.load_active_auth_mode", return_value="builtin"):
        response = _admin_client(app).post(
            "/api/admin/users",
            json={"username": "user", "password": "usr", "role": "user"},
        )

    assert response.status_code == 400
    assert response.json["error"] == "Password must be at least 4 characters"


def test_admin_can_manage_username_password_role_and_library_capability(app, user_db):
    user = user_db.create_user(
        username="alice", password_hash="old", library_capability="request-only"
    )
    with patch("bookflow.core.admin_routes.load_active_auth_mode", return_value="builtin"):
        response = _admin_client(app).put(
            f"/api/admin/users/{user['id']}",
            json={
                "username": "alice-reader",
                "password": "new-password",
                "role": "admin",
                "library_capability": "download-capable",
            },
        )
    assert response.status_code == 200
    assert response.json["username"] == "alice-reader"
    assert response.json["role"] == "admin"
    assert response.json["library_capability"] == "download-capable"
    assert "settings" not in response.json


def test_admin_cannot_edit_personal_preferences_through_user_api(app, user_db):
    user = user_db.create_user(username="alice")
    with patch("bookflow.core.admin_routes.load_active_auth_mode", return_value="builtin"):
        response = _admin_client(app).put(
            f"/api/admin/users/{user['id']}",
            json={"settings": {"notification_transport": "apprise"}},
        )
    assert response.status_code == 400
    assert user_db.get_personal_preferences(user["id"])["notification_transport"] is None


def test_removed_admin_override_endpoint_returns_not_found(app):
    with patch("bookflow.core.admin_routes.load_active_auth_mode", return_value="builtin"):
        response = _admin_client(app).get("/api/admin/settings/overrides-summary")
    assert response.status_code == 404


def test_admin_can_read_user_kobo_audit(app, user_db):
    user = user_db.create_user(username="reader")
    user_db.record_kobo_sync(
        user_id=user["id"],
        token_id=None,
        source_ip="203.0.113.44",
        user_agent="Kobo",
        status="completed",
        changes_sent=3,
    )
    with patch("bookflow.core.admin_routes.load_active_auth_mode", return_value="builtin"):
        response = _admin_client(app).get(f"/api/admin/users/{user['id']}/audit")

    assert response.status_code == 200
    assert response.json["kobo"]["recent_syncs"][0]["changes_sent"] == 3


def test_admin_download_audit_flags_three_ip_switches(app, user_db):
    user = user_db.create_user(username="reader")
    for index, source_ip in enumerate(
        ("192.0.2.10", "198.51.100.20", "192.0.2.10", "198.51.100.20"),
        start=1,
    ):
        user_db.record_file_download(
            user_id=user["id"],
            book_id=_create_book(user_db, provider_book_id=f"download-{index}"),
            history_id=None,
            source_ip=source_ip,
            user_agent="Test Browser",
            file_name=f"book-{index}.epub",
            file_format="epub",
        )

    with patch("bookflow.core.admin_routes.load_active_auth_mode", return_value="builtin"):
        response = _admin_client(app).get(f"/api/admin/users/{user['id']}/audit")

    assert response.status_code == 200
    assert response.json["downloads"]["ip_switch_streak"] == 3
    assert response.json["downloads"]["suspicious"] is True
    assert response.json["ip_addresses"][0]["sources"] == ["download"]


def test_admin_can_inspect_and_revoke_user_session(app, user_db):
    user = user_db.create_user(username="reader")
    session_key = user_db.create_web_session(
        user_id=user["id"],
        auth_source="builtin",
        source_ip="198.51.100.8",
        user_agent="Test Browser",
        expires_at=datetime.now(UTC) + timedelta(days=7),
    )
    user_db.record_auth_event(
        user_id=user["id"],
        username="reader",
        auth_source="builtin",
        event_type="login",
        outcome="success",
        source_ip="198.51.100.8",
        user_agent="Test Browser",
    )

    with patch("bookflow.core.admin_routes.load_active_auth_mode", return_value="builtin"):
        client = _admin_client(app)
        audit = client.get(f"/api/admin/users/{user['id']}/audit")
        tracked_session = audit.json["authentication"]["sessions"][0]
        revoked = client.post(
            f"/api/admin/users/{user['id']}/sessions/{tracked_session['id']}/revoke"
        )

    assert audit.status_code == 200
    assert tracked_session["is_active"] is True
    assert audit.json["ip_addresses"][0]["source_ip"] == "198.51.100.8"
    assert revoked.status_code == 200
    assert not user_db.validate_web_session(user_id=user["id"], session_key=session_key)
