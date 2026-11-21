"""Run a corpus-aware search-quality benchmark against the existing database.

Usage:
    python -m scripts.run_search_benchmark --limit 10

The fixture's relevance judgments are partial and may refer to sources that
are no longer indexed locally. Such cases are reported as not_indexed and are
excluded from search-quality averages rather than scored as retrieval failures.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from app.db import close_pool, connection, open_pool
from app.search_engine import search_library

DEFAULT_CASES = (
    Path(__file__).resolve().parent.parent
    / "tests"
    / "fixtures"
    / "search_quality_cases.json"
)


def _locator_matches(record: dict, locator: str | None) -> bool:
    if not locator:
        return True

    turath_match = re.search(
        r"Turath page_id\s+(\d+)\s*;\s*vol\s+([^;]+)\s*;\s*printed page\s+(.+)$",
        locator,
        flags=re.IGNORECASE,
    )
    if turath_match:
        page_id, volume, printed_page = turath_match.groups()
        return (
            record.get("source_type") == "turath"
            and str(record.get("turath_page_id")) == page_id
            and str(record.get("volume") or "").strip() == volume.strip()
            and str(record.get("printed_page") or "").strip() == printed_page.strip()
        )

    if locator.strip().casefold().startswith("page:"):
        return record.get("source_type") == "epub"

    # EPUB locators are stable section labels such as "Section 631 — part 1".
    return (
        record.get("source_type") == "epub"
        and str(record.get("locator") or "").strip() in locator
    )


def resolve_expected_records(
    expected_ids: list[int],
    expected_locators: list[str],
    available_records: list[dict],
) -> tuple[list[dict], list[int]]:
    """Resolve fixture IDs to records that exist locally at the expected locator."""
    resolved = []
    unavailable = []
    for index, expected_id in enumerate(expected_ids):
        locator = expected_locators[index] if index < len(expected_locators) else None
        matches = [
            record for record in available_records
            if int(record["source_record_id"]) == int(expected_id)
            and _locator_matches(record, locator)
        ]
        if matches:
            resolved.append({
                "source_type": matches[0]["source_type"],
                "source_record_id": int(expected_id),
                "expected_locator": locator,
            })
        else:
            unavailable.append(int(expected_id))
    return resolved, unavailable


def score_case(results: list[dict], expected_records: list[dict], k: int = 5) -> dict:
    """Score only gold records confirmed to exist in the local searchable corpus."""
    if not expected_records:
        return {
            "judged": False,
            "precision_at_k": None,
            "recall_at_k": None,
            "reciprocal_rank": None,
            "relevant_found_at_k": 0,
            "observed_records_at_k": [
                {
                    "source_type": row.get("source_type"),
                    "source_record_id": row.get("source_record_id"),
                }
                for row in results[:k]
            ],
        }

    expected = {
        (row["source_type"], int(row["source_record_id"]))
        for row in expected_records
    }
    top = results[:k]
    observed = [
        (row.get("source_type"), int(row["source_record_id"]))
        for row in top
        if row.get("source_record_id") is not None
    ]
    found = len(set(observed) & expected)
    first_rank = next(
        (rank for rank, key in enumerate(observed, start=1) if key in expected),
        None,
    )
    return {
        "judged": True,
        "precision_at_k": round(found / k, 4),
        "recall_at_k": round(found / len(expected), 4),
        "reciprocal_rank": round(1 / first_rank, 4) if first_rank else 0.0,
        "relevant_found_at_k": found,
        "observed_records_at_k": [
            {"source_type": source, "source_record_id": record_id}
            for source, record_id in observed
        ],
        "missing_indexed_expected_ids": sorted(
            record_id for source, record_id in expected
            if (source, record_id) not in set(observed)
        ),
    }


def _load_available_records(expected_ids: list[int]) -> list[dict]:
    if not expected_ids:
        return []
    with connection() as conn:
        rows = conn.execute(
            """
            SELECT
                'epub'::text AS source_type,
                p.id::bigint AS source_record_id,
                p.locator,
                NULL::bigint AS turath_page_id,
                NULL::text AS volume,
                NULL::text AS printed_page
            FROM passages p
            WHERE p.id = ANY(%s::bigint[])
            UNION ALL
            SELECT
                'turath'::text AS source_type,
                s.id::bigint AS source_record_id,
                NULL::text AS locator,
                p.turath_page_id,
                p.volume,
                p.printed_page
            FROM turath_segments s
            JOIN turath_source_pages p ON p.id = s.source_page_id
            WHERE s.id = ANY(%s::bigint[])
            """,
            (expected_ids, expected_ids),
        ).fetchall()
    return [dict(row) for row in rows]


def run_benchmark(limit: int = 10, cases_path: Path = DEFAULT_CASES) -> dict:
    fixture = json.loads(cases_path.read_text(encoding="utf-8"))
    cases = fixture["cases"]
    all_expected_ids = sorted({
        int(value)
        for case in cases
        for value in case.get("expected_passage_ids", [])
    })
    available_records = _load_available_records(all_expected_ids)

    rows = []
    for case in cases:
        expected_ids = [int(value) for value in case.get("expected_passage_ids", [])]
        expected_records, unavailable_ids = resolve_expected_records(
            expected_ids,
            case.get("expected_locators", []),
            available_records,
        )
        results = search_library(case["query"], limit=limit, source="all")
        score = score_case(results, expected_records, k=min(5, limit))
        rows.append({
            "id": case["id"],
            "query": case["query"],
            "topic": case.get("topic"),
            "baseline_status": case.get("status"),
            "result_count": len(results),
            "expected_passage_ids": expected_ids,
            "indexed_expected_records": expected_records,
            "unavailable_expected_ids": unavailable_ids,
            "corpus_coverage_ratio": (
                round(len(expected_records) / len(expected_ids), 4)
                if expected_ids else None
            ),
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
    total_expected = sum(len(row["expected_passage_ids"]) for row in rows)
    total_indexed = sum(len(row["indexed_expected_records"]) for row in rows)
    return {
        "dataset_version": fixture.get("dataset_version"),
        "case_count": len(rows),
        "judged_case_count": len(judged),
        "not_indexed_case_count": len(rows) - len(judged),
        "expected_source_count": total_expected,
        "indexed_expected_source_count": total_indexed,
        "expected_source_corpus_coverage": (
            round(total_indexed / total_expected, 4) if total_expected else None
        ),
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
            "Quality metrics include only expected records confirmed in the "
            "current local corpus at the expected locator. Missing source records "
            "are reported separately and are not counted as search failures."
        ),
        "cases": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Measure FiqhBooksMCP search quality.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    open_pool()
    try:
        report = run_benchmark(limit=max(5, min(args.limit, 100)))
    finally:
        close_pool()
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
