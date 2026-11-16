#!/usr/bin/env python3
"""
build_word_index.py — Builds the "word study" occurrence/gloss index for
River's SQLite DB (bible.sqlite): powers the "All occurrences" sidebar
reached from a word's popover.

Creates:
  1. lexicon_word_occurrences
  2. lexicon_gloss_stats (grouped using NLTK-based lemmatization)

Usage:
    python3 build_word_index.py --db /path/to/bible.sqlite
"""

import argparse
import re
import sqlite3
import sys
import time

import nltk

# Import NLTK lemmatizer
from lemmatizer import lemmatize_phrase

BRACKETED_RE = re.compile(r'[\[{][^\]}]*[\]}]')
CASE_SENSITIVE_WORDS_RE = re.compile(r'\b(god|lord)\b', re.IGNORECASE)
# Matches filler or place-marker glosses like "vvv", "-", "--", ". . .", or "..."
PLACEHOLDER_RE = re.compile(r'^(vvv|-+|\.\s*\.\s*\.|\.\.\.)$', re.IGNORECASE)

def strip_bracketed_words(gloss):
    """
    Removes bracket-wrapped supplied words from a gloss, collapses extra spaces,
    and maps placeholder tokens (like 'vvv' or '-') to '. . .'.
    """
    stripped = BRACKETED_RE.sub('', gloss)
    cleaned = re.sub(r'\s+', ' ', stripped).strip()
    
    # Map filler tokens (vvv, -, --, ..., . . .) to a single standard gloss
    if PLACEHOLDER_RE.match(cleaned):
        return '. . .'
        
    return cleaned


def gloss_dedup_key(gloss):
    """
    Builds the lemmatized and case-adjusted key used to group glosses.
    """
    if gloss == '. . .':
        return '. . .'

    tokens = nltk.word_tokenize(gloss)
    if not tokens:
        return ""

    # POS-tag the tokens to identify proper nouns
    tagged = nltk.pos_tag(tokens)
    normalized_tokens = []

    for word, tag in tagged:
        # 1. Check for theological exceptions (God/Lord/LORD)
        if CASE_SENSITIVE_WORDS_RE.match(word):
            normalized_tokens.append(word)
        # 2. Check for Proper Nouns (names, places) -> keep original capitalization
        elif tag in ('NNP', 'NNPS'):
            normalized_tokens.append(word)
        # 3. Everything else (common words like "Book") -> lowercase
        else:
            normalized_tokens.append(word.lower())

    normalized_gloss = " ".join(normalized_tokens)

    # Handle multi-word vs single-word lemmatization
    has_matches = bool(CASE_SENSITIVE_WORDS_RE.search(normalized_gloss))

    if has_matches:
        out, last = [], 0
        for m in CASE_SENSITIVE_WORDS_RE.finditer(normalized_gloss):
            prefix = normalized_gloss[last:m.start()]
            if prefix:
                out.append(lemmatize_phrase(prefix))
            out.append(m.group(0))
            last = m.end()
        suffix = normalized_gloss[last:]
        if suffix:
            out.append(lemmatize_phrase(suffix))
        return "".join(out)

    return lemmatize_phrase(normalized_gloss)


def ensure_index_schema(con):
    """Ensures index tables exist, recreating them from scratch."""
    con.executescript("""
        DROP TABLE IF EXISTS lexicon_word_occurrences;
        DROP TABLE IF EXISTS lexicon_gloss_stats;

        CREATE TABLE lexicon_word_occurrences (
            strong           TEXT NOT NULL,
            book_id          INTEGER NOT NULL,
            book_name        TEXT NOT NULL,
            book_order       INTEGER NOT NULL,
            testament        TEXT,
            chapter          INTEGER NOT NULL,
            verse            INTEGER NOT NULL,
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (strong, book_id, chapter, verse)
        );
        CREATE INDEX idx_word_occurrences_strong
            ON lexicon_word_occurrences(strong, book_order, chapter, verse);

        CREATE TABLE lexicon_gloss_stats (
            strong           TEXT NOT NULL,
            translation_id   INTEGER NOT NULL,
            gloss            TEXT NOT NULL,
            occurrence_count INTEGER NOT NULL,
            PRIMARY KEY (strong, translation_id, gloss)
        );
        CREATE INDEX idx_gloss_stats_strong
            ON lexicon_gloss_stats(strong, occurrence_count DESC);
    """)


def normalize_strong(raw, lang):
    """Normalizes Strong's numbers to uppercase prefixed format (e.g., G3056)."""
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw[0] in ('G', 'g', 'H', 'h'):
        return raw[0].upper() + raw[1:]
    prefix = 'H' if lang == 'Hebrew' else 'G'
    return prefix + raw


def rebuild_word_index(con):
    """Scans word tokens and aggregates occurrences and lemmatized gloss stats."""
    ensure_index_schema(con)

    print("Fetching word-level records across all translations...")

    query = """
        SELECT
            v.translation_id,
            v.book_id,
            b.name AS book_name,
            b.book_order,
            b.testament,
            v.chapter,
            v.verse,
            w.position,
            w.strong,
            w.lang,
            COALESCE(e.gloss, w.gloss, w.original, '') AS display_gloss
        FROM verses v
        JOIN books b ON b.id = v.book_id
        JOIN words w ON w.verse_id = v.id
        LEFT JOIN edits e ON e.word_id = w.id
        WHERE w.strong IS NOT NULL AND trim(w.strong) != ''
        ORDER BY v.translation_id, b.book_order, v.chapter, v.verse, w.position;
    """

    cursor = con.execute(query)

    per_translation_counts = {}
    location_meta = {}
    gloss_counts = {}
    gloss_display_counts = {}

    rows_seen = 0
    start_time = time.time()

    # Some source USFM (e.g. WEB-with-Strong's) splits a single original-
    # language word's English gloss across multiple adjacent \w tags that
    # each carry the same Strong's number (e.g. "has" / "eaten" for one
    # Greek verb), rather than wrapping the whole phrase in one tag the way
    # other translations do. Tallying each tag independently corrupts both
    # the occurrence counts and the gloss statistics with word fragments.
    # To fix this without touching how words are imported/stored, we fold
    # contiguous rows here — same translation, same verse, same normalized
    # Strong's number, and consecutive w.position values — into a single
    # logical occurrence before anything gets counted. A translation that
    # already stores one row per original word (single- or multi-word gloss)
    # is unaffected: every "group" is just size 1, a no-op.
    group_key = None       # (trans_id, book_id, chapter, verse, strong)
    group_position = None  # position of the last row folded into the group
    group_meta = None      # (book_name, book_order, testament)
    group_gloss_parts = []

    def flush_group():
        nonlocal group_key, group_position, group_meta, group_gloss_parts
        if group_key is None:
            return
        trans_id, book_id, chapter, verse, strong = group_key
        book_name, book_order, testament = group_meta

        loc_key = (strong, book_id, chapter, verse)
        pt_key = (strong, trans_id, book_id, chapter, verse)
        per_translation_counts[pt_key] = per_translation_counts.get(pt_key, 0) + 1
        if loc_key not in location_meta:
            location_meta[loc_key] = (book_name, book_order, testament)

        merged_gloss = ' '.join(p for p in group_gloss_parts if p).strip()
        gloss = strip_bracketed_words(merged_gloss)
        if gloss:
            dedup_key = gloss_dedup_key(gloss)
            g_key = (strong, trans_id, dedup_key)
            gloss_counts[g_key] = gloss_counts.get(g_key, 0) + 1
            display_counts = gloss_display_counts.setdefault(g_key, {})
            display_counts[gloss] = display_counts.get(gloss, 0) + 1

        group_key = None
        group_position = None
        group_meta = None
        group_gloss_parts = []

    for row in cursor:
        (trans_id, book_id, book_name, book_order, testament,
         chapter, verse, position, raw_strong, lang, gloss) = row

        strong = normalize_strong(raw_strong, lang)
        if not strong:
            continue

        rows_seen += 1

        key = (trans_id, book_id, chapter, verse, strong)
        contiguous = (
            group_key == key and
            group_position is not None and
            position == group_position + 1
        )

        if not contiguous:
            flush_group()
            group_key = key
            group_meta = (book_name, book_order, testament)

        group_gloss_parts.append(gloss or '')
        group_position = position

    flush_group()

    print(f"Scanned {rows_seen} Strong's-numbered word tokens.")

    print("Collapsing per-translation counts into location occurrences...")
    max_count_by_location = {}
    for (strong, trans_id, book_id, chapter, verse), count in per_translation_counts.items():
        loc_key = (strong, book_id, chapter, verse)
        if count > max_count_by_location.get(loc_key, 0):
            max_count_by_location[loc_key] = count

    occurrence_rows = []
    for loc_key, count in max_count_by_location.items():
        strong, book_id, chapter, verse = loc_key
        book_name, book_order, testament = location_meta[loc_key]
        occurrence_rows.append((strong, book_id, book_name, book_order, testament, chapter, verse, count))

    gloss_rows = []
    for (strong, trans_id, dedup_key), count in gloss_counts.items():
        display_counts = gloss_display_counts[(strong, trans_id, dedup_key)]
        display_gloss = sorted(display_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        gloss_rows.append((strong, trans_id, display_gloss, count))

    print(f"Inserting {len(occurrence_rows)} rows into 'lexicon_word_occurrences'...")
    con.executemany("""
        INSERT INTO lexicon_word_occurrences
            (strong, book_id, book_name, book_order, testament, chapter, verse, occurrence_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?);
    """, occurrence_rows)

    print(f"Inserting {len(gloss_rows)} rows into 'lexicon_gloss_stats'...")
    con.executemany("""
        INSERT INTO lexicon_gloss_stats (strong, translation_id, gloss, occurrence_count)
        VALUES (?, ?, ?, ?);
    """, gloss_rows)

    elapsed = time.time() - start_time
    strong_count = len(set(r[0] for r in occurrence_rows))
    print(f"Word index rebuild completed successfully in {elapsed:.2f} seconds "
          f"({strong_count} distinct Strong's numbers, {len(occurrence_rows)} locations).")


def main():
    parser = argparse.ArgumentParser(description="Build River's word-study occurrence/gloss index")
    parser.add_argument('--db', default='bible.sqlite', help="Path to SQLite database file")
    args = parser.parse_args()

    con = sqlite3.connect(args.db, timeout=30.0)
    con.execute("PRAGMA foreign_keys = ON;")

    try:
        with con:
            rebuild_word_index(con)
    except Exception as e:
        sys.exit(f"Index build failed: {e}")
    finally:
        con.close()


if __name__ == '__main__':
    main()