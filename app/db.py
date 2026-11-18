import os
from contextlib import contextmanager
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None

SCHEMA_SQL = """
CREATE OR REPLACE FUNCTION fiqh_normalize_arabic(input_text TEXT)
RETURNS TEXT
LANGUAGE SQL
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT translate(
        regexp_replace(
            coalesce(input_text, ''),
            '[ً-ٰٟۖ-ۭـ]',
            '',
            'g'
        ),
        'أإآٱىؤئ',
        'اااايوي'
    );
$$;

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
ALTER TABLE passages
    ADD COLUMN IF NOT EXISTS search_vector_ar TSVECTOR
    GENERATED ALWAYS AS (
        to_tsvector('simple', fiqh_normalize_arabic(content))
    ) STORED;
CREATE INDEX IF NOT EXISTS passages_search_idx ON passages USING GIN(search_vector);
CREATE INDEX IF NOT EXISTS passages_search_ar_idx ON passages USING GIN(search_vector_ar);

CREATE TABLE IF NOT EXISTS turath_source_pages (
    id BIGSERIAL PRIMARY KEY,
    turath_book_id BIGINT NOT NULL,
    sequential_index INTEGER NOT NULL,
    turath_page_id BIGINT NOT NULL,
    volume TEXT,
    printed_page TEXT,
    book_title TEXT,
    page_author TEXT,
    page_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    raw_text TEXT NOT NULL,
    indexed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (turath_book_id, sequential_index),
    UNIQUE (turath_book_id, turath_page_id)
);
CREATE TABLE IF NOT EXISTS turath_segments (
    id BIGSERIAL PRIMARY KEY,
    source_page_id BIGINT NOT NULL REFERENCES turath_source_pages(id) ON DELETE CASCADE,
    segment_order INTEGER NOT NULL,
    segment_type TEXT NOT NULL DEFAULT 'unclassified'
        CHECK (segment_type IN ('matn','sharh','hashiyah','editorial','fatwa','unclassified')),
    segment_title TEXT,
    segment_author TEXT,
    source_marker TEXT,
    attribution_method TEXT NOT NULL,
    content TEXT NOT NULL,
    search_vector_ar TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('simple', fiqh_normalize_arabic(content))
    ) STORED,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_page_id, segment_order)
);
CREATE INDEX IF NOT EXISTS turath_segments_search_ar_idx
    ON turath_segments USING GIN(search_vector_ar);
CREATE INDEX IF NOT EXISTS turath_segments_type_idx
    ON turath_segments(segment_type);

CREATE TABLE IF NOT EXISTS turath_index_failures (
    turath_book_id BIGINT NOT NULL,
    sequential_index INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    last_error TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (turath_book_id, sequential_index)
);

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
