"""Focused tests for personal and administrator notification delivery."""

import sqlite3
from types import SimpleNamespace

from bookflow.core import notifications as notifications_module


class _Executor:
    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        self.calls.append((fn, args))


class _UserDB:
    def __init__(self, preferences):
        self.preferences = preferences

    def get_personal_preferences(self, _user_id):
        return self.preferences


def _context(event):
    return notifications_module.NotificationContext(
        event=event, title="Book", author="Author", book_id=17
    )


def test_personal_delivery_uses_saved_enabled_apprise_destination(monkeypatch):
    executor = _Executor()
    monkeypatch.setattr(notifications_module, "_executor", executor)
    user_db = _UserDB(
        {
            "notifications_enabled": True,
            "notification_transport": "apprise",
            "notification_destination": "ntfys://ntfy.sh/reader",
        }
    )
    notifications_module.notify_user(
        user_db,
        4,
        notifications_module.NotificationEvent.REQUEST_FULFILLED,
        _context(notifications_module.NotificationEvent.REQUEST_FULFILLED),
    )
    assert executor.calls[0][1][0:2] == ("apprise", "ntfys://ntfy.sh/reader")


def test_personal_delivery_ignores_operational_events(monkeypatch):
    executor = _Executor()
    monkeypatch.setattr(notifications_module, "_executor", executor)
    user_db = _UserDB(
        {
            "notifications_enabled": True,
            "notification_transport": "apprise",
            "notification_destination": "ntfys://ntfy.sh/reader",
        }
    )
    notifications_module.notify_user(
        user_db,
        4,
        notifications_module.NotificationEvent.DOWNLOAD_FAILED,
        _context(notifications_module.NotificationEvent.DOWNLOAD_FAILED),
    )
    assert executor.calls == []


def test_shared_event_choice_applies_to_apprise_channel(monkeypatch, tmp_path):
    database_path = str(tmp_path / "notifications.db")
    with sqlite3.connect(database_path) as conn:
        conn.executescript(
            """
            CREATE TABLE user_preferences (
                user_id INTEGER PRIMARY KEY,
                notifications_enabled INTEGER NOT NULL,
                notification_transport TEXT,
                notification_destination TEXT
            );
            CREATE TABLE notification_rules (
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                enabled INTEGER NOT NULL,
                filters_json TEXT NOT NULL,
                PRIMARY KEY (user_id, event_type)
            );
            INSERT INTO user_preferences VALUES
                (4, 1, 'apprise', 'ntfys://ntfy.sh/reader');
            INSERT INTO notification_rules VALUES
                (4, 'book.available', 0, '{}');
            """
        )

    executor = _Executor()
    monkeypatch.setattr(notifications_module, "_executor", executor)
    notifications_module.notify_personal_event(
        database_path,
        user_id=4,
        event_type="book.available",
        payload={"title": "Hidden book"},
    )
    assert executor.calls == []

    with sqlite3.connect(database_path) as conn:
        conn.execute(
            "UPDATE notification_rules SET enabled = 1 "
            "WHERE user_id = 4 AND event_type = 'book.available'"
        )
    notifications_module.notify_personal_event(
        database_path,
        user_id=4,
        event_type="book.available",
        payload={"title": "Visible book"},
    )
    assert len(executor.calls) == 1
    assert executor.calls[0][1][0:2] == (
        "ntfys://ntfy.sh/reader",
        "book.available",
    )


def test_available_message_has_book_context_and_link():
    title, body = notifications_module._render_message(
        _context(notifications_module.NotificationEvent.REQUEST_FULFILLED)
    )
    assert title == "Requested Book Available"
    assert '"Book" by Author' in body
    assert "/library/17" in body


def test_download_messages_include_requesters():
    context = notifications_module.NotificationContext(
        event=notifications_module.NotificationEvent.DOWNLOAD_COMPLETE,
        title="Book",
        author="Author",
        requesters=("timminater", "reader-two"),
    )

    title, body = notifications_module._render_message(context)

    assert title == "Download Complete"
    assert body.endswith("Requested by: timminater, reader-two")


def test_apprise_delivery_attaches_only_http_cover_urls_and_falls_back(monkeypatch):
    class _Apprise:
        calls = []

        @staticmethod
        def instantiate(_url, *, asset=None):
            return object()

        def add(self, _plugin):
            return True

        def notify(self, **kwargs):
            self.calls.append(kwargs)
            return "attach" not in kwargs

    monkeypatch.setattr(
        notifications_module,
        "apprise",
        SimpleNamespace(Apprise=_Apprise, AppriseAsset=None),
    )
    monkeypatch.setattr(notifications_module, "_apprise_proxy_env", lambda: {})

    result = notifications_module._dispatch_to_apprise(
        ["ntfys://ntfy.sh/ops"],
        title="BookFlow",
        body="Body",
        notify_type="info",
        attach="https://covers.example/book.jpg",
    )

    assert result["success"] is True
    assert _Apprise.calls[0]["attach"] == "https://covers.example/book.jpg"
    assert "attach" not in _Apprise.calls[1]


def test_admin_targets_only_receive_operational_event_subscriptions(monkeypatch):
    executor = _Executor()
    monkeypatch.setattr(notifications_module, "_executor", executor)
    monkeypatch.setattr(
        notifications_module,
        "_resolve_admin_targets",
        lambda: [
            {
                "transport": "apprise",
                "destination": "ntfys://ntfy.sh/ops",
                "events": ["request_created"],
            }
        ],
    )
    notifications_module.notify_admin(
        notifications_module.NotificationEvent.REQUEST_CREATED,
        _context(notifications_module.NotificationEvent.REQUEST_CREATED),
    )
    notifications_module.notify_admin(
        notifications_module.NotificationEvent.REQUEST_REJECTED,
        _context(notifications_module.NotificationEvent.REQUEST_REJECTED),
    )
    assert len(executor.calls) == 1


def test_personal_test_requires_an_active_valid_destination():
    result = notifications_module.send_personal_test_notification(
        _UserDB(
            {
                "notifications_enabled": False,
                "notification_transport": "apprise",
                "notification_destination": "ntfys://ntfy.sh/reader",
            }
        ),
        1,
    )
    assert result["success"] is False
