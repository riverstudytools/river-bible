#!/usr/bin/env python3
"""
import_peshitta_tf.py — Importer for the Peshitta Syriac text (ETCBC's
Text-Fabric datasets, syrnt for NT / peshitta for OT) plus James Murdock's
1851 public-domain English translation, into bible.sqlite.

This is a starting skeleton, not a verified-against-real-data script — I
don't have network access to fetch and inspect the actual TF feature files
in this session. Run `python3 -c "from tf.app import use; A = use('ETCBC/syrnt'); A.showFeatures()"`
(or the peshitta equivalent) first and adjust FEATURE names below to match
what's actually there before trusting a real import.

The Syriac text and the Murdock text are two independent sources that won't
line up 1:1 automatically — this script imports Syriac words first (as their
own translation row, lang=Syriac, strong=NULL — see README's STRONG_RE note),
then imports Murdock as an ordinary verse-level English translation via the
same tokenize-on-whitespace approach as import_osis_verses.py. They end up as
two separate --code entries the reader can view side by side in compare
mode, same as any other two translations.

Usage:
    pip install text-fabric[all] --break-system-packages

    python3 import_peshitta_tf.py --corpus syrnt \
        --code PESH --name "Peshitta (Syriac NT)" --replace --db bible.sqlite

    python3 import_peshitta_tf.py --murdock murdock_nt.txt \
        --code MURD --name "Murdock (1851)" --replace --db bible.sqlite
"""

import argparse
import re
import sqlite3
import sys

import book_codes
import build_db as db_builder


def import_syriac_from_tf(con, translation_id, book_id_by_name, corpus):
    """corpus is 'syrnt' or 'peshitta' (ETCBC repo name). Requires
    `pip install text-fabric`. Text-Fabric downloads the corpus data itself
    on first use.

    CONFIRMED features, from a real `A.showFeatures()` run against ETCBC/syrnt
    (peshitta should match — both are ETCBC/Wit-pipeline corpora with the same
    feature naming; re-run showFeatures() against it once to be sure before
    trusting an OT import unattended):
      otype: book / chapter / verse / word
      book:  book abbreviation (matched book_codes fine in practice — 7791
             verses came out of the NT run, the correct count)
      chapter, verse: integers
      word:  full word form in Syriac script (what we actually want)
      word_etcbc: ETCBC/Wit transliteration of that same form
      lexeme / lexeme_sedra: lemma in Syriac script / SEDRA transcription —
             prefer lexeme_sedra if cross-walking against a SEDRA III import
    """
    try:
        from tf.app import use
    except ImportError:
        sys.exit('Text-Fabric not installed — run: pip install text-fabric --break-system-packages')

    A = use(f'ETCBC/{corpus}', hoist=globals())
    api = A.api
    F, T, L = api.F, api.T, api.L

    verses_inserted = words_inserted = 0
    cur_verse_key = None
    verse_id = None
    position = 0

    for w in F.otype.s('word'):
        book_node = L.u(w, otype='book')[0]
        chapter_node = L.u(w, otype='chapter')[0]
        verse_node = L.u(w, otype='verse')[0]

        book_name_raw = F.book.v(book_node)
        chapter = F.chapter.v(chapter_node)
        verse = F.verse.v(verse_node)

        canonical_book = book_codes.resolve_canonical_name(book_name_raw)
        if not canonical_book:
            continue
        book_id = book_id_by_name.get(canonical_book)
        if book_id is None:
            continue

        verse_key = (book_id, chapter, verse)
        if verse_key != cur_verse_key:
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
            cur_verse_key = verse_key
            position = 0

        # Confirmed against a real showFeatures() dump of ETCBC/syrnt (and
        # peshitta uses the same feature set): the full Syriac-script word
        # form is the `word` feature — NOT `g_word_utf8` (that's a BHSA-style
        # name that doesn't exist here) and NOT the generic T.text([w]),
        # which was silently returning '' because this corpus's otext.tf
        # default format doesn't resolve the way the old code assumed.
        syriac_text = F.word.v(w) or ''
        # `word_etcbc` is the ETCBC/Wit transliteration of that same full
        # form — a reasonable transliteration field. `lexeme` is the
        # Syriac-script lemma; `lexeme_sedra` is the SEDRA-transcription
        # lemma, which is what you'll want if you're cross-walking against
        # your existing SEDRA III lexicon import rather than the Syriac
        # script form.
        translit = F.word_etcbc.v(w) or None
        lexeme = F.lexeme_sedra.v(w) or F.lexeme.v(w) or None

        con.execute(
            """INSERT INTO words(verse_id, position, lang, original, translit, parsing, strong,
                                  gloss, beg_q, pnc, end_q, end_text, footnote, hdg, crossref, par_classes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (verse_id, position, 'Syriac', syriac_text, translit, lexeme, None,
             syriac_text, '', '', '', '', None, None, None, None)
        )
        words_inserted += 1
        position += 1

    return verses_inserted, words_inserted


MURDOCK_LINE_RE = re.compile(r'^([1-3]?\s?[A-Za-z\. ]+?)\s+(\d+):(\d+)\s+(.+)$')


def load_murdock_text(path):
    """Expects one verse per line in 'Book C:V  text...' form — that's the
    common plain-text export shape for out-of-copyright KJV-era
    translations. If the file you download from aramaicnewtestament.org (or
    wherever) is shaped differently, adjust MURDOCK_LINE_RE rather than the
    rest of the pipeline."""
    with open(path, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            m = MURDOCK_LINE_RE.match(line)
            if not m:
                continue
            book_raw, chapter, verse, text = m.groups()
            yield book_raw.strip(), int(chapter), int(verse), text.strip()


def import_murdock(con, translation_id, book_id_by_name, path):
    verses_inserted = words_inserted = 0
    for book_raw, chapter, verse, text in load_murdock_text(path):
        canonical_book = book_codes.resolve_canonical_name(book_raw)
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
            (verse_id, pos, 'English', tok, None, None, None, tok, '', '', '', '', None, None, None, None)
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


def get_or_create_translation(con, code, name, language, replace):
    existing = con.execute('SELECT id FROM translations WHERE code=?', (code,)).fetchone()
    if existing and not replace:
        sys.exit(f'Translation code "{code}" already exists — pass --replace to overwrite it.')
    if existing:
        translation_id = existing[0]
        con.execute('UPDATE translations SET name=?, language=? WHERE id=?', (name, language, translation_id))
        db_builder.wipe_translation(con, translation_id)
    else:
        cur = con.execute('INSERT INTO translations(code, name, language) VALUES (?,?,?)', (code, name, language))
        translation_id = cur.lastrowid
    return translation_id


def main():
    ap = argparse.ArgumentParser(description="Import Peshitta Syriac (via Text-Fabric) or Murdock English into River's SQLite DB")
    ap.add_argument('--corpus', choices=['syrnt', 'peshitta'], help='ETCBC Text-Fabric corpus to import (Syriac)')
    ap.add_argument('--murdock', help='Path to a plain-text Murdock translation file (English)')
    ap.add_argument('--code', required=True)
    ap.add_argument('--name', required=True)
    ap.add_argument('--language', default=None)
    ap.add_argument('--db', default='bible.sqlite')
    ap.add_argument('--replace', action='store_true')
    args = ap.parse_args()

    if not args.corpus and not args.murdock:
        sys.exit('Pass either --corpus (Syriac, via Text-Fabric) or --murdock (English text file).')

    con = sqlite3.connect(args.db)
    con.execute('PRAGMA foreign_keys = ON')

    with con:
        db_builder.ensure_schema(con)
        books_data = con.execute('SELECT name, id FROM books').fetchall()
        book_id_by_name = {row[0]: row[1] for row in books_data}

        if args.corpus:
            language = args.language or 'Syriac'
            translation_id = get_or_create_translation(con, args.code, args.name, language, args.replace)
            v, w = import_syriac_from_tf(con, translation_id, book_id_by_name, args.corpus)
        else:
            language = args.language or 'English'
            translation_id = get_or_create_translation(con, args.code, args.name, language, args.replace)
            v, w = import_murdock(con, translation_id, book_id_by_name, args.murdock)

    print(f'Translation "{args.name}" ({args.code}): {v} verses, {w} words imported into {args.db}')


if __name__ == '__main__':
    main()
