#!/usr/bin/env python3
"""
build_db.py — Core database schema initialization and wipe utilities for River's SQLite database (bible.sqlite).

Includes core translation tables, normalized lexicon schema, and baseline book initializations.
Search indexes (verses_fts, verse_text) are populated at the end via build_search_index.py.
"""

import sqlite3

BOOKS = [
    'Genesis','Exodus','Leviticus','Numbers','Deuteronomy','Joshua','Judges','Ruth',
    '1 Samuel','2 Samuel','1 Kings','2 Kings','1 Chronicles','2 Chronicles','Ezra','Nehemiah',
    'Esther','Job','Psalms','Proverbs','Ecclesiastes','Song of Solomon','Isaiah','Jeremiah',
    'Lamentations','Ezekiel','Daniel','Hosea','Joel','Amos','Obadiah','Jonah','Micah','Nahum',
    'Habakkuk','Zephaniah','Haggai','Zechariah','Malachi',
    'Matthew','Mark','Luke','John','Acts','Romans','1 Corinthians','2 Corinthians','Galatians',
    'Ephesians','Philippians','Colossians','1 Thessalonians','2 Thessalonians','1 Timothy',
    '2 Timothy','Titus','Philemon','Hebrews','James','1 Peter','2 Peter','1 John','2 John',
    '3 John','Jude','Revelation'
]


def testament_for(book_name):
    """Returns 'OT' for Old Testament books, 'NT' for New Testament, and 'AP' for others."""
    try:
        idx = BOOKS.index(book_name)
        return 'OT' if idx < 39 else 'NT'
    except ValueError:
        return 'AP'


SCHEMA = """
-- 1. Core Translation Tables
CREATE TABLE IF NOT EXISTS translations(
    id INTEGER PRIMARY KEY,
    code TEXT UNIQUE NOT NULL,
    name TEXT NOT NULL,
    language TEXT NOT NULL DEFAULT 'English'
);

CREATE TABLE IF NOT EXISTS books(
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    book_code TEXT,
    testament TEXT NOT NULL CHECK(testament IN ('OT','NT','AP')),
    book_order INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS verses(
    id INTEGER PRIMARY KEY,
    translation_id INTEGER NOT NULL REFERENCES translations(id) ON DELETE CASCADE,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    chapter INTEGER NOT NULL,
    verse INTEGER NOT NULL,
    UNIQUE(translation_id, book_id, chapter, verse)
);
CREATE INDEX IF NOT EXISTS idx_verses_lookup ON verses(translation_id, book_id, chapter, verse);

CREATE TABLE IF NOT EXISTS words(
    id INTEGER PRIMARY KEY,
    verse_id INTEGER NOT NULL REFERENCES verses(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    lang TEXT,
    original TEXT,
    translit TEXT,
    parsing TEXT,
    strong TEXT,
    lemma TEXT,
    gloss TEXT,
    beg_q TEXT,
    pnc TEXT,
    end_q TEXT,
    end_text TEXT,
    footnote TEXT,
    hdg TEXT,
    hdg_type TEXT,
    crossref TEXT,
    par_classes TEXT
);
CREATE INDEX IF NOT EXISTS idx_words_verse ON words(verse_id, position);
CREATE INDEX IF NOT EXISTS idx_words_strong ON words(strong) WHERE strong IS NOT NULL;

CREATE TABLE IF NOT EXISTS edits(
    word_id INTEGER PRIMARY KEY REFERENCES words(id) ON DELETE CASCADE,
    gloss TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 2. Lexicon & Dictionary Tables
CREATE TABLE IF NOT EXISTS lexicon_words (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strong TEXT UNIQUE NOT NULL,      -- e.g., 'G3972', 'H7257'
    lang TEXT NOT NULL,               -- 'Greek' or 'Hebrew'
    lexeme TEXT NOT NULL,             -- Lemma / Original dictionary word
    translit TEXT,                    -- Transliteration
    pronunciation TEXT,               -- Phonetic pronunciation guide
    short_gloss TEXT                  -- Brief summary definition
);
CREATE INDEX IF NOT EXISTS idx_lexicon_strong ON lexicon_words(strong);

CREATE TABLE IF NOT EXISTS lexicon_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT UNIQUE NOT NULL,        -- e.g., 'THAYER', 'BDB', 'DODSON', 'LSJ'
    name TEXT NOT NULL,               -- e.g., "Thayer's Greek Lexicon"
    copyright TEXT                    -- Public domain or licensing info
);

CREATE TABLE IF NOT EXISTS lexicon_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strong_id INTEGER NOT NULL REFERENCES lexicon_words(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES lexicon_sources(id) ON DELETE CASCADE,
    entry_html TEXT NOT NULL,         -- HTML formatted entry body for popover UI
    UNIQUE(strong_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_lexicon_entries ON lexicon_entries(strong_id, source_id);
"""


def ensure_schema(con):
    """Initializes tables and seeds initial books if empty."""
    con.executescript(SCHEMA)

    # Migration: add 'lemma' column to pre-existing 'words' tables that predate it.
    existing_cols = {row[1] for row in con.execute('PRAGMA table_info(words)').fetchall()}
    if 'lemma' not in existing_cols:
        con.execute('ALTER TABLE words ADD COLUMN lemma TEXT')

    # Migration: add 'hdg_type' to pre-existing 'words' tables that predate it.
    # Distinguishes a psalm superscription/descriptive title (USFM \d, MT
    # style — 'psalm') from an ordinary editorial section heading (\s/\ms/\qa
    # — NULL) stored in the existing 'hdg' column, so the frontend can style
    # the two differently (see index.html's w.hdgType / .psalm-hdg). Set by
    # import_usfm.py; NULL for words imported before this existed, which
    # just renders as a plain section heading rather than breaking anything.
    if 'hdg_type' not in existing_cols:
        con.execute('ALTER TABLE words ADD COLUMN hdg_type TEXT')

    n = con.execute('SELECT count(*) FROM books').fetchone()[0]
    if n == 0:
        con.executemany(
            'INSERT INTO books(id, name, testament, book_order) VALUES (?,?,?,?)',
            [(i + 1, b, testament_for(b), i) for i, b in enumerate(BOOKS)]
        )


def wipe_translation(con, translation_id):
    """Cleanly wipes edits, words, verse_text, and verses for a specific translation."""
    # Drop legacy FTS trigger if present
    con.execute("DROP TRIGGER IF EXISTS trg_verses_fts_delete;")

    # 1. Delete associated edits
    con.execute(
        """DELETE FROM edits WHERE word_id IN (
            SELECT words.id FROM words 
            JOIN verses ON verses.id = words.verse_id 
            WHERE verses.translation_id=?
        )""",
        (translation_id,)
    )

    # 2. Delete associated words
    con.execute(
        "DELETE FROM words WHERE verse_id IN (SELECT id FROM verses WHERE translation_id=?)",
        (translation_id,)
    )

    # 3. Delete verses
    con.execute("DELETE FROM verses WHERE translation_id=?", (translation_id,))


def main():
    con = sqlite3.connect("bible.sqlite")
    con.execute("PRAGMA foreign_keys = ON")
    try:
        ensure_schema(con)
        print("Schema (including lexicon structures) initialized in bible.sqlite.")
    finally:
        con.close()


if __name__ == '__main__':
    main()