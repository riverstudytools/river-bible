#!/usr/bin/env python3
"""
import_vulgate_proiel.py — Imports the Latin Vulgate NT from the
UD_Latin-PROIEL treebank (a CoNLL-U conversion of the PROIEL project's
hand-annotated Latin data) into River's SQLite DB, with real lemmas and
full morphology per word, linked into the Lewis & Short lexicon already
in your DB.

Source: https://github.com/UniversalDependencies/UD_Latin-PROIEL
Three files: la_proiel-ud-train.conllu, la_proiel-ud-dev.conllu,
la_proiel-ud-test.conllu — you want all three, since UD splits the data
for NLP training/testing, not by book; a given Gospel can straddle more
than one file. Concatenate them (or just pass all three paths — this
script accepts multiple files).

License: CC BY-NC-SA 4.0 (per proiel/syntacticus-dictionaries; you've
confirmed NC is fine for this project).

IMPORTANT — coverage gap: this treebank does NOT cover the whole
Vulgate. It's "most of" the NT plus some non-biblical Latin prose
(Caesar, Cicero, Palladius) mixed into the same files. Per PROIEL's own
release notes, entire chapters are missing in places (e.g. all of
Colossians 3-4, 1-2 Timothy, Hebrews, James, 1-2 Peter, 1-3 John, Jude
have little or no annotation at all; Acts 21-28, Romans 11 & 13,
Galatians, Ephesians 3-5, Philippians, Colossians 1-2 are only
partially annotated). There is NO Old Testament here at all — this is
Vulgate NT only. For the OT and the unannotated NT passages, you still
need import_vulgate_lemmatized.py (the CLTK-based one) to fill the
gaps — this script and that one are complementary, not a straight
swap.

FORMAT — this is standard CoNLL-U (10 tab-separated columns: ID, FORM,
LEMMA, UPOS, XPOS, FEATS, HEAD, DEPREL, DEPS, MISC; sentences separated
by blank lines; comment lines start with '#', including '# sent_id'
and '# text'). That part is a stable, well-documented open format and
this script's column parsing can be trusted.

WHAT'S UNVERIFIED — I could not actually fetch a real .conllu file from
this repo to confirm how book/chapter/verse citations are recorded in
the comments (I confirmed the repo, file names, and license, but not
one specific sentence's comment block). Bible citations in
PROIEL-derived treebanks are typically carried in a '# reference' or
similar comment, or embedded in the 'citation-part' MISC field per
token — but I'm not certain which, for this specific UD conversion.
This script tries several common patterns and, if none match, PRINTS
THE RAW COMMENT LINES of the first few sentences so you can see
immediately what's actually there and fix `extract_reference()`
in one place. Don't run this unattended on the full files before
checking that diagnostic output.

Usage:
    python3 import_vulgate_proiel.py la_proiel-ud-train.conllu la_proiel-ud-dev.conllu la_proiel-ud-test.conllu \
        --code VUL-NT --name "Vulgate NT (PROIEL)" --db bible.sqlite --replace
"""

import argparse
import re
import sqlite3
import sys

import book_codes
import build_db as db_builder

# PROIEL's own book abbreviations (from proiel-treebank citation schemes,
# e.g. "matthew", "mark", ... or numeric Nestle-Aland-style codes) mapped
# to this project's canonical book names. EXTEND/FIX after checking the
# diagnostic output against a real file — this is a best-guess starting
# table, not verified against actual sent_id/reference strings.
PROIEL_BOOK_MAP = {
    'matthew': 'Matthew', 'mark': 'Mark', 'luke': 'Luke', 'john': 'John',
    'acts': 'Acts', 'romans': 'Romans', '1corinthians': '1 Corinthians',
    '2corinthians': '2 Corinthians', 'galatians': 'Galatians',
    'ephesians': 'Ephesians', 'philippians': 'Philippians',
    'colossians': 'Colossians', '1thessalonians': '1 Thessalonians',
    '2thessalonians': '2 Thessalonians', '1timothy': '1 Timothy',
    '2timothy': '2 Timothy', 'titus': 'Titus', 'philemon': 'Philemon',
    'hebrews': 'Hebrews', 'james': 'James', '1peter': '1 Peter',
    '2peter': '2 Peter', '1john': '1 John', '2john': '2 John',
    '3john': '3 John', 'jude': 'Jude', 'revelation': 'Revelation',
}

# Candidate patterns for a Bible reference inside a CoNLL-U comment line.
# Tries each in order; first match wins. ADD MORE HERE once you've seen
# a real file — see the module docstring.
REFERENCE_PATTERNS = [
    re.compile(r'#\s*reference\s*=\s*([a-zA-Z0-9]+)\s+(\d+)\.(\d+)'),
    re.compile(r'#\s*sent_id\s*=\s*([a-zA-Z]+);(\d+)\.(\d+)'),
    re.compile(r'#\s*newpar\s+id\s*=\s*([a-zA-Z]+)-(\d+)-(\d+)'),
]


def extract_reference(comment_lines):
    """Tries each known pattern against a sentence's comment lines.
    Returns (book_key, chapter, verse) or None."""
    for line in comment_lines:
        for pat in REFERENCE_PATTERNS:
            m = pat.search(line)
            if m:
                book_key = m.group(1).lower()
                try:
                    return book_key, int(m.group(2)), int(m.group(3))
                except ValueError:
                    continue
    return None


def parse_conllu(paths):
    """Yields (comment_lines, tokens) per sentence, where tokens is a list
    of dicts with id/form/lemma/upos/feats for each non-multiword-span row."""
    for path in paths:
        with open(path, encoding='utf-8') as f:
            comment_lines = []
            tokens = []
            for line in f:
                line = line.rstrip('\n')
                if not line.strip():
                    if tokens:
                        yield comment_lines, tokens
                    comment_lines, tokens = [], []
                    continue
                if line.startswith('#'):
                    comment_lines.append(line)
                    continue
                cols = line.split('\t')
                if len(cols) < 10:
                    continue
                tid = cols[0]
                if '-' in tid or '.' in tid:
                    # Multiword token range or empty node (ellipsis) — skip;
                    # the individual sub-tokens on following lines carry
                    # the actual lemma/morphology we want.
                    continue
                tokens.append({
                    'id': tid, 'form': cols[1], 'lemma': cols[2],
                    'upos': cols[3], 'feats': cols[5],
                })
            if tokens:
                yield comment_lines, tokens


def build_lexicon_lookup(con):
    lookup = {}
    try:
        rows = con.execute("SELECT lexeme, strong FROM lexicon_words WHERE lang='Latin'").fetchall()
    except sqlite3.OperationalError:
        print('Warning: no lexicon_words table found for Latin — words will import without strong codes.', file=sys.stderr)
        return lookup
    for lexeme, strong in rows:
        if lexeme:
            lookup.setdefault(lexeme.strip().lower(), strong)
    return lookup


def diagnose(paths, n=5):
    """Prints the raw comment lines of the first n sentences from the
    first file, so you can see what reference format is really there."""
    shown = 0
    with open(paths[0], encoding='utf-8') as f:
        block = []
        for line in f:
            line = line.rstrip('\n')
            if not line.strip():
                if block:
                    print('--- sentence', shown + 1, '---')
                    for l in block:
                        if l.startswith('#'):
                            print(' ', l)
                    shown += 1
                    block = []
                    if shown >= n:
                        return
                continue
            block.append(line)


def main():
    ap = argparse.ArgumentParser(description='Import the Vulgate NT from UD_Latin-PROIEL CoNLL-U files')
    ap.add_argument('conllu_files', nargs='+', help='One or more .conllu files (train/dev/test)')
    ap.add_argument('--code', default='VUL-NT')
    ap.add_argument('--name', default='Vulgate NT (PROIEL)')
    ap.add_argument('--language', default='Latin')
    ap.add_argument('--db', default='bible.sqlite')
    ap.add_argument('--replace', action='store_true')
    ap.add_argument('--diagnose-only', action='store_true',
                     help="Just print the first 5 sentences' comment lines and exit — run this FIRST.")
    args = ap.parse_args()

    if args.diagnose_only:
        diagnose(args.conllu_files)
        return

    con = sqlite3.connect(args.db)
    con.execute('PRAGMA foreign_keys = ON')

    with con:
        db_builder.ensure_schema(con)

        existing = con.execute('SELECT id FROM translations WHERE code=?', (args.code,)).fetchone()
        if existing and not args.replace:
            sys.exit(f'Translation code "{args.code}" already exists — pass --replace to overwrite.')
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
        lexicon_lookup = build_lexicon_lookup(con)

        v_count = w_count = matched = unref_sentences = 0
        seen_verses = {}  # (book_id, chapter, verse) -> verse_id, to accumulate multi-sentence verses

        for comment_lines, tokens in parse_conllu(args.conllu_files):
            ref = extract_reference(comment_lines)
            if ref is None:
                unref_sentences += 1
                continue
            book_key, chapter, verse = ref
            canonical_book = PROIEL_BOOK_MAP.get(book_key) or book_codes.resolve_canonical_name(book_key)
            if not canonical_book or canonical_book not in book_id_by_name:
                continue
            book_id = book_id_by_name[canonical_book]

            vkey = (book_id, chapter, verse)
            if vkey in seen_verses:
                verse_id = seen_verses[vkey]
                start_pos = con.execute('SELECT COUNT(*) FROM words WHERE verse_id=?', (verse_id,)).fetchone()[0]
            else:
                existing_v = con.execute(
                    'SELECT id FROM verses WHERE translation_id=? AND book_id=? AND chapter=? AND verse=?',
                    (translation_id, book_id, chapter, verse)
                ).fetchone()
                if existing_v:
                    verse_id = existing_v[0]
                    con.execute('DELETE FROM words WHERE verse_id=?', (verse_id,))
                else:
                    cur = con.execute(
                        'INSERT INTO verses(translation_id, book_id, chapter, verse) VALUES (?,?,?,?)',
                        (translation_id, book_id, chapter, verse)
                    )
                    verse_id = cur.lastrowid
                    v_count += 1
                seen_verses[vkey] = verse_id
                start_pos = 0

            words_batch = []
            for i, tok in enumerate(tokens):
                lemma = (tok['lemma'] or '').strip().lower()
                strong = lexicon_lookup.get(lemma) if lemma else None
                if strong:
                    matched += 1
                parsing = f"{tok['upos']} {tok['feats']}".strip()
                words_batch.append((
                    verse_id, start_pos + i, 'Latin', tok['form'], None, parsing, strong,
                    tok['form'], '', '', '', '', None, None, None, None
                ))
            con.executemany(
                """INSERT INTO words(
                    verse_id, position, lang, original, translit, parsing, strong,
                    gloss, beg_q, pnc, end_q, end_text, footnote, hdg, crossref, par_classes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                words_batch
            )
            w_count += len(words_batch)

    pct = (matched / w_count * 100) if w_count else 0
    print(f'Imported {v_count} verses, {w_count} words into "{args.name}" ({args.code}).')
    print(f'Lexicon-linked: {matched}/{w_count} words ({pct:.1f}%) matched Lewis & Short by lemma.')
    if unref_sentences:
        print(f'{unref_sentences} sentences had no recognizable Bible reference and were skipped '
              f'(likely the non-biblical Caesar/Cicero/Palladius material mixed into these files, '
              f'or a reference format extract_reference() does not yet handle — run with '
              f'--diagnose-only first if this number looks too high).')


if __name__ == '__main__':
    main()
