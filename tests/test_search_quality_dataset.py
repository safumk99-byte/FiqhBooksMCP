import json
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "search_quality_cases.json"


def test_search_quality_fixture_is_valid_and_non_empty():
    dataset = json.loads(FIXTURE.read_text(encoding="utf-8"))

    assert dataset["dataset_version"] == 1
    assert dataset["cases"]

    case_ids = [case["id"] for case in dataset["cases"]]
    assert len(case_ids) == len(set(case_ids))

    for case in dataset["cases"]:
        assert case["query"].strip()
        assert case["language"] in {"ar", "en", "ml", "ms"}
        assert case["topic"].strip()
        assert case["expected_passage_ids"]
        assert case["expected_locators"]
        assert case["status"] in {
            "pass",
            "partial",
            "needs-ranking-review",
        }


def test_known_ranking_regressions_remain_in_the_evaluation_set():
    dataset = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in dataset["cases"]}

    assert cases["jamak-taqdim-malay"]["status"] == "needs-ranking-review"
    assert cases["jamak-takhir-malay"]["status"] == "needs-ranking-review"
    assert 352 in cases["jamak-taqdim-malay"]["expected_passage_ids"]


def test_urinary_catheter_long_arabic_regression_is_tracked():
    dataset = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cases = {case["id"]: case for case in dataset["cases"]}

    case = cases["urinary-catheter-long-arabic"]
    assert case["expected_passage_ids"] == [565990]
    assert case["expected_locators"]
    assert case["status"] == "needs-ranking-review"
