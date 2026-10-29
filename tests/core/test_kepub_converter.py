"""KEPUB cache, pinning, atomic-output, and corruption-regression tests."""

from __future__ import annotations

import hashlib
import subprocess
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bookflow.core.kepub_converter import (
    EPUB_MIME_TYPE,
    KepubConversionError,
    KepubConverter,
    KepubifyBinary,
)


def _write_epub(path: Path, content: str = "chapter") -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", EPUB_MIME_TYPE)
        archive.writestr("META-INF/container.xml", "<container />")
        archive.writestr("OEBPS/content.opf", "<package />")
        archive.writestr("OEBPS/chapter.xhtml", content)


def _converter(tmp_path: Path) -> KepubConverter:
    binary = tmp_path / "kepubify"
    binary.write_bytes(b"pinned kepubify test binary")
    return KepubConverter(
        KepubifyBinary(
            path=binary,
            version="kepubify 4.0.4",
            sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
        ),
        tmp_path / "cache",
        timeout_seconds=5,
        lock_timeout_seconds=10,
    )


def _successful_runner(conversion_calls: list[list[str]], delay_seconds: float = 0.0):
    def run(command, **kwargs):
        assert kwargs["env"] == {
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "TMPDIR": str(Path(kwargs["cwd"])),
        }
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "kepubify 4.0.4\n", "")
        conversion_calls.append(command)
        if delay_seconds:
            time.sleep(delay_seconds)
        output = Path(command[command.index("--output") + 1])
        # The real CLI copies rather than converts if the output ends .epub
        # without .kepub; emulate that behavior instead of hiding regressions.
        if not output.name.endswith(".kepub.epub"):
            output.write_bytes(Path(command[-1]).read_bytes())
            return subprocess.CompletedProcess(command, 0, "copied", "")
        _write_epub(output, "converted")
        return subprocess.CompletedProcess(command, 0, "", "")

    return run


def test_cache_hit_runs_one_conversion_and_uses_argument_array(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.epub"
    _write_epub(source)
    converter = _converter(tmp_path)
    conversion_calls: list[list[str]] = []
    monkeypatch.setattr(
        "bookflow.core.kepub_converter.subprocess.run", _successful_runner(conversion_calls)
    )

    first = converter.convert(source, options={"hyphenate": True, "smarten-punctuation": False})
    second = converter.convert(source, options={"smarten-punctuation": False, "hyphenate": True})

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert first.output_path == second.output_path
    assert first.output_path.read_bytes() != source.read_bytes()
    assert len(conversion_calls) == 1
    assert conversion_calls[0][0].endswith("kepubify")
    assert "--hyphenate" in conversion_calls[0]
    assert "--smarten-punctuation" not in conversion_calls[0]


def test_concurrent_requests_share_one_per_key_conversion(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.epub"
    _write_epub(source)
    converter = _converter(tmp_path)
    conversion_calls: list[list[str]] = []
    runner_lock = threading.Lock()
    runner = _successful_runner(conversion_calls, delay_seconds=0.1)

    def synchronized_runner(command, **kwargs):
        with runner_lock:
            return runner(command, **kwargs)

    monkeypatch.setattr("bookflow.core.kepub_converter.subprocess.run", synchronized_runner)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: converter.convert(source), range(2)))

    assert len(conversion_calls) == 1
    assert sorted(result.cache_hit for result in results) == [False, True]
    assert results[0].output_path == results[1].output_path


def test_changed_source_checksum_creates_a_new_cache_entry(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.epub"
    _write_epub(source, "first")
    converter = _converter(tmp_path)
    conversion_calls: list[list[str]] = []
    monkeypatch.setattr(
        "bookflow.core.kepub_converter.subprocess.run", _successful_runner(conversion_calls)
    )

    first = converter.convert(source)
    _write_epub(source, "changed source")
    second = converter.convert(source)

    assert first.source_sha256 != second.source_sha256
    assert first.output_path != second.output_path
    assert len(conversion_calls) == 2


def test_failure_removes_partial_cache_output(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.epub"
    _write_epub(source)
    converter = _converter(tmp_path)

    def failing_runner(command, **_kwargs):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "kepubify 4.0.4\n", "")
        output = Path(command[command.index("--output") + 1])
        output.write_bytes(b"partial output")
        return subprocess.CompletedProcess(command, 1, "", "failure")

    monkeypatch.setattr("bookflow.core.kepub_converter.subprocess.run", failing_runner)

    with pytest.raises(KepubConversionError, match="conversion failed"):
        converter.convert(source)

    assert not list(converter.cache_directory.glob("*.kepub.epub"))
    assert not list(converter.cache_directory.glob("*.tmp.epub"))


def test_corrupt_success_output_is_rejected_and_removed(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "source.epub"
    _write_epub(source)
    converter = _converter(tmp_path)

    def corrupt_runner(command, **_kwargs):
        if command[-1] == "--version":
            return subprocess.CompletedProcess(command, 0, "kepubify 4.0.4\n", "")
        Path(command[command.index("--output") + 1]).write_bytes(b"not an EPUB")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("bookflow.core.kepub_converter.subprocess.run", corrupt_runner)

    with pytest.raises(KepubConversionError, match="not a ZIP container"):
        converter.convert(source)

    assert not list(converter.cache_directory.glob("*.kepub.epub"))
    assert not list(converter.cache_directory.glob("*.tmp.epub"))
