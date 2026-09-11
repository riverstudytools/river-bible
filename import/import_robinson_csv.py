#!/usr/bin/env python3
"""
import_robinson_dual_csv.py — Importer for Robinson-Pierpont Byzantine Text using
dual input sources (parsed TSV/CSV with Strong's/morphology + accented CSV with variants).

Usage:
    python3 import_robinson_dual_csv.py path/to/parsed_folder path/to/accented_folder \
        --code BYZ --name "Robinson-Pierpont Byzantine Text (Accented)" --replace
"""

import argparse
import csv
import os
import re
import sqlite3
import sys
import unicodedata
import book_codes
import build_db as db_builder
import build_search_index as bsi

# Isolates leading/trailing punctuation including standard, Greek, and apparatus symbols
WORD_PUNCT_RE = re.compile(
    r"^([\s\.\,;:\·\?\!–—;·\(\)\[\]«»]*)"
    r"([\w'’᾽᾿\-]+)"
    r"([\s\.,;:\·\?\!–—;·\(\)\[\]«»]*)$",
    re.UNICODE
)

# Regex matching unaccented token format: greek_word strong_num {parsing_code}
UNACCENTED_TOKEN_RE = re.compile(r'([^\s{}]+)\s+(\d+(?:[&,/]\d+)?)\s*\{([^}]+)\}')


def strip_accents(s):
    """Normalizes Unicode to NFD, strips diacritical marks, lowercases, and converts final sigmas."""
    if not s:
        return ""
    s_nfd = unicodedata.normalize('NFD', s)
    stripped = ''.join(c for c in s_nfd if unicodedata.category(c) != 'Mn')
    cleaned = re.sub(r'[^\w]', '', stripped, flags=re.UNICODE).lower()
    return cleaned.replace('ς', 'σ')


def format_strongs(raw_strong):
    """Formats raw Strong's integers into standard 'G3972' codes."""
    if not raw_strong:
        return None
    primary_num = re.split(r'[&,/]', str(raw_strong).strip())[0]
    m = re.search(r'\d+', primary_num)
    return f"G{m.group(0)}" if m else None


def parse_unaccented_line(raw_text):
    """Parses unaccented verse line into tokens with Strong's numbers and morphology parsing."""
    tokens = []
    matches = list(UNACCENTED_TOKEN_RE.finditer(raw_text))

    if matches:
        for m in matches:
            greek_token = m.group(1).strip()
            strong_num = m.group(2).strip()
            parsing = m.group(3).strip()
            tokens.append({
                'word': greek_token,
                'norm': strip_accents(greek_token),
                'strong': format_strongs(strong_num),
                'parsing': parsing
            })
    else:
        for token in raw_text.split():
            tokens.append({
                'word': token,
                'norm': strip_accents(token),
                'strong': None,
                'parsing': None
            })

    return tokens


def parse_accented_line(raw_text):
    """
    Extracts reading tokens with accents and punctuation from line,
    attaching inline variant braces {NA ...} to the preceding word's footnote field.
    """
    pattern = re.compile(r'\{[^}]+\}|[^\s{}]+')
    raw_tokens = pattern.findall(raw_text)

    words = []

    for token in raw_tokens:
        if token.startswith('{') and token.endswith('}'):
            # It's a variant note annotation
            note = token[1:-1].strip()
            if words:
                if words[-1]['footnote']:
                    words[-1]['footnote'] += f"; {note}"
                else:
                    words[-1]['footnote'] = note
        else:
            # It's a reading word token
            pm = WORD_PUNCT_RE.match(token)
            if pm:
                beg_q, word_clean, pnc = pm.group(1), pm.group(2), pm.group(3)
            else:
                beg_q, word_clean, pnc = '', token, ''

            words.append({
                'gloss': word_clean,
                'norm': strip_accents(word_clean),
                'beg_q': beg_q,
                'pnc': pnc,
                'footnote': None
            })

    return words


def combine_verse_data(unacc_line, acc_line):
    """Combines unaccented parsing metadata with accented words and footnotes."""
    unacc_tokens = parse_unaccented_line(unacc_line)
    acc_tokens = parse_accented_line(acc_line)

    combined_words = []

    # 1:1 Sequential alignment if token counts match exactly (99% of verses)
    if len(unacc_tokens) == len(acc_tokens):
        for u_tok, a_tok in zip(unacc_tokens, acc_tokens):
            combined_words.append({
                'gloss': a_tok['gloss'],
                'original': a_tok['gloss'],  # Keeps accented text for dictionary lookup
                'strong': u_tok['strong'],
                'parsing': u_tok['parsing'],
                'beg_q': a_tok['beg_q'],
                'pnc': a_tok['pnc'],
                'footnote': a_tok['footnote']
            })
    else:
        # Fallback sequence alignment if token counts differ slightly
        u_idx = 0
        for a_tok in acc_tokens:
            u_match = None
            if u_idx < len(unacc_tokens):
                # Look ahead up to 2 positions for normalized match
                for lookahead in range(min(3, len(unacc_tokens) - u_idx)):
                    if unacc_tokens[u_idx + lookahead]['norm'] == a_tok['norm']:
                        u_idx += lookahead
                        u_match = unacc_tokens[u_idx]
                        u_idx += 1
                        break

            if not u_match and u_idx < len(unacc_tokens):
                u_match = unacc_tokens[u_idx]
                u_idx += 1

            combined_words.append({
                'gloss': a_tok['gloss'],
                'original': a_tok['gloss'],
                'strong': u_match['strong'] if u_match else None,
                'parsing': u_match['parsing'] if u_match else None,
                'beg_q': a_tok['beg_q'],
                'pnc': a_tok['pnc'],
                'footnote': a_tok['footnote']
            })

    return combined_words


def parse_dual_files(unacc_file, acc_file):
    """Reads parallel unaccented and accented files and yields structured verse objects."""
    filename = os.path.basename(acc_file)
    raw_book_code = os.path.splitext(filename)[0].strip()
    canonical_book = book_codes.resolve_canonical_name(raw_book_code)

    if not canonical_book:
        print(f"Error: Could not resolve book name for '{filename}'. Skipping.", file=sys.stderr)
        return []

    # Read unaccented CSV lines into lookup dictionary: (chapter, verse) -> text
    unacc_map = {}
    with open(unacc_file, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or len(row) < 3 or row[0].strip().lower() in ('chapter', 'ch', 'c'):
                continue
            try:
                ch, v = int(row[0].strip()), int(row[1].strip())
                unacc_map[(ch, v)] = row[2].strip()
            except ValueError:
                continue

    parsed_verses = []

    # Read accented CSV lines and merge
    with open(acc_file, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.reader(f)
        for row in reader:
            if not row or len(row) < 3 or row[0].strip().lower() in ('chapter', 'ch', 'c'):
                continue
            try:
                ch, v = int(row[0].strip()), int(row[1].strip())
            except ValueError:
                continue

            acc_text = row[2].strip()
            unacc_text = unacc_map.get((ch, v), '')

            words = combine_verse_data(unacc_text, acc_text)
            if words:
                parsed_verses.append({
                    'book': canonical_book,
                    'chapter': ch,
                    'verse': v,
                    'words': words
                })

    return parsed_verses


def process_input_directories(unacc_dir, acc_dir, con, book_id_by_name, book_order_map):
    all_verses = []

    acc_files = [f for f in os.listdir(acc_dir) if f.lower().endswith(('.csv', '.tsv', '.txt'))]

    for filename in acc_files:
        acc_path = os.path.join(acc_dir, filename)
        unacc_path = os.path.join(unacc_dir, filename)

        if not os.path.exists(unacc_path):
            print(f"Warning: Corresponding unaccented file for '{filename}' not found in {unacc_dir}. Skipping.", file=sys.stderr)
            continue

        records = parse_dual_files(unacc_path, acc_path)
        for rec in records:
            book_name = rec['book']

            if book_name in book_id_by_name:
                book_id = book_id_by_name[book_name]
                b_order = book_order_map[book_name]
            else:
                testament = bsi.testament_for(book_name)
                next_order = max(book_order_map.values(), default=-1) + 1
                cur = con.execute('INSERT INTO books(name, testament, book_order) VALUES (?,?,?)', (book_name, testament, next_order))
                book_id = cur.lastrowid
                b_order = next_order
                book_id_by_name[book_name] = book_id
                book_order_map[book_name] = b_order

            rec['book_id'] = book_id
            rec['book_order'] = b_order
            all_verses.append(rec)

    all_verses.sort(key=lambda x: (x['book_order'], x['chapter'], x['verse']))
    return all_verses


def import_robinson_translation(con, translation_id, verse_records):
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

        words_batch = [
            (
                verse_id, pos, 'Greek',
                w_obj['original'], None, w_obj['parsing'], w_obj['strong'],
                w_obj['gloss'], w_obj['beg_q'], w_obj['pnc'], '', '',
                w_obj['footnote'], None, None, None
            )
            for pos, w_obj in enumerate(rec['words'])
        ]

        con.executemany(
            """INSERT INTO words(
                verse_id, position, lang, original, translit, parsing, strong,
                gloss, beg_q, pnc, end_q, end_text, footnote, hdg, crossref, par_classes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            words_batch
        )
        words_inserted += len(words_batch)

    return verses_inserted, words_inserted


def main():
    ap = argparse.ArgumentParser(description="Import Accented Robinson-Pierpont GNT into River SQLite DB")
    ap.add_argument('unaccented_dir', help='Folder containing unaccented CSV files with Strongs/parsing')
    ap.add_argument('accented_dir', help='Folder containing accented CSV files with variants')
    ap.add_argument('--code', default='BYZ', help='Translation code (default: BYZ)')
    ap.add_argument('--name', default='Robinson-Pierpont Byzantine Text (Accented)', help='Display name')
    ap.add_argument('--language', default='Greek', help='Language of text')
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

            verse_records = process_input_directories(args.unaccented_dir, args.accented_dir, con, book_id_by_name, book_order_map)

            if not verse_records:
                sys.exit("Error: No valid verses could be parsed from input directories.")

            v_count, w_count = import_robinson_translation(con, translation_id, verse_records)

        print(f'Successfully imported "{args.name}" ({args.code}): {v_count} verses, {w_count} words into {args.db}.')

    except Exception as e:
        sys.exit(f"Database import failed: {e}")
    finally:
        con.close()


if __name__ == '__main__':
    main()