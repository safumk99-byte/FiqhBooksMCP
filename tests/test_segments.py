from app.segments import parse_turath_page


def test_three_layer_tuhfat_page_keeps_two_hashiyah_sources():
    page = (
        "MAIN COMMENTARY TEXT ــ "
        '<span data-type="title">[حاشية الشرواني]</span> SHIRWANI TEXT ــ '
        '<span data-type="title">[حاشية ابن قاسم العبادي]</span> ABBADI TEXT'
    )
    segments = parse_turath_page(page)
    assert [s["segment_type"] for s in segments] == [
        "unclassified", "hashiyah", "hashiyah"
    ]
    assert [s["segment_title"] for s in segments[1:]] == [
        "حاشية الشرواني", "حاشية ابن قاسم العبادي"
    ]
    assert segments[1]["segment_author"] == "الشرواني"
    assert segments[2]["segment_author"] == "ابن قاسم العبادي"
    assert segments[2]["content"] == "ABBADI TEXT"


def test_named_mughni_marker_is_a_sharh_segment():
    page = (
        '<span data-type="title">كتاب الطهارة</span> ــ '
        '<span data-type="title">[مغني المحتاج]</span> شرح الطهارة'
    )
    segments = parse_turath_page(page)
    assert len(segments) == 1
    assert segments[0]["segment_type"] == "sharh"
    assert segments[0]["segment_title"] == "مغني المحتاج"
    assert segments[0]["content"] == "شرح الطهارة"


def test_unmarked_page_is_preserved_as_unclassified():
    segments = parse_turath_page(
        '<span data-type="title">باب الوضوء</span> نص في الوضوء'
    )
    assert len(segments) == 1
    assert segments[0]["segment_type"] == "unclassified"
    assert segments[0]["segment_title"] == "باب الوضوء"
    assert "نص في الوضوء" in segments[0]["content"]


def test_empty_source_section_is_skipped_and_order_is_contiguous():
    page = (
        '<span data-type="title">[حاشية أولى]</span> ــ '
        '<span data-type="title">[حاشية ثانية]</span> نص ثان'
    )
    segments = parse_turath_page(page)
    assert len(segments) == 1
    assert segments[0]["segment_order"] == 1
    assert segments[0]["segment_title"] == "حاشية ثانية"


def test_unknown_marker_is_not_assigned_an_author():
    segments = parse_turath_page(
        '<span data-type="title">[مصدر غير معروف]</span> نص المصدر'
    )
    assert segments[0]["segment_type"] == "unclassified"
    assert segments[0]["segment_author"] is None



def test_turath_sequential_indexes_uses_official_print_page_mapping():
    from app.main import _turath_sequential_indexes

    info = {
        "indexes": {
            "page_map": [None, None, "1,2", "1,3", "1,4"],
            "print_pg_to_pg": {"1,2": 3, "1,3": 4, "1,4": 5},
        }
    }
    assert _turath_sequential_indexes(info) == [3, 4, 5]


def test_turath_sequential_indexes_ignores_null_page_map_padding():
    from app.main import _turath_sequential_indexes

    info = {"indexes": {"page_map": [None, None, "1,2", "1,3"]}}
    assert _turath_sequential_indexes(info) == [3, 4]
