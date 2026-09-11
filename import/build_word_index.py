#!/usr/bin/env python3
"""
build_word_index.py — Builds the "word study" occurrence/gloss index for
River's SQLite DB (bible.sqlite): powers the "All occurrences" sidebar
reached from a word's popover.

Creates:
  1. lexicon_word_occurrences (plain table): one row per distinct
     (strong, book, chapter, verse) location where a Strong's-numbered
     word occurs anywhere in the Bible, with a repeat count for words
     that occur more than once in the same verse. Location data is
     translation-independent (the underlying original-language word
     lives at the same verse regardless of which translation's
     interlinear you're reading), so this is deduplicated across every
     translation in the database rather than multiplied by how many
     translations exist.
  2. lexicon_gloss_stats (plain table): for each (strong, translation),
     how many times each distinct gloss was used to render that word —
     lets the UI show, per translation, which English glosses are most
     common for a given original-language word, ordered by frequency.
     Glosses are normalized before counting: any bracket-wrapped supplied
     word (e.g. "the [one]") has the bracketed portion stripped entirely,
     and glosses are folded case-insensitively so "The", "the", and "THE"
     all count as the same gloss — except for "god"/"God" and
     "lord"/"Lord"/"LORD", whose casing is theologically significant
     (e.g. LORD vs. Lord vs. lord) and so is always kept distinct. The
     stored gloss text is the most frequently occurring literal casing
     within each normalized group.

Both tables are keyed on the same normalized Strong's number used
elsewhere in River (see NORMALIZED_WORD_STRONG in db-queries.js):
words.strong is sometimes stored bare ('3056') rather than prefixed
('G3056'), so this script applies the same lang-based normalization
before aggregating, and always stores the prefixed, uppercase form
(e.g. 'G3056') so it matches getLexiconEntry()'s STRONG_RE expectations
and joins cleanly against lexicon_words.strong.

Usage:
    python3 build_word_index.py --db /path/to/bible.sqlite
"""

import argparse
import re
import sqlite3
import sys
import time

# Matches a bracket-wrapped "supplied" word/phrase, the same convention
# rendered with a dotted underline in the UI (see renderGlossHtml in
# index.html): "[one]" or "{to rise}". Stripped out (brackets and all)
# before a gloss is counted, so "the [one]" and "the" fold together.
BRACKETED_RE = re.compile(r'[\[{][^\]}]*[\]}]')

# Words whose casing carries real theological distinction and should
# never be folded together, however they're capitalized elsewhere in the
# gloss: "God" (deity) vs. "god" (generic/pagan), and "Lord"/"LORD"/"lord"
# (kurios/adonai vs. the divine name YHWH vs. a human lord).
CASE_SENSITIVE_WORDS_RE = re.compile(r'\b(god|lord)\b', re.IGNORECASE)


def strip_bracketed_words(gloss):
    """Removes every bracket-wrapped supplied word/phrase from a gloss
    (brackets and contents), then collapses any resulting doubled-up
    whitespace left behind."""
    stripped = BRACKETED_RE.sub('', gloss)
    return re.sub(r'\s+', ' ', stripped).strip()


def gloss_dedup_key(gloss):
    """Builds the case-folded key used to group glosses that should be
    counted as 'the same' gloss. Lowercases everything except whole-word
    occurrences of god/lord, which keep their original casing so distinct
    capitalizations (God vs. god, Lord vs. LORD vs. lord) stay separate
    buckets rather than merging with a generic case-insensitive fold."""
    out, last = [], 0
    for m in CASE_SENSITIVE_WORDS_RE.finditer(gloss):
        out.append(gloss[last:m.start()].lower())
        out.append(m.group(0))
        last = m.end()
    out.append(gloss[last:].lower())
    return ''.join(out)


def ensure_index_schema(con):
    """Ensures lexicon_word_occurrences/lexicon_gloss_stats exist, recreating
    them from scratch so a rerun always reflects the current word data."""
    con.executescript("""
        DROP TABLE IF EXISTS lexicon_word_occurrences;
        DROP TABLE IF EXISTS lexicon_gloss_stats;

        -- One row per distinct location a Strong's-numbered word occurs at,
        -- deduplicated across translations. occurrence_count handles a word
        -- appearing more than once in the same verse.
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

        -- Per-translation gloss frequency for a given Strong's number —
        -- how many times each distinct rendering was used across the whole
        -- Bible in that translation.
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
    """Mirrors NORMALIZED_WORD_STRONG in db-queries.js: if words.strong is
    already prefixed with G/H (any case), keep the number but uppercase the
    prefix; otherwise prepend G or H based on the word's language. Always
    returns an uppercase-prefixed Strong's number, or None if raw is empty."""
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
    """Scans every word token across every translation, aggregating
    occurrence locations and per-translation gloss frequencies keyed by
    normalized Strong's number."""
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

    # (strong, translation_id, book_id, chapter, verse) -> count of tokens
    # with that Strong's number at that location, in that translation.
    per_translation_counts = {}
    # (strong, book_id, chapter, verse) -> (book_name, book_order, testament),
    # kept alongside per_translation_counts so we can look up display fields
    # once we've collapsed across translations.
    location_meta = {}
    # (strong, translation_id, dedup_key) -> total occurrence count, keyed
    # on the case-folded/bracket-stripped form so e.g. "The [one]", "the",
    # and "THE" all accumulate under one bucket.
    gloss_counts = {}
    # (strong, translation_id, dedup_key) -> { literal_gloss: count }, kept
    # alongside gloss_counts so that once counting is done we can pick the
    # most common literal casing/spacing to actually display for that key.
    gloss_display_counts = {}

    rows_seen = 0
    start_time = time.time()

    for row in cursor:
        (trans_id, book_id, book_name, book_order, testament,
         chapter, verse, raw_strong, lang, gloss) = row

        strong = normalize_strong(raw_strong, lang)
        if not strong:
            continue

        rows_seen += 1

        loc_key = (strong, book_id, chapter, verse)
        pt_key = (strong, trans_id, book_id, chapter, verse)
        per_translation_counts[pt_key] = per_translation_counts.get(pt_key, 0) + 1
        if loc_key not in location_meta:
            location_meta[loc_key] = (book_name, book_order, testament)

        gloss = strip_bracketed_words(gloss or '')
        if gloss:
            dedup_key = gloss_dedup_key(gloss)
            g_key = (strong, trans_id, dedup_key)
            gloss_counts[g_key] = gloss_counts.get(g_key, 0) + 1
            display_counts = gloss_display_counts.setdefault(g_key, {})
            display_counts[gloss] = display_counts.get(gloss, 0) + 1

    print(f"Scanned {rows_seen} Strong's-numbered word tokens.")

    # Collapse per-translation counts down to one row per location: take the
    # max across translations rather than summing, so a word that occurs
    # twice in a verse still reads as 2 occurrences — not 2 x number of
    # translations that happen to carry that verse.
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

    # For each normalized (strong, translation, dedup_key) bucket, display
    # the literal casing/spacing that occurred most often — ties broken
    # alphabetically for a stable, deterministic result across reruns.
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
