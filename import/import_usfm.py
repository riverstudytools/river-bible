#!/usr/bin/env python3
"""
import_usfm.py — Production-hardened importer for USFM (Unified Standard Format Markers)
Bible files into River's SQLite database (bible.sqlite).

Poetry/paragraph structure (\\p \\q1-\\q4 \\qr \\qc \\qm \\b \\d \\li ...) is stored per word in
words.par_classes, on the first word of each line, like the Berean TSV importer.

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
# True: cross references (\\x) are stored in words.footnote like footnotes.
# False: they go in words.crossref instead.
XREF_AS_FOOTNOTE = True
NOTE_MARK_RE = re.compile(r'^__NOTE_(\d+)__$')

# Isolates leading/trailing punctuation (including brackets) while preserving internal apostrophes, hyphens, and quotes
WORD_PUNCT_RE = re.compile(r"^([^\w\[\]'\u2019]*)([\w'\u2019\-]+)([^\w\[\]'\u2019]*)$", re.UNICODE)


# Superscript Unicode forms for inline subverse markers (46b, 46c, ...) — no
# schema change needed since these just become part of the existing beg_q
# punctuation-prefix text. 'q' has no standard Unicode superscript letter;
# falls back to a plain 'q' rather than a missing-glyph box.
SUPERSCRIPT_LETTERS = {
    'a': '\u1d43', 'b': '\u1d47', 'c': '\u1d9c', 'd': '\u1d48', 'e': '\u1d49',
    'f': '\u1da0', 'g': '\u1d4d', 'h': '\u02b0', 'i': '\u2071', 'j': '\u02b2',
    'k': '\u1d4f', 'l': '\u02e1', 'm': '\u1d50', 'n': '\u207f', 'o': '\u1d52',
    'p': '\u1d56', 'q': 'q', 'r': '\u02b3', 's': '\u02e2', 't': '\u1d57',
    'u': '\u1d58', 'v': '\u1d5b', 'w': '\u02b7', 'x': '\u02e3', 'y': '\u02b8',
    'z': '\u1dbb',
}


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
    # Pull footnotes / cross references out IN PLACE, leaving a token so each note
    # attaches to the word it follows (not to the last word of the verse).
    notes = []  # (kind, text)

    def stash_note(m):
        kind = 'f' if m.group(1) == 'f' else 'x'
        body = re.sub(r'\\(?:fr|xo)\s+[^\\]*', '', m.group(2))   # drop origin ref ("1:27")
        body = re.sub(r'\\\+?[a-z0-9-]+\*?', ' ', body)            # drop remaining markers
        body = re.sub(r'^\s*[+\-?]\s+', '', body)                   # drop caller (+ / - / ?)
        body = re.sub(r'\s+', ' ', body).strip()
        notes.append((kind, body))
        return f' __NOTE_{len(notes) - 1}__ '

    clean_text = re.sub(r'\\([fx])\s+(.*?)(?:\\\1\*|$)', stash_note, raw_text)

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

    # 1. Convert USFM \add ... \add* (or nested \+add ... \+add*) to bracket delimiters
    #    Uses temporary ASCII symbols like '\x02' and '\x03' to avoid collision with punctuation splitting.
    processed_text = re.sub(r'\\\+?add\s+(.*?)\\\+?add\*', "\x02\\1\x03", processed_text)
    
    # 2. Strip remaining USFM tags including nested '+' prefixed tags
    processed_text = re.sub(r'\\\+?[a-z0-9-]+\*?', ' ', processed_text)

    # 2b. Keep whitespace OUTSIDE the \add markers: "and \add the \add*Lord" must
    #     become "and [the] Lord", not "and [the ]Lord". Done after tag stripping
    #     so spaces left behind by nested tags inside the \add span are covered too.
    processed_text = re.sub(r'\x02(\s+)', lambda m: m.group(1) + '\x02', processed_text)
    processed_text = re.sub(r'(\s+)\x03', lambda m: '\x03' + m.group(1), processed_text)

    # 3. Restore the markers as explicit brackets without surrounded whitespace
    processed_text = processed_text.replace('\x02', '[').replace('\x03', ']')

    final_words = []
    pending_notes = []

    def attach_notes(word):
        for kind, body in pending_notes:
            if kind == 'x' and not XREF_AS_FOOTNOTE:
                word['crossref'] = f"{word['crossref']}; {body}" if word['crossref'] else body
            else:
                word['footnote'] = f"{word['footnote']}; {body}" if word['footnote'] else body
        pending_notes.clear()

    pending_par = None  # line-start class (paragraph / poetry level) waiting for its first word
    for token in processed_text.split():
        token = token.strip()
        if not token:
            continue

        m_note = NOTE_MARK_RE.match(token)
        if m_note:
            kind, body = notes[int(m_note.group(1))]
            if body:
                pending_notes.append((kind, body))
            if final_words:
                attach_notes(final_words[-1])
            continue

        m_line = LINE_MARK_RE.match(token)
        if m_line:
            pending_par = m_line.group(1).replace('~', ' ')
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
            'crossref': None,
            'par': pending_par,
            'subverse': None,  # set by flush_verse on the first word of a lettered run (46a/46b/...)
        })
        pending_par = None
        if pending_notes:          # note appeared before any word: attach to first word
            attach_notes(final_words[-1])

    return final_words


# ---------------------------------------------------------------------------
# Paragraph / poetry marker handling
# ---------------------------------------------------------------------------
# Each line-level marker is turned into a class string stored in words.par_classes
# on the FIRST word of that line (same per-word convention the Berean TSV importer
# uses).  If your frontend/CSS uses different names, change paragraph_class() only.
MARKER_RE = re.compile(r'^\\([a-z]+)(\d*)(?=[\s\\]|$)\s*(.*)$', re.IGNORECASE)
LINE_MARK_RE = re.compile(r'^__LINE_([a-z0-9~\-]+)__$')

PARA_TAGS = {'p', 'm', 'pmo', 'pm', 'pmc', 'pmr', 'pi', 'mi', 'pc', 'pr', 'ph', 'cls',
             'li', 'lim', 'q', 'qm', 'qr', 'qc'}
HEADING_TAGS = {'s', 'ms', 'qa'}          # section / major section / acrostic heading
# Structural or non-scripture lines that must never leak into verse text
IGNORED_TAGS = {'ide', 'h', 'toc', 'mt', 'mte', 'imt', 'imte', 'is', 'ip', 'ipi', 'im', 'imi',
                'ipq', 'imq', 'ipr', 'iq', 'ib', 'ili', 'iot', 'io', 'iex', 'ie', 'r', 'mr',
                'sr', 'sp', 'cl', 'cd', 'cp', 'ca', 'rem', 'sts', 'usfm', 'periph'}


def paragraph_class(tag, level):
    """Class names match the CSS in index.html (p.block.indent1/2/3/4, .indent1stline, ...)."""
    n = min(int(level or 1), 4)
    if tag in ('p', 'pmo', 'pm', 'pmc', 'pmr'):
        return 'paragraph'
    if tag == 'm':                       # flush-left continuation: a line break, not a new paragraph
        return 'cont'
    if tag in ('pi', 'ph'):              # first-line-indented paragraph
        return 'paragraph indent1stline'
    if tag == 'mi':                      # whole block indented
        return 'paragraph indent1'
    if tag == 'pc':
        return 'paragraph center'
    if tag in ('pr', 'cls'):
        return 'paragraph right'
    if tag == 'li':                      # list item: hanging indent, level 1-4
        return f'list indent{n}'
    if tag == 'lim':                     # embedded list item: one level deeper, marked .embedded
        return f'list embedded indent{min(n + 1, 4)}'
    if tag == 'q':
        return f'poetry indent{n}'
    if tag == 'qm':                      # embedded poetry sits one level deeper
        return f'poetry indent{min(n + 1, 4)}'
    if tag == 'qr':
        return 'poetry right'
    if tag == 'qc':
        return 'poetry center'
    return 'paragraph'


def clean_inline(text):
    """Strip footnotes, cross-refs and character markers from heading text."""
    text = re.sub(r'\\f\s+.*?\\f\*', '', text)
    text = re.sub(r'\\x\s+.*?\\x\*', '', text)
    text = re.sub(r'\\\+?[a-z0-9-]+\*?', '', text)
    return re.sub(r'\s+', ' ', text).strip()


def parse_usfm_file(file_path):
    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()
    
    # Convert all straight apostrophes to curly apostrophes right after reading
    content = content.replace("'", "’")

    current_book_raw = None
    current_chapter = 0

    # State that describes the verse currently being collected
    current_verse_range = []
    current_verse_tokens = []
    verse_heading = None
    verse_heading_type = None   # 'psalm' when verse_heading is a pure \d descriptive title; None otherwise
    current_subverse_letter = None   # e.g. 'b' for a "\v 46b" continuation

    # State set by markers that PRECEDE the text they apply to
    pending_heading = None
    pending_heading_type = None  # mirrors verse_heading_type, for the not-yet-bound heading
    pending_base = None      # e.g. 'poetry indent-2', 'paragraph'
    pending_flags = []       # e.g. 'stanza', 'pshdg'

    parsed_verses = []

    def merge_heading(text_a, type_a, text_b, type_b):
        """Concatenates two heading fragments with an em dash, and folds their
        types together: 'psalm' only survives if BOTH sides agree it's a pure
        psalm superscription (\\d) — mixing in any \\s/\\ms/\\qa content falls
        back to the plain section-heading style, since a compound heading no
        longer reads as just the psalm's own title."""
        if not text_a:
            return text_b, type_b
        if not text_b:
            return text_a, type_a
        return f'{text_a} \u2014 {text_b}', (type_a if type_a == type_b else None)

    def add_heading(text, kind=None):
        nonlocal pending_heading, pending_heading_type
        text = clean_inline(text)
        if text:
            pending_heading, pending_heading_type = merge_heading(
                pending_heading, pending_heading_type, text, kind
            )

    def flush_verse():
        nonlocal current_verse_tokens, verse_heading, verse_heading_type, pending_heading, pending_heading_type, current_subverse_letter
        if current_verse_range and current_verse_tokens:
            words = parse_usfm_word_tokens(' '.join(current_verse_tokens))
            if words:
                fts_plain_text = ' '.join(w['gloss'] for w in words)
                # Lettered subverses ("\v 46a", "\v 46b", ... "\v 46f" — the LXX
                # of 1 Kings 3 is full of these) all share the SAME printed verse
                # number. Tag the first word of each lettered run so the letter
                # survives even though there's no separate verse row for it.
                if current_subverse_letter and words:
                    words[0]['subverse'] = current_subverse_letter
                for v_num in current_verse_range:
                    # If the immediately preceding parsed verse is this same
                    # chapter/verse (the normal case for 46a immediately
                    # followed by 46b, 46c, ...), this is a CONTINUATION of
                    # that verse's text, not a separate verse — merge the
                    # words in rather than appending a duplicate record.
                    # import_usfm_translation upserts by (chapter, verse), so
                    # two separate records for the same number would silently
                    # overwrite one another and only the last would survive.
                    prev = parsed_verses[-1] if parsed_verses else None
                    if (prev and prev['book_raw'] == current_book_raw and
                            prev['chapter'] == current_chapter and prev['verse'] == v_num):
                        prev['words'].extend(words)
                        prev['fts_text'] = (prev['fts_text'] + ' ' + fts_plain_text).strip()
                        if verse_heading and not prev['heading']:
                            prev['heading'] = verse_heading
                            prev['heading_type'] = verse_heading_type
                    else:
                        parsed_verses.append({
                            'book_raw': current_book_raw,
                            'chapter': current_chapter,
                            'verse': v_num,
                            'words': words,
                            'fts_text': fts_plain_text,
                            'heading': verse_heading,
                            'heading_type': verse_heading_type,
                        })
            elif verse_heading:  # empty verse: don't lose its heading
                pending_heading, pending_heading_type = merge_heading(
                    verse_heading, verse_heading_type, pending_heading, pending_heading_type
                )
        current_verse_tokens = []
        verse_heading = None
        verse_heading_type = None
        current_subverse_letter = None

    def start_verse(spec):
        nonlocal current_verse_range, verse_heading, verse_heading_type, pending_heading, pending_heading_type, current_subverse_letter
        flush_verse()
        # A lettered spec ("46a", "46f") names a subverse: the same printed
        # verse number as its neighbors, distinguished only by a trailing
        # letter — not a genuine new verse number, and not a range/list (those
        # never carry letters in practice), so it gets its own simple match
        # ahead of the general digit-extraction below.
        m_sub = re.match(r'^(\d+)([a-zA-Z])$', spec.strip())
        if m_sub:
            current_verse_range = [int(m_sub.group(1))]
            current_subverse_letter = m_sub.group(2).lower()
        else:
            current_subverse_letter = None
            nums = [int(n) for n in re.findall(r'\d+', spec)]
            if len(nums) >= 2:
                current_verse_range = list(range(nums[0], nums[-1] + 1))
            elif len(nums) == 1:
                current_verse_range = [nums[0]]
            else:
                current_verse_range = []
        # Bind heading to the verse that STARTS after it (not the one before it)
        verse_heading, pending_heading = pending_heading, None
        verse_heading_type, pending_heading_type = pending_heading_type, None

    def add_text(text):
        nonlocal pending_base, pending_flags
        text = text.strip()
        if not text or not current_verse_range:
            return
        cls = ' '.join(filter(None, [pending_base] + pending_flags))
        if cls:
            current_verse_tokens.append('__LINE_' + cls.replace(' ', '~') + '__')
            pending_base, pending_flags = None, []
        current_verse_tokens.append(text)

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        m = MARKER_RE.match(line)
        tag = m.group(1).lower() if m else None
        level = m.group(2) if m else ''
        rest = m.group(3) if m else line

        if tag == 'id':
            m_id = re.search(r'\\id\s+([A-Z0-9]{3})', line, re.IGNORECASE)
            if m_id:
                current_book_raw = m_id.group(1).upper()
            continue

        if tag == 'c':
            mc = re.match(r'(\d+)', rest)
            if mc:
                flush_verse()
                current_chapter = int(mc.group(1))
                current_verse_range = []
            continue

        if tag in IGNORED_TAGS:
            continue
        if tag in HEADING_TAGS:
            add_heading(rest)
            continue
        d_is_verse = False
        if tag == 'd':                       # descriptive title / psalm superscription
            # 'pshdg' names this paragraph-line class (matches the class
            # Berean's own TSV export uses for the same thing — see
            # import_berean_tsv.py and .block.pshdg in index.html), so the
            # frontend only needs one check to recognize a psalm
            # superscription line from either importer.
            if 'pshdg' not in pending_flags:
                pending_flags.append('pshdg')
            if re.search(r'\\v\s', rest):
                # LXX style: "\d \v 1 A Psalm of David." -- the title IS verse text (verse 1),
                # not a heading. Keep the 'pshdg' flag for the title line and fall through so
                # the \v is processed. (MT style, a bare "\d Title" with no \v, stays a heading.)
                d_is_verse = True
            else:
                add_heading(rest, 'psalm')
                continue
        if tag == 'b':                       # blank line = stanza break
            if 'stanza' not in pending_flags:
                pending_flags.append('stanza')
            continue
        if tag == 'nb':                      # "no break" cancels a paragraph start
            pending_base = None
            continue

        if d_is_verse:
            text = rest
        elif tag in PARA_TAGS:
            pending_base = paragraph_class(tag, level)
            text = rest
        else:
            text = line

        # Verse markers may open a line OR appear mid-line (e.g. "\q2 text \v 4 more")
        for piece in re.split(r'(?=\\v\s)', text):
            piece = piece.strip()
            if not piece:
                continue
            mv = re.match(r'\\v\s+([0-9a-zA-Z\-,]+)\s*(.*)', piece)
            if mv:
                start_verse(mv.group(1))
                add_text(mv.group(2))
            else:
                add_text(piece)

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
            par_classes = w_obj.get('par')  # set on the first word of each paragraph/poetry line
            hdg = rec['heading'] if pos == 0 else None
            hdg_type = rec.get('heading_type') if pos == 0 else None

            beg_q = w_obj['beg_q']
            sub = w_obj.get('subverse')
            if sub and sub != 'a':
                # Mark where a lettered subverse (46b, 46c, ...) begins inline,
                # since it has no verse row of its own to carry the label —
                # skip 'a' itself, since that's just the verse's normal start.
                marker = SUPERSCRIPT_LETTERS.get(sub, sub)
                beg_q = (marker + ' ' + beg_q).strip() if beg_q else marker

            if w_obj.get('strong'):
                word_lang = 'Hebrew' if w_obj['strong'].startswith('H') else 'Greek' if w_obj['strong'].startswith('G') else 'English'
            elif w_obj.get('original'):
                word_lang = 'Hebrew' if testament == 'OT' else 'Greek'
            else:
                word_lang = 'English'

            con.execute(
                """INSERT INTO words(
                    verse_id, position, lang, original, translit, parsing, strong,
                    gloss, beg_q, pnc, end_q, end_text, footnote, hdg, hdg_type, crossref, par_classes
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    verse_id, pos, word_lang,
                    w_obj.get('original'),
                    None, None,
                    w_obj.get('strong'),
                    w_obj['gloss'],
                    beg_q,
                    w_obj['pnc'],
                    '', '',
                    w_obj.get('footnote'),
                    hdg,
                    hdg_type,
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