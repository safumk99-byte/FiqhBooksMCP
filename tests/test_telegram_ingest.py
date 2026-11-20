import pytest

from app.telegram_ingest import _group_chapters


def test_group_chapters_preserves_spine_order_and_repeated_headings():
    chapters = [
        ("باب الصلاة — part 1", "first section, first chunk"),
        ("باب الصلاة — part 2", "first section, second chunk"),
        ("باب الصلاة — part 1", "second section with same heading"),
        ("باب الزكاة — part 1", "third section"),
    ]

    sections = _group_chapters(chapters)

    assert [section["page_number"] for section in sections] == [1, 2, 3]
    assert [section["label"] for section in sections] == [
        "باب الصلاة", "باب الصلاة", "باب الزكاة"
    ]
    assert [len(section["chunks"]) for section in sections] == [2, 1, 1]
    assert sections[0]["chunks"][1][1] == "first section, second chunk"


def test_group_chapters_rejects_missing_or_out_of_order_parts():
    with pytest.raises(ValueError, match="out of order"):
        _group_chapters([
            ("باب الصلاة — part 1", "first"),
            ("باب الصلاة — part 3", "third"),
        ])


def test_group_chapters_rejects_unknown_locator_format():
    with pytest.raises(ValueError, match="Invalid EPUB passage locator"):
        _group_chapters([("باب الصلاة", "text")])
