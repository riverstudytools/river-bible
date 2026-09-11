#!/usr/bin/env python3
"""
edit_bible.py — Lightweight Terminal Interface for Editing Bible Glosses.

Directly updates 'bible.sqlite' on disk and syncs changes to 'edits.json'.

Usage:
    python3 edit_bible.py --code HFB --db ../bible.sqlite --json sources/edits.json

Load edits into sqlite file:
    python3 edit_bible.py --db ../bible.sqlite --json sources/edits.json --load
"""

import argparse
import datetime
import json
import os
import re
import sqlite3
import sys

import book_codes

JSON_FILE = "edits.json"

def load_json_edits(filepath=JSON_FILE):
    """Loads JSON edits map from disk structured by passage context."""
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Warning: Could not read {filepath}: {e}")
        return {}

def save_json_edits(edits_data, filepath=JSON_FILE):
    """Saves edits dictionary to JSON on disk."""
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(edits_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving {filepath}: {e}")

def make_json_key(trans_code, book_name, chapter, verse, position):
    """Creates a predictable composite key for the JSON backup."""
    return f"{trans_code.upper()}:{book_name}:{chapter}:{verse}:{position}"

def sync_db_to_json(con, filepath=JSON_FILE):
    """Writes all edits currently in bible.sqlite out to edits.json using stable reference keys."""
    query = """
        SELECT 
            t.code,
            b.name,
            v.chapter,
            v.verse,
            w.position,
            e.gloss,
            e.updated_at
        FROM edits e
        JOIN words w ON w.id = e.word_id
        JOIN verses v ON v.id = w.verse_id
        JOIN books b ON b.id = v.book_id
        JOIN translations t ON t.id = v.translation_id
    """
    rows = con.execute(query).fetchall()
    edits_data = {}
    for r in rows:
        key = make_json_key(r[0], r[1], r[2], r[3], r[4])
        edits_data[key] = {
            "version": r[0].upper(),
            "book": r[1],
            "chapter": r[2],
            "verse": r[3],
            "position": r[4],
            "gloss": r[5],
            "updated_at": r[6] or datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
    save_json_edits(edits_data, filepath)
    print(f"Exported {len(edits_data)} edit(s) from database to '{filepath}'.")

def sync_json_to_db(con, filepath=JSON_FILE):
    """Replays all edits from edits.json into bible.sqlite by matching reference criteria."""
    edits_data = load_json_edits(filepath)
    if not edits_data:
        print(f"No edits found in '{filepath}' to import.")
        return 0

    lookup_query = """
        SELECT w.id 
        FROM words w
        JOIN verses v ON v.id = w.verse_id
        JOIN books b ON b.id = v.book_id
        JOIN translations t ON t.id = v.translation_id
        WHERE UPPER(t.code) = UPPER(?)
          AND LOWER(b.name) = LOWER(?)
          AND v.chapter = ?
          AND v.verse = ?
          AND w.position = ?
    """

    con.execute("BEGIN TRANSACTION;")
    try:
        count = 0
        unmatched = 0
        for key, val in edits_data.items():
            version = val.get("version")
            book = val.get("book")
            chapter = val.get("chapter")
            verse = val.get("verse")
            position = val.get("position")
            gloss = val.get("gloss", "")
            updated_at = val.get("updated_at") or datetime.datetime.now(datetime.timezone.utc).isoformat()

            # Resolve current word_id in rebuilt database
            row = con.execute(lookup_query, (version, book, chapter, verse, position)).fetchone()
            if not row:
                unmatched += 1
                continue

            word_id = row[0]
            con.execute("DELETE FROM edits WHERE word_id = ?", (word_id,))
            if gloss:
                con.execute(
                    "INSERT INTO edits (word_id, gloss, updated_at) VALUES (?, ?, ?)",
                    (word_id, gloss, updated_at)
                )
                count += 1

        con.commit()
        if unmatched:
            print(f"Warning: {unmatched} edit(s) could not be matched to target database entries.")
        print(f"Successfully loaded {count} edit(s) from '{filepath}' into database.")
        return count
    except Exception as e:
        con.rollback()
        print(f"Error importing JSON to DB: {e}")
        return 0

def fetch_translations(con):
    rows = con.execute("SELECT code, name FROM translations ORDER BY id").fetchall()
    return {code.upper(): name for code, name in rows}

def get_passage_words(con, translation_code, passage_str):
    m = re.match(r"^(.+?)\s+(\d+):(\d+)$", passage_str.strip())
    if not m:
        return None, None, None, None, []

    raw_book, ch_str, v_str = m.groups()
    canonical_book = book_codes.resolve_canonical_name(raw_book) or raw_book.strip()
    chapter = int(ch_str)
    verse = int(v_str)

    trans_row = con.execute(
        "SELECT id FROM translations WHERE UPPER(code) = UPPER(?)", (translation_code,)
    ).fetchone()
    if not trans_row:
        return None, None, None, None, []

    trans_id = trans_row[0]

    query = """
        SELECT 
            w.id AS word_id,
            w.position,
            w.original,
            w.gloss AS default_gloss,
            e.gloss AS edit_gloss
        FROM verses v
        JOIN books b ON b.id = v.book_id
        JOIN words w ON w.verse_id = v.id
        LEFT JOIN edits e ON e.word_id = w.id
        WHERE v.translation_id = ?
          AND LOWER(b.name) = LOWER(?)
          AND v.chapter = ?
          AND v.verse = ?
        ORDER BY w.position
    """
    rows = con.execute(query, [trans_id, canonical_book, chapter, verse]).fetchall()
    return trans_id, canonical_book, chapter, verse, rows

def display_verse(canonical_book, chapter, verse, words):
    if not words:
        print(f"\n==================================================================")
        print(f" EDITING: {canonical_book} {chapter}:{verse}")
        print(f"==================================================================")
        print(" [No verse data found]\n")
        return

    num_col_len = max(len("#"), len(str(len(words))))
    orig_col_len = max(len("ORIGINAL"), *(len(w[2] or "") for w in words))
    def_col_len = max(len("DEFAULT GLOSS"), *(len(w[3] or "") for w in words))
    edit_col_len = max(
        len("YOUR EDIT"),
        *(len(f"-> {w[4]}") if w[4] else 0 for w in words)
    )

    num_hdr = f"{'#':<{num_col_len}}"
    orig_hdr = f"{'ORIGINAL':<{orig_col_len}}"
    def_hdr = f"{'DEFAULT GLOSS':<{def_col_len}}"
    edit_hdr = f"{'YOUR EDIT':<{edit_col_len}}"

    header_line = f" {num_hdr} | {orig_hdr} | {def_hdr} | {edit_hdr}"
    separator = "-" * len(header_line)

    print(f"\n{separator}")
    print(f" EDITING: {canonical_book} {chapter}:{verse}")
    print(f"{separator}")
    print(header_line)
    print(separator)

    for idx, w in enumerate(words, start=1):
        word_id, pos, orig, default_g, edit_g = w
        
        num_str = f"{idx:<{num_col_len}}"
        orig_str = f"{(orig or ''):<{orig_col_len}}"
        def_str = f"{(default_g or ''):<{def_col_len}}"
        edit_val = f"-> {edit_g}" if edit_g else ""
        edit_str = f"{edit_val:<{edit_col_len}}"

        print(f" {num_str} | {orig_str} | {def_str} | {edit_str}")
    
    print(f"{separator}\n")

def save_edit(con, word_id, translation_code, canonical_book, chapter, verse, position, new_gloss, filepath=JSON_FILE):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    # 1. Update SQLite DB
    if not new_gloss:
        con.execute("DELETE FROM edits WHERE word_id = ?", (word_id,))
        print(f"Cleared edit for word #{word_id}.")
    else:
        con.execute("DELETE FROM edits WHERE word_id = ?", (word_id,))
        con.execute(
            "INSERT INTO edits (word_id, gloss, updated_at) VALUES (?, ?, ?)",
            (word_id, new_gloss, now)
        )
        print(f"Saved edit for word #{word_id}: '{new_gloss}'")
    con.commit()

    # 2. Update JSON file with reference object
    json_data = load_json_edits(filepath)
    key = make_json_key(translation_code, canonical_book, chapter, verse, position)
    
    if not new_gloss:
        if key in json_data:
            del json_data[key]
    else:
        json_data[key] = {
            "version": translation_code.upper(),
            "book": canonical_book,
            "chapter": chapter,
            "verse": verse,
            "position": position,
            "gloss": new_gloss,
            "updated_at": now
        }
    save_json_edits(json_data, filepath)

def interactive_loop(con, default_code, json_file):
    translations = fetch_translations(con)
    if not translations:
        sys.exit("Error: No translations found in database.")

    current_code = default_code.upper() if default_code.upper() in translations else list(translations.keys())[0]
    
    cur_book = "Gen"
    cur_ch = 1
    cur_v = 1
    
    _, cur_book, cur_ch, cur_v, words = get_passage_words(con, current_code, f"{cur_book} {cur_ch}:{cur_v}")

    print("\nBible Gloss Terminal Editor")
    print("Commands:")
    print("  <Book> <Ch>:<V>   Jump to verse (e.g., 'John 3:16', 'Gen 1:1')")
    print("  e <num> <text>    Edit word number (e.g., 'e 1 In the origin')")
    print("  d <num>           Delete edit for word number (e.g., 'd 1')")
    print("  n / p             Next / Previous verse")
    print("  use <code>        Switch translation (e.g., 'use KJV')")
    print("  sync-to-db        Import JSON edits into DB file")
    print("  sync-to-json      Export DB edits out to JSON file")
    print("  q                 Quit\n")

    if words:
        display_verse(cur_book, cur_ch, cur_v, words)

    while True:
        try:
            prompt_label = f"[{current_code} {cur_book or 'Ref'} {cur_ch}:{cur_v}] > "
            cmd = input(prompt_label).strip()
            if not cmd:
                continue

            if cmd.lower() in ('q', 'quit', 'exit'):
                print("Goodbye!")
                break

            if cmd.lower() == 'sync-to-db':
                sync_json_to_db(con, json_file)
                _, cur_book, cur_ch, cur_v, words = get_passage_words(con, current_code, f"{cur_book} {cur_ch}:{cur_v}")
                display_verse(cur_book, cur_ch, cur_v, words)
                continue

            if cmd.lower() == 'sync-to-json':
                sync_db_to_json(con, json_file)
                continue

            if cmd.lower().startswith(('use ', 'set ')):
                new_code = cmd.split()[1].upper()
                if new_code in translations:
                    current_code = new_code
                    _, cur_book, cur_ch, cur_v, words = get_passage_words(con, current_code, f"{cur_book} {cur_ch}:{cur_v}")
                    display_verse(cur_book, cur_ch, cur_v, words)
                else:
                    print(f"Unknown code '{new_code}'. Available: {', '.join(translations.keys())}")
                continue

            if cmd.lower() == 'n':
                cur_v += 1
                _, cur_book, cur_ch, cur_v, words = get_passage_words(con, current_code, f"{cur_book} {cur_ch}:{cur_v}")
                display_verse(cur_book, cur_ch, cur_v, words)
                continue

            if cmd.lower() == 'p':
                if cur_v > 1:
                    cur_v -= 1
                    _, cur_book, cur_ch, cur_v, words = get_passage_words(con, current_code, f"{cur_book} {cur_ch}:{cur_v}")
                display_verse(cur_book, cur_ch, cur_v, words)
                continue

            # Handle word edit command: 'e 1 New Gloss'
            if cmd.lower().startswith('e '):
                parts = cmd.split(maxsplit=2)
                if len(parts) >= 2 and parts[1].isdigit():
                    idx = int(parts[1]) - 1
                    new_gloss = parts[2] if len(parts) > 2 else ""
                    if 0 <= idx < len(words):
                        word_id, position = words[idx][0], words[idx][1]
                        save_edit(con, word_id, current_code, cur_book, cur_ch, cur_v, position, new_gloss, json_file)
                        _, cur_book, cur_ch, cur_v, words = get_passage_words(con, current_code, f"{cur_book} {cur_ch}:{cur_v}")
                        display_verse(cur_book, cur_ch, cur_v, words)
                    else:
                        print(f"Invalid word index #{parts[1]}. Must be 1-{len(words)}.")
                else:
                    print("Usage: e <word_number> <new gloss text>")
                continue

            # Handle edit deletion: 'd 1'
            if cmd.lower().startswith('d '):
                parts = cmd.split(maxsplit=1)
                if len(parts) == 2 and parts[1].isdigit():
                    idx = int(parts[1]) - 1
                    if 0 <= idx < len(words):
                        word_id, position = words[idx][0], words[idx][1]
                        save_edit(con, word_id, current_code, cur_book, cur_ch, cur_v, position, "", json_file)
                        _, cur_book, cur_ch, cur_v, words = get_passage_words(con, current_code, f"{cur_book} {cur_ch}:{cur_v}")
                        display_verse(cur_book, cur_ch, cur_v, words)
                    else:
                        print(f"Invalid word index #{parts[1]}. Must be 1-{len(words)}.")
                else:
                    print("Usage: d <word_number>")
                continue

            # Treat input as a passage query
            t_id, b_name, ch, v, new_words = get_passage_words(con, current_code, cmd)
            if new_words:
                cur_book, cur_ch, cur_v, words = b_name, ch, v, new_words
                display_verse(cur_book, cur_ch, cur_v, words)
            else:
                print(f"Could not load passage '{cmd}'. Try format like 'Gen 1:1'.")

        except (KeyboardInterrupt, EOFError):
            print("\nGoodbye!")
            break

def main():
    parser = argparse.ArgumentParser(description="Terminal Gloss Editor for bible.sqlite")
    parser.add_argument('-c', '--code', default='KJV', help="Translation code (e.g., KJV, WEB)")
    parser.add_argument('--db', default='bible.sqlite', help="Database path (default: bible.sqlite)")
    parser.add_argument('--json', default='edits.json', help="JSON edits file path (default: edits.json)")
    parser.add_argument('--load', action='store_true', help="Load JSON edits into the database file and exit")

    args = parser.parse_args()

    con = sqlite3.connect(args.db)
    try:
        if args.load:
            sync_json_to_db(con, args.json)
        else:
            interactive_loop(con, args.code, args.json)
    finally:
        con.close()

if __name__ == '__main__':
    main()