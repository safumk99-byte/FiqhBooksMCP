from scripts.run_search_benchmark import (
    resolve_expected_records,
    score_case,
)


def test_score_case_uses_source_type_and_exact_record_id():
    expected = [
        {"source_type": "turath", "source_record_id": 352},
        {"source_type": "turath", "source_record_id": 353},
    ]
    results = [
        {"source_type": "epub", "source_record_id": 352},
        {"source_type": "turath", "source_record_id": 352},
        {"source_type": "turath", "source_record_id": 353},
        {"source_type": "epub", "source_record_id": 777},
    ]

    score = score_case(results, expected, k=4)

    assert score["judged"] is True
    assert score["precision_at_k"] == 0.5
    assert score["recall_at_k"] == 1.0
    assert score["reciprocal_rank"] == 0.5
    assert score["relevant_found_at_k"] == 2


def test_expected_turath_locator_must_match_page_volume_and_printed_page():
    available = [
        {
            "source_type": "turath",
            "source_record_id": 119,
            "turath_page_id": 41,
            "volume": "1",
            "printed_page": "40",
            "locator": None,
        }
    ]

    resolved, unavailable = resolve_expected_records(
        [119],
        ["Turath page_id 447; vol 1; printed page 531"],
        available,
    )

    assert resolved == []
    assert unavailable == [119]


def test_page_locator_does_not_accept_a_turath_id_collision():
    available = [
        {
            "source_type": "turath",
            "source_record_id": 119,
            "turath_page_id": 41,
            "volume": "1",
            "printed_page": "40",
            "locator": None,
        }
    ]

    resolved, unavailable = resolve_expected_records(
        [119],
        ["page:117"],
        available,
    )

    assert resolved == []
    assert unavailable == [119]


def test_matching_expected_turath_record_is_counted_as_indexed():
    available = [
        {
            "source_type": "turath",
            "source_record_id": 352,
            "turath_page_id": 446,
            "volume": "1",
            "printed_page": "530",
            "locator": None,
        }
    ]

    resolved, unavailable = resolve_expected_records(
        [352],
        ["Turath page_id 446; vol 1; printed page 530"],
        available,
    )

    assert resolved == [
        {
            "source_type": "turath",
            "source_record_id": 352,
            "expected_locator": "Turath page_id 446; vol 1; printed page 530",
        }
    ]
    assert unavailable == []


def test_unjudged_case_is_not_given_a_false_zero_quality_score():
    score = score_case([{"source_type": "epub", "source_record_id": 123}], [], k=5)

    assert score["judged"] is False
    assert score["precision_at_k"] is None
    assert score["recall_at_k"] is None
    assert score["reciprocal_rank"] is None
