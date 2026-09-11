#!/usr/bin/env python3
"""
import_oshb.py — Importer for the Open Scriptures Hebrew Bible (OSHB),
i.e. the Westminster Leningrad Codex with lemma/Strong's numbers and full
morphology, into River's SQLite DB.

Source: https://github.com/openscriptures/morphhb (the `wlc/` directory,
one OSIS XML file per book, e.g. wlc/Gen.xml ... wlc/Mal.xml).
Lemma/morphology data: CC BY 4.0 (credit "Open Scriptures Hebrew Bible
Project"). The underlying WLC text itself: Public Domain.

Verified against the real wlc/Ruth.xml file while writing this (not
guessed). Key format facts about the OSHB word markup:

  - Each word is a <w lemma="..." morph="..." id="...">TEXT</w> element.
    TEXT itself contains literal "/" characters marking morpheme
    boundaries inside the word (e.g. "וַ/יְהִ֗י" = conjunction + root).
    These slashes are NOT punctuation — they're stripped for display,
    same as this script does for the `original`/`gloss` fields.

  - lemma is one or more "/"-separated morphemes, e.g. "c/1961" (waw
    conjunction + Strong's 1961) or "b/1035+" (preposition + construct
    Strong's 1035, "+" meaning the construct chain continues into the
    next word — the "+" is just stripped here, since each word is still
    its own row). Grammatical morphemes are lower-case letters with no
    digits (c, b, l, k, m, d, i, u, etc. — conjunction/prepositions/
    article/interrogative-he); lexical morphemes are digits, optionally
    followed by a disambiguating letter for homographs (e.g. "3588 a").
    This importer keeps the *last* digit-bearing morpheme as the word's
    primary Strong's number (the root), formatted "H<digits>", and
    drops the homograph letter (Strong's dictionaries aren't split by
    homograph — same convention already used for Greek in this project
    via format_strongs()).

  - morph starts with a literal "H" (Hebrew) prefix, e.g. "HC/Vqw3ms".
    Stored in the `parsing` column with the leading H stripped, so it
    matches the same "C/Vqw3ms" style already used for Greek parsing
    codes elsewhere in the schema.

  - Ketiv/Qere: the written form (ketiv) appears as
    <w type="x-ketiv">...</w> immediately followed by
    <note type="variant"><catchWord>.../<rdg type="x-qere"><w>...</w>
    </rdg></note>. This importer displays the Qere (the traditionally
    *read* form, which carries the vowel pointing and is what virtually
    every Hebrew Bible reader/printed edition shows) and records the
    Ketiv spelling in the footnote field as "Ketiv: <form>", rather than
    silently dropping one of the two readings.

  - <seg type="x-sof-pasuq">׃</seg> (verse-end marker) and
    <seg type="x-paseq">׀</seg> attach as trailing punctuation (`pnc`)
    to the previous word. <seg type="x-maqqef">־</seg> (the hyphen that
    binds two words into one accentual unit) is likewise attached as
    `pnc` on the preceding word rather than becoming its own token.

  - <note> elements (textual-critical annotations, e.g. "We read one or
    more accents in L differently from BHQ") are skipped — they're
    editorial notes about the Masoretic reading tradition, not verse
    footnotes in the usual sense, and there can be several stacked in a
    row with no clean way to attach them to one specific word.

  - `gloss` is set equal to the cleaned `original` text (same fallback
    pattern already used elsewhere in this project for un-glossed
    original-language words), so nothing renders blank.

Usage:
    python3 import_oshb.py path/to/wlc_directory \
        --code WLC --name "Westminster Leningrad Codex" --replace

    (wlc_directory should contain the book-named XML files exactly as
    they appear in openscriptures/morphhb's wlc/ folder, e.g. Gen.xml,
    Exod.xml, Ruth.xml, ... Mal.xml.)
"""

import argparse
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET

import book_codes
import build_db as db_builder

# ---------------------------------------------------------------------------
# OSHB uses its own short book-file names (Gen, Exod, Lev, Num, Deut, Josh,
# Judg, Ruth, 1Sam, 2Sam, 1Kgs, 2Kgs, 1Chr, 2Chr, Ezra, Neh, Esth, Job, Ps,
# Prov, Eccl, Song, Isa, Jer, Lam, Ezek, Dan, Hos, Joel, Amos, Obad, Jonah,
# Mic, Nah, Hab, Zeph, Hag, Zech, Mal) which don't all match the osisID
# inside the file 1:1 in a way book_codes.resolve_canonical_name() can
# necessarily be trusted to guess — so this maps from the <div osisID="...">
# value actually found inside each file, which is the authoritative source.
OSISID_BOOK_MAP = {
    'Gen': 'Genesis', 'Exod': 'Exodus', 'Lev': 'Leviticus', 'Num': 'Numbers',
    'Deut': 'Deuteronomy', 'Josh': 'Joshua', 'Judg': 'Judges', 'Ruth': 'Ruth',
    '1Sam': '1 Samuel', '2Sam': '2 Samuel', '1Kgs': '1 Kings', '2Kgs': '2 Kings',
    '1Chr': '1 Chronicles', '2Chr': '2 Chronicles', 'Ezra': 'Ezra', 'Neh': 'Nehemiah',
    'Esth': 'Esther', 'Job': 'Job', 'Ps': 'Psalms', 'Prov': 'Proverbs',
    'Eccl': 'Ecclesiastes', 'Song': 'Song of Solomon', 'Isa': 'Isaiah',
    'Jer': 'Jeremiah', 'Lam': 'Lamentations', 'Ezek': 'Ezekiel', 'Dan': 'Daniel',
    'Hos': 'Hosea', 'Joel': 'Joel', 'Amos': 'Amos', 'Obad': 'Obadiah',
    'Jonah': 'Jonah', 'Mic': 'Micah', 'Nah': 'Nahum', 'Hab': 'Habakkuk',
    'Zeph': 'Zephaniah', 'Hag': 'Haggai', 'Zech': 'Zechariah', 'Mal': 'Malachi',
}

NS = '{http://www.bibletechnologies.net/2003/OSIS/namespace}'


def tag(elem):
    """Strip the OSIS namespace off a tag name, e.g. '{...}w' -> 'w'."""
    t = elem.tag
    return t.split('}')[-1] if '}' in t else t


def clean_word_text(raw):
    """Strips the morpheme-boundary '/' markers OSHB embeds in word text."""
    return raw.replace('/', '') if raw else ''


LEMMA_NUM_RE = re.compile(r'^(\d+)')


def parse_lemma(lemma_attr):
    """
    Returns the primary Strong's number (formatted 'H1234') for a lemma
    attribute like 'c/1961', '3588 a', 'm/1035+', or None if the lemma
    has no digit-bearing morpheme at all.
    """
    if not lemma_attr:
        return None
    morphemes = lemma_attr.split('/')
    primary = None
    for m in morphemes:
        m = m.strip().rstrip('+')
        num_match = LEMMA_NUM_RE.match(m)
        if num_match:
            primary = num_match.group(1)
    return f'H{primary}' if primary else None


def parse_morph(morph_attr):
    """Strips the leading 'H' language marker from an OSHB morph code."""
    if not morph_attr:
        return None
    return morph_attr[1:] if morph_attr.startswith('H') else morph_attr


def get_testament(book_name):
    """Fallback determination for a book's testament."""
    return 'OT' if book_name in OSISID_BOOK_MAP.values() else 'NT'


def load_book_words(path):
    """
    Yields dicts, one per (chapter, verse), each with a 'words' list of
    per-word dicts: original, strong, parsing, footnote, pnc.
    """
    tree = ET.parse(path)
    root = tree.getroot()

    book_div = None
    for div in root.iter():
        if tag(div) == 'div' and div.attrib.get('type') == 'book':
            book_div = div
            break
    if book_div is None:
        print(f'Warning: no <div type="book"> found in {path}, skipping.', file=sys.stderr)
        return

    osis_book_id = book_div.attrib.get('osisID', '')
    canonical_book = OSISID_BOOK_MAP.get(osis_book_id) or book_codes.resolve_canonical_name(osis_book_id)
    if not canonical_book:
        print(f'Warning: could not resolve book name for osisID "{osis_book_id}" in {path}, skipping.', file=sys.stderr)
        return

    for chapter_elem in book_div:
        if tag(chapter_elem) != 'chapter':
            continue
        chapter_osis = chapter_elem.attrib.get('osisID', '')
        chapter_num_match = re.search(r'\.(\d+)$', chapter_osis)
        if not chapter_num_match:
            continue
        chapter_num = int(chapter_num_match.group(1))

        for verse_elem in chapter_elem:
            if tag(verse_elem) != 'verse':
                continue
            verse_osis = verse_elem.attrib.get('osisID', '')
            verse_num_match = re.search(r'\.(\d+)$', verse_osis)
            if not verse_num_match:
                continue
            verse_num = int(verse_num_match.group(1))

            words = []
            i = 0
            children = list(verse_elem)
            n = len(children)
            while i < n:
                child = children[i]
                ctag = tag(child)

                if ctag == 'w':
                    is_ketiv = child.attrib.get('type') == 'x-ketiv'

                    if is_ketiv:
                        ketiv_text = clean_word_text(''.join(child.itertext()))
                        qere_w = None
                        if i + 1 < n and tag(children[i + 1]) == 'note':
                            note_elem = children[i + 1]
                            for sub in note_elem.iter():
                                if tag(sub) == 'w':
                                    qere_w = sub
                                    break
                            i += 1
                        if qere_w is not None:
                            words.append({
                                'original': clean_word_text(''.join(qere_w.itertext())),
                                'strong': parse_lemma(qere_w.attrib.get('lemma')),
                                'parsing': parse_morph(qere_w.attrib.get('morph')),
                                'footnote': f'Ketiv: {ketiv_text}',
                                'pnc': '',
                            })
                        else:
                            words.append({
                                'original': ketiv_text,
                                'strong': parse_lemma(child.attrib.get('lemma')),
                                'parsing': parse_morph(child.attrib.get('morph')),
                                'footnote': None,
                                'pnc': '',
                            })
                    else:
                        words.append({
                            'original': clean_word_text(''.join(child.itertext())),
                            'strong': parse_lemma(child.attrib.get('lemma')),
                            'parsing': parse_morph(child.attrib.get('morph')),
                            'footnote': None,
                            'pnc': '',
                        })

                elif ctag == 'seg':
                    seg_type = child.attrib.get('type', '')
                    seg_text = (child.text or '').strip()
                    if seg_type in ('x-sof-pasuq', 'x-maqqef', 'x-paseq') and words:
                        words[-1]['pnc'] = (words[-1]['pnc'] + seg_text) if seg_text else words[-1]['pnc']

                elif ctag == 'note':
                    pass

                i += 1

            if words:
                yield {
                    'book': canonical_book,
                    'chapter': chapter_num,
                    'verse': verse_num,
                    'words': words,
                }


def process_directory(wlc_dir, con, book_id_by_name, book_order_map):
    all_verses = []
    xml_files = sorted(f for f in os.listdir(wlc_dir) if f.lower().endswith('.xml'))

    for filename in xml_files:
        path = os.path.join(wlc_dir, filename)
        for rec in load_book_words(path):
            book_name = rec['book']
            if book_name in book_id_by_name:
                book_id = book_id_by_name[book_name]
                b_order = book_order_map[book_name]
            else:
                testament = get_testament(book_name)
                next_order = max(book_order_map.values(), default=-1) + 1
                cur = con.execute(
                    'INSERT INTO books(name, testament, book_order) VALUES (?,?,?)',
                    (book_name, testament, next_order)
                )
                book_id = cur.lastrowid
                b_order = next_order
                book_id_by_name[book_name] = book_id
                book_order_map[book_name] = b_order

            rec['book_id'] = book_id
            rec['book_order'] = b_order
            all_verses.append(rec)

    all_verses.sort(key=lambda x: (x['book_order'], x['chapter'], x['verse']))
    return all_verses


def import_oshb_translation(con, translation_id, verse_records):
    verses_inserted = words_inserted = 0

    for rec in verse_records:
        existing = con.execute(
            'SELECT id FROM verses WHERE translation_id=? AND book_id=? AND chapter=? AND verse=?',
            (translation_id, rec['book_id'], rec['chapter'], rec['verse'])
        ).fetchone()
        if existing:
            verse_id = existing[0]
            con.execute('DELETE FROM words WHERE verse_id=?', (verse_id,))
        else:
            cur = con.execute(
                'INSERT INTO verses(translation_id, book_id, chapter, verse) VALUES (?,?,?,?)',
                (translation_id, rec['book_id'], rec['chapter'], rec['verse'])
            )
            verse_id = cur.lastrowid
            verses_inserted += 1

        words_batch = [
            (
                verse_id, pos, 'Hebrew',
                w['original'], None, w['parsing'], w['strong'],
                w['original'],
                '', w['pnc'], '', '', w['footnote'], None, None, None
            )
            for pos, w in enumerate(rec['words'])
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
    ap = argparse.ArgumentParser(description='Import the Open Scriptures Hebrew Bible (WLC + lemma + morphology) into River SQLite DB')
    ap.add_argument('wlc_dir', help="Path to OSHB's wlc/ directory (containing Gen.xml ... Mal.xml)")
    ap.add_argument('--code', default='WLC', help='Translation code (default: WLC)')
    ap.add_argument('--name', default='Westminster Leningrad Codex (OSHB)', help='Display name')
    ap.add_argument('--language', default='Hebrew', help='Language of text')
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
                cur = con.execute(
                    'INSERT INTO translations(code, name, language) VALUES (?,?,?)',
                    (args.code, args.name, args.language)
                )
                translation_id = cur.lastrowid

            books_data = con.execute('SELECT name, id, book_order FROM books').fetchall()
            book_id_by_name = {row[0]: row[1] for row in books_data}
            book_order_map = {row[0]: row[2] for row in books_data}

            verse_records = process_directory(args.wlc_dir, con, book_id_by_name, book_order_map)

            if not verse_records:
                sys.exit('Error: No valid verses could be parsed from the wlc directory.')

            v_count, w_count = import_oshb_translation(con, translation_id, verse_records)

        print(f'Successfully imported "{args.name}" ({args.code}): {v_count} verses, {w_count} words into {args.db}.')

    except Exception as e:
        sys.exit(f'Database import failed: {e}')
    finally:
        con.close()


if __name__ == '__main__':
    main()