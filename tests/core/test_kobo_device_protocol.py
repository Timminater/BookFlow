"""Reader-shaped requests: initialization, auth, delta sync, files and bookmarks.

Wire shapes are based on the Kobo Store API as implemented by Calibre-Web,
not the former synthetic BookFlow contract. Physical firmware acceptance is
recorded separately in docs/kobo.md.
"""

import base64
import json
import sqlite3
from urllib.parse import urlsplit

import pytest
from flask import Flask

from bookflow.core.kobo_pairing import KoboPairingService
from bookflow.core.kobo_pairing_routes import register_kobo_pairing_routes
from bookflow.core.kobo_protocol import book_uuid
from bookflow.core.kobo_routes import register_kobo_routes

from .test_auth_api import main_module as main_module
from .test_kobo_positions import write_epub
from .test_kobo_services import kobo_fixture as kobo_fixture


def _reader(fixture, guard=None, cover_fetcher=None, file_exporter=None):
    app = Flask(__name__)
    if guard:
        app.before_request(guard)
    register_kobo_routes(
        app,
        token_service=fixture["tokens"],
        sync_service=fixture["sync"],
        allowed_file_roots=(fixture["storage_root"],),
        tls_guard=lambda: True,
        cover_fetcher=cover_fetcher or (lambda *_: (b"image", "image/jpeg")),
        file_exporter=file_exporter,
    )
    user_id = fixture["user"]["id"]
    fixture["sync"].set_book(
        user_id=user_id, book_id=fixture["book_id"], file_id=fixture["file_id"]
    )
    token = fixture["tokens"].issue_token(user_id=user_id)
    return app.test_client(), f"/api/kobo/{token.token}"


def test_exported_download_and_bidirectional_sentence_position(kobo_fixture):
    original = kobo_fixture["storage_root"] / "book.epub"
    exported = kobo_fixture["storage_root"] / "converted.kepub.epub"
    write_epub(original, "<p>First. Second.</p>")
    write_epub(
        exported, '<p><span id="kobo.1.1">First. </span><span id="kobo.1.2">Second.</span></p>'
    )

    def export(user_id, row):
        assert user_id == kobo_fixture["user"]["id"]
        return {
            **row,
            "storage_path": str(exported),
            "file_format": "kepub",
            "size_bytes": exported.stat().st_size,
        }

    client, base = _reader(kobo_fixture, file_exporter=export)
    book_id, user_id = kobo_fixture["book_id"], kobo_fixture["user"]["id"]
    with sqlite3.connect(kobo_fixture["db_path"]) as conn:
        conn.execute(
            "INSERT INTO user_reading_progress(user_id, book_id, progress, active, source, revision, updated_at, reader_updated_at, file_id, locator_json) VALUES (?, ?, 50, 1, 'web', 1, ?, ?, ?, ?)",
            (
                user_id,
                book_id,
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                kobo_fixture["file_id"],
                json.dumps({"value": "epubcfi(/6/2!/4/2/1:7)", "progression": 0.5}),
            ),
        )
    item = client.get(base + "/v1/library/sync").json[0]["NewEntitlement"]
    download = item["BookMetadata"]["DownloadUrls"][0]
    assert download["Format"] == "KEPUB"
    assert (
        download["Size"]
        == len(client.get(urlsplit(download["Url"]).path).data)
        == exported.stat().st_size
    )
    assert item["ReadingState"]["CurrentBookmark"]["Location"]["Value"] == "kobo.1.2"
    response = client.put(
        base + f"/v1/library/{book_uuid(book_id)}/state",
        json={
            "ReadingStates": [
                {
                    "CurrentBookmark": {
                        "ProgressPercent": 20,
                        "LastModified": "2026-01-02T00:00:00Z",
                        "Location": {
                            "Type": "KoboSpan",
                            "Value": "kobo.1.1",
                            "Source": "OEBPS/Text/chapter.xhtml",
                        },
                    }
                }
            ]
        },
    )
    assert response.status_code == 200
    state = kobo_fixture["sync"].get_reading_state(user_id=user_id, book_id=book_id)
    assert state["progress"] == 20
    assert state["file_id"] == kobo_fixture["file_id"]
    assert json.loads(state["locator_json"])["value"] == "epubcfi(/6/2!/4/2/1:0)"
    assert original.read_bytes() != exported.read_bytes()


@pytest.mark.parametrize("valid", [True, False])
def test_reader_resolves_catalog_cover_proxy(kobo_fixture, valid):
    source = "https://example.org/cover.jpg"
    encoded = base64.urlsafe_b64encode(source.encode()).decode() if valid else "%%%"
    with sqlite3.connect(kobo_fixture["db_path"]) as conn:
        conn.execute(
            "UPDATE books SET cover_url = ? WHERE id = ?",
            (f"/api/covers/provider_book?url={encoded}", kobo_fixture["book_id"]),
        )
    calls = []

    def fetch(cache_id, url):
        calls.append(url)
        return b"jpeg", "image/jpeg"

    client, base = _reader(kobo_fixture, cover_fetcher=fetch)
    identifier = book_uuid(kobo_fixture["book_id"])
    response = client.get(base + f"/v1/covers/{identifier}/600/800/image.jpg")
    assert response.status_code == (200 if valid else 404)
    assert calls == ([source] if valid else [])
    if valid:
        assert response.data == b"jpeg"
        assert response.content_type == "image/jpeg"


def test_reader_discovers_resources_authenticates_and_downloads(kobo_fixture):
    client, base = _reader(kobo_fixture)
    init = client.get(base + "/v1/initialization", base_url="https://books.example")
    assert init.status_code == 200
    assert init.headers["x-kobo-apitoken"] == "e30="
    resources = init.json["Resources"]
    assert resources["library_sync"] == "https://books.example" + base + "/v1/library/sync"
    assert "{Ids}" in resources["library_metadata"]
    auth = client.post(
        urlsplit(resources["device_auth"]).path,
        json={"DeviceId": "Kobo-Glo", "UserKey": "device-user"},
    )
    assert auth.status_code == 200
    assert {"AccessToken", "RefreshToken", "TokenType", "TrackingId", "UserKey"} <= auth.json.keys()
    sync = client.get(base + "/v1/library/sync", base_url="https://books.example")
    assert isinstance(sync.json, list)
    item = sync.json[0]["NewEntitlement"]
    metadata = item["BookMetadata"]
    identifier = metadata["RevisionId"]
    assert identifier == item["BookEntitlement"]["Id"] == book_uuid(kobo_fixture["book_id"])
    assert {url["Format"] for url in metadata["DownloadUrls"]} == {"EPUB", "EPUB3"}
    for download in metadata["DownloadUrls"]:
        assert download["Url"].startswith("https://books.example" + base)
        assert download["Platform"] == "Generic"
        assert client.get(urlsplit(download["Url"]).path).data == b"synthetic kobo ebook"
    assert client.get(
        base + f"/v1/library/{identifier}/metadata", base_url="https://books.example"
    ).json == [metadata]
    cover = resources["image_url_quality_template"].format(
        ImageId=identifier, Width=600, Height=800, Quality=90, IsGreyscale="true"
    )
    assert client.get(urlsplit(cover).path).data == b"image"
    assert client.get(base).json["Resources"]
    assert sync.headers["Cache-Control"] == "private, no-store"


def test_reader_bootstrap_advertises_and_serves_optional_resources(kobo_fixture):
    client, base = _reader(kobo_fixture)
    resources = client.get(base + "/v1/initialization").json["Resources"]
    assert resources["use_one_store"] == "True"
    assert resources["kobo_onestorelibrary_enabled"] == "False"
    oauth = urlsplit(resources["oauth_host"]).path
    discovery = client.get(oauth + "/.well-known/openid-configuration")
    assert discovery.status_code == 200
    assert "access_token" not in discovery.json
    assert urlsplit(discovery.json["issuer"]).path == oauth
    for key in ("authorization_endpoint", "token_endpoint"):
        path = urlsplit(discovery.json[key]).path
        response = client.post(path, data={"grant_type": "refresh_token", "refresh_token": "old"})
        assert response.status_code == 200
        assert response.json["access_token"] == response.json["AccessToken"]
        assert response.json["expires_in"] > 0
        assert client.post(path.replace(base, "/api/kobo/invalid")).status_code == 401
    assert client.get(oauth + "/jwks").json == {"keys": []}
    assert (
        client.get(
            oauth.replace(base, "/api/kobo/invalid") + "/.well-known/openid-configuration"
        ).status_code
        == 401
    )
    for key in ("affiliaterequest", "assets", "deals", "book_subscription"):
        path = urlsplit(resources[key]).path
        assert path.startswith(base + "/")
        assert client.get(path).status_code == 200
        assert client.get(path.replace(base, "/api/kobo/invalid")).status_code == 401
    assert client.post(base + "/v1/affiliate", json={}).json == {}
    tests = client.post(
        base + "/v1/analytics/gettests", json={}, headers={"X-Kobo-UserKey": "reader-key"}
    )
    assert tests.json == {"Result": "Success", "TestKey": "reader-key", "Tests": {}}
    assert client.get(base + "/v1/user/loyalty/benefits").json == {"Benefits": {}}
    assert client.get(base + "/v1/assets").json == {"Assets": []}
    assert client.get(base + "/v1/not-implemented").status_code == 404
    identifier = book_uuid(kobo_fixture["book_id"])
    assert (
        client.get(base + f"/v1/user/library/books/{identifier}").json["RevisionId"] == identifier
    )


def test_kobo_progress_uses_timestamp_and_invalidates_old_browser_locator(kobo_fixture):
    client, base = _reader(kobo_fixture)
    user_id, book_id = kobo_fixture["user"]["id"], kobo_fixture["book_id"]
    kobo_fixture["sync"].set_reading_state(user_id=user_id, book_id=book_id, progress=60)
    with sqlite3.connect(kobo_fixture["db_path"]) as conn:
        conn.execute(
            "UPDATE user_reading_progress SET source='web', reader_updated_at=?, locator_json=?, file_id=?",
            ("2026-01-01T00:00:00+00:00", '{"value":"old-cfi"}', kobo_fixture["file_id"]),
        )
    url = base + f"/v1/library/{book_uuid(book_id)}/state"
    for progress, modified in ((20, "2026-01-02T00:00:00Z"), (90, "2026-01-01T00:00:00Z")):
        response = client.put(
            url,
            json={
                "ReadingStates": [
                    {
                        "CurrentBookmark": {"ProgressPercent": progress, "LastModified": modified},
                    }
                ]
            },
        )
        assert response.status_code == 200
    with sqlite3.connect(kobo_fixture["db_path"]) as conn:
        row = conn.execute(
            "SELECT progress, locator_json, reader_updated_at FROM user_reading_progress WHERE user_id=? AND book_id=?",
            (user_id, book_id),
        ).fetchone()
    assert row == (20, None, "2026-01-02T00:00:00+00:00")
    assert client.get(url).json[0]["CurrentBookmark"]["ProgressPercent"] == 20


def test_idle_sync_keeps_cursor_and_new_change_is_delivered(kobo_fixture):
    client, base = _reader(kobo_fixture)
    first = client.get(base + "/v1/library/sync")
    headers = {"X-Kobo-Synctoken": first.headers["X-Kobo-Synctoken"]}
    idle = client.get(base + "/v1/library/sync", headers=headers)
    assert idle.json == []
    assert idle.headers["X-Kobo-Synctoken"] == headers["X-Kobo-Synctoken"]
    kobo_fixture["sync"].set_reading_state(
        user_id=kobo_fixture["user"]["id"], book_id=kobo_fixture["book_id"], progress=45
    )
    delta = client.get(base + "/v1/library/sync", headers=headers)
    assert (
        delta.json[0]["ChangedReadingState"]["ReadingState"]["CurrentBookmark"]["ProgressPercent"]
        == 45
    )
    kobo_fixture["sync"].delete_book(
        user_id=kobo_fixture["user"]["id"], book_id=kobo_fixture["book_id"]
    )
    removed = client.get(
        base + "/v1/library/sync", headers={"X-Kobo-Synctoken": delta.headers["X-Kobo-Synctoken"]}
    )
    assert removed.json[0]["ChangedEntitlement"]["BookEntitlement"]["IsRemoved"] is True
    assert client.get(base + f"/v1/files/{kobo_fixture['file_id']}/download").status_code == 404


def test_reader_bookmark_roundtrip_and_stale_state_does_not_reset(kobo_fixture):
    client, base = _reader(kobo_fixture)
    identifier = book_uuid(kobo_fixture["book_id"])
    url = base + f"/v1/library/{identifier}/state"
    state = {
        "EntitlementId": identifier,
        "CurrentBookmark": {
            "ProgressPercent": 42,
            "ContentSourceProgressPercent": 42,
            "Location": {"Value": "chapter3.xhtml#p10", "Type": "KoboSpan", "Source": "kepub"},
        },
        "StatusInfo": {"Status": "Reading"},
        "Statistics": {"SpentReadingMinutes": 15},
    }
    result = client.put(url, json={"ReadingStates": [state]})
    assert result.status_code == 200
    assert result.json["RequestResult"] == "Success"
    read = client.get(url).json[0]
    assert read["CurrentBookmark"]["Location"] == state["CurrentBookmark"]["Location"]
    assert read["Statistics"]["SpentReadingMinutes"] == 15
    assert (
        client.put(
            url, json={"ReadingStates": [{"CurrentBookmark": {"ProgressPercent": 5}}]}
        ).status_code
        == 200
    )
    assert client.get(url).json[0]["CurrentBookmark"]["ProgressPercent"] == 42
    assert client.delete(base + f"/v1/library/{identifier}").status_code == 200
    assert client.get(url).status_code == 404


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"ReadingStates": []},
        {"ReadingStates": [{"CurrentBookmark": {"ProgressPercent": True}}]},
        {"ReadingStates": [{"CurrentBookmark": {"ProgressPercent": -1}}]},
        {"ReadingStates": [{"CurrentBookmark": []}]},
        {"ReadingStates": [{"EntitlementId": "another-book"}]},
    ],
)
def test_malformed_native_state_is_rejected(kobo_fixture, payload):
    client, base = _reader(kobo_fixture)
    response = client.put(
        base + f"/v1/library/{book_uuid(kobo_fixture['book_id'])}/state", json=payload
    )
    assert response.status_code == 400


def test_native_tokens_and_entitlements_do_not_cross_users(kobo_fixture):
    client, base = _reader(kobo_fixture)
    cursor = client.get(base + "/v1/library/sync").headers["X-Kobo-Synctoken"]
    other = kobo_fixture["tokens"].issue_token(user_id=kobo_fixture["other_user"]["id"])
    other_base = f"/api/kobo/{other.token}"
    for method, path in [
        ("get", "/v1/library/sync"),
        ("get", "/v1/initialization"),
        ("post", "/v1/auth/device"),
        ("post", "/v1/user/add-device"),
    ]:
        assert getattr(client, method)("/api/kobo/invalid-token" + path, json={}).status_code == 401
    assert (
        client.get(
            other_base + "/v1/library/sync", headers={"X-Kobo-Synctoken": cursor}
        ).status_code
        == 409
    )
    assert (
        client.get(
            base + "/v1/library/sync", headers={"X-Kobo-Synctoken": cursor + "broken"}
        ).status_code
        == 409
    )
    assert (
        client.get(
            other_base + f"/v1/library/{book_uuid(kobo_fixture['book_id'])}/metadata"
        ).status_code
        == 404
    )
    assert (
        client.delete(other_base + f"/v1/library/{book_uuid(kobo_fixture['book_id'])}").status_code
        == 404
    )


def test_reader_pagination_resumes_after_restart_without_replaying(kobo_fixture):
    client, base = _reader(kobo_fixture)
    sync = kobo_fixture["sync"]
    for number in range(101):
        sync.set_reading_state(
            user_id=kobo_fixture["user"]["id"], book_id=kobo_fixture["book_id"], progress=number
        )
    page = client.get(base + "/v1/library/sync")
    assert page.headers["X-Kobo-Sync"] == "continue"
    resumed = client.get(
        base + "/v1/library/sync", headers={"X-Kobo-Synctoken": page.headers["X-Kobo-Synctoken"]}
    )
    assert "X-Kobo-Sync" not in resumed.headers
    # A new service instance (server restart) accepts the same signed checkpoint.
    restarted = type(sync)(str(kobo_fixture["db_path"]), cursor_secret="test-cursor-secret")
    checkpoint = resumed.headers["X-Kobo-Synctoken"].removeprefix("bf1.")
    assert (
        restarted.sync(
            user_id=kobo_fixture["user"]["id"], cursor=checkpoint, persistent=True
        ).changes
        == []
    )


def test_real_origin_guard_allows_token_requests_but_rejects_cross_site(
    kobo_fixture, main_module, monkeypatch
):
    monkeypatch.setitem(main_module.app.config, "ENFORCE_MUTATION_ORIGIN", True)
    client, base = _reader(kobo_fixture, main_module.enforce_browser_mutation_origin)
    assert client.post(base + "/v1/auth/device", json={"DeviceId": "Glo"}).status_code == 200
    assert client.post(base + "/v1/auth/refresh", json={}).status_code == 200
    assert client.post(base + "/v1/user/add-device", json={}).status_code == 200
    assert (
        client.post(
            base + "/v1/auth/device", json={}, headers={"Origin": "https://evil.example"}
        ).status_code
        == 403
    )
    # A cookie is not a substitute for the URL token.
    client.set_cookie("session", "irrelevant")
    assert client.post("/api/kobo/invalid-token/v1/auth/device", json={}).status_code == 401


def test_patcher_pairing_works_through_production_origin_guard(
    kobo_fixture, main_module, monkeypatch
):
    monkeypatch.setitem(main_module.app.config, "ENFORCE_MUTATION_ORIGIN", True)
    service = KoboPairingService(
        str(kobo_fixture["db_path"]),
        secret="pairing-secret" * 4,
        token_service=kobo_fixture["tokens"],
    )
    code = service.create_code(user_id=kobo_fixture["user"]["id"], origin="https://books.example")
    app = Flask(__name__)
    app.before_request(main_module.enforce_browser_mutation_origin)
    register_kobo_pairing_routes(
        app,
        resolve_actor=lambda: None,
        service=service,
        patcher_cache=kobo_fixture["storage_root"] / "kobopatcher",
    )
    client = app.test_client()
    claim = client.post(
        "/api/kobo/pair/claim", json={"code": code["code"]}, base_url="https://books.example"
    )
    assert claim.status_code == 200
    session = claim.json["pairing_session"]
    complete = client.post(
        "/api/kobo/pair/complete",
        json={"pairing_session": session, "device_label": "Glo"},
        base_url="https://books.example",
    )
    assert complete.status_code == 200
    assert complete.json["api_endpoint"].startswith("https://books.example/api/kobo/")
    revoke = client.delete(
        "/api/kobo/pair/session",
        json={"pairing_session": session},
        base_url="https://books.example",
    )
    assert revoke.json["revoked"] is True
    assert client.post("/api/v1/users/me/kobo-pairing-code").status_code == 403
