#!/usr/bin/env python3
"""
import_abbott_smith.py

Parses Abbott-Smith TEI XML lexicon entries and imports/upserts them
into bible.sqlite matching the lexicon_words, lexicon_sources, and 
lexicon_entries schema.
"""

import argparse
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Schema & Source Setup
# ---------------------------------------------------------------------------

ABBOTT_SMITH_SOURCE = ("AS", "Abbott-Smith Manual Greek Lexicon", "Public domain")


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


def ensure_abbott_smith_source(conn):
    """Upserts the Abbott-Smith source entry and returns its source_id."""
    code, name, copyright_ = ABBOTT_SMITH_SOURCE
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
    """Strips XML namespace prefixes like '{http://www.tei-c.org/ns/1.0}entry' -> 'entry'."""
    return tag.split("}")[-1] if "}" in tag else tag


def get_element_text(elem):
    """Recursively extracts text inside an element, normalizing whitespace."""
    if elem is None:
        return ""
    text = "".join(elem.itertext())
    return re.sub(r"\s+", " ", text).strip()


def extract_strong_number(entry_elem):
    """Extracts Strong's number (e.g., G3686) from attributes or child elements."""
    # Check @n attribute (e.g. n="ὄνομα|G3686" or n="G3686")
    n_attr = entry_elem.attrib.get("n", "")
    match = re.search(r"\b([GH]\d+)\b", n_attr, re.IGNORECASE)
    if match:
        return match.group(1).upper()

    # Check @key or @id attributes
    for attr in ["key", "id", "xml:id"]:
        val = entry_elem.attrib.get(attr, "")
        match = re.search(r"\b([GH]\d+)\b", val, re.IGNORECASE)
        if match:
            return match.group(1).upper()

    # Search child nodes for ref/ref-like Strong tags
    for child in entry_elem.iter():
        for attr_val in child.attrib.values():
            match = re.search(r"\b([GH]\d+)\b", attr_val, re.IGNORECASE)
            if match:
                return match.group(1).upper()

    return None


def extract_entry_details(entry_elem):
    """Extracts Strong's key, lexeme, short gloss, and entry HTML."""
    strong = extract_strong_number(entry_elem)
    if not strong:
        return None

    lexeme = ""
    short_gloss = ""
    entry_html_parts = []

    for child in entry_elem:
        tag = clean_tag_name(child.tag)

        if tag == "form":
            # Extract main lexeme orthography if available
            for sub in child.iter():
                if clean_tag_name(sub.tag) == "orth" and sub.text:
                    lexeme = sub.text.strip()
                    break
            form_text = get_element_text(child)
            if not lexeme and form_text:
                lexeme = form_text.split(",")[0].strip()
            entry_html_parts.append(f'<p class="lex-form"><strong>{form_text}</strong></p>')

        elif tag == "etym":
            etym_text = get_element_text(child)
            entry_html_parts.append(f'<p class="lex-etym"><em>{etym_text}</em></p>')

        elif tag == "sense":
            sense_n = child.attrib.get("n", "")
            sense_text = get_element_text(child)
            
            # Use first gloss found as short_gloss
            if not short_gloss:
                for sub in child.iter():
                    if clean_tag_name(sub.tag) == "gloss" and sub.text:
                        short_gloss = sub.text.strip()
                        break
            
            prefix = f"<strong>{sense_n}</strong> " if sense_n else ""
            entry_html_parts.append(f'<p class="lex-sense">{prefix}{sense_text}</p>')

    # Fallback if XML structures weren't divided into form/sense blocks
    if not entry_html_parts:
        full_text = get_element_text(entry_elem)
        entry_html_parts.append(f"<p>{full_text}</p>")

    entry_html = "".join(entry_html_parts)
    return strong, lexeme, short_gloss, entry_html


# ---------------------------------------------------------------------------
# Database Upserts
# ---------------------------------------------------------------------------

def upsert_word(conn, strong, lexeme, short_gloss):
    """Inserts or updates lexicon_words."""
    lang = "Hebrew" if strong.upper().startswith("H") else "Greek"
    conn.execute(
        """
        INSERT INTO lexicon_words (strong, lang, lexeme, short_gloss)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(strong) DO UPDATE SET
            lang = excluded.lang,
            lexeme = CASE WHEN excluded.lexeme != '' THEN excluded.lexeme ELSE lexicon_words.lexeme END,
            short_gloss = CASE WHEN excluded.short_gloss != '' THEN excluded.short_gloss ELSE lexicon_words.short_gloss END
        """,
        (strong, lang, lexeme, short_gloss),
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
        source_id = ensure_abbott_smith_source(conn)

        with open(xml_path, "r", encoding="utf-8") as f:
            xml_content = f.read()

        # Wrap unrooted XML fragments if needed
        if not xml_content.strip().startswith("<?xml") and not xml_content.strip().startswith("<root>"):
            xml_content = f"<root>{xml_content}</root>"

        root = ET.fromstring(xml_content)

        # Namespace-agnostic search for all <entry> tags
        entries = [elem for elem in root.iter() if clean_tag_name(elem.tag) == "entry"]

        words_count = 0
        entries_count = 0

        for entry in entries:
            parsed = extract_entry_details(entry)
            if not parsed:
                continue

            strong, lexeme, short_gloss, entry_html = parsed
            word_id = upsert_word(conn, strong, lexeme, short_gloss)
            upsert_entry(conn, word_id, source_id, entry_html)

            words_count += 1
            entries_count += 1

        conn.commit()
        print(f"Successfully processed {xml_path}:")
        print(f"  - Lexicon Words Upserted: {words_count}")
        print(f"  - Abbott-Smith Lexicon Entries Upserted: {entries_count}")

    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import Abbott-Smith XML into bible.sqlite")
    parser.add_argument("xml_path", help="Path to Abbott-Smith XML file")
    parser.add_argument("db_path", nargs="?", default="bible.sqlite", help="Path to SQLite database")

    args = parser.parse_args()
    parse_and_import(args.xml_path, args.db_path)