#!/usr/bin/env python3
"""
build_search_index.py — Builds flat verse text and FTS5 search indexes for River's SQLite DB (bible.sqlite).

Creates:
  1. verse_text (plain table): Stores pre-flattened verse text for instant UI rendering 
     across active translations.
  2. verses_fts (FTS5 table): External-contentless FTS5 table indexed for fast full-text matching,
     with metadata fields marked UNINDEXED for efficient filtering.

Usage:
    python3 build_search_index.py --db /path/to/bible.sqlite
"""

import argparse
import sqlite3
import sys
import time


def ensure_index_schema(con):
    """Ensures verse_text table exists and recreates verses_fts with full UNINDEXED metadata columns."""
    con.executescript("""
        -- 1. Plain flattened verse text table for UI rendering
        CREATE TABLE IF NOT EXISTS verse_text (
            translation_id INTEGER NOT NULL REFERENCES translations(id) ON DELETE CASCADE,
            book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            chapter INTEGER NOT NULL,
            verse INTEGER NOT NULL,
            text TEXT NOT NULL,
            PRIMARY KEY(translation_id, book_id, chapter, verse)
        );

        CREATE INDEX IF NOT EXISTS idx_verse_text_lookup 
        ON verse_text(translation_id, book_id, chapter, verse);

        -- 2. Force drop old verses_fts schema to allow structure updates
        DROP TABLE IF EXISTS verses_fts;

        -- Re-create FTS5 table with UNINDEXED metadata columns
        CREATE VIRTUAL TABLE verses_fts USING fts5(
            text,
            translation_id UNINDEXED,
            book_id UNINDEXED,
            book_name UNINDEXED,
            chapter UNINDEXED,
            verse UNINDEXED,
            testament UNINDEXED,
            tokenize='porter unicode61'
        );
    """)


def rebuild_search_index(con):
    """
    Flattens words (respecting edits) into verse strings and populates verse_text & verses_fts.
    """
    ensure_index_schema(con)

    print("Clearing existing verse_text table...")
    con.execute("DELETE FROM verse_text;")

    print("Fetching word-level records across all translations...")

    query = """
        SELECT 
            v.translation_id,
            v.book_id,
            b.name AS book_name,
            b.testament,
            v.chapter,
            v.verse,
            v.id AS verse_id,
            w.beg_q,
            COALESCE(e.gloss, w.gloss, w.original, '') AS display_gloss,
            w.pnc
        FROM verses v
        JOIN books b ON b.id = v.book_id
        JOIN words w ON w.verse_id = v.id
        LEFT JOIN edits e ON e.word_id = w.id
        ORDER BY v.translation_id, b.book_order, v.chapter, v.verse, w.position;
    """

    cursor = con.execute(query)

    current_verse_key = None
    current_verse_meta = None
    verse_tokens = []

    verse_text_batch = []
    fts_batch = []

    verses_processed = 0
    start_time = time.time()

    def flush_verse():
        nonlocal current_verse_key, current_verse_meta, verse_tokens, verses_processed
        if not current_verse_key or not verse_tokens:
            return

        trans_id, book_id, ch, v = current_verse_key
        book_name, testament = current_verse_meta

        flattened_text = " ".join(verse_tokens).strip()

        if flattened_text:
            verse_text_batch.append((trans_id, book_id, ch, v, flattened_text))
            fts_batch.append((flattened_text, trans_id, book_id, book_name, ch, v, testament))
            verses_processed += 1

        verse_tokens = []

    for row in cursor:
        trans_id, book_id, book_name, testament, ch, v, verse_id, beg_q, gloss, pnc = row
        verse_key = (trans_id, book_id, ch, v)

        if verse_key != current_verse_key:
            flush_verse()
            current_verse_key = verse_key
            current_verse_meta = (book_name, testament)

        token = f"{beg_q or ''}{gloss}{pnc or ''}".strip()
        if token:
            verse_tokens.append(token)

    flush_verse()

    # Batch insert into verse_text
    print(f"Inserting {len(verse_text_batch)} rows into 'verse_text'...")
    con.executemany("""
        INSERT OR REPLACE INTO verse_text(translation_id, book_id, chapter, verse, text)
        VALUES (?, ?, ?, ?, ?);
    """, verse_text_batch)

    # Batch insert into verses_fts
    print(f"Inserting {len(fts_batch)} rows into 'verses_fts'...")
    con.executemany("""
        INSERT INTO verses_fts(text, translation_id, book_id, book_name, chapter, verse, testament)
        VALUES (?, ?, ?, ?, ?, ?, ?);
    """, fts_batch)

    elapsed = time.time() - start_time
    print(f"Search index rebuild completed successfully in {elapsed:.2f} seconds ({verses_processed} verses indexed).")


def main():
    parser = argparse.ArgumentParser(description="Build River search index tables (verse_text & verses_fts)")
    parser.add_argument('--db', default='bible.sqlite', help="Path to SQLite database file")
    args = parser.parse_args()

    con = sqlite3.connect(args.db, timeout=30.0)
    con.execute("PRAGMA foreign_keys = ON;")

    try:
        with con:
            rebuild_search_index(con)
    except Exception as e:
        sys.exit(f"Index build failed: {e}")
    finally:
        con.close()


if __name__ == '__main__':
    main()