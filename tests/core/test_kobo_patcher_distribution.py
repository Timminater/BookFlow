import hashlib
import json
import os
from pathlib import Path

import pytest
from flask import Flask

from bookflow.core.kobo_pairing_routes import register_kobo_pairing_routes, secure_patcher_cache


class _Actor:
    user_id = 1


class _PairingService:
    PROTOCOL_VERSION = 1


def _write_artifact(cache: Path, version: str = "1.0.0") -> Path:
    name = f"BookFlowKoboPatcher-{version}-win-x64.exe"
    artifact = cache / name
    payload = b"MZ self-contained patcher"
    artifact.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    artifact.with_name(f"{name}.sha256").write_text(f"{digest}  {name}", encoding="ascii")
    artifact.with_name(f"{name}.spdx.json").write_text(
        json.dumps(
            {
                "spdxVersion": "SPDX-2.3",
                "bookflow_kobo_patcher": {"protocol_version": 1, "target": "win-x64"},
                "packages": [
                    {
                        "SPDXID": "SPDXRef-BookFlowKoboPatcher",
                        "versionInfo": version,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return artifact


def _app(cache: Path, release_version: str = "1.0.0") -> Flask:
    app = Flask(__name__)
    register_kobo_pairing_routes(
        app,
        resolve_actor=lambda: _Actor(),
        service=_PairingService(),
        patcher_cache=cache,
        release_version=release_version,
    )
    return app


def test_manifest_only_serves_checksum_and_protocol_validated_artifact(tmp_path: Path) -> None:
    artifact = _write_artifact(tmp_path)
    response = _app(tmp_path).test_client().get("/api/v1/kobo/patcher/manifest")
    assert response.status_code == 200
    assert response.json["filename"] == artifact.name
    assert response.json["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()


def test_manifest_rejects_tampered_cached_artifact(tmp_path: Path) -> None:
    artifact = _write_artifact(tmp_path)
    artifact.write_bytes(b"tampered")
    response = _app(tmp_path).test_client().get("/api/v1/kobo/patcher/manifest")
    assert response.status_code == 404
    assert response.json["available"] is False


def test_manifest_rejects_artifact_for_another_release(tmp_path: Path) -> None:
    _write_artifact(tmp_path, version="1.0.0")
    response = (
        _app(tmp_path, release_version="2.0.0").test_client().get("/api/v1/kobo/patcher/manifest")
    )
    assert response.status_code == 404


def test_download_revalidates_cached_artifact_after_a_previous_manifest(tmp_path: Path) -> None:
    artifact = _write_artifact(tmp_path)
    client = _app(tmp_path).test_client()
    assert client.get("/api/v1/kobo/patcher/manifest").status_code == 200
    artifact.write_bytes(b"tampered after manifest")
    response = client.get("/api/v1/kobo/patcher/download")
    assert response.status_code == 404
    assert response.json["error"]["code"] == "patcher_unavailable"


def test_download_returns_the_finally_verified_snapshot(tmp_path: Path) -> None:
    artifact = _write_artifact(tmp_path)
    response = _app(tmp_path).test_client().get("/api/v1/kobo/patcher/download")
    assert response.status_code == 200
    assert response.data == artifact.read_bytes()
    assert response.headers["Content-Disposition"].endswith("filename=BookFlowKoboPatcher.exe")


def test_secure_cache_rejects_path_outside_the_config_root(tmp_path: Path) -> None:
    config = tmp_path / "config"
    outside = tmp_path / "outside" / "patcher-cache"
    assert secure_patcher_cache(outside, config_root=config) is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are not meaningful on Windows")
def test_secure_cache_creates_private_posix_directory(tmp_path: Path) -> None:
    cache = tmp_path / "config" / "patcher-cache"
    assert secure_patcher_cache(cache, config_root=cache.parent) == cache.resolve()
    assert cache.exists()
    assert cache.stat().st_mode & 0o077 == 0


def test_container_seeds_volume_and_keeps_operator_replacement(tmp_path):
    bundle = tmp_path / "image"
    bundle.mkdir()
    source = _write_artifact(bundle, "1.2.9")
    config = tmp_path / "config"
    destination = config / "kobopatcher"

    def start():
        app = Flask(__name__)
        register_kobo_pairing_routes(
            app,
            resolve_actor=lambda: _Actor(),
            service=_PairingService(),
            patcher_cache=destination,
            config_root=config,
            release_version="1.2.9",
            bundled_patcher_dir=bundle,
        )
        return app.test_client()

    client = start()
    installed = destination / source.name
    assert installed.read_bytes() == source.read_bytes()
    assert client.get("/api/v1/kobo/patcher/manifest").json["available"] is True
    replacement = b"MZ locally replaced patcher"
    installed.write_bytes(replacement)
    installed.with_name(installed.name + ".sha256").write_text(
        f"{hashlib.sha256(replacement).hexdigest()}  {installed.name}"
    )
    client = start()
    assert installed.read_bytes() == replacement
    assert client.get("/api/v1/kobo/patcher/download").data == replacement
