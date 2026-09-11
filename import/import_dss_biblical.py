#!/usr/bin/env python3
"""
import_dss_biblical.py — Importer for the biblical Dead Sea Scrolls
(ETCBC/dss Text-Fabric dataset) into bible.sqlite.

Features:
- Selects the longest, most complete manuscript witness as the baseline for each verse.
- Performs fine-grained sequence alignment using normalized lexemes to ignore orthographic noise.
- Outputs identical text segments once, attaching manuscript tags only to variant readings.
- Falls back to full-verse variants when secondary witnesses depart significantly (< 60% similarity).
- Preserves lexemes, transliteration, morphology, and Strong's cross-walk mappings.
"""

import argparse
import difflib
import re
import sqlite3
import sys

import book_codes
import build_db as db_builder

BOOK_NAME_MAP = {
    'Gen': 'Genesis', 'Ex': 'Exodus', 'Lev': 'Leviticus', 'Num': 'Numbers',
    'Deut': 'Deuteronomy', 'Josh': 'Joshua', 'Judg': 'Judges',
    '1Sam': '1 Samuel', '2Sam': '2 Samuel', '1Kgs': '1 Kings', '2Kgs': '2 Kings',
    'Is': 'Isaiah', 'Jer': 'Jeremiah', 'Ezek': 'Ezekiel',
    'Hos': 'Hosea', 'Joel': 'Joel', 'Amos': 'Amos', 'Obad': 'Obadiah',
    'Jonah': 'Jonah', 'Mic': 'Micah', 'Nah': 'Nahum', 'Hab': 'Habakkuk',
    'Zeph': 'Zephaniah', 'Hag': 'Haggai', 'Zech': 'Zechariah', 'Mal': 'Malachi',
    'Ps': 'Psalms', 'Job': 'Job', 'Prov': 'Proverbs', 'Ruth': 'Ruth',
    'Song': 'Song of Solomon', 'Eccl': 'Ecclesiastes', 'Lam': 'Lamentations',
    'Dan': 'Daniel', 'Ezra': 'Ezra', '2Chr': '2 Chronicles',
}

LANG_MAP = {None: 'Hebrew', 'a': 'Aramaic', 'g': 'Greek'}
HOMOGRAPH_SUFFIX_RE = re.compile(r'_\d+$')

CLEAN_REPLACEMENTS = [
    (re.compile(r'[?#|»«]'), ''),
    (re.compile(r'[\[\]]'), ''),
    (re.compile(r'<<|>>'), ''),
    (re.compile(r'[<>]'), ''),
    (re.compile(r'[\u2264\u2265]'), ''),
]


def clean_full(text):
    if not text:
        return ''
    for pattern, repl in CLEAN_REPLACEMENTS:
        text = pattern.sub(repl, text)
    return text.strip()


def extract_compare_key(word_dict):
    """Prefers lexeme for comparison to ignore spelling differences."""
    if word_dict.get('lex'):
        return word_dict['lex']
    return clean_full(word_dict.get('text', ''))


def load_tf_api(tf_locations, corpus_ref):
    if tf_locations:
        from tf.fabric import Fabric
        TF = Fabric(locations=tf_locations, silent='deep')
        return TF.load(
            'otype oslots book chapter verse scroll fragment full fulle lex lang',
            silent='deep'
        )
    else:
        from tf.app import use
        A = use(corpus_ref, hoist=globals())
        return A.api


def iter_biblical_words(api):
    F, L = api.F, api.L

    for w in F.otype.s('word'):
        book_raw = F.book.v(w)
        if not book_raw or book_raw not in BOOK_NAME_MAP:
            continue
        book_full = BOOK_NAME_MAP[book_raw]
        chapter = F.chapter.v(w)
        verse = F.verse.v(w)
        if not chapter or not verse:
            continue

        frag_nodes = L.u(w, otype='fragment')
        scroll = F.scroll.v(frag_nodes[0]) if frag_nodes else 'unknown'

        hebrew_text = F.full.v(w) or ''
        translit = F.fulle.v(w) or None
        lex_nodes = L.u(w, otype='lex')
        lex_text = F.lex.v(lex_nodes[0]) if lex_nodes else None
        if lex_text:
            lex_text = HOMOGRAPH_SUFFIX_RE.sub('', lex_text)
        lang = LANG_MAP.get(F.lang.v(w), 'Hebrew')

        yield book_full, int(chapter), int(verse), scroll, hebrew_text, translit, lex_text, lang


def group_into_verses(word_rows):
    from collections import OrderedDict
    verses = OrderedDict()

    for book, chapter, verse, scroll, text, translit, lex, lang in word_rows:
        key = (book, chapter, verse)
        runs = verses.setdefault(key, [])
        word_entry = {'text': text, 'translit': translit, 'lex': lex, 'lang': lang}
        if runs and runs[-1][0] == scroll:
            runs[-1][1].append(word_entry)
        else:
            runs.append((scroll, [word_entry]))

    return verses


def align_witnesses(runs, similarity_threshold=0.60):
    if len(runs) == 1:
        return [{'type': 'common', 'words': runs[0][1]}]

    # 1. Base witness selection: sort by word count descending
    sorted_runs = sorted(runs, key=lambda r: len(r[1]), reverse=True)
    base_scroll, base_words = sorted_runs[0]
    base_keys = [extract_compare_key(w) for w in base_words]

    # 2. Check for major divergence
    for scroll, words in sorted_runs[1:]:
        sec_keys = [extract_compare_key(w) for w in words]
        ratio = difflib.SequenceMatcher(None, base_keys, sec_keys).ratio()
        if ratio < similarity_threshold:
            return [{'type': 'major_variant', 'variants': sorted_runs}]

    # 3. Fine-grained sequence alignment
    base_len = len(base_words)
    token_variants = [{} for _ in range(base_len)]

    for scroll, words in sorted_runs[1:]:
        sec_keys = [extract_compare_key(w) for w in words]
        matcher = difflib.SequenceMatcher(None, base_keys, sec_keys)

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag != 'equal':
                for idx in range(i1, max(i1 + 1, i2)):
                    if idx < base_len:
                        token_variants[idx][scroll] = words[j1:j2]

    # 4. Segment construction
    aligned_segments = []
    i = 0
    while i < base_len:
        if not token_variants[i]:
            aligned_segments.append({'type': 'common', 'words': [base_words[i]]})
            i += 1
        else:
            var_dict = {base_scroll: [base_words[i]]}
            var_dict.update(token_variants[i])
            aligned_segments.append({'type': 'variant', 'variants': var_dict})
            i += 1

    return aligned_segments


def insert_word_record(con, verse_id, position, wd, lexeme_map, clean=False):
    text = clean_full(wd['text']) if clean else wd['text']
    strong = lexeme_map.get(wd['lex']) if wd.get('lex') else None

    con.execute(
        """INSERT INTO words(verse_id, position, lang, original, translit, parsing, strong,
                              gloss, beg_q, pnc, end_q, end_text, footnote, hdg, crossref, par_classes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (verse_id, position, wd['lang'], text, wd.get('translit'), wd.get('parsing'), strong,
         text, '', '', '', '', None, None, None, None)
    )


def insert_tag_record(con, verse_id, position, tag_text):
    con.execute(
        """INSERT INTO words(verse_id, position, lang, original, translit, parsing, strong,
                              gloss, beg_q, pnc, end_q, end_text, footnote, hdg, crossref, par_classes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (verse_id, position, 'tag', tag_text, None, None, None,
         tag_text, '', '', '', '', None, None, None, None)
    )


def main():
    ap = argparse.ArgumentParser(description="Import biblical Dead Sea Scrolls with variant deduplication into SQLite")
    ap.add_argument('--corpus', default='ETCBC/dss')
    ap.add_argument('--tf-locations', default=None)
    ap.add_argument('--code', default='DSS')
    ap.add_argument('--name', default='Dead Sea Scrolls (biblical variants)')
    ap.add_argument('--language', default='Hebrew')
    ap.add_argument('--db', default='bible.sqlite')
    ap.add_argument('--replace', action='store_true')
    ap.add_argument('--clean', action='store_true', help='Strip editorial markup from transcription')
    ap.add_argument('--threshold', type=float, default=0.60, help='Similarity threshold for full verse variant fallback')
    args = ap.parse_args()

    try:
        api = load_tf_api(args.tf_locations, args.corpus)
    except ImportError:
        sys.exit('Text-Fabric not installed — run: pip install text-fabric --break-system-packages')

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

        rows = con.execute(
            "SELECT lexeme, strong FROM lexicon_words WHERE lang IN ('Hebrew','Aramaic') AND lexeme IS NOT NULL AND lexeme != ''"
        ).fetchall()
        lexeme_map = {lexeme: strong for lexeme, strong in rows}

        verses = group_into_verses(iter_biblical_words(api))
        verses_inserted = words_inserted = 0

        for (book, chapter, verse), runs in verses.items():
            book_id = book_id_by_name.get(book)
            if book_id is None:
                continue

            cur = con.execute(
                'INSERT INTO verses(translation_id, book_id, chapter, verse) VALUES (?,?,?,?)',
                (translation_id, book_id, chapter, verse)
            )
            verse_id = cur.lastrowid
            verses_inserted += 1
            position = 0

            aligned_segments = align_witnesses(runs, similarity_threshold=args.threshold)

            for segment in aligned_segments:
                if segment['type'] == 'common':
                    for wd in segment['words']:
                        insert_word_record(con, verse_id, position, wd, lexeme_map, args.clean)
                        position += 1
                        words_inserted += 1

                elif segment['type'] == 'variant':
                    for scroll, var_words in segment['variants'].items():
                        insert_tag_record(con, verse_id, position, f'[{scroll}]')
                        position += 1
                        words_inserted += 1
                        for wd in var_words:
                            insert_word_record(con, verse_id, position, wd, lexeme_map, args.clean)
                            position += 1
                            words_inserted += 1

                elif segment['type'] == 'major_variant':
                    for scroll, var_words in segment['variants']:
                        insert_tag_record(con, verse_id, position, f'[{scroll}]')
                        position += 1
                        words_inserted += 1
                        for wd in var_words:
                            insert_word_record(con, verse_id, position, wd, lexeme_map, args.clean)
                            position += 1
                            words_inserted += 1

    print(f'Import finished: {verses_inserted} verses, {words_inserted} tokens/tags saved into {args.db}')


if __name__ == '__main__':
    main()