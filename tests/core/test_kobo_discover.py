"""Discover tab rows served from BookFlow in the Kobo Store's featured-list shape."""

import json
import sqlite3
from urllib.parse import urlsplit

from flask import Flask

from bookflow.core.kobo_discover import KoboDiscoverService
from bookflow.core.kobo_protocol import book_uuid, store_book
from bookflow.core.kobo_routes import register_kobo_routes

from .test_kobo_services import kobo_fixture as kobo_fixture

LISTS = "/v1/products/featured/?page_index=0&page_size=100&TypesToInclude=book"


def _discover_reader(fixture):
    db_path = fixture["db_path"]
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO user_library (user_id, book_id) VALUES (?, ?)",
            (fixture["user"]["id"], fixture["book_id"]),
        )
        conn.execute(
            "UPDATE books SET metadata_json = ? WHERE id = ?",
            (
                json.dumps({"description": "<p>Eerste &amp; alinea</p><script>x()</script>"}),
                fixture["book_id"],
            ),
        )
        catalog_id = conn.execute(
            "INSERT INTO books (metadata_provider, provider_book_id, title, author, cover_url)"
            " VALUES ('test', 'catalog-only', 'Catalog Only', 'Someone', 'https://covers.invalid/c')"
        ).lastrowid
        conn.execute(
            "INSERT INTO book_files (book_id, storage_path, file_format, size_bytes, sha256,"
            " import_status) VALUES (?, ?, 'epub', 1, ?, 'imported')",
            (catalog_id, str(fixture["storage_root"] / "other.epub"), "b" * 64),
        )
        bestseller_id = conn.execute(
            "INSERT INTO books (metadata_provider, provider_book_id, title, author, cover_url,"
            " isbn_13) VALUES ('test', 'bestseller', 'Finale', 'Michael Berg',"
            " 'https://covers.invalid/f', '9789026359125')"
        ).lastrowid
        hidden_id = conn.execute(
            "INSERT INTO books (metadata_provider, provider_book_id, title, cover_url)"
            " VALUES ('test', 'hidden', 'Hidden', 'https://covers.invalid/h')"
        ).lastrowid
        snapshot_id = conn.execute(
            "INSERT INTO bestseller_snapshots (source_year, source_week, status, is_active,"
            " fetched_at) VALUES (2026, 44, 'completed', 1, '2026-11-01T00:00:00Z')"
        ).lastrowid
        conn.execute(
            "INSERT INTO bestseller_entries (snapshot_id, list_key, list_rank, isbn_13, book_id,"
            " source_title) VALUES (?, 'overall', 1, '9789026359125', ?, 'Finale')",
            (snapshot_id, bestseller_id),
        )
    covers: list[str] = []
    app = Flask(__name__)
    register_kobo_routes(
        app,
        token_service=fixture["tokens"],
        sync_service=fixture["sync"],
        allowed_file_roots=(fixture["storage_root"],),
        tls_guard=lambda: True,
        cover_fetcher=lambda _key, url: covers.append(url) or (b"image", "image/jpeg"),
        discover_service=KoboDiscoverService(str(db_path)),
    )
    token = fixture["tokens"].issue_token(user_id=fixture["user"]["id"])
    other = fixture["tokens"].issue_token(user_id=fixture["other_user"]["id"])
    return {
        "client": app.test_client(),
        "base": f"/api/kobo/{token.token}",
        "other_base": f"/api/kobo/{other.token}",
        "catalog_id": int(catalog_id),
        "bestseller_id": int(bestseller_id),
        "hidden_id": int(hidden_id),
        "covers": covers,
    }


def _names(response):
    assert response.status_code == 200
    return [item["Name"] for item in response.json["Items"]]


def test_rows_follow_library_catalog_and_bestsellers(kobo_fixture):
    reader = _discover_reader(kobo_fixture)
    client, base = reader["client"], reader["base"]
    response = client.get(base + LISTS)
    assert _names(response) == ["Nog niet op je Kobo", "Nieuw in BookFlow", "Bestseller 60"]
    assert response.json["TotalItemCount"] == 3
    assert response.json["VersionCode"] == 2
    assert all(item["ProductTypes"] == ["Book"] for item in response.json["Items"])
    # The reader also asks without the trailing slash.
    assert _names(client.get(base + "/v1/products/featured")) == _names(response)
    # Another user has no library row but sees the shared catalog and suggestions.
    assert _names(client.get(reader["other_base"] + LISTS)) == [
        "Nieuw in BookFlow",
        "Bestseller 60",
    ]
    # Once the book is on the reader it leaves the "not yet on your Kobo" row.
    kobo_fixture["sync"].set_book(
        user_id=kobo_fixture["user"]["id"],
        book_id=kobo_fixture["book_id"],
        file_id=kobo_fixture["file_id"],
    )
    assert _names(client.get(base + LISTS))[0] == "Nieuw in BookFlow"
    audiobooks = client.get(base + "/v1/products/featured/?TypesToInclude=audiobook")
    assert audiobooks.json["Items"] == []
    assert client.get("/api/kobo/invalid" + LISTS).status_code == 401


def test_row_page_uses_store_book_shape_and_paging(kobo_fixture):
    reader = _discover_reader(kobo_fixture)
    client, base = reader["client"], reader["base"]
    rows = {item["Name"]: item["Id"] for item in client.get(base + LISTS).json["Items"]}

    library = client.get(base + f"/v1/products/featured/{rows['Nog niet op je Kobo']}")
    assert library.status_code == 200
    page = library.json
    assert page["TotalItemCount"] == page["ItemCount"] == 1
    assert page["CurrentPageIndex"] == 0
    assert page["Filters"] == {"SubscriptionsAvailable": ["False"], "PreOrders": ["False"]}
    book = page["Items"][0]["Book"]
    identifier = book_uuid(kobo_fixture["book_id"])
    assert book["Id"] == book["ImageId"] == book["CrossRevisionId"] == identifier
    assert book["Title"] == "Synthetic Kobo Book"
    assert book["Contributors"] == "BookFlow"
    assert book["IsFree"] is True
    assert book["Description"] == "<p>Eerste &amp; alinea</p><p>x()</p>"

    bestsellers = client.get(
        base + f"/v1/products/featured/{rows['Bestseller 60']}?page_index=0&page_size=5"
    ).json
    assert bestsellers["ItemsPerPage"] == 5
    assert [item["Book"]["Title"] for item in bestsellers["Items"]] == ["Finale"]
    assert bestsellers["Items"][0]["Book"]["ISBN"] == "9789026359125"
    beyond = client.get(
        base + f"/v1/products/featured/{rows['Nieuw in BookFlow']}?page_index=5&page_size=1"
    ).json
    assert beyond["Items"] == []
    assert beyond["TotalItemCount"] == 2
    assert beyond["TotalPageCount"] == 2

    for unknown in ("bf00d15c-0000-4000-8000-0000000000ff", "not-a-uuid", identifier):
        assert client.get(base + f"/v1/products/featured/{unknown}").status_code == 404
    library_id = rows["Nog niet op je Kobo"]
    assert client.get(f"/api/kobo/invalid/v1/products/featured/{library_id}").status_code == 401


def test_covers_are_served_only_for_books_discover_may_show(kobo_fixture):
    reader = _discover_reader(kobo_fixture)
    client, base = reader["client"], reader["base"]
    for book_id in (reader["catalog_id"], reader["bestseller_id"]):
        response = client.get(base + f"/v1/covers/{book_uuid(book_id)}/150/200/image.jpg")
        assert response.status_code == 200
        assert response.data == b"image"
    assert reader["covers"] == ["https://covers.invalid/c", "https://covers.invalid/f"]
    hidden = book_uuid(reader["hidden_id"])
    assert client.get(base + f"/v1/covers/{hidden}/150/200/image.jpg").status_code == 404
    assert (
        client.get(
            f"/api/kobo/invalid/v1/covers/{book_uuid(reader['catalog_id'])}/1/1/image.jpg"
        ).status_code
        == 401
    )


def test_wishlist_is_empty_and_resources_point_at_bookflow(kobo_fixture):
    reader = _discover_reader(kobo_fixture)
    client, base = reader["client"], reader["base"]
    resources = client.get(base + "/v1/initialization").json["Resources"]
    for key in ("featured_lists", "featured_list", "user_wishlist"):
        assert urlsplit(resources[key]).path.startswith(base + "/")
    wishlist = client.get(base + "/v1/user/wishlist?PageSize=100&PageIndex=0")
    assert wishlist.status_code == 200
    assert wishlist.json["Items"] == []
    assert wishlist.json["TotalItemCount"] == 0
    assert client.get("/api/kobo/invalid/v1/user/wishlist").status_code == 401


def test_store_book_keeps_series_and_omits_empty_values():
    book = store_book(
        {
            "id": 7,
            "title": "De Mergellandmoorden: Finale",
            "author": "",
            "publish_year": 2026,
            "series_name": "De Mergellandmoorden",
            "series_position": 3.0,
            "metadata_json": "not a dict",
        }
    )
    assert book["SeriesName"] == "De Mergellandmoorden"
    assert book["SeriesNumber"] == "3"
    assert book["SeriesNumberFloat"] == 3.0
    assert book["ContributorRoles"] == []
    assert book["Description"] == ""
    assert book["PublicationDate"] == "2026-01-01T00:00:00Z"
    assert book["Slug"] == "de-mergellandmoorden-finale"
