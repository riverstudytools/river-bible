#!/usr/bin/env python3
"""
import_berean_tsv.py — Importer for BSB / MSB multi-column word-level TSV Bible files into bible.sqlite.

Usage:
    python3 import_berean_tsv.py path/to/default.tsv --code MSB --name "Majority Standard Bible" --replace
"""

import argparse
import csv
import re
import sqlite3
import sys
from datetime import datetime, timezone

import book_codes
import build_db as db_builder

# Updated column index mappings to match default.tsv schema
COL = {
    'verse': 3,
    'lang': 4,
    'greek': 6,
    'translit': 7,
    'parsing': 8,
    'strHeb': 10,
    'strGrk': 11,
    'verseId': 12,
    'hdg': 13,
    'crossref': 14,
    'par': 15,
    'space': 16,
    'begQ': 17,
    'gloss': 18,
    'pnc': 19,
    'endQ': 20,
    'footnotes': 21,
    'endText': 22,
    'edit': 23,
}


def load_rows(tsv_path):
    """Loads raw rows from a Berean TSV file, skipping header comments."""
    rows = []
    with open(tsv_path, 'r', encoding='utf-8', errors='replace') as f:
        lines = f.read().splitlines()
        if lines and lines[0].startswith('Heb Sort'):
            lines = lines[1:]
        reader = csv.reader(lines, delimiter='\t')
        for row in reader:
            if not row or len(row) < 20:
                continue
            rows.append(row)
    return rows


def strip_tags(s):
    return re.sub(r'<[^>]+>', '', s or '').strip()


def strip_tags_keep_spacing(s):
    return re.sub(r'<[^>]+>', '', s or '')


def extract_classes(s):
    classes = re.findall(r'class=\|([^|]+)\|', s or '')
    return ' '.join(classes) if classes else None


def fix_crossref_html(s):
    if not s:
        return None
    s = re.sub(r'class=\|([^|]+)\|', r'class="\1"', s)

    def repl(m):
        path = re.sub(r'^(\.\./)+', '', m.group(1))
        return 'href="https://biblehub.com/' + path + '" target="_blank" rel="noopener"'

    return re.sub(r'href\s*=\s*\|(\.\./[^|]+)\|', repl, s)


def strip_reftext(s):
    """Berean's TSV embeds a clickable verse-number badge —
    <span class=|reftext|><a href=|#|><b>1</b></a></span> — right in the
    begQ/pnc/endQ/endText of some verses' first word (verse 1 of a chapter,
    at least — see Psalm 3:1, where it lands on "O LORD"'s begQ). The app
    already draws its own verse number from verses.verse (index.html's
    <sup class="vnum">), so this leftover badge is dropped whole — tag AND
    digit, not just the tag — or the digit survives as plain text baked
    into the word's own punctuation (e.g. "1O LORD" instead of a proper,
    separately-clickable verse number)."""
    return re.sub(r'<span class=\|reftext\|>.*?</span>', '', s or '', flags=re.S)


def import_translation(con, translation_id, book_id_by_name, rows):
    """Populates verses, words, and edits for one translation."""
    now = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    cur_verse_num = None
    group = None   # {book, chapter, verse, rows: [raw rows with content]}
    verses_inserted = words_inserted = edits_inserted = 0

    def flush(group):
        nonlocal verses_inserted, words_inserted, edits_inserted
        if group is None or not group['rows']:
            return
        book_id = book_id_by_name.get(group['book'])
        if book_id is None:
            return  # Unrecognized book name — skip

        cur_row = con.execute(
            'INSERT INTO verses(translation_id, book_id, chapter, verse) VALUES (?,?,?,?)',
            (translation_id, book_id, group['chapter'], group['verse'])
        )
        verse_id = cur_row.lastrowid
        verses_inserted += 1

        for position, row in enumerate(group['rows']):
            lang = row[COL['lang']] if len(row) > COL['lang'] else ''
            str_heb = row[COL['strHeb']] if len(row) > COL['strHeb'] else ''
            str_grk = row[COL['strGrk']] if len(row) > COL['strGrk'] else ''
            strong = str_heb if lang == 'Hebrew' else str_grk

            greek_val = row[COL['greek']] if len(row) > COL['greek'] else ''
            translit_val = row[COL['translit']] if len(row) > COL['translit'] else ''
            parsing_val = row[COL['parsing']] if len(row) > COL['parsing'] else ''
            gloss_val = row[COL['gloss']] if len(row) > COL['gloss'] else ''
            beg_q_val = strip_reftext(row[COL['begQ']]) if len(row) > COL['begQ'] else ''
            pnc_val = strip_reftext(row[COL['pnc']]) if len(row) > COL['pnc'] else ''
            end_q_val = strip_reftext(row[COL['endQ']]) if len(row) > COL['endQ'] else ''
            end_text_val = strip_reftext(row[COL['endText']]) if len(row) > COL['endText'] else ''
            footnotes_val = row[COL['footnotes']] if len(row) > COL['footnotes'] else ''
            hdg_val = row[COL['hdg']] if len(row) > COL['hdg'] else ''
            crossref_val = row[COL['crossref']] if len(row) > COL['crossref'] else ''
            par_val = row[COL['par']] if len(row) > COL['par'] else ''

            word_row = con.execute(
                """INSERT INTO words(verse_id, position, lang, original, translit, parsing, strong,
                                      gloss, beg_q, pnc, end_q, end_text, footnote, hdg, crossref, par_classes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    verse_id, position, lang,
                    greek_val, translit_val, (parsing_val or '').strip(),
                    strong, gloss_val or '',
                    strip_tags_keep_spacing(beg_q_val),
                    strip_tags_keep_spacing(pnc_val),
                    strip_tags_keep_spacing(end_q_val),
                    strip_tags_keep_spacing(end_text_val),
                    footnotes_val or '',
                    strip_tags(re.sub(r'class=\|[^|]+\|', '', hdg_val)) if hdg_val else None,
                    fix_crossref_html(crossref_val),
                    extract_classes(par_val) if par_val else None,
                )
            )
            words_inserted += 1

            edit_val = (row[COL['edit']] or '').strip() if len(row) > COL['edit'] else ''
            if edit_val:
                con.execute('INSERT INTO edits(word_id, gloss, updated_at) VALUES (?,?,?)',
                            (word_row.lastrowid, edit_val, now))
                edits_inserted += 1

    for row in rows:
        if len(row) <= COL['verse']:
            continue
        vnum = row[COL['verse']]
        if vnum != cur_verse_num:
            flush(group)
            verse_id_text = row[COL['verseId']] if len(row) > COL['verseId'] else ''
            m = re.match(r'^(.*)\s(\d+):(\d+)\s*$', verse_id_text or '')
            if m:
                raw_book = m.group(1)
                resolved_book = book_codes.resolve_canonical_name(raw_book)
                book, chapter, verse = resolved_book, int(m.group(2)), int(m.group(3))
            elif group is not None:
                book, chapter, verse = group['book'], group['chapter'], group['verse'] + 1
            else:
                book, chapter, verse = 'Unknown', 0, 0
            group = {'book': book, 'chapter': chapter, 'verse': verse, 'rows': []}
            cur_verse_num = vnum

        if len(row) > COL['greek'] and row[COL['greek']]:
            group['rows'].append(row)

    flush(group)

    return verses_inserted, words_inserted, edits_inserted


def main():
    ap = argparse.ArgumentParser(description="Import Berean TSV into River SQLite DB")
    ap.add_argument('tsv', help='Word-level TSV file for this translation')
    ap.add_argument('--code', required=True, help='Short unique code for this translation, e.g. MSB')
    ap.add_argument('--name', required=True, help='Display name, e.g. "Majority Standard Bible"')
    ap.add_argument('--language', default='English')
    ap.add_argument('--db', default='bible.sqlite')
    ap.add_argument('--replace', action='store_true', help='Overwrite this --code if it already exists')
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.execute('PRAGMA foreign_keys = ON')

    with con:
        db_builder.ensure_schema(con)

        existing = con.execute('SELECT id FROM translations WHERE code=?', (args.code,)).fetchone()
        if existing and not args.replace:
            sys.exit(f'Translation code "{args.code}" already exists in {args.db} — pass --replace to overwrite it.')

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

        rows = load_rows(args.tsv)

        v, w, e = import_translation(con, translation_id, book_id_by_name, rows)

    print(f'Translation "{args.name}" ({args.code}): {v} verses, {w} words, {e} edits imported into {args.db}')


if __name__ == '__main__':
    main()