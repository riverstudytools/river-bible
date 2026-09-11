#!/usr/bin/env python3
"""
merge_translations.py — Merges sections across existing database translations
into a new composite translation in bible.sqlite.

Example Usage:
    # Combine OT from WEB and NT from NE1904 into a new composite 'WEB_GNT' translation:
    python3 merge_translations.py --ot WEB --nt NE1904 --code WEB_GNT --name "WEB OT + Nestle 1904 NT" --replace

    # Optional: Include Apocrypha from OT source (or specify a separate source):
    python3 merge_translations.py --ot WEB --nt NE1904 --ap WEB --code HYBRID --name "Hybrid Reader Bible" --replace
"""

import argparse
import sqlite3
import sys
import build_db as db_builder


def get_translation_info(con, code):
    """Fetches translation metadata (id, name, language) by translation code."""
    row = con.execute('SELECT id, name, language FROM translations WHERE UPPER(code) = UPPER(?)', (code,)).fetchone()
    if not row:
        sys.exit(f"Error: Translation code '{code}' not found in database.")
    return {'id': row[0], 'code': code.upper(), 'name': row[1], 'language': row[2]}


def merge_testament_verses(con, source_trans_id, target_trans_id, testament_code):
    """
    Copies all verses and child words for a specific testament ('OT', 'NT', or 'AP')
    from source_trans_id to target_trans_id.
    """
    # Fetch all verses belonging to books in the requested testament
    verse_rows = con.execute("""
        SELECT v.id AS src_verse_id, v.book_id, v.chapter, v.verse
        FROM verses v
        JOIN books b ON b.id = v.book_id
        WHERE v.translation_id = ? AND b.testament = ?
        ORDER BY b.book_order, v.chapter, v.verse
    """, (source_trans_id, testament_code)).fetchall()

    if not verse_rows:
        print(f"Info: No verses found for testament '{testament_code}' in source translation ID {source_trans_id}.")
        return 0, 0

    verses_copied = 0
    words_copied = 0

    for src_verse_id, book_id, chapter, verse in verse_rows:
        # Create new verse record for target translation
        cur = con.execute(
            'INSERT INTO verses(translation_id, book_id, chapter, verse) VALUES (?,?,?,?)',
            (target_trans_id, book_id, chapter, verse)
        )
        new_verse_id = cur.lastrowid
        verses_copied += 1

        # Copy all word records for this verse
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
    parser = argparse.ArgumentParser(description="Merge testaments from different translations in bible.sqlite")
    parser.add_argument('--ot', required=True, help="Translation code for Old Testament source (e.g., WEB, KJV)")
    parser.add_argument('--nt', required=True, help="Translation code for New Testament source (e.g., NE1904, TR)")
    parser.add_argument('--ap', help="Optional translation code for Apocrypha source (defaults to OT source if present)")
    parser.add_argument('--code', required=True, help="New composite translation code (e.g., HYBRID, WEB_GNT)")
    parser.add_argument('--name', required=True, help="Display name for the merged translation")
    parser.add_argument('--language', default="English/Greek", help="Language label for composite entry")
    parser.add_argument('--db', default="bible.sqlite", help="SQLite database file (default: bible.sqlite)")
    parser.add_argument('--replace', action='store_true', help="Overwrite target translation if code already exists")

    args = parser.parse_args()

    con = sqlite3.connect(args.db, timeout=20.0)
    con.execute('PRAGMA foreign_keys = ON')

    try:
        with con:
            db_builder.ensure_schema(con)

            ot_info = get_translation_info(con, args.ot)
            nt_info = get_translation_info(con, args.nt)
            ap_info = get_translation_info(con, args.ap) if args.ap else ot_info

            # Check if target translation code already exists
            existing = con.execute('SELECT id FROM translations WHERE UPPER(code) = UPPER(?)', (args.code,)).fetchone()
            if existing and not args.replace:
                sys.exit(f'Error: Translation code "{args.code}" already exists. Use --replace to overwrite.')

            if existing:
                target_trans_id = existing[0]
                con.execute('UPDATE translations SET name=?, language=? WHERE id=?', (args.name, args.language, target_trans_id))
                db_builder.wipe_translation(con, target_trans_id)
                print(f"Replaced existing translation '{args.code}' (ID: {target_trans_id}).")
            else:
                cur = con.execute('INSERT INTO translations(code, name, language) VALUES (?,?,?)',
                                  (args.code.upper(), args.name, args.language))
                target_trans_id = cur.lastrowid
                print(f"Created new translation '{args.code}' (ID: {target_trans_id}).")

            # 1. Merge Old Testament
            ot_v, ot_w = merge_testament_verses(con, ot_info['id'], target_trans_id, 'OT')
            print(f"  + OT: {ot_v} verses, {ot_w} words copied from '{ot_info['code']}'.")

            # 2. Merge New Testament
            nt_v, nt_w = merge_testament_verses(con, nt_info['id'], target_trans_id, 'NT')
            print(f"  + NT: {nt_v} verses, {nt_w} words copied from '{nt_info['code']}'.")

            # 3. Merge Apocrypha (if available in source)
            ap_v, ap_w = merge_testament_verses(con, ap_info['id'], target_trans_id, 'AP')
            if ap_v > 0:
                print(f"  + AP: {ap_v} verses, {ap_w} words copied from '{ap_info['code']}'.")

            total_v = ot_v + nt_v + ap_v
            total_w = ot_w + nt_w + ap_w

        print(f"\nSuccessfully created composite translation '{args.name}' ({args.code.upper()}): {total_v} total verses, {total_w} total words.")

    except Exception as e:
        sys.exit(f"Merge operation failed: {e}")
    finally:
        con.close()


if __name__ == '__main__':
    main()