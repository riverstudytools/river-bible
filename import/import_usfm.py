#!/usr/bin/env python3
"""
import_usfm.py — Production-hardened importer for USFM (Unified Standard Format Markers)
Bible files into River's SQLite database (bible.sqlite).

Uses book_codes.py for centralized book resolution and build_db.py for schema management.

Usage:
    python3 import_usfm.py path/to/usfm_folder_or_file --code WEB --name "World English Bible" --replace
"""

import argparse
import os
import re
import sqlite3
import sys
import book_codes
import build_db as db_builder

# Isolates leading/trailing punctuation while preserving internal apostrophes, hyphens, and curly quotes
WORD_PUNCT_RE = re.compile(r"^([^\w]*)([\w'\u2019\-]+)([^\w]*)$", re.UNICODE)


def ensure_book_in_db(con, book_name, book_id_by_name, book_order_map):
    """Ensures dynamic insertion of canonical or Apocryphal books into the database."""
    if book_name in book_id_by_name:
        return book_id_by_name[book_name], book_order_map[book_name]

    testament = db_builder.testament_for(book_name)
    next_order = max(book_order_map.values(), default=-1) + 1

    cur = con.execute(
        'INSERT INTO books(name, testament, book_order) VALUES (?,?,?)',
        (book_name, testament, next_order)
    )
    book_id = cur.lastrowid
    book_id_by_name[book_name] = book_id
    book_order_map[book_name] = next_order

    print(f"Info: Dynamically added book '{book_name}' (ID={book_id}, testament='{testament}').")
    return book_id, next_order


def parse_usfm_word_tokens(raw_text):
    """
    Parses verse text into structured word objects with explicit whitespace stripping
    and punctuation separation (beg_q, gloss, pnc).
    """
    footnotes = re.findall(r'\\f\s+.*?(?:\\ft\s+([^\\*]+)|\\f\*)', raw_text)
    crossrefs = re.findall(r'\\x\s+.*?(?:\\xt\s+([^\\*]+)|\\x\*)', raw_text)

    clean_text = re.sub(r'\\f\s+.*?(\\f\*|$)', '', raw_text)
    clean_text = re.sub(r'\\x\s+.*?(\\x\*|$)', '', clean_text)

    parsed_words = []
    # Match standard \w ... \w* as well as nested \+w ... \+w* tags
    usfm_w_pattern = re.compile(r'\\\+?w\s+([^|\\]+)(?:\|([^\\*]*))?\\\+?w\*')

    def replace_usfm_w(match):
        english_word = match.group(1).strip()
        attr_str = match.group(2) or ''

        lemma_match = re.search(r'lemma="([^"]+)"', attr_str)
        original = lemma_match.group(1).strip() if lemma_match else None

        strong_match = re.search(r'strong="([^"]+)"', attr_str)
        strong = strong_match.group(1).strip() if strong_match else None

        idx = len(parsed_words)
        parsed_words.append({
            'raw_token': english_word,
            'original': original,
            'strong': strong
        })
        return f"__USFM_WORD_{idx}__"

    processed_text = usfm_w_pattern.sub(replace_usfm_w, clean_text)
    # Strip remaining USFM tags including nested '+' prefixed tags
    processed_text = re.sub(r'\\\+?[a-z0-9-]+\*?', ' ', processed_text)

    final_words = []
    for token in processed_text.split():
        token = token.strip()
        if not token:
            continue

        m_placeholder = re.search(r'^(.*?)__USFM_WORD_(\d+)__(.*?)$', token)
        if m_placeholder:
            prefix_punct = m_placeholder.group(1)
            idx = int(m_placeholder.group(2))
            suffix_punct = m_placeholder.group(3)

            w_obj = parsed_words[idx]
            raw = w_obj['raw_token']
            orig = w_obj['original']
            st = w_obj['strong']

            m = WORD_PUNCT_RE.match(raw)
            if m:
                beg_q, gloss, pnc = m.group(1), m.group(2), m.group(3)
            else:
                beg_q, gloss, pnc = '', raw, ''

            beg_q = (prefix_punct + beg_q).strip()
            pnc = (pnc + suffix_punct).strip()
        else:
            orig = None
            st = None
            m = WORD_PUNCT_RE.match(token)
            if m:
                beg_q, gloss, pnc = m.group(1), m.group(2), m.group(3)
            else:
                beg_q, gloss, pnc = '', token, ''

            beg_q = beg_q.strip()
            gloss = gloss.strip()
            pnc = pnc.strip()

        if not gloss and (beg_q or pnc):
            if final_words:
                final_words[-1]['pnc'] = (final_words[-1]['pnc'] + beg_q + pnc).strip()
                continue
            else:
                beg_q = (beg_q + pnc).strip()
                pnc = ''

        final_words.append({
            'gloss': gloss,
            'beg_q': beg_q,
            'pnc': pnc,
            'original': orig if orig else gloss,  # Fix popovers by providing fallback
            'strong': st,
            'footnote': None,
            'crossref': None
        })

    if final_words:
        fn_text = ' '.join(f[0] or f[1] for f in footnotes if any(f)).strip()
        xr_text = ' '.join(c[0] or c[1] for c in crossrefs if any(c)).strip()
        if fn_text:
            final_words[-1]['footnote'] = fn_text
        if xr_text:
            final_words[-1]['crossref'] = xr_text

    return final_words


def parse_usfm_file(file_path):
    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    lines = content.splitlines()

    current_book_raw = None
    current_chapter = 0
    current_heading = None
    pending_paragraph = False

    parsed_verses = []
    current_verse_range = []
    current_verse_tokens = []

    def flush_verse():
        nonlocal current_verse_range, current_verse_tokens, current_heading, pending_paragraph
        if current_verse_range and current_verse_tokens:
            raw_text = ' '.join(current_verse_tokens)
            words = parse_usfm_word_tokens(raw_text)

            if words:
                fts_plain_text = ' '.join(w['gloss'] for w in words)
                for v_num in current_verse_range:
                    parsed_verses.append({
                        'book_raw': current_book_raw,
                        'chapter': current_chapter,
                        'verse': v_num,
                        'words': words,
                        'fts_text': fts_plain_text,
                        'heading': current_heading,
                        'is_paragraph': pending_paragraph
                    })
                current_heading = None
                pending_paragraph = False
        current_verse_tokens = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        if line.startswith('\\id'):
            m_id = re.search(r'\\id\s+([A-Z0-9]{3})', line, re.IGNORECASE)
            if m_id:
                current_book_raw = m_id.group(1).upper()
            continue

        if line.startswith('\\c'):
            flush_verse()
            m = re.match(r'\\c\s+(\d+)', line)
            if m:
                current_chapter = int(m.group(1))
                current_verse_range = []
            continue

        if line.startswith('\\s') or line.startswith('\\h'):
            heading_text = re.sub(r'^\\[a-z0-9]+\s*', '', line).strip()
            current_heading = heading_text
            continue

        if re.match(r'^\\(p|m|q\d?|b)(\s|$)', line):
            pending_paragraph = True
            line = re.sub(r'^\\[a-z0-9]+\s*', '', line).strip()
            if not line:
                continue

        v_match = re.match(r'\\v\s+([0-9a-zA-Z\-,]+)\s*(.*)', line)
        if v_match:
            flush_verse()
            raw_v_spec = v_match.group(1)
            found_nums = [int(n) for n in re.findall(r'\d+', raw_v_spec)]

            if len(found_nums) >= 2:
                current_verse_range = list(range(found_nums[0], found_nums[-1] + 1))
            elif len(found_nums) == 1:
                current_verse_range = [found_nums[0]]
            else:
                current_verse_range = []

            verse_text = v_match.group(2)
            if verse_text:
                current_verse_tokens.append(verse_text)
            continue

        if current_verse_range:
            current_verse_tokens.append(line)

    flush_verse()
    return parsed_verses


def process_usfm_input(input_path, con, book_id_by_name, book_order_map):
    all_verses = []

    if os.path.isfile(input_path):
        files = [input_path]
    elif os.path.isdir(input_path):
        files = [
            os.path.join(input_path, f) for f in os.listdir(input_path)
            if f.lower().endswith(('.usfm', '.sfm', '.txt'))
        ]
    else:
        sys.exit(f"Error: Path '{input_path}' not found.")

    for f_path in files:
        records = parse_usfm_file(f_path)
        for rec in records:
            if not rec['book_raw']:
                continue

            raw_code = rec['book_raw'].upper()
            canonical_book = book_codes.resolve_canonical_name(raw_code)

            if not canonical_book:
                print(f"Warning: Could not resolve USFM book code '{raw_code}'. Skipping.", file=sys.stderr)
                continue

            book_id, b_order = ensure_book_in_db(con, canonical_book, book_id_by_name, book_order_map)

            rec['book'] = canonical_book
            rec['book_id'] = book_id
            rec['book_order'] = b_order
            all_verses.append(rec)

    all_verses.sort(key=lambda x: (x['book_order'], x['chapter'], x['verse']))
    return all_verses


def import_usfm_translation(con, translation_id, verse_records):
    verses_inserted = 0
    words_inserted = 0

    for rec in verse_records:
        existing_verse = con.execute(
            'SELECT id FROM verses WHERE translation_id=? AND book_id=? AND chapter=? AND verse=?',
            (translation_id, rec['book_id'], rec['chapter'], rec['verse'])
        ).fetchone()

        if existing_verse:
            verse_id = existing_verse[0]
            con.execute('DELETE FROM words WHERE verse_id=?', (verse_id,))
        else:
            cur_row = con.execute(
                'INSERT INTO verses(translation_id, book_id, chapter, verse) VALUES (?,?,?,?)',
                (translation_id, rec['book_id'], rec['chapter'], rec['verse'])
            )
            verse_id = cur_row.lastrowid
            verses_inserted += 1

        testament = db_builder.testament_for(rec['book'])

        for pos, w_obj in enumerate(rec['words']):
            par_classes = 'paragraph' if (pos == 0 and rec.get('is_paragraph')) else None
            hdg = rec['heading'] if pos == 0 else None

            if w_obj.get('strong'):
                word_lang = 'Hebrew' if w_obj['strong'].startswith('H') else 'Greek' if w_obj['strong'].startswith('G') else 'English'
            elif w_obj.get('original'):
                word_lang = 'Hebrew' if testament == 'OT' else 'Greek'
            else:
                word_lang = 'English'

            con.execute(
                """INSERT INTO words(
                    verse_id, position, lang, original, translit, parsing, strong,
                    gloss, beg_q, pnc, end_q, end_text, footnote, hdg, crossref, par_classes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    verse_id, pos, word_lang,
                    w_obj.get('original'),
                    None, None,
                    w_obj.get('strong'),
                    w_obj['gloss'],
                    w_obj['beg_q'],
                    w_obj['pnc'],
                    '', '',
                    w_obj.get('footnote'),
                    hdg,
                    w_obj.get('crossref'),
                    par_classes
                )
            )
            words_inserted += 1

    return verses_inserted, words_inserted


def main():
    ap = argparse.ArgumentParser(description="Import USFM Bible files into River SQLite DB")
    ap.add_argument('input', help='Path to USFM file or folder containing USFM files')
    ap.add_argument('--code', required=True, help='Translation code (e.g. WEB, BSST)')
    ap.add_argument('--name', required=True, help='Display name (e.g. "World English Bible")')
    ap.add_argument('--language', default='English', help='Language of translation')
    ap.add_argument('--db', default='bible.sqlite', help='SQLite DB path')
    ap.add_argument('--replace', action='store_true', help='Replace existing translation')
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=20.0)
    con.execute('PRAGMA foreign_keys = ON')

    try:
        with con:
            db_builder.ensure_schema(con)

            existing = con.execute('SELECT id FROM translations WHERE code=?', (args.code,)).fetchone()
            if existing and not args.replace:
                sys.exit(f'Error: Translation code "{args.code}" exists. Use --replace to overwrite.')

            if existing:
                translation_id = existing[0]
                con.execute('UPDATE translations SET name=?, language=? WHERE id=?', (args.name, args.language, translation_id))
                db_builder.wipe_translation(con, translation_id)
            else:
                cur = con.execute('INSERT INTO translations(code, name, language) VALUES (?,?,?)',
                                  (args.code, args.name, args.language))
                translation_id = cur.lastrowid

            books_data = con.execute('SELECT name, id, book_order FROM books').fetchall()
            book_id_by_name = {row[0]: row[1] for row in books_data}
            book_order_map = {row[0]: row[2] for row in books_data}

            print("Parsing USFM files...")
            verse_records = process_usfm_input(args.input, con, book_id_by_name, book_order_map)

            if not verse_records:
                sys.exit("Error: No valid verses could be parsed from input USFM file(s).")

            print("Importing verses into database...")
            v_count, w_count = import_usfm_translation(con, translation_id, verse_records)

        print(f'Successfully imported "{args.name}" ({args.code}): {v_count} verses, {w_count} words into {args.db}.')

    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(f"Database import failed: {e}")
    finally:
        con.close()


if __name__ == '__main__':
    main()