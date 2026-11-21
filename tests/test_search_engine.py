from contextlib import contextmanager

from app import search_engine


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def execute(self, sql, params):
        self.calls.append((sql, params))
        return _Result(self.rows)


def _row(source_type, source_id, phrase_match, matched_terms, total_terms, rank):
    return {
        "source_type": source_type,
        "source_id": source_id,
        "local_book_id": 57 if source_type == "epub" else None,
        "book_title": "كتاب اختبار",
        "edition_title": "test.epub" if source_type == "epub" else None,
        "page_id": None,
        "page_number": None,
        "locator": "Section 1" if source_type == "epub" else None,
        "turath_book_id": None if source_type == "epub" else 9059,
        "turath_page_id": None if source_type == "epub" else 123,
        "sequential_index": None if source_type == "epub" else 10,
        "volume": None if source_type == "epub" else "2",
        "printed_page": None if source_type == "epub" else "227",
        "segment_order": None if source_type == "epub" else 1,
        "segment_type": None if source_type == "epub" else "unclassified",
        "segment_title": None,
        "segment_author": None,
        "source_marker": None,
        "attribution_method": None if source_type == "epub" else "no_explicit_source_marker",
        "content": "نص فقهي للاختبار",
        "matched_terms": matched_terms,
        "total_terms": total_terms,
        "phrase_match": phrase_match,
        "rank": rank,
    }


def test_unified_search_merges_epub_and_turath_and_prefers_phrase_match(monkeypatch):
    rows = [
        _row("epub", "565990", 0, 6, 8, 0.9),
        _row("turath", "9059:123:1", 1, 4, 8, 0.2),
    ]
    fake_conn = _Connection(rows)

    @contextmanager
    def fake_connection():
        yield fake_conn

    monkeypatch.setattr(search_engine, "connection", fake_connection)

    results = search_engine.search_library(
        "ولا يجوز لمن به سلس البول ان يعلق قارورة يقطر فيها"
    )

    assert [item["source_type"] for item in results] == ["turath", "epub"]
    assert results[0]["phrase_match"] == 1
    assert results[1]["matched_terms"] == 6
    assert results[0]["coverage_ratio"] == 0.5
    assert len(fake_conn.calls) == 1
    assert "UNION ALL" in fake_conn.calls[0][0]
    assert "coverage.matched_terms" in fake_conn.calls[0][0]
    assert "phraseto_tsquery" in fake_conn.calls[0][0]
    assert "strpos(" not in fake_conn.calls[0][0]
    assert fake_conn.calls[0][1][1:3] == (
        "ولا يجوز لمن به سلس البول ان يعلق قارورة يقطر فيها",
        "ولا يجوز لمن به سلس البول ان يعلق قارورة يقطر فيها",
    )


def test_unified_search_deduplicates_same_source_record_across_expansions(monkeypatch):
    rows = [_row("epub", "565990", 0, 3, 5, 0.4)]
    fake_conn = _Connection(rows)

    @contextmanager
    def fake_connection():
        yield fake_conn

    monkeypatch.setattr(search_engine, "connection", fake_connection)

    results = search_engine.search_library("jamak solat musafir")

    keys = [(item["source_type"], item["source_id"]) for item in results]
    assert len(keys) == len(set(keys))


def test_unified_search_validates_source_and_segment_type(monkeypatch):
    @contextmanager
    def fake_connection():
        yield _Connection([])

    monkeypatch.setattr(search_engine, "connection", fake_connection)

    try:
        search_engine.search_library("فقه", source="unknown")
    except ValueError as exc:
        assert "source must be" in str(exc)
    else:
        raise AssertionError("invalid source should be rejected")

    try:
        search_engine.search_library("فقه", segment_type="unknown")
    except ValueError as exc:
        assert "Unsupported segment_type" in str(exc)
    else:
        raise AssertionError("invalid segment type should be rejected")


def test_unified_search_returns_empty_for_blank_query(monkeypatch):
    @contextmanager
    def fake_connection():
        raise AssertionError("database should not be queried for a blank query")
        yield

    monkeypatch.setattr(search_engine, "connection", fake_connection)
    assert search_engine.search_library("   ") == []
