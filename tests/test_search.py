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
