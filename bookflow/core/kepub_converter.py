"""Pinned, cache-safe conversion of EPUB files to Kobo KEPUB containers.

The converter deliberately accepts only a local, checksummed ``kepubify``
binary.  It never starts a shell and does not inherit the BookFlow process
environment, which can contain provider and identity-provider credentials.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from bookflow.core.catalog_service import CatalogService
    from bookflow.core.file_store import FileStore

EPUB_MIME_TYPE: Final = "application/epub+zip"
_SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")
_OPTION_NAME_PATTERN: Final = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_MAX_ARCHIVE_MEMBERS: Final = 10_000
_MAX_ARCHIVE_UNCOMPRESSED_BYTES: Final = 2 * 1024 * 1024 * 1024
_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


class KepubConversionError(RuntimeError):
    """Raised when a KEPUB conversion cannot safely produce a cache entry."""


@dataclass(frozen=True)
class KepubifyBinary:
    """The explicit binary identity that must be verified before use."""

    path: Path
    version: str
    sha256: str


@dataclass(frozen=True)
class KepubConversionResult:
    """One validated KEPUB cache object."""

    output_path: Path
    source_sha256: str
    converter_version: str
    options_hash: str
    cache_key: str
    cache_hit: bool


@dataclass(frozen=True)
class CatalogKepubConversion:
    """A cache result plus its central ``book_files`` record."""

    conversion: KepubConversionResult
    file_record: dict[str, object]


def kepub_cache_key(source_sha256: str, converter_version: str, options_hash: str) -> str:
    """Return a stable, unambiguous cache key for one conversion input."""
    _validate_sha256(source_sha256, "source_sha256")
    _validate_sha256(options_hash, "options_hash")
    version = converter_version.strip()
    if not version:
        raise ValueError("converter_version is required")
    return hashlib.sha256(f"{source_sha256}\0{version}\0{options_hash}".encode()).hexdigest()


def kepub_options_hash(options: Mapping[str, str | int | bool] | None = None) -> str:
    """Hash canonical, restricted kepubify options for cache invalidation."""
    normalized = _normalize_options(options or {})
    # Version the output contract as well as CLI options. Older temporary
    # .epub names made kepubify copy the input rather than convert it.
    payload = json.dumps(
        {"output": "kepub-v2", "options": normalized}, separators=(",", ":"), sort_keys=True
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class KepubConverter:
    """Produce atomic KEPUB cache entries with a verified kepubify binary."""

    def __init__(
        self,
        binary: KepubifyBinary,
        cache_directory: Path,
        *,
        timeout_seconds: float = 300.0,
        lock_timeout_seconds: float = 360.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if lock_timeout_seconds <= timeout_seconds:
            raise ValueError("lock_timeout_seconds must exceed timeout_seconds")
        self._binary = KepubifyBinary(
            path=Path(binary.path),
            version=binary.version.strip(),
            sha256=binary.sha256.strip().lower(),
        )
        if not self._binary.version:
            raise ValueError("kepubify version is required")
        _validate_sha256(self._binary.sha256, "kepubify binary sha256")
        self._cache_directory = self._prepare_cache_directory(Path(cache_directory))
        self._timeout_seconds = timeout_seconds
        self._lock_timeout_seconds = lock_timeout_seconds

    @property
    def cache_directory(self) -> Path:
        """Return the protected directory holding conversion cache files."""
        return self._cache_directory

    @property
    def converter_version(self) -> str:
        """Return the declared, verified kepubify version string."""
        return self._binary.version

    def convert(
        self,
        source_path: Path,
        *,
        options: Mapping[str, str | int | bool] | None = None,
        expected_source_sha256: str | None = None,
    ) -> KepubConversionResult:
        """Convert one EPUB safely and return its immutable cache object.

        ``expected_source_sha256`` should be supplied by a ``book_files`` row
        when this is called from catalog code.  It prevents a stale database
        record from being associated with a changed source path.
        """
        source = self._validate_epub_path(Path(source_path), source=True)
        source_sha256 = self._sha256(source)
        if expected_source_sha256 is not None:
            _validate_sha256(expected_source_sha256, "expected_source_sha256")
            if source_sha256 != expected_source_sha256.lower():
                raise KepubConversionError(
                    "Source file checksum no longer matches its book_files row"
                )

        normalized_options = _normalize_options(options or {})
        options_hash = kepub_options_hash(normalized_options)
        cache_key = kepub_cache_key(source_sha256, self._binary.version, options_hash)
        output_path = self._cache_directory / f"{cache_key}.kepub.epub"

        self._verify_binary()
        with self._key_lock(cache_key):
            if output_path.exists():
                try:
                    self._validate_epub_path(output_path, source=False)
                except KepubConversionError:
                    output_path.unlink(missing_ok=True)
                else:
                    return KepubConversionResult(
                        output_path=output_path,
                        source_sha256=source_sha256,
                        converter_version=self._binary.version,
                        options_hash=options_hash,
                        cache_key=cache_key,
                        cache_hit=True,
                    )

            temporary_output = (
                self._cache_directory / f".{cache_key}.{uuid.uuid4().hex}.tmp.kepub.epub"
            )
            try:
                self._run_converter(source, temporary_output, normalized_options)
                self._validate_epub_path(temporary_output, source=False)
                self._ensure_source_is_unchanged(source, source_sha256)
                temporary_output.replace(output_path)
            except Exception:
                temporary_output.unlink(missing_ok=True)
                output_path.unlink(missing_ok=True)
                raise

            return KepubConversionResult(
                output_path=output_path,
                source_sha256=source_sha256,
                converter_version=self._binary.version,
                options_hash=options_hash,
                cache_key=cache_key,
                cache_hit=False,
            )

    @staticmethod
    def _prepare_cache_directory(path: Path) -> Path:
        if not path.is_absolute():
            raise KepubConversionError("KEPUB cache directory must be absolute")
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise KepubConversionError("KEPUB cache directory must be a real directory")
        resolved = path.resolve(strict=True)
        if resolved != path.absolute():
            raise KepubConversionError("KEPUB cache directory must not resolve through a link")
        return resolved

    def _verify_binary(self) -> None:
        binary = self._binary.path
        if not binary.is_absolute() or binary.is_symlink() or not binary.is_file():
            raise KepubConversionError("kepubify binary must be an absolute, regular file")
        if self._sha256(binary) != self._binary.sha256:
            raise KepubConversionError("kepubify binary checksum does not match the configured pin")
        try:
            completed = subprocess.run(
                [str(binary), "--version"],
                check=False,
                cwd=self._cache_directory,
                env=self._limited_environment(),
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KepubConversionError("Unable to execute the pinned kepubify binary") from exc
        reported_version = completed.stdout.strip()
        if completed.returncode != 0 or reported_version != self._binary.version:
            raise KepubConversionError("kepubify version does not match the configured pin")

    def _run_converter(
        self, source: Path, temporary_output: Path, options: Mapping[str, str | int | bool]
    ) -> None:
        command = [
            str(self._binary.path),
            "--output",
            str(temporary_output),
            *_options_to_arguments(options),
            str(source),
        ]
        try:
            completed = subprocess.run(
                command,
                check=False,
                cwd=self._cache_directory,
                env=self._limited_environment(),
                capture_output=True,
                text=True,
                timeout=self._timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise KepubConversionError("kepubify conversion timed out") from exc
        except OSError as exc:
            raise KepubConversionError("Unable to start kepubify conversion") from exc
        if completed.returncode != 0:
            raise KepubConversionError("kepubify conversion failed")
        if not temporary_output.is_file() or temporary_output.is_symlink():
            raise KepubConversionError("kepubify did not create a regular output file")

    def _validate_epub_path(self, path: Path, *, source: bool) -> Path:
        label = "Source EPUB" if source else "KEPUB output"
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise KepubConversionError(f"{label} must be an absolute, regular file")
        resolved = path.resolve(strict=True)
        if source and resolved.suffix.lower() != ".epub":
            raise KepubConversionError("Source file must use the .epub extension")
        if not source and not resolved.name.endswith(".kepub.epub"):
            raise KepubConversionError("KEPUB output must use a controlled .kepub.epub name")
        validate_epub_container(resolved, label)
        return resolved

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as file:
            for chunk in iter(lambda: file.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _ensure_source_is_unchanged(self, source: Path, expected_sha256: str) -> None:
        if self._sha256(source) != expected_sha256:
            raise KepubConversionError("Source file changed while kepubify was running")

    def _limited_environment(self) -> dict[str, str]:
        """Avoid passing service credentials and user-controlled environment to kepubify."""
        return {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(self._cache_directory),
        }

    @contextmanager
    def _key_lock(self, cache_key: str) -> Iterator[None]:
        lock = _thread_lock_for(str(self._cache_directory / cache_key))
        with lock:
            lock_path = self._cache_directory / f".{cache_key}.lock"
            self._acquire_process_lock(lock_path)
            try:
                yield
            finally:
                lock_path.unlink(missing_ok=True)

    def _acquire_process_lock(self, lock_path: Path) -> None:
        deadline = time.monotonic() + self._lock_timeout_seconds
        while True:
            try:
                descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as exc:
                if time.monotonic() >= deadline:
                    raise KepubConversionError(
                        "Timed out waiting for the KEPUB cache lock"
                    ) from exc
                time.sleep(0.05)
                continue
            try:
                os.write(descriptor, f"{os.getpid()}\n".encode("ascii"))
            finally:
                os.close(descriptor)
            return


class KepubCatalogService:
    """Bridge a verified KEPUB cache result into central file storage/catalog."""

    def __init__(
        self, converter: KepubConverter, file_store: FileStore, catalog_service: CatalogService
    ) -> None:
        self._converter = converter
        self._file_store = file_store
        self._catalog_service = catalog_service

    def convert_and_register(
        self,
        *,
        book_id: int,
        source_file_id: int,
        source_path: Path,
        source_sha256: str,
        options: Mapping[str, str | int | bool] | None = None,
    ) -> CatalogKepubConversion:
        """Convert a recorded EPUB and persist the output as a derived KEPUB."""
        normalized_options = _normalize_options(options or {})
        options_hash = kepub_options_hash(normalized_options)
        cache_key = kepub_cache_key(source_sha256, self._converter.converter_version, options_hash)
        existing = self._catalog_service.find_kepub_conversion(
            source_sha256=source_sha256,
            converter_version=self._converter.converter_version,
            options_hash=options_hash,
        )
        if existing is not None:
            return CatalogKepubConversion(
                conversion=KepubConversionResult(
                    output_path=Path(str(existing["storage_path"])),
                    source_sha256=source_sha256,
                    converter_version=self._converter.converter_version,
                    options_hash=options_hash,
                    cache_key=cache_key,
                    cache_hit=True,
                ),
                file_record=existing,
            )
        conversion = self._converter.convert(
            source_path,
            options=normalized_options,
            expected_source_sha256=source_sha256,
        )
        published_file = self._file_store.publish(
            conversion.output_path,
            source_roots=(self._converter.cache_directory,),
            file_format="kepub",
        )
        file_record = self._catalog_service.register_kepub_conversion(
            book_id=book_id,
            source_file_id=source_file_id,
            published_file=published_file,
            converter_version=conversion.converter_version,
            options_hash=conversion.options_hash,
        )
        return CatalogKepubConversion(conversion=conversion, file_record=file_record)

    def convert_entitled_for_user(
        self,
        *,
        user_id: int,
        book_id: int,
        source_file_id: int,
        options: Mapping[str, str | int | bool] | None = None,
    ) -> int:
        """Derive KEPUB on demand, then grant the requesting user that file.

        The source entitlement is checked before conversion.  The resulting
        central KEPUB is shared through the conversion cache, but entitlement
        remains explicit and per user.
        """
        source = self._catalog_service.get_entitled_book_file(
            user_id=user_id,
            book_id=book_id,
            file_id=source_file_id,
        )
        if source is None:
            raise KepubConversionError("Source EPUB is not entitled for this user")
        if str(source["file_format"]).lower() != "epub":
            raise KepubConversionError("Only EPUB files can be converted to KEPUB")
        conversion = self.convert_and_register(
            book_id=book_id,
            source_file_id=source_file_id,
            source_path=Path(str(source["storage_path"])),
            source_sha256=str(source["sha256"]),
            options=options,
        )
        derived_file_id = conversion.file_record["id"]
        if not isinstance(derived_file_id, int):
            raise KepubConversionError("KEPUB catalog record has no numeric file ID")
        self._catalog_service.grant_file_entitlement(user_id=user_id, file_id=derived_file_id)
        return derived_file_id


def _validate_sha256(value: str, label: str) -> None:
    if not _SHA256_PATTERN.fullmatch(value.lower()):
        raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")


def _normalize_options(options: Mapping[str, str | int | bool]) -> dict[str, str | int | bool]:
    normalized: dict[str, str | int | bool] = {}
    for raw_name, value in options.items():
        name = raw_name.strip().lower()
        if not _OPTION_NAME_PATTERN.fullmatch(name) or name in {"o", "output", "help", "version"}:
            raise ValueError(f"Unsupported kepubify option: {raw_name!r}")
        if not isinstance(value, str | int | bool) or (isinstance(value, str) and "\0" in value):
            raise ValueError(f"Unsupported value for kepubify option {raw_name!r}")
        normalized[name] = value
    return normalized


def _options_to_arguments(options: Mapping[str, str | int | bool]) -> list[str]:
    arguments: list[str] = []
    for name, value in options.items():
        if value is False:
            continue
        arguments.append(f"--{name}")
        if value is not True:
            arguments.append(str(value))
    return arguments


def _thread_lock_for(cache_key: str) -> threading.Lock:
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(cache_key, threading.Lock())


def validate_epub_container(path: Path, label: str = "EPUB") -> None:
    """Validate the structural and path-safety requirements of an EPUB container."""
    if not zipfile.is_zipfile(path):
        raise KepubConversionError(f"{label} is not a ZIP container")
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > _MAX_ARCHIVE_MEMBERS:
                raise KepubConversionError(f"{label} contains too many ZIP members")
            if sum(member.file_size for member in members) > _MAX_ARCHIVE_UNCOMPRESSED_BYTES:
                raise KepubConversionError(f"{label} exceeds the uncompressed size limit")
            names = {member.filename for member in members}
            if "mimetype" not in names or "META-INF/container.xml" not in names:
                raise KepubConversionError(f"{label} is missing required EPUB members")
            if not any(name.lower().endswith(".opf") for name in names):
                raise KepubConversionError(f"{label} is missing an EPUB package document")
            for member in members:
                member_path = Path(member.filename)
                if member_path.is_absolute() or ".." in member_path.parts:
                    raise KepubConversionError(f"{label} contains an unsafe ZIP member path")
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise KepubConversionError(f"{label} contains a symbolic-link ZIP member")
            if archive.read("mimetype") != EPUB_MIME_TYPE.encode("ascii"):
                raise KepubConversionError(f"{label} MIME type is not {EPUB_MIME_TYPE}")
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise KepubConversionError(f"Unable to validate {label} ZIP container") from exc
