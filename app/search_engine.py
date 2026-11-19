"""Unified hybrid retrieval across EPUB passages and indexed Turath segments."""

from __future__ import annotations

from app.db import connection
from app.search import (
    build_arabic_fallback_search,
    expand_search_query,
    normalize_search_result_value,
)


def search_library(
    query: str,
    limit: int = 20,
    source: str = "all",
    turath_book_id: int | None = None,
    segment_type: str | None = None,
) -> list[dict]:
    """Search all locally indexed text sources and merge candidates by relevance.

    Retrieval always evaluates the conservative term-coverage fallback. Exact
    phrase presence is a ranking signal, not a gate that can suppress broader
    but relevant candidates.
    """
    limit = max(1, min(int(limit), 100))
    if source not in {"all", "epub", "turath"}:
        raise ValueError("source must be 'all', 'epub', or 'turath'")
    if segment_type and segment_type not in {
        "matn", "sharh", "hashiyah", "editorial", "fatwa", "unclassified"
    }:
        raise ValueError("Unsupported segment_type")

    alternatives = expand_search_query(query)
    if not alternatives:
        return []

    candidates: dict[tuple[str, str], dict] = {}
    with connection() as conn:
        for alternative in alternatives:
            tsquery, term_groups, minimum_matches = build_arabic_fallback_search(alternative)
            if not tsquery:
                continue

            rows = conn.execute(
                """
                WITH passage_hits AS (
                    SELECT
                        'epub'::text AS source_type,
                        p.id::text AS source_id,
                        b.id::bigint AS local_book_id,
                        b.title AS book_title,
                        e.title AS edition_title,
                        p.page_id,
                        pg.page_number,
                        p.locator,
                        NULL::bigint AS turath_book_id,
                        NULL::bigint AS turath_page_id,
                        NULL::integer AS sequential_index,
                        NULL::text AS volume,
                        NULL::text AS printed_page,
                        NULL::integer AS segment_order,
                        NULL::text AS segment_type,
                        NULL::text AS segment_title,
                        NULL::text AS segment_author,
                        NULL::text AS source_marker,
                        NULL::text AS attribution_method,
                        p.content,
                        coverage.matched_terms,
                        %s::integer AS total_terms,
                        CASE WHEN strpos(
                            fiqh_normalize_arabic(p.content),
                            fiqh_normalize_arabic(%s)
                        ) > 0 THEN 1 ELSE 0 END AS phrase_match,
                        ts_rank_cd(p.search_vector_ar, to_tsquery('simple', %s)) AS rank
                    FROM passages p
                    JOIN editions e ON e.id = p.edition_id
                    JOIN books b ON b.id = e.book_id
                    LEFT JOIN pages pg ON pg.id = p.page_id
                    CROSS JOIN LATERAL (
                        SELECT count(*)::integer AS matched_terms
                        FROM unnest(%s::text[]) AS q(token)
                        WHERE p.search_vector_ar @@ to_tsquery('simple', q.token)
                    ) AS coverage
                    WHERE %s::text IN ('all', 'epub')
                      AND p.search_vector_ar @@ to_tsquery('simple', %s)
                      AND coverage.matched_terms >= %s
                      AND NOT EXISTS (
                          SELECT 1
                          FROM telegram_imports old_ti
                          JOIN editions old_e ON old_e.id = old_ti.edition_id
                          JOIN books old_b ON old_b.id = old_e.book_id
                          JOIN telegram_imports new_ti
                            ON new_ti.id > old_ti.id
                           AND new_ti.status = 'imported'
                          JOIN editions new_e ON new_e.id = new_ti.edition_id
                          JOIN books new_b ON new_b.id = new_e.book_id
                          WHERE old_ti.edition_id = e.id
                            AND lower(btrim(old_b.title)) = lower(btrim(new_b.title))
                            AND lower(btrim(coalesce(old_b.author, ''))) =
                                lower(btrim(coalesce(new_b.author, '')))
                      )
                ),
                turath_hits AS (
                    SELECT
                        'turath'::text AS source_type,
                        concat(p.turath_book_id, ':', p.turath_page_id, ':', s.segment_order) AS source_id,
                        NULL::bigint AS local_book_id,
                        p.book_title,
                        NULL::text AS edition_title,
                        NULL::bigint AS page_id,
                        NULL::integer AS page_number,
                        NULL::text AS locator,
                        p.turath_book_id,
                        p.turath_page_id,
                        p.sequential_index,
                        p.volume,
                        p.printed_page,
                        s.segment_order,
                        s.segment_type,
                        s.segment_title,
                        s.segment_author,
                        s.source_marker,
                        s.attribution_method,
                        s.content,
                        coverage.matched_terms,
                        %s::integer AS total_terms,
                        CASE WHEN strpos(
                            fiqh_normalize_arabic(s.content),
                            fiqh_normalize_arabic(%s)
                        ) > 0 THEN 1 ELSE 0 END AS phrase_match,
                        ts_rank_cd(s.search_vector_ar, to_tsquery('simple', %s)) AS rank
                    FROM turath_segments s
                    JOIN turath_source_pages p ON p.id = s.source_page_id
                    CROSS JOIN LATERAL (
                        SELECT count(*)::integer AS matched_terms
                        FROM unnest(%s::text[]) AS q(token)
                        WHERE s.search_vector_ar @@ to_tsquery('simple', q.token)
                    ) AS coverage
                    WHERE %s::text IN ('all', 'turath')
                      AND s.search_vector_ar @@ to_tsquery('simple', %s)
                      AND coverage.matched_terms >= %s
                      AND (%s::bigint IS NULL OR p.turath_book_id = %s::bigint)
                      AND (%s::text IS NULL OR s.segment_type = %s::text)
                )
                SELECT * FROM passage_hits
                UNION ALL
                SELECT * FROM turath_hits
                ORDER BY phrase_match DESC,
                         (matched_terms::numeric / greatest(total_terms, 1)) DESC,
                         matched_terms DESC,
                         rank DESC
                LIMIT %s
                """,
                (
                    len(term_groups), alternative, tsquery, term_groups,
                    source, tsquery, minimum_matches,
                    len(term_groups), alternative, tsquery, term_groups,
                    source, tsquery, minimum_matches,
                    turath_book_id, turath_book_id, segment_type, segment_type,
                    max(limit * 3, 30),
                ),
            ).fetchall()

            for row in rows:
                item = normalize_search_result_value(dict(row))
                key = (item["source_type"], item["source_id"])
                coverage_ratio = (
                    item["matched_terms"] / max(item["total_terms"], 1)
                )
                item["coverage_ratio"] = round(coverage_ratio, 4)
                item["search_score"] = round(
                    (1000 if item["phrase_match"] else 0)
                    + coverage_ratio * 100
                    + float(item["rank"] or 0),
                    6,
                )
                previous = candidates.get(key)
                if previous is None or item["search_score"] > previous["search_score"]:
                    candidates[key] = item

    ordered = sorted(
        candidates.values(),
        key=lambda item: (
            item["phrase_match"],
            item["coverage_ratio"],
            item["matched_terms"],
            item["rank"] or 0,
        ),
        reverse=True,
    )
    return ordered[:limit]
