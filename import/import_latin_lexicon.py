#!/usr/bin/env python3
"""
import_latin_lexicon.py — Importer for A Latin Dictionary (Lewis & Short,
1879), Perseus Digital Library's TEI XML edition, into bible.sqlite's
lexicon_words / lexicon_entries / lexicon_sources tables.
"""

import argparse
import html as html_module
import re
import sqlite3
import sys
import unicodedata

DEFAULT_COPYRIGHT = (
    "Text provided under a CC BY-SA license by Perseus Digital Library, "
    "http://www.perseus.tufts.edu, with funding from The National Endowment "
    "for the Humanities. Data accessed from https://github.com/PerseusDL/lexica/."
)

ENTRY_RE = re.compile(r'<entryFree\b([^>]*)>(.*?)</entryFree>', re.DOTALL)
ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
ORTH_RE = re.compile(r'<orth\b[^>]*>(.*?)</orth>', re.DOTALL)
FIRST_TRANS_RE = re.compile(r'<trans\b[^>]*>\s*<tr\b[^>]*>(.*?)</tr>', re.DOTALL)
TAG_RE = re.compile(r'<[^>]+>')


def ensure_schema(conn):
    """Creates the three lexicon tables if they don't already exist."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS lexicon_words (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strong TEXT UNIQUE NOT NULL,
            lang TEXT NOT NULL,
            lexeme TEXT NOT NULL,
            translit TEXT,
            pronunciation TEXT,
            short_gloss TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_lexicon_strong ON lexicon_words(strong);

        CREATE TABLE IF NOT EXISTS lexicon_sources (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            copyright TEXT
        );

        CREATE TABLE IF NOT EXISTS lexicon_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strong_id INTEGER NOT NULL REFERENCES lexicon_words(id) ON DELETE CASCADE,
            source_id INTEGER NOT NULL REFERENCES lexicon_sources(id) ON DELETE CASCADE,
            entry_html TEXT NOT NULL,
            UNIQUE(strong_id, source_id)
        );
        CREATE INDEX IF NOT EXISTS idx_lexicon_entries ON lexicon_entries(strong_id, source_id);
    """)


def strip_diacritics(text):
    """NFD-decompose and drop combining marks, so ăb -> ab."""
    decomposed = unicodedata.normalize('NFD', text)
    return ''.join(ch for ch in decomposed if not unicodedata.combining(ch))


def strip_tags(text):
    """Bare text content only — used for orth/translit/short_gloss extraction."""
    return html_module.unescape(TAG_RE.sub('', text)).strip()


def entry_to_html(inner):
    """Full entry body -> readable HTML."""
    text = inner
    text = re.sub(r'<hi\s+rend="ital"[^>]*>(.*?)</hi>', r'<em>\1</em>', text, flags=re.DOTALL)
    text = re.sub(r'<foreign\s+lang="greek"[^>]*>(.*?)</foreign>', r'<span class="lex-greek">\1</span>', text, flags=re.DOTALL)
    text = re.sub(r'</sense>', '</p><p>', text)
    text = re.sub(r'<sense\b[^>]*>', '', text)
    text = re.sub(r'</?(?!em\b|/?em\b|span\b|/?span\b)[a-zA-Z][^>]*>', '', text)
    text = html_module.unescape(text)
    text = '<p>' + text.strip() + '</p>'
    text = text.replace('<p></p>', '')
    return text


def parse_entries(xml_text):
    for attrs_raw, inner in ENTRY_RE.findall(xml_text):
        attrs = dict(ATTR_RE.findall(attrs_raw))
        orth_match = ORTH_RE.search(inner)
        if not orth_match:
            continue
        orth_raw = strip_tags(orth_match.group(1))
        if not orth_raw:
            continue
        translit = strip_diacritics(orth_raw)
        trans_match = FIRST_TRANS_RE.search(inner)
        short_gloss = strip_tags(trans_match.group(1)) if trans_match else None
        yield {
            'key': attrs.get('key', ''),
            'orth': orth_raw,
            'translit': translit,
            'short_gloss': short_gloss,
            'entry_html': entry_to_html(inner),
        }


def main():
    ap = argparse.ArgumentParser(description="Import Lewis & Short's A Latin Dictionary into River's lexicon tables")
    ap.add_argument('xml_file', help='Path to lat.ls.perseus-eng2.xml')
    ap.add_argument('--source-code', default='LS')
    ap.add_argument('--source-name', default='A Latin Dictionary (Lewis & Short, 1879)')
    ap.add_argument('--copyright', default=DEFAULT_COPYRIGHT)
    ap.add_argument('--db', default='bible.sqlite')
    ap.add_argument('--replace', action='store_true', help='Re-import, replacing any existing LS entries/words first')
    args = ap.parse_args()

    with open(args.xml_file, encoding='utf-8') as f:
        xml_text = f.read()

    con = sqlite3.connect(args.db)
    con.execute('PRAGMA foreign_keys = ON')

    with con:
        ensure_schema(con)

        existing_source = con.execute('SELECT id FROM lexicon_sources WHERE code=?', (args.source_code,)).fetchone()
        if existing_source and not args.replace:
            sys.exit(f'Lexicon source "{args.source_code}" already exists — pass --replace to reimport it.')
        if existing_source:
            source_id = existing_source[0]
            con.execute('UPDATE lexicon_sources SET name=?, copyright=? WHERE id=?',
                        (args.source_name, args.copyright, source_id))
            con.execute(
                'DELETE FROM lexicon_entries WHERE source_id=?', (source_id,))
            con.execute(
                "DELETE FROM lexicon_words WHERE strong LIKE 'L%' AND id NOT IN "
                "(SELECT strong_id FROM lexicon_entries)"
            )
        else:
            cur = con.execute('INSERT INTO lexicon_sources(code, name, copyright) VALUES (?,?,?)',
                              (args.source_code, args.source_name, args.copyright))
            source_id = cur.lastrowid

        count = 0
        skipped_dupe = 0
        for i, entry in enumerate(parse_entries(xml_text), start=1):
            strong = f'L{i}'
            try:
                cur = con.execute(
                    'INSERT INTO lexicon_words(strong, lang, lexeme, translit, pronunciation, short_gloss) '
                    'VALUES (?,?,?,?,?,?)',
                    (strong, 'Latin', entry['orth'], entry['translit'], None, entry['short_gloss'])
                )
                word_id = cur.lastrowid
            except sqlite3.IntegrityError:
                skipped_dupe += 1
                continue
            con.execute(
                'INSERT INTO lexicon_entries(strong_id, source_id, entry_html) VALUES (?,?,?)',
                (word_id, source_id, entry['entry_html'])
            )
            count += 1

    print(f'Imported {count} Latin lexicon entries from Lewis & Short into {args.db}.')
    if skipped_dupe:
        print(f'Skipped {skipped_dupe} due to strong-ID collisions — run with --replace for a clean reimport.', file=sys.stderr)


if __name__ == '__main__':
    main()