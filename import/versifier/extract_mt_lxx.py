#!/usr/bin/env python3
"""
extract_mt_lxx.py

Reduces STEPBible's TVTMS (Translators Versification Traditions with
Methodology for Standardisation, CC BY 4.0, https://github.com/STEPBible/STEPBible-Data)
down to just the piece River needs: a table of every place River's
"MT-tradition" English translations (KJV/ESV/NASB-style: numbered by
English/KJV-standard chapter:verse) and its "LXX-tradition" translations
(Brenton/NETS-style: numbered by Greek/Septuagint chapter:verse) call
the "same" verse by different numbers.

IMPORTANT: this is StandardRef (English/KJV numbering) vs. the Greek=
annotation - NOT Hebrew= vs Greek=. TVTMS's "Hebrew=" column records how
printed Hebrew Bibles themselves chapter/verse things, which sometimes
differs from English-standard numbering even for an all-Masoretic-text
passage (e.g. Gen.31:55 in English standard is Hebrew print's 32:1 -
same Hebrew text, just a different chapter break in English vs. Hebrew
printing conventions). River's MT-tradition translations are numbered
the English/KJV way, so StandardRef *is* "the MT side" for our purposes;
Hebrew= would only be relevant if River ever ingested a raw
Hebrew-numbered text.

TVTMS's "Expanded Version" has one row per verse per underlying source
tradition, with that StandardRef and an "Ancient Versions" column
annotating what that same verse is called in the Hebrew, Latin and
Greek traditions, e.g.:

    (Hebrew=32:1 • Latin=31:55 • Greek=31:55)

We don't need TVTMS's full reversification engine (SourceType/Tests/
Action) - only the Greek= half of this annotation, deduped per
StandardRef, and only where it disagrees with StandardRef itself.

Output: mt_lxx_diffs.tsv - book, std_chapter, std_verse, lxx_ref
  - std_chapter/std_verse are the English/KJV-style reference (what
    River's MT-tradition translations use)
  - lxx_ref is the verbatim Greek= string from TVTMS (may be a range
    like "5:32-6:1", a sub-verse like "3:1a", or a free-text annotation
    for badly-scrambled passages like Exo.28) relative to the same book
  - Only rows where lxx_ref != "{std_chapter}:{std_verse}" are kept
"""
import re
import sys
from collections import OrderedDict

SRC = "TVTMS.txt"
OUT = "mt_lxx_diffs.tsv"

# OT books only (by TVTMS's 3-letter abbreviations) - MT/LXX numbering
# differences don't apply to the NT.
OT_BOOKS = {
    "Gen", "Exo", "Lev", "Num", "Deu", "Jos", "Jdg", "Rut", "1Sa", "2Sa",
    "1Ki", "2Ki", "1Ch", "2Ch", "Ezr", "Neh", "Est", "Job", "Psa", "Pro",
    "Ecc", "Sng", "Isa", "Jer", "Lam", "Ezk", "Dan", "Hos", "Jol", "Amo",
    "Oba", "Jon", "Mic", "Nam", "Hab", "Zep", "Hag", "Zec", "Mal",
}

GREEK_RE = re.compile(r"Greek\s*=\s*([^\u2022()/]+)", re.UNICODE)

ref_re = re.compile(r"^([1-3]?[A-Za-z]{2,3})\.(\d+):(.+)$")


def parse_standard_ref(ref):
    """'Gen.32:1' -> ('Gen', '32', '1'). Returns None if it doesn't match
    a simple Book.Chapter:Verse shape, or if the verse part is itself a
    range/list ('Gen.5:32-6:1', 'Gen.5:32; 6:1') - those are
    Concatenation rows describing how two verses collapse into one, not
    the plain per-verse rows this table models."""
    if ";" in ref or "," in ref:
        return None
    m = ref_re.match(ref.strip())
    if not m:
        return None
    book, chapter, verse = m.group(1), m.group(2), m.group(3)
    if not verse.isdigit():
        return None
    return book, chapter, verse


def main():
    with open(SRC, encoding="utf-8-sig") as f:
        lines = f.readlines()

    start = next(i for i, l in enumerate(lines) if l.startswith("#DataStart(Expanded)"))
    end = next(i for i, l in enumerate(lines) if l.startswith("#DataEnd(Expanded)"))
    rows = lines[start + 1:end]

    # Dedup per (book, chapter, verse). TVTMS branches into several
    # SourceType blocks (e.g. "Eng-KJV+Hebrew", "Eng-KJV+Latin+Greek",
    # "LatinUndivided") for the same StandardRef, representing different
    # manuscript conventions for an unrelated ambiguity (typically an
    # English/Latin chapter-break choice). Prefer the plain "Eng-KJV"
    # SourceType when duplicates disagree, since that's the convention
    # River's own English-numbered translations follow; otherwise keep
    # the first-seen row and log the conflict for manual review.
    seen = OrderedDict()      # key -> (lxx_ref, source_type)
    conflicts = []

    for line in rows:
        if not line.strip() or line.startswith("'") or line.startswith("#"):
            continue
        cols = line.rstrip("\n").split("\t")
        if len(cols) < 8:
            continue
        source_type = cols[0].strip()
        std_ref = cols[2].strip()
        ancient = cols[7].strip()
        if not ancient or "Greek=" not in ancient:
            continue

        parsed = parse_standard_ref(std_ref)
        if parsed is None:
            continue
        book, chapter, verse = parsed
        if book not in OT_BOOKS:
            continue

        m = GREEK_RE.search(ancient)
        if not m:
            continue
        lxx_ref = m.group(1).strip()

        key = (book, chapter, verse)
        if key not in seen:
            seen[key] = (lxx_ref, source_type)
            continue

        prev_lxx, prev_type = seen[key]
        if lxx_ref == prev_lxx:
            continue  # agrees, nothing to do
        conflicts.append((key, seen[key], (lxx_ref, source_type)))
        if not prev_type.startswith("Eng-KJV") and source_type.startswith("Eng-KJV"):
            seen[key] = (lxx_ref, source_type)

    diffs = [
        (book, chapter, verse, lxx_ref)
        for (book, chapter, verse), (lxx_ref, _src) in seen.items()
        if lxx_ref != f"{chapter}:{verse}"
    ]

    with open(OUT, "w", encoding="utf-8") as f:
        f.write("book\tstd_chapter\tstd_verse\tlxx_ref\n")
        for book, chapter, verse, lxx_ref in diffs:
            f.write(f"{book}\t{chapter}\t{verse}\t{lxx_ref}\n")

    with open("mt_lxx_conflicts.tsv", "w", encoding="utf-8") as f:
        f.write("book\tchapter\tverse\tkept_lxx\tkept_sourcetype\tother_lxx\tother_sourcetype\n")
        for key, kept_before, other in conflicts:
            book, chapter, verse = key
            kept = seen[key]
            f.write(f"{book}\t{chapter}\t{verse}\t{kept[0]}\t{kept[1]}\t{other[0]}\t{other[1]}\n")

    print(f"Total StandardRef rows with a Greek annotation: {len(seen)}")
    print(f"Rows where English-standard != Greek (written to {OUT}): {len(diffs)}")
    print(f"Conflicting duplicate annotations (written to mt_lxx_conflicts.tsv): {len(conflicts)}")


if __name__ == "__main__":
    main()
