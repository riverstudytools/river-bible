#!/usr/bin/env python3
"""
import_lexicon_copyright.py

Imports or updates copyright/license text for a specific lexicon 
in the lexicon_sources table of bible.sqlite.
"""

import argparse
import os
import sqlite3
import sys


def ensure_sources_table(conn: sqlite3.Connection):
    """Ensures that the lexicon_sources table exists."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lexicon_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            copyright TEXT
        );
    """)
    conn.commit()


def get_lexicon_sources(conn: sqlite3.Connection):
    """Fetches all registered lexicon sources."""
    cursor = conn.execute("SELECT id, code, name, copyright FROM lexicon_sources ORDER BY code ASC")
    return cursor.fetchall()


def update_lexicon_copyright(conn: sqlite3.Connection, source_code: str, copyright_text: str):
    """Updates the copyright field for a given lexicon source code."""
    cursor = conn.execute(
        """
        UPDATE lexicon_sources
        SET copyright = ?
        WHERE code = ?
        """,
        (copyright_text, source_code),
    )
    conn.commit()
    return cursor.rowcount > 0


def select_source_interactively(sources):
    """Displays available lexicon sources and prompts the user to select one."""
    print("\n--- Available Lexicon Sources ---")
    for idx, (s_id, code, name, copyright_val) in enumerate(sources, 1):
        has_cp = "Yes" if copyright_val else "No"
        print(f"[{idx}] Code: {code:<8} | Name: {name:<35} | Has Copyright: {has_cp}")
    print("---------------------------------")

    while True:
        choice = input("Select a lexicon source by number or code: ").strip()
        if not choice:
            continue
        
        # Check by numeric index selection
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(sources):
                return sources[idx][1]

        # Check by code directly
        for _, code, _, _ in sources:
            if choice.upper() == code.upper():
                return code

        print("Invalid choice. Please enter a valid number or source code.")


def main():
    parser = argparse.ArgumentParser(
        description="Import copyright/license text into lexicon_sources table."
    )
    parser.add_argument("txt_path", help="Path to the text file containing copyright/license text.")
    parser.add_argument(
        "--db", default="bible.sqlite", help="Path to SQLite database (default: bible.sqlite)"
    )
    parser.add_argument(
        "--code", help="Lexicon source code (e.g., 'AS'). If omitted, an interactive menu is shown."
    )

    args = parser.parse_args()

    if not os.path.exists(args.txt_path):
        print(f"Error: File not found: {args.txt_path}", file=sys.stderr)
        sys.exit(1)

    if not os.path.exists(args.db):
        print(f"Error: Database not found: {args.db}", file=sys.stderr)
        sys.exit(1)

    with open(args.txt_path, "r", encoding="utf-8") as f:
        copyright_text = f.read().strip()

    if not copyright_text:
        print("Error: Copyright text file is empty.", file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db)
    try:
        ensure_sources_table(conn)
        sources = get_lexicon_sources(conn)

        if not sources:
            print("Error: No entries found in 'lexicon_sources'. Run lexicon importers first.", file=sys.stderr)
            sys.exit(1)

        source_code = args.code
        if not source_code:
            source_code = select_source_interactively(sources)
        else:
            source_code = source_code.upper()

        success = update_lexicon_copyright(conn, source_code, copyright_text)
        if success:
            print(f"\nSuccessfully updated copyright information for lexicon source '{source_code}'.")
        else:
            print(f"\nError: Source code '{source_code}' not found in lexicon_sources.", file=sys.stderr)
            sys.exit(1)

    finally:
        conn.close()


if __name__ == "__main__":
    main()