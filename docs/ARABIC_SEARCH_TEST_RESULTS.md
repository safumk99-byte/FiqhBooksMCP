# Arabic Search Test Results

Date: 2026-11-18

## Test scope

Read-only search tests against the currently indexed Turath segments for book 9059.

## Results

| Query | Results (limit 3) | Observation |
|---|---:|---|
| `قوله` | 3 | Matches found |
| `قَوْلُهُ` | 3 | Same vocalized word is found |
| `الصلاة` | 3 | Matches found |
| `الصَّلَاةُ` | 3 | Vocalized form is found |
| `قو` | 0 | Partial-prefix query did not find matches |

The tests confirm that the current Arabic normalization handles the tested tashkeel/no-tashkeel cases. They do not establish overall search completeness or correctness for every Arabic orthographic variant.

## Storage check

- Neon object storage is enabled for the current branch.
- No object-storage buckets currently exist.
- No bucket, credential, or storage object was created during this audit.

## Implications

1. Keep original book files and large full-text/search data outside the primary application tables.
2. A dedicated search engine/index is still required; object storage alone is not a query engine.
3. Add regression tests for exact phrase search, diacritics, common letter variants, prefixes, partial terms, and multi-token queries.
4. Do not claim full-book coverage until the page/section completeness status reaches 100% and validation tests pass.
