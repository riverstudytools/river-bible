# River Bible — Database Architecture Specification

`bible.sqlite`, accessed via `better-sqlite3`, shared identically by the
Express server (`server.js`, HTTP/JSON, read-only) and the Electron main
process (`main.js`, IPC). All SQL lives in one place — `db-queries.js` —
so both transports stay in sync and no client can ever inject raw SQL.

The schema is built incrementally by four independent scripts, run in
this order against the same `bible.sqlite`:

1. `build_db.py` — core schema (`translations`, `books`, `verses`, `words`, `edits`, `lexicon_words`, `lexicon_sources`, `lexicon_entries`)
2. `build_search_index.py` — `verse_text`, `verses_fts` (full-text search)
3. `build_word_index.py` — `lexicon_word_occurrences`, `lexicon_gloss_stats` (word-study/occurrences)
4. `build_versification_index.py` — `versification_diffs`, plus adds `translations.versification`

Steps 2–4 are optional passes, each rerunnable independently, and each
rebuilds its own tables from scratch (`DROP`/`DELETE` + reinsert) from
the current state of `words`/`edits` — so they're safe to rerun any
time the underlying text or lexicon data changes. Every one of these
optional tables is *feature-detected at startup*
(`searchReady`, `lexiconReady`, `occurrencesReady`, `versificationReady`)
rather than assumed to exist, so a `bible.sqlite` built at an earlier
pipeline stage degrades gracefully instead of crashing.

---

## 1. Core text tables

### `translations`
One row per Bible translation/version. (`versification` is technically added by `build_versification_index.py`, not `build_db.py` — grouped here since it's a core translation attribute.)

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `code` | TEXT UNIQUE NOT NULL | short version code, e.g. `KJV`, `ESV` |
| `name` | TEXT NOT NULL | display name |
| `language` | TEXT NOT NULL DEFAULT `'English'` | |
| `copyright_short` | TEXT | *(added by migration, optional)* shown per-translation |
| `license_text` | TEXT | *(added by migration, optional)* full license/copyright text |
| `versification` | TEXT | *(added by migration, optional)* `'MT'`, `'LXX'` or `'VUL'`, optionally followed by `/` and what the NT's cross-references into the OT expect — e.g. `'LXX/MT'` = Septuagint-numbered OT, but NT xrefs written in MT/English numbering. No slash means the NT expects the same numbering as the OT. NULL treated as `'MT'` client-side. The first part tells compare mode and the Xrefs sidebar which numbering this translation's own text follows; both parts drive how OT references in footnotes/cross-reference lines are renumbered and linked (see §4) |
| `info_html` | TEXT | *(added by migration, optional)* "About this translation" HTML shown on a new tab |

`db-queries.js` checks `PRAGMA table_info(translations)` at startup and
only selects the optional columns if present — this is the graceful-
degradation pattern used throughout.

### `books`
Seeded once from a fixed 66-book canon list (`BOOKS` in `build_db.py`); apocryphal/other books can be added beyond it.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `name` | TEXT UNIQUE NOT NULL | e.g. `"Genesis"` |
| `book_code` | TEXT | short code, populated by import scripts |
| `testament` | TEXT CHECK IN `('OT','NT','AP')` | derived from canon position; `AP` for anything not in the fixed list |
| `book_order` | INTEGER NOT NULL | canonical ordering (0-based, per `BOOKS` index) |

### `verses`
One row per (translation, book, chapter, verse) — the text-container level; the actual words live in `words`.

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `translation_id` | INTEGER → `translations.id` ON DELETE CASCADE | |
| `book_id` | INTEGER → `books.id` ON DELETE CASCADE | |
| `chapter` | INTEGER NOT NULL | |
| `verse` | INTEGER NOT NULL | |
| — | UNIQUE(`translation_id`,`book_id`,`chapter`,`verse`) | |

Index: `idx_verses_lookup(translation_id, book_id, chapter, verse)` — the primary lookup path for "give me this book/chapter in this translation."

### `words`
The interlinear core: one row per token (original-language word *or* English rendering token, depending on translation type), in reading order.

| Column | Notes |
|---|---|
| `id` | PK |
| `verse_id` | → `verses.id` ON DELETE CASCADE |
| `position` | order within the verse |
| `lang` | e.g. `Hebrew`, `Greek`, `Syriac`, `Latin`, `Samaritan`, or an English translation's own language |
| `original` | the original-language text of this token (or English word for translation-only rows) |
| `translit` | transliteration |
| `parsing` | morphological parsing code |
| `strong` | Strong's number — see the **Strong's-number-family** note below; sometimes bare (`'3056'`) rather than prefixed (`'G3056'`), normalized at query time |
| `lemma` | dictionary/lemma form *(added by a later migration — `ensure_schema` ALTERs it in if missing)* |
| `gloss` | this token's English gloss |
| `beg_q`, `end_q` | opening/closing quotation-mark rendering hints |
| `pnc` | punctuation following the word |
| `end_text` | trailing text/punctuation rendering hint |
| `footnote` | footnote text/marker |
| `hdg` | section heading text, when this "word" is actually a heading marker |
| `crossref` | cross-reference text |
| `par_classes` | paragraph/formatting CSS-class hints for rendering |

Indexes: `idx_words_verse(verse_id, position)` (verse text assembly), `idx_words_strong(strong) WHERE strong IS NOT NULL` (Strong's lookups).

**Strong's-number family (`STRONG_RE = /^[GHLS]\d{1,6}[A-Za-z]?$/`):** `G`/`H` are real Strong's Greek/Hebrew numbers; `L` (Latin, Lewis & Short, sequential IDs assigned at import) and `S` (Syriac, SEDRA's own lexeme IDs) are synthetic extensions of the same shape for languages Strong's never covered. Everywhere downstream — lookup, lexeme matching, occurrences — treats all four identically.

### `edits`
User/editor overrides to a single word's gloss, keyed 1:1 to `words`.

| Column | Notes |
|---|---|
| `word_id` | PK, → `words.id` ON DELETE CASCADE |
| `gloss` | overriding gloss text |
| `updated_at` | timestamp |

**Precedence rule used everywhere a gloss is displayed:** `edits.gloss` wins over `words.gloss` when present (`COALESCE(e.gloss, w.gloss)` / `LEFT JOIN edits`).

---

## 2. Lexicon tables

### `lexicon_words`
One row per lemma (dictionary headword), keyed by Strong's-family ID.

| Column | Notes |
|---|---|
| `id` | PK |
| `strong` | UNIQUE NOT NULL — `'G3972'`, `'H7257'`, etc. |
| `lang` | `'Greek'` or `'Hebrew'` (and by extension Latin/Syriac given the `L`/`S` extension) |
| `lexeme` | the lemma spelling itself |
| `translit` | transliteration |
| `pronunciation` | phonetic guide |
| `short_gloss` | brief definition |

Index: `idx_lexicon_strong(strong)`.

### `lexicon_sources`
One row per dictionary source (Thayer, BDB, Dodson, LSJ, etc.).

| Column | Notes |
|---|---|
| `id` | PK |
| `code` | UNIQUE — e.g. `'THAYER'`, `'BDB'` |
| `name` | display name |
| `copyright` | licensing info |

### `lexicon_entries`
The actual dictionary text — many-to-many join of lemma × source.

| Column | Notes |
|---|---|
| `id` | PK |
| `strong_id` | → `lexicon_words.id` ON DELETE CASCADE |
| `source_id` | → `lexicon_sources.id` ON DELETE CASCADE |
| `entry_html` | NOT NULL — HTML-formatted entry body for the popover UI |
| — | UNIQUE(`strong_id`,`source_id`) — one entry per lemma per source |

Index: `idx_lexicon_entries(strong_id, source_id)`.

A single Strong's number can thus surface several dictionary entries at once (one per source) — `getLexiconEntry` returns `{ lemma, entries: [...] }`.

---

## 3. Optional pipeline-built tables

Each of these is created by its own standalone script and is
feature-detected via `sqlite_master` at query-module startup; every
feature built on it fails soft (`503 httpError`) rather than crashing
the app if the table is absent.

### `verse_text` *(built by `build_search_index.py`)*
A flattened, one-row-per-verse plain-text companion to `words` (rather than one row per token) — used to cheaply attach display text to search hits and to word-occurrence rows without reassembling from `words`.

| Column | Type | Notes |
|---|---|---|
| `translation_id` | INTEGER → `translations.id` ON DELETE CASCADE | part of composite PK |
| `book_id` | INTEGER → `books.id` ON DELETE CASCADE | part of composite PK |
| `chapter` | INTEGER NOT NULL | part of composite PK |
| `verse` | INTEGER NOT NULL | part of composite PK |
| `text` | TEXT NOT NULL | flattened verse text |
| — | PRIMARY KEY(`translation_id`,`book_id`,`chapter`,`verse`) | |

Index: `idx_verse_text_lookup(translation_id, book_id, chapter, verse)`.

**Build logic:** for each verse, tokens are pulled in `position` order (`COALESCE(edits.gloss, words.gloss, words.original, '')` — the same edit-wins precedence used everywhere else) and joined as `` `{beg_q}{gloss}{pnc}` `` per token, then space-joined and trimmed into one string. Fully rebuilt on every run (`DELETE FROM verse_text` first, `INSERT OR REPLACE`).

### `verses_fts` *(FTS5 virtual table, built by `build_search_index.py`)*
```sql
CREATE VIRTUAL TABLE verses_fts USING fts5(
    text,
    translation_id UNINDEXED,
    book_id UNINDEXED,
    book_name UNINDEXED,
    chapter UNINDEXED,
    verse UNINDEXED,
    testament UNINDEXED,
    tokenize='porter unicode61'
);
```
External-contentless FTS5 table (it owns its own copy of `text`, not a `content=` shadow of `verse_text`) using the Porter-stemming Unicode61 tokenizer. All metadata columns are `UNINDEXED` — present for filtering/display but not tokenized for search — so only `text` itself is actually matched against. Populated from the exact same per-verse flattening pass as `verse_text` (built in lockstep, one row per translation × verse). **Dropped and fully recreated on every run** (`DROP TABLE` + `CREATE VIRTUAL TABLE`), unlike the other optional tables, which use `CREATE TABLE IF NOT EXISTS` — this is how schema changes to the FTS column set get picked up.

Queried with FTS5 `MATCH` and ranked with `bm25()`; results are grouped down to one best-ranked hit per (book, chapter, verse) across translations, then optionally filtered to a caller-supplied book list.

### `lexicon_word_occurrences` *(built by `build_word_index.py`)*

| Column | Type | Notes |
|---|---|---|
| `strong` | TEXT NOT NULL | normalized (prefixed, uppercase) Strong's number; part of PK |
| `book_id` | INTEGER NOT NULL | part of PK |
| `book_name` | TEXT NOT NULL | denormalized for display without a join |
| `book_order` | INTEGER NOT NULL | denormalized for canonical-order sorting without a join |
| `testament` | TEXT | |
| `chapter` | INTEGER NOT NULL | part of PK |
| `verse` | INTEGER NOT NULL | part of PK |
| `occurrence_count` | INTEGER NOT NULL DEFAULT 1 | times this Strong's number recurs *within the same verse* |
| — | PRIMARY KEY(`strong`,`book_id`,`chapter`,`verse`) | |

Index: `idx_word_occurrences_strong(strong, book_order, chapter, verse)`.

Powers the "All occurrences" word-study sidebar: every distinct verse location a given Strong's number occurs at, **deduplicated across translations** — location is translation-independent (the same original-language word sits at the same verse regardless of which translation's interlinear is being read). Built by scanning every token across every translation, counting occurrences per (strong, translation, location), then collapsing to one row per location by taking the **max** count across translations (not summing) — so a word occurring twice in a verse reads as `2`, not `2 × number of translations carrying that verse`. Strong's numbers are normalized identically to `NORMALIZED_WORD_STRONG` in `db-queries.js` (bare `'3056'` → `'G3056'`/`'H3056'` by `words.lang`) before aggregating. Table is dropped and rebuilt from scratch on every run.

### `lexicon_gloss_stats` *(built by `build_word_index.py`)*

| Column | Type | Notes |
|---|---|---|
| `strong` | TEXT NOT NULL | part of PK |
| `translation_id` | INTEGER NOT NULL | part of PK — gloss frequency is per-translation |
| `gloss` | TEXT NOT NULL | display text for this gloss bucket; part of PK |
| `occurrence_count` | INTEGER NOT NULL | how many times this gloss was used for this word in this translation |
| — | PRIMARY KEY(`strong`,`translation_id`,`gloss`) | |

Index: `idx_gloss_stats_strong(strong, occurrence_count DESC)`.

Per-translation gloss frequency for a Strong's number — which English glosses render that word, and how often, translation by translation; backs the frequency chips in the Occurrences sidebar. **Normalization before counting:** bracket/brace-wrapped "supplied" text is stripped (`"the [one]"` → `"the"`); glosses are then case-folded so `"The"`/`"the"`/`"THE"` bucket together — **except** whole-word `god`/`lord`, whose casing is theologically significant (`God` vs. `god`; `LORD`/`Lord`/`lord`) and is always kept as a distinct bucket regardless of the general fold. Within each folded bucket, the most-frequent literal casing/spacing (ties broken alphabetically) is stored as the display `gloss`. Table is dropped and rebuilt from scratch on every run.

### `versification_diffs` *(built by `build_versification_index.py`)*

| Column | Type | Notes |
|---|---|---|
| `book` | TEXT NOT NULL | River's own lowercase book key (matches `lower(books.name)`); part of PK |
| `std_chapter` | INTEGER NOT NULL | English/KJV-standard (MT-tradition) chapter; part of PK |
| `std_verse` | INTEGER NOT NULL | English/KJV-standard verse; part of PK |
| `lxx_ref` | TEXT NOT NULL | verbatim LXX-side reference string from TVTMS |
| — | PRIMARY KEY(`book`,`std_chapter`,`std_verse`) | |

Loaded from STEPBible's TVTMS dataset (CC BY 4.0) via a `mt_lxx_diffs.tsv` produced by a separate `extract_mt_lxx.py`. OT-only — TVTMS's Ancient Versions annotations never cover the NT. `lxx_ref` is most often a plain `"6:1b"`/`"5:32-6:1"`-style reference but is occasionally free text for a handful of badly-scrambled passages (Exodus 28, parts of Jeremiah/1 Kings); the client falls back to "not available" for those rather than guessing. Rows for books absent from the current `bible.sqlite` (e.g. an NT-only build) are skipped at load time rather than inserted orphaned. This script is also where `translations.versification` actually gets added and populated — see next section.

This script also **best-effort auto-tags** `translations.versification`: any translation whose `code` is in a known-LXX-code set (`LXE`, `NETS`, `LES`, `ABP`, `LXX`) is tagged `'LXX'` automatically; anything else is prompted for interactively (blank answer → `'MT'`, the default). It never overwrites a `versification` value already set. The prompt accepts `lxx/mt`-style answers (OT numbering / NT-xref expectation, stored normalized as `LXX/MT`); `--retag` re-asks about every translation (current value as default) to add that second half to automatically-tagged ones.

---

## 4. How the pieces fit together

```
translations ──┐
               │
   books ──┬── verses ──┬── words ──── edits            (interlinear text + user overrides)
           │            │      │
           │            │      └── lexicon_words ──┬── lexicon_entries ── lexicon_sources
           │            │           (via strong)    │
           │            │                           └── lexicon_word_occurrences
           │            │                                lexicon_gloss_stats
           │            │
           │            └── verse_text                    (flattened plain text per verse)
           │
           └── versification_diffs                        (MT↔LXX numbering, keyed by book name)

verses_fts (FTS5, denormalized snapshot of verse text across translations, for search)
```

**Reading a chapter (`getBookWords`):** `verses` (filtered to translation+book) → join `books` → join `words` (ordered by `position`) → left join `edits` (gloss override) → left join `lexicon_words` (via normalized `strong`) for the popover's quick-glance lemma/translit/pronunciation/short-gloss, without a full lexicon lookup.

**Word study / lexicon popover:** click a word → `lexicon_words` + `lexicon_entries` + `lexicon_sources` → full multi-source dictionary entry. If the word has no Strong's number, its literal spelling (`words.original`) is matched against other tokens that *do* carry one, via `strongsByLiteralText` — this is the `resolveLexicalQuery` fallback chain: Strong's number → exact lexeme match → literal-text match → fuzzy substring match across lexeme/translit/short_gloss.

**Occurrences sidebar:** resolved Strong's number → `lexicon_word_occurrences` for every location, left-joined to `verse_text` for a snippet, plus a correlated subquery back into `words`/`edits` for the *exact* gloss used at that verse in the currently active translation (not just aggregate stats) → `lexicon_gloss_stats` separately for the frequency breakdown shown as filter chips.

**Search:** client builds an FTS5 `MATCH` expression (never raw user SQL) → `verses_fts`, optionally book-filtered, ranked by `bm25()`, deduplicated to best hit per verse-location, then left-joined to `verse_text` for the currently active translation's own wording of that verse.

**OT reference links (footnotes and cross-reference lines):** a reference to an OT book found in a footnote or a `crossref` line is first read in the numbering its source book implies — the translation's NT half of `versification` when the footnote/xref sits in an NT book, its OT half otherwise — then converted (via the same `versification_links` edges, cached per book in the client) into the numbering of the translation that will open. Each such link is followed by a link for the other Hebrew text type (MT<->LXX; Vulgate not offered yet) that opens in the first translation of that type in the user's translation list which actually has the book. A verse with no equivalent in one type simply gets no link for it, and the remaining link is labelled with its type. Nothing is fetched at all when the library has only one Hebrew text type and the tag has no NT half.

**Versification/compare mode:** book name → `versification_diffs`, loaded whole-book-at-a-time and resolved client-side per verse, the same lazy-load-then-cache pattern used for chapter text.

---

## 5. API surface (server.js) — how the tables reach the client

| Endpoint | Backing query | Tables touched |
|---|---|---|
| `GET /api/translations` | `listTranslations` | `translations` |
| `GET /api/books/:translationId` | `getAvailableBooks` | `books`, `verses` |
| `GET /api/books/:translationId/:bookKey` | `getBookWords` | `verses`, `books`, `words`, `edits`, `lexicon_words` |
| `GET /api/books/:translationId/:bookKey/structure` | `getBookStructure` | `verses`, `books` (chapter/verse numbers only, no word text) |
| `GET /api/search` | `search` | `verses_fts`, `translations`, `verse_text` |
| `GET /api/lexicon/:strong` | `getLexiconEntry` | `lexicon_words`, `lexicon_entries`, `lexicon_sources` |
| `GET /api/lexicon/:strong/occurrences` | `getWordOccurrences` | `lexicon_word_occurrences`, `verse_text`, `words`, `edits`, `lexicon_gloss_stats` |
| `GET /api/lexicon-search/:query` | `searchLexicon` | `lexicon_words` (+ `lexicon_word_occurrences` for ranking) |
| `GET /api/lexicon-search/occurrences/:query` | `getWordOccurrencesByQuery` | same as `/lexicon/:strong/occurrences`, via lexeme resolution |
| `GET /api/versification/:bookKey` | `getVersificationDiffs` | `versification_diffs` |

Electron's `main.js`/`preload.js` expose the identical `createQueries(db)` object over IPC rather than HTTP — same functions, same tables, no separate SQL path.

---

## 6. Notes for extending the schema

- **Add a new optional feature table the same way the existing ones do:** write it as its own standalone `build_*.py` script that can rerun independently and rebuilds its table(s) from scratch each time (`DROP`/`DELETE` + reinsert from current `words`/`edits` state — see `build_search_index.py`/`build_word_index.py`/`build_versification_index.py`), detect it via `sqlite_master` at `createQueries()` startup into a `*Ready` boolean, gate every statement that touches it behind that flag, and throw a `503 httpError` (not a crash) when it's absent. This is what lets `bible.sqlite` files from different pipeline stages all keep working, and lets a downstream table be refreshed after a text/lexicon edit without rerunning the whole pipeline.
- **New per-word metadata** belongs as a new nullable column on `words` (like `lemma` was added via `ALTER TABLE ... ADD COLUMN`), not a new table, unless it's genuinely 1:many per word.
- **New per-translation metadata** (like `copyright_short`/`versification`/`info_html`) belongs as a nullable column on `translations`, added via the same `PRAGMA table_info` + conditional-ALTER pattern, so old databases degrade instead of breaking.
- **Any new original-language corpus** reuses `words`/`verses`/`translations` as-is — `lang` and `strong` are what distinguish it, not a parallel table.
- **Any new lexicon/dictionary source** is just a new `lexicon_sources` row plus `lexicon_entries` rows — never a parallel lexicon table — provided its headwords key naturally into the Strong's-family ID scheme (or can be assigned a new synthetic prefix the way Latin got `L` and Syriac got `S`).
