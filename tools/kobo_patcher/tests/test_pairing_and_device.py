# ruff: noqa: ANN001, ANN002, ANN201, ANN202, ANN204, N802, S101

import base64
import json
from pathlib import Path

import pytest
from kobo_patcher.device import WindowsVolumeScanner, _version_details, eject_volume
from kobo_patcher.pairing import PairingClient, PairingCode, PairingError, parse_pairing_code


def code(payload: dict[str, object]) -> str:
    blob = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"SM1.{blob}.signature"


def test_pairing_code_reads_origin_without_exposing_secret():
    parsed = parse_pairing_code(
        code(
            {
                "origin": "https://books.example",
                "pairing_id": "nonce",
                "expires_at": 1,
                "protocol_version": 1,
            }
        )
    )
    assert parsed.host == "books.example"
    assert not parsed.insecure_lan


@pytest.mark.parametrize(
    "origin", ["http://example.com", "ftp://books.example", "https://books.example/subpath"]
)
def test_pairing_code_rejects_unsafe_or_non_origin_urls(origin):
    with pytest.raises(PairingError):
        parse_pairing_code(
            code({"origin": origin, "pairing_id": "x", "expires_at": 1, "protocol_version": 1})
        )


def test_pairing_requests_send_a_named_user_agent(monkeypatch):
    import io

    from kobo_patcher import pairing as pairing_module

    sent = []

    def fake_urlopen(request, timeout):
        sent.append(request)
        return io.BytesIO(b"{}")

    monkeypatch.setattr(pairing_module, "urlopen", fake_urlopen)
    client = PairingClient(PairingCode("code", "https://books.example", "id", 4_000_000_000, 1))
    client._request("POST", "/api/kobo-pairing/claim", {})
    assert sent[0].get_header("User-agent") == "BookFlowKoboPatcher/1"


def test_complete_refuses_an_endpoint_from_a_different_origin(monkeypatch):
    pairing = PairingCode("code", "https://books.example", "id", 4_000_000_000, 1)
    client = PairingClient(pairing)
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: {
            "api_endpoint": "https://other.example/api/kobo/token",
            "protocol_version": 1,
        },
    )
    with pytest.raises(PairingError, match="invalid Kobo endpoint"):
        client.complete("session", "Kobo")


def test_complete_refuses_public_http_even_if_returned_by_server(monkeypatch):
    pairing = PairingCode("code", "http://127.0.0.1", "id", 4_000_000_000, 1)
    client = PairingClient(pairing)
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: {
            "api_endpoint": "http://books.example/api/kobo/token",
            "protocol_version": 1,
        },
    )
    with pytest.raises(PairingError, match="invalid Kobo endpoint"):
        client.complete("session", "Kobo")


def test_complete_requires_matching_protocol_version(monkeypatch):
    pairing = PairingCode("code", "https://books.example", "id", 4_000_000_000, 1)
    client = PairingClient(pairing)
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args, **_kwargs: {
            "api_endpoint": "https://books.example/api/kobo/token",
            "protocol_version": 2,
        },
    )
    with pytest.raises(PairingError, match="incompatible pairing protocol"):
        client.complete("session", "Kobo")


def test_revoke_retries_and_requires_server_confirmation(monkeypatch):
    pairing = PairingCode("code", "https://books.example", "id", 4_000_000_000, 1)
    client = PairingClient(pairing)
    attempts = 0

    def request(*_args: object, **_kwargs: object) -> dict[str, bool]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PairingError("temporary network failure")
        return {"revoked": True}

    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr("kobo_patcher.pairing.time.sleep", lambda _delay: None)
    assert client.revoke("session") is True
    assert attempts == 3


def test_revoke_reports_unconfirmed_cleanup_after_retries(monkeypatch):
    pairing = PairingCode("code", "https://books.example", "id", 4_000_000_000, 1)
    client = PairingClient(pairing)
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: {"revoked": False})
    monkeypatch.setattr("kobo_patcher.pairing.time.sleep", lambda _delay: None)
    assert client.revoke("session") is False


class FakeScanner(WindowsVolumeScanner):
    def __init__(self, roots: list[Path]):
        self.roots = roots

    def mounted_roots(self):
        return self.roots

    def volume_label(self, root):
        return "Kobo Libra"


def test_scanner_identifies_only_complete_kobo_layout(tmp_path):
    kobo = tmp_path / "kobo"
    (kobo / ".kobo" / "Kobo").mkdir(parents=True)
    (kobo / ".kobo" / "Kobo" / "Kobo eReader.conf").write_text("[General]\n")
    (kobo / ".kobo" / "KoboReader.sqlite").write_bytes(b"sqlite untouched")
    (kobo / ".kobo" / "version").write_text("firmware=4.40\nserial=AB123456CD\n")
    other = tmp_path / "other"
    other.mkdir()
    devices = FakeScanner([kobo, other]).scan()
    assert len(devices) == 1
    assert devices[0].label == "Kobo Libra"
    assert devices[0].serial_masked.startswith("AB")


def test_scanner_handles_arbitrary_roots_and_ignores_non_kobo_volumes(tmp_path):
    first = tmp_path / "arbitrary-volume-a"
    second = tmp_path / "arbitrary-volume-b"
    non_kobo = tmp_path / "camera-card"
    for root in (first, second):
        (root / ".kobo" / "Kobo").mkdir(parents=True)
        (root / ".kobo" / "Kobo" / "Kobo eReader.conf").write_text("[General]\nkey=value\n")
        (root / ".kobo" / "KoboReader.sqlite").write_bytes(b"database bytes")
        (root / ".kobo" / "version").write_text("4.38.21908,N873", encoding="utf-8")
    (non_kobo / ".kobo" / "Kobo").mkdir(parents=True)
    (non_kobo / ".kobo" / "Kobo" / "Kobo eReader.conf").write_text("[General]\n")
    (non_kobo / ".kobo" / "version").write_text("4.38.21908,N873")

    devices = FakeScanner([first, non_kobo, second]).scan()

    assert [device.root for device in devices] == [first, second]
    assert all(device.platform == "Libra 2" for device in devices)


def test_version_parser_reads_kobo_csv_firmware_and_platform(tmp_path):
    version = tmp_path / "version"
    version.write_text("4.38.21908,ab12cd34,N873,serial=AB123456CD", encoding="utf-8")
    firmware, platform, serial = _version_details(version)
    assert firmware == "4.38.21908"
    assert platform == "Libra 2"
    assert serial is not None and serial.startswith("AB") and serial.endswith("CD")


def test_eject_preserves_full_width_handle(monkeypatch):
    class FakeKernel32:
        def __init__(self):
            self.closed = None

        def CreateFileW(self, *_args):
            return 0x1_0000_1234

        def DeviceIoControl(self, *_args):
            return True

        def CloseHandle(self, handle):
            self.closed = handle
            return True

    fake = FakeKernel32()
    monkeypatch.setattr("kobo_patcher.device._kernel32", lambda: fake)
    assert eject_volume(Path("E:\\"))
    assert fake.closed == 0x1_0000_1234
