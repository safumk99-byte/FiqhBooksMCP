import os
import hmac
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from mcp.server.fastmcp import FastMCP
from app.db import open_pool, close_pool, initialize_schema, connection

mcp = FastMCP("FiqhBooksMCP", stateless_http=True, json_response=True)
# Mount prefix is /mcp, so the inner MCP route must be /.
mcp_app = mcp.streamable_http_app(streamable_http_path="/")

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
    """Search indexed Arabic or other text passages; returns source locators."""
    limit = max(1, min(limit, 100))
    with connection() as conn:
        rows = conn.execute(
            """SELECT p.id, b.title AS book_title, e.title AS edition_title,
                      p.page_id, pg.page_number, p.locator, p.content,
                      ts_rank(p.search_vector, plainto_tsquery('simple', %s)) AS rank
               FROM passages p
               JOIN editions e ON e.id=p.edition_id
               JOIN books b ON b.id=e.book_id
               LEFT JOIN pages pg ON pg.id=p.page_id
               WHERE p.search_vector @@ plainto_tsquery('simple', %s)
               ORDER BY rank DESC, p.id DESC LIMIT %s""",
            (query, query, limit),
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
app.mount("/mcp", mcp_app)

@app.get("/health")
def health():
    try:
        with connection() as conn:
            conn.execute("SELECT 1")
        return {"status": "healthy", "database": "connected", "service": "FiqhBooksMCP"}
    except Exception:
        return JSONResponse({"status": "degraded", "database": "unavailable"}, status_code=503)
