from scripts.run_search_benchmark import score_case


def test_score_case_reports_precision_recall_and_reciprocal_rank():
    results = [
        {"book_title": "كتاب غير متوقع"},
        {"book_title": "نهاية المطلب"},
        {"book_title": "العزيز شرح الوجيز"},
        {"book_title": "كتاب آخر"},
    ]

    score = score_case(results, ["نهاية المطلب", "العزيز شرح الوجيز"], k=4)

    assert score == {
        "judged": True,
        "precision_at_k": 0.5,
        "recall_at_k": 1.0,
        "reciprocal_rank": 0.5,
        "relevant_found_at_k": 2,
    }


def test_score_case_marks_unjudged_smoke_query_without_fake_zero_score():
    score = score_case([{"book_title": "أي كتاب"}], [], k=5)

    assert score["judged"] is False
    assert score["precision_at_k"] is None
    assert score["recall_at_k"] is None
    assert score["reciprocal_rank"] is None


def test_score_case_does_not_count_unrelated_title_substrings():
    score = score_case(
        [{"book_title": "المجموع شرح المهذب"}, {"book_title": "كتاب آخر"}],
        ["المهذب"],
        k=2,
    )

    assert score["relevant_found_at_k"] == 1
