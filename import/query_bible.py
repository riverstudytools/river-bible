#!/usr/bin/env python3
"""
query_bible.py — Complete Raw Data Dump Utility for bible.sqlite.

Fetches 100% of the relational data stored for a verse and its child words,
including verse primary keys, book metadata, word tokens, original lemmas,
Strong's numbers, punctuation, headings, footnotes, cross-references, and edits.

Usage:
    python3 query_bible.py [--code KJV] [--db bible.sqlite]
"""

import argparse
import json
import re
import sqlite3
import sys
import book_codes as bc


def fetch_available_translations(con):
    rows = con.execute("SELECT code, name FROM translations ORDER BY id").fetchall()
    return {code.upper(): name for code, name in rows}


def query_and_print_passage(con, translation_code, passage_str):
    m = re.match(r"^(.+?)\s+(\d+)(?::(\d+)(?:-(\d+))?)?$", passage_str.strip())
    if not m:
        print("Invalid format. Try formats like: 'Gen 1:1', 'John 3', or '1 Cor 13:4-7'")
        return

    raw_book, ch_str, v_start_str, v_end_str = m.groups()
    canonical_book = bc.resolve_canonical_name(raw_book) or raw_book
    chapter = int(ch_str)
    v_start = int(v_start_str) if v_start_str else None
    v_end = int(v_end_str) if v_end_str else v_start

    trans_row = con.execute("SELECT id, name, language FROM translations WHERE UPPER(code) = UPPER(?)", (translation_code,)).fetchone()
    if not trans_row:
        print(f"Error: Translation '{translation_code}' not found.")
        return

    trans_id, trans_name, trans_lang = trans_row

    # Query every single column across verses, books, words, and edits
    query = """
        SELECT 
            v.id AS verse_id,
            v.translation_id,
            b.id AS book_id,
            b.name AS book_name,
            b.testament,
            b.book_order,
            v.chapter,
            v.verse,
            w.id AS word_id,
            w.position,
            w.lang AS word_lang,
            w.original,
            w.translit,
            w.parsing,
            w.strong,
            w.gloss,
            w.beg_q,
            w.pnc,
            w.end_q,
            w.end_text,
            w.footnote,
            w.hdg,
            w.crossref,
            w.par_classes,
            e.gloss AS edit_gloss
        FROM verses v
        JOIN books b ON b.id = v.book_id
        JOIN words w ON w.verse_id = v.id
        LEFT JOIN edits e ON e.word_id = w.id
        WHERE v.translation_id = ?
          AND LOWER(b.name) = LOWER(?)
          AND v.chapter = ?
    """
    params = [trans_id, canonical_book, chapter]

    if v_start is not None and v_end is not None:
        query += " AND v.verse BETWEEN ? AND ?"
        params.extend([v_start, v_end])

    query += " ORDER BY v.chapter, v.verse, w.position"

    rows = con.execute(query, params).fetchall()

    if not rows:
        v_label = f":{v_start}" if v_start else ""
        if v_start and v_end and v_start != v_end:
            v_label = f":{v_start}-{v_end}"
        print(f"No records found for {canonical_book} {chapter}{v_label} in '{translation_code}'.")
        return

    # Group raw word records by verse ID
    verses_dict = {}
    for r in rows:
        v_id = r[0]
        if v_id not in verses_dict:
            verses_dict[v_id] = {
                'verse_id': r[0],
                'translation_id': r[1],
                'translation_code': translation_code.upper(),
                'translation_name': trans_name,
                'book_id': r[2],
                'book_name': r[3],
                'testament': r[4],
                'book_order': r[5],
                'chapter': r[6],
                'verse': r[7],
                'words': []
            }

        word_dict = {
            'word_id': r[8],
            'position': r[9],
            'lang': r[10],
            'original': r[11],
            'translit': r[12],
            'parsing': r[13],
            'strong': r[14],
            'gloss': r[15],
            'beg_q': r[16],
            'pnc': r[17],
            'end_q': r[18],
            'end_text': r[19],
            'footnote': r[20],
            'heading': r[21],
            'crossref': r[22],
            'par_classes': r[23],
            'edit_gloss': r[24]
        }
        clean_word = {k: v for k, v in word_dict.items() if v is not None and v != ''}
        verses_dict[v_id]['words'].append(clean_word)

    print(f"\nRAW VERSE DUMP FOR {trans_name} ({translation_code.upper()}) | {canonical_book} {chapter}")
    print("=" * 100)

    for v_id, verse_data in verses_dict.items():
        print(json.dumps(verse_data, indent=2, ensure_ascii=False))
        print("-" * 100)


def interactive_loop(con, default_code):
    translations = fetch_available_translations(con)
    if not translations:
        sys.exit("Error: No translations found in bible.sqlite.")

    current_code = default_code.upper() if default_code.upper() in translations else list(translations.keys())[0]

    print("\n=======================================================")
    print("  Bible SQLite Complete Raw Data Query Tool")
    print("=======================================================")
    print(f"Loaded Translations: {', '.join(translations.keys())}")
    print(f"Active Translation:  {current_code} ({translations[current_code]})")
    print("\nCommands:")
    print("  - Type a passage:  e.g., 'Gen 1:1', 'John 3:16', '1 Cor 13:4'")
    print("  - Switch version:  e.g., 'use WEB' or 'use KJV'")
    print("  - Exit session:    'exit', 'quit', or Ctrl+C\n")

    while True:
        try:
            user_input = input(f"[{current_code}] > ").strip()
            if not user_input:
                continue

            if user_input.lower() in ('exit', 'quit', 'q'):
                print("Goodbye!")
                break

            if user_input.lower().startswith(('use ', 'set ')):
                new_code = user_input.split()[1].upper()
                if new_code in translations:
                    current_code = new_code
                    print(f"Switched active translation to: {current_code} ({translations[current_code]})\n")
                else:
                    print(f"Unknown translation code '{new_code}'. Available: {', '.join(translations.keys())}\n")
                continue

            query_and_print_passage(con, current_code, user_input)

        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break


def main():
    parser = argparse.ArgumentParser(description="Complete Raw Data Query Tool for bible.sqlite")
    parser.add_argument('-c', '--code', default='KJV', help="Default translation code (e.g., KJV, WEB)")
    parser.add_argument('--db', default='bible.sqlite', help="Database path (default: bible.sqlite)")

    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    try:
        interactive_loop(con, args.code)
    finally:
        con.close()


if __name__ == '__main__':
    main()