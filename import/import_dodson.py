#!/usr/bin/env python3
"""
import_dodson.py

Parses Jeffrey Dodson's Greek Lexicon XML (biblicalhumanities/Dodson-Greek-Lexicon,
dodson.xml) and imports/upserts entries into bible.sqlite, matching the
lexicon_words, lexicon_sources, and lexicon_entries schema.

Source format (TEI/OSIS-flavored, not namespaced the same as Abbott-Smith):

    <entry n="ἄλφα | 0001">
        <orth>ἄλφα</orth>
        <def role="brief">the first letter of the Greek alphabet</def>
        <def role="full">alpha; the first letter of the Greek alphabet.</def>
    </entry>

The numeric id after the "|" in @n is Dodson's sequence number, which is
identical to the standard Strong's Greek numbering (0001 -> G1, 5624 -> G5624).
Leading zeros are stripped and a "G" prefix is added to match the strong
column convention used by the other Greek lexicon importers.
"""

import argparse
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Schema & Source Setup
# ---------------------------------------------------------------------------

DODSON_SOURCE = ("DODSON", "Dodson Greek Lexicon", "Public domain")


def ensure_schema(conn):
    """Creates the three lexicon tables if they don't already exist."""
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


def ensure_dodson_source(conn):
    """Upserts the Dodson source entry and returns its source_id."""
    code, name, copyright_ = DODSON_SOURCE
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
    row = conn.execute("SELECT id FROM lexicon_sources WHERE code = ?", (code,)).fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# XML Parsing Helpers
# ---------------------------------------------------------------------------

def clean_tag_name(tag):
    """Strips XML namespace prefixes like '{http://www.crosswire.org/...}entry' -> 'entry'."""
    return tag.split("}")[-1] if "}" in tag else tag


def get_element_text(elem):
    """Recursively extracts text inside an element, normalizing whitespace."""
    if elem is None:
        return ""
    text = "".join(elem.itertext())
    return re.sub(r"\s+", " ", text).strip()


def extract_strong_number(entry_elem):
    """Extracts the Strong's number from the @n attribute, e.g. 'ἄλφα | 0001' -> 'G1'."""
    n_attr = entry_elem.attrib.get("n", "")
    match = re.search(r"\|\s*(\d+)\s*$", n_attr)
    if match:
        return f"G{int(match.group(1))}"

    # Fallback: any bare number in the attribute
    match = re.search(r"\b(\d+)\b", n_attr)
    if match:
        return f"G{int(match.group(1))}"

    return None


def extract_entry_details(entry_elem):
    """Extracts Strong's key, lexeme, short gloss, and entry HTML."""
    strong = extract_strong_number(entry_elem)
    if not strong:
        return None

    lexeme = ""
    short_gloss = ""
    full_def = ""

    orth_elem = entry_elem.find("orth")
    if orth_elem is None:
        for child in entry_elem:
            if clean_tag_name(child.tag) == "orth":
                orth_elem = child
                break
    if orth_elem is not None:
        lexeme = get_element_text(orth_elem)

    for child in entry_elem:
        if clean_tag_name(child.tag) != "def":
            continue
        role = child.attrib.get("role", "")
        text = get_element_text(child)
        if role == "brief":
            short_gloss = text
        elif role == "full":
            full_def = text

    # Fallback: use lexeme's leading orthography chunk if no explicit orth found
    if not lexeme:
        n_attr = entry_elem.attrib.get("n", "")
        lexeme = n_attr.split("|")[0].strip()

    entry_html_parts = [f'<p class="lex-form"><strong>{lexeme}</strong></p>']
    if full_def:
        entry_html_parts.append(f'<p class="lex-def">{full_def}</p>')
    elif short_gloss:
        entry_html_parts.append(f'<p class="lex-def">{short_gloss}</p>')

    entry_html = "".join(entry_html_parts)
    return strong, lexeme, short_gloss, entry_html


# ---------------------------------------------------------------------------
# Database Upserts
# ---------------------------------------------------------------------------

def upsert_word(conn, strong, lexeme, short_gloss):
    """Inserts or updates lexicon_words."""
    conn.execute(
        """
        INSERT INTO lexicon_words (strong, lang, lexeme, short_gloss)
        VALUES (?, 'Greek', ?, ?)
        ON CONFLICT(strong) DO UPDATE SET
            lexeme = CASE WHEN excluded.lexeme != '' THEN excluded.lexeme ELSE lexicon_words.lexeme END,
            short_gloss = CASE WHEN excluded.short_gloss != '' THEN excluded.short_gloss ELSE lexicon_words.short_gloss END
        """,
        (strong, lexeme, short_gloss),
    )
    row = conn.execute("SELECT id FROM lexicon_words WHERE strong = ?", (strong,)).fetchone()
    return row[0]


def upsert_entry(conn, word_id, source_id, entry_html):
    """Inserts or updates lexicon_entries."""
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
# Importer Driver
# ---------------------------------------------------------------------------

def parse_and_import(xml_path, db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        ensure_schema(conn)
        source_id = ensure_dodson_source(conn)

        with open(xml_path, "r", encoding="utf-8") as f:
            xml_content = f.read()

        if not xml_content.strip().startswith("<?xml") and not xml_content.strip().startswith("<root>"):
            xml_content = f"<root>{xml_content}</root>"

        root = ET.fromstring(xml_content)

        # Namespace-agnostic search for all <entry> tags
        entries = [elem for elem in root.iter() if clean_tag_name(elem.tag) == "entry"]

        words_count = 0
        entries_count = 0
        skipped = 0

        for entry in entries:
            parsed = extract_entry_details(entry)
            if not parsed:
                skipped += 1
                continue

            strong, lexeme, short_gloss, entry_html = parsed
            word_id = upsert_word(conn, strong, lexeme, short_gloss)
            upsert_entry(conn, word_id, source_id, entry_html)

            words_count += 1
            entries_count += 1

        conn.commit()
        print(f"Successfully processed {xml_path}:")
        print(f"  - Lexicon Words Upserted: {words_count}")
        print(f"  - Dodson Lexicon Entries Upserted: {entries_count}")
        if skipped:
            print(f"  - Skipped (no Strong's number found): {skipped}")

    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import Dodson Greek Lexicon XML into bible.sqlite")
    parser.add_argument("xml_path", help="Path to dodson.xml")
    parser.add_argument("db_path", nargs="?", default="bible.sqlite", help="Path to SQLite database")

    args = parser.parse_args()
    parse_and_import(args.xml_path, args.db_path)
