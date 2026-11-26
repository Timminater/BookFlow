"""Provider-independent release candidate normalization, filtering, and scoring."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_SPACE_PATTERN = re.compile(r"\s+")
_PUNCT_PATTERN = re.compile(r"[^\w\s]+", re.UNICODE)
_SUSPICIOUS_EXTENSIONS = frozenset(
    {".bat", ".cmd", ".com", ".exe", ".js", ".lnk", ".msi", ".ps1", ".scr", ".vbs"}
)


def normalize_release_text(value: object) -> str:
    """Return stable text suitable for matching and fingerprints."""
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    normalized = _PUNCT_PATTERN.sub(" ", normalized)
    return _SPACE_PATTERN.sub(" ", normalized).strip()


def _safe_url_identity(value: str) -> str:
    parsed = urlsplit(value)
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.hostname.casefold() if parsed.hostname else "",
            parsed.path,
            parsed.query,
            "",
        )
    )


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    """Normalized search result consumed by all release sources."""

    source: str
    title: str
    download_url: str | None = None
    indexer: str | None = None
    guid: str | None = None
    info_hash: str | None = None
    nzb_id: str | None = None
    author: str | None = None
    isbn: str | None = None
    language: str | None = None
    format: str | None = None
    protocol: str | None = None
    size_bytes: int | None = None
    seeders: int | None = None
    publish_year: int | None = None
    files: tuple[str, ...] = ()
    is_bundle: bool = False
    is_archive: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def source_reference_fingerprint(reference: str | None) -> str | None:
    """Identify a source-native release without exposing the opaque source ID."""
    if reference is None:
        return None
    return f"source-reference:{hashlib.sha256(reference.encode()).hexdigest()}"


def candidate_fingerprint(candidate: ReleaseCandidate) -> str:
    """Build a namespaced stable fingerprint without persisting URL credentials."""
    source = normalize_release_text(candidate.source) or "unknown"
    if candidate.indexer and candidate.guid:
        identity = (
            f"indexer-guid:{normalize_release_text(candidate.indexer)}:{candidate.guid.strip()}"
        )
    elif candidate.info_hash:
        identity = f"infohash:{candidate.info_hash.strip().casefold()}"
    elif candidate.nzb_id:
        identity = f"nzb:{candidate.nzb_id.strip()}"
    elif candidate.download_url:
        url_digest = hashlib.sha256(_safe_url_identity(candidate.download_url).encode()).hexdigest()
        identity = f"url-sha256:{url_digest}"
    else:
        identity = ":".join(
            (
                "release",
                normalize_release_text(candidate.title),
                str(candidate.size_bytes or 0),
            )
        )
    return f"{source}:{identity}"


@dataclass(frozen=True, slots=True)
class ReleaseProfile:
    """Immutable filter and scoring snapshot for an request_process."""

    title: str
    authors: tuple[str, ...] = ()
    isbn: str | None = None
    language: str | None = None
    publish_year: int | None = None
    allowed_formats: tuple[str, ...] = ("epub",)
    allowed_protocols: tuple[str, ...] = ("torrent", "usenet", "direct")
    allowed_indexers: tuple[str, ...] = ()
    allowed_languages: tuple[str, ...] = ()
    max_size_bytes: int | None = None
    min_seeders: int = 0
    positive_terms: tuple[str, ...] = ()
    negative_terms: tuple[str, ...] = ()
    allow_bundles: bool = False
    allow_archives: bool = False
    indexer_priorities: dict[str, int] = field(default_factory=dict)
    minimum_automatic_score: int = 0
    score_weights: dict[str, int] = field(default_factory=dict)
    term_scores: dict[str, int] = field(default_factory=dict)

    def fingerprint(self) -> str:
        payload = {
            "allowed_formats": sorted(value.casefold() for value in self.allowed_formats),
            "allowed_protocols": sorted(value.casefold() for value in self.allowed_protocols),
            "allow_archives": self.allow_archives,
            "allow_bundles": self.allow_bundles,
            "authors": [normalize_release_text(value) for value in self.authors],
            "indexer_priorities": dict(sorted(self.indexer_priorities.items())),
            "isbn": normalize_release_text(self.isbn),
            "language": normalize_release_text(self.language),
            "max_size_bytes": self.max_size_bytes,
            "min_seeders": self.min_seeders,
            "negative_terms": [normalize_release_text(value) for value in self.negative_terms],
            "positive_terms": [normalize_release_text(value) for value in self.positive_terms],
            "publish_year": self.publish_year,
            "title": normalize_release_text(self.title),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class CandidateEvaluation:
    """Persistable result of deterministic hard filters and scoring."""

    fingerprint: str
    accepted: bool
    total_score: int
    components: dict[str, int]
    reject_reasons: tuple[str, ...]
    format_rank: int
    source_priority: int


def evaluate_candidate(
    candidate: ReleaseCandidate,
    profile: ReleaseProfile,
    *,
    blocked_fingerprints: frozenset[str] = frozenset(),
) -> CandidateEvaluation:
    """Apply security/quality filters and return explainable integer scoring."""
    fingerprint = candidate_fingerprint(candidate)
    reasons: list[str] = []
    normalized_title = normalize_release_text(candidate.title)
    expected_title = normalize_release_text(profile.title)
    normalized_author = normalize_release_text(candidate.author)
    expected_authors = tuple(normalize_release_text(author) for author in profile.authors)
    normalized_format = normalize_release_text(candidate.format)
    normalized_protocol = normalize_release_text(candidate.protocol)
    normalized_language = normalize_release_text(candidate.language)
    allowed_formats = tuple(normalize_release_text(value) for value in profile.allowed_formats)
    allowed_protocols = tuple(normalize_release_text(value) for value in profile.allowed_protocols)
    allowed_indexers = tuple(normalize_release_text(value) for value in profile.allowed_indexers)
    allowed_languages = tuple(normalize_release_text(value) for value in profile.allowed_languages)

    if fingerprint in blocked_fingerprints:
        reasons.append("blocked_release")
    if not normalized_title or (expected_title and expected_title not in normalized_title):
        reasons.append("title_mismatch")
    if allowed_formats and normalized_format not in allowed_formats:
        reasons.append("format_not_allowed")
    if allowed_protocols and normalized_protocol not in allowed_protocols:
        reasons.append("protocol_not_allowed")
    if (
        allowed_indexers
        and normalize_release_text(candidate.indexer or candidate.source) not in allowed_indexers
    ):
        reasons.append("indexer_not_allowed")
    if profile.language and normalized_language != normalize_release_text(profile.language):
        reasons.append("language_mismatch")
    if allowed_languages and normalized_language not in allowed_languages:
        reasons.append("language_not_allowed")
    if (
        profile.max_size_bytes is not None
        and candidate.size_bytes is not None
        and candidate.size_bytes > profile.max_size_bytes
    ):
        reasons.append("size_limit_exceeded")
    if candidate.seeders is not None and candidate.seeders < profile.min_seeders:
        reasons.append("insufficient_seeders")
    if candidate.is_bundle and not profile.allow_bundles:
        reasons.append("bundle_not_allowed")
    if candidate.is_archive and not profile.allow_archives:
        reasons.append("archive_not_allowed")
    if any(
        any(filename.casefold().endswith(extension) for extension in _SUSPICIOUS_EXTENSIONS)
        for filename in candidate.files
    ):
        reasons.append("suspicious_executable")

    components: dict[str, int] = {}
    components["isbn"] = (
        100
        if profile.isbn
        and normalize_release_text(candidate.isbn) == normalize_release_text(profile.isbn)
        else 0
    )
    components["title"] = 50 if expected_title and expected_title in normalized_title else 0
    components["author"] = (
        30
        if expected_authors and any(author in normalized_author for author in expected_authors)
        else 0
    )
    components["language"] = (
        20
        if profile.language and normalized_language == normalize_release_text(profile.language)
        else 0
    )
    components["year"] = (
        10
        if profile.publish_year is not None and candidate.publish_year == profile.publish_year
        else 0
    )
    format_rank = (
        len(allowed_formats) - allowed_formats.index(normalized_format)
        if normalized_format in allowed_formats
        else 0
    )
    components["format"] = format_rank * 10
    components["seeders"] = min(max(candidate.seeders or 0, 0), 100)
    source_priority = profile.indexer_priorities.get(candidate.indexer or candidate.source, 0)
    components["source_priority"] = source_priority
    components["positive_terms"] = 5 * sum(
        term in normalized_title
        for term in (normalize_release_text(value) for value in profile.positive_terms)
        if term
    )
    components["negative_terms"] = -20 * sum(
        term in normalized_title
        for term in (normalize_release_text(value) for value in profile.negative_terms)
        if term
    )
    components["bundle_penalty"] = -25 if candidate.is_bundle else 0
    components["archive_penalty"] = -10 if candidate.is_archive else 0
    components["term_scores"] = sum(
        score
        for term, score in profile.term_scores.items()
        if normalize_release_text(term) in normalized_title
    )
    for component, weight in profile.score_weights.items():
        if component in components:
            components[component] *= weight

    total = sum(components.values())
    if total < profile.minimum_automatic_score:
        reasons.append("below_minimum_score")
    return CandidateEvaluation(
        fingerprint=fingerprint,
        accepted=not reasons,
        total_score=total,
        components=components,
        reject_reasons=tuple(sorted(set(reasons))),
        format_rank=format_rank,
        source_priority=source_priority,
    )


def rank_candidates(
    candidates: list[ReleaseCandidate],
    profile: ReleaseProfile,
    *,
    blocked_fingerprints: frozenset[str] = frozenset(),
) -> list[tuple[ReleaseCandidate, CandidateEvaluation]]:
    """Return accepted candidates in a fully deterministic order."""
    evaluated = [
        (
            candidate,
            evaluate_candidate(
                candidate,
                profile,
                blocked_fingerprints=blocked_fingerprints,
            ),
        )
        for candidate in candidates
    ]
    accepted = [item for item in evaluated if item[1].accepted]
    return sorted(
        accepted,
        key=lambda item: (
            -item[1].total_score,
            -item[1].format_rank,
            -item[1].source_priority,
            -(item[0].seeders or 0),
            item[1].fingerprint,
        ),
    )
