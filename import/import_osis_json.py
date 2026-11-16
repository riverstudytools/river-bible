#!/usr/bin/env python3
"""
import_osis_json.py — Importer for JSON Bible files with embedded OSIS XML tags into bible.sqlite.

Handles OSIS inline markup (<w lemma="..." morph="...">phrase</w> and <transChange type="added">),
normalizes Strong's numbers (OT/NT), extracts Robinson morphology, captures lemmas, strips 
leading/trailing punctuation onto word records, and imports verses.

Usage:
    python3 import_osis_json.py path/to/bible.json --code KJV_OSIS --name "King James Version (OSIS)" --replace
"""

import argparse
import json
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET

import book_codes
import build_db as db_builder

# Matches leading non-word, word content, and trailing non-word/punctuation
WORD_PUNCT_RE = re.compile(
    r"^([^\w'’᾽᾿\-]*)"
    r"([\w'’᾽᾿\-]+)?"
    r"(.*)$",
    re.UNICODE
)


def clean_strongs(lemma_attr):
    """Extracts Strong's codes (e.g. 'strong:G976 lemma.TR:βιβλος' -> 'G976')."""
    if not lemma_attr:
        return None
    codes = []
    for part in lemma_attr.split():
        if part.startswith('strong:'):
            cleaned = re.sub(r'^strong:', '', part).strip()
            cleaned = re.sub(r'^([GH])0+', r'\1', cleaned)
            if cleaned:
                codes.append(cleaned)
    return ' '.join(codes) if codes else None


def clean_lemma(lemma_attr):
    """Extracts the lemma string (e.g. 'strong:G976 lemma.TR:βιβλος' -> 'βιβλος')."""
    if not lemma_attr:
        return None
    for part in lemma_attr.split():
        if 'lemma.' in part:
            return part.split(':', 1)[-1].strip()
    return None


def clean_morph(morph_attr):
    """Extracts morphology codes (e.g. 'robinson:N-NSF' -> 'N-NSF')."""
    if not morph_attr:
        return None
    codes = []
    for part in morph_attr.split():
        cleaned = re.sub(r'^(strongMorph|robinson):', '', part).strip()
        if cleaned:
            codes.append(cleaned)
    return ' '.join(codes) if codes else None


def tokenize_and_add(words, text, strong=None, parsing=None, lemma_word=None):
    """Tokenizes raw text or XML element text into individual word items."""
    if not text:
        return

    tokens = text.split()
    for idx, token in enumerate(tokens):
        pm = WORD_PUNCT_RE.match(token)
        if pm:
            beg_q, word_clean, pnc = pm.groups()
        else:
            beg_q, word_clean, pnc = '', token, ''

        # Add space to punctuation if it's not the last token in a sequence
        trailing_pnc = pnc + (' ' if idx < len(tokens) - 1 else '')

        if word_clean:
            words.append({
                'original': lemma_word or word_clean,
                'gloss': word_clean,
                'strong': strong,
                'parsing': parsing,
                'beg_q': beg_q or '',
                'pnc': trailing_pnc,
                'footnote': ''
            })
        elif words and (beg_q or trailing_pnc):
            words[-1]['pnc'] += (beg_q + trailing_pnc)


def parse_verse_text(html_text):
    """Parses OSIS tagged XML text inside a verse into word tokens with punctuation and footnotes."""
    wrapped_xml = f"<root>{html_text}</root>"
    
    try:
        root = ET.fromstring(wrapped_xml)
    except ET.ParseError:
        sanitized = html_text.replace('&', '&amp;')
        try:
            root = ET.fromstring(f"<root>{sanitized}</root>")
        except ET.ParseError:
            return []

    words = []

    def traverse(elem, inherit_strong=None, inherit_parsing=None, inherit_lemma=None):
        if elem.tag == 'note':
            note_content = "".join(elem.itertext()).strip()
            if words and note_content:
                existing_note = words[-1]['footnote']
                words[-1]['footnote'] = f"{existing_note} {note_content}".strip() if existing_note else note_content
            
            if elem.tail:
                tokenize_and_add(words, elem.tail)
            return

        own_strong = clean_strongs(elem.attrib.get('lemma'))
        own_parsing = clean_morph(elem.attrib.get('morph'))
        own_lemma_word = clean_lemma(elem.attrib.get('lemma'))

        strong = own_strong if own_strong is not None else inherit_strong
        parsing = own_parsing if own_parsing is not None else inherit_parsing
        lemma_word = own_lemma_word if own_lemma_word is not None else inherit_lemma

        if elem.text:
            tokenize_and_add(words, elem.text, strong, parsing, lemma_word)

        for child in elem:
            traverse(child, strong, parsing, lemma_word)

        if elem.tail and elem.tag != 'root':
            tokenize_and_add(words, elem.tail)

    traverse(root)

    return words


def import_osis_json(con, translation_id, json_path, book_id_by_name, book_order_map):
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    verses_inserted = 0
    words_inserted = 0

    books_list = data.get('books', [])
    for b_data in books_list:
        raw_book = b_data.get('name')
        canonical_book = book_codes.resolve_canonical_name(raw_book)
        
        if not canonical_book:
            print(f"Skipping unknown book: {raw_book}", file=sys.stderr)
            continue

        if canonical_book in book_id_by_name:
            book_id = book_id_by_name[canonical_book]
        else:
            testament = db_builder.testament_for(canonical_book)
            next_order = max(book_order_map.values(), default=-1) + 1
            cur = con.execute(
                'INSERT INTO books(name, testament, book_order) VALUES (?,?,?)',
                (canonical_book, testament, next_order)
            )
            book_id = cur.lastrowid
            book_id_by_name[canonical_book] = book_id
            book_order_map[canonical_book] = next_order

        for ch_data in b_data.get('chapters', []):
            chapter_num = ch_data.get('chapter')

            for v_data in ch_data.get('verses', []):
                verse_num = v_data.get('verse')
                raw_text = v_data.get('text', '')

                cur_row = con.execute(
                    'INSERT INTO verses(translation_id, book_id, chapter, verse) VALUES (?,?,?,?)',
                    (translation_id, book_id, chapter_num, verse_num)
                )
                verse_id = cur_row.lastrowid
                verses_inserted += 1

                parsed_words = parse_verse_text(raw_text)

                words_batch = [
                    (
                        verse_id, pos, None,
                        w['original'], None, w['parsing'], w['strong'],
                        w['gloss'], w['beg_q'], w['pnc'], '', '',
                        w['footnote'], None, None, None
                    )
                    for pos, w in enumerate(parsed_words)
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
    ap = argparse.ArgumentParser(description="Import OSIS JSON into River SQLite DB")
    ap.add_argument('json_file', help='Path to OSIS JSON file')
    ap.add_argument('--code', required=True, help='Short unique translation code, e.g. KJV_OSIS')
    ap.add_argument('--name', required=True, help='Display name, e.g. "King James Version"')
    ap.add_argument('--language', default='English', help='Translation language')
    ap.add_argument('--db', default='bible.sqlite', help='SQLite DB path')
    ap.add_argument('--replace', action='store_true', help='Overwrite translation if code exists')
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.execute('PRAGMA foreign_keys = ON')

    try:
        with con:
            db_builder.ensure_schema(con)

            existing = con.execute('SELECT id FROM translations WHERE code=?', (args.code,)).fetchone()
            if existing and not args.replace:
                sys.exit(f'Error: Translation code "{args.code}" already exists in {args.db}. Use --replace to overwrite.')

            if existing:
                translation_id = existing[0]
                con.execute('UPDATE translations SET name=?, language=? WHERE id=?', (args.name, args.language, translation_id))
                db_builder.wipe_translation(con, translation_id)
            else:
                cur = con.execute(
                    'INSERT INTO translations(code, name, language) VALUES (?,?,?)',
                    (args.code, args.name, args.language)
                )
                translation_id = cur.lastrowid

            books_data = con.execute('SELECT name, id, book_order FROM books').fetchall()
            book_id_by_name = {row[0]: row[1] for row in books_data}
            book_order_map = {row[0]: row[2] for row in books_data}

            v_count, w_count = import_osis_json(con, translation_id, args.json_file, book_id_by_name, book_order_map)

        print(f'Successfully imported "{args.name}" ({args.code}): {v_count} verses, {w_count} words into {args.db}.')

    finally:
        con.close()


if __name__ == '__main__':
    main()