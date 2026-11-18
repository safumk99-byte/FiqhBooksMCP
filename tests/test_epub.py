from io import BytesIO
from zipfile import ZipFile

import pytest

from app.epub import CHUNK_CHARS, parse_epub


def make_epub(*, missing: bool = False, empty: bool = False,
              corrupt: bool = False, non_linear: bool = False,
              long_text: bool = False) -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        second_ref = '<itemref idref="ch2"/>'
        linear_ref = '<itemref idref="nav" linear="no"/>' if non_linear else ''
        archive.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            '<dc:title>اختبار الفقه</dc:title><dc:creator>مؤلف تجريبي</dc:creator>'
            '<dc:language>ar</dc:language></metadata><manifest>'
            '<item id="ch1" href="chapter1.xhtml" media-type="application/xhtml+xml"/>'
            + ('<item id="ch2" href="chapter2.xhtml" media-type="application/xhtml+xml"/>'
               if not missing else '')
            + ('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
               if non_linear else '')
            + '</manifest><spine><itemref idref="ch1"/>' + second_ref
            + linear_ref + '</spine></package>',
        )
        body = ('<html><body><h1>كتاب الطهارة</h1><p>' +
                (('كلمة ' * (CHUNK_CHARS + 100)) if long_text else
                 'الوضوء شرط لصحة الصلاة.') + '</p></body></html>')
        archive.writestr("OEBPS/chapter1.xhtml", body)
        if not missing:
            if corrupt:
                archive.writestr("OEBPS/chapter2.xhtml", b'\xff\xfe')
            elif empty:
                archive.writestr("OEBPS/chapter2.xhtml", '<html><body><script>x</script></body></html>')
            else:
                archive.writestr("OEBPS/chapter2.xhtml", '<html><body><p>باب الصلاة</p></body></html>')
        if non_linear:
            archive.writestr("OEBPS/nav.xhtml", '<html><body>Navigation</body></html>')
    return output.getvalue()


def test_parse_epub_extracts_ordered_arabic_text_and_audit():
    result = parse_epub(make_epub())
    assert result["title"] == "اختبار الفقه"
    assert result["author"] == "مؤلف تجريبي"
    assert result["language"] == "ar"
    assert len(result["chapters"]) == 2
    assert "كتاب الطهارة" in result["chapters"][0][0]
    assert "الوضوء شرط لصحة الصلاة" in result["chapters"][0][1]
    assert result["audit"]["validation"] == "passed"
    assert result["audit"]["linear_items_expected"] == 2
    assert result["audit"]["linear_items_extracted"] == 2


def test_rejects_non_epub_input():
    with pytest.raises(ValueError, match="not a valid EPUB"):
        parse_epub(b"not an epub")


@pytest.mark.parametrize("kwargs,reason", [
    ({"missing": True}, "missing_manifest_item"),
    ({"empty": True}, "no_readable_text"),
    ({"corrupt": True}, "utf-8"),
])
def test_rejects_incomplete_linear_spine(kwargs, reason):
    with pytest.raises(ValueError, match=reason):
        parse_epub(make_epub(**kwargs))


def test_non_linear_spine_item_is_explicitly_excluded():
    result = parse_epub(make_epub(non_linear=True))
    assert result["audit"]["non_linear_items_skipped"] == 1
    assert result["audit"]["linear_items_expected"] == 2
    assert result["audit"]["linear_items_extracted"] == 2


def test_long_text_is_split_within_chunk_limit():
    result = parse_epub(make_epub(long_text=True))
    assert len(result["chapters"]) >= 2
    assert all(len(content) <= CHUNK_CHARS for _, content in result["chapters"])
