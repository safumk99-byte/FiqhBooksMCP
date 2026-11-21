"""Run the live-library search quality benchmark against the existing database.

Usage:
    python -m scripts.run_search_benchmark --limit 10

The relevance judgments are intentionally partial. Metrics are therefore
reported as measured against the known seed judgments, not as corpus-wide truth.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.search_engine import search_library


DEFAULT_CASES = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "search_quality_cases.json"


def _title_matches(result: dict, expected_titles: list[str]) -> bool:
    title = str(result.get("book_title") or "").casefold()
    return any(expected.casefold() in title for expected in expected_titles)


def score_case(results: list[dict], expected_titles: list[str], k: int = 5) -> dict:
    """Score one result list against known relevant book-title judgments."""
    if not expected_titles:
        return {
            "judged": False,
            "precision_at_k": None,
            "recall_at_k": None,
            "reciprocal_rank": None,
            "relevant_found_at_k": 0,
        }

    top = results[:k]
    relevant_flags = [_title_matches(row, expected_titles) for row in top]
    found = sum(relevant_flags)
    first_rank = next((i + 1 for i, matched in enumerate(relevant_flags) if matched), None)
    return {
        "judged": True,
        "precision_at_k": round(found / k, 4),
        "recall_at_k": round(found / len(expected_titles), 4),
        "reciprocal_rank": round(1 / first_rank, 4) if first_rank else 0.0,
        "relevant_found_at_k": found,
    }


def run_benchmark(limit: int = 10, cases_path: Path = DEFAULT_CASES) -> dict:
    fixture = json.loads(cases_path.read_text(encoding="utf-8"))
    rows = []
    for case in fixture["cases"]:
        results = search_library(case["query"], limit=limit, source="all")
        score = score_case(results, case.get("expected_titles", []), k=min(5, limit))
        rows.append({
            "id": case["id"],
            "query": case["query"],
            "result_count": len(results),
            "required_results_ok": bool(results) if case.get("require_results") else True,
            "expected_titles": case.get("expected_titles", []),
            "top_results": [
                {
                    "book_title": result.get("book_title"),
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
    summary = {
        "case_count": len(rows),
        "judged_case_count": len(judged),
        "smoke_case_count": len(rows) - len(judged),
        "empty_required_result_cases": [
            row["id"] for row in rows if not row["required_results_ok"]
        ],
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
            "Expected titles are partial, live-verified seed judgments. "
            "Unjudged smoke cases measure retrieval availability only. "
            "A zero score may indicate ranking failure or incomplete local corpus."
        ),
        "cases": rows,
    }
    return summary


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
