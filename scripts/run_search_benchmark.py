"""Run the production search-quality benchmark against the existing database.

Usage:
    python -m scripts.run_search_benchmark --limit 10

Expected passage IDs are a manually reviewed, partial relevance set. The report
never treats unjudged results as irrelevant or a search hit as a fiqh ruling.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.search_engine import search_library

DEFAULT_CASES = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "search_quality_cases.json"
)


def score_case(results: list[dict], expected_ids: list[int], k: int = 5) -> dict:
    """Score results against exact database passage/segment IDs."""
    if not expected_ids:
        return {
            "judged": False,
            "precision_at_k": None,
            "recall_at_k": None,
            "reciprocal_rank": None,
            "relevant_found_at_k": 0,
            "observed_ids_at_k": [
                int(row["source_record_id"])
                for row in results[:k]
                if row.get("source_record_id") is not None
            ],
        }

    top = results[:k]
    expected = {int(value) for value in expected_ids}
    observed = [
        int(row["source_record_id"])
        for row in top
        if row.get("source_record_id") is not None
    ]
    found_ranks = [
        rank for rank, value in enumerate(observed, start=1)
        if value in expected
    ]
    found = len(set(observed) & expected)
    first_rank = min(found_ranks) if found_ranks else None
    return {
        "judged": True,
        "precision_at_k": round(found / k, 4),
        "recall_at_k": round(found / len(expected), 4),
        "reciprocal_rank": round(1 / first_rank, 4) if first_rank else 0.0,
        "relevant_found_at_k": found,
        "observed_ids_at_k": observed,
        "missing_expected_ids": sorted(expected - set(observed)),
    }


def run_benchmark(limit: int = 10, cases_path: Path = DEFAULT_CASES) -> dict:
    fixture = json.loads(cases_path.read_text(encoding="utf-8"))
    rows = []
    for case in fixture["cases"]:
        results = search_library(case["query"], limit=limit, source="all")
        score = score_case(
            results,
            case.get("expected_passage_ids", []),
            k=min(5, limit),
        )
        rows.append({
            "id": case["id"],
            "query": case["query"],
            "topic": case.get("topic"),
            "baseline_status": case.get("status"),
            "result_count": len(results),
            "expected_passage_ids": case.get("expected_passage_ids", []),
            "expected_locators": case.get("expected_locators", []),
            "top_results": [
                {
                    "source_type": result.get("source_type"),
                    "source_record_id": result.get("source_record_id"),
                    "book_title": result.get("book_title"),
                    "turath_page_id": result.get("turath_page_id"),
                    "volume": result.get("volume"),
                    "printed_page": result.get("printed_page"),
                    "segment_order": result.get("segment_order"),
                    "matched_terms": result.get("matched_terms"),
                    "total_terms": result.get("total_terms"),
                    "phrase_match": result.get("phrase_match"),
                    "search_score": result.get("search_score"),
                }
                for result in results[:5]
            ],
            **score,
        })

    judged = [row for row in rows if row["judged"]]
    return {
        "dataset_version": fixture.get("dataset_version"),
        "case_count": len(rows),
        "judged_case_count": len(judged),
        "unjudged_case_count": len(rows) - len(judged),
        "mean_precision_at_5": (
            round(sum(row["precision_at_k"] for row in judged) / len(judged), 4)
            if judged else None
        ),
        "mean_recall_at_5": (
            round(sum(row["recall_at_k"] for row in judged) / len(judged), 4)
            if judged else None
        ),
        "mean_reciprocal_rank": (
            round(sum(row["reciprocal_rank"] for row in judged) / len(judged), 4)
            if judged else None
        ),
        "note": (
            "Judgments are partial and cover only indexed passages known when "
            "the fixture was curated. Missing hits may reflect search ranking "
            "or absent local corpus; inspect coverage before changing ranking."
        ),
        "cases": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure FiqhBooksMCP search quality.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_benchmark(limit=max(5, min(args.limit, 100)))
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
