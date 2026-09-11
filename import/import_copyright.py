#!/usr/bin/env python3
"""
import_copyright.py — Standalone tool to import copyright and license text 
into a specific translation in River's SQLite DB (bible.sqlite).

Usage:
    python3 import_copyright.py --code LXX_SWETE --info-file path/to/copyright.txt
"""

import argparse
import os
import sqlite3
import sys


def parse_info_file(info_path):
    """
    Parses a copyright text file.
    The text before the first double newline (\n\n) is copyright_short.
    The remainder of the text is license_text.
    """
    if not os.path.exists(info_path):
        sys.exit(f"Error: Info file not found at path '{info_path}'")

    with open(info_path, 'r', encoding='utf-8') as f:
        content = f.read().strip()

    if not content:
        sys.exit(f"Error: Info file '{info_path}' is empty.")

    # Normalize line breaks to \n before splitting on double newlines
    content = content.replace('\r\n', '\n')
    parts = content.split('\n\n', 1)

    copyright_short = parts[0].strip() if parts[0].strip() else None
    license_text = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None

    return copyright_short, license_text


def ensure_schema_columns(con):
    """Ensures copyright_short and license_text columns exist on the translations table."""
    cur = con.execute("PRAGMA table_info(translations)")
    columns = [row[1] for row in cur.fetchall()]

    if 'copyright_short' not in columns:
        con.execute("ALTER TABLE translations ADD COLUMN copyright_short TEXT")
    
    if 'license_text' not in columns:
        con.execute("ALTER TABLE translations ADD COLUMN license_text TEXT")


def update_copyright(db_path, code, info_path):
    copyright_short, license_text = parse_info_file(info_path)

    if not os.path.exists(db_path):
        sys.exit(f"Error: Database file not found at '{db_path}'")

    con = sqlite3.connect(db_path, timeout=20.0)
    con.execute('PRAGMA foreign_keys = ON')

    try:
        with con:
            # Auto-migrate schema if columns are missing
            ensure_schema_columns(con)

            cur = con.execute(
                '''UPDATE translations 
                   SET copyright_short = ?, license_text = ? 
                   WHERE code = ?''',
                (copyright_short, license_text, code)
            )

            if cur.rowcount == 0:
                sys.exit(f"Error: No translation found with code '{code}'.")

        print(f"Successfully updated copyright info for '{code}' in {db_path}.")

    except Exception as e:
        sys.exit(f"Database update failed: {e}")
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser(
        description="Import copyright/license details into a translation by code."
    )
    ap.add_argument('--code', '-c', required=True, help='Translation code/abbreviation (e.g., LXX_SWETE)')
    ap.add_argument('--info-file', '-i', required=True, help='Path to copyright txt file')
    ap.add_argument('--db', default='bible.sqlite', help='SQLite DB path (default: bible.sqlite)')

    args = ap.parse_args()
    update_copyright(args.db, args.code, args.info_file)


if __name__ == '__main__':
    main()