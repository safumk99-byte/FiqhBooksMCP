import os
import hmac
import re
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from app.db import open_pool, close_pool, initialize_schema, connection
from app.search import expand_search_query

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
        # Keep exact all-terms matching as the precision-first path.
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
            (alternatives[0], alternatives[0], limit),
        ).fetchall()

        if not rows:
            # OR fallback prevents natural-language queries from requiring every token.
            tokens = []
            for alternative in alternatives:
                tokens.extend(re.findall(r"[\\w]+", alternative, flags=re.UNICODE))
            tokens = list(dict.fromkeys(token for token in tokens if token))
            tsquery = " | ".join(f"{token}:*" for token in tokens)
            if not tsquery:
                return []
            rows = conn.execute(
                """SELECT p.id, b.title AS book_title, e.title AS edition_title,
                          p.page_id, pg.page_number, p.locator, p.content,
                          ts_rank(p.search_vector_ar, to_tsquery('simple', %s)) AS rank
                   FROM passages p
                   JOIN editions e ON e.id=p.edition_id
                   JOIN books b ON b.id=e.book_id
                   LEFT JOIN pages pg ON pg.id=p.page_id
                   WHERE p.search_vector_ar @@ to_tsquery('simple', %s)
                   ORDER BY rank DESC, p.id DESC LIMIT %s""",
                (tsquery, tsquery, limit),
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
