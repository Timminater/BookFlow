"""HTTP contract shared by BookFlow and the self-contained Kobo patcher."""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from flask import Blueprint, Response, jsonify, request, send_file

from bookflow.core.kobo_pairing import KoboPairingService, PairingError, PairingRateLimitError

if TYPE_CHECKING:
    from collections.abc import Callable


class ActorLike(Protocol):
    @property
    def user_id(self) -> int: ...


_MAX_PATCHER_SIZE = 100 * 1024 * 1024


def secure_patcher_cache(cache: Path, *, config_root: Path | None = None) -> Path | None:
    """Create/use a private cache only beneath the configured application root.

    Container deployments are POSIX, where mode 0700 prevents a second local
    account from replacing an EXE and its adjacent checksum together. Windows
    ACL inheritance is controlled by the application-data mapping, so this
    helper still confines the cache path but does not make unsafe ACL guesses.
    """
    try:
        root = (config_root or cache.parent).resolve(strict=False)
        resolved = cache.resolve(strict=False)
        resolved.relative_to(root)
        if resolved == root:
            return None
        resolved.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            mode = stat.S_IMODE(resolved.stat().st_mode)
            if mode & 0o077:
                resolved.chmod(0o700)
            if stat.S_IMODE(resolved.stat().st_mode) & 0o077:
                return None
    except OSError, RuntimeError, ValueError:
        return None
    return resolved


def register_kobo_pairing_routes(
    app: Any,
    *,
    resolve_actor: Callable[[], ActorLike | None],
    service: KoboPairingService,
    patcher_cache: Path,
    config_root: Path | None = None,
    release_api_url: str | None = None,
    release_version: str | None = None,
    bundled_patcher_dir: Path | None = None,
) -> None:
    """Register authenticated code generation and anonymous one-time exchange."""
    blueprint = Blueprint("kobo_pairing", __name__)
    cache_root = secure_patcher_cache(patcher_cache, config_root=config_root)

    def require_pairing_tls() -> tuple[Response, int] | None:
        """Pairing codes are bearer-like secrets; public HTTP is never acceptable."""
        if request.is_secure:
            return None
        try:
            peer = ipaddress.ip_address(request.remote_addr or "")
        except ValueError:
            return jsonify({"error": {"code": "tls_required"}}), 400
        if peer.is_loopback or peer.is_private:
            return None
        return jsonify({"error": {"code": "tls_required"}}), 400

    @blueprint.post("/api/v1/users/me/kobo-pairing-code")
    def create_code() -> tuple[Response, int] | Response:
        tls_failure = require_pairing_tls()
        if tls_failure is not None:
            return tls_failure
        actor = resolve_actor()
        if actor is None:
            return jsonify({"error": {"code": "unauthorized"}}), 401
        origin = request.url_root.rstrip("/")
        try:
            result = service.create_code(user_id=cast("ActorLike", actor).user_id, origin=origin)
        except PairingError as exc:
            return jsonify({"error": {"code": "invalid_origin", "message": str(exc)}}), 400
        return jsonify(result), 201

    @blueprint.post("/api/kobo/pair/claim")
    def claim() -> tuple[Response, int] | Response:
        tls_failure = require_pairing_tls()
        if tls_failure is not None:
            return tls_failure
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or not isinstance(payload.get("code"), str):
            return jsonify({"error": {"code": "invalid_request"}}), 400
        try:
            claimed = service.claim(
                code=payload["code"], source_ip=request.remote_addr or "unknown"
            )
        except PairingRateLimitError:
            return jsonify({"error": {"code": "rate_limited"}}), 429
        except PairingError as exc:
            return jsonify({"error": {"code": "invalid_pairing_code", "message": str(exc)}}), 400
        return jsonify(
            {
                "pairing_session": claimed.session,
                "session": claimed.session,
                "origin": claimed.origin,
                "expires_at": claimed.expires_at,
                "protocol_version": service.PROTOCOL_VERSION,
            }
        )

    @blueprint.post("/api/kobo/pair/complete")
    def complete() -> tuple[Response, int] | Response:
        tls_failure = require_pairing_tls()
        if tls_failure is not None:
            return tls_failure
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": {"code": "invalid_request"}}), 400
        session = payload.get("pairing_session", payload.get("session"))
        if not isinstance(session, str):
            return jsonify({"error": {"code": "invalid_request"}}), 400
        label = payload.get("device_label")
        if label is not None and not isinstance(label, str):
            return jsonify({"error": {"code": "invalid_request"}}), 400
        try:
            return jsonify(service.complete(session=session, device_label=label))
        except PairingError as exc:
            return jsonify({"error": {"code": "invalid_pairing_session", "message": str(exc)}}), 400

    @blueprint.delete("/api/kobo/pair/session")
    def revoke_session() -> tuple[Response, int] | Response:
        tls_failure = require_pairing_tls()
        if tls_failure is not None:
            return tls_failure
        payload = request.get_json(silent=True)
        session = (
            payload.get("pairing_session", payload.get("session"))
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(session, str):
            return jsonify({"error": {"code": "invalid_request"}}), 400
        return jsonify({"revoked": service.revoke_session(session=session)})

    expected_release_version = (release_version or "").strip().removeprefix("v")
    if expected_release_version.casefold() in {"", "n/a", "unknown"}:
        expected_release_version = ""

    def artifact_version(path: Path) -> str | None:
        match = re.fullmatch(r"BookFlowKoboPatcher-([A-Za-z0-9._-]+)-win-x64\.exe", path.name)
        return match.group(1) if match else None

    def read_cached_artifact(path: Path) -> tuple[str, dict[str, Any]] | None:
        """Re-validate cache on every request; never trust a stale local EXE."""
        version = artifact_version(path)
        checksum_path = path.with_name(f"{path.name}.sha256")
        sbom_path = path.with_name(f"{path.name}.spdx.json")
        if version is None or (expected_release_version and version != expected_release_version):
            return None
        try:
            checksum_parts = checksum_path.read_text(encoding="ascii").split()
            if len(checksum_parts) != 2 or checksum_parts[1] != path.name:
                return None
            expected_hash = checksum_parts[0].casefold()
            if not re.fullmatch(r"[0-9a-f]{64}", expected_hash):
                return None
            if path.stat().st_size > _MAX_PATCHER_SIZE:
                return None
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual_hash != expected_hash:
                return None
            sbom = json.loads(sbom_path.read_text(encoding="utf-8-sig"))
        except OSError, ValueError, json.JSONDecodeError:
            return None
        if not isinstance(sbom, dict):
            return None
        metadata = sbom.get("bookflow_kobo_patcher")
        packages = sbom.get("packages")
        if (
            not isinstance(metadata, dict)
            or metadata.get("protocol_version") != service.PROTOCOL_VERSION
        ):
            return None
        if not isinstance(packages, list) or not any(
            isinstance(package, dict)
            and package.get("SPDXID") == "SPDXRef-BookFlowKoboPatcher"
            and package.get("versionInfo") == version
            for package in packages
        ):
            return None
        return actual_hash, sbom

    def patcher_artifact() -> Path | None:
        if cache_root is None:
            return None
        candidates = sorted(
            cache_root.glob("BookFlowKoboPatcher-*-win-x64.exe"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        return next(
            (candidate for candidate in candidates if read_cached_artifact(candidate)), None
        )

    def install_bundled_patcher() -> None:
        """Seed the persistent volume without overwriting operator replacements."""
        if cache_root is None or bundled_patcher_dir is None:
            return
        for source in bundled_patcher_dir.glob("BookFlowKoboPatcher-*-win-x64.exe"):
            if read_cached_artifact(source) is None:
                continue
            destination = cache_root / source.name
            # Never overwrite even an invalid replacement: an administrator
            # may be uploading its sidecars. The next manifest rechecks it.
            if destination.exists():
                continue
            try:
                for suffix in (".sha256", ".spdx.json", ""):
                    target = destination.with_name(destination.name + suffix)
                    with target.open("xb") as stream:
                        stream.write(source.with_name(source.name + suffix).read_bytes())
            except FileExistsError:
                continue

    install_bundled_patcher()

    def refresh_patcher_cache() -> Path | None:
        if not release_api_url or cache_root is None:
            return None
        parsed = urlsplit(release_api_url)
        if parsed.scheme != "https" or parsed.hostname != "api.github.com":
            return None
        request_headers = {"Accept": "application/vnd.github+json", "User-Agent": "BookFlow"}
        with urlopen(Request(release_api_url, headers=request_headers), timeout=10) as response:
            release = json.loads(response.read(1_000_000))
        assets = release.get("assets", []) if isinstance(release, dict) else []
        if not isinstance(assets, list):
            return None
        downloads = {
            str(asset.get("name")): str(asset.get("browser_download_url"))
            for asset in assets
            if isinstance(asset, dict)
        }
        names = sorted(
            name
            for name in downloads
            if re.fullmatch(r"BookFlowKoboPatcher-[A-Za-z0-9._-]+-win-x64\.exe", name)
        )
        if expected_release_version:
            names = [
                name for name in names if artifact_version(Path(name)) == expected_release_version
            ]
        if not names:
            return None
        name = names[-1]
        checksum_url = downloads.get(f"{name}.sha256")
        sbom_url = downloads.get(f"{name}.spdx.json")
        executable_url = downloads[name]
        if checksum_url is None or sbom_url is None:
            return None
        for url in (checksum_url, sbom_url, executable_url):
            target = urlsplit(url)
            if target.scheme != "https" or target.hostname not in {
                "github.com",
                "objects.githubusercontent.com",
            }:
                return None
        with urlopen(Request(checksum_url, headers=request_headers), timeout=10) as response:
            checksum_text = response.read(4096).decode("ascii")
        expected = checksum_text.split()[0].casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            return None
        with urlopen(Request(sbom_url, headers=request_headers), timeout=10) as response:
            sbom_bytes = response.read(1_000_000)
        try:
            sbom = json.loads(sbom_bytes.decode("utf-8-sig"))
        except UnicodeDecodeError, json.JSONDecodeError:
            return None
        version = artifact_version(Path(name))
        metadata = sbom.get("bookflow_kobo_patcher") if isinstance(sbom, dict) else None
        packages = sbom.get("packages") if isinstance(sbom, dict) else None
        if (
            version is None
            or (expected_release_version and version != expected_release_version)
            or not isinstance(metadata, dict)
            or metadata.get("protocol_version") != service.PROTOCOL_VERSION
            or not isinstance(packages, list)
            or not any(
                isinstance(package, dict)
                and package.get("SPDXID") == "SPDXRef-BookFlowKoboPatcher"
                and package.get("versionInfo") == version
                for package in packages
            )
        ):
            return None
        with urlopen(Request(executable_url, headers=request_headers), timeout=30) as response:
            executable = response.read(_MAX_PATCHER_SIZE + 1)
        if (
            len(executable) > _MAX_PATCHER_SIZE
            or hashlib.sha256(executable).hexdigest() != expected
        ):
            return None
        temporary = cache_root / f".{name}.tmp"
        temporary.write_bytes(executable)
        destination = cache_root / name
        temporary.replace(destination)
        destination.with_name(f"{name}.sha256").write_text(f"{expected}  {name}", encoding="ascii")
        destination.with_name(f"{name}.spdx.json").write_bytes(sbom_bytes)
        if read_cached_artifact(destination) is None:
            return None
        return destination

    @blueprint.get("/api/v1/kobo/patcher/manifest")
    def patcher_manifest() -> tuple[Response, int] | Response:
        actor = resolve_actor()
        if actor is None:
            return jsonify({"error": {"code": "unauthorized"}}), 401
        artifact = patcher_artifact()
        if artifact is None:
            try:
                artifact = refresh_patcher_cache()
            except OSError, TimeoutError, ValueError, json.JSONDecodeError:
                artifact = None
        if artifact is None:
            return (
                jsonify(
                    {
                        "available": False,
                        "protocol_version": service.PROTOCOL_VERSION,
                        "manual_endpoint_suffix": "/api/kobo/<token>",
                    }
                ),
                404,
            )
        validated = read_cached_artifact(artifact)
        if validated is None:
            return jsonify({"error": {"code": "patcher_integrity_failed"}}), 503
        digest, _sbom = validated
        return jsonify(
            {
                "available": True,
                "filename": artifact.name,
                "sha256": digest,
                "size_bytes": artifact.stat().st_size,
                "protocol_version": service.PROTOCOL_VERSION,
                "download_url": "/api/v1/kobo/patcher/download",
            }
        )

    @blueprint.get("/api/v1/kobo/patcher/download")
    def patcher_download() -> tuple[Response, int] | Response:
        if resolve_actor() is None:
            return jsonify({"error": {"code": "unauthorized"}}), 401
        artifact = patcher_artifact()
        if artifact is None:
            return jsonify({"error": {"code": "patcher_unavailable"}}), 404
        # Serve a freshly re-hashed in-memory snapshot.  This closes the window
        # between selecting a valid cache entry and Flask opening it for send.
        validated = read_cached_artifact(artifact)
        if validated is None:
            return jsonify({"error": {"code": "patcher_integrity_failed"}}), 503
        expected_hash, _sbom = validated
        try:
            payload = artifact.read_bytes()
        except OSError:
            return jsonify({"error": {"code": "patcher_unavailable"}}), 404
        if len(payload) > _MAX_PATCHER_SIZE or hashlib.sha256(payload).hexdigest() != expected_hash:
            return jsonify({"error": {"code": "patcher_integrity_failed"}}), 503
        return send_file(
            io.BytesIO(payload),
            as_attachment=True,
            download_name="BookFlowKoboPatcher.exe",
            mimetype="application/vnd.microsoft.portable-executable",
            conditional=True,
        )

    app.register_blueprint(blueprint)
