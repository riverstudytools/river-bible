#!/usr/bin/env python3
"""
import_spe_english.py — Importer for "Samaritan Pentateuch in English" (SPE),
Aleksandr Sigalov's KJV-based English rendering of the Samaritan Pentateuch,
into bible.sqlite. This is the "archaic" SP translation — it's built on the
public-domain KJV text, edited at each point where the Samaritan reading
differs from the Masoretic Text, per Kennicott's 1780 parallel edition
cross-checked against von Gall's 1918 critical edition.

Get the module first — this script only reads a copy you already have:
  - It's a SWORD module distributed through CrossWire's main repository
    (module name "SPE", category Biblical Texts). Easiest path: install
    any SWORD-based reader (e.g. Xiphos, BibleTime, or the `installmgr`/
    `mods.d` tooling) and let it fetch "SPE" from the CrossWire repo, or
    download the raw module .zip directly from CrossWire's module list.
  - It's also distributed for e-Sword/MySword/theWord — those are
    different (non-SWORD) formats; if that's what you find, this script
    won't read it as-is (see the note at the bottom about that case).
  - Whichever way you get it, you want a folder or .zip containing
    `mods.d/spe.conf` and `modules/texts/...` — that's the SWORD module
    shape this script expects.

This script's pysword-based read pipeline (SwordModules -> parse_modules ->
get_bible_from_module -> get_structure/get) was verified end-to-end against
a hand-built minimal RawText SWORD fixture while writing this — the API
calls are confirmed correct. What ISN'T verified is the real SPE module's
own internals (its ModDrv might be RawText, RawText4, zText, or zText4;
pysword auto-detects this from `moddrv`/`v11n` in the conf file, so it
should just work, but I haven't run this against the actual SPE download).

Usage:
    pip install pysword --break-system-packages

    python3 import_spe_english.py /path/to/SPE.zip \
        --code SPE --name "Samaritan Pentateuch in English" --replace
    # or, if you've already unzipped it:
    python3 import_spe_english.py /path/to/spe_module_folder/ --code SPE --replace
"""

import argparse
import sqlite3
import sys

import book_codes
import build_db as db_builder


def load_spe_verses(module_path, module_key=None):
    """Yields (book_name, chapter, verse, text) for every verse pysword's
    structure reports for this module. module_key defaults to the first
    (usually only) module found in the conf files, since a single-purpose
    download like this normally contains just SPE itself."""
    try:
        from pysword.modules import SwordModules
    except ImportError:
        sys.exit('pysword not installed — run: pip install pysword --break-system-packages')

    mods = SwordModules(module_path)
    found = mods.parse_modules()
    if not found:
        sys.exit(f'No SWORD modules found at {module_path} — expected a mods.d/*.conf inside it.')
    key = module_key or next(iter(found))
    if key not in found:
        sys.exit(f'Module "{key}" not found — modules present: {list(found.keys())}')

    bible = mods.get_bible_from_module(key)
    structure = bible.get_structure()
    books = structure.get_books()

    # SPE is Torah-only, but this walks whatever testaments/books the
    # module actually declares rather than hardcoding Genesis-Deuteronomy,
    # so it degrades gracefully if you point it at a different SP-related
    # SWORD module (SPMT, SPVar, SPDS — see the STEPBible catalog) instead.
    for testament_books in books.values():
        for book in testament_books:
            for chapter in range(1, book.num_chapters + 1):
                verse_count = book.chapter_lengths[chapter - 1]
                for verse in range(1, verse_count + 1):
                    text = bible.get(books=book.name, chapters=chapter, verses=verse, clean=True)
                    text = (text or '').strip()
                    if not text:
                        continue
                    yield book.name, chapter, verse, text


def import_translation(con, translation_id, book_id_by_name, verses, language):
    verses_inserted = words_inserted = 0

    for book_name_raw, chapter, verse, text in verses:
        canonical_book = book_codes.resolve_canonical_name(book_name_raw)
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
    ap = argparse.ArgumentParser(description="Import the Samaritan Pentateuch in English (SPE) SWORD module into River's SQLite DB")
    ap.add_argument('module_path', help='Path to the SWORD module .zip or unzipped folder')
    ap.add_argument('--module-key', default=None, help='Module name inside the conf, if not auto-detected correctly')
    ap.add_argument('--code', default='SPE')
    ap.add_argument('--name', default='Samaritan Pentateuch in English')
    ap.add_argument('--language', default='English')
    ap.add_argument('--db', default='bible.sqlite')
    ap.add_argument('--replace', action='store_true')
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.execute('PRAGMA foreign_keys = ON')

    with con:
        db_builder.ensure_schema(con)

        existing = con.execute('SELECT id FROM translations WHERE code=?', (args.code,)).fetchone()
        if existing and not args.replace:
            sys.exit(f'Translation code "{args.code}" already exists — pass --replace to overwrite it.')
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

        verses = list(load_spe_verses(args.module_path, args.module_key))
        if not verses:
            sys.exit('No verses read from the module — check --module-key against the names pysword reported, '
                     'or confirm this is really a SWORD-format module (not e-Sword/theWord/MySword).')

        v, w = import_translation(con, translation_id, book_id_by_name, verses, args.language)

    print(f'Translation "{args.name}" ({args.code}): {v} verses, {w} words imported into {args.db}')


if __name__ == '__main__':
    main()
