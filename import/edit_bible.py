#!/usr/bin/env python3
"""
edit_bible.py — Lightweight Terminal Interface for Editing Bible Glosses, Punctuation, & Word Order.

Directly updates 'bible.sqlite' on disk and syncs validated changes to 'edits.json'.
Reorders (both partial and full), blank/custom gloss edits, and punctuation edits are anchored to 
canonical import positions, making them idempotent and safely re-appliable after database rebuilds.

Usage:
    python3 edit_bible.py --code KJV --db ../bible.sqlite --json sources/edits.json

Load edits/reorders into sqlite file:
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
    """Loads JSON edits structured by reorders, gloss overrides, and punctuation."""
    if not os.path.exists(filepath):
        return {"reorders": {}, "glosses": {}, "punctuation": {}}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
            if "reorders" not in data or "glosses" not in data:
                # Fallback migration for legacy flat edits.json files
                return {"reorders": {}, "glosses": data, "punctuation": {}}
            if "punctuation" not in data:
                data["punctuation"] = {}
            return data
    except Exception as e:
        print(f"Warning: Could not read {filepath}: {e}")
        return {"reorders": {}, "glosses": {}, "punctuation": {}}

def save_json_edits(edits_data, filepath=JSON_FILE):
    """Saves structured edits dictionary to JSON on disk."""
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(edits_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"Error saving {filepath}: {e}")

def make_verse_key(trans_code, book_name, chapter, verse):
    """Creates a predictable composite verse key."""
    return f"{trans_code.upper()}:{book_name}:{chapter}:{verse}"

def parse_range_indices(range_str, max_len):
    """
    Parses range strings like '3-5', '1,3,5', or '2-4,6' 
    into 0-indexed integer list. Used for commands that support ranges (e.g., 'd', 'p').
    """
    indices = []
    tokens = range_str.split(',')
    for token in tokens:
        token = token.strip()
        if '-' in token:
            parts = token.split('-')
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                start, end = int(parts[0]), int(parts[1])
                indices.extend(range(start - 1, end))
            else:
                return None
        elif token.isdigit():
            indices.append(int(token) - 1)
        else:
            return None
    valid_indices = [i for i in indices if 0 <= i < max_len]
    return list(dict.fromkeys(valid_indices))  # preserve order & deduplicate

def expand_partial_reorder(partial_indices, total_words):
    """
    Expands a partial list of 0-based indices into a full verse permutation.
    Unmentioned indices maintain their original relative positions in remaining slots.
    """
    if len(partial_indices) > total_words:
        return None

    target_slots = sorted(partial_indices)
    full_order = list(range(total_words))

    for slot, word_idx in zip(target_slots, partial_indices):
        full_order[slot] = word_idx

    return full_order

def sync_json_to_db(con, filepath=JSON_FILE):
    """Replays all reorders, gloss edits, and punctuation edits from edits.json into bible.sqlite idempotently."""
    edits_data = load_json_edits(filepath)
    reorders = edits_data.get("reorders", {})
    glosses = edits_data.get("glosses", {})
    punctuation = edits_data.get("punctuation", {})

    if not reorders and not glosses and not punctuation:
        print(f"No edits found in '{filepath}' to import.")
        return 0

    con.execute("BEGIN TRANSACTION;")
    try:
        reorder_count = 0
        gloss_count = 0
        pnc_count = 0

        # Phase 1: Replay Verse Word Reorders Idempotently
        for verse_key, sequence in reorders.items():
            parts = verse_key.split(":")
            if len(parts) != 4:
                continue
            version, book, chapter, verse = parts[0], parts[1], int(parts[2]), int(parts[3])

            rows = con.execute("""
                SELECT w.id, w.position, w.original 
                FROM words w
                JOIN verses v ON v.id = w.verse_id
                JOIN books b ON b.id = v.book_id
                JOIN translations t ON t.id = v.translation_id
                WHERE UPPER(t.code) = UPPER(?)
                  AND LOWER(b.name) = LOWER(?)
                  AND v.chapter = ?
                  AND v.verse = ?
                ORDER BY w.id
            """, (version, book, chapter, verse)).fetchall()

            if len(rows) != len(sequence):
                print(f"Skipping reorder for {verse_key}: word count mismatch with DB.")
                continue

            needs_update = False
            for target_pos, item in enumerate(sequence, start=1):
                orig_idx = item["orig_pos"] - 1
                if orig_idx < len(rows):
                    word_id, current_pos, _ = rows[orig_idx]
                    if current_pos != target_pos:
                        needs_update = True
                        break

            if needs_update:
                for idx, row in enumerate(rows, start=1):
                    word_id = row[0]
                    con.execute("UPDATE words SET position = ? WHERE id = ?", (-idx, word_id))
                
                for target_pos, item in enumerate(sequence, start=1):
                    orig_idx = item["orig_pos"] - 1
                    word_id = rows[orig_idx][0]
                    con.execute("UPDATE words SET position = ? WHERE id = ?", (target_pos, word_id))
                reorder_count += 1

        # Phase 2: Replay Gloss Edits using Canonical Position Mapping
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        for key, val in glosses.items():
            version = val.get("version")
            book = val.get("book")
            chapter = val.get("chapter")
            verse = val.get("verse")
            canon_pos = val.get("position")
            edit_gloss = val.get("gloss", "")

            rows = con.execute("""
                SELECT w.id 
                FROM words w
                JOIN verses v ON v.id = w.verse_id
                JOIN books b ON b.id = v.book_id
                JOIN translations t ON t.id = v.translation_id
                WHERE UPPER(t.code) = UPPER(?)
                  AND LOWER(b.name) = LOWER(?)
                  AND v.chapter = ?
                  AND v.verse = ?
                ORDER BY w.id
            """, (version, book, chapter, verse)).fetchall()

            if canon_pos and 1 <= canon_pos <= len(rows):
                word_id = rows[canon_pos - 1][0]
                con.execute("DELETE FROM edits WHERE word_id = ?", (word_id,))
                con.execute(
                    "INSERT INTO edits (word_id, gloss, updated_at) VALUES (?, ?, ?)",
                    (word_id, edit_gloss, now)
                )
                gloss_count += 1

        # Phase 3: Replay Punctuation Edits
        for key, val in punctuation.items():
            version = val.get("version")
            book = val.get("book")
            chapter = val.get("chapter")
            verse = val.get("verse")
            canon_pos = val.get("position")
            pnc_val = val.get("pnc", "")

            rows = con.execute("""
                SELECT w.id 
                FROM words w
                JOIN verses v ON v.id = w.verse_id
                JOIN books b ON b.id = v.book_id
                JOIN translations t ON t.id = v.translation_id
                WHERE UPPER(t.code) = UPPER(?)
                  AND LOWER(b.name) = LOWER(?)
                  AND v.chapter = ?
                  AND v.verse = ?
                ORDER BY w.id
            """, (version, book, chapter, verse)).fetchall()

            if canon_pos and 1 <= canon_pos <= len(rows):
                word_id = rows[canon_pos - 1][0]
                con.execute("UPDATE words SET pnc = ? WHERE id = ?", (pnc_val, word_id))
                pnc_count += 1

        con.commit()
        print(f"Applied {reorder_count} verse reorder(s), {gloss_count} gloss edit(s), and {pnc_count} punctuation edit(s) to database.")
        return reorder_count + gloss_count + pnc_count
    except Exception as e:
        con.rollback()
        print(f"Error importing edits to DB: {e}")
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
            e.gloss AS edit_gloss,
            w.pnc
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
        *(len(f"-> '{w[4]}'") if w[4] is not None else 0 for w in words)
    )
    pnc_col_len = max(len("PNC"), *(len(w[5] or "") for w in words))

    num_hdr = f"{'#':<{num_col_len}}"
    orig_hdr = f"{'ORIGINAL':<{orig_col_len}}"
    def_hdr = f"{'DEFAULT GLOSS':<{def_col_len}}"
    edit_hdr = f"{'YOUR EDIT':<{edit_col_len}}"
    pnc_hdr = f"{'PNC':<{pnc_col_len}}"

    header_line = f" {num_hdr} | {orig_hdr} | {def_hdr} | {edit_hdr} | {pnc_hdr}"
    separator = "-" * len(header_line)

    print(f"\n{separator}")
    print(f" EDITING: {canonical_book} {chapter}:{verse}")
    print(f"{separator}")
    print(header_line)
    print(separator)

    for idx, w in enumerate(words, start=1):
        word_id, pos, orig, default_g, edit_g, pnc = w
        
        num_str = f"{idx:<{num_col_len}}"
        orig_str = f"{(orig or ''):<{orig_col_len}}"
        def_str = f"{(default_g or ''):<{def_col_len}}"
        edit_val = f"-> '{edit_g}'" if edit_g is not None else ""
        edit_str = f"{edit_val:<{edit_col_len}}"
        pnc_str = f"{(pnc or ''):<{pnc_col_len}}"

        print(f" {num_str} | {orig_str} | {def_str} | {edit_str} | {pnc_str}")
    
    print(f"{separator}\n")

def save_gloss_edit(con, word_info, translation_code, canonical_book, chapter, verse, new_gloss, filepath=JSON_FILE):
    """
    Saves or deletes a gloss edit.
    - If new_gloss is string (even ""), it saves explicitly to SQLite edits and edits.json.
    - If new_gloss is None, it deletes from edits table and edits.json (reverting to default).
    """
    word_id, position, original, default_g = word_info
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    
    trans_row = con.execute("SELECT id FROM translations WHERE UPPER(code) = UPPER(?)", (translation_code,)).fetchone()
    if not trans_row:
        return
    trans_id = trans_row[0]

    base_rows = con.execute("""
        SELECT w.id 
        FROM words w
        JOIN verses v ON v.id = w.verse_id
        JOIN books b ON b.id = v.book_id
        WHERE v.translation_id = ?
          AND LOWER(b.name) = LOWER(?)
          AND v.chapter = ?
          AND v.verse = ?
        ORDER BY w.id
    """, (trans_id, canonical_book, chapter, verse)).fetchall()

    word_id_to_canon_pos = {row[0]: idx + 1 for idx, row in enumerate(base_rows)}
    canon_pos = word_id_to_canon_pos.get(word_id, position)

    edits_data = load_json_edits(filepath)
    key = f"{translation_code.upper()}:{canonical_book}:{chapter}:{verse}:{canon_pos}"

    if new_gloss is None:
        con.execute("DELETE FROM edits WHERE word_id = ?", (word_id,))
        if key in edits_data["glosses"]:
            del edits_data["glosses"][key]
    else:
        con.execute("DELETE FROM edits WHERE word_id = ?", (word_id,))
        con.execute(
            "INSERT INTO edits (word_id, gloss, updated_at) VALUES (?, ?, ?)",
            (word_id, new_gloss, now)
        )
        edits_data["glosses"][key] = {
            "version": translation_code.upper(),
            "book": canonical_book,
            "chapter": chapter,
            "verse": verse,
            "position": canon_pos,
            "original": original or "",
            "default_gloss": default_g or "",
            "gloss": new_gloss,
            "updated_at": now
        }
    
    con.commit()
    save_json_edits(edits_data, filepath)

def save_pnc_edit(con, word_info, translation_code, canonical_book, chapter, verse, new_pnc, filepath=JSON_FILE):
    """
    Saves trailing punctuation (words.pnc) in SQLite and updates edits.json.
    - If new_pnc is None, removes override from edits.json and restores default pnc from database.
    """
    word_id, position, original, default_g = word_info

    trans_row = con.execute("SELECT id FROM translations WHERE UPPER(code) = UPPER(?)", (translation_code,)).fetchone()
    if not trans_row:
        return
    trans_id = trans_row[0]

    base_rows = con.execute("""
        SELECT w.id 
        FROM words w
        JOIN verses v ON v.id = w.verse_id
        JOIN books b ON b.id = v.book_id
        WHERE v.translation_id = ?
          AND LOWER(b.name) = LOWER(?)
          AND v.chapter = ?
          AND v.verse = ?
        ORDER BY w.id
    """, (trans_id, canonical_book, chapter, verse)).fetchall()

    word_id_to_canon_pos = {row[0]: idx + 1 for idx, row in enumerate(base_rows)}
    canon_pos = word_id_to_canon_pos.get(word_id, position)

    edits_data = load_json_edits(filepath)
    if "punctuation" not in edits_data:
        edits_data["punctuation"] = {}

    key = f"{translation_code.upper()}:{canonical_book}:{chapter}:{verse}:{canon_pos}"
    
    if new_pnc is None:
        # Reset requested: delete override entry from JSON
        if key in edits_data["punctuation"]:
            del edits_data["punctuation"][key]
        
        # Restore default punctuation by re-checking DB or setting default
        con.execute("UPDATE words SET pnc = (SELECT pnc FROM words WHERE id = ?) WHERE id = ?", (word_id, word_id))
    else:
        con.execute("UPDATE words SET pnc = ? WHERE id = ?", (new_pnc, word_id))
        edits_data["punctuation"][key] = {
            "version": translation_code.upper(),
            "book": canonical_book,
            "chapter": chapter,
            "verse": verse,
            "position": canon_pos,
            "original": original or "",
            "pnc": new_pnc,
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
    
    con.commit()
    save_json_edits(edits_data, filepath)

def reset_word_edits(con, word_info, translation_code, canonical_book, chapter, verse, filepath=JSON_FILE):
    """Resets both gloss and punctuation overrides back to default source values."""
    save_gloss_edit(con, word_info, translation_code, canonical_book, chapter, verse, None, filepath)
    save_pnc_edit(con, word_info, translation_code, canonical_book, chapter, verse, None, filepath)

def reorder_verse(con, translation_code, canonical_book, chapter, verse, words, new_order, filepath=JSON_FILE):
    """
    Physically updates words.position in SQLite and updates edits.json reorder map.
    new_order is a 0-based list specifying the new display sequence for every word.
    """
    trans_row = con.execute("SELECT id FROM translations WHERE UPPER(code) = UPPER(?)", (translation_code,)).fetchone()
    if not trans_row:
        return False
    trans_id = trans_row[0]

    base_rows = con.execute("""
        SELECT w.id, w.original 
        FROM words w
        JOIN verses v ON v.id = w.verse_id
        JOIN books b ON b.id = v.book_id
        WHERE v.translation_id = ?
          AND LOWER(b.name) = LOWER(?)
          AND v.chapter = ?
          AND v.verse = ?
        ORDER BY w.id
    """, (trans_id, canonical_book, chapter, verse)).fetchall()

    word_id_to_canon_pos = {row[0]: idx + 1 for idx, row in enumerate(base_rows)}

    con.execute("BEGIN TRANSACTION;")
    try:
        for idx, w in enumerate(words, start=1):
            con.execute("UPDATE words SET position = ? WHERE id = ?", (-idx, w[0]))

        target_sequence = []
        for new_pos, orig_idx in enumerate(new_order, start=1):
            target_word = words[orig_idx]
            word_id = target_word[0]
            con.execute("UPDATE words SET position = ? WHERE id = ?", (new_pos, word_id))

            canon_pos = word_id_to_canon_pos[word_id]
            target_sequence.append({
                "orig_pos": canon_pos,
                "original": target_word[2] or ""
            })

        con.commit()

        edits_data = load_json_edits(filepath)
        verse_key = make_verse_key(translation_code, canonical_book, chapter, verse)
        
        is_canonical = all(item["orig_pos"] == i + 1 for i, item in enumerate(target_sequence))
        if is_canonical:
            if verse_key in edits_data["reorders"]:
                del edits_data["reorders"][verse_key]
        else:
            edits_data["reorders"][verse_key] = target_sequence

        save_json_edits(edits_data, filepath)
        return True
    except Exception as e:
        con.rollback()
        print(f"Error executing reorder: {e}")
        return False

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
    print("  <Book> <Ch>:<V>     Jump to verse (e.g., 'John 3:16', 'Gen 1:1')")
    print("  e <num> [txt]       Edit single word gloss. Blank ('e 10') sets blank gloss. 'e 1 hello'")
    print("  p <num|range> [pnc] Edit trailing punctuation. Blank ('p 10') clears punctuation.")
    print("  r <num1> <num2>...  Reorder subset or all words (e.g., 'r 5 2' moves word 5 into slot 2)")
    print("  d <num|range>       Reset gloss AND punctuation back to default source values")
    print("  n / p               Next / Previous verse")
    print("  use <code>          Switch translation (e.g., 'use KJV')")
    print("  sync-to-db          Import JSON edits into DB file")
    print("  q                   Quit\n")

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

            # Punctuation edit command (e.g. 'p 10' sets blank punctuation, 'p 10 ,' sets comma)
            if cmd.lower().startswith('p ') and len(cmd.split()) >= 2:
                parts = cmd.split(maxsplit=2)
                range_token = parts[1]
                indices = parse_range_indices(range_token, len(words))
                if indices is not None and len(indices) > 0:
                    pnc_text = parts[2].strip() if len(parts) > 2 else ""
                    for idx in indices:
                        w = words[idx]
                        word_info = (w[0], w[1], w[2], w[3])
                        save_pnc_edit(con, word_info, current_code, cur_book, cur_ch, cur_v, pnc_text, json_file)
                    
                    _, cur_book, cur_ch, cur_v, words = get_passage_words(con, current_code, f"{cur_book} {cur_ch}:{cur_v}")
                    display_verse(cur_book, cur_ch, cur_v, words)
                else:
                    print(f"Invalid range or position '{range_token}'. Must be valid indices (1-{len(words)}).")
                continue

            # Word reordering command (Partial or Full)
            if cmd.lower().startswith('r '):
                parts = cmd.split()[1:]
                if parts:
                    try:
                        input_indices = [int(p) - 1 for p in parts]
                        if len(input_indices) == len(set(input_indices)) and all(0 <= i < len(words) for i in input_indices):
                            if len(input_indices) == len(words):
                                new_order = input_indices
                            else:
                                new_order = expand_partial_reorder(input_indices, len(words))

                            success = reorder_verse(con, current_code, cur_book, cur_ch, cur_v, words, new_order, json_file)
                            if success:
                                _, cur_book, cur_ch, cur_v, words = get_passage_words(con, current_code, f"{cur_book} {cur_ch}:{cur_v}")
                                display_verse(cur_book, cur_ch, cur_v, words)
                        else:
                            print(f"Invalid sequence. Word numbers must be distinct and within 1-{len(words)}.")
                    except ValueError:
                        print("Usage: r <num1> <num2> ... (e.g., 'r 5 2' to put word 5 in word 2's slot)")
                else:
                    print("Usage: r <num1> <num2> ... (e.g., 'r 4 2')")
                continue

            # Single-word gloss edit command ('e 10' sets blank gloss, 'e 10 hello world' sets gloss to 'hello world')
            if cmd.lower().startswith('e '):
                parts = cmd.split(maxsplit=2)
                if len(parts) >= 2 and parts[1].isdigit():
                    idx = int(parts[1]) - 1
                    if 0 <= idx < len(words):
                        w = words[idx]
                        word_info = (w[0], w[1], w[2], w[3])
                        raw_text = parts[2].strip() if len(parts) > 2 else ""
                        save_gloss_edit(con, word_info, current_code, cur_book, cur_ch, cur_v, raw_text, json_file)
                        
                        _, cur_book, cur_ch, cur_v, words = get_passage_words(con, current_code, f"{cur_book} {cur_ch}:{cur_v}")
                        display_verse(cur_book, cur_ch, cur_v, words)
                    else:
                        print(f"Invalid position '{parts[1]}'. Must be between 1 and {len(words)}.")
                else:
                    print("Usage: e <num> [new gloss text] (e.g., 'e 3 hello', 'e 3' for blank)")
                continue

            # Reset both gloss and punctuation edit command
            if cmd.lower().startswith('d '):
                parts = cmd.split(maxsplit=1)
                if len(parts) == 2:
                    range_token = parts[1]
                    indices = parse_range_indices(range_token, len(words))
                    if indices is not None and len(indices) > 0:
                        for idx in indices:
                            w = words[idx]
                            word_info = (w[0], w[1], w[2], w[3])
                            reset_word_edits(con, word_info, current_code, cur_book, cur_ch, cur_v, json_file)
                        
                        _, cur_book, cur_ch, cur_v, words = get_passage_words(con, current_code, f"{cur_book} {cur_ch}:{cur_v}")
                        display_verse(cur_book, cur_ch, cur_v, words)
                    else:
                        print(f"Invalid range or position '{range_token}'. Must be valid indices (1-{len(words)}).")
                else:
                    print("Usage: d <num|range>")
                continue

            # Passage query
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
    parser = argparse.ArgumentParser(description="Terminal Gloss Editor & Reorder Tool for bible.sqlite")
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