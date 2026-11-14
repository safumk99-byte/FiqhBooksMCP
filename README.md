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
