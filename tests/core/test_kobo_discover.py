"""Discover tab rows served from BookFlow in the Kobo Store's featured-list shape."""

import json
import sqlite3
from urllib.parse import urlsplit

from flask import Flask

from bookflow.core.kobo_discover import KoboDiscoverService
from bookflow.core.kobo_protocol import book_uuid, store_book
from bookflow.core.kobo_routes import register_kobo_routes
from bookflow.core.release_profile_service import ReleaseProfileService

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
            " VALUES ('test', 'catalog-only', 'Catalog Only', 'BookFlow', 'https://covers.invalid/c')"
        ).lastrowid
        conn.execute(
            "INSERT INTO book_files (book_id, storage_path, file_format, size_bytes, sha256,"
            " import_status, content_type) VALUES (?, ?, 'epub', 1, ?, 'imported', 'ebook')",
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

    for unknown in ("bf00d15c-0000-4000-8000-0000000000ff", "not-a-uuid"):
        assert client.get(base + f"/v1/products/featured/{unknown}").status_code == 404
    # A remembered Kobo Store list ("Top 50 Books") falls back to Bestseller 60.
    top50 = client.get(
        base + "/v1/products/featured/81844d90-8f90-40b0-98e4-f0cb6c3534e7"
        "?page_index=0&page_size=2700&sort=Temperature&Filters=%7B%7D&TypesToInclude=book"
    )
    assert top50.status_code == 200
    assert [item["Book"]["Title"] for item in top50.json["Items"]] == ["Finale"]
    assert top50.json["ItemsPerPage"] == 100
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


def test_kobo_plus_tab_and_store_pages_never_error(kobo_fixture):
    reader = _discover_reader(kobo_fixture)
    client, base = reader["client"], reader["base"]
    kobo_plus = client.get(
        base + "/v1/products/featuredforkoboplus/?page_index=0&page_size=100&TypesToInclude=book"
    )
    assert _names(kobo_plus) == _names(client.get(base + LISTS))
    assert client.get(base + "/v1/products/dailydeal").json == []
    identifier = book_uuid(kobo_fixture["book_id"])
    reviews = client.get(base + f"/v1/products/{identifier}/reviews?PageSize=3")
    assert reviews.json["Items"] == []
    assert client.get(base + f"/v1/products/{identifier}/nextread").json["Items"] == []
    shelf = client.get(base + "/v1/user/recommendations?page_index=0&page_size=50")
    assert shelf.json["TotalItemCount"] == 0
    for path in ("/v1/products/featuredforkoboplus/", "/v1/products/dailydeal"):
        assert client.get("/api/kobo/invalid" + path).status_code == 401


def test_product_page_search_and_more_by_author(kobo_fixture):
    reader = _discover_reader(kobo_fixture)
    client, base = reader["client"], reader["base"]
    catalog = book_uuid(reader["catalog_id"])
    product = client.get(base + f"/v1/products/books/{catalog}/?SendToBrowseHistory=true")
    assert product.status_code == 200
    assert product.json["Id"] == catalog
    assert product.json["Title"] == "Catalog Only"
    assert product.json["ApplicableSubscriptions"] == []
    assert product.json["RatingHistogram"] == {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    hidden = book_uuid(reader["hidden_id"])
    assert client.get(base + f"/v1/products/books/{hidden}/").status_code == 404
    assert client.get(base + "/v1/products/books/not-a-uuid/").status_code == 404
    assert client.get(f"/api/kobo/invalid/v1/products/books/{catalog}/").status_code == 401

    more = client.get(
        base
        + f"/v1/products/{book_uuid(kobo_fixture['book_id'])}/recommendations"
        + "?page_index=0&page_size=100"
    ).json
    assert [item["Book"]["Title"] for item in more["Items"]] == ["Catalog Only"]

    search = client.get(
        base + "/v1/products?q=catalog&Filters=%7B%7D&page_index=0&page_size=200"
        "&TypesToInclude=book,audiobook"
    )
    assert search.status_code == 200
    assert [item["Book"]["Title"] for item in search.json["Items"]] == ["Catalog Only"]
    assert search.json["SafeSearch"] is True
    assert client.get(base + "/v1/products?q=").json["Items"] == []


def test_get_button_adds_catalog_book_to_library_and_reader(kobo_fixture):
    reader = _discover_reader(kobo_fixture)
    client, base = reader["client"], reader["base"]
    user_id = kobo_fixture["user"]["id"]
    catalog = book_uuid(reader["catalog_id"])

    response = client.post(base + f"/v1/library/{catalog}", json={})
    assert response.status_code == 200
    assert response.json["Id"] == catalog
    with sqlite3.connect(kobo_fixture["db_path"]) as conn:
        in_library = conn.execute(
            "SELECT 1 FROM user_library WHERE user_id = ? AND book_id = ?",
            (user_id, reader["catalog_id"]),
        ).fetchone()
    assert in_library is not None
    on_reader = kobo_fixture["sync"].get_book_metadata(
        user_id=user_id, book_id=reader["catalog_id"]
    )
    assert on_reader["file_format"] == "epub"
    # The next sync delivers it to the reader.
    synced = client.get(base + "/v1/library/sync").json
    assert catalog in {
        change["NewEntitlement"]["BookEntitlement"]["Id"]
        for change in synced
        if "NewEntitlement" in change
    }

    hidden = book_uuid(reader["hidden_id"])
    assert client.post(base + f"/v1/library/{hidden}", json={}).status_code == 404
    assert client.post(f"/api/kobo/invalid/v1/library/{catalog}", json={}).status_code == 401


def test_get_button_requests_a_bestseller_without_a_file(kobo_fixture):
    reader = _discover_reader(kobo_fixture)
    client, base = reader["client"], reader["base"]
    db_path = kobo_fixture["db_path"]
    user_id = kobo_fixture["user"]["id"]
    bestseller = book_uuid(reader["bestseller_id"])

    # Without an active release profile there is nothing to request with.
    with sqlite3.connect(db_path) as conn:
        conn.execute("UPDATE release_profiles SET is_default = 0, is_active = 0")
    assert client.post(base + f"/v1/library/{bestseller}", json={}).status_code == 409

    ReleaseProfileService(str(db_path)).create(name="Standaard", config=None, is_default=True)
    response = client.post(base + f"/v1/library/{bestseller}", json={})
    assert response.status_code == 200
    assert response.json["Id"] == bestseller
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        requested = conn.execute(
            "SELECT status, note, add_to_kobo_when_available FROM requests"
            " WHERE user_id = ? AND book_id = ?",
            (user_id, reader["bestseller_id"]),
        ).fetchone()
        searches = conn.execute(
            "SELECT COUNT(*) FROM background_jobs WHERE job_type = 'search_request'"
        ).fetchone()[0]
    assert dict(requested) == {
        "status": "pending_approval",
        "note": "Aangevraagd vanaf de Kobo",
        "add_to_kobo_when_available": 1,
    }
    # A pending request waits for an admin before anything is searched.
    assert searches == 0
    # Pressing again reuses the active request.
    assert client.post(base + f"/v1/library/{bestseller}", json={}).status_code == 200
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1

    # An admin's request is approved at once and starts the search.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE users SET role = 'admin' WHERE id = ?", (kobo_fixture["other_user"]["id"],)
        )
    other = client.post(reader["other_base"] + f"/v1/library/{bestseller}", json={})
    assert other.status_code == 200
    with sqlite3.connect(db_path) as conn:
        searches = conn.execute(
            "SELECT COUNT(*) FROM background_jobs WHERE job_type = 'search_request'"
        ).fetchone()[0]
    assert searches == 1


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
