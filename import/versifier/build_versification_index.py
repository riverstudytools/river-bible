#!/usr/bin/env python3
"""
build_versification_index.py

Loads the MT/LXX verse-numbering diff table (produced by
extract_mt_lxx.py from STEPBible's TVTMS, CC BY 4.0 —
https://github.com/STEPBible/STEPBible-Data) into bible.sqlite, and
makes sure the translations table has a `versification` column.

Run this once per bible.sqlite build, after build_db.py has created the
`translations` and `books` tables (it needs both to exist already: it
reads `books.name` to validate/normalize book keys, and offers to tag
`translations.versification` for you interactively).

Usage:
    python3 build_versification_index.py bible.sqlite mt_lxx_diffs.tsv

This is intentionally a separate, optional pass — like
build_word_index.py, a bible.sqlite built without ever running this
script just won't offer the compare view's "Align verse numbers" option
(db-queries.js's versificationReady flag turns it off gracefully),
rather than anything breaking.
"""
import sqlite3
import sys

# TVTMS's 3-letter (occasionally digit-prefixed) book codes -> River's
# own lowercase book keys (books.name, lowercased — see BOOKS in
# index.html). Only OT books: TVTMS's Hebrew/Greek Ancient Versions
# annotations, and therefore mt_lxx_diffs.tsv, never cover the NT.
BOOK_CODE_MAP = {
    "Gen": "genesis", "Exo": "exodus", "Lev": "leviticus", "Num": "numbers",
    "Deu": "deuteronomy", "Jos": "joshua", "Jdg": "judges", "Rut": "ruth",
    "1Sa": "1 samuel", "2Sa": "2 samuel", "1Ki": "1 kings", "2Ki": "2 kings",
    "1Ch": "1 chronicles", "2Ch": "2 chronicles", "Ezr": "ezra", "Neh": "nehemiah",
    "Est": "esther", "Job": "job", "Psa": "psalms", "Pro": "proverbs",
    "Ecc": "ecclesiastes", "Sng": "song of solomon", "Isa": "isaiah", "Jer": "jeremiah",
    "Lam": "lamentations", "Ezk": "ezekiel", "Dan": "daniel", "Hos": "hosea",
    "Jol": "joel", "Amo": "amos", "Oba": "obadiah", "Jon": "jonah",
    "Mic": "micah", "Nam": "nahum", "Hab": "habakkuk", "Zep": "zephaniah",
    "Hag": "haggai", "Zec": "zechariah", "Mal": "malachi",
}

# Known LXX-tradition English translation codes, for the interactive
# tagging step below. Extend this as River adds more Greek-numbered
# texts (a raw Rahlfs/Göttingen Greek text would also be 'LXX', same as
# an English translation of it — versification is about the numbering
# scheme, not the language).
KNOWN_LXX_CODES = {"LXE", "NETS", "LES", "ABP", "LXX"}


def ensure_schema(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(translations)")]
    if "versification" not in cols:
        print("Adding translations.versification column...")
        conn.execute("ALTER TABLE translations ADD COLUMN versification TEXT")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS versification_diffs (
            book        TEXT NOT NULL,
            std_chapter INTEGER NOT NULL,
            std_verse   INTEGER NOT NULL,
            lxx_ref     TEXT NOT NULL,
            PRIMARY KEY (book, std_chapter, std_verse)
        )
    """)


def load_diffs(conn, tsv_path):
    known_books = {r[0] for r in conn.execute("SELECT DISTINCT lower(name) FROM books")}
    rows = []
    skipped_books = set()
    with open(tsv_path, encoding="utf-8") as f:
        next(f)  # header
        for line in f:
            book_code, std_chapter, std_verse, lxx_ref = line.rstrip("\n").split("\t")
            book = BOOK_CODE_MAP.get(book_code)
            if book is None:
                skipped_books.add(book_code)
                continue
            if known_books and book not in known_books:
                # Book isn't in *this* bible.sqlite at all (e.g. a build
                # with only the NT loaded) - nothing to align against,
                # skip rather than insert an orphaned row.
                continue
            rows.append((book, int(std_chapter), int(std_verse), lxx_ref))

    conn.execute("DELETE FROM versification_diffs")
    conn.executemany(
        "INSERT INTO versification_diffs (book, std_chapter, std_verse, lxx_ref) VALUES (?, ?, ?, ?)",
        rows,
    )
    print(f"Inserted {len(rows)} versification diff rows.")
    if skipped_books:
        print(f"Note: unmapped TVTMS book codes in the TSV (add to BOOK_CODE_MAP): {sorted(skipped_books)}")


def tag_translations(conn):
    """Best-effort auto-tagging by known code, falling back to asking.
    Never overwrites a value someone already set by hand."""
    rows = conn.execute("SELECT id, code, name, versification FROM translations").fetchall()
    for tid, code, name, current in rows:
        if current:
            continue
        guess = "LXX" if code.upper() in KNOWN_LXX_CODES else None
        if guess:
            conn.execute("UPDATE translations SET versification = ? WHERE id = ?", (guess, tid))
            print(f"  {code} ({name}): tagged '{guess}' (known code)")
            continue
        # Unrecognized code - ask rather than guess wrong. Answering
        # blank leaves it NULL, which db-queries.js/index.html already
        # treat as 'MT' (the overwhelmingly common case).
        answer = input(f"  {code} ({name}): versification? [MT/lxx/blank=MT] ").strip().lower()
        value = "LXX" if answer == "lxx" else "MT"
        conn.execute("UPDATE translations SET versification = ? WHERE id = ?", (value, tid))


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} bible.sqlite mt_lxx_diffs.tsv")
        sys.exit(1)
    db_path, tsv_path = sys.argv[1], sys.argv[2]

    conn = sqlite3.connect(db_path)
    try:
        ensure_schema(conn)
        load_diffs(conn, tsv_path)
        print("\nTagging translations by versification (Enter = MT):")
        tag_translations(conn)
        conn.commit()
    finally:
        conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()
