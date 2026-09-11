#!/usr/bin/env python3
"""
import_hebrew_lexicons.py

Parses Open Scriptures Hebrew Lexicon files (HebrewStrong, BrownDriverBriggs,
LexicalIndex, PartsOfSpeech, BDBPartsOfSpeech) and imports/upserts them into
bible.sqlite following the lexicon_words, lexicon_sources, and lexicon_entries schema.
"""

import argparse
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET

# ---------------------------------------------------------------------------
# Schema & Source Setup
# ---------------------------------------------------------------------------

SOURCES = {
    "STRONGS": ("HSTR", "Strong's Hebrew Dictionary", "Public Domain"),
    "BDB": ("BDB", "Brown-Driver-Briggs Hebrew Lexicon", "Public Domain"),
}


def ensure_schema(conn):
    """Creates lexicon tables if they do not exist."""
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


def get_or_create_sources(conn):
    """Ensures source entries exist and returns a mapping of key -> source_id."""
    source_ids = {}
    for key, (code, name, copyright_) in SOURCES.items():
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
        source_ids[key] = row[0]
    return source_ids


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def clean_tag(tag):
    return tag.split("}")[-1] if "}" in tag else tag


def load_pos_mapping(xml_path):
    """Loads Part of Speech mappings from XML file into a dictionary."""
    mapping = {}
    if not os.path.exists(xml_path):
        return mapping
    try:
        tree = ET.parse(xml_path)
        root = tree.getroot()
        for pos_elem in root.findall(".//POS"):
            code = pos_elem.findtext("Code")
            name = pos_elem.findtext("Name")
            if code and name:
                mapping[code.strip()] = name.strip()
    except Exception as e:
        print(f"Warning: Could not parse {xml_path}: {e}")
    return mapping


def elem_to_html(elem, pos_map=None):
    """Converts XML element tree recursively to sanitized HTML string."""
    if elem is None:
        return ""
    
    tag = clean_tag(elem.tag)
    
    # Process attributes and text inside nodes
    if tag == "w":
        content = "".join([elem_to_html(child, pos_map) for child in elem]) if len(elem) else (elem.text or "")
        return f'<span class="hebrew">{content}</span>' + (elem.tail or "")
    
    elif tag == "def":
        content = "".join([elem_to_html(child, pos_map) for child in elem]) if len(elem) else (elem.text or "")
        return f'<em class="def">{content}</em>' + (elem.tail or "")
    
    elif tag == "pos":
        raw_pos = "".join(elem.itertext()).strip()
        expanded_pos = pos_map.get(raw_pos, raw_pos) if pos_map else raw_pos
        return f'<span class="pos" title="{raw_pos}">{expanded_pos}</span>' + (elem.tail or "")
    
    elif tag == "ref":
        content = "".join([elem_to_html(child, pos_map) for child in elem]) if len(elem) else (elem.text or "")
        r_attr = elem.attrib.get("r", "")
        return f'<span class="ref" data-ref="{r_attr}">{content}</span>' + (elem.tail or "")
    
    elif tag == "sense":
        n_attr = elem.attrib.get("n", "")
        prefix = f'<strong>{n_attr}.</strong> ' if n_attr else ""
        content = "".join([elem_to_html(child, pos_map) for child in elem])
        inner_text = (elem.text or "") + content
        return f'<div class="sense">{prefix}{inner_text}</div>' + (elem.tail or "")

    # Default fallback element renderer
    result = elem.text or ""
    for child in elem:
        result += elem_to_html(child, pos_map)
    
    if tag in ["entry", "part", "section"]:
        return f'<div class="lex-{tag}">{result}</div>' + (elem.tail or "")
    
    return result + (elem.tail or "")


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_strongs(file_path, pos_map):
    """Parses HebrewStrong.xml and returns lexicon data."""
    tree = ET.parse(file_path)
    root = tree.getroot()
    data = {}

    for entry in root.findall(".//{*}entry"):
        raw_id = entry.attrib.get("id", "")
        if not raw_id.startswith("H"):
            continue
        strong_num = f"H{int(raw_id[1:]):04d}"  # Zero-padded standard (H0001, H0002)

        w_elem = entry.find("{*}w")
        lexeme = w_elem.text if w_elem is not None and w_elem.text else ""
        translit = w_elem.attrib.get("xlit", "") if w_elem is not None else ""
        pron = w_elem.attrib.get("pron", "") if w_elem is not None else ""
        raw_pos = w_elem.attrib.get("pos", "") if w_elem is not None else ""
        pos_expanded = pos_map.get(raw_pos, raw_pos)

        source = entry.findtext("{*}source") or ""
        meaning_elem = entry.find("{*}meaning")
        def_elem = meaning_elem.find("{*}def") if meaning_elem is not None else None
        short_gloss = def_elem.text if def_elem is not None and def_elem.text else ""
        meaning = "".join(meaning_elem.itertext()) if meaning_elem is not None else ""
        usage = entry.findtext("{*}usage") or ""

        html_parts = [f'<p><strong>{lexeme}</strong> ({translit}) - <em>{pos_expanded}</em></p>']
        if source:
            html_parts.append(f'<p class="source"><strong>Derivation:</strong> {source}</p>')
        if meaning:
            html_parts.append(f'<p class="meaning"><strong>Definition:</strong> {meaning}</p>')
        if usage:
            html_parts.append(f'<p class="usage"><strong>Usage:</strong> {usage}</p>')

        entry_html = "".join(html_parts)

        data[strong_num] = {
            "lexeme": lexeme,
            "translit": translit,
            "pronunciation": pron,
            "short_gloss": short_gloss,
            "entry_html": entry_html,
        }

    return data


def parse_lexical_index(file_path):
    """Parses LexicalIndex.xml to build mapping of Strong -> list of BDB IDs."""
    tree = ET.parse(file_path)
    root = tree.getroot()
    strong_to_bdb = {}

    for entry in root.findall(".//{*}entry"):
        for xref in entry.findall("{*}xref"):
            strong_attr = xref.attrib.get("strong", "")
            bdb_attr = xref.attrib.get("bdb")
            
            if strong_attr and bdb_attr:
                # Extract only the numeric digits (e.g., "1b" -> "1", "H0005" -> "5")
                digits = re.sub(r"\D", "", strong_attr)
                
                if digits:
                    strong_num = f"H{int(digits):04d}"
                    # Append BDB ID to list so multiple references group under the same Strong's entry
                    strong_to_bdb.setdefault(strong_num, []).append(bdb_attr)

    return strong_to_bdb


def parse_bdb(file_path, pos_map):
    """Parses BrownDriverBriggs.xml and indexes entries by entry ID."""
    tree = ET.parse(file_path)
    root = tree.getroot()
    bdb_entries = {}

    for entry in root.findall(".//{*}entry"):
        entry_id = entry.attrib.get("id")
        if not entry_id:
            continue
        
        has_senses = len(entry.findall(".//{*}sense")) > 0
        html = elem_to_html(entry, pos_map)
        bdb_entries[entry_id] = {"html": html, "has_senses": has_senses}

    return bdb_entries


# ---------------------------------------------------------------------------
# Database Ingestion
# ---------------------------------------------------------------------------

def process_and_import(data_folder, db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        ensure_schema(conn)
        source_ids = get_or_create_sources(conn)

        pos_strong = load_pos_mapping(os.path.join(data_folder, "PartsOfSpeech.xml"))
        pos_bdb = load_pos_mapping(os.path.join(data_folder, "BDBPartsOfSpeech.xml"))

        print("Parsing HebrewStrong.xml...")
        strongs_data = parse_strongs(os.path.join(data_folder, "HebrewStrong.xml"), pos_strong)

        print("Parsing LexicalIndex.xml...")
        strong_to_bdb = parse_lexical_index(os.path.join(data_folder, "LexicalIndex.xml"))

        print("Parsing BrownDriverBriggs.xml...")
        bdb_entries = parse_bdb(os.path.join(data_folder, "BrownDriverBriggs.xml"), pos_bdb)

        words_inserted = 0
        entries_inserted = 0

        print("Importing into database...")
        for strong_num, sdata in strongs_data.items():
            # 1. Upsert Word
            conn.execute(
                """
                INSERT INTO lexicon_words (strong, lang, lexeme, translit, pronunciation, short_gloss)
                VALUES (?, 'Hebrew', ?, ?, ?, ?)
                ON CONFLICT(strong) DO UPDATE SET
                    lexeme = excluded.lexeme,
                    translit = excluded.translit,
                    pronunciation = excluded.pronunciation,
                    short_gloss = excluded.short_gloss
                """,
                (
                    strong_num,
                    sdata["lexeme"],
                    sdata["translit"],
                    sdata["pronunciation"],
                    sdata["short_gloss"],
                ),
            )
            
            word_id = conn.execute("SELECT id FROM lexicon_words WHERE strong = ?", (strong_num,)).fetchone()[0]
            words_inserted += 1

            # 2. Upsert Strong's Entry
            conn.execute(
                """
                INSERT INTO lexicon_entries (strong_id, source_id, entry_html)
                VALUES (?, ?, ?)
                ON CONFLICT(strong_id, source_id) DO UPDATE SET entry_html = excluded.entry_html
                """,
                (word_id, source_ids["STRONGS"], sdata["entry_html"]),
            )
            entries_inserted += 1

            # 3. Combine and Upsert BDB Entry if mapping exists
            bdb_ids = strong_to_bdb.get(strong_num, [])
            if bdb_ids:
                mapped_bdb_entries = [bdb_entries[bid] for bid in bdb_ids if bid in bdb_entries]
                if mapped_bdb_entries:
                    # Sort entries so the one with <sense> nodes comes first
                    mapped_bdb_entries.sort(key=lambda x: x["has_senses"], reverse=True)

                    combined_bdb_html = "".join([item["html"] for item in mapped_bdb_entries])
                    conn.execute(
                        """
                        INSERT INTO lexicon_entries (strong_id, source_id, entry_html)
                        VALUES (?, ?, ?)
                        ON CONFLICT(strong_id, source_id) DO UPDATE SET entry_html = excluded.entry_html
                        """,
                        (word_id, source_ids["BDB"], combined_bdb_html),
                    )
                    entries_inserted += 1

        conn.commit()
        print(f"Successfully processed lexicons:")
        print(f"  - Total Lexicon Words Upserted: {words_inserted}")
        print(f"  - Total Lexicon Entries Upserted: {entries_inserted}")

    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import Hebrew Lexicons into bible.sqlite")
    parser.add_argument("folder_path", help="Folder containing the XML files")
    parser.add_argument("db_path", nargs="?", default="bible.sqlite", help="Path to SQLite database")

    args = parser.parse_args()
    process_and_import(args.folder_path, args.db_path)