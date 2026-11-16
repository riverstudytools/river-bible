# Textus Receptus + Strong's Greek Dictionary importers

Both scripts follow the same shape as your existing `import_abbott_smith.py`
and `import_robinson_csv.py`: they use your shared `book_codes.py` /
`build_db.py` (and `build_search_index.py` for the TR importer) modules, and
upsert into the same `translations`/`books`/`verses`/`words` and
`lexicon_words`/`lexicon_sources`/`lexicon_entries` tables.

I fetched and test-parsed the actual source data (whole NT + whole
dictionary) against stand-in copies of your helper modules before handing
these over, so the parsing logic itself is verified — just point them at
your real `bible.sqlite` and real `book_codes.py`.

## 1. Textus Receptus — `import_textus_receptus.py`

**Source:** [byztxt/greektext-textus-receptus](https://github.com/byztxt/greektext-textus-receptus)
— Dr. Maurice A. Robinson's parsed edition of the Textus Receptus, with
Strong's numbers and morphological parsing tags. **Public domain.**

```
git clone https://github.com/byztxt/greektext-textus-receptus.git
python3 import_textus_receptus.py greektext-textus-receptus/parsed \
    --code TR --name "Textus Receptus (Robinson parsed text)" \
    --db bible.sqlite --replace
```

Notes on the source format (handled by the importer):
- Words are unaccented, in a simple ASCII letter scheme (a=α, b=β, ... v=ς,
  w=ω — no breathings/accents). The importer decodes these to real Unicode
  Greek.
- A small amount of critical-apparatus markup appears inline (alternate
  spellings in `| a | b |` form, double parses for one word, bracketed
  disputed spans, and `(chapter:verse)` alternate-versification notes).
  I validated the parser against the entire NT (7,957 verses): with this
  handling, only ~0.3% of verses have any leftover unhandled markup, and
  those are essentially all inert versification cross-reference notes, not
  lost text. Alternate parses are preserved as a `footnote` on the word.
- `translit` is left `NULL` — the source has no accented/scholarly
  transliteration to draw from. If you want SBL-style translit generated,
  that's a separate follow-up.
- Book codes in the filenames (`MT`, `1CO`, `RE`, etc.) are resolved via
  your `book_codes.resolve_canonical_name()`, same as the Robinson-Pierpont
  importer. Any codes it can't resolve are reported at the end.

## 2. Strong's Greek Dictionary — `import_strongs_greek.py`

**Source:** [openscriptures/strongs](https://github.com/openscriptures/strongs)
— `greek/StrongsGreekDictionaryXML_1.4/strongsgreek.xml`. James Strong's
1890 dictionary text is public domain; the XML e-text itself is
**CC BY-SA 4.0** (Open Scriptures / Ulrik Petersen).

```
git clone https://github.com/openscriptures/strongs.git
python3 import_strongs_greek.py \
    strongs/greek/StrongsGreekDictionaryXML_1.4/strongsgreek.xml \
    bible.sqlite
```

- Imports all 5,624 entries: lexeme, SBL-style transliteration,
  pronunciation, a derived short gloss, and a full HTML entry (derivation,
  definition, KJV usage note, cross-references as `<span data-strong="Gxxx">`
  for easy linking in your lexicon sidebar).
- Uses source code `STRONG-GR` in `lexicon_sources` — I didn't reuse a
  code from your earlier Strong's import (noted in my project memory as
  already done at some point) since I don't know what code that used;
  if it collides or duplicates something you already have, just point me
  at the existing table state and I'll reconcile them.
- `short_gloss` is a best-effort heuristic (first usable KJV rendering,
  skipping Strong's "X ..." filler-word markers) — good enough for a list
  view, but the full `entry_html` is what has the real definition.
