from io import BytesIO
from zipfile import ZipFile

from app.epub import parse_epub


def make_epub(extra_entries: int = 0) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            '</rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:title>اختبار الفقه</dc:title><dc:creator>مؤلف تجريبي</dc:creator>'
            '<dc:language>ar</dc:language></metadata>'
            '<manifest><item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="ch1"/></spine></package>',
        )
        for index in range(extra_entries):
            archive.writestr(f"OEBPS/assets/placeholder-{index}.bin", b"")
        archive.writestr(
            "OEBPS/chapter1.xhtml",
            '<html xmlns="http://www.w3.org/1999/xhtml"><head><title>كتاب الطهارة</title>'
            '<style>.x{}</style></head><body><h1>كتاب الطهارة</h1>'
            '<p>الوضوء شرط لصحة الصلاة.</p><p>ويجب غسل الوجه واليدين.</p>'
            '<script>alert("ignore")</script></body></html>',
        )
    return output.getvalue()


def test_parse_epub_extracts_ordered_arabic_text_and_metadata():
    result = parse_epub(make_epub())
    assert result["title"] == "اختبار الفقه"
    assert result["author"] == "مؤلف تجريبي"
    assert result["language"] == "ar"
    assert len(result["chapters"]) == 1
    locator, text = result["chapters"][0]
    assert "كتاب الطهارة" in locator
    assert "الوضوء شرط لصحة الصلاة" in text
    assert "alert" not in text


def test_parse_epub_rejects_non_epub_input():
    try:
        parse_epub(b"not an epub")
    except ValueError as exc:
        assert "not a valid EPUB" in str(exc)
    else:
        raise AssertionError("Expected invalid EPUB to be rejected")


def test_parse_epub_accepts_books_with_more_than_2000_archive_entries():
    result = parse_epub(make_epub(extra_entries=2001))
    assert result["title"] == "اختبار الفقه"
    assert result["chapters"]
