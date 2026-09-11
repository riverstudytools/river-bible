#!/usr/bin/env python3
"""
import_kjv_json.py — Production importer for JSON Bible translations.
Supports OT, NT, Catholic, Eastern Orthodox, and Ethiopian Orthodox canons.
Uses book_codes.py for centralized book resolution and build_db.py for schema management.
"""

import argparse
import json
import re
import sqlite3
import sys
import book_codes
import build_db as db_builder
import build_search_index as bsi

WORD_PUNCT_RE = re.compile(r"^([^\w]*)([\w'\u2019\-]+)([^\w]*)$", re.UNICODE)


def ensure_book_in_db(con, book_name, book_id_by_name, book_order_map):
    """
    If a book is not in the database, inserts it under its appropriate testament
    and assigns it the next available book_order.
    """
    if book_name in book_id_by_name:
        return book_id_by_name[book_name], book_order_map[book_name]

    testament = bsi.testament_for(book_name)
    next_order = max(book_order_map.values(), default=0) + 1

    cur = con.execute(
        'INSERT INTO books(name, testament, book_order) VALUES (?,?,?)',
        (book_name, testament, next_order)
    )
    book_id = cur.lastrowid
    book_id_by_name[book_name] = book_id
    book_order_map[book_name] = next_order

    print(f"Info: Dynamically added book '{book_name}' to database (ID={book_id}, testament='{testament}').")
    return book_id, next_order


def normalize_json_data(raw_data, con, book_id_by_name, book_order_map):
    normalized = []

    def parse_entry(book_raw, chap_str, verse_str, text):
        # Use centralized book_codes resolver instead of raw bsi lookup
        canonical_book = book_codes.resolve_canonical_name(str(book_raw))
        
        if not canonical_book:
            print(f"Warning: Could not resolve book name for '{book_raw}'. Skipping passage.", file=sys.stderr)
            return None

        book_id, b_order = ensure_book_in_db(con, canonical_book, book_id_by_name, book_order_map)
        
        has_paragraph = text.startswith('#')
        cleaned_text = text.replace('#', '').strip()
        
        return {
            'book': canonical_book,
            'book_id': book_id,
            'chapter': int(chap_str),
            'verse': int(verse_str),
            'text': cleaned_text,
            'is_paragraph': has_paragraph,
            'book_order': b_order
        }

    # 1. Dictionary Formats
    if isinstance(raw_data, dict):
        first_val = next(iter(raw_data.values()), None) if raw_data else None

        # Flat Dict ("Genesis 1:1": "In the beginning...")
        if isinstance(first_val, str):
            pattern = re.compile(r'^(.*?)\s+(\d+):(\d+)$')
            for key, text in raw_data.items():
                m = pattern.match(key.strip())
                if m:
                    entry = parse_entry(m.group(1), m.group(2), m.group(3), text)
                    if entry:
                        normalized.append(entry)

        # Nested Dict ({"Genesis": {"1": {"1": "..."}}})
        elif isinstance(first_val, dict):
            for book_name, chapters in raw_data.items():
                if not isinstance(chapters, dict):
                    continue
                for chap_num, verses in chapters.items():
                    if not isinstance(verses, dict):
                        continue
                    for v_num, text in verses.items():
                        if isinstance(text, str) and text.strip():
                            entry = parse_entry(book_name, chap_num, v_num, text)
                            if entry:
                                normalized.append(entry)

    # 2. List of Objects ([{"book": "Genesis", "chapter": 1, "verse": 1, "text": "..."}])
    elif isinstance(raw_data, list):
        for item in raw_data:
            book_raw = item.get('book') or item.get('book_name') or item.get('abbrev') or item.get('code')
            chapter = item.get('chapter', 0)
            verse = item.get('verse', 0)
            text = str(item.get('text', '')).strip()
            if book_raw and chapter and verse and text:
                entry = parse_entry(book_raw, chapter, verse, text)
                if entry:
                    normalized.append(entry)

    normalized.sort(key=lambda x: (x['book_order'], x['chapter'], x['verse']))
    return normalized


def import_json_translation(con, translation_id, verse_records):
    verses_inserted = 0
    words_inserted = 0

    for rec in verse_records:
        # Check if verse already exists to prevent integrity collisions
        existing_verse = con.execute(
            'SELECT id FROM verses WHERE translation_id=? AND book_id=? AND chapter=? AND verse=?',
            (translation_id, rec['book_id'], rec['chapter'], rec['verse'])
        ).fetchone()

        if existing_verse:
            verse_id = existing_verse[0]
            con.execute('DELETE FROM words WHERE verse_id=?', (verse_id,))
            con.execute('DELETE FROM verses_fts WHERE rowid=?', (verse_id,))
        else:
            cur_row = con.execute(
                'INSERT INTO verses(translation_id, book_id, chapter, verse) VALUES (?,?,?,?)',
                (translation_id, rec['book_id'], rec['chapter'], rec['verse'])
            )
            verse_id = cur_row.lastrowid
            verses_inserted += 1

        con.execute('INSERT OR REPLACE INTO verses_fts(rowid, text) VALUES (?,?)', (verse_id, rec['text']))

        raw_words = rec['text'].split()
        for pos, word_token in enumerate(raw_words):
            m = WORD_PUNCT_RE.match(word_token)
            beg_q, gloss, pnc = (m.group(1), m.group(2), m.group(3)) if m else ('', word_token, '')
            par_classes = 'paragraph' if (pos == 0 and rec.get('is_paragraph')) else None

            con.execute(
                """INSERT INTO words(
                    verse_id, position, lang, original, translit, parsing, strong,
                    gloss, beg_q, pnc, end_q, end_text, footnote, hdg, crossref, par_classes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    verse_id, pos, 'English',
                    None, None, None, None,
                    gloss, beg_q, pnc, '', '',
                    None, None, None, par_classes
                )
            )
            words_inserted += 1

    return verses_inserted, words_inserted


def main():
    ap = argparse.ArgumentParser(description="Import JSON Bible translation into River SQLite DB")
    ap.add_argument('json_file', help='Path to JSON file')
    ap.add_argument('--code', default='KJV', help='Translation code')
    ap.add_argument('--name', default='King James Version', help='Display name')
    ap.add_argument('--language', default='English', help='Language')
    ap.add_argument('--db', default='bible.sqlite', help='SQLite DB path')
    ap.add_argument('--replace', action='store_true', help='Replace if translation code exists')
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=20.0)
    con.execute('PRAGMA foreign_keys = ON')

    try:
        with con:
            db_builder.ensure_schema(con)

            existing = con.execute('SELECT id FROM translations WHERE code=?', (args.code,)).fetchone()
            if existing and not args.replace:
                sys.exit(f'Error: Translation code "{args.code}" exists. Pass --replace to overwrite.')

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

            with open(args.json_file, 'r', encoding='utf-8') as f:
                raw_json = json.load(f)

            verse_records = normalize_json_data(raw_json, con, book_id_by_name, book_order_map)

            if not verse_records:
                sys.exit("Error: No valid verses could be parsed from input JSON.")

            v_count, w_count = import_json_translation(con, translation_id, verse_records)

        print(f'Successfully imported "{args.name}" ({args.code}): {v_count} verses, {w_count} words into {args.db}.')

    except Exception as e:
        sys.exit(f"Database import failed: {e}")
    finally:
        con.close()


if __name__ == '__main__':
    main()