"""Kobo Store API wire shapes, separate from BookFlow's internal change journal.

The field names follow the device protocol also implemented by Calibre-Web
(cps/kobo.py). No Kobo account credentials or requests are forwarded upstream.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

_BOOK_PREFIX = "bf000000-0000-4000-8000-"


def book_uuid(book_id: int) -> str:
    """Stable, reversible namespace; every lookup still checks user entitlement."""
    return f"{_BOOK_PREFIX}{book_id:012x}"


def parse_book_uuid(value: str) -> int:
    canonical = str(UUID(value))
    if not canonical.startswith(_BOOK_PREFIX):
        raise ValueError("Unknown Kobo book identifier")
    return int(canonical[-12:], 16)


def timestamp(value: object) -> str:
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        parsed = datetime(1970, 1, 1, tzinfo=UTC)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def resources(endpoint: str) -> dict[str, Any]:
    """Advertise absolute resource URLs, including literal device placeholders."""
    paths = {
        "library_sync": "/v1/library/sync",
        "library_metadata": "/v1/library/{Ids}/metadata",
        "library_book": "/v1/user/library/books/{LibraryItemId}",
        "reading_state": "/v1/library/{Ids}/state",
        "delete_entitlement": "/v1/library/{Ids}",
        "device_auth": "/v1/auth/device",
        "device_refresh": "/v1/auth/refresh",
        "add_device": "/v1/user/add-device",
        "exchange_auth": "/v1/auth/exchange",
        "get_tests_request": "/v1/analytics/gettests",
        "post_analytics_event": "/v1/analytics/event",
        "configuration_data": "/v1/configuration",
        "affiliaterequest": "/v1/affiliate",
        "assets": "/v1/assets",
        "deals": "/v1/deals",
        "book_subscription": "/v1/products/books/subscriptions",
        "user_profile": "/v1/user/profile",
        "user_platform": "/v1/user/platform",
        "user_loyalty_benefits": "/v1/user/loyalty/benefits",
        "oauth_host": "/oauth",
        "tags": "/v1/library/tags",
        "image_url_template": "/v1/covers/{ImageId}/{Width}/{Height}/image.jpg",
        "image_url_quality_template": "/v1/covers/{ImageId}/{Width}/{Height}/{Quality}/{IsGreyscale}/image.jpg",
    }
    return {
        **{key: endpoint + path for key, path in paths.items()},
        "image_host": endpoint,
        "store_host": "www.kobo.com",
        "store_home": "www.kobo.com/{region}/{language}",
        # Nickel stores these as string-valued OneStoreServices settings.
        # Omitting use_one_store can leave the reader on its legacy sync flow.
        "use_one_store": "True",
        "kobo_onestorelibrary_enabled": "False",
        "kobo_subscriptions_enabled": "False",
        "kobo_superpoints_enabled": "False",
        "kobo_wishlist_enabled": "False",
        "kobo_nativeborrow_enabled": "False",
    }


def entitlement(book_id: int, modified: object, *, removed: bool = False) -> dict[str, Any]:
    identifier = book_uuid(book_id)
    date = timestamp(modified)
    return {
        "Id": identifier,
        "RevisionId": identifier,
        "CrossRevisionId": identifier,
        "Accessibility": "Full",
        "ActivePeriod": {"From": date},
        "Created": date,
        "LastModified": date,
        "IsRemoved": removed,
        "IsHiddenFromArchive": False,
        "IsLocked": False,
        "OriginCategory": "Imported",
        "Status": "Active",
    }


def metadata(book: dict[str, Any], endpoint: str) -> dict[str, Any]:
    identifier = book_uuid(int(book["id"]))
    formats = ["KEPUB"] if str(book["file_format"]).lower() == "kepub" else ["EPUB3", "EPUB"]
    author = str(book.get("author") or "")
    year = book.get("publish_year")
    publication = f"{int(year):04d}-01-01T00:00:00Z" if year else timestamp(book["updated_at"])
    return {
        "RevisionId": identifier,
        "CrossRevisionId": identifier,
        "EntitlementId": identifier,
        "WorkId": identifier,
        "Title": book["title"],
        "Contributors": [author] if author else [],
        "ContributorRoles": [{"Name": author}] if author else [],
        "Language": book.get("language") or "en",
        "PublicationDate": publication,
        "Publisher": {"Name": "", "Imprint": ""},
        "Description": "",
        "CoverImageId": identifier,
        "DownloadUrls": [
            {
                "Format": item,
                "Size": book["size_bytes"],
                "Platform": "Generic",
                "Url": f"{endpoint}/v1/files/{book['file_id']}/download",
            }
            for item in formats
        ],
        "Categories": ["00000000-0000-0000-0000-000000000001"],
        "Genre": "00000000-0000-0000-0000-000000000001",
        "CurrentDisplayPrice": {"CurrencyCode": "EUR", "TotalAmount": 0},
        "CurrentLoveDisplayPrice": {"TotalAmount": 0},
        "ExternalIds": [],
        "IsEligibleForKoboLove": False,
        "IsInternetArchive": False,
        "IsPreOrder": False,
        "IsSocialEnabled": False,
        "PhoneticPronunciations": {},
    }


def reading_state(book_id: int, state: dict[str, Any] | None) -> dict[str, Any]:
    progress = float(state["progress"]) if state else 0
    modified = timestamp(
        (state.get("reader_updated_at") or state.get("updated_at")) if state else None
    )
    status = "Finished" if progress >= 100 else "Reading" if progress > 0 else "ReadyToRead"
    result: dict[str, Any] = {
        "EntitlementId": book_uuid(book_id),
        "Created": modified,
        "LastModified": modified,
        "PriorityTimestamp": modified,
        "StatusInfo": {"Status": status, "LastModified": modified},
        "Statistics": {"LastModified": modified},
        "CurrentBookmark": {
            "ProgressPercent": progress,
            "LastModified": modified,
        },
    }
    if state and state.get("source") == "kobo" and isinstance(state.get("device_state"), dict):
        device = state["device_state"]
        # Keep the actual EPUB bookmark when the progress still belongs to it.
        bookmark = device.get("CurrentBookmark")
        if isinstance(bookmark, dict) and bookmark.get("ProgressPercent") == progress:
            result["CurrentBookmark"].update(bookmark)
        if isinstance(device.get("Statistics"), dict):
            result["Statistics"].update(device["Statistics"])
    return result


def parse_reading_state(
    payload: Any, identifier: str, stored: dict[str, Any] | None
) -> tuple[dict[str, Any], float]:
    if not isinstance(payload, dict):
        raise TypeError("A reading state object is required")
    states = payload.get("ReadingStates")
    if not isinstance(states, list) or len(states) != 1 or not isinstance(states[0], dict):
        raise ValueError("One reading state is required")
    state = states[0]
    if state.get("EntitlementId", identifier) != identifier:
        raise ValueError("Mismatching entitlement")
    if len(json.dumps(state)) > 65536:
        raise ValueError("Reading state is too large")
    bookmark = state.get("CurrentBookmark")
    status = state.get("StatusInfo")
    bookmark = {} if bookmark is None else bookmark
    status = {} if status is None else status
    if not isinstance(bookmark, dict) or not isinstance(status, dict):
        raise TypeError("Invalid bookmark or status")
    progress = bookmark.get("ProgressPercent", stored["progress"] if stored else 0)
    if status.get("Status") == "Finished":
        progress = 100
    if (
        isinstance(progress, bool)
        or not isinstance(progress, (int, float))
        or not 0 <= progress <= 100
    ):
        raise ValueError("Invalid reading progress")
    return state, float(progress)
