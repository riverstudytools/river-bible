#!/usr/bin/env python3
"""
import_vulgate_dra.py — Importer for the Clementine Vulgate and the
Douay-Rheims from seven1m/open-bibles, in their REAL formats.

You were right to be suspicious — neither file is OSIS. Checked directly
against the real repo:
    lat-clementine.usfx.xml  -> USFX  (Vulgate)
    eng-dra.zefania.xml      -> Zefania  (Douay-Rheims)
There's no OSIS Vulgate or Douay-Rheims in that repo at all; every fork of
it I checked has the same two formats for these two texts. This replaces
the old import_osis_verses.py, which would not have parsed either file
correctly — it was written against a format assumption that turned out to
be wrong, sorry about that.

Both formats verified against the real downloaded files while writing
this script (not guessed):
  - USFX: flat milestone markup inside <book id="GEN">...</book> —
    self-closing <c id="N"/> for chapter starts, <v id="N"/> ... <ve/> for
    verse boundaries, plain text with NO inline footnote/crossref markup
    in this particular file (checked: the only tags present anywhere in
    lat-clementine.usfx.xml are book/h/c/v/ve).
  - Zefania: conventional nested XML — <BIBLEBOOK bname="Genesis"><CHAPTER
    cnumber="1"><VERS vnumber="1">text</VERS>...</CHAPTER></BIBLEBOOK>,
    also with no inline markup inside <VERS> in this file.

Both are verse-level only, same as before — no per-word Strong's tagging
in the source, so each verse's text is split on whitespace into one
`words` row per token (lang from --language, strong left NULL). Doesn't
get lexicon click-through (see README's note on STRONG_RE for Latin) but
renders fine in the interlinear pane and is fully searchable.

Usage:
    python3 import_vulgate_dra.py lat-clementine.usfx.xml \
        --code VUL --name "Clementine Vulgate" --language Latin --replace

    python3 import_vulgate_dra.py eng-dra.zefania.xml \
        --code DRA --name "Douay-Rheims" --language English --replace
"""

import argparse
import html
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET

import book_codes
import build_db as db_builder

# USFX uses standard USFM/Paratext 3-letter book codes, not full names.
# This is the complete, real set found in lat-clementine.usfx.xml (73
# books — the Vulgate includes the deuterocanonicals) mapped to full
# English names, since a full name is far more likely to match whatever
# your book_codes.resolve_canonical_name() actually expects than a terse
# 3-letter code is.
USFX_BOOK_MAP = {
    'GEN': 'Genesis', 'EXO': 'Exodus', 'LEV': 'Leviticus', 'NUM': 'Numbers',
    'DEU': 'Deuteronomy', 'JOS': 'Joshua', 'JDG': 'Judges', 'RUT': 'Ruth',
    '1SA': '1 Samuel', '2SA': '2 Samuel', '1KI': '1 Kings', '2KI': '2 Kings',
    '1CH': '1 Chronicles', '2CH': '2 Chronicles', 'EZR': 'Ezra', 'NEH': 'Nehemiah',
    'TOB': 'Tobit', 'JDT': 'Judith', 'EST': 'Esther', 'JOB': 'Job',
    'PSA': 'Psalms', 'PRO': 'Proverbs', 'ECC': 'Ecclesiastes', 'SNG': 'Song of Solomon',
    'WIS': 'Wisdom', 'SIR': 'Sirach', 'ISA': 'Isaiah', 'JER': 'Jeremiah',
    'LAM': 'Lamentations', 'BAR': 'Baruch', 'EZK': 'Ezekiel', 'DAN': 'Daniel',
    'HOS': 'Hosea', 'JOL': 'Joel', 'AMO': 'Amos', 'OBA': 'Obadiah',
    'JON': 'Jonah', 'MIC': 'Micah', 'NAM': 'Nahum', 'HAB': 'Habakkuk',
    'ZEP': 'Zephaniah', 'HAG': 'Haggai', 'ZEC': 'Zechariah', 'MAL': 'Malachi',
    '1MA': '1 Maccabees', '2MA': '2 Maccabees',
    'MAT': 'Matthew', 'MRK': 'Mark', 'LUK': 'Luke', 'JHN': 'John', 'ACT': 'Acts',
    'ROM': 'Romans', '1CO': '1 Corinthians', '2CO': '2 Corinthians', 'GAL': 'Galatians',
    'EPH': 'Ephesians', 'PHP': 'Philippians', 'COL': 'Colossians',
    '1TH': '1 Thessalonians', '2TH': '2 Thessalonians', '1TI': '1 Timothy',
    '2TI': '2 Timothy', 'TIT': 'Titus', 'PHM': 'Philemon', 'HEB': 'Hebrews',
    'JAS': 'James', '1PE': '1 Peter', '2PE': '2 Peter', '1JN': '1 John',
    '2JN': '2 John', '3JN': '3 John', 'JUD': 'Jude', 'REV': 'Revelation',
}


def strip_tags(text):
    return html.unescape(re.sub(r'<[^>]+>', '', text)).strip()


USFX_BOOK_RE = re.compile(r'<book id="([A-Z0-9]+)">(.*?)</book>', re.DOTALL)
USFX_CONTROL_RE = re.compile(r'<c id="(\d+)"\s*/>|<v id="([^"]+)"\s*/>|<ve\s*/>')


def load_usfx_verses(path):
    """Yields (book_full_name, chapter, verse, text). Cuts each verse's
    text at whatever control token comes next (<ve/>, the next <v>, or the
    next <c>) rather than requiring a well-formed <ve/> to always be
    present — more robust to any stray malformed markup than assuming
    every verse is perfectly closed."""
    with open(path, encoding='utf-8') as f:
        data = f.read()

    for book_id, inner in USFX_BOOK_RE.findall(data):
        book_full = USFX_BOOK_MAP.get(book_id)
        if not book_full:
            continue  # book id not in our map -> let it fall through, same safety net as elsewhere

        chapter = None
        matches = list(USFX_CONTROL_RE.finditer(inner))
        for i, m in enumerate(matches):
            if m.group(1) is not None:
                chapter = int(m.group(1))
                continue
            if m.group(2) is not None:
                if chapter is None:
                    continue
                vm = re.match(r'\d+', m.group(2))
                if not vm:
                    continue
                verse = int(vm.group(0))
                text_start = m.end()
                text_end = matches[i + 1].start() if i + 1 < len(matches) else len(inner)
                text = strip_tags(inner[text_start:text_end])
                if text:
                    yield book_full, chapter, verse, text


def load_zefania_verses(path):
    """Straightforward nested-XML walk — Zefania needs no milestone-style
    parsing gymnastics. bname is already a full English name in this file
    ("1 Samuel", "Song of Solomon", etc.), passed through as-is."""
    tree = ET.parse(path)
    root = tree.getroot()
    for book_elem in root.iter('BIBLEBOOK'):
        book_full = book_elem.attrib.get('bname')
        if not book_full:
            continue
        for chapter_elem in book_elem.iter('CHAPTER'):
            chapter = chapter_elem.attrib.get('cnumber')
            if not chapter:
                continue
            chapter = int(chapter)
            for verse_elem in chapter_elem.iter('VERS'):
                verse = verse_elem.attrib.get('vnumber')
                if not verse:
                    continue
                text = ''.join(verse_elem.itertext()).strip()
                if text:
                    yield book_full, chapter, int(verse), text


def detect_format(path):
    with open(path, encoding='utf-8') as f:
        head = f.read(2000)
    if '<usfx' in head:
        return 'usfx'
    if '<XMLBIBLE' in head:
        return 'zefania'
    sys.exit(f'Could not detect format of {path} — expected a <usfx> or <XMLBIBLE> root element. '
              'If this is actually OSIS or something else, this script doesn\'t handle it.')


def import_translation(con, translation_id, book_id_by_name, verses, language):
    verses_inserted = words_inserted = 0

    for book_name_raw, chapter, verse, text in verses:
        canonical_book = book_codes.resolve_canonical_name(book_name_raw)
        if not canonical_book:
            continue
        book_id = book_id_by_name.get(canonical_book)
        if book_id is None:
            continue

        existing = con.execute(
            'SELECT id FROM verses WHERE translation_id=? AND book_id=? AND chapter=? AND verse=?',
            (translation_id, book_id, chapter, verse)
        ).fetchone()
        if existing:
            verse_id = existing[0]
            con.execute('DELETE FROM words WHERE verse_id=?', (verse_id,))
        else:
            cur = con.execute(
                'INSERT INTO verses(translation_id, book_id, chapter, verse) VALUES (?,?,?,?)',
                (translation_id, book_id, chapter, verse)
            )
            verse_id = cur.lastrowid
            verses_inserted += 1

        tokens = text.split()
        words_batch = [
            (verse_id, pos, language, tok, None, None, None, tok, '', '', '', '', None, None, None, None)
            for pos, tok in enumerate(tokens)
        ]
        con.executemany(
            """INSERT INTO words(
                verse_id, position, lang, original, translit, parsing, strong,
                gloss, beg_q, pnc, end_q, end_text, footnote, hdg, crossref, par_classes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            words_batch
        )
        words_inserted += len(words_batch)

    return verses_inserted, words_inserted


def main():
    ap = argparse.ArgumentParser(description="Import the Clementine Vulgate (USFX) or Douay-Rheims (Zefania) into River's SQLite DB")
    ap.add_argument('xml_file', help='Path to lat-clementine.usfx.xml or eng-dra.zefania.xml')
    ap.add_argument('--format', choices=['usfx', 'zefania'], default=None, help='Override auto-detection')
    ap.add_argument('--code', required=True)
    ap.add_argument('--name', required=True)
    ap.add_argument('--language', required=True)
    ap.add_argument('--db', default='bible.sqlite')
    ap.add_argument('--replace', action='store_true')
    args = ap.parse_args()

    fmt = args.format or detect_format(args.xml_file)
    verses = list(load_usfx_verses(args.xml_file) if fmt == 'usfx' else load_zefania_verses(args.xml_file))
    if not verses:
        sys.exit(f'No verses parsed as {fmt} — check the file structure, or pass --format to override detection.')

    con = sqlite3.connect(args.db)
    con.execute('PRAGMA foreign_keys = ON')

    with con:
        db_builder.ensure_schema(con)

        existing = con.execute('SELECT id FROM translations WHERE code=?', (args.code,)).fetchone()
        if existing and not args.replace:
            sys.exit(f'Translation code "{args.code}" already exists — pass --replace to overwrite it.')
        if existing:
            translation_id = existing[0]
            con.execute('UPDATE translations SET name=?, language=? WHERE id=?', (args.name, args.language, translation_id))
            db_builder.wipe_translation(con, translation_id)
        else:
            cur = con.execute('INSERT INTO translations(code, name, language) VALUES (?,?,?)',
                              (args.code, args.name, args.language))
            translation_id = cur.lastrowid

        books_data = con.execute('SELECT name, id FROM books').fetchall()
        book_id_by_name = {row[0]: row[1] for row in books_data}

        v, w = import_translation(con, translation_id, book_id_by_name, verses, args.language)

    print(f'Translation "{args.name}" ({args.code}), parsed as {fmt}: {v} verses, {w} words imported into {args.db}')


if __name__ == '__main__':
    main()
