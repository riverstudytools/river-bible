#!/usr/bin/env python3
"""
import_lexicon_sample.py

A worked example of how a real lexicon import into River's bible.sqlite
should work, using the SAMPLE_LEXICON_DATA below as stand-ins for whatever
a real source (Thayer's, BDB, Dodson, LSJ, ...) actually looks like once
parsed.

This is meant as a template for build_db.py's eventual lexicon import
step, not a one-off migration — run it as many times as you like. Every
write is an upsert keyed on the natural identity of the row (a lemma's
Strong's number; a source's code; an (word, source) pair), so re-running
with corrected or expanded data updates existing rows instead of
duplicating them.

Usage:
    python3 import_lexicon_sample.py [path/to/bible.sqlite]
"""

import argparse
import html
import sqlite3
import sys


# ---------------------------------------------------------------------------
# Stand-in for real dictionary data. A real import would replace this with
# a parser for whatever format the source dictionary ships in (XML, JSON,
# a scanned/OCR'd text file, etc.) that yields dicts shaped like this.
# ---------------------------------------------------------------------------
SAMPLE_LEXICON_DATA = [
    {
        "strongs": "G3056",
        "lemma": "\u03bb\u03cc\u03b3\u03bf\u03c2",
        "translit": "logos",
        "pronunciation": "log'-os",
        "part_of_speech": "Noun, Masculine",
        "short_def": "a word (as embodying an idea), a statement, a divine expression",
        "strongs_def": (
            "From G3004 (\u03bb\u03ad\u03b3\u03c9); something said (including the "
            "thought); by implication a topic (matter, reasoning) or motive; "
            "computation; specifically (with the article) the Divine Expression "
            "(i.e. Christ)."
        ),
        "thayer_def": (
            "1. A word, uttered by a live voice, which embodies a conception or idea.\n"
            "2. What someone has said: a saying, decree, mandate, or message.\n"
            "3. Discourse, speech, or context of thought.\n"
            "4. In Johannine theology: The Divine Word, the second person of the "
            "Trinity, incarnate in Jesus Christ (John 1:1, John 1:14)."
        ),
    },
    {
        "strongs": "G26",
        "lemma": "\u1f00\u03b3\u03ac\u03c0\u03b7",
        "translit": "agape",
        "pronunciation": "ag-ah'-pay",
        "part_of_speech": "Noun, Feminine",
        "short_def": "love, goodwill, benevolence, self-giving affection",
        "strongs_def": (
            "From G25 (\u1f00\u03b3\u03b1\u03c0\u03ac\u03c9); love, i.e. affection or "
            "benevolence; specially (plural) a love-feast."
        ),
        "thayer_def": (
            "1. Brotherly love, affection, goodwill, benevolence.\n"
            "2. The ultimate, unconditional love of God toward humanity, "
            "demonstrated supremely at the cross (Rom 5:8).\n"
            "3. Plural (\u1f00\u03b3\u03ac\u03c0\u03b1\u03b9): Love-feasts, communal "
            "meals celebrated by early Christians alongside the Lord's Supper "
            "(Jude 1:12)."
        ),
    },
    {
        "strongs": "G2588",
        "lemma": "\u03ba\u03b1\u03c1\u03b4\u03af\u03b1",
        "translit": "kardia",
        "pronunciation": "kar-dee'-ah",
        "part_of_speech": "Noun, Feminine",
        "short_def": "the heart, mind, inner self, center of physical and spiritual life",
        "strongs_def": (
            "Prolonged from a primary \u03ba\u03ac\u03c1 (Latin cor, 'heart'); the "
            "heart, i.e. (figuratively) the thoughts or feelings (mind); also (by "
            "analogy) the middle."
        ),
        "thayer_def": (
            "1. Literally: The organ in the animal body which is the center of "
            "blood circulation.\n"
            "2. Figuratively: The inner center and seat of all physical, "
            "emotional, and spiritual life:\n"
            "   a) Seat of the mind, understanding, and intelligence.\n"
            "   b) Seat of the will, moral choices, and character.\n"
            "   c) Seat of deep sensibilities, desires, and passions."
        ),
    },
]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def ensure_schema(conn):
    """Creates the three lexicon tables if they don't already exist. Matches
    the schema server.js queries against exactly — see lexicon_words,
    lexicon_entries, lexicon_sources in server.js's stmts.lexiconEntry."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lexicon_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strong TEXT UNIQUE NOT NULL,
            lang TEXT NOT NULL,
            lexeme TEXT NOT NULL,
            translit TEXT,
            pronunciation TEXT,
            short_gloss TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_lexicon_strong ON lexicon_words(strong);

        CREATE TABLE IF NOT EXISTS lexicon_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            copyright TEXT
        );

        CREATE TABLE IF NOT EXISTS lexicon_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strong_id INTEGER NOT NULL REFERENCES lexicon_words(id) ON DELETE CASCADE,
            source_id INTEGER NOT NULL REFERENCES lexicon_sources(id) ON DELETE CASCADE,
            entry_html TEXT NOT NULL,
            UNIQUE(strong_id, source_id)
        );
        CREATE INDEX IF NOT EXISTS idx_lexicon_entries ON lexicon_entries(strong_id, source_id);
    """)


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

# A real import adds one row here per dictionary you actually import —
# adding a new source later never requires a schema change, just a new
# row here and entries pointing at it.
SOURCES = [
    ("STRONGS", "Strong's Definitions", "Public domain"),
    ("THAYER", "Thayer's Greek Lexicon", "Public domain"),
]


def ensure_sources(conn):
    """Upserts every known dictionary source and returns {code: source_id}."""
    for code, name, copyright_ in SOURCES:
        conn.execute(
            """
            INSERT INTO lexicon_sources (code, name, copyright)
            VALUES (?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                name = excluded.name,
                copyright = excluded.copyright
            """,
            (code, name, copyright_),
        )
    rows = conn.execute("SELECT code, id FROM lexicon_sources").fetchall()
    return {code: source_id for code, source_id in rows}


# ---------------------------------------------------------------------------
# Words (lemma-level)
# ---------------------------------------------------------------------------

def detect_lang(strong):
    """Strong's numbers are prefixed G (Greek) or H (Hebrew)."""
    return "Hebrew" if strong.upper().startswith("H") else "Greek"


def upsert_word(conn, strong, lexeme, translit, pronunciation, short_gloss):
    """Inserts or updates one lexicon_words row, keyed on strong. Returns
    its id. Safe to call repeatedly with corrected data — re-running the
    import with an updated short_gloss, for instance, just overwrites it."""
    conn.execute(
        """
        INSERT INTO lexicon_words (strong, lang, lexeme, translit, pronunciation, short_gloss)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(strong) DO UPDATE SET
            lang = excluded.lang,
            lexeme = excluded.lexeme,
            translit = excluded.translit,
            pronunciation = excluded.pronunciation,
            short_gloss = excluded.short_gloss
        """,
        (strong, detect_lang(strong), lexeme, translit, pronunciation, short_gloss),
    )
    row = conn.execute("SELECT id FROM lexicon_words WHERE strong = ?", (strong,)).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Entries (one per lemma + source)
# ---------------------------------------------------------------------------

def text_to_html(text):
    """Turns a source's plain-text definition into the minimal HTML
    lexicon_entries.entry_html expects. Real sources vary wildly in their
    raw formatting (numbered points, sub-points, italics for cross-refs,
    etc.) — this only handles the newline-separated-points shape
    SAMPLE_LEXICON_DATA uses; a real importer's text_to_html would be
    source-specific."""
    paragraphs = [line.strip() for line in text.strip().split("\n") if line.strip()]
    return "".join("<p>" + html.escape(p) + "</p>" for p in paragraphs)


def upsert_entry(conn, word_id, source_id, entry_html):
    """Inserts or updates one lexicon_entries row for a (word, source)
    pair. This is what keeps different lexicons' entries for the same
    word separate — Thayer's and Strong's rows both reference the same
    word_id but have distinct source_ids and their own entry_html."""
    conn.execute(
        """
        INSERT INTO lexicon_entries (strong_id, source_id, entry_html)
        VALUES (?, ?, ?)
        ON CONFLICT(strong_id, source_id) DO UPDATE SET
            entry_html = excluded.entry_html
        """,
        (word_id, source_id, entry_html),
    )


# ---------------------------------------------------------------------------
# Import driver
# ---------------------------------------------------------------------------

def import_lexicon_data(conn, entries):
    source_ids = ensure_sources(conn)
    words_imported = 0
    entries_imported = 0

    for item in entries:
        word_id = upsert_word(
            conn,
            strong=item["strongs"],
            lexeme=item["lemma"],
            translit=item.get("translit"),
            pronunciation=item.get("pronunciation"),
            short_gloss=item.get("short_def"),
        )
        words_imported += 1

        strongs_def = item.get("strongs_def")
        if strongs_def and strongs_def.strip():
            upsert_entry(conn, word_id, source_ids["STRONGS"], text_to_html(strongs_def))
            entries_imported += 1

        thayer_def = item.get("thayer_def")
        if thayer_def and thayer_def.strip():
            upsert_entry(conn, word_id, source_ids["THAYER"], text_to_html(thayer_def))
            entries_imported += 1

    return words_imported, entries_imported


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "db_path", nargs="?", default="bible.sqlite",
        help="Path to bible.sqlite (default: ./bible.sqlite)",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        ensure_schema(conn)
        words_imported, entries_imported = import_lexicon_data(conn, SAMPLE_LEXICON_DATA)
        conn.commit()
        print(f"Imported/updated {words_imported} lexicon_words rows "
              f"and {entries_imported} lexicon_entries rows into {args.db_path}")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
