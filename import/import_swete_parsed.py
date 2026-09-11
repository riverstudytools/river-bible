#!/usr/bin/env python3
"""
import_swete_parsed.py — Importer for Swete LXX Parsed TSV into River's SQLite DB (bible.sqlite).

Inputs:
  1. Parsed TSV (e.g., swete_parsed.tsv) containing: ref, word_id, surface, lemma, parse, pos, confidence, strongs, strongs_source
  2. Punctuation CSV (01-Swete_word_with_punctuations.csv) containing: word_id, word_with_punct
  3. Transliterations CSV (03-Swete_SBL_transliterations.csv) containing: word_id, transliteration

Usage:
    python3 import_swete_parsed.py swete_parsed.tsv 01-Swete_word_with_punctuations.csv 03-Swete_SBL_transliterations.csv \
        --code LXX_SWETE --name "Swete Septuagint Parsed" --replace
"""

import argparse
import csv
import re
import sqlite3
import sys
import book_codes
import build_db as db_builder

# Isolates leading/trailing punctuation including standard, Greek, and apparatus symbols
WORD_PUNCT_RE = re.compile(
    r"^([\s\.\,;:\·\?\!–—;·⸀-⹿\(\)\[\]«»]*)"
    r"([\w'’᾽᾿\-]+)"
    r"([\s\.,;:\·\?\!–—;·⸀-⹿\(\)\[\]«»]*)$",
    re.UNICODE
)


def load_punctuation_map(punct_path):
    """
    Reads 01-Swete_word_with_punctuations.csv and returns a map of word_id -> (beg_q, pnc).
    """
    punct_map = {}
    with open(punct_path, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.reader(f, delimiter='\t')
        for row in reader:
            if not row or len(row) < 2:
                continue
            try:
                wid = int(row[0].strip())
            except ValueError:
                continue

            raw_word = row[1].strip()
            pm = WORD_PUNCT_RE.match(raw_word)

            if pm:
                beg_q, _, pnc = pm.group(1), pm.group(2), pm.group(3)
            else:
                beg_q, pnc = raw_word, ''

            punct_map[wid] = (beg_q, pnc)

    return punct_map


def load_transliteration_map(translit_path):
    """
    Reads 03-Swete_SBL_transliterations.csv and returns a map of word_id -> transliteration.
    """
    translit_map = {}
    with open(translit_path, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.reader(f, delimiter='\t')
        for row in reader:
            if not row or len(row) < 2:
                continue
            try:
                wid = int(row[0].strip())
            except ValueError:
                continue

            translit_map[wid] = row[1].strip()

    return translit_map


def parse_swete_parsed_tsv(parsed_tsv_path, punct_map, translit_map):
    """
    Parses swete_parsed.tsv and combines data with punctuation and transliterations.
    """
    verses_dict = {}

    with open(parsed_tsv_path, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.DictReader(f, delimiter='\t')
        for row in reader:
            ref_str = row.get('ref', '').strip()
            if not ref_str:
                continue

            m_ref = re.match(r'^([^\.]+)\.?(\d+):(\d+)$', ref_str)
            if not m_ref:
                continue

            raw_book, ch_str, v_str = m_ref.groups()
            canonical_book = book_codes.resolve_canonical_name(raw_book)
            if not canonical_book:
                continue

            ref_tuple = (canonical_book, int(ch_str), int(v_str))

            try:
                wid = int(row['word_id'].strip())
            except (KeyError, ValueError):
                wid = None

            surface = row.get('surface', '').strip()
            lemma = row.get('lemma', '').strip()
            pos = row.get('pos', '').strip()
            parse = row.get('parse', '').strip()
            strongs_raw = row.get('strongs', '').strip()

            # Format POS and Parsing combined (e.g. "V- AAI.3S" or "N- N-NSF")
            parsing_combined = f"{pos} {parse}".strip() if pos or parse else None

            # Format Strong's identifier (e.g. "G4160")
            strongs_formatted = f"G{strongs_raw}" if strongs_raw and strongs_raw.isdigit() else strongs_raw or None

            # Pull punctuation and transliteration by word_id
            beg_q, pnc = punct_map.get(wid, ('', ''))
            translit = translit_map.get(wid, None)

            if ref_tuple not in verses_dict:
                verses_dict[ref_tuple] = []

            verses_dict[ref_tuple].append({
                'original': surface,
                'translit': translit,
                'gloss': lemma,
                'strong': strongs_formatted,
                'parsing': parsing_combined,
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


def process_input_files(parsed_tsv_path, punct_path, translit_path, con, book_id_by_name, book_order_map):
    punct_map = load_punctuation_map(punct_path)
    translit_map = load_transliteration_map(translit_path)
    records = parse_swete_parsed_tsv(parsed_tsv_path, punct_map, translit_map)

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

        words_batch = [
            (
                verse_id, pos, 'Greek',
                w_obj['original'], w_obj['translit'], w_obj['parsing'], w_obj['strong'],
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
    ap = argparse.ArgumentParser(description="Import Swete LXX Parsed TSV into River SQLite DB")
    ap.add_argument('parsed_tsv', help='Path to swete_parsed.tsv')
    ap.add_argument('punct_csv', help='Path to 01-Swete_word_with_punctuations.csv')
    ap.add_argument('translit_csv', help='Path to 03-Swete_SBL_transliterations.csv')
    ap.add_argument('--code', default='LXX_SWETE', help='Translation code (default: LXX_SWETE)')
    ap.add_argument('--name', default='Swete Septuagint Parsed', help='Display name')
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

            verse_records = process_input_files(
                args.parsed_tsv, args.punct_csv, args.translit_csv,
                con, book_id_by_name, book_order_map
            )

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