from scripts.run_search_benchmark import score_case


def test_score_case_uses_exact_source_record_ids_for_relevance():
    results = [
        {"source_record_id": 901, "book_title": "كتاب آخر"},
        {"source_record_id": 352, "book_title": "تحفة المحتاج"},
        {"source_record_id": 353, "book_title": "تحفة المحتاج"},
        {"source_record_id": 777, "book_title": "كتاب آخر"},
    ]

    score = score_case(results, [353, 352], k=4)

    assert score["judged"] is True
    assert score["precision_at_k"] == 0.5
    assert score["recall_at_k"] == 1.0
    assert score["reciprocal_rank"] == 0.5
    assert score["relevant_found_at_k"] == 2
    assert score["missing_expected_ids"] == []


def test_score_case_reports_missing_relevant_ids_without_fabricating_them():
    score = score_case(
        [{"source_record_id": 901}, {"source_record_id": 352}],
        [353, 352],
        k=5,
    )

    assert score["relevant_found_at_k"] == 1
    assert score["recall_at_k"] == 0.5
    assert score["missing_expected_ids"] == [353]


def test_unjudged_case_is_not_given_a_false_zero_quality_score():
    score = score_case([{"source_record_id": 123}], [], k=5)

    assert score["judged"] is False
    assert score["precision_at_k"] is None
    assert score["recall_at_k"] is None
    assert score["reciprocal_rank"] is None
