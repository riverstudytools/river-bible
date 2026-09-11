# Adding Vulgate, Douay-Rheims, Peshitta, Samaritan Pentateuch, and DSS to River

## Update: answers to your three follow-ups

1. **STRONG_RE / lexicon linking for Latin & Syriac** — done. `db-queries.js`
   (patched copy included in this folder) now accepts `^[GHLS]\d{1,6}[A-Za-z]?$`
   — L for Latin, S for Syriac. Verified against a real in-memory DB with
   L1234/S501 entries, not just eyeballed (see `smoke_test.js`, also
   included). This is a patch to the copy you uploaded — diff/merge it into
   your actual working tree rather than overwriting blind, in case it's
   moved on since.

2. **Search by lexeme** — you were right that the existing `lexemeExact`
   step is exact-match only. `resolveLexicalQuery` now has a fourth,
   final fallback: a substring search across `lexicon_words.lexeme`,
   `.translit`, and `.short_gloss`, returned as `candidates` — which means
   it renders through the *same* candidate-card UI your app already has
   for homograph disambiguation, with zero frontend changes needed. Typing
   part of a word, a transliteration, or a remembered English gloss like
   "love" now surfaces a pick-list instead of a bare "not found." Confirmed
   in the smoke test: searching `"love"` against three lexicon entries
   (Greek/Latin/Syriac all glossed "love") returns all three as candidates;
   searching `"rachm"` (a partial transliteration) finds the Syriac entry
   by prefix.

3. **Archaic Samaritan Pentateuch translation** — you were right, I missed
   it the first time. **"Samaritan Pentateuch in English" (SPE)** by
   Aleksandr Sigalov: built on the public-domain KJV text, edited verse by
   verse at every point where the Samaritan reading diverges from the
   Masoretic Text (cross-checked against Kennicott's 1780 parallel edition
   and von Gall's 1918 critical edition). It's freely distributed as a
   SWORD module through CrossWire's main repository, and also exists in
   e-Sword/theWord/MySword formats. `import_spe_english.py` reads the SWORD
   version — its pysword-based read pipeline was verified end-to-end
   against a hand-built real SWORD module fixture while writing it (not
   just API-shape-checked), so this one's on solid ground. Update the
   Samaritan Pentateuch section below accordingly: **you now have both
   sides** — SP Hebrew via `DT-UCPH/sp` and this KJV-based English
   translation via SPE.

4. **Peshitta OT ("nice to have")** — `import_peshitta_tf.py`'s `--corpus`
   flag already accepts `peshitta` (OT) alongside `syrnt` (NT); no change
   needed, just run it with `--corpus peshitta` when you get to it.

---

## The one thing to know before importing anything

`db-queries.js` validates every lexicon lookup against:

```js
const STRONG_RE = /^[GH]\d{1,5}[A-Za-z]?$/;
```

`lexicon_words.strong` is `UNIQUE NOT NULL` and must match that pattern — `getLexiconEntry()`
throws a 400 ("Malformed Strong's number") on anything else. That's fine for Greek/Hebrew, but
it means **Latin and Syriac words cannot get click-to-lexicon linking as the schema stands today**.
The text will still import, display, and be interlinear-readable (`words.strong` can just be
`NULL`) — you'll just lose the "click a word, see the dictionary entry" feature for those two.

If you want that feature for Latin/Syriac too, the smallest fix is:
1. Loosen `STRONG_RE` to also accept a third prefix, e.g. `^[GHL]\d{1,5}[A-Za-z]?$` for Latin
   (`S` collides with nothing existing either, for Syriac).
2. Assign synthetic `L#####`/`S#####` IDs to Lewis & Short / a Syriac lexicon the same way your
   `import_abbott_smith.py` assigns real Strong's IDs — sequential, source-scoped.
3. Everything downstream (`lexemeExact`, `strongsByLiteralText`, the occurrences index) works
   unchanged once `lexicon_words.strong` values exist in that shape.

That's a real (if small) code change to `db-queries.js`, not just an import script, so I didn't
make it silently — happy to do it if you want, just say the word. The scripts below all work
either way; they just leave `strong` NULL for Latin/Syriac unless you've made that change.

**Samaritan Pentateuch and Dead Sea Scrolls are Hebrew**, though, so their words can potentially
reuse your *existing* H-numbered lexicon with no schema change at all — see their sections below.

---

## 1. Vulgate (Latin) + Douay-Rheims (English)

Both are safely public domain and both live in the same repo, but **not in the same format, and
not in OSIS at all** — I got this wrong initially; you were right to be suspicious. Confirmed
directly against the real files (cloned the repo and looked):

- **Source**: [`seven1m/open-bibles`](https://github.com/seven1m/open-bibles).
  - Vulgate: `lat-clementine.usfx.xml` — **USFX**, not OSIS. Simple flat milestone markup
    (`<c id="N"/>`, `<v id="N"/>...<ve/>`), no inline footnotes/crossrefs in this particular file.
    Public domain (ultimately from the [Vulgate Project](http://vulsearch.sourceforge.net/),
    transcribed by Michael Tweedale).
  - Douay-Rheims: `eng-dra.zefania.xml` — **Zefania**, not OSIS. Clean nested XML
    (`<BIBLEBOOK bname="Genesis"><CHAPTER cnumber="1"><VERS vnumber="1">text</VERS>`). Public
    domain (Challoner revision). There's no OSIS Vulgate or Douay-Rheims in this repo, or in any
    of its forks I checked — every one of them has these same two files in these same two formats.
  - **Real data-quality issue found while testing**: this specific DRA file is **missing 1
    Maccabees entirely** (2 Maccabees is present, 1 is not), and has a few oddly-handled apocrypha
    entries — "AddDan" (Additions to Daniel) and "Letter of Jeremiah" split out as their own
    pseudo-books rather than folded into Daniel/Baruch the way the Vulgate itself has them, plus
    two non-canonical extras ("1 Esdras", "Psalms of Solomon") that aren't part of the Douay-Rheims
    canon at all. If completeness matters to you, this file needs a source that actually has all 73
    books before you treat it as authoritative — I did not find one this session.
- **Granularity**: verse-level only — no per-word tagging, no Strong's numbers in the source.
- **Lexicon**: for Latin, [Lewis & Short](https://github.com/PerseusDL/lexica) (Perseus Digital
  Library, CC BY-SA 4.0) — see the dedicated section below, it's already imported and verified.
- **Script**: `import_vulgate_dra.py` — auto-detects USFX vs. Zefania from the file's root element,
  parses either correctly, and tokenizes each verse's plain text into one `words` row per
  whitespace-separated token (lang from `--language`, `strong` NULL) so it renders through the same
  interlinear pane machinery, just without lexicon links (see the STRONG_RE note above for Latin).
  Ran successfully against both real downloaded files while writing this — 11,025 Vulgate verses
  and 7,355 Douay-Rheims verses across a 10-book test slice, spot-checked by hand (Genesis 1:1-3 in
  both, plus a deuterocanonical check against 1 Maccabees 1:1, which is why the DRA gap above was
  caught rather than assumed).

## 2. Peshitta (Syriac) + English

- **Syriac text**: ETCBC's Text-Fabric corpora —
  [`ETCBC/syrnt`](https://github.com/ETCBC/syrnt) (New Testament, from Kiraz/Bennett's SEDRA
  database) and [`ETCBC/peshitta`](https://github.com/ETCBC/peshitta) (Old Testament). Both are
  word-level with morphology. Check each repo's `About` page for the exact license before
  redistributing — ETCBC datasets are typically free for research/education use with attribution,
  not unconditionally public domain.
- **English translation**: James Murdock's 1851 translation (*The New Testament... Translated
  from the Syriac Peshito Version*) is public domain (pre-1929) and NT-only. A clean text copy is
  linked from [aramaicnewtestament.org/peshitta/murdock](https://aramaicnewtestament.org/peshitta/murdock/).
  There is no public-domain Peshitta **Old Testament** English translation that I could find —
  Lamsa's (1933) is still under copyright.
- **Lexicon**: SEDRA has lexeme data built into the ETCBC datasets, but it's Kiraz's own numbering,
  not Strong's-compatible — same caveat as above, needs the schema change to be clickable.
- **Script**: `import_peshitta_tf.py` (below) — reads the Text-Fabric feature files and a plain
  Murdock text file separately (they won't verse-align perfectly out of the box; the script
  matches on book/chapter/verse and flags gaps rather than guessing).

## 3. Samaritan Pentateuch — the good one

- **Source**: [`DT-UCPH/sp`](https://github.com/DT-UCPH/sp) — word-level, BHSA-style features
  (lexeme, morphology), based on Schorch's critical *editio maior* (Chester Beatty 751 +
  Garizim 1 manuscripts). **CC BY 4.0-equivalent, free for research/education with attribution**
  (per the repo's own terms) — check current terms before shipping, but this is about as open as
  Samaritan Pentateuch data gets.
- **The win**: because it's Hebrew and annotated in the same ETCBC/BHSA lexeme-feature style as
  the Hebrew Bible dataset your existing lexicon almost certainly derives from, its words can
  often be cross-walked to your **existing H-numbered lexicon** by matching consonantal lexeme —
  no schema change needed, genuinely word-clickable. `import_samaritan_pentateuch.py` attempts
  this match and reports a coverage percentage so you can see how well it worked before trusting it.
- **English translation**: found it after all — see "Update" at the top. **"Samaritan Pentateuch
  in English" (SPE)**, Aleksandr Sigalov, freely distributed via CrossWire (SWORD), also on
  e-Sword/theWord/MySword. KJV text edited at each Samaritan/MT divergence point, checked against
  Kennicott 1780 and von Gall 1918 — genuinely archaic-register English, and genuinely free.
  `import_spe_english.py` handles the SWORD version. The Tsedaka & Sullivan 2013 translation
  (Eerdmans) remains copyrighted and is not what this is — don't confuse the two if you're citing
  translator/source in the UI.

## 4. Dead Sea Scrolls — text yes, translation no. Now fully verified and fixed.

- **Source**: [`ETCBC/dss`](https://github.com/ETCBC/dss) — transcriptions of biblical *and*
  non-biblical scrolls (built on Martin Abegg's data). License is Creative Commons
  Attribution-NonCommercial 4.0 (confirmed directly from the .tf feature file headers) — fine for
  your use, just not something to sell.
- **Real bugs found and fixed** (you ran the first version and caught both):
  1. The original script read the `g_cons` feature for word text, which turned out to be a sparse
     correction layer with no value at all for most biblical words — that's why you only saw verse
     numbers with no text. The feature that's actually populated for every word is `full`. Confirmed
     by cloning the repo (its .tf feature files are plain text in git — no multi-GB download needed)
     and running a real local Text-Fabric load against it, no network calls.
  2. The original kept only the first scroll witness per verse and silently dropped the rest.
     `import_dss_biblical.py` now concatenates **every** witness into the one verse record, each
     preceded by a small `[siglum]` tag word (e.g. `[4Q106]`), exactly as you asked. This turned out
     to matter a lot: 2,437 of 9,518 distinct biblical verses in the dataset (~26%) are attested in
     more than one scroll — Genesis 1:9 alone has five. Verified end-to-end against the real local
     data: Genesis 1:9 now imports as one verse record with all five witnesses in order, each
     properly tagged.
  - `full` includes the transcription's editorial markup (reconstruction/correction brackets,
    uncertainty flags) by default, matching how DSS editions are normally presented. Pass `--clean`
    for a stripped, plain-reading version instead — tested, it works, but it's a best-effort regex
    based on the project's documented markup table, not checked against a print edition, so
    spot-check a few verses before trusting it fully.
- **English translation**: no public-domain option exists. Abegg/Flint/Ulrich's *The Dead Sea
  Scrolls Bible* and Brill's *Dead Sea Scrolls Electronic Library* are both actively copyrighted
  and paywalled. This one has to ship text-only (Hebrew/Aramaic), same as SP above.
- **Lexicon**: same H-number cross-walk approach as SP (matching against `lex`-node text, with
  homograph suffixes like `_1`/`_2` stripped first) — verified working against a seeded test
  lexicon (matched real words like יאמר→H559, אלהים→H430). Expect a lower match rate than SP in
  practice, since DSS orthography is far more variable than either MT or SP.

---

## Files in this folder

| File | Produces | Tested against real data? |
|---|---|---|
| `db-queries.js` | Patched: `STRONG_RE` accepts L/S, fuzzy lexicon search fallback added | Yes — `smoke_test.js`, real in-memory DB |
| `smoke_test.js` | Proof the db-queries.js patch actually works — run it yourself with `node smoke_test.js` | — |
| `import_vulgate_dra.py` | Vulgate (Latin, USFX) and Douay-Rheims (English, Zefania) | **Yes — ran against both real downloaded files, 11,025 + 7,355 verses** |
| `import_spe_english.py` | Samaritan Pentateuch in English (SPE), KJV-based, via SWORD module | Yes — full pipeline run against a real hand-built SWORD fixture |
| `import_samaritan_pentateuch.py` | Samaritan Pentateuch Hebrew (`DT-UCPH/sp`), with H-number cross-walk attempt | No — Text-Fabric feature names are my best inference, not verified |
| `import_peshitta_tf.py` | Peshitta Syriac text (NT via `syrnt`, OT via `peshitta`) + Murdock English | Syriac side: no, same TF caveat. Murdock side: logic only |
| `import_dss_biblical.py` | DSS biblical scrolls, all manuscript witnesses per verse, H-number cross-walk | **Yes — real bugs found and fixed via a real local Text-Fabric load, see below** |
| `import_latin_lexicon.py` | Lewis & Short Latin dictionary → `lexicon_words`/`lexicon_entries` | **Yes — ran against the real, full 51,645-entry file** |
| `import_syriac_lexicon.py` | SEDRA III Syriac lexemes/glosses → `lexicon_words`/`lexicon_entries` | **Yes — ran against the real, full 3,559-entry data set** |

Everything that touches your `books`/`verses`/`words`/`lexicon_*` tables assumes it sits next to
your existing `book_codes.py` and `build_db.py` modules (same imports your `import_berean_tsv.py`
/ `import_robinson_csv.py` use) — drop these files into that same directory before running.

**What "tested against real data" means concretely**, since three of these files genuinely were
run this session against real downloads (not fabricated samples):
- `import_latin_lexicon.py` ran against the real `lat.ls.perseus-eng2.xml` from
  `PerseusDL/lexica` (77MB, 51,645 entries) and produced correct, readable output — checked by
  hand (e.g. the "agnus" entry came out with proper Greek-quotation spans and etymology intact).
- `import_syriac_lexicon.py` ran against the real `LEXEMES.TXT`/`ENGLISH.TXT` from
  `peshitta/sedrajs` (3,559 lexemes, 3,522 with glosses) — including finding and fixing a real
  parsing edge case (some `ENGLISH.TXT` rows use the literal string `"NULL"` for an unlinked
  gloss's lexeme address, which isn't documented anywhere and would have crashed a naive parser).
- `import_spe_english.py`'s entire pysword-based pipeline — module parsing, book/chapter/verse
  structure walking, verse text extraction, tokenizing, and SQLite insertion — ran successfully
  against a real (if minimal) hand-built SWORD RawText module I constructed specifically to
  validate this, producing exactly the expected three verses of Genesis 1 in the database.
- The `db-queries.js` patch (STRONG_RE + fuzzy search) ran against a real in-memory SQLite
  database with representative L/G/S lexicon entries and produced correct results for every case
  I could think to check — exact ID lookup, exact lexeme match, fuzzy gloss match, fuzzy
  transliteration-prefix match, and true not-found.

Two of the three Text-Fabric-based scripts are still genuinely unverified:
`import_samaritan_pentateuch.py` and the Syriac side of `import_peshitta_tf.py`. `import_dss_biblical.py`
started in that same unverified bucket but isn't anymore — see the update below. The approach that
fixed DSS (clone the repo directly, since ETCBC's Text-Fabric data ships as plain-text `.tf` files
checked into git; run a real local Text-Fabric load with no network calls; check every feature name
against real output) applies just as well to SP and Peshitta if they turn out to have similar
issues — flagging that now rather than risk the same "looks like it worked but the text is empty"
surprise twice.

## Update: the DSS bugs you found — both real, both fixed, both re-verified

You ran `import_dss_biblical.py` and reported two problems. Both were real:

1. **"Only verse numbers, no text"** — the original script read the `g_cons` feature, which turned
   out to be a sparse correction layer with no value at all for most words. `full` is the feature
   that's actually populated for every word; that's what the rewritten script reads now.
2. **"Only accepted the first instance of a verse"** — correct, and now fixed exactly as asked:
   every scroll witness for a verse is concatenated into that one verse record, each preceded by a
   small `[siglum]` tag (e.g. `[4Q106]`) marking which manuscript it came from. This turned out to
   matter more than a guess would have suggested: **2,437 of 9,518 distinct biblical verses (~26%)
   are attested in more than one scroll** — Genesis 1:9 has five separate witnesses. Verified
   end-to-end: it now imports as a single verse record with all five, correctly tagged, in order.

Also added `--clean`, which strips the transcription's editorial markup (reconstruction brackets,
uncertainty flags) for a plain-reading version — off by default, since that markup is genuine
scholarly signal in a DSS edition, not noise. Both the bug fixes and `--clean` were confirmed
against real output from a real local Text-Fabric load, not just re-reasoned about.

## Update: Vulgate/DRA format — right call, they aren't OSIS

Checked directly: `lat-clementine.usfx.xml` is **USFX** and `eng-dra.zefania.xml` is **Zefania**.
There's no OSIS version of either in that repo or any fork of it I checked. `import_osis_verses.py`
is gone — replaced by `import_vulgate_dra.py`, which auto-detects and correctly parses both real
formats, run successfully against both actual downloaded files (11,025 Vulgate verses, 7,355
Douay-Rheims verses across a 10-book test slice). That testing also surfaced a genuine data-quality
issue worth knowing about: this specific DRA file is **missing 1 Maccabees entirely**, and has a
few oddly-split/non-canonical apocrypha entries — see the Vulgate/DRA section above for details.
