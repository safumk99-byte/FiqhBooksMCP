# FiqhBooks Search Architecture Plan

Date: 2026-11-18

## Decision direction

Use a hybrid search architecture so that the primary Neon PostgreSQL database does not become the storage location for every page of every book.

### A. Turath-sourced books

Where a FiqhBooks record has a verified Turath book ID, search the Turath full-text endpoint directly and preserve the returned page attribution.

A live read-only test on 2026-11-18 confirmed results for these sample books with the Arabic query `الصلاة`:

| Turath book ID | Matching pages reported | Results returned in sample |
|---:|---:|---:|
| 9059 | 1,244 | 20 |
| 21623 | 263 | 20 |
| 1655 | 451 | 20 |
| 6140 | 153 | 20 |
| 20561 | 493 | 20 |

This shows that the upstream source can search much more than the 66 pages currently copied into the local index for book 9059. It does not, by itself, prove coverage for every registered book or every query.

### B. User-provided EPUB books

For books without a verified Turath full-text source:
- Store the original EPUB outside PostgreSQL.
- Extract all EPUB content documents and preserve chapter/section locators.
- Build a dedicated searchable index outside the primary application tables.
- Keep only source metadata, file references, edition information, indexing status, and locators in PostgreSQL.
- Do not invent printed page numbers when an EPUB has no printed page map.

Neon object storage is enabled on the current branch, but no buckets or credentials have been created. Object storage can hold original files or index snapshots; it is not itself a search engine.

### C. ChatGPT access

The existing FiqhBooks MCP connection is already available in this ChatGPT environment. Extend it with:
- `get_library_search_status`: show which books have a Turath source, local passages, or no searchable source.
- `search_all_books`: search all Turath-linked books and combine those results with local passage results where a remote source does not return a match.

These tools are being added on the isolated feature branch and are not deployed to the production Render service.

## Current inventory

- 36 registered book records.
- 27 records have a Turath numeric source ID in their current description.
- 2 records have local searchable passages; these contain 352 passages in total (347 for زبد ابن رسلان and 5 for مغني المحتاج).
- 8 records have neither a Turath source ID nor local searchable passages.
- 3 records have locally stored pages; this does not mean those pages are all indexed for search.

## Required follow-up before production

1. Replace source IDs embedded in free-text descriptions with structured `book_sources` records.
2. Add an automated completeness status for each edition/source.
3. Validate the new all-books MCP search tool through CI and a staging deployment.
4. Add an EPUB ingestion pipeline and a dedicated external search index.
5. Verify exact phrase, diacritics/no-diacritics, partial-word, multi-token, page locator, and source attribution behavior.
6. Only then mark a book as fully searchable.

## Data protection

No production records were deleted or rewritten during the audit. The current implementation changes are isolated in `audit/full-text-search-readiness`. No production database migration, storage bucket, credential, or Render deployment has been created.
