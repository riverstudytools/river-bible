#!/usr/bin/env python3
"""
import_strongs_greek.py

Parses the OpenScriptures Strong's Greek Dictionary XML (derived from
James Strong's 1890 public-domain dictionary; XML e-text CC BY-SA by
Ulrik Petersen / Open Scriptures) and imports/upserts it into
bible.sqlite, matching the lexicon_words, lexicon_sources, and
lexicon_entries schema used by the other lexicon importers.

Source data:
    https://github.com/openscriptures/strongs
    greek/StrongsGreekDictionaryXML_1.4/strongsgreek.xml

Usage:
    python3 import_strongs_greek.py path/to/strongsgreek.xml bible.sqlite
"""

import argparse
import html
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Schema & Source Setup
# ---------------------------------------------------------------------------

STRONGS_GREEK_SOURCE = (
    "GSTR",
    "Strong's Greek Dictionary (1890)",
    "Public domain (1890 text); XML e-text released under CC0 1.0 (morphgnt/strongs-dictionary-xml)",
)


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


def ensure_source(conn):
    """Upserts the Strong's Greek source entry and returns its source_id."""
    code, name, copyright_ = STRONGS_GREEK_SOURCE
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

def esc(text):
    return html.escape(text or "", quote=False)


def serialize_mixed(elem, skip_tags=()):
    """Renders an entry's mixed content (#PCDATA + greek/latin/strongsref/
    pronunciation children) into a small HTML fragment."""
    parts = []
    if elem.text:
        parts.append(esc(elem.text))

    for child in elem:
        tag = child.tag
        if tag in skip_tags:
            pass
        elif tag == "greek":
            unicode_form = child.attrib.get("unicode", "")
            translit = child.attrib.get("translit", "")
            parts.append(f'<span class="lex-greek" title="{esc(translit)}">{esc(unicode_form)}</span>')
        elif tag == "latin":
            parts.append(f"<em>{esc(child.text)}</em>")
        elif tag == "pronunciation":
            pron = child.attrib.get("strongs", "")
            parts.append(f'<span class="lex-pron">[{esc(pron)}]</span>')
        elif tag == "strongsref":
            lang = child.attrib.get("language", "")
            num = child.attrib.get("strongs", "")
            prefix = "G" if lang.upper() == "GREEK" else ("H" if lang.upper() == "HEBREW" else "")
            parts.append(f'<span class="lex-xref" data-strong="{prefix}{esc(num)}">{prefix}{esc(num)}</span>')
        elif tag == "see":
            lang = child.attrib.get("language", "")
            num = child.attrib.get("strongs", "")
            prefix = "G" if lang.upper() == "GREEK" else ("H" if lang.upper() == "HEBREW" else "")
            parts.append(f'<span class="lex-see" data-strong="{prefix}{esc(num)}">see {prefix}{esc(num)}</span>')
        else:
            # Unknown/unexpected element: fall back to its text content only.
            if child.text:
                parts.append(esc(child.text))

        if child.tail:
            parts.append(esc(child.tail))

    return "".join(parts).strip()


def extract_short_gloss(kjv_def_text, strongs_def_text):
    """Derives a short display gloss from the KJV usage note, falling back
    to the first clause of the fuller definition. Strong's KJV usage notes
    prefix a word with a bare 'X' when it's folded into another word's
    translation rather than rendered on its own (e.g. 'X exceeding'); such
    tokens are skipped in favor of the next usable one."""
    candidate = re.sub(r"^\s*:?--?\s*", "", kjv_def_text or "").strip()
    if candidate:
        candidate = re.sub(r"\([^)]*\)", "", candidate)
        for part in re.split(r"[;,]", candidate):
            part = part.strip(" .")
            part = re.sub(r"^X\s+", "", part).strip()
            if part:
                return part

    fallback = (strongs_def_text or "").strip()
    first = re.split(r"[;,.]", fallback)[0].strip()
    return first[:80]


def extract_entry(entry_elem):
    strong_num = entry_elem.attrib.get("strongs")
    if not strong_num:
        return None
    strong = f"G{int(strong_num)}"

    greek_el = entry_elem.find("greek")
    lexeme = greek_el.attrib.get("unicode", "") if greek_el is not None else ""
    translit = greek_el.attrib.get("translit", "") if greek_el is not None else ""

    pron_el = entry_elem.find("pronunciation")
    pronunciation = pron_el.attrib.get("strongs", "") if pron_el is not None else ""

    derivation_el = entry_elem.find("strongs_derivation")
    def_el = entry_elem.find("strongs_def")
    kjv_el = entry_elem.find("kjv_def")

    derivation_html = serialize_mixed(derivation_el) if derivation_el is not None else ""
    def_html = serialize_mixed(def_el) if def_el is not None else ""
    kjv_html = serialize_mixed(kjv_el) if kjv_el is not None else ""

    def_text_plain = "".join(def_el.itertext()) if def_el is not None else ""
    kjv_text_plain = "".join(kjv_el.itertext()) if kjv_el is not None else ""
    short_gloss = extract_short_gloss(kjv_text_plain, def_text_plain)

    entry_html_parts = []
    if pronunciation:
        entry_html_parts.append(f'<p class="lex-pron-line">Pronunciation: {esc(pronunciation)}</p>')
    if derivation_html:
        entry_html_parts.append(f'<p class="lex-derivation">{derivation_html}</p>')
    if def_html:
        entry_html_parts.append(f'<p class="lex-def">{def_html}</p>')
    if kjv_html:
        entry_html_parts.append(f'<p class="lex-kjv">KJV usage: {kjv_html}</p>')

    for see_el in entry_elem.findall("see"):
        lang = see_el.attrib.get("language", "")
        num = see_el.attrib.get("strongs", "")
        prefix = "G" if lang.upper() == "GREEK" else ("H" if lang.upper() == "HEBREW" else "")
        entry_html_parts.append(f'<p class="lex-see"><span class="lex-see" data-strong="{prefix}{esc(num)}">See {prefix}{esc(num)}</span></p>')

    if not entry_html_parts:
        entry_html_parts.append(f"<p>{esc(''.join(entry_elem.itertext()))}</p>")

    return strong, lexeme, translit, pronunciation, short_gloss, "".join(entry_html_parts)


# ---------------------------------------------------------------------------
# Database Upserts
# ---------------------------------------------------------------------------

def upsert_word(conn, strong, lexeme, translit, pronunciation, short_gloss):
    conn.execute(
        """
        INSERT INTO lexicon_words (strong, lang, lexeme, translit, pronunciation, short_gloss)
        VALUES (?, 'Greek', ?, ?, ?, ?)
        ON CONFLICT(strong) DO UPDATE SET
            lexeme = CASE WHEN excluded.lexeme != '' THEN excluded.lexeme ELSE lexicon_words.lexeme END,
            translit = CASE WHEN excluded.translit != '' THEN excluded.translit ELSE lexicon_words.translit END,
            pronunciation = CASE WHEN excluded.pronunciation != '' THEN excluded.pronunciation ELSE lexicon_words.pronunciation END,
            short_gloss = CASE WHEN excluded.short_gloss != '' THEN excluded.short_gloss ELSE lexicon_words.short_gloss END
        """,
        (strong, lexeme, translit, pronunciation, short_gloss),
    )
    row = conn.execute("SELECT id FROM lexicon_words WHERE strong = ?", (strong,)).fetchone()
    return row[0]


def upsert_entry(conn, word_id, source_id, entry_html):
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
        source_id = ensure_source(conn)

        tree = ET.parse(xml_path)
        root = tree.getroot()
        entries_container = root.find("entries")
        entries = entries_container.findall("entry") if entries_container is not None else root.findall(".//entry")

        words_count = 0
        skipped = 0

        for entry in entries:
            parsed = extract_entry(entry)
            if not parsed:
                skipped += 1
                continue

            strong, lexeme, translit, pronunciation, short_gloss, entry_html = parsed
            word_id = upsert_word(conn, strong, lexeme, translit, pronunciation, short_gloss)
            upsert_entry(conn, word_id, source_id, entry_html)
            words_count += 1

        conn.commit()
        print(f"Successfully processed {xml_path}:")
        print(f"  - Lexicon Words Upserted: {words_count}")
        print(f"  - Strong's Greek Entries Upserted: {words_count}")
        if skipped:
            print(f"  - Entries skipped (no strongs number): {skipped}")

    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import OpenScriptures Strong's Greek XML into bible.sqlite")
    parser.add_argument("xml_path", help="Path to strongsgreek.xml")
    parser.add_argument("db_path", nargs="?", default="bible.sqlite", help="Path to SQLite database")

    args = parser.parse_args()
    parse_and_import(args.xml_path, args.db_path)
