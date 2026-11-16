#!/usr/bin/env python3
"""
build_versification_index.py — v2

Loads the multi-tradition verse-numbering link table (produced by
extract_versification.py from STEPBible's TVTMS, CC BY 4.0 —
https://github.com/STEPBible/STEPBible-Data) into bible.sqlite, and
makes sure the translations table has a `versification` column.

WHAT CHANGED FROM v1
--------------------
1. The table is now `versification_links`: an edge list
   (book, from_tradition, from_chapter, from_verse, to_tradition, to_ref)
   rather than a set of diffs-against-standard. TVTMS still only produces
   pivot-anchored edges (from_tradition = 'MT'), but the shape now also
   accepts hand-curated DIRECT edges — LXX <-> VUL, say — for passages
   where no pivot address exists at all because the passage isn't in the
   English canon. Load those with --manual; see MANUAL EDGES below.

2. Deuterocanon is loaded. BOOK_CODE_MAP grew DC entries, and anything
   it doesn't know is now handed to book_codes.py (the same resolver
   import_nestle_csv.py and build_xrefs.py already share) before being
   given up on, so a DC book River imported under a spelling of its own
   still resolves.

3. Rows are kept per source ('tvtms' or 'manual'), and a manual edge
   wins over a generated one for the same key. Re-running the TVTMS load
   no longer clobbers hand-curated work.

THE PIVOT IS A REFERENCE SPACE, NOT A TRANSLATION
-------------------------------------------------
Conversion goes X -> pivot -> Y. The pivot ('MT', spelled that way for
continuity — read it as "standard") is TVTMS's English StandardRef,
which assigns addresses to the deuterocanon as well. So "Bar 3:9" has a
pivot address even though no MT-tradition translation in River has text
there. That matters: an LXX-vs-Vulgate comparison of Baruch routes
through a pivot address that is never displayed to anyone, and works
fine.

Where the pivot genuinely has nothing to say — Vulgate Tobit is a
different recension rather than a renumbering of the Greek; the Greek
and Latin Esther additions aren't a shift of each other — a two-hop
conversion can't succeed, and the app is expected to say "no equivalent
verse in this text" rather than guess. That's what --manual is the
escape hatch for.

MANUAL EDGES
------------
A manual file uses the identical 6-column format, with from_tradition
set to whatever you like:

    book    from_tradition  from_chapter  from_verse  to_tradition  to_ref
    sirach  LXX             33            13          VUL           36:16
    tobit   LXX             5             3           VUL           --

Book names in a manual file are River's own book keys (lower(books.name))
rather than TVTMS codes, since you're writing them by hand. Use '--' for
"this verse has no equivalent at all in the target tradition" — the app
renders that as "not available" instead of falling through to an
identity guess.

TRANSLATION TAGS: "OT" OR "OT/NT"
---------------------------------
translations.versification holds the numbering tradition of a
translation's own Old Testament: MT, LXX or VUL. It can also record what
the NT cross-references *in that Bible* expect, as one combined value:

    lxx/mt     OT is Septuagint-numbered, but NT xrefs into the OT are
               written in Masoretic/English numbering
    lxx        no slash: the NT expects the same numbering the OT has

It is entered at the same single prompt as before, and stored normalized
upper-case ('LXX/MT'; a redundant 'LXX/LXX' is stored as plain 'LXX').
The reader (index.html) uses the second half to renumber OT references
made from NT books before linking to them. Translations tagged
automatically from a known code get no NT half; run with --retag to be
asked again for every translation (current value offered as the default).

Run this once per bible.sqlite build, after build_db.py has created the
`translations` and `books` tables.

Usage:
    python3 build_versification_index.py bible.sqlite versification_links.tsv
    python3 build_versification_index.py bible.sqlite versification_links.tsv --manual manual_links.tsv manual_vulg.tsv
    python3 build_versification_index.py bible.sqlite --manual manual_links.tsv   # manual only, leave TVTMS rows alone

Legacy inputs are still accepted: the v1 5-column
(book/tradition/std_chapter/std_verse/ref) and the original 4-column
mt_lxx_diffs.tsv are both detected by their header and loaded as
pivot-anchored edges.

This is intentionally a separate, optional pass — a bible.sqlite built
without ever running it just won't offer the compare view's "Align verse
numbers" option or versification-aware cross references
(db-queries.js's versificationReady flag turns both off gracefully),
rather than anything breaking.
"""
import argparse
import re
import sqlite3
import sys

try:
    import book_codes
except ImportError:      # resolver is optional; BOOK_CODE_MAP alone still works
    book_codes = None

PIVOT = "MT"
VALID_TRADITIONS = {"MT", "LXX", "VUL"}

BOOK_CODE_MAP = {
    # Protestant OT
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
    # Deuterocanon / apocrypha
    "Tob": "tobit", "Jdt": "judith", "Wis": "wisdom of solomon", "Sir": "sirach",
    "Bar": "baruch", "LJe": "letter of jeremiah",
    "1Ma": "1 maccabees", "2Ma": "2 maccabees", "3Ma": "3 maccabees", "4Ma": "4 maccabees",
    "1Es": "1 esdras", "2Es": "2 esdras",
    "Man": "prayer of manasseh", "Ps2": "psalm 151",
    "Sus": "susanna", "Bel": "bel and the dragon", "Aza": "prayer of azariah",
    "EsG": "greek esther", "Esg": "greek esther",
    "DaG": "greek daniel", "Dag": "greek daniel",
    "Ode": "odes", "PsS": "psalms of solomon",
}

KNOWN_LXX_CODES = {"LXE", "NETS", "LES", "ABP", "LXX", "SWETE", "BRENTON"}
KNOWN_VUL_CODES = {"VUL", "VULG", "CLEM", "CLVUL", "CLEMENT", "DRA", "DRB", "DRC", "DR", "DOUAY", "DOUAYRHEIMS"}


# ---------------------------------------------------------------- schema

def ensure_schema(conn):
    cols = [r[1] for r in conn.execute("PRAGMA table_info(translations)")]
    if "versification" not in cols:
        print("Adding translations.versification column...")
        conn.execute("ALTER TABLE translations ADD COLUMN versification TEXT")

    _create_links_table(conn)
    _migrate_legacy_diffs(conn)
    _create_compat_view(conn)


def _create_links_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS versification_links (
            book           TEXT NOT NULL,     -- lower(books.name)
            from_tradition TEXT NOT NULL,     -- 'MT' (the pivot/standard reference space), 'LXX' or 'VUL'
            from_chapter   INTEGER NOT NULL,
            from_verse     INTEGER NOT NULL,
            to_tradition   TEXT NOT NULL,
            to_ref         TEXT NOT NULL,     -- verbatim ref, e.g. '31:55', '5:32-6:1', '3:1a', '--' (no equivalent)
            source         TEXT NOT NULL DEFAULT 'tvtms',  -- 'tvtms' (generated) | 'manual' (hand-curated; wins)
            PRIMARY KEY (book, from_tradition, from_chapter, from_verse, to_tradition, source)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_versification_pair
            ON versification_links(book, from_tradition, to_tradition)
    """)


def _migrate_legacy_diffs(conn):
    existing = [r[1] for r in conn.execute("PRAGMA table_info(versification_diffs)")]
    if not existing:
        return
    is_table = conn.execute(
        "SELECT type FROM sqlite_master WHERE name = 'versification_diffs'"
    ).fetchone()
    if not is_table or is_table[0] != 'table':
        return

    if "lxx_ref" in existing:
        print("Migrating v0 versification_diffs (lxx_ref) into versification_links...")
        conn.execute(f"""
            INSERT OR REPLACE INTO versification_links
                (book, from_tradition, from_chapter, from_verse, to_tradition, to_ref, source)
            SELECT book, '{PIVOT}', std_chapter, std_verse, 'LXX', lxx_ref, 'tvtms'
            FROM versification_diffs
        """)
    elif "tradition" in existing:
        print("Migrating v1 versification_diffs (tradition/ref) into versification_links...")
        conn.execute(f"""
            INSERT OR REPLACE INTO versification_links
                (book, from_tradition, from_chapter, from_verse, to_tradition, to_ref, source)
            SELECT book, '{PIVOT}', std_chapter, std_verse, tradition, ref, 'tvtms'
            FROM versification_diffs
        """)
    else:
        return
    moved = conn.execute("SELECT count(*) FROM versification_links").fetchone()[0]
    conn.execute("DROP TABLE versification_diffs")
    print(f"  migrated {moved} row(s).")


def _create_compat_view(conn):
    conn.execute("DROP VIEW IF EXISTS versification_diffs")
    conn.execute(f"""
        CREATE VIEW versification_diffs AS
        SELECT book,
               to_tradition AS tradition,
               from_chapter AS std_chapter,
               from_verse   AS std_verse,
               to_ref       AS ref
        FROM versification_links
        WHERE from_tradition = '{PIVOT}'
    """)


# ------------------------------------------------------------------ load

def resolve_book(code, known_books):
    book = BOOK_CODE_MAP.get(code)
    if book is None and book_codes is not None:
        resolved = book_codes.resolve_canonical_name(code.replace('_', ' ').replace('-', ' '))
        if resolved:
            book = resolved.lower()
    if book is None:
        return None, "unmapped"
    if known_books and book not in known_books:
        return None, "absent"
    return book, None


def load_links(conn, tsv_path, source_tag, known_books, manual=False, clear_existing=True):
    rows = []
    unmapped = set()
    absent = set()
    malformed = 0
    fmt = "edge"

    with open(tsv_path, encoding="utf-8") as f:
        if manual:
            lines = f
        else:
            header = [h.strip() for h in next(f).rstrip("\n").split("\t")]
            if "from_tradition" in header:
                fmt = "edge"
            elif "tradition" in header:
                fmt = "v1"
            else:
                fmt = "v0"
                print("Note: input has no tradition column — loading as LXX-only (legacy "
                      "mt_lxx_diffs.tsv). Re-run extract_versification.py for Vulgate and "
                      "deuterocanon coverage.")
            lines = f

        for line in lines:
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = [p.strip() for p in line.rstrip("\n").split("\t")]
            try:
                if fmt == "edge":
                    code, from_trad, from_ch, from_v, to_trad, to_ref = parts[:6]
                elif fmt == "v1":
                    code, to_trad, from_ch, from_v, to_ref = parts[:5]
                    from_trad = PIVOT
                else:
                    code, from_ch, from_v, to_ref = parts[:4]
                    from_trad, to_trad = PIVOT, "LXX"
                from_ch, from_v = int(from_ch), int(from_v)
            except ValueError:
                malformed += 1
                continue

            from_trad, to_trad = from_trad.upper(), to_trad.upper()
            if from_trad not in VALID_TRADITIONS or to_trad not in VALID_TRADITIONS:
                continue
            if from_trad == to_trad:
                continue

            if manual:
                book = code.strip().lower()
                if known_books and book not in known_books:
                    absent.add(code)
                    continue
            else:
                book, why = resolve_book(code, known_books)
                if book is None:
                    (unmapped if why == "unmapped" else absent).add(code)
                    continue

            rows.append((book, from_trad, from_ch, from_v, to_trad, to_ref, source_tag))

    if clear_existing:
        conn.execute("DELETE FROM versification_links WHERE source = ?", (source_tag,))

    conn.executemany(
        "INSERT OR REPLACE INTO versification_links "
        "(book, from_tradition, from_chapter, from_verse, to_tradition, to_ref, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )

    per_pair = {}
    for _b, ft, _c, _v, tt, _r, _s in rows:
        key = f"{ft}->{tt}"
        per_pair[key] = per_pair.get(key, 0) + 1
    print(f"Loaded {len(rows)} link(s) from {tsv_path} as source='{source_tag}'.")
    for pair, n in sorted(per_pair.items()):
        print(f"  {pair}: {n}")
    if unmapped:
        print(f"  WARNING: book codes with no mapping (add to BOOK_CODE_MAP): {sorted(unmapped)}")
    if absent:
        print(f"  Note: books not present in this bible.sqlite, skipped: {sorted(absent)}")
    if malformed:
        print(f"  Note: {malformed} line(s) didn't parse as {fmt} format and were skipped.")
    return len(rows)


def report_coverage(conn):
    rows = conn.execute("""
        SELECT from_tradition || '->' || to_tradition AS pair, count(DISTINCT book), count(*)
        FROM versification_links GROUP BY pair ORDER BY pair
    """).fetchall()
    print("\nCoverage:")
    for pair, books, links in rows:
        print(f"  {pair}: {links} link(s) across {books} book(s)")
    dc = conn.execute("""
        SELECT DISTINCT book FROM versification_links
        WHERE book NOT IN (SELECT lower(name) FROM books WHERE testament IS NOT NULL)
        ORDER BY book
    """).fetchall() if _has_column(conn, "books", "testament") else []
    if dc:
        print(f"  (books outside the tagged testaments: {[r[0] for r in dc]})")


def _has_column(conn, table, col):
    return col in [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


# --------------------------------------------------------------- tagging

def parse_versification_tag(text):
    parts = [p for p in re.split(r"\s*[/,]\s*|\s+", (text or "").strip().upper()) if p]
    if not parts:
        return "MT"
    if len(parts) > 2 or any(p not in VALID_TRADITIONS for p in parts):
        return None
    ot = parts[0]
    nt = parts[1] if len(parts) == 2 else ot
    return ot if nt == ot else f"{ot}/{nt}"


def tag_translations(conn, retag=False):
    rows = conn.execute("SELECT id, code, name, versification FROM translations").fetchall()
    auto_tagged = False
    for tid, code, name, current in rows:
        if current and not retag:
            continue
        up = (code or "").upper()
        guess = "LXX" if up in KNOWN_LXX_CODES else ("VUL" if up in KNOWN_VUL_CODES else None)
        if guess and not retag:
            conn.execute("UPDATE translations SET versification = ? WHERE id = ?", (guess, tid))
            print(f"  {code} ({name}): tagged '{guess}' (known code)")
            auto_tagged = True
            continue
        default = current or guess or "MT"
        while True:
            answer = input(f"  {code} ({name}): versification? "
                           f"[MT/lxx/vul; 'lxx/mt' = LXX OT, NT xrefs expect MT; Enter = {default}] ")
            value = default if not answer.strip() else parse_versification_tag(answer)
            if value is not None:
                break
            print("    Not recognized — use MT, LXX or VUL, optionally followed by /MT, /LXX or /VUL.")
        conn.execute("UPDATE translations SET versification = ? WHERE id = ?", (value, tid))
    if auto_tagged:
        print("  (Tagged automatically without an NT half. If any of these has NT cross-references "
              "numbered differently from its OT, re-run with --retag and answer e.g. 'lxx/mt'.)")


def main():
    ap = argparse.ArgumentParser(description="Load verse-numbering links into bible.sqlite")
    ap.add_argument("db", help="SQLite DB path (bible.sqlite)")
    ap.add_argument("links", nargs="?", help="versification_links.tsv from extract_versification.py")
    ap.add_argument("--manual", nargs="+", metavar="FILE",
                    help="Hand-curated link TSVs (same 6 columns, River book keys). "
                         "Kept separately from generated rows and wins over them.")
    ap.add_argument("--no-tag", action="store_true", help="Skip the interactive translation tagging step")
    ap.add_argument("--retag", action="store_true",
                    help="Ask about every translation again, offering its current tag as the default "
                         "(use this to add the NT half, e.g. 'lxx/mt')")
    args = ap.parse_args()

    if not args.links and not args.manual:
        sys.exit("Nothing to load: give a links TSV, --manual, or both.")

    conn = sqlite3.connect(args.db)
    try:
        ensure_schema(conn)
        known_books = {r[0] for r in conn.execute("SELECT DISTINCT lower(name) FROM books")}
        
        if args.links:
            load_links(conn, args.links, "tvtms", known_books, manual=False)
            
        if args.manual:
            for idx, manual_path in enumerate(args.manual):
                # Clear existing manual rows only on the first file, so subsequent manual files append
                clear_existing = (idx == 0)
                load_links(conn, manual_path, "manual", known_books, manual=True, clear_existing=clear_existing)
                
        report_coverage(conn)
        if not args.no_tag:
            print("\nTagging translations by versification:")
            tag_translations(conn, retag=args.retag)
        conn.commit()
    finally:
        conn.close()
    print("\nDone.")


if __name__ == "__main__":
    main()