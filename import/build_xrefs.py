#!/usr/bin/env python3
"""
build_xrefs.py — Cross-reference table builder/importer for bible.sqlite.

Loads a book/chapter/verse cross-reference dataset (e.g. openbible.info's
Cross References dataset, or a set harvested from a Douay-Rheims
edition) into a single, translation-independent `cross_references`
table. This follows the same optional-pipeline-table pattern as
build_versification_index.py rather than import_nestle_csv.py's
per-translation pattern: cross-references aren't per-translation the way
words are, they're keyed by book/chapter/verse only, so one script owns
both the table's schema and the load of its external source file, and is
safe to rerun any time a newer/expanded dataset is available.

VERSIFICATION (read this before importing a Douay set)
------------------------------------------------------
A cross-reference dataset is numbered in whatever tradition the edition
it was harvested from uses, and that is NOT interchangeable:

  * openbible.info's set is English/KJV-standard numbered  -> MT
  * anything scraped from a Douay-Rheims / Clementine text -> VUL
  * anything keyed to Brenton/NETS/Rahlfs/Swete            -> LXX

Import a Douay set without saying so and every Psalm reference in it
lands one psalm off for most of the Psalter, every Daniel 3 reference
after v.23 lands ~67 verses off, and the Esther additions land nowhere
at all — silently, because the book slugs still resolve fine. The
numbers are the part that's wrong, not the names.

So each row carries the versification it was written in, and the app
converts at read time (db-queries.js's getCrossReferences, via
versification_links — see build_versification_index.py) rather than the
importer rewriting the numbers here. Rationale for converting late
rather than normalizing on import:

  - it's lossless: refs include ranges ("5:32-6:1"), sub-verses ("3:1a")
    and outright "no equivalent" markers, none of which survive being
    flattened into a single integer verse number;
  - re-running build_versification_index.py with better data (or a
    hand-curated manual edge file) improves every already-imported xref
    for free, with no re-import;
  - it matches how compare mode already works, so there's one conversion
    path to reason about instead of two.

Note that 'MT' is River's tag for the English-standard *reference
space*, not for the Masoretic text as such — it covers deuterocanonical
addresses too. A Douay xref pointing at Ecclesiasticus is still a VUL
reference into a book the pivot can address.

Pass --versification when the dataset isn't English-standard:

    python3 build_xrefs.py douay_xrefs.tsv --source douay --versification VUL

Expected input format (tab- or comma-separated, header required):
    source_reference  target_reference  source  votes  from_book  from_chapter  from_verse  to_book  to_chapter  to_verse  to_verse_end

Only the structured columns are used: from_book/from_chapter/from_verse,
to_book/to_chapter/to_verse/to_verse_end, source, and votes. The two
human-readable *_reference columns are ignored — the structured columns
already carry everything they encode, without needing to reparse a string
like "1 Thessalonians 5:23-24".

book slugs (from_book/to_book, e.g. "1-corinthians", "song-of-solomon",
or a Douay-Rheims/Vulgate name like "josue", "3-kings", "apocalypse",
"ecclesiasticus", "paralipomenon") are resolved to River's own lowercase
book key — lower(books.name) — via the same book_codes.py resolver the
other importers already share, falling back to a live check against the
`books` table for anything book_codes.py doesn't recognize (covers
apocryphal/deuterocanonical books some other import script added under a
name of its own). See slug_to_book_key.

A blank from_verse/to_verse (seen in the wild for "loose", chapter-level
cross-references — e.g. "1 Corinthians 15:5" -> the whole of "Luke 24"
rather than one verse of it) is stored as 0, River's own sentinel for "no
specific verse, whole chapter" — not SQL NULL, so the UNIQUE constraint
below (which SQLite would otherwise treat every NULL as distinct under,
breaking idempotent reruns) still dedupes chapter-level rows correctly.
Genuinely malformed rows (non-numeric garbage, a missing chapter) are
skipped and reported rather than treated as chapter-level.

Self-referencing rows — same book/chapter, where the source verse falls
within the target's verse or verse range (e.g. "Romans 3:23" -> "Romans
3:23-25", or a whole-chapter reference on either side) — are detected
and skipped rather than imported. See is_self_reference.

Usage:
    python3 build_xrefs.py path/to/cross_references.txt --source openbible
    python3 build_xrefs.py path/to/cross_references.txt --source openbible --replace
    python3 build_xrefs.py path/to/douay_xrefs.tsv --source douay --versification VUL
"""

import argparse
import csv
import sqlite3
import sys

import book_codes
import build_db as db_builder

VALID_VERSIFICATIONS = ("MT", "LXX", "VUL")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cross_references (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_book TEXT NOT NULL,        -- lower(books.name), e.g. 'romans', 'song of solomon'
    from_chapter INTEGER NOT NULL,
    from_verse INTEGER NOT NULL,    -- 0 = whole-chapter reference, no specific verse (see module docstring)
    to_book TEXT NOT NULL,
    to_chapter INTEGER NOT NULL,
    to_verse INTEGER NOT NULL,      -- 0 = whole-chapter reference, no specific verse
    to_verse_end INTEGER,           -- NULL when the target is a single verse, not a range (and always NULL when to_verse is 0)
    source TEXT NOT NULL DEFAULT 'openbible',
    versification TEXT NOT NULL DEFAULT 'MT',  -- numbering tradition these chapter/verse numbers are written in: 'MT' | 'LXX' | 'VUL'
    votes INTEGER NOT NULL DEFAULT 0,
    UNIQUE(from_book, from_chapter, from_verse, to_book, to_chapter, to_verse, source)
);
-- Powers the sidebar's primary lookup: every outbound xref for the verse
-- currently being read, best-voted first.
CREATE INDEX IF NOT EXISTS idx_xrefs_from ON cross_references(from_book, from_chapter, from_verse, votes DESC);
-- Powers an optional "referenced by" reverse lookup (not required for the
-- outbound sidebar case, but cheap to keep available for later).
CREATE INDEX IF NOT EXISTS idx_xrefs_to ON cross_references(to_book, to_chapter, to_verse);
-- getCrossReferences queries one versification at a time (it converts the
-- open range into each dataset's own numbering separately), so give that
-- predicate an index of its own.
CREATE INDEX IF NOT EXISTS idx_xrefs_versification ON cross_references(versification);
"""

# NOTE on the UNIQUE constraint: `versification` is deliberately NOT part
# of it. It's a property of the *source*, which is already in the key —
# one dataset is written in one numbering tradition throughout. Leaving
# it out means re-importing the same dataset with a corrected
# --versification flag fixes the existing rows in place (see the upsert's
# DO UPDATE SET below) instead of doubling them.


def ensure_xrefs_schema(con):
    """Creates cross_references and its indexes if they don't already
    exist, and adds the versification column to a table created by an
    earlier version of this script."""
    cols = [r[1] for r in con.execute("PRAGMA table_info(cross_references)")]
    if cols and "versification" not in cols:
        # Pre-existing rows were all imported before any of this mattered,
        # i.e. from an English-standard dataset. 'MT' is the correct
        # backfill; re-import with --versification if that's ever untrue.
        print("Adding cross_references.versification column (existing rows default to 'MT')...")
        con.execute("ALTER TABLE cross_references ADD COLUMN versification TEXT NOT NULL DEFAULT 'MT'")
    con.executescript(SCHEMA)


def slug_to_book_key(slug, known_book_keys):
    """
    Maps a book slug to River's own lowercase book key. Tries
    book_codes.py's centralized resolver first — it already knows the
    standard canon plus a wide range of code/abbreviation/variant
    spellings, including the Douay-Rheims/Vulgate names ("josue",
    "3-kings", "apocalypse", "ecclesiasticus", ...) real cross-reference
    dumps mix in — and is trusted even for a book not currently in *this*
    database's `books` table, since cross_references has no foreign key
    to books: an xref for a book that gets imported later just starts
    resolving on its own, with no need to re-run this script.

    Only falls back to a live check against known_book_keys (this
    database's actual `lower(books.name)` set) for slugs book_codes.py
    doesn't recognize at all — covers apocryphal/deuterocanonical books
    some other import script added under a name of its own. Returns None
    if neither resolves — the caller logs and skips those rows.

    NOTE: this normalizes the book *name* only. It does not and cannot
    normalize the chapter/verse numbers that come with it — "3-kings 20"
    becomes "1 kings 20", but whether that 20 means the same chapter an
    English Bible calls 20 depends on the dataset's versification, which
    is recorded per row instead. See the module docstring.
    """
    candidate = slug.strip().lower().replace('_', '-').replace('-', ' ')
    resolved = book_codes.resolve_canonical_name(candidate)
    if resolved:
        return resolved.lower()
    return candidate if candidate in known_book_keys else None


def parse_xrefs_file(file_path, default_source):
    """Parses a cross-reference CSV/TSV file into structured row dicts."""
    with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = '\t' if '\t' in sample else ','
        reader = csv.DictReader(f, delimiter=delimiter)

        required = {'from_book', 'from_chapter', 'from_verse', 'to_book', 'to_chapter', 'to_verse'}
        missing = required - set(reader.fieldnames or [])
        if missing:
            sys.exit(f"Error: input file is missing required column(s): {', '.join(sorted(missing))}")

        rows = []
        skipped = 0
        skipped_examples = []
        # line_num starts at 2: DictReader's first data row is line 2 (the
        # header consumed line 1), matching what a text editor would show.
        for line_num, row in enumerate(reader, start=2):
            try:
                from_verse_str = (row.get('from_verse') or '').strip()
                to_verse_str = (row.get('to_verse') or '').strip()
                to_verse_end = (row.get('to_verse_end') or '').strip()
                votes = (row.get('votes') or '').strip()
                rows.append({
                    'from_book_slug': row['from_book'].strip(),
                    'from_chapter': int(row['from_chapter']),
                    # Blank verse = a "loose", chapter-level reference (see
                    # module docstring) rather than bad data — stored as 0
                    # rather than skipped.
                    'from_verse': int(from_verse_str) if from_verse_str else 0,
                    'to_book_slug': row['to_book'].strip(),
                    'to_chapter': int(row['to_chapter']),
                    'to_verse': int(to_verse_str) if to_verse_str else 0,
                    'to_verse_end': int(to_verse_end) if to_verse_end else None,
                    'source': (row.get('source') or '').strip() or default_source,
                    'votes': int(votes) if votes else 0,
                })
            except (ValueError, KeyError) as e:
                # Genuinely malformed rows only, now that a blank verse is
                # handled above — a missing/non-numeric chapter, or
                # anything else unexpected — shouldn't sink the whole
                # import, so skip just that row and keep going, but
                # surface what got skipped rather than staying silent.
                skipped += 1
                if len(skipped_examples) < 10:
                    skipped_examples.append(f'line {line_num}: {e} — {row}')
                continue

        if skipped:
            print(f"Warning: skipped {skipped} row(s) with a missing/non-numeric chapter number (or other malformed field):", file=sys.stderr)
            for ex in skipped_examples:
                print(f"  {ex}", file=sys.stderr)
            if skipped > len(skipped_examples):
                print(f"  ... and {skipped - len(skipped_examples)} more", file=sys.stderr)

        return rows


def is_self_reference(from_book, from_chapter, from_verse, to_book, to_chapter, to_verse, to_verse_end):
    """
    True if a cross-reference points back at itself — same book/chapter,
    with the source verse contained within the target's verse (or verse
    range). Also true whenever either side is a whole-chapter reference
    (verse 0) and the book/chapter match, since a whole chapter
    necessarily contains any specific verse in it (and itself).

    Only book/chapter need to match here because from_verse is always a
    single verse (there's no from_verse_end in the schema); the
    containment check below only needs to test that one point against
    the target's [to_verse, to_verse_end] range.

    Safe to run pre-conversion: both sides of a row are in the same
    versification, so containment within a book/chapter is unaffected by
    which tradition the numbers are written in.
    """
    if from_book != to_book or from_chapter != to_chapter:
        return False
    if from_verse == 0 or to_verse == 0:
        return True
    lo = to_verse
    hi = to_verse_end if to_verse_end else to_verse
    return lo <= from_verse <= hi


def import_xrefs(con, rows, versification):
    """Upserts parsed rows into cross_references, skipping unresolvable book slugs and self-references."""
    known_book_keys = {r[0] for r in con.execute('SELECT lower(name) FROM books').fetchall()}

    stmt = """
        INSERT INTO cross_references
            (from_book, from_chapter, from_verse, to_book, to_chapter, to_verse, to_verse_end, source, versification, votes)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(from_book, from_chapter, from_verse, to_book, to_chapter, to_verse, source)
        DO UPDATE SET to_verse_end = excluded.to_verse_end,
                      versification = excluded.versification,
                      votes = excluded.votes
    """

    inserted = 0
    skipped = 0
    self_ref_skipped = 0
    unresolved_slugs = set()

    for r in rows:
        from_book = slug_to_book_key(r['from_book_slug'], known_book_keys)
        to_book = slug_to_book_key(r['to_book_slug'], known_book_keys)

        if not from_book:
            unresolved_slugs.add(r['from_book_slug'])
        if not to_book:
            unresolved_slugs.add(r['to_book_slug'])
        if not from_book or not to_book:
            skipped += 1
            continue

        if is_self_reference(from_book, r['from_chapter'], r['from_verse'],
                              to_book, r['to_chapter'], r['to_verse'], r['to_verse_end']):
            self_ref_skipped += 1
            continue

        con.execute(stmt, (
            from_book, r['from_chapter'], r['from_verse'],
            to_book, r['to_chapter'], r['to_verse'], r['to_verse_end'],
            r['source'], versification, r['votes']
        ))
        inserted += 1

    if unresolved_slugs:
        print(f"Warning: skipped {skipped} row(s) referencing unrecognized book slug(s): "
              f"{', '.join(sorted(unresolved_slugs))}", file=sys.stderr)
    if self_ref_skipped:
        print(f"Skipped {self_ref_skipped} self-referencing row(s) (source verse within its own target range).",
              file=sys.stderr)

    return inserted


def main():
    ap = argparse.ArgumentParser(description="Import a book/chapter/verse cross-reference dataset into bible.sqlite")
    ap.add_argument('input', help='Path to cross-reference CSV/TSV file')
    ap.add_argument('--source', default='openbible',
                     help='Source tag for rows whose own "source" column is blank, and the tag --replace clears (default: openbible)')
    ap.add_argument('--versification', default='MT', choices=VALID_VERSIFICATIONS,
                     help='Numbering tradition this dataset is written in: MT (English/KJV standard, e.g. openbible.info), '
                          'LXX (Greek/Septuagint), or VUL (Vulgate/Douay-Rheims). Applies to every row in this import. '
                          'Getting this wrong mis-links Psalms, Daniel 3 and Esther silently. (default: MT)')
    ap.add_argument('--db', default='bible.sqlite', help='SQLite DB path')
    ap.add_argument('--replace', action='store_true',
                     help='Delete existing rows tagged with --source before importing, instead of upserting in place')
    args = ap.parse_args()

    con = sqlite3.connect(args.db, timeout=20.0)
    con.execute('PRAGMA foreign_keys = ON')

    try:
        with con:
            db_builder.ensure_schema(con)  # books must already exist so slugs can be validated against them
            ensure_xrefs_schema(con)

            if args.replace:
                deleted = con.execute('DELETE FROM cross_references WHERE source = ?', (args.source,)).rowcount
                print(f'Cleared {deleted} existing row(s) tagged "{args.source}".')

            rows = parse_xrefs_file(args.input, args.source)
            if not rows:
                sys.exit("Error: no rows could be parsed from input file.")

            count = import_xrefs(con, rows, args.versification)

        print(f'Successfully imported {count} cross-reference(s) from "{args.input}" into {args.db} '
              f'as {args.versification}-numbered.')
        if args.versification != 'MT':
            print('Reminder: run build_versification_index.py against this database, or these '
                  'references will be displayed with their source numbering unconverted.')

    except Exception as e:
        sys.exit(f"Database import failed: {e}")
    finally:
        con.close()


if __name__ == '__main__':
    main()


# ----------------------------------------------------------------------
# Already wired into the app end-to-end: db-queries.js (xrefsReady,
# getCrossReferences), server.js (GET /api/xrefs/:translationId/:bookKey/
# :chapter/:verse), preload.js + main.js (matching IPC channel),
# db-client.js (electronAPI/fetch dispatch), and index.html (the reading
# pane's Xrefs sidebar tab).
#
# Known gap 1: chapter-level *source* rows (from_verse = 0) are stored and
# queryable, but nothing in the UI currently triggers a lookup for one,
# since the Xrefs tab only ever asks for whichever single verse is
# selected via the reading pane's existing verse-number click-to-select —
# there's no "click the chapter heading" equivalent yet. Chapter-level
# *target* rows (to_verse = 0) do render already, as a Chapter-only link
# with no specific verse text.
#
# Known gap 2: cross-source duplicate suppression. With both an openbible
# (MT) and a Douay (VUL) set loaded, the same conceptual link will appear
# twice under different numbers wherever the traditions disagree. The
# UNIQUE constraint can't catch that — it compares raw numbers, and the
# raw numbers legitimately differ. Deduping means converting both to the
# pivot first, which is a read-time job (db-queries.js) rather than an
# import-time one: once getCrossReferences converts everything into the
# pane's numbering anyway, dedupe on the converted (book, chapter, verse)
# tuple just before grouping, keeping the higher-voted row.
# ----------------------------------------------------------------------
