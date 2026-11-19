from app.search import build_fallback_search


def test_fallback_requires_two_matches_for_longer_queries():
    query, minimum = build_fallback_search(["qasr", "travel", "prayer"])
    assert query == "qasr:* | travel:* | prayer:*"
    assert minimum == 2


def test_fallback_keeps_single_match_for_short_queries():
    query, minimum = build_fallback_search(["qasr", "travel"])
    assert query == "qasr:* | travel:*"
    assert minimum == 1


def test_fallback_deduplicates_tokens():
    query, minimum = build_fallback_search(["qasr", "qasr", "travel"])
    assert query == "qasr:* | travel:*"
    assert minimum == 1


def test_fallback_returns_empty_for_no_tokens():
    assert build_fallback_search([]) == ("", 0)


def test_malay_jamak_travel_phrase_expands_to_related_arabic_terms():
    from app.search import expand_search_query

    expanded = expand_search_query("jamak solat musafir")
    assert "جمع الصلاة السفر" in expanded


def test_malay_qasar_phrase_expands_to_related_arabic_terms():
    from app.search import expand_search_query

    expanded = expand_search_query("solat qasar")
    assert "قصر الصلاة السفر" in expanded


def test_malay_jamak_taqdim_expands_to_specific_arabic_phrase():
    from app.search import expand_search_query

    expanded = expand_search_query("jamak taqdim")
    assert "جمع التقديم" in expanded


def test_malay_jamak_takhir_expands_to_specific_arabic_phrase():
    from app.search import expand_search_query

    expanded = expand_search_query("jamak takhir")
    assert "جمع التاخير" in expanded


def test_tashkeel_is_removed_from_expansion_matching():
    from app.search import expand_search_query

    assert "جمع التاخير" in expand_search_query("jamak ta'khir")


def test_search_result_bytes_are_decoded_safely():
    from app.search import normalize_search_result_value

    assert normalize_search_result_value("الصلاة".encode("utf-8")) == "الصلاة"
    assert normalize_search_result_value(b"abc\\xd9") == "abc�"
    assert normalize_search_result_value(memoryview("فقه".encode("utf-8"))) == "فقه"
