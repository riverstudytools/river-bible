#!/usr/bin/env python3
"""
import_nestle_csv.py — Importer for word-per-row Greek Nestle/GNT CSV/TSV formats into bible.sqlite.

Format handled:
    BCV       text       func_morph  form_morph  strongs    lemma   normalized
    Matt 1:1  Βίβλος     N-NSF       N-NSF       976        βίβλος  Βίβλος
    Matt 1:1  Ἀβραάμ.    N-PRI       N-PRI       11         Ἀβραάμ  Ἀβραάμ

Usage:
    python3 import_nestle_csv.py path/to/nestle.csv --code NE1904 --name "Nestle 1904 Greek NT" --replace
"""

import argparse
import csv
import os
import re
import sqlite3
import sys
import book_codes
import build_db as db_builder
import build_search_index as bsi

# Isolates leading/trailing punctuation including standard and Greek-specific marks (· and ;)
WORD_PUNCT_RE = re.compile(r"^([^\w]*)([\w'\u2019\-]+)([\s\.,;:\·\?\!–—\u037e\u0387]*)$", re.UNICODE)


def format_strongs(raw_strong):
    """Formats raw Strong's values (e.g. '1080&5656' or '976') to standard 'G1080' codes."""
    if not raw_strong:
        return None

    # Extract primary numeric sequence
    primary_num = re.split(r'[&,]', raw_strong.strip())[0]
    m = re.search(r'\d+', primary_num)
    if m:
        return f"G{m.group(0)}"
    return None


def parse_nestle_file(file_path):
    """Parses a word-per-row TSV/CSV file into structured verse records."""
    verses_dict = {}

    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        sample = f.read(2048)
        f.seek(0)
        delimiter = '\t' if '\t' in sample else ','
        reader = csv.reader(f, delimiter=delimiter)

        for row in reader:
            if not row or len(row) < 3:
                continue

            # Skip header row
            if row[0].strip().upper() in ('BCV', 'REF', 'VERSE'):
                continue

            bcv = row[0].strip()  # e.g. "Matt 1:1"
            text_word = row[1].strip()  # e.g. "Ἀβραάμ."

            # Optional columns
            form_morph = row[3].strip() if len(row) > 3 else None
            strongs_raw = row[4].strip() if len(row) > 4 else None
            lemma = row[5].strip() if len(row) > 5 else None

            # Parse BCV format: "Matt 1:1" -> book="Matt", chapter=1, verse=1
            m_bcv = re.match(r'^(.+?)\s+(\d+):(\d+)$', bcv)
            if not m_bcv:
                continue

            raw_book, ch_str, v_str = m_bcv.groups()
            
            # Use centralized book_codes module for resolution
            canonical_book = book_codes.resolve_canonical_name(raw_book)

            if not canonical_book:
                print(f"Warning: Could not resolve book name for '{raw_book}' in line: {row}", file=sys.stderr)
                continue

            chapter = int(ch_str)
            verse = int(v_str)

            # Isolate leading/trailing punctuation from reading text token
            pm = WORD_PUNCT_RE.match(text_word)
            if pm:
                beg_q, word_clean, pnc = pm.group(1), pm.group(2), pm.group(3)
            else:
                beg_q, word_clean, pnc = '', text_word, ''

            verse_key = (canonical_book, chapter, verse)

            if verse_key not in verses_dict:
                verses_dict[verse_key] = []

            verses_dict[verse_key].append({
                'gloss': word_clean,      # Display text in UI (same convention as the Swete/Robinson importers)
                'original': word_clean,   # Source-language surface text
                'lemma': lemma,           # Dictionary form, straight from the file's own lemma column
                'strong': format_strongs(strongs_raw),
                'parsing': form_morph,
                'beg_q': beg_q,
                'pnc': pnc
            })

    # Convert dictionary entries into structured verse records
    parsed_verses = []
    for (book, ch, v), words in verses_dict.items():
        parsed_verses.append({
            'book': book,
            'chapter': ch,
            'verse': v,
            'words': words
        })

    return parsed_verses


def process_input_files(input_path, con, book_id_by_name, book_order_map):
    all_verses = []

    if os.path.isfile(input_path):
        files = [input_path]
    elif os.path.isdir(input_path):
        files = [
            os.path.join(input_path, f) for f in os.listdir(input_path)
            if f.lower().endswith(('.csv', '.tsv', '.txt'))
        ]
    else:
        sys.exit(f"Error: Path '{input_path}' not found.")

    for f_path in files:
        records = parse_nestle_file(f_path)
        for rec in records:
            book_name = rec['book']

            if book_name in book_id_by_name:
                book_id = book_id_by_name[book_name]
                b_order = book_order_map[book_name]
            else:
                testament = bsi.testament_for(book_name)
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


def import_nestle_translation(con, translation_id, verse_records):
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
                w_obj['original'], None, w_obj['parsing'], w_obj['strong'],
                w_obj['lemma'], w_obj['gloss'], w_obj['beg_q'], w_obj['pnc'], '', '',
                None, None, None, None
            )
            for pos, w_obj in enumerate(rec['words'])
        ]

        con.executemany(
            """INSERT INTO words(
                verse_id, position, lang, original, translit, parsing, strong,
                lemma, gloss, beg_q, pnc, end_q, end_text, footnote, hdg, crossref, par_classes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            words_batch
        )
        words_inserted += len(words_batch)

    return verses_inserted, words_inserted


def main():
    ap = argparse.ArgumentParser(description="Import Greek Nestle/GNT CSV files into River SQLite DB")
    ap.add_argument('input', help='Path to CSV/TSV file or directory containing files')
    ap.add_argument('--code', required=True, help='Translation code (e.g. NE1904, SBLGNT)')
    ap.add_argument('--name', required=True, help='Display name (e.g. "Nestle 1904 Greek NT")')
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

            verse_records = process_input_files(args.input, con, book_id_by_name, book_order_map)

            if not verse_records:
                sys.exit("Error: No valid verses could be parsed from input file(s).")

            v_count, w_count = import_nestle_translation(con, translation_id, verse_records)

        print(f'Successfully imported "{args.name}" ({args.code}): {v_count} verses, {w_count} words into {args.db}.')

    except Exception as e:
        sys.exit(f"Database import failed: {e}")
    finally:
        con.close()


if __name__ == '__main__':
    main()