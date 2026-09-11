#!/usr/bin/env python3
"""
import_swete.py — Fast Importer for Swete LXX TSV/CSV files into River's SQLite DB (bible.sqlite).

Format:
  00-Swete_versification.csv : TSV file mapping 1-based word index to reference (e.g. 1 \t Gen.1:1)
  01-Swete_word_with_punctuations.csv : TSV file mapping word index to word token (e.g. 1 \t ΕΝ)

Usage:
    python3 import_swete.py path/to/00-Swete_versification.csv path/to/01-Swete_word_with_punctuations.csv \
        --code LXX_SWETE --name "Swete Septuagint" --replace
"""

import argparse
import bisect
import csv
import os
import re
import sqlite3
import sys
import book_codes
import build_db as db_builder
import build_search_index as bsi

# Isolates leading/trailing punctuation including standard, Greek, and apparatus symbols (e.g. ⸂⸆⸃)
WORD_PUNCT_RE = re.compile(
    r"^([\s\.\,;:\·\?\!–—;·⸀-⹿\(\)\[\]«»]*)"
    r"([\w'’᾽᾿\-]+)"
    r"([\s\.,;:\·\?\!–—;·⸀-⹿\(\)\[\]«»]*)$",
    re.UNICODE
)


def parse_swete_files(versification_path, words_path):
    """
    Parses Swete versification and words files using O(log N) binary search for instant reference matching.
    """
    # 1. Parse Versification File (00-Swete_versification.csv)
    vers_markers = []
    with open(versification_path, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.reader(f, delimiter='\t')
        for row in reader:
            if not row or len(row) < 2:
                continue
            try:
                wid = int(row[0].strip())
                ref_str = row[1].strip()
                vers_markers.append((wid, ref_str))
            except ValueError:
                continue

    vers_markers.sort(key=lambda x: x[0])

    if not vers_markers:
        print("Error: No valid versification markers found.", file=sys.stderr)
        return []

    # Parallel arrays for fast O(log N) binary search lookup
    marker_ids = []
    ref_tuples = []

    for start_wid, ref_str in vers_markers:
        m_ref = re.match(r'^([^\.]+)\.?(\d+):(\d+)$', ref_str)
        if not m_ref:
            continue

        raw_book, ch_str, v_str = m_ref.groups()
        canonical_book = book_codes.resolve_canonical_name(raw_book)

        if not canonical_book:
            continue

        marker_ids.append(start_wid)
        ref_tuples.append((canonical_book, int(ch_str), int(v_str)))

    # 2. Parse Words File (01-Swete_word_with_punctuations.csv) using Binary Search
    verses_dict = {}

    with open(words_path, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.reader(f, delimiter='\t')
        for row in reader:
            if not row or len(row) < 2:
                continue
            try:
                wid = int(row[0].strip())
            except ValueError:
                continue

            raw_word = row[1].strip()

            # Binary search lookup: O(log N) instead of O(N)
            idx = bisect.bisect_right(marker_ids, wid) - 1
            if idx < 0:
                continue

            ref_tuple = ref_tuples[idx]

            pm = WORD_PUNCT_RE.match(raw_word)
            if pm:
                beg_q, word_clean, pnc = pm.group(1), pm.group(2), pm.group(3)
            else:
                # Standalone punctuation/symbol token
                if verses_dict.get(ref_tuple):
                    verses_dict[ref_tuple][-1]['pnc'] = (verses_dict[ref_tuple][-1]['pnc'] + raw_word).strip()
                    continue
                else:
                    beg_q, word_clean, pnc = raw_word, '', ''

            if ref_tuple not in verses_dict:
                verses_dict[ref_tuple] = []

            verses_dict[ref_tuple].append({
                'original': word_clean,
                'gloss': word_clean,
                'strong': None,
                'parsing': None,
                'beg_q': beg_q,
                'pnc': pnc
            })

    # Build verse records
    parsed_verses = []
    for (book, ch, v), words in verses_dict.items():
        if not words:
            continue
        parsed_verses.append({
            'book': book,
            'chapter': ch,
            'verse': v,
            'words': words
        })

    return parsed_verses


def process_input_files(versification_path, words_path, con, book_id_by_name, book_order_map):
    records = parse_swete_files(versification_path, words_path)
    all_verses = []

    for rec in records:
        book_name = rec['book']

        if book_name in book_id_by_name:
            book_id = book_id_by_name[book_name]
            b_order = book_order_map[book_name]
        else:
            testament = db_builder.testament_for(book_name)
            next_order = max(book_order_map.values(), default=-1) + 1
            cur = con.execute('INSERT INTO books(name, testament, book_order) VALUES (?,?,?)', (book_name, testament, next_order))
            book_id = cur.lastrowid
            b_order = next_order
            book_id_by_name[book_name] = book_id
            book_order_map[book_name] = b_order

        rec['book_id'] = book_id
        rec['book_order'] = b_order
        all_verses.append(rec)

    all_verses.sort(key=lambda x: (x['book_order'], x['chapter'], x['verse']))
    return all_verses


def import_swete_translation(con, translation_id, verse_records):
    verses_inserted = 0
    words_inserted = 0

    for rec in verse_records:
        existing_verse = con.execute(
            'SELECT id FROM verses WHERE translation_id=? AND book_id=? AND chapter=? AND verse=?',
            (translation_id, rec['book_id'], rec['chapter'], rec['verse'])
        ).fetchone()

        if existing_verse:
            verse_id = existing_verse[0]
            con.execute('DELETE FROM words WHERE verse_id=?', (verse_id,))
        else:
            cur_row = con.execute(
                'INSERT INTO verses(translation_id, book_id, chapter, verse) VALUES (?,?,?,?)',
                (translation_id, rec['book_id'], rec['chapter'], rec['verse'])
            )
            verse_id = cur_row.lastrowid
            verses_inserted += 1

        # Batch insert word rows for high speed
        words_batch = [
            (
                verse_id, pos, 'Greek',
                w_obj['original'], None, w_obj['parsing'], w_obj['strong'],
                w_obj['gloss'], w_obj['beg_q'], w_obj['pnc'], '', '',
                None, None, None, None
            )
            for pos, w_obj in enumerate(rec['words'])
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
    ap = argparse.ArgumentParser(description="Import Swete LXX TSV/CSV files into River SQLite DB")
    ap.add_argument('versification', help='Path to 00-Swete_versification.csv')
    ap.add_argument('words', help='Path to 01-Swete_word_with_punctuations.csv')
    ap.add_argument('--code', default='LXX_SWETE', help='Translation code (default: LXX_SWETE)')
    ap.add_argument('--name', default='Swete Septuagint', help='Display name')
    ap.add_argument('--language', default='Greek', help='Language of text')
    ap.add_argument('--db', default='bible.sqlite', help='SQLite DB path')
    ap.add_argument('--replace', action='store_true', help='Replace existing translation')
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=20.0)
    con.execute('PRAGMA foreign_keys = ON')

    try:
        with con:
            db_builder.ensure_schema(con)

            existing = con.execute('SELECT id FROM translations WHERE code=?', (args.code,)).fetchone()
            if existing and not args.replace:
                sys.exit(f'Error: Translation code "{args.code}" exists. Use --replace to overwrite.')

            if existing:
                translation_id = existing[0]
                con.execute('UPDATE translations SET name=?, language=? WHERE id=?', (args.name, args.language, translation_id))
                db_builder.wipe_translation(con, translation_id)
            else:
                cur = con.execute('INSERT INTO translations(code, name, language) VALUES (?,?,?)',
                                  (args.code, args.name, args.language))
                translation_id = cur.lastrowid

            books_data = con.execute('SELECT name, id, book_order FROM books').fetchall()
            book_id_by_name = {row[0]: row[1] for row in books_data}
            book_order_map = {row[0]: row[2] for row in books_data}

            verse_records = process_input_files(args.versification, args.words, con, book_id_by_name, book_order_map)

            if not verse_records:
                sys.exit("Error: No valid verses could be parsed from input file(s).")

            v_count, w_count = import_swete_translation(con, translation_id, verse_records)

        print(f'Successfully imported "{args.name}" ({args.code}): {v_count} verses, {w_count} words into {args.db}.')

    except Exception as e:
        sys.exit(f"Database import failed: {e}")
    finally:
        con.close()


if __name__ == '__main__':
    main()