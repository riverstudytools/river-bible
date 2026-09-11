# MT/LXX verse-alignment for River's compare view

## What this adds
A new "⇄ Align verse numbers" toggle in the compare bar, shown only when
the columns you're comparing mix an MT-tradition translation (numbered
English/KJV-style — ESV, NASB, KJV, etc.) with an LXX-tradition
translation (numbered Greek/Septuagint-style — Brenton, NETS, etc.).
Turning it on makes River look up the real matching verse instead of
assuming the numbers line up, and shows a small superscript badge (e.g.
`3:3`) on any cell whose text was pulled from a different chapter:verse
than the row it's displayed in.

Direction isn't a separate setting: whichever translation occupies the
*first* compare column is the anchor everyone else aligns to. Put your
MT-tradition text first to force LXX to conform to MT, or vice versa
(you already have a control for this — the compare chip's dropdown lets
you reorder/swap which translation is in which column).

## Data source & license
[STEPBible's TVTMS](https://github.com/STEPBible/STEPBible-Data)
("Translators Versification Traditions with Methodology for
Standardisation"), © Tyndale House Cambridge / STEPBible.org,
**CC BY 4.0**. Keep that attribution wherever this data ships.

`extract_mt_lxx.py` reduces TVTMS's ~30,000-line reversification-engine
format down to the one thing River needs: for every Old Testament verse,
does the English-standard (StandardRef) chapter:verse disagree with
TVTMS's Greek= annotation? Output is `mt_lxx_diffs.tsv`
(book, std_chapter, std_verse, lxx_ref) — 4,382 rows.

Deliberately **not** using TVTMS's Hebrew= annotation: that records how
*printed Hebrew Bibles* number things, which sometimes differs from
English-standard numbering even with no real MT/LXX issue (Gen.31:55
English = Hebrew print's 32:1 — same text, different chapter break
convention). River's MT-tradition translations are numbered the English
way, so English-standard *is* "the MT side" here.

`mt_lxx_conflicts.tsv` (650 rows) lists StandardRefs where TVTMS itself
records disagreeing Greek= annotations across different manuscript-
convention branches (mostly Exodus 25–28's tabernacle-furnishing
material, and Jeremiah/1 Kings' large-scale reordering) — the extractor
resolves these by preferring the plain "Eng-KJV" branch, but it's worth
a manual skim before treating them as gospel.

## Running it
```
python3 extract_mt_lxx.py                          # reads TVTMS.txt, writes mt_lxx_diffs.tsv
python3 build_versification_index.py bible.sqlite mt_lxx_diffs.tsv
```
`build_versification_index.py`:
1. Adds `translations.versification` if it's not already there.
2. Creates `versification_diffs` (book, std_chapter, std_verse, lxx_ref).
3. Loads every diff row whose book exists in *this* bible.sqlite.
4. Walks every translation and tags `versification` as `'MT'` or
   `'LXX'` — auto-tagging known LXX codes (LXE, NETS, LES, ABP), asking
   interactively for anything else, and never overwriting a value you
   already set by hand.

Everything here degrades gracefully: a bible.sqlite built without ever
running this script just doesn't get the versification_diffs table, and
`db-queries.js`'s `versificationReady` flag turns the whole feature off
(same pattern as `searchReady`/`occurrencesReady`/`lexiconReady`).

## Known gaps (by design, not bugs)
- Whole-book differences (LXX-only books like Tobit, Judith, the
  Maccabees, or LXX's extra Psalm 151 and additions to Daniel/Esther)
  aren't in scope — this is purely about verse-*numbering* within books
  both traditions share, not canon differences.
- A handful of passages don't reduce to a clean one-cell match at all
  (e.g. Exo.28:29 splitting into three separate Greek verses). Those are
  dropped during extraction rather than guessed at; the compare view
  shows "not available" for them when alignment is on.
