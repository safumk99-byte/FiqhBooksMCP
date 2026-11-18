"""Small, defensive EPUB text extractor for Telegram-imported books.

Original EPUB binaries remain on Telegram. This module extracts ordered spine
content and emits chapter/section chunks for the existing PostgreSQL passage
search index. EPUB files do not reliably encode printed page numbers.
"""
from __future__ import annotations

from html.parser import HTMLParser
from io import BytesIO
import posixpath
import re
from urllib.parse import unquote
from zipfile import BadZipFile, ZipFile
import xml.etree.ElementTree as ET

MAX_ARCHIVE_ENTRIES = 20000
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_EXTRACTED_TEXT = 30 * 1024 * 1024
CHUNK_CHARS = 3200

_BLOCK_TAGS = {
    "address", "article", "blockquote", "br", "dd", "div", "dl", "dt",
    "figcaption", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ol",
    "p", "pre", "section", "table", "td", "th", "tr", "ul",
}
_SKIP_TAGS = {"script", "style", "svg", "head", "noscript"}


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
        text = "".join(self.parts)
        lines = [re.sub(r"\s+", " ", line).strip()
                 for line in text.splitlines()]
        paragraphs = [line for line in lines if line]
        heading = re.sub(r"\s+", " ", " ".join(self.heading_parts)).strip()
        return heading, "\n".join(paragraphs)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _read_xml(archive: ZipFile, name: str) -> ET.Element:
    try:
        return ET.fromstring(archive.read(name))
    except (KeyError, ET.ParseError) as exc:
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


def parse_epub(data: bytes) -> dict:
    """Return OPF metadata and ordered (locator, text) chapter chunks."""
    if not data or not data.startswith(b"PK"):
        raise ValueError("The uploaded file is not a valid EPUB/ZIP file")

    try:
        with ZipFile(BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise ValueError("EPUB contains too many archive entries")
            uncompressed_size = sum(item.file_size for item in entries)
            if uncompressed_size > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("EPUB expands beyond the 100 MB safety limit")

            container = _read_xml(archive, "META-INF/container.xml")
            rootfile = next(
                (node.attrib.get("full-path") for node in container.iter()
                 if _local_name(node.tag) == "rootfile"),
                None,
            )
            if not rootfile or rootfile not in archive.namelist():
                raise ValueError("EPUB package document was not found")

            package = _read_xml(archive, rootfile)
            metadata: dict[str, str] = {}
            for node in package.iter():
                key = _local_name(node.tag)
                if key in {"title", "creator", "publisher", "date", "language"}:
                    value = " ".join("".join(node.itertext()).split())
                    if value and key not in metadata:
                        metadata[key] = value

            manifest: dict[str, str] = {}
            spine_ids: list[str] = []
            for node in package.iter():
                key = _local_name(node.tag)
                if key == "item":
                    item_id = node.attrib.get("id")
                    href = node.attrib.get("href")
                    media = node.attrib.get("media-type", "")
                    properties = node.attrib.get("properties", "")
                    if item_id and href and media in {
                        "application/xhtml+xml", "text/html"
                    } and "nav" not in properties.split():
                        manifest[item_id] = posixpath.normpath(
                            posixpath.join(posixpath.dirname(rootfile), unquote(href.split("#", 1)[0]))
                        )
                elif key == "itemref":
                    item_id = node.attrib.get("idref")
                    if item_id:
                        spine_ids.append(item_id)

            chapters: list[tuple[str, str]] = []
            total_text = 0
            for index, item_id in enumerate(spine_ids, start=1):
                path = manifest.get(item_id)
                if not path or path not in archive.namelist():
                    continue
                extractor = _TextExtractor()
                try:
                    extractor.feed(archive.read(path).decode("utf-8-sig", errors="replace"))
                    extractor.close()
                except (KeyError, Exception) as exc:
                    if isinstance(exc, ValueError):
                        raise
                    continue
                heading, text = extractor.result()
                if not text:
                    continue
                total_text += len(text)
                if total_text > MAX_EXTRACTED_TEXT:
                    raise ValueError("Extracted EPUB text exceeds the 30 MB safety limit")
                chapter_name = heading or f"Section {index}"
                for chunk_no, chunk in enumerate(_split_text(text), start=1):
                    locator = f"{chapter_name} — part {chunk_no}"
                    chapters.append((locator, chunk))

            if not chapters:
                raise ValueError("No readable chapter text was found in this EPUB")

            return {
                "title": metadata.get("title") or "Untitled EPUB",
                "author": metadata.get("creator") or "",
                "publisher": metadata.get("publisher") or "",
                "edition_year": metadata.get("date") or "",
                "language": metadata.get("language") or "ar",
                "chapters": chapters,
            }
    except BadZipFile as exc:
        raise ValueError("The uploaded file is a damaged EPUB archive") from exc
