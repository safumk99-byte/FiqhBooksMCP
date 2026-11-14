import os
from contextlib import contextmanager
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS books (
    id BIGSERIAL PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT,
    language TEXT NOT NULL DEFAULT 'ar',
    description TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS editions (
    id BIGSERIAL PRIMARY KEY,
    book_id BIGINT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    publisher TEXT,
    edition_year TEXT,
    isbn TEXT,
    source_file_key TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS pages (
    id BIGSERIAL PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    page_label TEXT,
    content TEXT NOT NULL DEFAULT '',
    UNIQUE (edition_id, page_number)
);
CREATE TABLE IF NOT EXISTS passages (
    id BIGSERIAL PRIMARY KEY,
    edition_id BIGINT NOT NULL REFERENCES editions(id) ON DELETE CASCADE,
    page_id BIGINT REFERENCES pages(id) ON DELETE SET NULL,
    locator TEXT,
    content TEXT NOT NULL,
    search_vector TSVECTOR GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS passages_search_idx ON passages USING GIN(search_vector);
CREATE TABLE IF NOT EXISTS citations (
    id BIGSERIAL PRIMARY KEY,
    passage_id BIGINT REFERENCES passages(id) ON DELETE SET NULL,
    book_id BIGINT REFERENCES books(id) ON DELETE SET NULL,
    citation_text TEXT NOT NULL,
    locator TEXT,
    note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS research_notes (
    id BIGSERIAL PRIMARY KEY,
    book_id BIGINT REFERENCES books(id) ON DELETE SET NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    tags TEXT[] NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

def _check_connection(conn) -> None:
    """Reject stale or already-closed pooled connections before handing them to tools."""
    conn.execute("SELECT 1")

def open_pool() -> None:
    global _pool
    dsn = os.environ["DATABASE_URL"]
    if "sslmode=" not in dsn:
        dsn += ("&" if "?" in dsn else "?") + "sslmode=require"

    _pool = ConnectionPool(
        conninfo=dsn,
        min_size=1,
        max_size=5,
        kwargs={
            "row_factory": dict_row,
            "connect_timeout": 15,
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 3,
        },
        check=_check_connection,
        max_lifetime=300,
        max_idle=60,
        reconnect_timeout=30,
        open=True,
    )

def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None

@contextmanager
def connection():
    if _pool is None:
        raise RuntimeError("Database pool is not initialized")
    with _pool.connection() as conn:
        yield conn

def initialize_schema() -> None:
    with connection() as conn:
        conn.execute(SCHEMA_SQL)
