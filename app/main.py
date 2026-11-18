import os
import hmac
import re
import json
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from app.db import open_pool, close_pool, initialize_schema, connection
from app.search import expand_search_query, build_fallback_search
from app.segments import parse_turath_page
from app.telegram_ingest import attach_telegram_routes, initialize_telegram_schema

mcp = FastMCP(
    "FiqhBooksMCP",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(
        allowed_hosts=["fiqhbooksmcp-1.onrender.com", "fiqhbooksmcp-1.onrender.com:*"]
    ),
)

@mcp.tool()
def list_books(limit: int = 50) -> list[dict]:
    """List books registered in the fiqh research library."""
    limit = max(1, min(limit, 200))
    with connection() as conn:
        rows = conn.execute(
            "SELECT id, title, author, language, description FROM books ORDER BY id DESC LIMIT %s",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]

@mcp.tool()
def add_book(title: str, author: str = "", language: str = "ar", description: str = "") -> dict:
    """Register a book in the research library."""
    with connection() as conn:
        row = conn.execute(
            "INSERT INTO books (title, author, language, description) VALUES (%s,%s,%s,%s) RETURNING id,title,author,language,description",
            (title.strip(), author.strip(), language.strip(), description.strip()),
        ).fetchone()
    return dict(row)

@mcp.tool()
def add_edition(book_id: int, title: str, publisher: str = "", edition_year: str = "", isbn: str = "", source_file_key: str = "") -> dict:
    """Register a specific edition and its durable object-storage key, if available."""
    with connection() as conn:
        row = conn.execute(
            """INSERT INTO editions (book_id,title,publisher,edition_year,isbn,source_file_key)
               VALUES (%s,%s,%s,%s,%s,%s)
               RETURNING id,book_id,title,publisher,edition_year,isbn,source_file_key""",
            (book_id, title.strip(), publisher.strip(), edition_year.strip(), isbn.strip(), source_file_key.strip()),
        ).fetchone()
    return dict(row)

@mcp.tool()
def save_page(edition_id: int, page_number: int, content: str, page_label: str = "") -> dict:
    """Save extracted text for one edition page."""
    with connection() as conn:
        row = conn.execute(
            """INSERT INTO pages (edition_id,page_number,page_label,content)
               VALUES (%s,%s,%s,%s)
               ON CONFLICT (edition_id,page_number) DO UPDATE
               SET page_label=EXCLUDED.page_label, content=EXCLUDED.content
               RETURNING id,edition_id,page_number,page_label""",
            (edition_id, page_number, page_label, content),
        ).fetchone()
    return dict(row)

@mcp.tool()
def add_passage(edition_id: int, content: str, page_id: int | None = None, locator: str = "") -> dict:
    """Index a searchable passage with its edition and page locator."""
    with connection() as conn:
        row = conn.execute(
            """INSERT INTO passages (edition_id,page_id,locator,content)
               VALUES (%s,%s,%s,%s) RETURNING id,edition_id,page_id,locator""",
            (edition_id, page_id, locator, content),
        ).fetchone()
    return dict(row)

@mcp.tool()
def search_passages(query: str, limit: int = 20) -> list[dict]:
    """Search normalized Arabic text, with a conservative multilingual fallback."""
    limit = max(1, min(limit, 100))
    alternatives = expand_search_query(query)
    if not alternatives:
        return []

    with connection() as conn:
        # Try exact all-terms matching for the original query and each explicit
        # language expansion before using the broader OR-prefix fallback.
        rows = []
        for alternative in alternatives:
            rows = conn.execute(
                """SELECT p.id, b.title AS book_title, e.title AS edition_title,
                          p.page_id, pg.page_number, p.locator, p.content,
                          ts_rank(p.search_vector_ar,
                                  plainto_tsquery('simple', fiqh_normalize_arabic(%s))) AS rank
                   FROM passages p
                   JOIN editions e ON e.id=p.edition_id
                   JOIN books b ON b.id=e.book_id
                   LEFT JOIN pages pg ON pg.id=p.page_id
                   WHERE p.search_vector_ar @@
                         plainto_tsquery('simple', fiqh_normalize_arabic(%s))
                   ORDER BY rank DESC, p.id DESC LIMIT %s""",
                (alternative, alternative, limit),
            ).fetchall()
            if rows:
                break

        if not rows:
            # Require multi-token coverage for longer queries to reduce OR-fallback noise.
            tokens = []
            for alternative in alternatives:
                tokens.extend(re.findall(r"[\w]+", alternative, flags=re.UNICODE))
            tokens = list(dict.fromkeys(token for token in tokens if token))
            tsquery, minimum_matches = build_fallback_search(tokens)
            if not tsquery:
                return []
            rows = conn.execute(
                """SELECT p.id, b.title AS book_title, e.title AS edition_title,
                          p.page_id, pg.page_number, p.locator, p.content,
                          coverage.matched_terms,
                          ts_rank(p.search_vector_ar, to_tsquery('simple', %s)) AS rank
                   FROM passages p
                   JOIN editions e ON e.id=p.edition_id
                   JOIN books b ON b.id=e.book_id
                   LEFT JOIN pages pg ON pg.id=p.page_id
                   CROSS JOIN LATERAL (
                       SELECT count(*)::int AS matched_terms
                       FROM unnest(%s::text[]) AS q(token)
                       WHERE p.search_vector_ar @@ to_tsquery('simple', q.token || ':*')
                   ) AS coverage
                   WHERE p.search_vector_ar @@ to_tsquery('simple', %s)
                     AND coverage.matched_terms >= %s
                   ORDER BY coverage.matched_terms DESC, rank DESC, p.id DESC
                   LIMIT %s""",
                (tsquery, tokens, tsquery, minimum_matches, limit),
            ).fetchall()
    return [dict(r) for r in rows]

@mcp.tool()
def save_research_note(title: str, body: str, book_id: int | None = None, tags: list[str] | None = None) -> dict:
    """Save a research note, linked optionally to a book."""
    with connection() as conn:
        row = conn.execute(
            """INSERT INTO research_notes (book_id,title,body,tags)
               VALUES (%s,%s,%s,%s) RETURNING id,book_id,title,body,tags,created_at""",
            (book_id, title.strip(), body, tags or []),
        ).fetchone()
    return dict(row)


import httpx

TURATH_API = "https://api.turath.io"
TURATH_FILES = "https://files.turath.io"
TURATH_TIMEOUT = httpx.Timeout(25.0, connect=10.0)

def _turath_get(url: str, params: dict | None = None):
    """Fetch source data from Turath; return upstream attribution with the payload."""
    try:
        with httpx.Client(timeout=TURATH_TIMEOUT, follow_redirects=True) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
        if isinstance(data, dict):
            return data
        return {"data": data, "source": {"provider": "turath", "url": str(response.url)}}
    except httpx.HTTPStatusError as exc:
        raise ValueError(f"Turath API returned HTTP {exc.response.status_code}") from exc
    except (httpx.RequestError, ValueError) as exc:
        raise ValueError(f"Turath request failed: {str(exc)[:300]}") from exc

@mcp.tool()
def turath_search(query: str, author: int | None = None, book: int | None = None,
                  category: int | None = None, page: int | None = None,
                  precision: int | None = None, sort_field: str | None = None) -> dict:
    """Search Turath's Arabic book catalog and indexed text; returns source references."""
    params = {"q": query, "ver": "3"}
    if author is not None: params["author"] = author
    if book is not None: params["book"] = book
    if category is not None: params["cat_id"] = category
    if page is not None: params["page"] = page
    if precision is not None: params["precision"] = precision
    if sort_field is not None: params["sortField"] = sort_field
    return _turath_get(f"{TURATH_API}/search", params)

@mcp.tool()
def turath_get_book_info(book_id: int) -> dict:
    """Get Turath book metadata, headings, page map, and volume indexes."""
    return _turath_get(f"{TURATH_API}/book", {"id": book_id, "include": "indexes", "ver": "3"})

@mcp.tool()
def turath_get_page(book_id: int, page_number: int) -> dict:
    """Get one parsed Turath book page with its text and source attribution."""
    return _turath_get(f"{TURATH_API}/page", {"book_id": book_id, "pg": page_number, "ver": "3"})

def _index_turath_page(book_id: int, sequential_index: int) -> dict:
    """Fetch one Turath page and idempotently index its explicitly marked source segments."""
    if book_id <= 0 or sequential_index <= 0:
        raise ValueError("book_id and sequential_index must be positive integers")

    payload = _turath_get(
        f"{TURATH_API}/page",
        {"book_id": book_id, "pg": sequential_index, "ver": "3"},
    )
    raw_meta = payload.get("meta", {})
    if isinstance(raw_meta, str):
        try:
            metadata = json.loads(raw_meta)
        except json.JSONDecodeError as exc:
            raise ValueError("Turath page metadata is not valid JSON") from exc
    elif isinstance(raw_meta, dict):
        metadata = raw_meta
    else:
        metadata = {}

    page_text = payload.get("text", "")
    if not isinstance(page_text, str) or not page_text.strip():
        raise ValueError("Turath returned an empty page; nothing was indexed")
    turath_page_id = metadata.get("page_id")
    if turath_page_id is None:
        raise ValueError("Turath page metadata has no page_id; refusing to index without a stable locator")

    segments = parse_turath_page(page_text)
    if not segments:
        raise ValueError("No non-empty text segments were extracted; original page was not indexed")

    with connection() as conn:
        page = conn.execute(
            """INSERT INTO turath_source_pages
               (turath_book_id,sequential_index,turath_page_id,volume,printed_page,
                book_title,page_author,page_metadata,raw_text,indexed_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,now())
               ON CONFLICT (turath_book_id,sequential_index) DO UPDATE SET
                 turath_page_id=EXCLUDED.turath_page_id,
                 volume=EXCLUDED.volume,
                 printed_page=EXCLUDED.printed_page,
                 book_title=EXCLUDED.book_title,
                 page_author=EXCLUDED.page_author,
                 page_metadata=EXCLUDED.page_metadata,
                 raw_text=EXCLUDED.raw_text,
                 indexed_at=now()
               RETURNING id""",
            (
                book_id, sequential_index, int(turath_page_id),
                str(metadata.get("vol") or ""),
                str(metadata.get("page") or ""),
                metadata.get("book_name"),
                metadata.get("author_name"),
                json.dumps(metadata, ensure_ascii=False),
                page_text,
            ),
        ).fetchone()
        source_page_id = page["id"]
        conn.execute("DELETE FROM turath_segments WHERE source_page_id=%s", (source_page_id,))
        for segment in segments:
            conn.execute(
                """INSERT INTO turath_segments
                   (source_page_id,segment_order,segment_type,segment_title,
                    segment_author,source_marker,attribution_method,content)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    source_page_id, segment["segment_order"], segment["segment_type"],
                    segment.get("segment_title"), segment.get("segment_author"),
                    segment.get("source_marker"), segment["attribution_method"],
                    segment["content"],
                ),
            )

    return {
        "status": "indexed",
        "turath_book_id": book_id,
        "turath_page_id": int(turath_page_id),
        "sequential_index": sequential_index,
        "volume": str(metadata.get("vol") or ""),
        "printed_page": str(metadata.get("page") or ""),
        "segments_indexed": len(segments),
        "segment_types": [segment["segment_type"] for segment in segments],
        "idempotent": True,
    }


@mcp.tool()
def turath_index_page(book_id: int, sequential_index: int) -> dict:
    """Fetch and idempotently index one explicitly attributed Turath page."""
    return _index_turath_page(book_id, sequential_index)


def _turath_sequential_indexes(book_info: dict) -> list[int]:
    """Read only real page-map entries; never treat null padding as pages."""
    indexes = book_info.get("indexes") or {}
    print_map = indexes.get("print_pg_to_pg")
    values: set[int] = set()
    if isinstance(print_map, dict):
        for value in print_map.values():
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0:
                values.add(number)
    if not values:
        page_map = indexes.get("page_map") or []
        for position, entry in enumerate(page_map):
            if entry:
                # Turath's page endpoint uses 1-based pg values; page_map
                # has two null padding positions before its first real page.
                values.add(position + 1)
    return sorted(values)


@mcp.tool()
def turath_index_batch(book_id: int, batch_size: int = 5) -> dict:
    """Index the next missing Turath pages; safe to rerun after interruption.

    The book's official page map defines the valid page range. Completed pages
    are skipped, each page commits independently, and failures are checkpointed
    for automatic retry on a later call.
    """
    if book_id <= 0:
        raise ValueError("book_id must be a positive integer")
    batch_size = max(1, min(int(batch_size), 5))

    book_info = turath_get_book_info(book_id)
    sequential_indexes = _turath_sequential_indexes(book_info)
    if not sequential_indexes:
        raise ValueError("Turath returned no valid page-map entries; refusing to start indexing")

    with connection() as conn:
        indexed_rows = conn.execute(
            "SELECT sequential_index FROM turath_source_pages WHERE turath_book_id=%s",
            (book_id,),
        ).fetchall()
        indexed = {int(row["sequential_index"]) for row in indexed_rows}
        pending = [number for number in sequential_indexes if number not in indexed]

    selected = pending[:batch_size]
    page_results = []
    for sequential_index in selected:
        try:
            result = _index_turath_page(book_id, sequential_index)
        except Exception as exc:
            error_text = str(exc)[:1000]
            with connection() as conn:
                conn.execute(
                    """INSERT INTO turath_index_failures
                       (turath_book_id,sequential_index,attempts,last_error,updated_at)
                       VALUES (%s,%s,1,%s,now())
                       ON CONFLICT (turath_book_id,sequential_index) DO UPDATE SET
                         attempts=turath_index_failures.attempts+1,
                         last_error=EXCLUDED.last_error,
                         updated_at=now()""",
                    (book_id, sequential_index, error_text),
                )
            page_results.append({
                "sequential_index": sequential_index,
                "status": "failed",
                "error": error_text,
            })
        else:
            with connection() as conn:
                conn.execute(
                    "DELETE FROM turath_index_failures WHERE turath_book_id=%s AND sequential_index=%s",
                    (book_id, sequential_index),
                )
            page_results.append({
                "sequential_index": sequential_index,
                "status": "indexed",
                "turath_page_id": result["turath_page_id"],
                "volume": result["volume"],
                "printed_page": result["printed_page"],
                "segments_indexed": result["segments_indexed"],
                "segment_types": result["segment_types"],
            })

    with connection() as conn:
        counts = conn.execute(
            """SELECT
                 count(*)::int AS indexed_pages,
                 coalesce(sum(segment_count),0)::int AS indexed_segments
               FROM (
                 SELECT p.id, count(s.id)::int AS segment_count
                 FROM turath_source_pages p
                 LEFT JOIN turath_segments s ON s.source_page_id=p.id
                 WHERE p.turath_book_id=%s
                 GROUP BY p.id
               ) indexed_data""",
            (book_id,),
        ).fetchone()
        failures = conn.execute(
            """SELECT sequential_index,attempts,last_error
               FROM turath_index_failures
               WHERE turath_book_id=%s
               ORDER BY sequential_index""",
            (book_id,),
        ).fetchall()

    indexed_pages = int(counts["indexed_pages"])
    total_pages = len(sequential_indexes)
    remaining_pages = max(0, total_pages - indexed_pages)
    next_missing = next(
        (number for number in sequential_indexes
         if number not in indexed and number not in {
             item["sequential_index"] for item in page_results
             if item["status"] == "indexed"
         }),
        None,
    )
    return {
        "status": "complete" if remaining_pages == 0 else "partial",
        "turath_book_id": book_id,
        "book_title": (book_info.get("meta") or {}).get("name"),
        "valid_page_map_entries": total_pages,
        "indexed_pages": indexed_pages,
        "remaining_pages": remaining_pages,
        "indexed_segments": int(counts["indexed_segments"]),
        "failed_page_count": len(failures),
        "failed_pages": [dict(row) for row in failures[:25]],
        "next_missing_sequential_index": next_missing,
        "batch_size": batch_size,
        "processed_this_batch": page_results,
        "resume_hint": "Call turath_index_batch again with the same book_id; completed pages are skipped and failed pages are retried.",
    }


@mcp.tool()
def search_turath_segments(
    query: str,
    book_id: int | None = None,
    segment_type: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search independently indexed Turath source segments with their page and layer attribution."""
    limit = max(1, min(limit, 100))
    if segment_type and segment_type not in {
        "matn", "sharh", "hashiyah", "editorial", "fatwa", "unclassified"
    }:
        raise ValueError("Unsupported segment_type")
    alternatives = expand_search_query(query)
    if not alternatives:
        return []

    with connection() as conn:
        rows = []
        for alternative in alternatives:
            rows = conn.execute(
                """SELECT
                       p.turath_book_id, p.turath_page_id, p.sequential_index,
                       p.volume, p.printed_page, p.book_title, p.page_author,
                       s.segment_order, s.segment_type, s.segment_title,
                       s.segment_author, s.source_marker, s.attribution_method,
                       s.content,
                       ts_rank(s.search_vector_ar,
                           plainto_tsquery('simple', fiqh_normalize_arabic(%s))) AS rank
                   FROM turath_segments s
                   JOIN turath_source_pages p ON p.id=s.source_page_id
                   WHERE s.search_vector_ar @@
                         plainto_tsquery('simple', fiqh_normalize_arabic(%s))
                     AND (%s::integer IS NULL OR p.turath_book_id=%s::integer)
                     AND (%s::text IS NULL OR s.segment_type=%s::text)
                   ORDER BY rank DESC, p.turath_book_id, p.sequential_index,
                            s.segment_order
                   LIMIT %s""",
                (alternative, alternative, book_id, book_id,
                 segment_type, segment_type, limit),
            ).fetchall()
            if rows:
                break
    return [dict(row) for row in rows]


@mcp.tool()
def turath_get_book_file(book_id: int) -> dict:
    """Get a complete Turath book JSON dump, including parsed pages; may be large."""
    if book_id <= 0:
        raise ValueError("book_id must be a positive integer")
    return _turath_get(f"{TURATH_FILES}/books/{book_id}.json")

@mcp.tool()
def turath_get_author(author_id: int) -> dict:
    """Get Turath author biography and metadata by author ID."""
    return _turath_get(f"{TURATH_API}/author", {"id": author_id, "ver": "3"})


class MCPAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/mcp"):
            expected = os.getenv("MCP_BEARER_TOKEN", "")
            supplied = request.headers.get("authorization", "")
            query_token = request.query_params.get("token", "")
            header_valid = bool(expected) and hmac.compare_digest(supplied, f"Bearer {expected}")
            query_valid = bool(expected) and hmac.compare_digest(query_token, expected)
            if not (header_valid or query_valid):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)

@asynccontextmanager
async def lifespan(app: FastAPI):
    open_pool()
    initialize_schema()
    initialize_telegram_schema()
    try:
        async with mcp.session_manager.run():
            yield
    finally:
        close_pool()

app = FastAPI(title="FiqhBooksMCP", version="0.1.1", lifespan=lifespan)
app.add_middleware(MCPAuthMiddleware)
app.mount("/mcp", mcp.streamable_http_app())

@app.get("/health")
def health():
    try:
        with connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "healthy", "database": "connected", "service": "FiqhBooksMCP"}
    except Exception:
        return JSONResponse({"status": "degraded", "database": "unavailable"}, status_code=503)


# Optional Telegram EPUB ingestion routes; disabled until Telegram env vars are set.
attach_telegram_routes(app)
