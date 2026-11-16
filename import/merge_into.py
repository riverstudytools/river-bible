#!/usr/bin/env python3
import argparse
import sqlite3
import sys
import build_db as db_builder

def get_translation_info(con, code):
    row = con.execute('SELECT id, name, language FROM translations WHERE UPPER(code) = UPPER(?)', (code,)).fetchone()
    if not row:
        sys.exit(f"Error: Translation code '{code}' not found in database.")
    return {'id': row[0], 'code': code.upper(), 'name': row[1], 'language': row[2]}

def clear_testament_verses(con, target_trans_id, testament_code):
    """Deletes existing verses (and cascading words) for a specific testament."""
    con.execute("""
        DELETE FROM verses 
        WHERE translation_id = ? 
          AND book_id IN (SELECT id FROM books WHERE testament = ?)
    """, (target_trans_id, testament_code))

def merge_testament_verses(con, source_trans_id, target_trans_id, testament_code):
    verse_rows = con.execute("""
        SELECT v.id AS src_verse_id, v.book_id, v.chapter, v.verse
        FROM verses v
        JOIN books b ON b.id = v.book_id
        WHERE v.translation_id = ? AND b.testament = ?
        ORDER BY b.book_order, v.chapter, v.verse
    """, (source_trans_id, testament_code)).fetchall()

    if not verse_rows:
        return 0, 0

    verses_copied = 0
    words_copied = 0

    for src_verse_id, book_id, chapter, verse in verse_rows:
        cur = con.execute(
            'INSERT INTO verses(translation_id, book_id, chapter, verse) VALUES (?,?,?,?)',
            (target_trans_id, book_id, chapter, verse)
        )
        new_verse_id = cur.lastrowid
        verses_copied += 1

        words = con.execute("""
            SELECT position, lang, original, translit, parsing, strong, gloss,
                   beg_q, pnc, end_q, end_text, footnote, hdg, crossref, par_classes
            FROM words
            WHERE verse_id = ?
            ORDER BY position
        """, (src_verse_id,)).fetchall()

        if words:
            words_batch = [(new_verse_id, *w) for w in words]
            con.executemany("""
                INSERT INTO words(
                    verse_id, position, lang, original, translit, parsing, strong,
                    gloss, beg_q, pnc, end_q, end_text, footnote, hdg, crossref, par_classes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, words_batch)
            words_copied += len(words)

    return verses_copied, words_copied

def main():
    parser = argparse.ArgumentParser(description="In-place testament merger")
    parser.add_argument('--ot', help="Translation code for Old Testament source")
    parser.add_argument('--nt', help="Translation code for New Testament source")
    parser.add_argument('--target', required=True, help="Target translation code to update in-place")
    parser.add_argument('--db', default="bible.sqlite", help="SQLite database file")

    args = parser.parse_args()
    con = sqlite3.connect(args.db, timeout=20.0)
    con.execute('PRAGMA foreign_keys = ON')

    try:
        with con:
            db_builder.ensure_schema(con)
            target_info = get_translation_info(con, args.target)
            target_id = target_info['id']

            # Overwrite OT in target if --ot is provided and different
            if args.ot:
                ot_info = get_translation_info(con, args.ot)
                if ot_info['id'] != target_id:
                    clear_testament_verses(con, target_id, 'OT')
                    v, w = merge_testament_verses(con, ot_info['id'], target_id, 'OT')
                    print(f"Replaced OT in '{args.target}' using '{args.ot}': {v} verses copied.")

            # Overwrite NT in target if --nt is provided and different
            if args.nt:
                nt_info = get_translation_info(con, args.nt)
                if nt_info['id'] != target_id:
                    clear_testament_verses(con, target_id, 'NT')
                    v, w = merge_testament_verses(con, nt_info['id'], target_id, 'NT')
                    print(f"Replaced NT in '{args.target}' using '{args.nt}': {v} verses copied.")

        print(f"\nIn-place update completed for '{args.target}'.")
    finally:
        con.close()

if __name__ == '__main__':
    main()