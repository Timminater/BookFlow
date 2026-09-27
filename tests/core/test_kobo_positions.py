"""Cross-format sentence bookmarks, including changed editions and unsafe XML."""

from zipfile import ZipFile

from bookflow.core.kobo_positions import from_kobo, to_kobo


def write_epub(path, content):
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<container><rootfiles><rootfile full-path="OEBPS/book.opf"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/book.opf",
            '<package><metadata/><manifest><item id="c" href="Text/chapter.xhtml"/></manifest><spine><itemref idref="c"/></spine></package>',
        )
        archive.writestr(
            "OEBPS/Text/chapter.xhtml", "<html><head/><body>" + content + "</body></html>"
        )


def test_round_trip_original_cfi_and_converted_kobo_sentence(tmp_path):
    original, kepub = tmp_path / "book.epub", tmp_path / "book.kepub.epub"
    write_epub(original, "<p>First sentence. Second sentence.</p><p>Next paragraph.</p>")
    write_epub(
        kepub,
        '<div><p><span id="kobo.1.1">First sentence. </span><span id="kobo.1.2">Second sentence.</span></p><p><span id="kobo.2.1">Next paragraph.</span></p></div>',
    )
    bookmark = to_kobo(original, kepub, "epubcfi(/6/2!/4/2,/1:16,/1:20)")
    assert bookmark["Location"] == {
        "Source": "OEBPS/Text/chapter.xhtml",
        "Type": "KoboSpan",
        "Value": "kobo.1.2",
    }
    assert 0 < bookmark["ContentSourceProgressPercent"] < 100
    assert (
        from_kobo(original, kepub, "OEBPS/Text/chapter.xhtml", "kobo.1.2")
        == "epubcfi(/6/2!/4/2/1:16)"
    )


def test_utf16_offsets_and_cfi_assertions(tmp_path):
    original, kepub = tmp_path / "book.epub", tmp_path / "book.kepub.epub"
    write_epub(original, "<p>😀 Hello. Next.</p>")
    write_epub(
        kepub, '<p><span id="kobo.1.1">😀 Hello. </span><span id="kobo.1.2">Next.</span></p>'
    )
    assert to_kobo(original, kepub, "epubcfi(/6/2[c]!/4/2/1:10)")["Location"]["Value"] == "kobo.1.2"
    assert (
        from_kobo(original, kepub, "OEBPS/Text/chapter.xhtml", "kobo.1.2")
        == "epubcfi(/6/2!/4/2/1:10)"
    )


def test_changed_text_unknown_marker_and_invalid_cfi_fall_back(tmp_path):
    original, kepub = tmp_path / "book.epub", tmp_path / "book.kepub.epub"
    write_epub(original, "<p>Original text.</p>")
    write_epub(kepub, '<p><span id="kobo.1.1">Different text.</span></p>')
    assert to_kobo(original, kepub, "epubcfi(/6/2!/4/2/1:0)") is None
    assert from_kobo(original, kepub, "OEBPS/Text/chapter.xhtml", "kobo.1.1") is None
    assert to_kobo(original, kepub, "invalid") is None
    assert from_kobo(original, original, "OEBPS/Text/chapter.xhtml", "kobo.999.1") is None


def test_invalid_archive_and_entity_expansion_are_not_indexed(tmp_path):
    path = tmp_path / "invalid.epub"
    path.write_bytes(b"not a zip")
    assert to_kobo(path, path, "epubcfi(/6/2!/4/2/1:0)") is None
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><container>&x;</container>',
        )
    assert to_kobo(path, path, "epubcfi(/6/2!/4/2/1:0)") is None
