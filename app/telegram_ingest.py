"""Telegram channel webhook and EPUB-to-search-index ingestion."""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import time
from typing import Any

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from app.db import connection
from app.epub import parse_epub

router = APIRouter()
# httpx INFO logs include full request URLs; Telegram Bot API URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
TELEGRAM_API = "https://api.telegram.org"
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024


def initialize_telegram_schema() -> None:
    with connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS telegram_imports (
                id BIGSERIAL PRIMARY KEY,
                file_unique_id TEXT NOT NULL UNIQUE,
                file_id TEXT,
                chat_id BIGINT NOT NULL,
                message_id BIGINT NOT NULL,
                file_name TEXT NOT NULL,
                file_size BIGINT,
                book_id BIGINT REFERENCES books(id) ON DELETE SET NULL,
                edition_id BIGINT REFERENCES editions(id) ON DELETE SET NULL,
                status TEXT NOT NULL DEFAULT 'processing'
                    CHECK (status IN ('processing','imported','failed')),
                error TEXT,
                audit JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            ALTER TABLE telegram_imports
                ADD COLUMN IF NOT EXISTS audit JSONB NOT NULL DEFAULT '{}'::jsonb;
            ALTER TABLE telegram_imports
                ADD COLUMN IF NOT EXISTS file_id TEXT;
            ALTER TABLE telegram_imports
                DROP CONSTRAINT IF EXISTS telegram_imports_status_check;
            ALTER TABLE telegram_imports
                ADD CONSTRAINT telegram_imports_status_check
                CHECK (status IN ('queued','processing','imported','failed'));
            ALTER TABLE telegram_imports
                ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0;
            ALTER TABLE telegram_imports
                ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ;
            UPDATE telegram_imports
                SET processing_started_at=updated_at
                WHERE status='processing' AND processing_started_at IS NULL;
            CREATE INDEX IF NOT EXISTS telegram_imports_status_idx
                ON telegram_imports(status, created_at);
        """)


def _telegram_request(method: str, token: str, payload: dict | None = None) -> dict:
    # Telegram's getFile endpoint can occasionally time out even when the file
    # is valid. Retry transient transport failures before marking the import failed.
    max_attempts = 3 if method == "getFile" else 1
    for attempt in range(max_attempts):
        try:
            response = httpx.post(
                f"{TELEGRAM_API}/bot{token}/{method}",
                json=payload or {},
                timeout=httpx.Timeout(60.0, connect=15.0),
            )
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPStatusError as exc:
            if method == "getFile" and exc.response.status_code in (429, 500, 502, 503, 504) and attempt + 1 < max_attempts:
                time.sleep(2 ** attempt)
                continue
            # Telegram's error description is safe to log; never include request URLs,
            # which contain the bot token.
            try:
                error_body = exc.response.json()
                description = str(error_body.get("description") or "HTTP error")
            except (ValueError, AttributeError):
                description = "HTTP error"
            raise ValueError(
                f"Telegram {method} returned HTTP {exc.response.status_code}: {description[:160]}"
            ) from None
        except httpx.RequestError as exc:
            if method == "getFile" and attempt + 1 < max_attempts:
                time.sleep(2 ** attempt)
                continue
            # Exception strings from HTTP clients can contain the token-bearing URL.
            raise ValueError(
                f"Telegram {method} request failed ({type(exc).__name__})"
            ) from None
        except ValueError as exc:
            raise ValueError(f"Telegram {method} returned an invalid JSON response") from None

        if not body.get("ok"):
            description = str(body.get("description") or "request rejected")
            raise ValueError(f"Telegram {method} rejected the request: {description[:160]}")
        return body.get("result") or {}

    raise ValueError(f"Telegram {method} request failed after {max_attempts} attempts")


def _claim_file(file_unique_id: str, file_id: str, chat_id: int, message_id: int,
                file_name: str, file_size: int | None) -> bool:
    with connection() as conn:
        row = conn.execute(
            """INSERT INTO telegram_imports
               (file_unique_id,file_id,chat_id,message_id,file_name,file_size,status)
               VALUES (%s,%s,%s,%s,%s,%s,'queued')
               ON CONFLICT (file_unique_id) DO UPDATE SET
                   file_id=EXCLUDED.file_id,
                   chat_id=EXCLUDED.chat_id,
                   message_id=EXCLUDED.message_id,
                   file_name=EXCLUDED.file_name,
                   file_size=EXCLUDED.file_size,
                   status='queued', error=NULL, processing_started_at=NULL, updated_at=now()
               WHERE telegram_imports.status IN ('failed','imported')
               RETURNING id""",
            (file_unique_id, file_id, chat_id, message_id, file_name, file_size),
        ).fetchone()
    return row is not None


def _download_epub(token: str, file_id: str, file_size: int | None) -> bytes:
    if file_size is not None and file_size > MAX_DOWNLOAD_BYTES:
        raise ValueError("Telegram Bot API download limit is 20 MB; this EPUB is too large")
    result = _telegram_request("getFile", token, {"file_id": file_id})
    file_path = result.get("file_path")
    if not file_path:
        raise ValueError("Telegram did not return a downloadable file path")
    url = f"{TELEGRAM_API}/file/bot{token}/{file_path}"
    try:
        with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0),
                          follow_redirects=True) as client:
            response = client.get(url)
            response.raise_for_status()
            if len(response.content) > MAX_DOWNLOAD_BYTES:
                raise ValueError("Downloaded file exceeds the 20 MB Telegram limit")
            return response.content
    except httpx.HTTPError as exc:
        raise ValueError("Could not download the EPUB from Telegram") from exc


def _group_chapters(chapters: list[tuple[str, str]]) -> list[dict]:
    """Group ordered EPUB chunks into their original linear spine documents.

    A new section starts at part 1. Repeated headings remain distinct because
    their position in the EPUB reading order, not their heading text, defines
    identity. These are EPUB sections, not claims about printed page numbers.
    """
    sections: list[dict] = []
    for locator, content in chapters:
        match = re.match(r"^(.*?) — part (\d+)$", locator)
        if not match:
            raise ValueError(f"Invalid EPUB passage locator: {locator[:160]}")
        label, part_number_text = match.groups()
        part_number = int(part_number_text)
        if part_number == 1:
            sections.append({
                "page_number": len(sections) + 1,
                "label": label,
                "chunks": [],
            })
        if not sections or part_number != len(sections[-1]["chunks"]) + 1:
            raise ValueError(f"EPUB passage parts are out of order: {locator[:160]}")
        sections[-1]["chunks"].append((locator, content))
    if not sections:
        raise ValueError("EPUB contains no sections to index")
    return sections


def initialize_epub_page_mapping() -> dict:
    """Backfill EPUB spine-section records from the existing ordered passages.

    The importer writes passages in linear spine order and labels chunks with
    part 1, part 2, etc. This migration uses those boundaries to rebuild
    section containers without changing any indexed text. page_number is the
    EPUB spine-section ordinal, never an assumed printed page number.
    """
    migration_sql = """
        WITH eligible AS (
            SELECT e.id AS edition_id
            FROM editions e
            WHERE NOT EXISTS (
                SELECT 1 FROM pages pg WHERE pg.edition_id = e.id
            )
              AND EXISTS (
                SELECT 1 FROM passages p WHERE p.edition_id = e.id
              )
        ), parsed AS (
            SELECT p.id, p.edition_id, p.locator,
                   (regexp_match(p.locator, ' — part ([0-9]+)metadata: dict[str, Any], file_unique_id: str,
                  chat_id: int, message_id: int, file_name: str,
                  file_size: int | None) -> dict:
    chapters = metadata["chapters"]
    sections = _group_chapters(chapters)
    with connection() as conn:
        existing = conn.execute(
            """SELECT book_id, edition_id FROM telegram_imports
               WHERE file_unique_id=%s FOR UPDATE""",
            (file_unique_id,),
        ).fetchone()

        if existing and existing["book_id"] and existing["edition_id"]:
            book = conn.execute(
                """UPDATE books SET title=%s, author=%s, language=%s
                   WHERE id=%s RETURNING id,title""",
                (metadata["title"], metadata["author"], metadata["language"],
                 existing["book_id"]),
            ).fetchone()
            edition = conn.execute(
                """UPDATE editions SET title=%s, publisher=%s, edition_year=%s
                   WHERE id=%s RETURNING id""",
                (file_name, metadata["publisher"], metadata["edition_year"],
                 existing["edition_id"]),
            ).fetchone()
            # Re-index in place: preserve the existing book and edition IDs.
            conn.execute("DELETE FROM passages WHERE edition_id=%s",
                         (existing["edition_id"],))
            conn.execute("DELETE FROM pages WHERE edition_id=%s",
                         (existing["edition_id"],))
        else:
            book = conn.execute(
                """INSERT INTO books (title,author,language,description)
                   VALUES (%s,%s,%s,%s)
                   RETURNING id,title""",
                (metadata["title"], metadata["author"], metadata["language"],
                 "Imported from the private Telegram FiqhBooks channel; "
                 "original EPUB remains in Telegram."),
            ).fetchone()
            edition = conn.execute(
                """INSERT INTO editions
                   (book_id,title,publisher,edition_year,source_file_key)
                   VALUES (%s,%s,%s,%s,%s)
                   RETURNING id""",
                (book["id"], file_name, metadata["publisher"],
                 metadata["edition_year"], f"telegram:file_unique_id:{file_unique_id}"),
            ).fetchone()

        passage_count = 0
        for section in sections:
            page = conn.execute(
                """INSERT INTO pages (edition_id,page_number,page_label,content)
                   VALUES (%s,%s,%s,'')
                   RETURNING id""",
                (
                    edition["id"], section["page_number"],
                    f"EPUB section {section['page_number']} — {section['label']}",
                ),
            ).fetchone()
            for locator, content in section["chunks"]:
                conn.execute(
                    """INSERT INTO passages (edition_id,page_id,locator,content)
                       VALUES (%s,%s,%s,%s)""",
                    (edition["id"], page["id"], locator, content),
                )
                passage_count += 1

        audit = dict(metadata.get("audit", {}))
        audit["epub_sections_indexed"] = len(sections)
        conn.execute(
            """UPDATE telegram_imports
               SET book_id=%s, edition_id=%s, status='imported',
                   audit=%s::jsonb, error=NULL, updated_at=now()
               WHERE file_unique_id=%s""",
            (book["id"], edition["id"], json.dumps(audit, ensure_ascii=False), file_unique_id),
        )
    return {
        "book_id": book["id"],
        "title": book["title"],
        "edition_id": edition["id"],
        "indexed_passages": passage_count,
        "indexed_epub_sections": len(sections),
        "audit": audit,
    }


def _mark_failed(file_unique_id: str, error: str) -> None:
    with connection() as conn:
        conn.execute(
            """UPDATE telegram_imports
               SET status='failed', error=%s, updated_at=now()
               WHERE file_unique_id=%s""",
            (error[:1000], file_unique_id),
        )


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    allowed_chat = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
    if not expected or not token:
        raise HTTPException(status_code=503, detail="Telegram integration is not configured")
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, expected
    ):
        raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")

    update = await request.json()
    message = update.get("channel_post")
    if not isinstance(message, dict):
        return {"ok": True, "ignored": "not_a_channel_post"}

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat.get("type") != "channel" or not isinstance(chat_id, int):
        return {"ok": True, "ignored": "not_a_channel"}
    if allowed_chat and str(chat_id) != allowed_chat:
        raise HTTPException(status_code=403, detail="This Telegram channel is not allowed")

    document = message.get("document") or {}
    file_id = document.get("file_id")
    file_unique_id = document.get("file_unique_id")
    file_name = document.get("file_name") or ""
    file_size = document.get("file_size")
    if not file_id or not file_unique_id:
        return {"ok": True, "ignored": "no_document"}
    if not file_name.lower().endswith(".epub"):
        return {"ok": True, "ignored": "only_epub_files_are_supported"}
    if file_size is not None and file_size > MAX_DOWNLOAD_BYTES:
        raise HTTPException(status_code=413, detail="EPUB exceeds Telegram's 20 MB Bot API download limit")

    claimed = _claim_file(
        str(file_unique_id), str(file_id), int(chat_id), int(message.get("message_id", 0)),
        file_name, file_size if isinstance(file_size, int) else None,
    )
    if not claimed:
        return {"ok": True, "duplicate": True}

    # Telegram webhook delivery is event-driven: acknowledge the upload immediately,
    # then process the queued EPUB in the background without waiting for the next poll.
    background_tasks.add_task(process_next_telegram_import, expected)
    return {"ok": True, "status": "queued", "file_name": file_name}


@router.post("/process-next")
def process_next_telegram_import(
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Claim and process one durable queued import; safe to call from the poller."""
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not expected or not token:
        raise HTTPException(status_code=503, detail="Telegram integration is not configured")
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, expected
    ):
        raise HTTPException(status_code=401, detail="Invalid Telegram worker secret")

    with connection() as conn:
        job = conn.execute(
            """WITH candidate AS (
                   SELECT id FROM telegram_imports
                   WHERE status='queued'
                      OR (status='processing'
                          AND processing_started_at < now() - interval '30 minutes')
                   ORDER BY created_at, id
                   FOR UPDATE SKIP LOCKED
                   LIMIT 1
               )
               UPDATE telegram_imports AS job
               SET status='processing',
                   attempt_count=job.attempt_count+1,
                   processing_started_at=now(),
                   updated_at=now(),
                   error=NULL
               FROM candidate
               WHERE job.id=candidate.id
               RETURNING job.file_unique_id,job.file_id,job.chat_id,
                         job.message_id,job.file_name,job.file_size""",
        ).fetchone()
    if not job:
        return {"ok": True, "status": "idle"}

    try:
        binary = _download_epub(token, job["file_id"], job["file_size"])
        metadata = parse_epub(binary)
        result = _store_import(
            metadata, job["file_unique_id"], int(job["chat_id"]),
            int(job["message_id"]), job["file_name"], job["file_size"],
        )
        return {"ok": True, "status": "imported", **result}
    except Exception as exc:
        _mark_failed(job["file_unique_id"], str(exc))
        return JSONResponse(
            {"ok": True, "status": "failed", "error": str(exc)[:300]},
            status_code=200,
        )


def _require_worker_secret(supplied: str | None) -> None:
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if not expected or not os.getenv("TELEGRAM_BOT_TOKEN", ""):
        raise HTTPException(status_code=503, detail="Telegram integration is not configured")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid Telegram worker secret")


@router.get("/status")
def telegram_import_status(
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    limit: int = 50,
):
    """Return queue totals and recent EPUB imports for an authenticated dashboard."""
    _require_worker_secret(x_telegram_bot_api_secret_token)
    limit = max(1, min(int(limit), 100))
    with connection() as conn:
        totals = conn.execute(
            """SELECT
                 count(*)::int AS total,
                 count(*) FILTER (WHERE status='queued')::int AS queued,
                 count(*) FILTER (WHERE status='processing')::int AS processing,
                 count(*) FILTER (WHERE status='imported')::int AS imported,
                 count(*) FILTER (WHERE status='failed')::int AS failed,
                 coalesce(sum(attempt_count),0)::int AS total_attempts
               FROM telegram_imports"""
        ).fetchone()
        recent = conn.execute(
            """SELECT id,file_name,file_size,status,attempt_count,error,
                      book_id,edition_id,audit,created_at,updated_at
               FROM telegram_imports
               ORDER BY created_at DESC,id DESC
               LIMIT %s""",
            (limit,),
        ).fetchall()
    return {
        "ok": True,
        "summary": dict(totals),
        "recent_imports": [dict(row) for row in recent],
        "limit": limit,
    }


@router.post("/retry/{import_id}")
def retry_failed_telegram_import(
    import_id: int,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Requeue one failed EPUB; never resets a running or completed import."""
    _require_worker_secret(x_telegram_bot_api_secret_token)
    with connection() as conn:
        row = conn.execute(
            """UPDATE telegram_imports
               SET status='queued',error=NULL,processing_started_at=NULL,updated_at=now()
               WHERE id=%s AND status='failed' AND file_id IS NOT NULL
               RETURNING id,file_name,status,attempt_count""",
            (import_id,),
        ).fetchone()
    if not row:
        raise HTTPException(
            status_code=409,
            detail="Import not found, not failed, or missing its Telegram file reference",
        )
    return {"ok": True, "message": "Import queued for retry", "import": dict(row)}


@router.post("/register")
async def register_telegram_webhook(request: Request):
    """Register Telegram webhook; protected with the existing MCP bearer token."""
    expected_mcp = os.getenv("MCP_BEARER_TOKEN", "")
    supplied = request.headers.get("authorization", "")
    if not expected_mcp or not hmac.compare_digest(supplied, f"Bearer {expected_mcp}"):
        raise HTTPException(status_code=401, detail="Unauthorized")

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    if not bot_token or not secret or not webhook_url:
        raise HTTPException(status_code=503, detail="Set TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET and TELEGRAM_WEBHOOK_URL")
    if not webhook_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="TELEGRAM_WEBHOOK_URL must use HTTPS")
    result = _telegram_request(
        "setWebhook",
        bot_token,
        {
            "url": webhook_url.rstrip("/") + "/telegram/webhook",
            "secret_token": secret,
            "allowed_updates": ["channel_post"],
            "drop_pending_updates": False,
        },
    )
    return {"ok": True, "webhook_registered": bool(result), "url": webhook_url.rstrip("/") + "/telegram/webhook"}


def attach_telegram_routes(app) -> None:
    app.include_router(router, prefix="/telegram", tags=["Telegram EPUB ingestion"])


def ensure_telegram_webhook_registered() -> bool:
    """Register the webhook automatically on service startup when configured."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    if not bot_token or not secret or not webhook_url:
        missing = [
            name for name, value in (
                ("TELEGRAM_BOT_TOKEN", bot_token),
                ("TELEGRAM_WEBHOOK_SECRET", secret),
                ("TELEGRAM_WEBHOOK_URL", webhook_url),
            ) if not value
        ]
        print("Telegram webhook registration skipped; missing configuration: " + ", ".join(missing))
        return False
    if not webhook_url.startswith("https://"):
        raise ValueError("TELEGRAM_WEBHOOK_URL must use HTTPS")
    result = _telegram_request(
        "setWebhook",
        bot_token,
        {
            "url": webhook_url.rstrip("/") + "/telegram/webhook",
            "secret_token": secret,
            "allowed_updates": ["channel_post"],
            "drop_pending_updates": False,
        },
    )
    print("Telegram webhook registration successful.")
    return bool(result)
))[1]::integer AS part_no,
                   row_number() OVER (PARTITION BY p.edition_id ORDER BY p.id) AS row_no,
                   lag((regexp_match(p.locator, ' — part ([0-9]+)metadata: dict[str, Any], file_unique_id: str,
                  chat_id: int, message_id: int, file_name: str,
                  file_size: int | None) -> dict:
    chapters = metadata["chapters"]
    with connection() as conn:
        existing = conn.execute(
            """SELECT book_id, edition_id FROM telegram_imports
               WHERE file_unique_id=%s FOR UPDATE""",
            (file_unique_id,),
        ).fetchone()

        if existing and existing["book_id"] and existing["edition_id"]:
            book = conn.execute(
                """UPDATE books SET title=%s, author=%s, language=%s
                   WHERE id=%s RETURNING id,title""",
                (metadata["title"], metadata["author"], metadata["language"],
                 existing["book_id"]),
            ).fetchone()
            edition = conn.execute(
                """UPDATE editions SET title=%s, publisher=%s, edition_year=%s
                   WHERE id=%s RETURNING id""",
                (file_name, metadata["publisher"], metadata["edition_year"],
                 existing["edition_id"]),
            ).fetchone()
            # Re-index in place: preserve the existing book and edition IDs.
            conn.execute("DELETE FROM passages WHERE edition_id=%s",
                         (existing["edition_id"],))
        else:
            book = conn.execute(
                """INSERT INTO books (title,author,language,description)
                   VALUES (%s,%s,%s,%s)
                   RETURNING id,title""",
                (metadata["title"], metadata["author"], metadata["language"],
                 "Imported from the private Telegram FiqhBooks channel; "
                 "original EPUB remains in Telegram."),
            ).fetchone()
            edition = conn.execute(
                """INSERT INTO editions
                   (book_id,title,publisher,edition_year,source_file_key)
                   VALUES (%s,%s,%s,%s,%s)
                   RETURNING id""",
                (book["id"], file_name, metadata["publisher"],
                 metadata["edition_year"], f"telegram:file_unique_id:{file_unique_id}"),
            ).fetchone()

        passage_count = 0
        for locator, content in chapters:
            conn.execute(
                """INSERT INTO passages (edition_id,locator,content)
                   VALUES (%s,%s,%s)""",
                (edition["id"], locator, content),
            )
            passage_count += 1

        conn.execute(
            """UPDATE telegram_imports
               SET book_id=%s, edition_id=%s, status='imported',
                   audit=%s::jsonb, error=NULL, updated_at=now()
               WHERE file_unique_id=%s""",
            (book["id"], edition["id"], json.dumps(metadata.get("audit", {}), ensure_ascii=False), file_unique_id),
        )
    return {
        "book_id": book["id"],
        "title": book["title"],
        "edition_id": edition["id"],
        "indexed_passages": passage_count,
        "audit": metadata.get("audit", {}),
    }


def _mark_failed(file_unique_id: str, error: str) -> None:
    with connection() as conn:
        conn.execute(
            """UPDATE telegram_imports
               SET status='failed', error=%s, updated_at=now()
               WHERE file_unique_id=%s""",
            (error[:1000], file_unique_id),
        )


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    allowed_chat = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
    if not expected or not token:
        raise HTTPException(status_code=503, detail="Telegram integration is not configured")
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, expected
    ):
        raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")

    update = await request.json()
    message = update.get("channel_post")
    if not isinstance(message, dict):
        return {"ok": True, "ignored": "not_a_channel_post"}

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat.get("type") != "channel" or not isinstance(chat_id, int):
        return {"ok": True, "ignored": "not_a_channel"}
    if allowed_chat and str(chat_id) != allowed_chat:
        raise HTTPException(status_code=403, detail="This Telegram channel is not allowed")

    document = message.get("document") or {}
    file_id = document.get("file_id")
    file_unique_id = document.get("file_unique_id")
    file_name = document.get("file_name") or ""
    file_size = document.get("file_size")
    if not file_id or not file_unique_id:
        return {"ok": True, "ignored": "no_document"}
    if not file_name.lower().endswith(".epub"):
        return {"ok": True, "ignored": "only_epub_files_are_supported"}
    if file_size is not None and file_size > MAX_DOWNLOAD_BYTES:
        raise HTTPException(status_code=413, detail="EPUB exceeds Telegram's 20 MB Bot API download limit")

    claimed = _claim_file(
        str(file_unique_id), str(file_id), int(chat_id), int(message.get("message_id", 0)),
        file_name, file_size if isinstance(file_size, int) else None,
    )
    if not claimed:
        return {"ok": True, "duplicate": True}

    # Telegram webhook delivery is event-driven: acknowledge the upload immediately,
    # then process the queued EPUB in the background without waiting for the next poll.
    background_tasks.add_task(process_next_telegram_import, expected)
    return {"ok": True, "status": "queued", "file_name": file_name}


@router.post("/process-next")
def process_next_telegram_import(
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Claim and process one durable queued import; safe to call from the poller."""
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not expected or not token:
        raise HTTPException(status_code=503, detail="Telegram integration is not configured")
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, expected
    ):
        raise HTTPException(status_code=401, detail="Invalid Telegram worker secret")

    with connection() as conn:
        job = conn.execute(
            """WITH candidate AS (
                   SELECT id FROM telegram_imports
                   WHERE status='queued'
                      OR (status='processing'
                          AND processing_started_at < now() - interval '30 minutes')
                   ORDER BY created_at, id
                   FOR UPDATE SKIP LOCKED
                   LIMIT 1
               )
               UPDATE telegram_imports AS job
               SET status='processing',
                   attempt_count=job.attempt_count+1,
                   processing_started_at=now(),
                   updated_at=now(),
                   error=NULL
               FROM candidate
               WHERE job.id=candidate.id
               RETURNING job.file_unique_id,job.file_id,job.chat_id,
                         job.message_id,job.file_name,job.file_size""",
        ).fetchone()
    if not job:
        return {"ok": True, "status": "idle"}

    try:
        binary = _download_epub(token, job["file_id"], job["file_size"])
        metadata = parse_epub(binary)
        result = _store_import(
            metadata, job["file_unique_id"], int(job["chat_id"]),
            int(job["message_id"]), job["file_name"], job["file_size"],
        )
        return {"ok": True, "status": "imported", **result}
    except Exception as exc:
        _mark_failed(job["file_unique_id"], str(exc))
        return JSONResponse(
            {"ok": True, "status": "failed", "error": str(exc)[:300]},
            status_code=200,
        )


def _require_worker_secret(supplied: str | None) -> None:
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if not expected or not os.getenv("TELEGRAM_BOT_TOKEN", ""):
        raise HTTPException(status_code=503, detail="Telegram integration is not configured")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid Telegram worker secret")


@router.get("/status")
def telegram_import_status(
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    limit: int = 50,
):
    """Return queue totals and recent EPUB imports for an authenticated dashboard."""
    _require_worker_secret(x_telegram_bot_api_secret_token)
    limit = max(1, min(int(limit), 100))
    with connection() as conn:
        totals = conn.execute(
            """SELECT
                 count(*)::int AS total,
                 count(*) FILTER (WHERE status='queued')::int AS queued,
                 count(*) FILTER (WHERE status='processing')::int AS processing,
                 count(*) FILTER (WHERE status='imported')::int AS imported,
                 count(*) FILTER (WHERE status='failed')::int AS failed,
                 coalesce(sum(attempt_count),0)::int AS total_attempts
               FROM telegram_imports"""
        ).fetchone()
        recent = conn.execute(
            """SELECT id,file_name,file_size,status,attempt_count,error,
                      book_id,edition_id,audit,created_at,updated_at
               FROM telegram_imports
               ORDER BY created_at DESC,id DESC
               LIMIT %s""",
            (limit,),
        ).fetchall()
    return {
        "ok": True,
        "summary": dict(totals),
        "recent_imports": [dict(row) for row in recent],
        "limit": limit,
    }


@router.post("/retry/{import_id}")
def retry_failed_telegram_import(
    import_id: int,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Requeue one failed EPUB; never resets a running or completed import."""
    _require_worker_secret(x_telegram_bot_api_secret_token)
    with connection() as conn:
        row = conn.execute(
            """UPDATE telegram_imports
               SET status='queued',error=NULL,processing_started_at=NULL,updated_at=now()
               WHERE id=%s AND status='failed' AND file_id IS NOT NULL
               RETURNING id,file_name,status,attempt_count""",
            (import_id,),
        ).fetchone()
    if not row:
        raise HTTPException(
            status_code=409,
            detail="Import not found, not failed, or missing its Telegram file reference",
        )
    return {"ok": True, "message": "Import queued for retry", "import": dict(row)}


@router.post("/register")
async def register_telegram_webhook(request: Request):
    """Register Telegram webhook; protected with the existing MCP bearer token."""
    expected_mcp = os.getenv("MCP_BEARER_TOKEN", "")
    supplied = request.headers.get("authorization", "")
    if not expected_mcp or not hmac.compare_digest(supplied, f"Bearer {expected_mcp}"):
        raise HTTPException(status_code=401, detail="Unauthorized")

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    if not bot_token or not secret or not webhook_url:
        raise HTTPException(status_code=503, detail="Set TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET and TELEGRAM_WEBHOOK_URL")
    if not webhook_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="TELEGRAM_WEBHOOK_URL must use HTTPS")
    result = _telegram_request(
        "setWebhook",
        bot_token,
        {
            "url": webhook_url.rstrip("/") + "/telegram/webhook",
            "secret_token": secret,
            "allowed_updates": ["channel_post"],
            "drop_pending_updates": False,
        },
    )
    return {"ok": True, "webhook_registered": bool(result), "url": webhook_url.rstrip("/") + "/telegram/webhook"}


def attach_telegram_routes(app) -> None:
    app.include_router(router, prefix="/telegram", tags=["Telegram EPUB ingestion"])


def ensure_telegram_webhook_registered() -> bool:
    """Register the webhook automatically on service startup when configured."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    if not bot_token or not secret or not webhook_url:
        missing = [
            name for name, value in (
                ("TELEGRAM_BOT_TOKEN", bot_token),
                ("TELEGRAM_WEBHOOK_SECRET", secret),
                ("TELEGRAM_WEBHOOK_URL", webhook_url),
            ) if not value
        ]
        print("Telegram webhook registration skipped; missing configuration: " + ", ".join(missing))
        return False
    if not webhook_url.startswith("https://"):
        raise ValueError("TELEGRAM_WEBHOOK_URL must use HTTPS")
    result = _telegram_request(
        "setWebhook",
        bot_token,
        {
            "url": webhook_url.rstrip("/") + "/telegram/webhook",
            "secret_token": secret,
            "allowed_updates": ["channel_post"],
            "drop_pending_updates": False,
        },
    )
    print("Telegram webhook registration successful.")
    return bool(result)
))[1]::integer)
                       OVER (PARTITION BY p.edition_id ORDER BY p.id) AS previous_part
            FROM passages p
            JOIN eligible e ON e.edition_id = p.edition_id
        ), valid_editions AS (
            SELECT edition_id
            FROM parsed
            GROUP BY edition_id
            HAVING count(*) FILTER (WHERE part_no IS NULL) = 0
               AND count(*) FILTER (WHERE row_no = 1 AND part_no <> 1) = 0
               AND count(*) FILTER (
                   WHERE part_no > 1
                     AND (previous_part IS NULL OR part_no <> previous_part + 1)
               ) = 0
        ), numbered AS (
            SELECT p.id, p.edition_id, p.locator,
                   sum(CASE WHEN p.part_no = 1 THEN 1 ELSE 0 END)
                     OVER (PARTITION BY p.edition_id ORDER BY p.id
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS section_no
            FROM parsed p
            JOIN valid_editions v ON v.edition_id = p.edition_id
        ), sections AS (
            SELECT edition_id, section_no::integer AS section_no,
                   regexp_replace((array_agg(locator ORDER BY id))[1],
                                  ' — part [0-9]+metadata: dict[str, Any], file_unique_id: str,
                  chat_id: int, message_id: int, file_name: str,
                  file_size: int | None) -> dict:
    chapters = metadata["chapters"]
    with connection() as conn:
        existing = conn.execute(
            """SELECT book_id, edition_id FROM telegram_imports
               WHERE file_unique_id=%s FOR UPDATE""",
            (file_unique_id,),
        ).fetchone()

        if existing and existing["book_id"] and existing["edition_id"]:
            book = conn.execute(
                """UPDATE books SET title=%s, author=%s, language=%s
                   WHERE id=%s RETURNING id,title""",
                (metadata["title"], metadata["author"], metadata["language"],
                 existing["book_id"]),
            ).fetchone()
            edition = conn.execute(
                """UPDATE editions SET title=%s, publisher=%s, edition_year=%s
                   WHERE id=%s RETURNING id""",
                (file_name, metadata["publisher"], metadata["edition_year"],
                 existing["edition_id"]),
            ).fetchone()
            # Re-index in place: preserve the existing book and edition IDs.
            conn.execute("DELETE FROM passages WHERE edition_id=%s",
                         (existing["edition_id"],))
        else:
            book = conn.execute(
                """INSERT INTO books (title,author,language,description)
                   VALUES (%s,%s,%s,%s)
                   RETURNING id,title""",
                (metadata["title"], metadata["author"], metadata["language"],
                 "Imported from the private Telegram FiqhBooks channel; "
                 "original EPUB remains in Telegram."),
            ).fetchone()
            edition = conn.execute(
                """INSERT INTO editions
                   (book_id,title,publisher,edition_year,source_file_key)
                   VALUES (%s,%s,%s,%s,%s)
                   RETURNING id""",
                (book["id"], file_name, metadata["publisher"],
                 metadata["edition_year"], f"telegram:file_unique_id:{file_unique_id}"),
            ).fetchone()

        passage_count = 0
        for locator, content in chapters:
            conn.execute(
                """INSERT INTO passages (edition_id,locator,content)
                   VALUES (%s,%s,%s)""",
                (edition["id"], locator, content),
            )
            passage_count += 1

        conn.execute(
            """UPDATE telegram_imports
               SET book_id=%s, edition_id=%s, status='imported',
                   audit=%s::jsonb, error=NULL, updated_at=now()
               WHERE file_unique_id=%s""",
            (book["id"], edition["id"], json.dumps(metadata.get("audit", {}), ensure_ascii=False), file_unique_id),
        )
    return {
        "book_id": book["id"],
        "title": book["title"],
        "edition_id": edition["id"],
        "indexed_passages": passage_count,
        "audit": metadata.get("audit", {}),
    }


def _mark_failed(file_unique_id: str, error: str) -> None:
    with connection() as conn:
        conn.execute(
            """UPDATE telegram_imports
               SET status='failed', error=%s, updated_at=now()
               WHERE file_unique_id=%s""",
            (error[:1000], file_unique_id),
        )


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    allowed_chat = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
    if not expected or not token:
        raise HTTPException(status_code=503, detail="Telegram integration is not configured")
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, expected
    ):
        raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")

    update = await request.json()
    message = update.get("channel_post")
    if not isinstance(message, dict):
        return {"ok": True, "ignored": "not_a_channel_post"}

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat.get("type") != "channel" or not isinstance(chat_id, int):
        return {"ok": True, "ignored": "not_a_channel"}
    if allowed_chat and str(chat_id) != allowed_chat:
        raise HTTPException(status_code=403, detail="This Telegram channel is not allowed")

    document = message.get("document") or {}
    file_id = document.get("file_id")
    file_unique_id = document.get("file_unique_id")
    file_name = document.get("file_name") or ""
    file_size = document.get("file_size")
    if not file_id or not file_unique_id:
        return {"ok": True, "ignored": "no_document"}
    if not file_name.lower().endswith(".epub"):
        return {"ok": True, "ignored": "only_epub_files_are_supported"}
    if file_size is not None and file_size > MAX_DOWNLOAD_BYTES:
        raise HTTPException(status_code=413, detail="EPUB exceeds Telegram's 20 MB Bot API download limit")

    claimed = _claim_file(
        str(file_unique_id), str(file_id), int(chat_id), int(message.get("message_id", 0)),
        file_name, file_size if isinstance(file_size, int) else None,
    )
    if not claimed:
        return {"ok": True, "duplicate": True}

    # Telegram webhook delivery is event-driven: acknowledge the upload immediately,
    # then process the queued EPUB in the background without waiting for the next poll.
    background_tasks.add_task(process_next_telegram_import, expected)
    return {"ok": True, "status": "queued", "file_name": file_name}


@router.post("/process-next")
def process_next_telegram_import(
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Claim and process one durable queued import; safe to call from the poller."""
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not expected or not token:
        raise HTTPException(status_code=503, detail="Telegram integration is not configured")
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, expected
    ):
        raise HTTPException(status_code=401, detail="Invalid Telegram worker secret")

    with connection() as conn:
        job = conn.execute(
            """WITH candidate AS (
                   SELECT id FROM telegram_imports
                   WHERE status='queued'
                      OR (status='processing'
                          AND processing_started_at < now() - interval '30 minutes')
                   ORDER BY created_at, id
                   FOR UPDATE SKIP LOCKED
                   LIMIT 1
               )
               UPDATE telegram_imports AS job
               SET status='processing',
                   attempt_count=job.attempt_count+1,
                   processing_started_at=now(),
                   updated_at=now(),
                   error=NULL
               FROM candidate
               WHERE job.id=candidate.id
               RETURNING job.file_unique_id,job.file_id,job.chat_id,
                         job.message_id,job.file_name,job.file_size""",
        ).fetchone()
    if not job:
        return {"ok": True, "status": "idle"}

    try:
        binary = _download_epub(token, job["file_id"], job["file_size"])
        metadata = parse_epub(binary)
        result = _store_import(
            metadata, job["file_unique_id"], int(job["chat_id"]),
            int(job["message_id"]), job["file_name"], job["file_size"],
        )
        return {"ok": True, "status": "imported", **result}
    except Exception as exc:
        _mark_failed(job["file_unique_id"], str(exc))
        return JSONResponse(
            {"ok": True, "status": "failed", "error": str(exc)[:300]},
            status_code=200,
        )


def _require_worker_secret(supplied: str | None) -> None:
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if not expected or not os.getenv("TELEGRAM_BOT_TOKEN", ""):
        raise HTTPException(status_code=503, detail="Telegram integration is not configured")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid Telegram worker secret")


@router.get("/status")
def telegram_import_status(
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    limit: int = 50,
):
    """Return queue totals and recent EPUB imports for an authenticated dashboard."""
    _require_worker_secret(x_telegram_bot_api_secret_token)
    limit = max(1, min(int(limit), 100))
    with connection() as conn:
        totals = conn.execute(
            """SELECT
                 count(*)::int AS total,
                 count(*) FILTER (WHERE status='queued')::int AS queued,
                 count(*) FILTER (WHERE status='processing')::int AS processing,
                 count(*) FILTER (WHERE status='imported')::int AS imported,
                 count(*) FILTER (WHERE status='failed')::int AS failed,
                 coalesce(sum(attempt_count),0)::int AS total_attempts
               FROM telegram_imports"""
        ).fetchone()
        recent = conn.execute(
            """SELECT id,file_name,file_size,status,attempt_count,error,
                      book_id,edition_id,audit,created_at,updated_at
               FROM telegram_imports
               ORDER BY created_at DESC,id DESC
               LIMIT %s""",
            (limit,),
        ).fetchall()
    return {
        "ok": True,
        "summary": dict(totals),
        "recent_imports": [dict(row) for row in recent],
        "limit": limit,
    }


@router.post("/retry/{import_id}")
def retry_failed_telegram_import(
    import_id: int,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Requeue one failed EPUB; never resets a running or completed import."""
    _require_worker_secret(x_telegram_bot_api_secret_token)
    with connection() as conn:
        row = conn.execute(
            """UPDATE telegram_imports
               SET status='queued',error=NULL,processing_started_at=NULL,updated_at=now()
               WHERE id=%s AND status='failed' AND file_id IS NOT NULL
               RETURNING id,file_name,status,attempt_count""",
            (import_id,),
        ).fetchone()
    if not row:
        raise HTTPException(
            status_code=409,
            detail="Import not found, not failed, or missing its Telegram file reference",
        )
    return {"ok": True, "message": "Import queued for retry", "import": dict(row)}


@router.post("/register")
async def register_telegram_webhook(request: Request):
    """Register Telegram webhook; protected with the existing MCP bearer token."""
    expected_mcp = os.getenv("MCP_BEARER_TOKEN", "")
    supplied = request.headers.get("authorization", "")
    if not expected_mcp or not hmac.compare_digest(supplied, f"Bearer {expected_mcp}"):
        raise HTTPException(status_code=401, detail="Unauthorized")

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    if not bot_token or not secret or not webhook_url:
        raise HTTPException(status_code=503, detail="Set TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET and TELEGRAM_WEBHOOK_URL")
    if not webhook_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="TELEGRAM_WEBHOOK_URL must use HTTPS")
    result = _telegram_request(
        "setWebhook",
        bot_token,
        {
            "url": webhook_url.rstrip("/") + "/telegram/webhook",
            "secret_token": secret,
            "allowed_updates": ["channel_post"],
            "drop_pending_updates": False,
        },
    )
    return {"ok": True, "webhook_registered": bool(result), "url": webhook_url.rstrip("/") + "/telegram/webhook"}


def attach_telegram_routes(app) -> None:
    app.include_router(router, prefix="/telegram", tags=["Telegram EPUB ingestion"])


def ensure_telegram_webhook_registered() -> bool:
    """Register the webhook automatically on service startup when configured."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    if not bot_token or not secret or not webhook_url:
        missing = [
            name for name, value in (
                ("TELEGRAM_BOT_TOKEN", bot_token),
                ("TELEGRAM_WEBHOOK_SECRET", secret),
                ("TELEGRAM_WEBHOOK_URL", webhook_url),
            ) if not value
        ]
        print("Telegram webhook registration skipped; missing configuration: " + ", ".join(missing))
        return False
    if not webhook_url.startswith("https://"):
        raise ValueError("TELEGRAM_WEBHOOK_URL must use HTTPS")
    result = _telegram_request(
        "setWebhook",
        bot_token,
        {
            "url": webhook_url.rstrip("/") + "/telegram/webhook",
            "secret_token": secret,
            "allowed_updates": ["channel_post"],
            "drop_pending_updates": False,
        },
    )
    print("Telegram webhook registration successful.")
    return bool(result)
, '') AS section_title
            FROM numbered
            GROUP BY edition_id, section_no
        ), inserted AS (
            INSERT INTO pages (edition_id, page_number, page_label, content)
            SELECT edition_id, section_no,
                   'EPUB section ' || section_no || ' — ' || section_title,
                   ''
            FROM sections
            ON CONFLICT (edition_id, page_number) DO NOTHING
            RETURNING id, edition_id, page_number
        ), linked AS (
            UPDATE passages p
            SET page_id = i.id
            FROM numbered n
            JOIN inserted i
              ON i.edition_id = n.edition_id
             AND i.page_number = n.section_no
            WHERE p.id = n.id AND p.page_id IS NULL
            RETURNING p.edition_id
        )
        SELECT
            (SELECT count(*)::integer FROM inserted) AS pages_created,
            (SELECT count(*)::integer FROM linked) AS passages_linked,
            (SELECT count(DISTINCT edition_id)::integer FROM linked) AS editions_mapped
    """
    with connection() as conn:
        row = conn.execute(migration_sql).fetchone()
    return dict(row)


def _store_import(metadata: dict[str, Any], file_unique_id: str,
                  chat_id: int, message_id: int, file_name: str,
                  file_size: int | None) -> dict:
    chapters = metadata["chapters"]
    with connection() as conn:
        existing = conn.execute(
            """SELECT book_id, edition_id FROM telegram_imports
               WHERE file_unique_id=%s FOR UPDATE""",
            (file_unique_id,),
        ).fetchone()

        if existing and existing["book_id"] and existing["edition_id"]:
            book = conn.execute(
                """UPDATE books SET title=%s, author=%s, language=%s
                   WHERE id=%s RETURNING id,title""",
                (metadata["title"], metadata["author"], metadata["language"],
                 existing["book_id"]),
            ).fetchone()
            edition = conn.execute(
                """UPDATE editions SET title=%s, publisher=%s, edition_year=%s
                   WHERE id=%s RETURNING id""",
                (file_name, metadata["publisher"], metadata["edition_year"],
                 existing["edition_id"]),
            ).fetchone()
            # Re-index in place: preserve the existing book and edition IDs.
            conn.execute("DELETE FROM passages WHERE edition_id=%s",
                         (existing["edition_id"],))
        else:
            book = conn.execute(
                """INSERT INTO books (title,author,language,description)
                   VALUES (%s,%s,%s,%s)
                   RETURNING id,title""",
                (metadata["title"], metadata["author"], metadata["language"],
                 "Imported from the private Telegram FiqhBooks channel; "
                 "original EPUB remains in Telegram."),
            ).fetchone()
            edition = conn.execute(
                """INSERT INTO editions
                   (book_id,title,publisher,edition_year,source_file_key)
                   VALUES (%s,%s,%s,%s,%s)
                   RETURNING id""",
                (book["id"], file_name, metadata["publisher"],
                 metadata["edition_year"], f"telegram:file_unique_id:{file_unique_id}"),
            ).fetchone()

        passage_count = 0
        for locator, content in chapters:
            conn.execute(
                """INSERT INTO passages (edition_id,locator,content)
                   VALUES (%s,%s,%s)""",
                (edition["id"], locator, content),
            )
            passage_count += 1

        conn.execute(
            """UPDATE telegram_imports
               SET book_id=%s, edition_id=%s, status='imported',
                   audit=%s::jsonb, error=NULL, updated_at=now()
               WHERE file_unique_id=%s""",
            (book["id"], edition["id"], json.dumps(metadata.get("audit", {}), ensure_ascii=False), file_unique_id),
        )
    return {
        "book_id": book["id"],
        "title": book["title"],
        "edition_id": edition["id"],
        "indexed_passages": passage_count,
        "audit": metadata.get("audit", {}),
    }


def _mark_failed(file_unique_id: str, error: str) -> None:
    with connection() as conn:
        conn.execute(
            """UPDATE telegram_imports
               SET status='failed', error=%s, updated_at=now()
               WHERE file_unique_id=%s""",
            (error[:1000], file_unique_id),
        )


@router.post("/webhook")
async def telegram_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    allowed_chat = os.getenv("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
    if not expected or not token:
        raise HTTPException(status_code=503, detail="Telegram integration is not configured")
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, expected
    ):
        raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")

    update = await request.json()
    message = update.get("channel_post")
    if not isinstance(message, dict):
        return {"ok": True, "ignored": "not_a_channel_post"}

    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    if chat.get("type") != "channel" or not isinstance(chat_id, int):
        return {"ok": True, "ignored": "not_a_channel"}
    if allowed_chat and str(chat_id) != allowed_chat:
        raise HTTPException(status_code=403, detail="This Telegram channel is not allowed")

    document = message.get("document") or {}
    file_id = document.get("file_id")
    file_unique_id = document.get("file_unique_id")
    file_name = document.get("file_name") or ""
    file_size = document.get("file_size")
    if not file_id or not file_unique_id:
        return {"ok": True, "ignored": "no_document"}
    if not file_name.lower().endswith(".epub"):
        return {"ok": True, "ignored": "only_epub_files_are_supported"}
    if file_size is not None and file_size > MAX_DOWNLOAD_BYTES:
        raise HTTPException(status_code=413, detail="EPUB exceeds Telegram's 20 MB Bot API download limit")

    claimed = _claim_file(
        str(file_unique_id), str(file_id), int(chat_id), int(message.get("message_id", 0)),
        file_name, file_size if isinstance(file_size, int) else None,
    )
    if not claimed:
        return {"ok": True, "duplicate": True}

    # Telegram webhook delivery is event-driven: acknowledge the upload immediately,
    # then process the queued EPUB in the background without waiting for the next poll.
    background_tasks.add_task(process_next_telegram_import, expected)
    return {"ok": True, "status": "queued", "file_name": file_name}


@router.post("/process-next")
def process_next_telegram_import(
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Claim and process one durable queued import; safe to call from the poller."""
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    if not expected or not token:
        raise HTTPException(status_code=503, detail="Telegram integration is not configured")
    if not x_telegram_bot_api_secret_token or not hmac.compare_digest(
        x_telegram_bot_api_secret_token, expected
    ):
        raise HTTPException(status_code=401, detail="Invalid Telegram worker secret")

    with connection() as conn:
        job = conn.execute(
            """WITH candidate AS (
                   SELECT id FROM telegram_imports
                   WHERE status='queued'
                      OR (status='processing'
                          AND processing_started_at < now() - interval '30 minutes')
                   ORDER BY created_at, id
                   FOR UPDATE SKIP LOCKED
                   LIMIT 1
               )
               UPDATE telegram_imports AS job
               SET status='processing',
                   attempt_count=job.attempt_count+1,
                   processing_started_at=now(),
                   updated_at=now(),
                   error=NULL
               FROM candidate
               WHERE job.id=candidate.id
               RETURNING job.file_unique_id,job.file_id,job.chat_id,
                         job.message_id,job.file_name,job.file_size""",
        ).fetchone()
    if not job:
        return {"ok": True, "status": "idle"}

    try:
        binary = _download_epub(token, job["file_id"], job["file_size"])
        metadata = parse_epub(binary)
        result = _store_import(
            metadata, job["file_unique_id"], int(job["chat_id"]),
            int(job["message_id"]), job["file_name"], job["file_size"],
        )
        return {"ok": True, "status": "imported", **result}
    except Exception as exc:
        _mark_failed(job["file_unique_id"], str(exc))
        return JSONResponse(
            {"ok": True, "status": "failed", "error": str(exc)[:300]},
            status_code=200,
        )


def _require_worker_secret(supplied: str | None) -> None:
    expected = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    if not expected or not os.getenv("TELEGRAM_BOT_TOKEN", ""):
        raise HTTPException(status_code=503, detail="Telegram integration is not configured")
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid Telegram worker secret")


@router.get("/status")
def telegram_import_status(
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    limit: int = 50,
):
    """Return queue totals and recent EPUB imports for an authenticated dashboard."""
    _require_worker_secret(x_telegram_bot_api_secret_token)
    limit = max(1, min(int(limit), 100))
    with connection() as conn:
        totals = conn.execute(
            """SELECT
                 count(*)::int AS total,
                 count(*) FILTER (WHERE status='queued')::int AS queued,
                 count(*) FILTER (WHERE status='processing')::int AS processing,
                 count(*) FILTER (WHERE status='imported')::int AS imported,
                 count(*) FILTER (WHERE status='failed')::int AS failed,
                 coalesce(sum(attempt_count),0)::int AS total_attempts
               FROM telegram_imports"""
        ).fetchone()
        recent = conn.execute(
            """SELECT id,file_name,file_size,status,attempt_count,error,
                      book_id,edition_id,audit,created_at,updated_at
               FROM telegram_imports
               ORDER BY created_at DESC,id DESC
               LIMIT %s""",
            (limit,),
        ).fetchall()
    return {
        "ok": True,
        "summary": dict(totals),
        "recent_imports": [dict(row) for row in recent],
        "limit": limit,
    }


@router.post("/retry/{import_id}")
def retry_failed_telegram_import(
    import_id: int,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    """Requeue one failed EPUB; never resets a running or completed import."""
    _require_worker_secret(x_telegram_bot_api_secret_token)
    with connection() as conn:
        row = conn.execute(
            """UPDATE telegram_imports
               SET status='queued',error=NULL,processing_started_at=NULL,updated_at=now()
               WHERE id=%s AND status='failed' AND file_id IS NOT NULL
               RETURNING id,file_name,status,attempt_count""",
            (import_id,),
        ).fetchone()
    if not row:
        raise HTTPException(
            status_code=409,
            detail="Import not found, not failed, or missing its Telegram file reference",
        )
    return {"ok": True, "message": "Import queued for retry", "import": dict(row)}


@router.post("/register")
async def register_telegram_webhook(request: Request):
    """Register Telegram webhook; protected with the existing MCP bearer token."""
    expected_mcp = os.getenv("MCP_BEARER_TOKEN", "")
    supplied = request.headers.get("authorization", "")
    if not expected_mcp or not hmac.compare_digest(supplied, f"Bearer {expected_mcp}"):
        raise HTTPException(status_code=401, detail="Unauthorized")

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    if not bot_token or not secret or not webhook_url:
        raise HTTPException(status_code=503, detail="Set TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET and TELEGRAM_WEBHOOK_URL")
    if not webhook_url.startswith("https://"):
        raise HTTPException(status_code=400, detail="TELEGRAM_WEBHOOK_URL must use HTTPS")
    result = _telegram_request(
        "setWebhook",
        bot_token,
        {
            "url": webhook_url.rstrip("/") + "/telegram/webhook",
            "secret_token": secret,
            "allowed_updates": ["channel_post"],
            "drop_pending_updates": False,
        },
    )
    return {"ok": True, "webhook_registered": bool(result), "url": webhook_url.rstrip("/") + "/telegram/webhook"}


def attach_telegram_routes(app) -> None:
    app.include_router(router, prefix="/telegram", tags=["Telegram EPUB ingestion"])


def ensure_telegram_webhook_registered() -> bool:
    """Register the webhook automatically on service startup when configured."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    secret = os.getenv("TELEGRAM_WEBHOOK_SECRET", "")
    webhook_url = os.getenv("TELEGRAM_WEBHOOK_URL", "").strip()
    if not bot_token or not secret or not webhook_url:
        missing = [
            name for name, value in (
                ("TELEGRAM_BOT_TOKEN", bot_token),
                ("TELEGRAM_WEBHOOK_SECRET", secret),
                ("TELEGRAM_WEBHOOK_URL", webhook_url),
            ) if not value
        ]
        print("Telegram webhook registration skipped; missing configuration: " + ", ".join(missing))
        return False
    if not webhook_url.startswith("https://"):
        raise ValueError("TELEGRAM_WEBHOOK_URL must use HTTPS")
    result = _telegram_request(
        "setWebhook",
        bot_token,
        {
            "url": webhook_url.rstrip("/") + "/telegram/webhook",
            "secret_token": secret,
            "allowed_updates": ["channel_post"],
            "drop_pending_updates": False,
        },
    )
    print("Telegram webhook registration successful.")
    return bool(result)
