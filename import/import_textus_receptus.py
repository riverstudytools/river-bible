#!/usr/bin/env python3
"""
import_textus_receptus.py — Importer for Dr. Maurice A. Robinson's parsed
Textus Receptus (byztxt/greektext-textus-receptus, public domain), into
River Bible's SQLite database.

Source data:
    https://github.com/byztxt/greektext-textus-receptus
    (the `parsed` folder — one *.UTR file per NT book)

The source encodes each Greek word in a simple lowercase-ASCII letter
scheme (a=alpha, b=beta, ... v=final sigma, w=omega — NO accents/breathings)
followed by its Strong's number, an optional tense-voice-mood number for
verbs, and a morphological parsing tag in braces, e.g.:

    1:1 en 1722 {PREP} arch 746 {N-DSF} hn 1510 5707 {V-IAI-3S} ...

The source also contains a small amount of critical-apparatus markup:
  - `| wordA | wordB | 1234 {TAG}` — alternate spellings sharing one
    parse; we keep the reading closest to the parse (wordB) and drop the
    bracketing pipes.
  - `word0 1234 {TAG} 5678 {TAG2}` — a second, alternate parse for the
    same word; we keep the first parse and record the second in the
    word's footnote field.
  - `[... ]` bracketed spans (e.g. the disputed subscription at the end
    of some epistles) — brackets are stripped, the words inside are
    imported normally.
  - `(chapter:verse)` parenthetical alternate-versification notes — these
    are discarded; they don't carry textual content.

Usage:
    python3 import_textus_receptus.py path/to/parsed_folder \
        --code TR --name "Textus Receptus (Scrivener/Robinson parsed)" --replace
"""

import argparse
import glob
import os
import re
import sqlite3
import sys

import book_codes
import build_db as db_builder
import build_search_index as bsi

# ---------------------------------------------------------------------------
# Greek transliteration table
# ---------------------------------------------------------------------------
# The source uses a simple lowercase-Latin letter-for-letter scheme with
# no accents/breathings, and a distinct 'v'/'V' for final sigma.
_GREEK_MAP = {
    "a": "α", "b": "β", "g": "γ", "d": "δ", "e": "ε", "z": "ζ", "h": "η",
    "q": "θ", "i": "ι", "k": "κ", "l": "λ", "m": "μ", "n": "ν", "x": "ξ",
    "o": "ο", "p": "π", "r": "ρ", "s": "σ", "t": "τ", "u": "υ", "f": "φ",
    "c": "χ", "y": "ψ", "w": "ω", "v": "ς",
}
_GREEK_TRANSLATE_TABLE = {}
for _lower, _greek in _GREEK_MAP.items():
    _GREEK_TRANSLATE_TABLE[ord(_lower)] = _greek
    _GREEK_TRANSLATE_TABLE[ord(_lower.upper())] = _greek.upper() if _greek != "ς" else "Σ"


def decode_greek_word(ascii_word):
    """Converts the source's ASCII letter scheme into Unicode Greek."""
    return ascii_word.translate(_GREEK_TRANSLATE_TABLE)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

VERSE_RE = re.compile(r"(\d+):(\d+)\s+(.*?)(?=\n\d+:\d+\s|\Z)", re.S)

# Strips apparatus markup that carries no textual content of its own.
NOISE_CHARS_RE = re.compile(r"[\[\]|]")
VERSIFICATION_NOTE_RE = re.compile(r"\(\d+:\d+\)")

# Matches one word token, optionally preceded by an alternate-spelling word
# (from `| wordA | wordB |` apparatus, already stripped of pipes), followed
# by 1-3 numbers (strong, optionally TVM, optionally a stray placeholder),
# a {PARSING} tag, and an optional second alternate parse for the same word.
TOKEN_RE = re.compile(
    r"(?:([A-Za-z]+)\s+)?"          # optional alternate-spelling word (discarded)
    r"([A-Za-z]+)"                  # the word we keep
    r"((?:\s+\d+){1,3})"            # 1-3 numbers: strong [+ tvm] [+ placeholder]
    r"\s*\{([^}]+)\}"               # primary parsing tag
    r"(?:\s+(\d+(?:\s+\d+)?)\s*\{([^}]+)\})?"  # optional alternate parse
)


def format_strong(nums_str):
    """Picks the first non-zero number from a whitespace-separated group."""
    nums = [n for n in nums_str.split() if n != "0"]
    return f"G{int(nums[0])}" if nums else None


def parse_book_file(path):
    """Yields (chapter, verse, [word_dict, ...]) tuples for one *.UTR file."""
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        raw = f.read()
    text = raw.replace("\r\n", "\n").replace("\r", "\n")

    for vm in VERSE_RE.finditer(text):
        chapter, verse, body = vm.groups()
        body = VERSIFICATION_NOTE_RE.sub(" ", body)
        body = NOISE_CHARS_RE.sub(" ", body)
        body = re.sub(r"\s+", " ", body).strip()

        words = []
        for tm in TOKEN_RE.finditer(body):
            _alt_spelling, ascii_word, nums, tag, alt_nums, alt_tag = tm.groups()
            greek = decode_greek_word(ascii_word)
            strong = format_strong(nums)

            footnote = None
            if alt_tag:
                alt_strong = format_strong(alt_nums) if alt_nums else None
                footnote = f"alt. parse: {alt_strong or ''} {{{alt_tag}}}".strip()

            words.append({
                "original": greek,
                "gloss": greek,
                "strong": strong,
                "parsing": tag,
                "footnote": footnote,
            })

        yield int(chapter), int(verse), words


# ---------------------------------------------------------------------------
# Database import
# ---------------------------------------------------------------------------

def import_translation(con, translation_id, book_id, verse_records):
    verses_inserted = 0
    words_inserted = 0

    for chapter, verse, words in verse_records:
        if not words:
            continue

        existing = con.execute(
            "SELECT id FROM verses WHERE translation_id=? AND book_id=? AND chapter=? AND verse=?",
            (translation_id, book_id, chapter, verse),
        ).fetchone()

        if existing:
            verse_id = existing[0]
            con.execute("DELETE FROM words WHERE verse_id=?", (verse_id,))
        else:
            cur = con.execute(
                "INSERT INTO verses(translation_id, book_id, chapter, verse) VALUES (?,?,?,?)",
                (translation_id, book_id, chapter, verse),
            )
            verse_id = cur.lastrowid
            verses_inserted += 1

        rows = [
            (
                verse_id, pos, "Greek",
                w["original"], None, w["parsing"], w["strong"],
                w["gloss"], "", "", "", "", w["footnote"], None, None, None,
            )
            for pos, w in enumerate(words)
        ]
        con.executemany(
            """INSERT INTO words(
                verse_id, position, lang, original, translit, parsing, strong,
                gloss, beg_q, pnc, end_q, end_text, footnote, hdg, crossref, par_classes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )
        words_inserted += len(rows)

    return verses_inserted, words_inserted


def ensure_book(con, book_id_by_name, book_order_map, canonical_book):
    if canonical_book in book_id_by_name:
        return book_id_by_name[canonical_book]
    testament = bsi.testament_for(canonical_book)
    next_order = max(book_order_map.values(), default=-1) + 1
    cur = con.execute(
        "INSERT INTO books(name, testament, book_order) VALUES (?,?,?)",
        (canonical_book, testament, next_order),
    )
    book_id_by_name[canonical_book] = cur.lastrowid
    book_order_map[canonical_book] = next_order
    return cur.lastrowid


def main():
    ap = argparse.ArgumentParser(description="Import byztxt parsed Textus Receptus into River SQLite DB")
    ap.add_argument("parsed_dir", help="Path to the 'parsed' folder of byztxt/greektext-textus-receptus")
    ap.add_argument("--code", default="TR", help="Translation code (default: TR)")
    ap.add_argument("--name", default="Textus Receptus (Robinson parsed text)", help="Display name")
    ap.add_argument("--language", default="Greek", help="Language of text")
    ap.add_argument("--db", default="bible.sqlite", help="SQLite DB path")
    ap.add_argument("--replace", action="store_true", help="Replace existing translation")
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=20.0)
    con.execute("PRAGMA foreign_keys = ON")

    try:
        with con:
            db_builder.ensure_schema(con)

            existing = con.execute("SELECT id FROM translations WHERE code=?", (args.code,)).fetchone()
            if existing and not args.replace:
                sys.exit(f'Error: Translation code "{args.code}" exists. Use --replace to overwrite.')

            if existing:
                translation_id = existing[0]
                con.execute("UPDATE translations SET name=?, language=? WHERE id=?", (args.name, args.language, translation_id))
                db_builder.wipe_translation(con, translation_id)
            else:
                cur = con.execute(
                    "INSERT INTO translations(code, name, language) VALUES (?,?,?)",
                    (args.code, args.name, args.language),
                )
                translation_id = cur.lastrowid

            books_data = con.execute("SELECT name, id, book_order FROM books").fetchall()
            book_id_by_name = {row[0]: row[1] for row in books_data}
            book_order_map = {row[0]: row[2] for row in books_data}

            total_verses = 0
            total_words = 0
            unresolved_files = []

            for path in sorted(glob.glob(os.path.join(args.parsed_dir, "*.UTR"))):
                raw_code = os.path.splitext(os.path.basename(path))[0]
                canonical_book = book_codes.resolve_canonical_name(raw_code)
                if not canonical_book:
                    unresolved_files.append(raw_code)
                    continue

                book_id = ensure_book(con, book_id_by_name, book_order_map, canonical_book)
                verse_records = list(parse_book_file(path))
                v_count, w_count = import_translation(con, translation_id, book_id, verse_records)
                total_verses += v_count
                total_words += w_count
                print(f"  {raw_code:6s} -> {canonical_book:20s} {v_count:4d} verses, {w_count:6d} words")

            if unresolved_files:
                print("Warning: could not resolve book codes (add aliases to book_codes.py):", unresolved_files, file=sys.stderr)

        print(f'Successfully imported "{args.name}" ({args.code}): {total_verses} verses, {total_words} words into {args.db}.')

    except Exception as e:
        con.rollback()
        sys.exit(f"Database import failed: {e}")
    finally:
        con.close()


if __name__ == "__main__":
    main()
