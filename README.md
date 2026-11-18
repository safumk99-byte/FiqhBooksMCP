# FiqhBooksMCP

A private-by-token, Streamable HTTP MCP server for organizing fiqh books, editions, pages, searchable passages, citations, and research notes.

## Stack
- Python 3.12+
- FastAPI + MCP Python SDK (Streamable HTTP)
- Neon PostgreSQL
- Render Web Service

## Local setup
1. Create a virtual environment.
2. Install dependencies: `pip install -r requirements.txt`
3. Copy `.env.example` to `.env` and set `DATABASE_URL` and `MCP_BEARER_TOKEN`.
4. Run: `uvicorn app.main:app --host 0.0.0.0 --port 8000`

Health: `GET /health`  
MCP: `POST/GET/DELETE /mcp` (Streamable HTTP; Bearer token required)

The database schema is initialized automatically on startup. PDF binaries are intentionally not stored in PostgreSQL; store them in durable object storage and keep their object keys/metadata in the database.


## Telegram EPUB ingestion

The optional Telegram integration imports EPUB documents posted to a private channel
where the bot is an administrator. The original EPUB remains in Telegram; extracted
chapter chunks are stored as searchable passages in PostgreSQL (the content is needed
for full-text search). EPUB results use chapter/section locators, not invented printed
page numbers.

Configure these Render environment variables (never commit their values):

- \`TELEGRAM_BOT_TOKEN\`: token issued by @BotFather.
- \`TELEGRAM_WEBHOOK_SECRET\`: 32+ random characters using only A-Z, a-z, 0-9, underscore or hyphen.
- \`TELEGRAM_WEBHOOK_URL\`: \`https://fiqhbooksmcp-1.onrender.com\`.
- \`TELEGRAM_ALLOWED_CHAT_ID\`: optional numeric private-channel chat ID; recommended after the first webhook test.

Then call \`POST /telegram/register\` with \`Authorization: Bearer <MCP_BEARER_TOKEN>\`.
Telegram will send channel posts to \`POST /telegram/webhook\`. The bot must be a channel
administrator and the document must be an EPUB. Duplicate files are detected using
Telegram's \`file_unique_id\`.

**Telegram Bot API download limit:** the standard \`getFile\` method currently allows
the bot to download files up to 20 MB. Larger EPUBs are rejected rather than partially
indexed. The extractor also applies ZIP expansion and text-size safety limits.

No original EPUB binary is stored in PostgreSQL. Searchable extracted passages are
stored there and receive PostgreSQL Arabic-normalized GIN search indexes.

### Import queue status and retry

The worker status endpoint returns queue totals and the latest imports for an
authenticated monitoring client:

```http
GET /telegram/status?limit=50
X-Telegram-Bot-Api-Secret-Token: <TELEGRAM_WEBHOOK_SECRET>
```

A single failed import can be queued again without re-uploading the EPUB:

```http
POST /telegram/retry/<import_id>
X-Telegram-Bot-Api-Secret-Token: <TELEGRAM_WEBHOOK_SECRET>
```

Only records currently marked `failed` and retaining a Telegram file reference
can be retried. The endpoint does not reset active or completed imports. Queue
processing continues through the scheduled poller in batches of up to four jobs.
