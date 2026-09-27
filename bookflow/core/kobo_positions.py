"""Translate EPUB CFI positions and Kobo sentence markers without changing books.

Only chapters whose normalized text matches are translated. Unsupported CFIs,
malformed EPUBs and editions with different text retain percentage-only sync.
"""

from __future__ import annotations

import posixpath
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING
from urllib.parse import unquote
from xml.etree.ElementTree import Element, ParseError
from zipfile import BadZipFile, ZipFile

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class TextPosition:
    path: str
    text: str
    start: int


@dataclass(frozen=True)
class Chapter:
    href: str
    package_path: str
    text: str
    positions: tuple[TextPosition, ...]
    markers: tuple[tuple[str, int], ...]


def _plain(text: str) -> str:
    return "".join(text.split())


def _chapter(root: Element, href: str, package_path: str) -> Chapter:
    positions: list[TextPosition] = []
    markers: list[tuple[str, int]] = []
    chunks: list[str] = []
    length = 0

    def append(text: str | None, path: str) -> None:
        nonlocal length
        if text:
            positions.append(TextPosition(path, text, length))
            normalized = _plain(text)
            chunks.append(normalized)
            length += len(normalized)

    def walk(element: Element, path: str) -> None:
        if element.tag.rsplit("}", 1)[-1] in {"head", "script", "style"}:
            return
        identifier = element.get("id", "")
        if re.fullmatch(r"kobo\.\d+\.\d+", identifier):
            markers.append((identifier, length))
        append(element.text, path + "/1")
        for index, child in enumerate(element):
            walk(child, f"{path}/{2 * (index + 1)}")
            append(child.tail, f"{path}/{2 * (index + 1) + 1}")

    walk(root, "")
    return Chapter(href, package_path, "".join(chunks), tuple(positions), tuple(markers))


def _xml(archive: ZipFile, name: str) -> Element:
    if archive.getinfo(name).file_size > 10 * 1024 * 1024:
        raise ValueError("EPUB document exceeds position indexing limit")
    return fromstring(archive.read(name))


@lru_cache(maxsize=16)
def _index(path: str, size: int, modified: int) -> tuple[Chapter, ...]:
    # File metadata is part of the key so replacing a file invalidates its map.
    del size, modified
    with ZipFile(path) as archive:
        if sum(info.file_size for info in archive.infolist()) > 64 * 1024 * 1024:
            raise ValueError("EPUB exceeds position indexing limit")
        container = _xml(archive, "META-INF/container.xml")
        rootfile = container.find(".//{*}rootfile")
        if rootfile is None:
            raise ValueError("Missing EPUB package")
        package_name = rootfile.attrib["full-path"]
        package = _xml(archive, package_name)
        manifest = {
            item.attrib["id"]: item.attrib["href"]
            for item in package.findall("{*}manifest/{*}item")
        }
        spine = package.find("{*}spine")
        if spine is None:
            raise ValueError("Missing EPUB spine")
        spine_step = (list(package).index(spine) + 1) * 2
        chapters = []
        for index, item in enumerate(spine):
            href = unquote(manifest[item.attrib["idref"]].split("#", 1)[0])
            member = posixpath.normpath(posixpath.join(posixpath.dirname(package_name), href))
            chapters.append(
                _chapter(_xml(archive, member), member, f"/{spine_step}/{2 * (index + 1)}")
            )
        return tuple(chapters)


def _chapters(path: Path) -> tuple[Chapter, ...]:
    stat = path.stat()
    return _index(str(path), stat.st_size, stat.st_mtime_ns)


def _cfi_location(chapters: tuple[Chapter, ...], cfi: str) -> tuple[Chapter, int] | None:
    if not cfi.startswith("epubcfi(") or not cfi.endswith(")"):
        return None
    # Assertions are optional hints; path indexes refer to the original EPUB.
    value = re.sub(r"\[(?:\^.|[^\]])*\]", "", cfi[8:-1])
    parts = value.split(",")
    value = parts[0] + parts[1] if len(parts) == 3 else value
    if "!" not in value:
        return None
    package_path, content_path = value.split("!", 1)
    match = re.fullmatch(r"((?:/\d+)+)(?::(\d+))?", content_path)
    if match is None:
        return None
    chapter = next((item for item in chapters if item.package_path == package_path), None)
    if chapter is None:
        return None
    node_path, offset = match.group(1), int(match.group(2) or 0)
    for position in chapter.positions:
        if position.path == node_path:
            # EPUB CFI text offsets count UTF-16 code units, as does JavaScript.
            prefix = position.text.encode("utf-16-le")[: offset * 2].decode("utf-16-le", "ignore")
            return chapter, position.start + len(_plain(prefix))
        if position.path.startswith(node_path + "/"):
            return chapter, position.start
    return None


def _matching(chapters: tuple[Chapter, ...], chapter: Chapter) -> Chapter | None:
    return next(
        (item for item in chapters if item.href == chapter.href and item.text == chapter.text), None
    )


_MAP_ERRORS = (OSError, ValueError, KeyError, BadZipFile, ParseError, DefusedXmlException)


def to_kobo(source: Path, exported: Path, cfi: str) -> dict[str, object] | None:
    """Return a real KoboSpan at or immediately before the original CFI."""
    try:
        location = _cfi_location(_chapters(source), cfi)
        if location is None:
            return None
        original, offset = location
        chapter = _matching(_chapters(exported), original)
        if chapter is None or not chapter.markers:
            return None
        marker = chapter.markers[0][0]
        for identifier, start in chapter.markers:
            if start > offset:
                break
            marker = identifier
        return {
            "Location": {"Source": chapter.href, "Type": "KoboSpan", "Value": marker},
            "ContentSourceProgressPercent": 100 * offset / max(1, len(chapter.text)),
        }
    except _MAP_ERRORS:
        return None


def from_kobo(source: Path, exported: Path, href: str, marker: str) -> str | None:
    """Resolve a Kobo sentence marker to a CFI in the unmodified web EPUB."""
    try:
        chapter = next((item for item in _chapters(exported) if item.href == unquote(href)), None)
        if chapter is None:
            return None
        offset = next(
            (start for identifier, start in chapter.markers if identifier == marker), None
        )
        original = _matching(_chapters(source), chapter)
        if offset is None or original is None:
            return None
        for position in original.positions:
            if not _plain(position.text) or position.start + len(_plain(position.text)) <= offset:
                continue
            remaining = offset - position.start
            units = 0
            for character in position.text:
                if not character.isspace():
                    if remaining <= 0:
                        break
                    remaining -= 1
                units += len(character.encode("utf-16-le")) // 2
            return f"epubcfi({original.package_path}!{position.path}:{units})"
    except _MAP_ERRORS:
        return None
    return None
