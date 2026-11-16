#!/usr/bin/env python3
"""
build_search_index.py — Builds flat verse text and FTS5 search indexes for River's SQLite DB (bible.sqlite).

Creates:
  1. verse_text (plain table): Stores pre-flattened verse text for instant UI rendering 
     across active translations.
  2. verses_fts (FTS5 table): Full-text index with three searchable columns plus UNINDEXED
     metadata:
       text    - the verse's glosses/text, diacritics stripped
       orig    - the original-language words (Greek/Hebrew/...), diacritics stripped
       orig_x  - the same original-language words with every diacritic kept, each combining
                 mark encoded as a private-use letter so FTS5 doesn't split words at them
                 (used only by the "diacritics:keep" search option)

Diacritics: fold_text() and exact_text() below MUST stay in sync with foldText() and
exactText() in index.html, which normalize search terms the same way.

Usage:
    python3 build_search_index.py --db /path/to/bible.sqlite
"""

import argparse
import sqlite3
import sys
import time
import unicodedata

# Interlinear elision markers from the Berean TSV importer (see index.html's
# placeholder-dot handling); never part of the readable verse text.
PLACEHOLDER_GLOSSES = {'-', 'vvv', '. . .'}


def fold_text(s):
    """Lowercase and strip every combining mark (Greek accents/breathings/iota subscript,
    Hebrew niqqud/cantillation, Latin accents, ...). Final sigma becomes plain sigma."""
    s = unicodedata.normalize('NFD', s.lower())
    s = ''.join(c for c in s if not unicodedata.category(c).startswith('M'))
    return s.replace('\u03c2', '\u03c3')


def exact_text(s):
    """Like fold_text but keeps the marks, each re-encoded as a private-use character
    (U+E000 + codepoint - U+0300). FTS5's unicode61 tokenizer treats private-use characters
    as word characters, whereas real combining marks (esp. Hebrew points) would split words.
    Marks outside U+0300-U+06FF are dropped."""
    out = []
    for c in unicodedata.normalize('NFD', s.lower()):
        if unicodedata.category(c).startswith('M'):
            cp = ord(c)
            if 0x300 <= cp < 0x700:
                out.append(chr(0xE000 + cp - 0x300))
        else:
            out.append(c)
    return ''.join(out).replace('\u03c2', '\u03c3')


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
            orig,
            orig_x,
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
            w.pnc,
            w.original
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
    orig_tokens = []
    pending_prefix = ''

    verse_text_batch = []
    fts_batch = []

    verses_processed = 0
    start_time = time.time()

    def flush_verse():
        nonlocal current_verse_key, current_verse_meta, verse_tokens, orig_tokens, verses_processed, pending_prefix
        pending_prefix = ''
        if not current_verse_key or not verse_tokens:
            orig_tokens = []
            return

        trans_id, book_id, ch, v = current_verse_key
        book_name, testament = current_verse_meta

        flattened_text = " ".join(verse_tokens).strip()

        if flattened_text:
            # verse_text keeps the display text untouched (diacritics and all);
            # only the FTS copies are folded.
            orig_text = " ".join(orig_tokens)
            verse_text_batch.append((trans_id, book_id, ch, v, flattened_text))
            fts_batch.append((fold_text(flattened_text), fold_text(orig_text), exact_text(orig_text),
                              trans_id, book_id, book_name, ch, v, testament))
            verses_processed += 1

        verse_tokens = []
        orig_tokens = []

    for row in cursor:
        trans_id, book_id, book_name, testament, ch, v, verse_id, beg_q, gloss, pnc, original = row
        verse_key = (trans_id, book_id, ch, v)

        if verse_key != current_verse_key:
            flush_verse()
            current_verse_key = verse_key
            current_verse_meta = (book_name, testament)

        # Berean TSV elision placeholders carry no readable text: drop the gloss
        # but keep any punctuation, attached to the previous word (or carried
        # forward to the next one if the verse opens with a placeholder).
        gloss = (gloss or '').strip()
        if gloss in PLACEHOLDER_GLOSSES:
            gloss = ''
        # Strip the punctuation fields too: Berean's carry their own spacing,
        # which would otherwise surface as "word ," once tokens are space-joined.
        beg_q = (beg_q or '').strip()
        pnc = (pnc or '').strip()
        if gloss:
            token = f"{pending_prefix}{beg_q}{gloss}{pnc}"
            pending_prefix = ''
            verse_tokens.append(token)
        elif beg_q or pnc:
            if verse_tokens:
                verse_tokens[-1] += beg_q + pnc
            else:
                pending_prefix += beg_q + pnc
        if original and original.strip():
            orig_tokens.append(original.strip())

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
        INSERT INTO verses_fts(text, orig, orig_x, translation_id, book_id, book_name, chapter, verse, testament)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
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