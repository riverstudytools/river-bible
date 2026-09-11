#!/usr/bin/env python3
"""
import_osis_verses.py — Importer for verse-level OSIS XML Bibles (no per-word
Strong's tagging) into bible.sqlite. Built for the Clementine Vulgate and the
Douay-Rheims/Challoner text from seven1m/open-bibles, but works for any plain
OSIS file: <verse osisID="Gen.1.1">...text...</verse>.

Since the source has no word-level Strong's tags, each verse's text is split
on whitespace into one `words` row per token (lang set from --language,
strong left NULL). That's enough for the interlinear reading pane to render
the text and for search to index it; it does NOT get click-to-lexicon linking
(see README.md's note on STRONG_RE) unless you add a lexicon keyed some other
way and adapt this script to attach it.

Usage:
    python3 import_osis_verses.py lat-clementine.osis.xml \
        --code VUL --name "Clementine Vulgate" --language Latin --replace

    python3 import_osis_verses.py eng-drc.osis.xml \
        --code DRC --name "Douay-Rheims (Challoner)" --language English --replace
"""

import argparse
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET

import book_codes
import build_db as db_builder

OSIS_NS = {'osis': 'http://www.bibletechnologies.net/2003/OSIS/namespace'}


def clean_tag(tag):
    return tag.split('}')[-1] if '}' in tag else tag


def local_findall(elem, tagname):
    """Namespace-agnostic descendant search, since OSIS files vary on
    whether they declare/use the osis namespace consistently."""
    return [e for e in elem.iter() if clean_tag(e.tag) == tagname]


def osis_id_to_ref(osis_id):
    """'Gen.1.1' -> ('Gen', 1, 1). Some OSIS files use milestone verses with
    a range like 'Gen.1.1 Gen.1.2' (osisID has a space-separated list for
    bridged verses) — this takes just the first ref, which is what the vast
    majority of single-verse elements use."""
    first = osis_id.split()[0]
    parts = first.split('.')
    if len(parts) < 3:
        return None
    book_osis, chapter, verse = parts[0], parts[1], parts[2]
    # Bridged verse numbers like "5-6" — take the start.
    verse = re.match(r'\d+', verse)
    chapter = re.match(r'\d+', chapter)
    if not verse or not chapter:
        return None
    return book_osis, int(chapter.group(0)), int(verse.group(0))


def get_verse_text(verse_elem):
    """Concatenates all text in a <verse> element, stripping any inline
    markup (note/title/etc. children some OSIS files embed)."""
    text = ''.join(verse_elem.itertext())
    return re.sub(r'\s+', ' ', text).strip()


def load_verses(osis_path):
    """Yields (book_osis, chapter, verse, text) for every verse element that
    carries actual text content (skips empty milestone-style start/end verse
    pairs some OSIS exports use instead of container verses)."""
    tree = ET.parse(osis_path)
    root = tree.getroot()

    for verse_elem in local_findall(root, 'verse'):
        osis_id = verse_elem.attrib.get('osisID') or verse_elem.attrib.get('sID')
        if not osis_id:
            continue
        ref = osis_id_to_ref(osis_id)
        if not ref:
            continue
        text = get_verse_text(verse_elem)
        if not text:
            continue
        yield ref[0], ref[1], ref[2], text


def import_translation(con, translation_id, book_id_by_name, verses, language):
    verses_inserted = words_inserted = 0

    for book_osis, chapter, verse, text in verses:
        canonical_book = book_codes.resolve_canonical_name(book_osis)
        if not canonical_book:
            continue
        book_id = book_id_by_name.get(canonical_book)
        if book_id is None:
            continue

        existing = con.execute(
            'SELECT id FROM verses WHERE translation_id=? AND book_id=? AND chapter=? AND verse=?',
            (translation_id, book_id, chapter, verse)
        ).fetchone()
        if existing:
            verse_id = existing[0]
            con.execute('DELETE FROM words WHERE verse_id=?', (verse_id,))
        else:
            cur = con.execute(
                'INSERT INTO verses(translation_id, book_id, chapter, verse) VALUES (?,?,?,?)',
                (translation_id, book_id, chapter, verse)
            )
            verse_id = cur.lastrowid
            verses_inserted += 1

        tokens = text.split()
        words_batch = [
            (verse_id, pos, language, tok, None, None, None, tok, '', '', '', '', None, None, None, None)
            for pos, tok in enumerate(tokens)
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
    ap = argparse.ArgumentParser(description="Import a verse-level OSIS Bible into River's SQLite DB")
    ap.add_argument('osis_xml', help='Path to the OSIS XML file')
    ap.add_argument('--code', required=True, help='Short unique code, e.g. VUL or DRC')
    ap.add_argument('--name', required=True, help='Display name, e.g. "Clementine Vulgate"')
    ap.add_argument('--language', required=True, help='e.g. Latin or English')
    ap.add_argument('--db', default='bible.sqlite')
    ap.add_argument('--replace', action='store_true')
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.execute('PRAGMA foreign_keys = ON')

    with con:
        db_builder.ensure_schema(con)

        existing = con.execute('SELECT id FROM translations WHERE code=?', (args.code,)).fetchone()
        if existing and not args.replace:
            sys.exit(f'Translation code "{args.code}" already exists in {args.db} — pass --replace to overwrite it.')

        if existing:
            translation_id = existing[0]
            con.execute('UPDATE translations SET name=?, language=? WHERE id=?', (args.name, args.language, translation_id))
            db_builder.wipe_translation(con, translation_id)
        else:
            cur = con.execute('INSERT INTO translations(code, name, language) VALUES (?,?,?)',
                              (args.code, args.name, args.language))
            translation_id = cur.lastrowid

        books_data = con.execute('SELECT name, id FROM books').fetchall()
        book_id_by_name = {row[0]: row[1] for row in books_data}

        verses = list(load_verses(args.osis_xml))
        if not verses:
            sys.exit('No verses parsed — check the OSIS file structure (expected <verse osisID="Book.C.V">).')

        v, w = import_translation(con, translation_id, book_id_by_name, verses, args.language)

    print(f'Translation "{args.name}" ({args.code}): {v} verses, {w} words imported into {args.db}')
    print('Note: words have no Strong\'s number (strong=NULL) — no lexicon click-through for this text.')


if __name__ == '__main__':
    main()
