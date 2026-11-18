"""Defensive EPUB text extraction with explicit spine-completeness validation."""
from __future__ import annotations

from html.parser import HTMLParser
import codecs
from io import BytesIO
import posixpath
import re
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

MAX_ARCHIVE_ENTRIES = 20000
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_TEXT = 60 * 1024 * 1024
CHUNK_CHARS = 3200

_BLOCK_TAGS = {
    "address", "article", "blockquote", "br", "dd", "div", "dl", "dt",
    "figcaption", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ol",
    "p", "pre", "section", "table", "td", "th", "tr", "ul",
}
_SKIP_TAGS = {"script", "style", "svg", "head", "noscript"}
_TEXT_MEDIA_TYPES = {"application/xhtml+xml", "text/html"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0
        self.heading_depth = 0
        self.heading_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        if tag in {"h1", "h2", "h3", "title"}:
            self.heading_depth += 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
            return
        if self.skip_depth:
            return
        if tag in {"h1", "h2", "h3", "title"} and self.heading_depth:
            self.heading_depth -= 1
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        value = data.strip()
        if not value:
            return
        self.parts.append(value + " ")
        if self.heading_depth:
            self.heading_parts.append(value)

    def result(self) -> tuple[str, str]:
        raw = "".join(self.parts)
        lines = [re.sub(r"\s+", " ", line).strip() for line in raw.splitlines()]
        heading = re.sub(r"\s+", " ", " ".join(self.heading_parts)).strip()
        return heading, "\n".join(line for line in lines if line)



def _decode_document(raw: bytes) -> str:
    """Decode EPUB XHTML/HTML using its BOM or declared character encoding."""
    sample = raw[:4096]
    encoding = None

    xml_decl = re.search(
        br"""<\?xml[^>]*\bencoding\s*=\s*["']([^"']+)["']""",
        sample[:512],
        flags=re.IGNORECASE,
    )
    if xml_decl:
        encoding = xml_decl.group(1).decode("ascii", errors="strict")

    if encoding is None:
        meta_charset = re.search(
            br"""<meta\\b[^>]*\\bcharset\\s*=\\s*["']?([A-Za-z0-9._:-]+)""",
            sample,
            flags=re.IGNORECASE,
        )
        if meta_charset:
            encoding = meta_charset.group(1).decode("ascii", errors="strict")

    if encoding is None:
        meta_http_equiv = re.search(
            br"""<meta\\b(?=[^>]*\\bhttp-equiv\\s*=\\s*["']?content-type)[^>]*>""",
            sample,
            flags=re.IGNORECASE,
        )
        if meta_http_equiv:
            content_attr = re.search(
                br"""\\bcontent\\s*=\\s*["']([^"']*)["']""",
                meta_http_equiv.group(0),
                flags=re.IGNORECASE,
            )
            if content_attr:
                declared = re.search(
                    br"""\\bcharset\\s*=\\s*([A-Za-z0-9._:-]+)""",
                    content_attr.group(1),
                    flags=re.IGNORECASE,
                )
                if declared:
                    encoding = declared.group(1).decode("ascii", errors="strict")

    if encoding is None:
        if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            encoding = "utf-16"
        elif raw.startswith(codecs.BOM_UTF8):
            encoding = "utf-8-sig"
        else:
            encoding = "utf-8"

    try:
        return raw.decode(encoding, errors="strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise ValueError(f"unsupported_or_invalid_encoding:{encoding}") from exc


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _read_xml(archive: ZipFile, name: str) -> ET.Element:
    try:
        return ET.fromstring(archive.read(name))
    except (KeyError, ET.ParseError, UnicodeDecodeError) as exc:
        raise ValueError(f"Invalid EPUB package metadata: {name}") from exc


def _split_text(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    paragraphs = [p.strip() for p in text.splitlines() if p.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        while len(paragraph) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            cut = paragraph.rfind(" ", 0, max_chars + 1)
            if cut < max_chars // 2:
                cut = max_chars
            chunks.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        candidate = f"{current}\n{paragraph}" if current else paragraph
        if len(candidate) > max_chars and current:
            chunks.append(current)
            current = paragraph
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _archive_path(rootfile: str, href: str) -> str:
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        raise ValueError("Spine resource uses an external URL")
    decoded = unquote(parsed.path)
    path = posixpath.normpath(posixpath.join(posixpath.dirname(rootfile), decoded))
    if path == ".." or path.startswith("../") or path.startswith("/"):
        raise ValueError("Spine resource resolves outside the EPUB package")
    return path


def parse_epub(data: bytes) -> dict:
    """Return metadata, ordered text chunks, and a strict extraction audit.

    Every linear spine item must resolve to a supported XHTML/HTML manifest
    resource and yield readable text. Non-linear spine items are counted but
    intentionally excluded from the reading order.
    """
    if not data or not data.startswith(b"PK"):
        raise ValueError("The uploaded file is not a valid EPUB/ZIP file")

    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise ValueError("EPUB contains too many archive entries")
            if sum(item.file_size for item in entries) > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("EPUB expands beyond the 100 MB safety limit")
            names = set(archive.namelist())

            container = _read_xml(archive, "META-INF/container.xml")
            rootfile = next((node.attrib.get("full-path") for node in container.iter()
                             if _local_name(node.tag) == "rootfile"), None)
            if not rootfile:
                raise ValueError("EPUB package document was not declared")
            rootfile = posixpath.normpath(unquote(rootfile))
            if rootfile not in names:
                raise ValueError("EPUB package document was not found")

            package = _read_xml(archive, rootfile)
            metadata: dict[str, str] = {}
            manifest: dict[str, dict[str, str]] = {}
            spine: list[tuple[str, bool]] = []
            for node in package.iter():
                key = _local_name(node.tag)
                if key in {"title", "creator", "publisher", "date", "language"}:
                    value = " ".join("".join(node.itertext()).split())
                    if value and key not in metadata:
                        metadata[key] = value
                elif key == "item":
                    item_id = node.attrib.get("id")
                    href = node.attrib.get("href")
                    if item_id and href:
                        if item_id in manifest:
                            raise ValueError(f"Duplicate EPUB manifest id: {item_id}")
                        manifest[item_id] = {
                            "href": href,
                            "media_type": node.attrib.get("media-type", "").lower(),
                            "properties": node.attrib.get("properties", ""),
                        }
                elif key == "itemref":
                    item_id = node.attrib.get("idref")
                    if item_id:
                        spine.append((item_id, node.attrib.get("linear", "yes").lower() != "no"))

            if not spine:
                raise ValueError("EPUB reading order (spine) is empty")

            chapters: list[tuple[str, str]] = []
            failures: list[dict[str, str]] = []
            linear_expected = 0
            non_linear_skipped = 0
            total_text = 0
            extracted_sections = 0

            for index, (item_id, is_linear) in enumerate(spine, start=1):
                if not is_linear:
                    non_linear_skipped += 1
                    continue
                linear_expected += 1
                item = manifest.get(item_id)
                if item is None:
                    failures.append({"idref": item_id, "reason": "missing_manifest_item"})
                    continue
                if item["media_type"] not in _TEXT_MEDIA_TYPES:
                    failures.append({"idref": item_id, "reason": "unsupported_spine_media_type:" + item["media_type"]})
                    continue
                try:
                    path = _archive_path(rootfile, item["href"])
                    if path not in names:
                        raise ValueError("archive_resource_missing")
                    raw = archive.read(path)
                    decoded = _decode_document(raw)
                    extractor = _TextExtractor()
                    extractor.feed(decoded)
                    extractor.close()
                    heading, text = extractor.result()
                    if not text:
                        raise ValueError("no_readable_text")
                except (KeyError, UnicodeDecodeError, LookupError, ValueError) as exc:
                    reason = str(exc) or type(exc).__name__
                    failures.append({"idref": item_id, "reason": reason[:240]})
                    continue
                total_text += len(text)
                if total_text > MAX_EXTRACTED_TEXT:
                    raise ValueError("Extracted EPUB text exceeds the 60 MB safety limit")
                extracted_sections += 1
                section_name = heading or f"Section {index}"
                for chunk_no, chunk in enumerate(_split_text(text), start=1):
                    chapters.append((f"{section_name} — part {chunk_no}", chunk))

            if failures:
                details = "; ".join(f"{f['idref']}:{f['reason']}" for f in failures[:8])
                suffix = f" (+{len(failures) - 8} more)" if len(failures) > 8 else ""
                raise ValueError(
                    f"EPUB completeness validation failed: {len(failures)} of "
                    f"{linear_expected} linear spine items could not be extracted: {details}{suffix}"
                )
            if not chapters:
                raise ValueError("No readable chapter text was found in this EPUB")

            audit = {
                "validation": "passed",
                "spine_items_total": len(spine),
                "linear_items_expected": linear_expected,
                "linear_items_extracted": extracted_sections,
                "non_linear_items_skipped": non_linear_skipped,
                "extracted_characters": total_text,
                "passage_chunks": len(chapters),
                "failed_items": [],
            }
            return {
                "title": metadata.get("title") or "Untitled EPUB",
                "author": metadata.get("creator") or "",
                "publisher": metadata.get("publisher") or "",
                "edition_year": metadata.get("date") or "",
                "language": metadata.get("language") or "ar",
                "chapters": chapters,
                "audit": audit,
            }
    except BadZipFile as exc:
        raise ValueError("The uploaded file is a damaged EPUB archive") from exc
