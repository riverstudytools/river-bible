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

def get_book_info(con, book_identifier):
    """Finds a book by ID, exact name, or partial name match."""
    # Check if identifier is an integer ID
    if book_identifier.isdigit():
        row = con.execute('SELECT id, name FROM books WHERE id = ?', (int(book_identifier),)).fetchone()
        if row:
            return {'id': row[0], 'name': row[1]}

    # Check exact match (case-insensitive)
    row = con.execute('SELECT id, name FROM books WHERE UPPER(name) = UPPER(?)', (book_identifier,)).fetchone()
    if row:
        return {'id': row[0], 'name': row[1]}

    # Check partial match
    rows = con.execute('SELECT id, name FROM books WHERE UPPER(name) LIKE UPPER(?)', (f'%{book_identifier}%',)).fetchall()
    if len(rows) == 1:
        return {'id': rows[0][0], 'name': rows[0][1]}
    elif len(rows) > 1:
        matched_names = ", ".join([r[1] for r in rows])
        sys.exit(f"Error: Ambiguous book name '{book_identifier}'. Multiple matches found: {matched_names}")

    sys.exit(f"Error: Book '{book_identifier}' not found in database.")

def remove_book_from_translation(con, translation_id, book_id):
    """Deletes all verses (and cascades to words/edits) for a book in a translation."""
    cur = con.execute("""
        DELETE FROM verses 
        WHERE translation_id = ? 
          AND book_id = ?
    """, (translation_id, book_id))
    return cur.rowcount

def main():
    parser = argparse.ArgumentParser(description="Remove a specific book from a translation.")
    parser.add_argument('--target', required=True, help="Target translation code (e.g., KJV, WEB)")
    parser.add_argument('--book', required=True, help="Book name or ID to remove (e.g., Genesis, '1 John', 1)")
    parser.add_argument('--db', default="bible.sqlite", help="SQLite database file (default: bible.sqlite)")

    args = parser.parse_args()
    con = sqlite3.connect(args.db, timeout=20.0)
    con.execute('PRAGMA foreign_keys = ON')

    try:
        with con:
            db_builder.ensure_schema(con)
            
            target_info = get_translation_info(con, args.target)
            book_info = get_book_info(con, args.book)

            deleted_count = remove_book_from_translation(con, target_info['id'], book_info['id'])

            if deleted_count > 0:
                print(f"Successfully removed '{book_info['name']}' ({deleted_count} verses deleted) from translation '{target_info['code']}'.")
            else:
                print(f"No verses found for '{book_info['name']}' in translation '{target_info['code']}'. Nothing deleted.")

    finally:
        con.close()

if __name__ == '__main__':
    main()