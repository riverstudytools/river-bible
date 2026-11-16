#!/usr/bin/env python3
"""
import_shebanq_gloss.py

Imports the short English "gloss" feature from the ETCBC/BHSA Text-Fabric
dataset (the linguistic database behind https://shebanq.ancient-data.org/)
into bible.sqlite, following the lexicon_words / lexicon_sources /
lexicon_entries schema used by the other lexicon importers.

LICENSE NOTE: unlike Strong's, BDB, and Dodson (all public domain / CC BY),
the BHSA data is licensed CC BY-NC 4.0 (Attribution-NonCommercial). It is
imported here on that basis -- non-commercial use only, with attribution to
the Eep Talstra Centre for Bible and Computer (ETCBC) / SHEBANQ. Do not
enable this source for a commercial build of River Bible without separate
permission from the German Bible Society / ETCBC.

WHAT THIS ACTUALLY IS: SHEBANQ itself is a search UI, not a standalone
lexicon -- the underlying BHSA corpus attaches one short English gloss
(a few words, not a full definition) to each Hebrew/Aramaic lexeme node.
There is no BDB-style full entry and no official Strong's-number mapping
in the BHSA data, so this script cross-references BHSA lexemes to your
existing Strong's Hebrew entries (from import_strongs_bdb_fixed.py) by
matching the Hebrew wordform itself, in two passes:

  1. Exact match on the fully-pointed lexeme (handles nouns, adjectives,
     particles, etc., where BHSA's voc_lex_utf8 is fully vocalized).
  2. Consonant-skeleton match (niqqud/cantillation stripped from both
     sides) for whatever remains unmatched -- this is what's needed for
     verbs, since BHSA cites verb lexemes by bare root (e.g. "ברא") while
     Strong's/BDB cite a vocalized citation form (e.g. "בָּרָא").

Either pass is skipped for a given BHSA lexeme if the match is ambiguous
(more than one existing Strong's entry shares that wordform/skeleton) --
this is a gloss *enrichment* feature, so a missed match is far better than
a wrong one. Expect roughly two-thirds of BHSA's ~9,200 lexemes to match
cleanly; the rest (mostly homographs and proper names) are left alone.

INPUT: point this script at a folder containing three files downloaded
from https://github.com/ETCBC/bhsa/tree/master/tf/2021 :
  - otype.tf
  - gloss.tf
  - voc_lex_utf8.tf
e.g.:
  mkdir bhsa_tf && cd bhsa_tf
  curl -LO https://raw.githubusercontent.com/ETCBC/bhsa/master/tf/2021/otype.tf
  curl -LO https://raw.githubusercontent.com/ETCBC/bhsa/master/tf/2021/gloss.tf
  curl -LO https://raw.githubusercontent.com/ETCBC/bhsa/master/tf/2021/voc_lex_utf8.tf

Then:
  python3 import_shebanq_gloss.py bhsa_tf bible.sqlite

Requires that Strong's Hebrew entries already exist in lexicon_words
(run import_strongs_bdb_fixed.py first) -- this script only cross-links
to them, it does not create new Strong's numbers.
"""

import argparse
import os
import re
import sqlite3
import sys
import unicodedata

SHEBANQ_SOURCE = (
    "SHEBANQ",
    "SHEBANQ / ETCBC BHSA Gloss",
    "CC BY-NC 4.0 -- Eep Talstra Centre for Bible and Computer (ETCBC) / "
    "SHEBANQ (https://shebanq.ancient-data.org). Non-commercial use only; "
    "attribution required.",
)

# Hebrew points, cantillation marks, and other combining marks in the
# Hebrew Unicode block (U+0591-U+05C7), used to reduce a pointed wordform
# to its bare consonant skeleton.
_HEBREW_POINTS_RE = re.compile(r"[\u0591-\u05C7]")


# ---------------------------------------------------------------------------
# Schema & Source Setup (same schema as the other lexicon importers)
# ---------------------------------------------------------------------------

def ensure_schema(conn):
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


def ensure_shebanq_source(conn):
    code, name, copyright_ = SHEBANQ_SOURCE
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
# Text-Fabric plain-text feature file parser
# ---------------------------------------------------------------------------

def parse_tf_feature(path):
    """Parses a Text-Fabric @node plain-text feature file into {node: value}.

    Format: a header block of "@key=value" lines, a blank separator line,
    then one data line per assignment. A bare line assigns the next node
    (auto-incrementing from the last one used); a line containing a tab
    is "<node_number>\\t<value>" and jumps to that node number (used
    after gaps, e.g. when the feature has no value for a run of nodes).
    """
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    lines = text.split("\n")

    i = 0
    while i < len(lines) and lines[i].strip() != "":
        i += 1
    i += 1  # skip the blank separator line

    data = {}
    node = 1
    for line in lines[i:]:
        if line == "":
            continue
        if "\t" in line:
            num_str, value = line.split("\t", 1)
            node = int(num_str)
        else:
            value = line
        data[node] = value
        node += 1
    return data


def parse_otype_ranges(path):
    """Parses otype.tf's node-range table into {otype_name: (first, last)}."""
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    lines = text.split("\n")

    i = 0
    while i < len(lines) and lines[i].strip() != "":
        i += 1
    i += 1

    ranges = {}
    for line in lines[i:]:
        if not line.strip() or "\t" not in line:
            continue
        node_range, otype = line.split("\t", 1)
        otype = otype.strip()
        if "-" in node_range:
            start, end = node_range.split("-")
        else:
            start = end = node_range
        ranges[otype] = (int(start), int(end))
    return ranges


# ---------------------------------------------------------------------------
# Wordform normalization / matching
# ---------------------------------------------------------------------------

def strip_hebrew_points(text):
    """Reduces a pointed Hebrew string to its bare consonant skeleton."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFD", text)
    consonants_only = _HEBREW_POINTS_RE.sub("", decomposed)
    return unicodedata.normalize("NFC", consonants_only)


def build_match_indexes(conn):
    """Builds exact-pointed-form and consonant-skeleton lookup indexes from
    the Strong's Hebrew entries already in lexicon_words."""
    rows = conn.execute(
        "SELECT strong, lexeme FROM lexicon_words WHERE lang = 'Hebrew'"
    ).fetchall()

    exact_index = {}
    skeleton_index = {}
    for strong, lexeme in rows:
        exact_key = unicodedata.normalize("NFC", lexeme or "")
        exact_index.setdefault(exact_key, []).append(strong)

        skeleton_key = strip_hebrew_points(lexeme)
        skeleton_index.setdefault(skeleton_key, []).append(strong)

    return exact_index, skeleton_index


def match_strong(voc_lex, exact_index, skeleton_index):
    """Returns (strong_number, tier) or (None, reason) for a BHSA lexeme's
    vocalized wordform, using the two-tier matching strategy described in
    the module docstring. tier/reason is one of:
        'exact'     -- unambiguous exact pointed-form match
        'skeleton'  -- unambiguous consonant-skeleton match
        'ambiguous' -- more than one existing Strong's entry matched
        'unmatched' -- no existing Strong's entry matched at all
    """
    exact_key = unicodedata.normalize("NFC", voc_lex or "")
    candidates = exact_index.get(exact_key, [])
    if len(candidates) == 1:
        return candidates[0], "exact"
    if len(candidates) > 1:
        return None, "ambiguous"

    skeleton_key = strip_hebrew_points(voc_lex)
    candidates = skeleton_index.get(skeleton_key, [])
    if len(candidates) == 1:
        return candidates[0], "skeleton"
    if len(candidates) > 1:
        return None, "ambiguous"

    return None, "unmatched"


# ---------------------------------------------------------------------------
# Importer Driver
# ---------------------------------------------------------------------------

def process_and_import(tf_folder, db_path):
    otype_path = os.path.join(tf_folder, "otype.tf")
    gloss_path = os.path.join(tf_folder, "gloss.tf")
    voc_path = os.path.join(tf_folder, "voc_lex_utf8.tf")

    for p in (otype_path, gloss_path, voc_path):
        if not os.path.exists(p):
            print(f"Error: required file not found: {p}", file=sys.stderr)
            sys.exit(1)

    print("Parsing otype.tf...")
    otype_ranges = parse_otype_ranges(otype_path)
    if "lex" not in otype_ranges:
        print("Error: no 'lex' node type found in otype.tf", file=sys.stderr)
        sys.exit(1)
    lex_start, lex_end = otype_ranges["lex"]

    print("Parsing gloss.tf...")
    gloss_data = parse_tf_feature(gloss_path)

    print("Parsing voc_lex_utf8.tf...")
    voc_data = parse_tf_feature(voc_path)

    lex_nodes = [n for n in gloss_data if lex_start <= n <= lex_end]
    print(f"Found {len(lex_nodes)} lexeme nodes with a gloss value.")

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        ensure_schema(conn)
        source_id = ensure_shebanq_source(conn)

        print("Building match indexes against existing Strong's Hebrew entries...")
        exact_index, skeleton_index = build_match_indexes(conn)
        if not exact_index:
            print(
                "Warning: no existing Hebrew entries found in lexicon_words. "
                "Run import_strongs_bdb_fixed.py first -- this importer only "
                "cross-links to existing Strong's numbers, it does not create "
                "new ones.",
                file=sys.stderr,
            )

        stats = {"exact": 0, "skeleton": 0, "ambiguous": 0, "unmatched": 0}
        entries_written = 0
        short_gloss_filled = 0

        for node in lex_nodes:
            voc_lex = voc_data.get(node, "")
            gloss = (gloss_data.get(node) or "").strip()
            if not gloss:
                continue

            strong, tier = match_strong(voc_lex, exact_index, skeleton_index)
            stats[tier if strong is None else tier] += 1
            if strong is None:
                continue

            word_id = conn.execute(
                "SELECT id, short_gloss FROM lexicon_words WHERE strong = ?", (strong,)
            ).fetchone()
            if word_id is None:
                continue
            word_id, existing_short_gloss = word_id

            entry_html = f'<p class="gloss">{gloss}</p>'
            conn.execute(
                """
                INSERT INTO lexicon_entries (strong_id, source_id, entry_html)
                VALUES (?, ?, ?)
                ON CONFLICT(strong_id, source_id) DO UPDATE SET entry_html = excluded.entry_html
                """,
                (word_id, source_id, entry_html),
            )
            entries_written += 1

            # Fallback: fill short_gloss only where it's currently empty --
            # never overwrites a gloss already sourced from Strong's/BDB.
            if not existing_short_gloss:
                conn.execute(
                    "UPDATE lexicon_words SET short_gloss = ? WHERE id = ?",
                    (gloss, word_id),
                )
                short_gloss_filled += 1

        conn.commit()
        print("Successfully processed SHEBANQ/BHSA gloss data:")
        print(f"  - Lexicon Entries Written: {entries_written}")
        print(f"  - short_gloss Fallback Fields Filled: {short_gloss_filled}")
        print(f"  - Unambiguous exact-form matches: {stats['exact']}")
        print(f"  - Unambiguous consonant-skeleton matches: {stats['skeleton']}")
        print(f"  - Skipped (ambiguous wordform, multiple candidates): {stats['ambiguous']}")
        print(f"  - Skipped (no matching Strong's entry found): {stats['unmatched']}")

    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Import SHEBANQ/ETCBC BHSA lexeme glosses into bible.sqlite"
    )
    parser.add_argument("tf_folder", help="Folder containing otype.tf, gloss.tf, voc_lex_utf8.tf")
    parser.add_argument("db_path", nargs="?", default="bible.sqlite", help="Path to SQLite database")

    args = parser.parse_args()
    process_and_import(args.tf_folder, args.db_path)
