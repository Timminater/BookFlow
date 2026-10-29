import io
import json
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from kobo_patcher.account import AccountError, BookFlowAccount, _NoRedirect


def test_login_verifies_account_and_does_not_keep_password(monkeypatch):
    account = BookFlowAccount("https://books.example/")
    calls = []
    replies = [{"success": True}, {"authenticated": True, "username": "Marion"}]

    def request(req, timeout):
        calls.append(req)
        return io.BytesIO(json.dumps(replies.pop(0)).encode())

    monkeypatch.setattr(account.opener, "open", request)
    assert account.login("Marion", "test-only-password") == "Marion"
    payload = json.loads(calls[0].data)
    assert payload["remember_me"] is False
    assert calls[0].get_header("X-bookflow-csrf") == "1"
    assert calls[1].full_url == "https://books.example/api/auth/check"
    assert "test-only-password" not in repr(vars(account))
    account.clear()
    assert account.username is None


@pytest.mark.parametrize("origin", ["http://books.example", "https://user:secret@books.example", "https://books.example/settings", "https://books.example?x=y", "https://"])
def test_login_refuses_unsafe_or_ambiguous_destination(origin):
    with pytest.raises(AccountError):
        BookFlowAccount(origin)


def test_redirect_never_forwards_credentials():
    with pytest.raises(AccountError):
        _NoRedirect().redirect_request(Request("https://books.example"), None, 307, "", {}, "https://other.example")


def test_login_error_does_not_expose_response_or_keep_session(monkeypatch):
    account = BookFlowAccount("https://books.example")
    def reject(*args, **kwargs):
        raise HTTPError("https://books.example", 401, "sensitive-body", {}, None)
    monkeypatch.setattr(account.opener, "open", reject)
    with pytest.raises(AccountError, match="Gebruikersnaam") as error:
        account.login("Marion", "private-password")
    assert "private-password" not in str(error.value)
    assert "sensitive-body" not in str(error.value)
    assert account.username is None
    assert list(account.cookies) == []


def test_login_requires_personal_authenticated_user(monkeypatch):
    account = BookFlowAccount("https://books.example")
    replies = iter([{"success": True}, {"authenticated": True, "auth_mode": "none"}])
    monkeypatch.setattr(account, "_request", lambda *args: next(replies))
    with pytest.raises(AccountError, match="persoonlijk"):
        account.login("Marion", "test-only")
