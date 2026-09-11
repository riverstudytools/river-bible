#!/usr/bin/env python3
"""
import_samaritan_pentateuch.py — Importer for the Samaritan Pentateuch
(DT-UCPH/sp Text-Fabric dataset) into bible.sqlite.

Like import_peshitta_tf.py, this is an unverified starting skeleton — run
`A.showFeatures()` against the real dataset first and adjust FEATURE names
if they don't match what's assumed below.

Because the Samaritan Pentateuch is Hebrew, this script tries to cross-walk
each word to your EXISTING H-numbered lexicon_words table by matching
consonantal lexeme spelling — no schema change needed, and genuinely
word-clickable if the match rate is good. It reports a coverage percentage
at the end; a low number means the SP dataset's lexeme transliteration
scheme doesn't line up cleanly with whatever your existing Hebrew lexicon
uses as its spelling, and the cross-walk logic in `lexeme_to_strong()` below
will need actual tuning against real data rather than guessing.

Words that don't cross-walk still import fine — they just get strong=NULL,
same as any other word without a Strong's number (your fallback lexical
resolution in db-queries.js can sometimes still resolve those by literal
text match against words elsewhere in the DB that DO carry a Strong's
number).

Usage:
    pip install text-fabric[all] --break-system-packages

    python3 import_samaritan_pentateuch.py \
        --code SP --name "Samaritan Pentateuch" --replace --db bible.sqlite
"""

import argparse
import sqlite3
import sys

import book_codes
import build_db as db_builder


def build_lexeme_to_strong_map(con):
    """Reads your existing lexicon_words table into a {lexeme: strong} dict
    for cross-walking. Only meaningful for Hebrew entries (H-numbers) —
    the SP is entirely Torah/Pentateuch, so only Genesis-Deuteronomy H
    entries are relevant, but pulling all Hebrew entries is harmless."""
    rows = con.execute(
        "SELECT lexeme, strong FROM lexicon_words WHERE lang = 'Hebrew' AND lexeme IS NOT NULL AND lexeme != ''"
    ).fetchall()
    return {lexeme: strong for lexeme, strong in rows}


def lexeme_to_strong(sp_lexeme, lexeme_map):
    """Direct match first; SP transcriptions sometimes carry final-form
    letters or vowel-pointing your MT-based lexicon doesn't, so a stripped
    fallback is attempted too. Tune this against real mismatches once you
    can see them — this is a starting guess, not a verified mapping."""
    if not sp_lexeme:
        return None
    if sp_lexeme in lexeme_map:
        return lexeme_map[sp_lexeme]
    stripped = sp_lexeme.replace('\u05bc', '')  # strip dagesh as a first pass
    return lexeme_map.get(stripped)


def import_sp_from_tf(con, translation_id, book_id_by_name, lexeme_map):
    """ASSUMED features (verify with A.showFeatures()):
      otype: book / chapter / verse / word
      book: book name
      chapter, verse: integers
      g_cons or g_word_utf8: the Hebrew consonantal text
      lex: lemma/lexeme transliteration (BHSA-style)
      sp_diff or similar: marks where this word differs from MT, if present
        — worth surfacing in a footnote if it exists, since the MT/SP
        differences are the whole point of studying this text
    """
    try:
        from tf.app import use
    except ImportError:
        sys.exit('Text-Fabric not installed — run: pip install text-fabric --break-system-packages')

    A = use('DT-UCPH/sp', hoist=globals())
    api = A.api
    F, L = api.F, api.L

    verses_inserted = words_inserted = matched = 0
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

        hebrew_text = F.g_cons.v(w) if hasattr(F, 'g_cons') else F.g_word_utf8.v(w)
        lex = F.lex.v(w) if hasattr(F, 'lex') else None
        strong = lexeme_to_strong(lex, lexeme_map)
        if strong:
            matched += 1

        con.execute(
            """INSERT INTO words(verse_id, position, lang, original, translit, parsing, strong,
                                  gloss, beg_q, pnc, end_q, end_text, footnote, hdg, crossref, par_classes)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (verse_id, position, 'Hebrew', hebrew_text, None, None, strong,
             hebrew_text, '', '', '', '', None, None, None, None)
        )
        words_inserted += 1
        position += 1

    return verses_inserted, words_inserted, matched


def main():
    ap = argparse.ArgumentParser(description="Import the Samaritan Pentateuch (DT-UCPH/sp) into River's SQLite DB")
    ap.add_argument('--code', default='SP')
    ap.add_argument('--name', default='Samaritan Pentateuch')
    ap.add_argument('--language', default='Hebrew')
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

        lexeme_map = build_lexeme_to_strong_map(con)
        if not lexeme_map:
            print('Warning: no existing Hebrew lexicon_words found — every word will import with strong=NULL. '
                  'Import your Hebrew lexicon first if you want the cross-walk to have anything to match against.',
                  file=sys.stderr)

        v, w, matched = import_sp_from_tf(con, translation_id, book_id_by_name, lexeme_map)

    coverage = (matched / w * 100) if w else 0
    print(f'Translation "{args.name}" ({args.code}): {v} verses, {w} words imported into {args.db}')
    print(f'Strong\'s cross-walk matched {matched}/{w} words ({coverage:.1f}%) — '
          f'{"looks usable" if coverage > 60 else "low coverage, tune lexeme_to_strong() before trusting lexicon links"}.')


if __name__ == '__main__':
    main()
