#!/usr/bin/env python3
"""
build_word_index.py — Builds the "word study" occurrence/gloss index for
River's SQLite DB (bible.sqlite): powers the "All occurrences" sidebar
reached from a word's popover.

Creates:
  1. lexicon_word_occurrences (plain table): one row per distinct
     (key, book, chapter, verse) location where a word occurs anywhere in
     the Bible, with a repeat count for words that occur more than once
     in the same verse. Location data is translation-independent (the
     underlying original-language word lives at the same verse regardless
     of which translation's interlinear you're reading), so this is
     deduplicated across every translation in the database rather than
     multiplied by how many translations exist.
  2. lexicon_gloss_stats (plain table): for each (key, translation), how
     many times each distinct gloss was used to render that word — lets
     the UI show, per translation, which English glosses are most common
     for a given original-language word, ordered by frequency. Glosses
     are normalized before counting: any bracket-wrapped supplied word
     (e.g. "the [one]") has the bracketed portion stripped entirely, and
     glosses are folded case-insensitively so "The", "the", and "THE" all
     count as the same gloss — except for "god"/"God" and
     "lord"/"Lord"/"LORD", whose casing is theologically significant
     (e.g. LORD vs. Lord vs. lord) and so is always kept distinct. The
     stored gloss text is the most frequently occurring literal casing
     within each normalized group.

Grouping key: most words carry a Strong's number, and for those this is
the same normalized, prefixed form used elsewhere in River (see
NORMALIZED_WORD_STRONG in db-queries.js) — words.strong is sometimes
stored bare ('3056') rather than prefixed ('G3056'), so this applies the
same lang-based normalization before aggregating. But not every word has
one — an LXX-only lemma awaiting a Strong's-style ID, a supplied word, or
anything else an importer left untagged — and those used to be dropped
from this index entirely, invisible to "All occurrences" even though the
popover itself could show their lemma. Such words are now grouped by a
synthetic key built from words.lemma instead (see LEMMA_KEY_PREFIX/
resolve_word_key below), so occurrences of the same untagged lemma still
collect together. The 'strong' column on both tables holds this
grouping key either way — genuinely a Strong's number for Strong's-keyed
rows, a LEX:-prefixed synthetic key for lemma-keyed ones — and a sibling
'lemma' column carries the lemma text alongside it for display, when
known, regardless of which kind of key the row has.

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

        -- One row per distinct location a word occurs at, deduplicated
        -- across translations. occurrence_count handles a word appearing
        -- more than once in the same verse. 'strong' holds the grouping
        -- key — a real Strong's number for most rows, or a LEX:-prefixed
        -- synthetic lemma key for words that don't have one (see
        -- resolve_word_key). 'lemma' carries the lemma text for display
        -- alongside it, when known, regardless of which kind of key.
        CREATE TABLE lexicon_word_occurrences (
            strong           TEXT NOT NULL,
            lemma            TEXT,
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

        -- Per-translation gloss frequency for a given grouping key — how
        -- many times each distinct rendering was used across the whole
        -- Bible in that translation. Same 'strong'/'lemma' convention as
        -- lexicon_word_occurrences above.
        CREATE TABLE lexicon_gloss_stats (
            strong           TEXT NOT NULL,
            lemma            TEXT,
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


# Prefix applied to the synthetic key built from a word's lemma when it
# has no Strong's number of its own. A normalized Strong's number (see
# normalize_strong above) always starts with 'G' or 'H' followed by
# digits, so this can never collide with one — Strong's-keyed and
# lemma-keyed rows always land in disjoint key space within the same
# 'strong' column.
LEMMA_KEY_PREFIX = 'LEX:'


def resolve_word_key(raw_strong, lemma, lang):
    """Returns the identifier a word should be grouped/occurrence-indexed
    under: its normalized Strong's number when it has one, or — when it
    doesn't — a synthetic key built from its lemma, so words that were
    never assigned a Strong's number (an LXX-only lemma awaiting one, a
    supplied word, anything an importer left untagged) still get grouped
    with every other occurrence sharing that lemma, instead of being
    invisible to the word-study index entirely. Returns None only when
    the word has neither a Strong's number nor a lemma — nothing to key
    it by, so (as before) it's skipped."""
    strong = normalize_strong(raw_strong, lang)
    if strong:
        return strong
    if lemma:
        lemma = lemma.strip()
        if lemma:
            return LEMMA_KEY_PREFIX + lemma
    return None


def rebuild_word_index(con):
    """Scans every word token across every translation, aggregating
    occurrence locations and per-translation gloss frequencies keyed by
    resolve_word_key() — a word's normalized Strong's number, or a
    synthetic lemma-based key when it has no Strong's number."""
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
            w.lemma,
            w.lang,
            COALESCE(e.gloss, w.gloss, w.original, '') AS display_gloss
        FROM verses v
        JOIN books b ON b.id = v.book_id
        JOIN words w ON w.verse_id = v.id
        LEFT JOIN edits e ON e.word_id = w.id
        WHERE (w.strong IS NOT NULL AND trim(w.strong) != '')
           OR (w.lemma IS NOT NULL AND trim(w.lemma) != '')
        ORDER BY v.translation_id, b.book_order, v.chapter, v.verse, w.position;
    """

    cursor = con.execute(query)

    # (key, translation_id, book_id, chapter, verse) -> count of tokens
    # with that key at that location, in that translation.
    per_translation_counts = {}
    # (key, book_id, chapter, verse) -> (book_name, book_order, testament),
    # kept alongside per_translation_counts so we can look up display fields
    # once we've collapsed across translations.
    location_meta = {}
    # key -> { literal_lemma: count }, so once scanning is done we can pick
    # the most common literal lemma spelling to store per key, the same way
    # gloss_display_counts does for glosses below. Kept independent of
    # translation, since a key's lemma doesn't vary by translation.
    lemma_display_counts = {}
    # (key, translation_id, dedup_key) -> total occurrence count, keyed
    # on the case-folded/bracket-stripped form so e.g. "The [one]", "the",
    # and "THE" all accumulate under one bucket.
    gloss_counts = {}
    # (key, translation_id, dedup_key) -> { literal_gloss: count }, kept
    # alongside gloss_counts so that once counting is done we can pick the
    # most common literal casing/spacing to actually display for that key.
    gloss_display_counts = {}

    rows_seen = 0
    start_time = time.time()

    for row in cursor:
        (trans_id, book_id, book_name, book_order, testament,
         chapter, verse, raw_strong, lemma, lang, gloss) = row

        key = resolve_word_key(raw_strong, lemma, lang)
        if not key:
            continue

        rows_seen += 1

        loc_key = (key, book_id, chapter, verse)
        pt_key = (key, trans_id, book_id, chapter, verse)
        per_translation_counts[pt_key] = per_translation_counts.get(pt_key, 0) + 1
        if loc_key not in location_meta:
            location_meta[loc_key] = (book_name, book_order, testament)

        if lemma:
            lemma_clean = lemma.strip()
            if lemma_clean:
                counts = lemma_display_counts.setdefault(key, {})
                counts[lemma_clean] = counts.get(lemma_clean, 0) + 1

        gloss = strip_bracketed_words(gloss or '')
        if gloss:
            dedup_key = gloss_dedup_key(gloss)
            g_key = (key, trans_id, dedup_key)
            gloss_counts[g_key] = gloss_counts.get(g_key, 0) + 1
            display_counts = gloss_display_counts.setdefault(g_key, {})
            display_counts[gloss] = display_counts.get(gloss, 0) + 1

    print(f"Scanned {rows_seen} word tokens with a Strong's number or a lemma.")

    # Collapse per-translation counts down to one row per location: take the
    # max across translations rather than summing, so a word that occurs
    # twice in a verse still reads as 2 occurrences — not 2 x number of
    # translations that happen to carry that verse.
    print("Collapsing per-translation counts into location occurrences...")
    max_count_by_location = {}
    for (key, trans_id, book_id, chapter, verse), count in per_translation_counts.items():
        loc_key = (key, book_id, chapter, verse)
        if count > max_count_by_location.get(loc_key, 0):
            max_count_by_location[loc_key] = count

    # For each key, the literal lemma spelling that occurred most often —
    # ties broken alphabetically for a stable, deterministic result across
    # reruns. None for a Strong's-keyed word that never had a words.lemma
    # value recorded anywhere (most importers besides Swete/Nestle don't
    # populate it yet); always present for a lemma-keyed one, since that's
    # what the key was built from.
    lemma_by_key = {}
    for key, display_counts in lemma_display_counts.items():
        lemma_by_key[key] = sorted(display_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

    occurrence_rows = []
    for loc_key, count in max_count_by_location.items():
        key, book_id, chapter, verse = loc_key
        book_name, book_order, testament = location_meta[loc_key]
        occurrence_rows.append((key, lemma_by_key.get(key), book_id, book_name, book_order, testament, chapter, verse, count))

    # For each (key, translation, dedup_key) bucket, display the literal
    # casing/spacing that occurred most often — ties broken alphabetically
    # for a stable, deterministic result across reruns.
    gloss_rows = []
    for (key, trans_id, dedup_key), count in gloss_counts.items():
        display_counts = gloss_display_counts[(key, trans_id, dedup_key)]
        display_gloss = sorted(display_counts.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        gloss_rows.append((key, lemma_by_key.get(key), trans_id, display_gloss, count))

    print(f"Inserting {len(occurrence_rows)} rows into 'lexicon_word_occurrences'...")
    con.executemany("""
        INSERT INTO lexicon_word_occurrences
            (strong, lemma, book_id, book_name, book_order, testament, chapter, verse, occurrence_count)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
    """, occurrence_rows)

    print(f"Inserting {len(gloss_rows)} rows into 'lexicon_gloss_stats'...")
    con.executemany("""
        INSERT INTO lexicon_gloss_stats (strong, lemma, translation_id, gloss, occurrence_count)
        VALUES (?, ?, ?, ?, ?);
    """, gloss_rows)

    elapsed = time.time() - start_time
    all_keys = set(r[0] for r in occurrence_rows)
    lemma_keyed = sum(1 for k in all_keys if k.startswith(LEMMA_KEY_PREFIX))
    print(f"Word index rebuild completed successfully in {elapsed:.2f} seconds "
          f"({len(all_keys)} distinct keys — {len(all_keys) - lemma_keyed} Strong's numbers, "
          f"{lemma_keyed} lemma-only — {len(occurrence_rows)} locations).")


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
