# FiqhBooks സമഗ്ര ഗ്രന്ഥതിരച്ചിൽ — നിലവിലെ പരിശോധന

പരിശോധനാ തീയതി: 2026-11-18

## 1. പരിശോധിച്ച നിലവിലെ സംവിധാനം

- GitHub repository: `safumk99-byte/FiqhBooksMCP`
- Default branch: `main`
- Backend: Python, FastAPI, MCP Python SDK (Streamable HTTP)
- Database: Neon PostgreSQL
- Hosting: Render
- ChatGPT-ൽ FiqhBooks MCP tools നിലവിൽ ലഭ്യമാണ്; അതിനാൽ നിലവിലുള്ള MCP ബന്ധം വികസിപ്പിക്കണം.

## 2. ഡാറ്റാബേസ് പരിശോധനാ ഫലങ്ങൾ

Read-only SQL പരിശോധനയിൽ ലഭിച്ചത്:

| ഇനം | എണ്ണം |
|---|---:|
| രജിസ്റ്റർ ചെയ്ത കിതാബുകൾ | 36 |
| പതിപ്പുകൾ | 28 |
| സാധാരണ സംഭരിച്ച പേജുകൾ | 354 |
| സാധാരണ തിരച്ചിൽ ഭാഗങ്ങൾ | 352 |
| Turath ഉറവിട പേജുകൾ | 66 |
| Turath തിരച്ചിൽ ഭാഗങ്ങൾ | 198 |
| രേഖപ്പെടുത്തിയ Index പിശകുകൾ | 0 |
| ഡാറ്റാബേസ് വലുപ്പം | 13 MB |
| `turath_source_pages` ആകെ വലുപ്പം | 464 kB |
| `turath_segments` ആകെ വലുപ്പം | 2,352 kB |

Turath-ൽ നിന്ന് പ്രാദേശികമായി Index ചെയ്ത പേജുകൾ നിലവിൽ `turath_book_id=9059` എന്ന ഒരു കിതാബിൽ മാത്രമാണ്.

## 3. 9059 കിതാബിന്റെ പൂർണത

- കിതാബ്: تحفة المحتاج في شرح المنهاج وحواشي الشرواني والعبادي
- Turath-ന്റെ ഔദ്യോഗിക page map: 4,675 സാധുവായ പേജ് എൻട്രികൾ
- നിലവിൽ Index ചെയ്തിരിക്കുന്നത്: 66 പേജുകൾ
- ശേഷിക്കുന്നത്: 4,609 പേജുകൾ
- പൂർണത: ഏകദേശം 1.41%
- നിലവിലെ Index ചെയ്ത പേജുകളിൽ നിന്ന് ലഭിച്ച ഭാഗങ്ങൾ: 198
- രേഖപ്പെടുത്തിയ പിശകുകൾ: 0

അതിനാൽ ഈ കിതാബിനെ ഇപ്പോൾ പൂർണമായി തിരയാവുന്നതായി കണക്കാക്കാൻ പാടില്ല.

## 4. നിലവിലെ കോഡിൽ കണ്ടെത്തിയ സാങ്കേതിക വസ്തുതകൾ

- `turath_source_pages.raw_text` എന്ന ഫീൽഡിൽ ഓരോ Index ചെയ്ത പേജിന്റെയും പൂർണ ഉറവിട ടെക്സ്റ്റ് PostgreSQL-ൽ സൂക്ഷിക്കുന്നു.
- `turath_segments.content` എന്ന ഫീൽഡിൽ തിരയാവുന്ന ഭാഗങ്ങൾ PostgreSQL-ൽ സൂക്ഷിക്കുന്നു.
- `turath_segments.search_vector_ar` എന്ന Generated TSVECTOR-നും അതിനുള്ള GIN Index-നും നിലവിലുണ്ട്.
- സാധാരണ passages-നും അറബി തിരച്ചിലിനായി PostgreSQL GIN Index ഉണ്ട്.
- അറബി normalization ഹറകത്തുകളും തത്വീൽ അടയാളവും നീക്കുകയും ചില അക്ഷരരൂപങ്ങൾ ഏകീകരിക്കുകയും ചെയ്യുന്നു.
- Turath composite pages-ൽ വ്യക്തമായ source marker ഉള്ളപ്പോൾ മാത്രമാണ് Matn/Sharh/Hashiyah വേർതിരിക്കുന്നത്; തിരിച്ചറിയാത്ത എഴുത്ത് unclassified ആയി സൂക്ഷിക്കുന്നു.
- `turath_index_batch` പരമാവധി അഞ്ച് പേജുകൾ വീതം Index ചെയ്യുന്നു; പൂർത്തിയായ പേജുകൾ ഒഴിവാക്കി തുടർന്നുപോകാൻ കഴിയും.
- നിലവിലെ `search_turath_segments` തിരച്ചിൽ all-terms PostgreSQL text-search അടിസ്ഥാനത്തിലാണ്; സാധാരണ `search_passages`-ൽ ഉള്ള fallback തിരച്ചിൽ ഇതിൽ നിലവിലില്ല.

## 5. പ്രധാന വിടവുകൾ

1. രജിസ്റ്റർ ചെയ്ത 36 കിതാബുകളും പൂർണമായി തിരയാവുന്നവയല്ല. രജിസ്ട്രേഷൻ, Turath-ൽ ഉള്ളടക്കം ലഭ്യമാകുക, പ്രാദേശികമായി പൂർണമായി Index ചെയ്യുക എന്നിവ വ്യത്യസ്ത നിലകളായി കാണിക്കണം.
2. വലിയ ഗ്രന്ഥങ്ങളുടെ മുഴുവൻ raw text, segments, generated search vectors, GIN indexes എന്നിവ PostgreSQL-ൽ സൂക്ഷിക്കുന്നത് ദീർഘകാലത്ത് ഡാറ്റാബേസ് വലുപ്പം കൂട്ടും.
3. ഓരോ കിതാബിന്റെയും total pages, indexed pages, pending pages, failed pages എന്നിവ കാണിക്കുന്ന പൊതുവായ completeness/status സംവിധാനം MCP/API-യിൽ ഇല്ല.
4. EPUB ingestion, extraction, stable chapter/page locator mapping, background indexing queue എന്നിവ നിലവിലെ പരിശോധിച്ച കോഡിൽ ഇല്ല.
5. Arabic search quality-യ്ക്ക് normalization, exact phrase, prefix/partial-word, tashkeel/no-tashkeel, multi-token coverage എന്നിവയ്ക്കായി പ്രത്യേക regression test suite വേണം.
6. ChatGPT-ക്ക് MCP വഴി ചില തിരച്ചിൽ ഉപകരണങ്ങൾ ലഭ്യമാണെങ്കിലും എല്ലാ കിതാബുകളും പൂർണമായി ഉൾപ്പെട്ടിട്ടുണ്ടെന്ന് ഉറപ്പാക്കുന്ന coverage/status സംവിധാനം വേണം.

## 6. ശുപാർശ ചെയ്യുന്ന അടുത്ത പ്രവർത്തനങ്ങൾ

### ഘട്ടം A — നിലവിലെ സംവിധാനം സുരക്ഷിതമാക്കുക
- നിലവിലെ ഡാറ്റാബേസ്, source attribution, page locators എന്നിവ മാറ്റാതെ സൂക്ഷിക്കുക.
- കിതാബ്/പതിപ്പ്/ഉറവിടം അടിസ്ഥാനമാക്കിയ completeness audit ചേർക്കുക.
- എല്ലാ നിലവിലുള്ള കിതാബുകളെയും `metadata_only`, `source_available`, `partially_indexed`, `fully_indexed`, `failed` എന്നീ വ്യക്തമായ നിലകളിൽ വർഗ്ഗീകരിക്കുക.

### ഘട്ടം B — വേർതിരിച്ച Search Index രൂപകൽപ്പന
- PostgreSQL-ൽ പുസ്തക metadata, edition metadata, source references, indexing status എന്നിവ മാത്രം നിലനിർത്തുക.
- പൂർണമായ searchable text/index പ്രത്യേകം Search Storage/Engine-ൽ സൂക്ഷിക്കുക.
- യഥാർത്ഥ EPUB/PDF ഫയലുകൾ PostgreSQL-ൽ സൂക്ഷിക്കരുത്.
- ചെലവും സംഭരണ പരിധിയും പരിശോധിച്ചശേഷം മാത്രമേ Search Engine/Storage തിരഞ്ഞെടുക്കാവൂ.

### ഘട്ടം C — പൂർണമായ ingestion
- EPUB-യിലെ എല്ലാ spine/content documents-ഉം Extract ചെയ്യുക.
- Turath-ന്റെ ഔദ്യോഗിക page map അനുസരിച്ച് എല്ലാ സാധുവായ പേജുകളും ശേഖരിക്കുക.
- യഥാർത്ഥ page/volume locators സംരക്ഷിക്കുക; ലഭ്യമല്ലാത്ത printed page നമ്പർ ഊഹിക്കരുത്.
- ഓരോ കിതാബിന്റെയും extraction/indexing completeness പരിശോധിക്കുക.

### ഘട്ടം D — ChatGPT MCP ബന്ധം
- നിലവിലുള്ള MCP server-ൽ global search, book-scoped search, read-page, book-index-status ഉപകരണങ്ങൾ നൽകുക.
- ഫലങ്ങളിൽ കിതാബ്, പതിപ്പ്, വാള്യം, പേജ്/locator, യഥാർത്ഥ അറബി ഉദ്ധരണം എന്നിവ നിർബന്ധമാക്കുക.
- അപൂർണമായ കിതാബുകളെ പൂർണമായി തിരഞ്ഞതായി അവകാശപ്പെടാതിരിക്കുക.

## 7. സുരക്ഷാ നിയന്ത്രണങ്ങൾ

- ഈ പരിശോധനയിൽ ഉപയോഗിച്ചത് read-only SQL മാത്രമാണ്.
- നിലവിലെ production database-ൽ schema/data മാറ്റങ്ങളൊന്നും നടത്തിയിട്ടില്ല.
- ഈ audit branch-ൽ മാത്രമാണ് ഈ രേഖ ചേർത്തിരിക്കുന്നത്; `main` branch മാറ്റിയിട്ടില്ല.
- Search storage തിരഞ്ഞെടുക്കുന്നതിന് മുമ്പ് ചെലവ്, സൗജന്യ പരിധി, ഡാറ്റാ ഉടമസ്ഥത, backup/restore എന്നിവ വിലയിരുത്തണം.
