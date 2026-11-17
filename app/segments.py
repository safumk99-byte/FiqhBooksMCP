"""Conservative segmentation of Turath composite page text.

Only explicit bracketed source labels create segment boundaries. Unlabelled text
is retained as unclassified rather than guessed to be matn or sharh.
"""
from __future__ import annotations

import re
from html import unescape

_SOURCE_MARKER = re.compile(
    r'(?:ــ\s*)?<span\b(?=[^>]*data-type=["\']title["\'])[^>]*>'
    r'\s*\[([^\]]{1,160})\]\s*</span>',
    flags=re.IGNORECASE,
)
_TITLE_MARKER = re.compile(
    r'<span\b(?=[^>]*data-type=["\']title["\'])[^>]*>(.*?)</span>',
    flags=re.IGNORECASE | re.DOTALL,
)
_TAGS = re.compile(r"<[^>]+>")
_DIVIDERS = re.compile(r"(?:\s*ـ{2,}\s*|\s*_{4,}\s*)")

def _clean(value: str) -> str:
    value = _TAGS.sub(" ", value or "")
    value = unescape(value)
    return re.sub(r"[ \t\r\f\v]+", " ", value).strip()

def _kind(label: str) -> tuple[str, str | None]:
    normalized = re.sub(r"\s+", " ", label).strip()
    if "حاشية" in normalized:
        author = normalized.replace("حاشية", "", 1).strip(" :：-")
        return "hashiyah", author or None
    # Explicit named work label: a commentary/source layer, not inferred from
    # the page's principal author metadata.
    known_shuruh = {"مغني المحتاج", "تحفة المحتاج", "فتح الوهاب"}
    if normalized in known_shuruh:
        return "sharh", None
    return "unclassified", None

def parse_turath_page(content: str) -> list[dict]:
    """Return ordered source segments; ambiguous leading text stays unclassified."""
    raw = content or ""
    markers = list(_SOURCE_MARKER.finditer(raw))
    headings = [_clean(m.group(1)) for m in _TITLE_MARKER.finditer(raw)
                if not m.group(1).strip().startswith("[")]
    fallback_title = headings[0] if headings else None

    if not markers:
        text = _clean(_DIVIDERS.sub(" ", raw))
        return ([{
            "segment_order": 1,
            "segment_type": "unclassified",
            "segment_title": fallback_title,
            "segment_author": None,
            "source_marker": None,
            "attribution_method": "no_explicit_source_marker",
            "content": text,
        }] if text else [])

    result: list[dict] = []
    prefix_raw = _TITLE_MARKER.sub(" ", raw[:markers[0].start()])
    prefix = _clean(_DIVIDERS.sub(" ", prefix_raw))
    if prefix:
        result.append({
            "segment_order": 1,
            "segment_type": "unclassified",
            "segment_title": fallback_title,
            "segment_author": None,
            "source_marker": None,
            "attribution_method": "leading_text_before_explicit_source_marker",
            "content": prefix,
        })

    for index, marker in enumerate(markers):
        end = markers[index + 1].start() if index + 1 < len(markers) else len(raw)
        label = _clean(marker.group(1))
        body = _clean(_DIVIDERS.sub(" ", raw[marker.end():end]))
        if not body:
            continue
        kind, author = _kind(label)
        result.append({
            "segment_order": len(result) + 1,
            "segment_type": kind,
            "segment_title": label,
            "segment_author": author,
            "source_marker": label,
            "attribution_method": "explicit_bracketed_title_marker",
            "content": body,
        })

    for position, segment in enumerate(result, start=1):
        segment["segment_order"] = position
    return result
