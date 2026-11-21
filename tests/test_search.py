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
    assert normalize_search_result_value(bytes([0x61, 0x62, 0x63, 0xD9])) == "abc�"
    assert normalize_search_result_value(memoryview("فقه".encode("utf-8"))) == "فقه"



def test_arabic_fallback_ignores_common_particles_and_keeps_key_terms():
    from app.search import build_arabic_fallback_search

    query, groups, minimum = build_arabic_fallback_search(
        "ولا يجوز لنحو السلس تعليق نحو قارورة يقطر فيها بوله مثلا وهو في الصلاة بل تبطل الصلاة به"
    )

    assert "قارورة:*" in query
    assert "يقطر:*" in query
    assert "بوله:*" in query
    assert "السلس:* | سلس:*" in query
    assert "(ولا:* | لا:*)" in query
    assert len(groups) == 10
    assert minimum == 5


def test_arabic_fallback_counts_variants_as_one_original_term():
    from app.search import build_arabic_fallback_search

    query, groups, minimum = build_arabic_fallback_search("والسلس وقارورة يقطر فيها")
    assert len(groups) == 4
    assert minimum == 2
    assert groups[0] == "والسلس:* | السلس:* | سلس:*"



def test_arabic_legal_negation_is_preserved_and_required():
    from app.search import build_arabic_fallback_search

    query, groups, minimum = build_arabic_fallback_search("لا يجوز")

    assert query == "(لا:*) & (يجوز:*)"
    assert groups == ["لا:*", "يجوز:*"]
    assert minimum == 2


def test_arabic_ruling_verb_is_not_discarded_as_a_stopword():
    from app.search import build_arabic_fallback_search

    query, groups, minimum = build_arabic_fallback_search("يجوز")

    assert query == "(يجوز:*)"
    assert groups == ["يجوز:*"]
    assert minimum == 1


def test_compound_negation_is_expanded_to_its_negation_particle():
    from app.search import build_arabic_fallback_search

    query, groups, minimum = build_arabic_fallback_search("ولا يجوز")

    assert "(ولا:* | لا:*)" in query
    assert "يجوز:*" in query
    assert minimum == 2



def test_query_expansions_match_complete_words_not_substrings():
    from app.search import expand_search_query

    assert "جمع" in expand_search_query("jam")
    assert "جمع" in expand_search_query("jamak")
    assert "جمع الصلاة السفر" in expand_search_query("jamak solat musafir")


def test_interrogative_ma_is_not_forced_as_a_negation_particle():
    from app.search import build_arabic_fallback_search

    query, groups, minimum = build_arabic_fallback_search("ما حكم الصلاة")

    assert "(ما:*)" not in query
    assert "(ما:*) &" not in query
