#!/usr/bin/env python3
"""
import_payne_smith_syriac.py — importer for J. Payne Smith's "A
Compendious Syriac Dictionary" (Oxford, 1903), into the same
lexicon_words / lexicon_sources / lexicon_entries tables used by
import_abbott_smith.py.

REVISED after pulling the actual OCR text (archive.org's djvu.txt for
identifier ACompendiousSyriacDictionary) and checking it against real
entries rather than guessing. Two things that changed from a first pass:

  1. There is NO separator (no dash, no colon) between the Syriac
     headword and the English material that follows it — entries are
     just "headword" immediately followed by grammar codes (pl., fut.,
     rt., emph., etc.) and then the gloss, all run together in one
     paragraph. The only reliable entry boundary is the paragraph break
     itself (a blank line in the OCR text). So this version splits on
     blank lines and treats each paragraph as one entry, rather than
     trying to regex out headword-vs-definition within a line.

  2. OCR reading order breaks down at the start of each of the 22
     Syriac-letter sections, where a large drop-cap letter in the
     margin gets read out of sequence relative to the entry text next
     to it (e.g. the very first entry, "Alep or Olaph", has its
     transliteration and the drop-cap Alep in a scrambled order). This
     happens at most ~22 times in the whole dictionary and is easy to
     spot and hand-fix; it is NOT worth writing regex gymnastics for.

  3. OCR quality on the Syriac glyphs themselves is uneven — expect a
     meaningful error rate in the headword text (this is 120-year-old
     print with a lot of small vowel-pointing marks, and Tesseract
     wasn't tuned for it). Treat the `lexeme` column as "best effort,
     needs a proofing pass," not ground truth.

Source: https://archive.org/details/ACompendiousSyriacDictionary
Internet Archive marks this item's usage rights as Public Domain.
Grab the plain-text OCR layer:

    https://archive.org/download/ACompendiousSyriacDictionary/ACompendiousSyriacDictionary_djvu.txt

(there's also an OCR SEARCH TEXT file, hocr_searchtext.txt.gz, which is
much smaller and may have cleaner line breaks — worth comparing if the
full djvu.txt gives you trouble.)

The same approach and this same script should also work — unverified,
but structurally these are the same kind of 1900s-era printed lexicon
scan — for William Jennings' 1926 "Lexicon to the Syriac New
Testament" (also public domain, smaller — NT vocabulary only), whose
OCR text you can get from either:
    https://archive.org/details/lexicontosyriacn0000will  (djvu.txt)
    http://dukhrana.com/lexicon/Jennings/Jennings_-_Lexicon_to_the_Syriac_New_Testament.pdf
      (Dukhrana's own scan — a plain download, not gated behind their
      SEDRA-based "Analytical Lexicon" tool at dukhrana.com/lexicon/,
      so this doesn't carry the licensing complication SEDRA did.
      It's just a PDF, so you'd need a text-extraction pass — see the
      pdf skill — rather than feeding it directly to this script.)

Usage:
    python3 import_payne_smith_syriac.py ACompendiousSyriacDictionary_djvu.txt bible.sqlite
    python3 import_payne_smith_syriac.py --source-code PS-J --source-name "J. Payne Smith, A Compendious Syriac Dictionary (1903)" payne.txt bible.sqlite
"""

import argparse
import re
import sqlite3
import sys

DEFAULT_SOURCE = ("PS", "J. Payne Smith, A Compendious Syriac Dictionary (1903)", "Public domain")

SYRIAC_RUN_RE = re.compile(r'[\u0700-\u074F][\u0700-\u074F\u0300-\u036F\'\u2019]*')

# Lines that are clearly OCR noise from running headers/page numbers
# rather than dictionary content (e.g. "ABBREVIATIONS AND EXPLANATIONS",
# stray page-number-only lines, the two title-page repeats at the start
# of the file). Extend this list as you spot more junk in your copy.
JUNK_LINE_RE = re.compile(
    r'^\s*(?:\d+|ABBREVIATIONS AND EXPLANATIONS|PREFACE|ARRANGEMENT|[A-Z ]{10,})\s*$'
)


def ensure_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lexicon_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strong TEXT UNIQUE NOT NULL,
            lang TEXT NOT NULL,
            lexeme TEXT NOT NULL,
            translit TEXT,
            pronunciation TEXT,
            short_gloss TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_lexicon_strong ON lexicon_words(strong);

        CREATE TABLE IF NOT EXISTS lexicon_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            copyright TEXT
        );

        CREATE TABLE IF NOT EXISTS lexicon_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strong_id INTEGER NOT NULL REFERENCES lexicon_words(id) ON DELETE CASCADE,
            source_id INTEGER NOT NULL REFERENCES lexicon_sources(id) ON DELETE CASCADE,
            entry_html TEXT NOT NULL,
            UNIQUE(strong_id, source_id)
        );
    """)


def ensure_source(conn, code, name, copyright_):
    conn.execute(
        """INSERT INTO lexicon_sources (code, name, copyright) VALUES (?, ?, ?)
           ON CONFLICT(code) DO UPDATE SET name=excluded.name, copyright=excluded.copyright""",
        (code, name, copyright_),
    )
    return conn.execute("SELECT id FROM lexicon_sources WHERE code=?", (code,)).fetchone()[0]


def clean_paragraph(text):
    # OCR line-wrap hyphens and stray Adobe-style right-to-left marks
    text = text.replace('\u200f', '').replace('\u200e', '')
    text = re.sub(r'-\n', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def split_entries(raw_text):
    """Splits the OCR text into paragraph-sized entry blocks (blank-line
    delimited), skipping obvious front-matter/header noise."""
    # Normalize blank-line runs, then split.
    blocks = re.split(r'\n\s*\n+', raw_text)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        if JUNK_LINE_RE.match(block):
            continue
        yield block


def parse_entry(block):
    """Returns (headword, full_cleaned_text) or None if this block doesn't
    look like a dictionary entry (e.g. leftover front-matter)."""
    m = SYRIAC_RUN_RE.search(block)
    if not m:
        return None
    headword = m.group(0)
    if len(headword) < 1:
        return None
    cleaned = clean_paragraph(block)
    return headword, cleaned


def make_lexeme_id(seen_counter):
    """
    Payne Smith has no Strong's-style numbering, so this assigns a
    synthetic S-prefixed ID in entry order (matching the S-prefix scheme
    you're already using for SEDRA lemma IDs). If you later want these
    IDs to line up with a real Syriac Strong's numbering elsewhere in
    your data, swap this for a lookup against that table — entry order
    won't match any existing numbering on its own.
    """
    seen_counter[0] += 1
    return f'S{seen_counter[0]:05d}'


def import_file(txt_path, db_path, source_code, source_name, source_copyright):
    with open(txt_path, encoding='utf-8', errors='replace') as f:
        raw_text = f.read()

    conn = sqlite3.connect(db_path)
    conn.execute('PRAGMA foreign_keys = ON')

    seen_counter = [0]
    imported = skipped = 0

    try:
        ensure_schema(conn)
        source_id = ensure_source(conn, source_code, source_name, source_copyright)

        for block in split_entries(raw_text):
            parsed = parse_entry(block)
            if parsed is None:
                skipped += 1
                continue
            headword, full_text = parsed
            lexeme_id = make_lexeme_id(seen_counter)

            conn.execute(
                """INSERT INTO lexicon_words (strong, lang, lexeme, short_gloss)
                   VALUES (?, 'Syriac', ?, ?)
                   ON CONFLICT(strong) DO UPDATE SET lexeme=excluded.lexeme, short_gloss=excluded.short_gloss""",
                (lexeme_id, headword, full_text[:150]),
            )
            word_id = conn.execute("SELECT id FROM lexicon_words WHERE strong=?", (lexeme_id,)).fetchone()[0]
            conn.execute(
                """INSERT INTO lexicon_entries (strong_id, source_id, entry_html)
                   VALUES (?, ?, ?)
                   ON CONFLICT(strong_id, source_id) DO UPDATE SET entry_html=excluded.entry_html""",
                (word_id, source_id, f'<p>{full_text}</p>'),
            )
            imported += 1

        conn.commit()
        print(f'Imported {imported} entries from {txt_path} ({skipped} blocks skipped as non-entries).')
        print('*** Spot-check a sample against the real dictionary before trusting them wholesale —')
        print('*** OCR errors in the Syriac headwords and ~22 scrambled section-letter entries are expected.')

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description="Importer for Payne Smith's Compendious Syriac Dictionary (or similarly-structured public-domain Syriac lexicon) OCR text")
    ap.add_argument('txt_path', help='Path to the _djvu.txt OCR text file')
    ap.add_argument('db_path', nargs='?', default='bible.sqlite')
    ap.add_argument('--source-code', default=DEFAULT_SOURCE[0])
    ap.add_argument('--source-name', default=DEFAULT_SOURCE[1])
    ap.add_argument('--source-copyright', default=DEFAULT_SOURCE[2])
    args = ap.parse_args()
    import_file(args.txt_path, args.db_path, args.source_code, args.source_name, args.source_copyright)
