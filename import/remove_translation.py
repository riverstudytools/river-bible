#!/usr/bin/env python3
"""
remove_translation.py — Safely removes a Bible translation from bible.sqlite.

Features:
  - Deletes translation metadata, verses, words, edits, and cached verse text.
  - Cleans up corresponding entries in full-text search (verses_fts).
  - Offers a --list option to inspect installed translations and row counts.

Usage:
    python3 remove_translation.py --code MSB --db path/to/bible.sqlite
    python3 remove_translation.py --list --db path/to/bible.sqlite
"""

import argparse
import sqlite3
import sys
import build_db as db_builder


def list_translations(con):
    """Prints a summary of all installed translations in the database."""
    query = """
        SELECT 
            t.id, 
            t.code, 
            t.name, 
            t.language, 
            COUNT(DISTINCT v.id) AS verse_count,
            COUNT(w.id) AS word_count
        FROM translations t
        LEFT JOIN verses v ON v.translation_id = t.id
        LEFT JOIN words w ON w.verse_id = v.id
        GROUP BY t.id
        ORDER BY t.id;
    """
    rows = con.execute(query).fetchall()

    if not rows:
        print("No translations found in database.")
        return

    print(f"\n{'ID':<5} {'Code':<10} {'Language':<10} {'Verses':<10} {'Words':<10} {'Name'}")
    print("-" * 70)
    for r in rows:
        print(f"{r[0]:<5} {r[1]:<10} {r[3]:<10} {r[4]:<10} {r[5]:<10} {r[2]}")
    print()


def remove_translation(con, code):
    """Deletes all data associated with a translation code."""
    # Lookup translation ID and details
    row = con.execute(
        "SELECT id, name FROM translations WHERE code = ?", (code,)
    ).fetchone()

    if not row:
        sys.exit(f"Error: Translation code '{code}' not found in database.")

    translation_id, translation_name = row[0], row[1]

    # Count records before deletion
    verse_count = con.execute(
        "SELECT COUNT(*) FROM verses WHERE translation_id = ?", (translation_id,)
    ).fetchone()[0]

    word_count = con.execute(
        """SELECT COUNT(*) FROM words w 
           JOIN verses v ON v.id = w.verse_id 
           WHERE v.translation_id = ?""", (translation_id,)
    ).fetchone()[0]

    print(f"Removing '{translation_name}' ({code}): {verse_count} verses, {word_count} words...")

    # 1. Wipe child records (verses, words, edits, FTS entries) using build_db utility
    db_builder.wipe_translation(con, translation_id)

    # 2. Clean up verse_text cache table if present
    con.execute("DELETE FROM verse_text WHERE translation_id = ?", (translation_id,))

    # 3. Delete translation row from translations table
    con.execute("DELETE FROM translations WHERE id = ?", (translation_id,))

    print(f"Successfully deleted translation '{translation_name}' ({code}) from database.")


def main():
    parser = argparse.ArgumentParser(description="Remove a Bible translation from River SQLite DB")
    parser.add_argument('--code', help="Short translation code to remove (e.g. MSB, WEB, BYZ)")
    parser.add_argument('--list', action='store_true', help="List all installed translations and exit")
    parser.add_argument('--db', default='bible.sqlite', help="Path to SQLite database file")

    args = parser.parse_args()

    con = sqlite3.connect(args.db, timeout=30.0)
    con.execute("PRAGMA foreign_keys = ON;")

    try:
        if args.list:
            list_translations(con)
            return

        if not args.code:
            parser.print_help()
            sys.exit("\nError: Please specify a translation code to remove using --code, or use --list.")

        with con:
            remove_translation(con, args.code.upper())

    except Exception as e:
        sys.exit(f"Deletion failed: {e}")
    finally:
        con.close()


if __name__ == '__main__':
    main()