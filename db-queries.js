// db-queries.js
//
// All of River's actual SQL lives here, in one place, shared by both
// transports: server.js (website, over HTTP) and main.js (Electron, over
// IPC). Neither transport should ever build its own SQL — they just call
// these functions and translate the result (or a thrown error) into
// whatever their own protocol needs.
//
// createQueries(db) takes an already-open better-sqlite3 Database and
// returns { searchReady, lexiconReady, xrefsReady, listTranslations,
// getBookWords, getChapterWords, getAvailableBooks, getBookStructure, search,
// getLexiconEntry, searchLexicon, getWordOccurrences,
// getWordOccurrencesByQuery, getVersificationLinks, getCrossReferences }.
// Errors thrown from these functions carry a `statusCode` — server.js
// maps that straight onto the HTTP response; main.js's IPC handlers can
// ignore it (Electron just rejects the renderer's ipcRenderer.invoke()
// promise with the error's message either way).

function httpError(message, statusCode) {
  const err = new Error(message);
  err.statusCode = statusCode;
  return err;
}

// ---------------------------------------------------------------------
// Versification conversion — pure functions, no `db` dependency, so they
// can be unit-tested directly and reused wherever a chapter/verse needs
// to move between numbering traditions.
//
// index.html carries its own copy of this same logic (compare mode's
// "Align verse numbers" resolves entirely client-side, since it's only
// ever converting between translations already loaded in the browser).
// Keep the two in sync if either changes — same function names and
// shapes on purpose, to make that easy to verify by eye.
//
// THE PIVOT: 'MT' names the English-standard *reference space* (TVTMS's
// StandardRef), not "the Masoretic Text" and not "the KJV". StandardRef
// assigns addresses to the deuterocanon too (Baruch, Sirach, the Greek
// Esther/Daniel additions, ...), so an LXX-vs-Vulgate comparison in
// Baruch still has a pivot address to route through even though no
// MT-tradition translation in River displays anything there. Kept
// spelled 'MT' rather than renamed to 'STD' to avoid a migration across
// translations.versification, cross_references.versification, and every
// saved tab/compare state — read it as "standard".
//
// Data model: versification_links is an EDGE list, not a diff-against-
// pivot list. Most edges are pivot-anchored (from_tradition='MT',
// generated wholesale from TVTMS by build_versification_index.py), but
// the shape also accepts hand-curated DIRECT edges between two non-pivot
// traditions (from_tradition='LXX', to_tradition='VUL') for the places
// where no pivot address reconciles them at all — Sirach's Greek/Latin
// block transposition, Vulgate Tobit's different recension. See
// build_versification_index.py's --manual.
const PIVOT_VERSIFICATION = 'MT';

// A translation's versification tag is either a single tradition ('MT',
// 'LXX', 'VUL') or "<OT>/<NT>" — e.g. 'LXX/MT' means the Old Testament
// text is Septuagint-numbered but the NT cross-references written in
// that Bible assume Masoretic/English numbering. The part after the slash
// is what OT references made *from NT books* are written in; with no
// slash the NT expects the same numbering the OT has. Anything
// unrecognized falls back to the pivot ('MT') for the OT part, and the NT
// part follows the OT part. index.html has its own copy — keep in sync.
const VERSIFICATION_TRADITIONS = ['MT', 'LXX', 'VUL'];
function parseVersificationTag(tag) {
  const parts = String(tag || '').toUpperCase().split('/').map(s => s.trim());
  const ot = VERSIFICATION_TRADITIONS.includes(parts[0]) ? parts[0] : PIVOT_VERSIFICATION;
  const nt = VERSIFICATION_TRADITIONS.includes(parts[1]) ? parts[1] : ot;
  return { ot, nt };
}

// Parses a ref string relative to a known book — "6:1b" ->
// {chapter:6, verse:1, subverse:'b', approximate:false}. Ranges
// ("5:32-6:1"), free-text scrambles ("28:22*a-f [=28:23-28]") and "--"
// (no equivalent verse at all) are flagged `approximate` (a range) or
// return null (can't be resolved to one cell at all) — callers fall
// back to "not available" for those rather than guessing.
function parseVersificationRef(ref) {
  if (!ref || ref === '--' || /NotExist/i.test(ref)) return null;
  const s = String(ref).trim();
  const m = /^(\d+):(\d+)([a-z])?/.exec(s);
  if (!m) return null;
  // A same-chapter verse range right after the matched start ("21:1-2") is
  // a confident one-to-many correspondence — almost always a Psalm
  // superscription that one tradition counts as its own verse and the
  // other folds into the following verse — not a scramble. Captured as
  // verseEnd so callers can resolve/display every verse in the span
  // instead of only its first cell. Deliberately excludes anything where
  // the digits after the dash are themselves followed by ':' (a
  // chapter-crossing range like "5:32-6:1"), which stays approximate.
  const rangeM = /^-(\d+)(?!:)/.exec(s.slice(m[0].length));
  const verseEnd = rangeM ? parseInt(rangeM[1], 10) : null;
  const approximate = (s.indexOf('-') !== -1 && verseEnd == null) ||
    s.indexOf('[') !== -1 || s.indexOf(';') !== -1 || s.indexOf('*') !== -1;
  return { chapter: parseInt(m[1], 10), verse: parseInt(m[2], 10), verseEnd, subverse: m[3] || '', approximate };
}

function identityRef(chapter, verse) {
  return { chapter, verse, subverse: '', approximate: false, label: null };
}

// Builds forward/reverse lookup maps from one book's edge rows, once per
// request instead of re-scanning per verse.
//
//   fwd['MT>LXX|23|1']     -> parsed target ref, or null for '--'/unparseable
//   rev['MT>LXX|22|1']     -> { chapter:23, verse:1 }  (that same edge, backwards)
//   claimed['MT>LXX|23|1'] -> true: standard 23:1 IS renumbered in LXX
//
// 'manual' rows are applied after 'tvtms' rows and overwrite them for the
// same key, so a hand-curated edge always wins for the same address —
// see build_versification_index.py's --manual.
function buildVersificationIndex(rows) {
  const idx = { fwd: {}, rev: {}, claimed: {} };
  const ordered = (rows || []).slice().sort((a, b) =>
    (a.source === 'manual' ? 1 : 0) - (b.source === 'manual' ? 1 : 0));
  ordered.forEach((r) => {
    const pair = r.fromTradition + '>' + r.toTradition;
    const k = pair + '|' + r.fromChapter + '|' + r.fromVerse;
    idx.claimed[k] = true;
    const p = parseVersificationRef(r.toRef);
    idx.fwd[k] = null;
    if (!p) return; // '--' or unparseable: recorded as "no equivalent"
    p.label = r.toRef;
    idx.fwd[k] = p;
    if (p.verseEnd != null) {
      // A same-chapter multi-verse span ("21:1-2") is a confident,
      // one-to-many correspondence, not a scramble — register a reverse
      // entry for EVERY verse in the span so converting any of them back
      // to the source verse works, not just the span's first verse.
      // Tagged `split` (not `approximate`) since we know exactly which
      // source verse this is, just not that it's the sole match.
      for (let v = p.verse; v <= p.verseEnd; v++) {
        idx.rev[pair + '|' + p.chapter + '|' + v] = { chapter: r.fromChapter, verse: r.fromVerse, split: true, spanStart: p.verse, spanEnd: p.verseEnd };
      }
    } else if (!p.approximate) {
      idx.rev[pair + '|' + p.chapter + '|' + p.verse] = { chapter: r.fromChapter, verse: r.fromVerse };
    }
  });
  return idx;
}

// An explicit edge for exactly this address, in either orientation.
// Returns a resolved ref, { noEquivalent: true }, or undefined for "no
// edge recorded here at all" — the three cases are distinct and the
// caller needs to tell them apart.
function directEdge(idx, chapter, verse, from, to) {
  const k = from + '>' + to + '|' + chapter + '|' + verse;
  if (k in idx.fwd) {
    const p = idx.fwd[k];
    if (!p) return { noEquivalent: true };
    return { chapter: p.chapter, verse: p.verse, verseEnd: p.verseEnd, subverse: p.subverse, approximate: p.approximate, label: p.label };
  }
  const r = idx.rev[to + '>' + from + '|' + chapter + '|' + verse];
  if (r) return { chapter: r.chapter, verse: r.verse, subverse: '', approximate: false, split: !!r.split, spanStart: r.spanStart, spanEnd: r.spanEnd, label: r.chapter + ':' + r.verse };
  return undefined;
}

// One hop where exactly one side is the pivot. No recorded edge usually
// means "same number in both" — but NOT if the identically-numbered
// pivot address is itself renumbered in the other tradition, which
// proves the two aren't the same verse. Psalms is full of this: standard
// 23:1 is Vulgate 22:1, and nothing in the Vulgate is 23:1 by way of
// standard 23:1 — returning identity there would silently point at the
// wrong psalm.
function pivotStep(idx, chapter, verse, from, to) {
  const e = directEdge(idx, chapter, verse, from, to);
  if (e) return e.noEquivalent ? null : e;
  if (idx.claimed[to + '>' + from + '|' + chapter + '|' + verse]) return null;
  return identityRef(chapter, verse);
}

// Resolves a verse from one versification family to another. from/to are
// 'MT' (pivot), 'LXX' or 'VUL'.
//
// Order matters: an explicit direct edge for this exact address wins,
// then the pivot hop. Checking per-address rather than per-tradition-pair
// is deliberate — a manual file holding a dozen LXX->VUL edges for
// Sirach 33-36 must not divert every other verse in the book away from
// the pivot route.
//
// Returns { chapter, verse, subverse, approximate, label } or null when
// there's no usable equivalent (a genuine content gap, or a passage too
// scrambled to point at one cell).
function convertVerse(idx, chapter, verse, from, to) {
  if (from === to) return identityRef(chapter, verse);

  const e = directEdge(idx, chapter, verse, from, to);
  if (e) return e.noEquivalent ? null : e;

  if (from === PIVOT_VERSIFICATION || to === PIVOT_VERSIFICATION) {
    return pivotStep(idx, chapter, verse, from, to);
  }

  const mid = pivotStep(idx, chapter, verse, from, PIVOT_VERSIFICATION);
  if (!mid) return null;
  const out = pivotStep(idx, mid.chapter, mid.verse, PIVOT_VERSIFICATION, to);
  if (!out) return null;
  // Uncertainty accumulates: if either leg was a range or a scramble,
  // the result is approximate even when the other leg was exact.
  out.approximate = !!(out.approximate || mid.approximate);
  return out;
}

// Whole-chapter references (build_xrefs.py's verse-0 sentinel). Probes
// the first few verses rather than just v.1 — in a psalm where the
// Vulgate counts the superscription separately, v.1 is exactly the verse
// that maps oddly while v.2 onward are a clean offset.
function convertChapter(idx, chapter, from, to) {
  if (from === to) return chapter;
  for (let v = 1; v <= 4; v++) {
    const r = convertVerse(idx, chapter, v, from, to);
    if (r && !r.approximate) return r.chapter;
  }
  return chapter; // nothing usable — assume the chapter number is shared
}

// Strong's numbers proper only ever look like G1234 / H1234, optionally
// with a trailing disambiguation letter (e.g. H1234a). L and S extend the
// same shape to synthetic IDs for languages Strong's never covered: L for
// Latin (Lewis & Short, sequential IDs assigned at import time) and S for
// Syriac (SEDRA's own lexeme IDs, reused as-is so they stay stable across
// re-imports). Everything downstream — lookup, lexeme matching, the
// occurrences index — treats these exactly like a real Strong's number;
// only the letter says which vocabulary/lexicon a given ID belongs to.
const STRONG_RE = /^[GHLS]\d{1,6}[A-Za-z]?$/;

function createQueries(db) {
  const translationCols = db.prepare('PRAGMA table_info(translations)').all().map(c => c.name);
  const hasCopyrightCols = translationCols.includes('copyright_short') && translationCols.includes('license_text');
  // 'MT' (English/KJV-standard chapter:verse — KJV, ESV, NASB, etc. —
  // read as "standard", not "Masoretic"; see PIVOT_VERSIFICATION above),
  // 'LXX' (Septuagint-tradition, Greek chapter:verse — Brenton, NETS,
  // Swete, etc.), or 'VUL' (Vulgate/Douay-Rheims chapter:verse). NULL for
  // anything else (NT-only translations, or a bible.sqlite built before
  // this column existed) — a missing value is treated as 'MT' throughout
  // (translationVersification below), since that's the overwhelming
  // majority.
  const hasVersificationCol = translationCols.includes('versification');

  // Optional per-translation "About this translation" HTML — shown on
  // the new-tab page for whichever translation the new tab defaults to.
  // Same graceful-degradation pattern as the columns above: a
  // bible.sqlite built before this existed just won't have anything to
  // show there, rather than breaking startup.
  const hasInfoHtmlCol = translationCols.includes('info_html');

  // Distinguishes a psalm superscription/descriptive title (USFM \d, MT
  // style) from an ordinary section heading, both of which otherwise share
  // the words.hdg column — see build_db.py's migration and import_usfm.py.
  // Same graceful-degradation pattern: a bible.sqlite built before this
  // existed just renders every heading as a plain section heading.
  const wordCols = db.prepare('PRAGMA table_info(words)').all().map(c => c.name);
  const hasHdgTypeCol = wordCols.includes('hdg_type');

  const searchReady = db.prepare(
    "SELECT count(*) AS n FROM sqlite_master WHERE type = 'table' AND name = 'verses_fts'"
  ).get().n > 0;

  const lexiconReady = db.prepare(
    "SELECT count(*) AS n FROM sqlite_master WHERE type = 'table' " +
    "AND name IN ('lexicon_words', 'lexicon_entries', 'lexicon_sources')"
  ).get().n === 3;

  // Built by build_word_index.py — powers the "All occurrences" sidebar.
  // Optional like verses_fts: a bible.sqlite built before this existed
  // just won't offer the feature rather than breaking everything else.
  const occurrencesReady = db.prepare(
    "SELECT count(*) AS n FROM sqlite_master WHERE type = 'table' " +
    "AND name IN ('lexicon_word_occurrences', 'lexicon_gloss_stats')"
  ).get().n === 2;

  // Built by build_word_relations.py + build_word_families.py from open
  // lexicon data (Strong's derivations, Abbott-Smith, Wiktionary via
  // kaikki.org) — powers the Occurrences sidebar's opt-in "Include related
  // words" panel. Optional like the tables above: without it
  // getWordOccurrences simply returns family: null and the panel never
  // appears. lexicon_word_pos (token-derived part of speech) is optional
  // on top of that; members just show no POS label without it.
  const familyReady = occurrencesReady && db.prepare(
    "SELECT count(*) AS n FROM sqlite_master WHERE type = 'table' " +
    "AND name = 'lexicon_word_family'"
  ).get().n > 0;
  const posReady = db.prepare(
    "SELECT count(*) AS n FROM sqlite_master WHERE type = 'table' " +
    "AND name = 'lexicon_word_pos'"
  ).get().n > 0;

  // Built by build_versification_index.py from STEPBible's TVTMS data
  // (CC BY 4.0) — powers the compare view's "Align verse numbers" option
  // and versification-aware cross references for translations whose
  // chapter:verse numbering diverges (MT/LXX/VUL). Optional like the
  // tables above: a bible.sqlite built before this existed just won't
  // offer the feature.
  //
  // Two shapes are recognized. `versification_links` is the current
  // edge-list table. `versification_diffs` may be either an old (v0/v1)
  // real table from a bible.sqlite that hasn't been rebuilt since, or —
  // on a database that HAS been rebuilt — a compat VIEW that
  // build_versification_index.py leaves in place over versification_links
  // (pivot-anchored rows only, no direct LXX<->VUL edges) for any query
  // that still uses the old name. Prefer the real edge table when both
  // exist.
  const hasVersificationLinks = db.prepare(
    "SELECT count(*) AS n FROM sqlite_master WHERE type = 'table' AND name = 'versification_links'"
  ).get().n === 1;
  const hasVersificationDiffsCompat = db.prepare(
    "SELECT count(*) AS n FROM sqlite_master WHERE type IN ('table','view') AND name = 'versification_diffs'"
  ).get().n === 1;
  const versificationReady = hasVersificationLinks || hasVersificationDiffsCompat;

  // Built by build_xrefs.py from a book/chapter/verse cross-reference
  // dataset (e.g. openbible.info) — powers the reading pane's Cross
  // References sidebar tab. Optional like the tables above: a
  // bible.sqlite that never had build_xrefs.py run against it just
  // won't offer the feature.
  const xrefsReady = db.prepare(
    "SELECT count(*) AS n FROM sqlite_master WHERE type = 'table' " +
    "AND name = 'cross_references'"
  ).get().n > 0;

  // verse_text (built by build_search_index.py, same as verses_fts) is
  // what lets a cross-reference target actually show its own verse text
  // rather than just a bare reference — but it's a *separate* optional
  // table from cross_references itself: a bible.sqlite can easily have
  // one pipeline run and not the other. Checked independently so xrefsReady
  // (whether the feature exists at all) doesn't get tangled up with
  // whether readable snippets happen to be available too — see
  // crossReferences below, which drops the text subquery (and, to keep
  // its placeholder count matching, the translationId argument) entirely
  // when this is false rather than erroring on a missing table.
  const verseTextReady = db.prepare(
    "SELECT count(*) AS n FROM sqlite_master WHERE type = 'table' " +
    "AND name = 'verse_text'"
  ).get().n > 0;

  // words.strong is sometimes stored bare ('3056') rather than prefixed
  // ('G3056') — words.lang already disambiguates Greek/Hebrew, so some
  // interlinear source data skips the letter on the token table even
  // though lexicon_words.strong is always prefixed. This normalizes
  // words.strong to the prefixed form at query time, both for the
  // lexicon join and for the value actually returned to callers.
  const NORMALIZED_WORD_STRONG =
    "(CASE WHEN words.strong LIKE 'G%' OR words.strong LIKE 'H%' THEN words.strong " +
    "ELSE (CASE WHEN words.lang = 'Hebrew' THEN 'H' ELSE 'G' END) || words.strong END)";
  // Same expression, aliased for the "w" table alias used in the
  // per-occurrence exact-gloss subquery below (see wordOccurrences).
  const NORMALIZED_WORD_STRONG_W = NORMALIZED_WORD_STRONG.replace(/words\./g, 'w.');

  // Separates individual glosses within the GROUP_CONCAT'd string built by
  // the wordOccurrences query below. Uses the ASCII Unit Separator (0x1F)
  // rather than a printable character like ',' or ';' since those can
  // legitimately appear inside a gloss/short phrase itself.
  const GLOSS_SEP = '\u001f';

  // A gloss cell in words/edits can carry a bracket- or brace-wrapped
  // "supplied" portion (e.g. "the [one]", "sun {to rise}") — see
  // renderGlossHtml in index.html. build_word_index.py strips these
  // (and normalizes whitespace) before aggregating into
  // lexicon_gloss_stats, so the gloss-filter chips shown in the
  // Occurrences sidebar are always bracket-free. The per-occurrence
  // glosses returned below come straight from words/edits instead, so
  // they need the same stripping applied here — otherwise a chip like
  // "one" never matches an occurrence whose raw gloss is "the [one]",
  // and the filter silently shows zero matches for every occurrence.
  function stripGlossBrackets(s){
    return s.replace(/[[\]{}]/g, '').replace(/\s+/g, ' ').trim();
  }

  // Builds the word-level SELECT shared by bookWords and chapterWords.
  // extraWhere is '' or an extra AND-clause with its own placeholders,
  // appended after the translation/book conditions.
  function bookWordsSql(extraWhere) {
    return (
      'SELECT verses.chapter AS chapter, verses.verse AS verse, books.name AS book, ' +
      '       words.lang AS lang, words.original AS original, words.translit AS translit, ' +
      '       words.parsing AS parsing, ' + NORMALIZED_WORD_STRONG + ' AS strong, words.gloss AS gloss, ' +
      '       words.lemma AS wordLemma, ' +
      '       words.beg_q AS begQ, words.pnc AS pnc, words.end_q AS endQ, words.end_text AS endText, ' +
      '       words.footnote AS footnote, words.hdg AS hdg, ' +
      (hasHdgTypeCol ? 'words.hdg_type AS hdgType, ' : '') +
      'words.crossref AS crossref, ' +
      '       words.par_classes AS parClasses, edits.gloss AS editGloss' +
      (lexiconReady ?
        ', lw.lexeme AS lexLexeme, lw.translit AS lexTranslit, ' +
        'lw.pronunciation AS lexPronunciation, lw.short_gloss AS lexShortGloss'
        : '') + ' ' +
      'FROM verses ' +
      'JOIN books ON books.id = verses.book_id ' +
      'JOIN words ON words.verse_id = verses.id ' +
      'LEFT JOIN edits ON edits.word_id = words.id ' +
      (lexiconReady ? 'LEFT JOIN lexicon_words lw ON lw.strong = ' + NORMALIZED_WORD_STRONG + ' ' : '') +
      'WHERE verses.translation_id = ? ' +
      '  AND verses.book_id IN (SELECT id FROM books WHERE lower(name) = ?) ' +
      extraWhere +
      'ORDER BY verses.chapter, verses.verse, words.position'
    );
  }

  const stmts = {
    listTranslations: db.prepare(
      'SELECT id, code, name, language' +
      (hasCopyrightCols ? ', copyright_short, license_text' : '') +
      (hasVersificationCol ? ', versification' : '') +
      (hasInfoHtmlCol ? ', info_html' : '') +
      ' FROM translations ORDER BY id'
    ),

    // Every word of a book (bookWords) or of a chapter range within it
    // (chapterWords, which takes two extra bound parameters: first and
    // last chapter). The book is matched with `book_id IN (SELECT ...)`
    // rather than `lower(books.name) = ?` directly on the joined rows:
    // wrapping a column in lower() makes it impossible for SQLite to use
    // an index on it, so the old form had to look at every verse row in
    // the table for the translation. Resolving the (tiny) books table
    // first turns that into a plain book_id lookup — same rows out, far
    // fewer read. IN rather than = so a books table that ever holds two
    // rows with the same name still returns both.
    bookWords: db.prepare(bookWordsSql('')),
    chapterWords: db.prepare(bookWordsSql('AND verses.chapter BETWEEN ? AND ? ')),

    availableBooks: db.prepare(
      'SELECT books.name AS name FROM books ' +
      'JOIN verses ON verses.book_id = books.id ' +
      'WHERE verses.translation_id = ? ' +
      'GROUP BY books.id ORDER BY books.book_order'
    ),

    // Every (chapter, verse) that actually exists for one book+translation,
    // straight off the verses table (one row per verse) rather than words
    // (one row per *word*, often 10-20x more rows). Backs the browse-by-
    // book popover's chapter grid and per-chapter verse grid, both of
    // which only ever needed chapter/verse *numbers* and previously had
    // to pull through getBookWords/ensureBookLoaded (every word of the
    // whole book) just to derive them client-side. Deliberately not
    // collapsed to a per-chapter MAX(verse) here — verse numbers aren't
    // guaranteed gap-free, so the client still needs the exact list (see
    // ensureVerseList's comment in index.html). Order matches bookWords'
    // own (chapter, verse) ordering.
    bookStructure: db.prepare(
      'SELECT verses.chapter AS chapter, verses.verse AS verse ' +
      'FROM verses ' +
      'WHERE verses.translation_id = ? ' +
      '  AND verses.book_id IN (SELECT id FROM books WHERE lower(name) = ?) ' +
      '  AND EXISTS (SELECT 1 FROM words WHERE words.verse_id = verses.id) ' +
      'ORDER BY verses.chapter, verses.verse'
    ),

    // search is intentionally NOT a single fixed prepared statement — see
    // getSearchStmt below. A bible.sqlite built before search support
    // existed just won't offer the feature (searchReady stays false)
    // rather than breaking everything else.

    // Only prepared when the lexicon tables exist. One lemma can have
    // several dictionary entries (one per source); this returns one row
    // per entry, grouped into { lemma, entries[] } below.
    lexiconEntry: lexiconReady ? db.prepare(
      'SELECT lw.strong AS strong, lw.lexeme AS lexeme, lw.translit AS translit, ' +
      '       lw.pronunciation AS pronunciation, lw.short_gloss AS shortGloss, ' +
      '       ls.code AS lexiconCode, ls.name AS lexiconName, ls.copyright AS lexiconCopyright, le.entry_html AS entryHtml ' +
      'FROM lexicon_words lw ' +
      'LEFT JOIN lexicon_entries le ON le.strong_id = lw.id ' +
      'LEFT JOIN lexicon_sources ls ON ls.id = le.source_id ' +
      'WHERE lw.strong = ?'
    ) : null,

    // Exact match against a dictionary lexeme (the lemma spelling
    // lexicon_words.lexeme itself carries, e.g. "\u03bb\u03cc\u03b3\u03bf\u03c2" or
    // "\u05d0\u05b1\u05dc\u05b9\u05d4\u05b4\u05d9\u05dd") — powers the "paste a lexeme" half of
    // resolveLexicalQuery below. Usually one row; more than one only
    // when two Strong's numbers happen to share an identical spelling
    // (homographs), which the caller surfaces as a pick-one list rather
    // than guessing. totalOccurrences is only meaningful once
    // occurrencesReady — NULL otherwise, and toCandidate() below treats
    // that as 0 for sorting purposes.
    lexemeExact: lexiconReady ? db.prepare(
      'SELECT lw.strong AS strong, lw.lexeme AS lexeme, lw.translit AS translit, ' +
      '       lw.short_gloss AS shortGloss' +
      (occurrencesReady ?
        ', (SELECT SUM(o.occurrence_count) FROM lexicon_word_occurrences o WHERE o.strong = lw.strong) AS totalOccurrences'
        : ', NULL AS totalOccurrences') + ' ' +
      'FROM lexicon_words lw WHERE lw.lexeme = ? ' +
      'ORDER BY totalOccurrences DESC'
    ) : null,

    // A word with no Strong's number of its own (e.g. a supplied word,
    // or interlinear data that simply never tagged it) can\u2019t be looked
    // up by strong or matched against a dictionary lexeme directly \u2014
    // but its own literal spelling (words.original) may recur elsewhere
    // in the text on tokens that *do* carry a Strong's number. This
    // finds every Strong's number that literal spelling maps to
    // elsewhere, most-attested first, so the caller can either jump
    // straight to the one clear match or offer the handful of
    // candidates to pick from \u2014 see resolveLexicalQuery.
    strongsByLiteralText: lexiconReady ? db.prepare(
      'WITH matches AS (' +
      '  SELECT ' + NORMALIZED_WORD_STRONG + ' AS strong, COUNT(*) AS matchCount ' +
      '  FROM words WHERE words.original = ? ' +
      '  GROUP BY ' + NORMALIZED_WORD_STRONG +
      ') ' +
      'SELECT m.strong AS strong, m.matchCount AS matchCount, lw.lexeme AS lexeme, ' +
      '       lw.translit AS translit, lw.short_gloss AS shortGloss' +
      (occurrencesReady ?
        ', (SELECT SUM(o.occurrence_count) FROM lexicon_word_occurrences o WHERE o.strong = m.strong) AS totalOccurrences'
        : ', NULL AS totalOccurrences') + ' ' +
      'FROM matches m LEFT JOIN lexicon_words lw ON lw.strong = m.strong ' +
      'WHERE m.strong IS NOT NULL ' +
      'ORDER BY COALESCE(totalOccurrences, m.matchCount) DESC ' +
      'LIMIT 25'
    ) : null,

    // Substring search across lexicon_words — the fallback of last resort
    // in resolveLexicalQuery, used when a pasted query isn't a Strong's
    // number, doesn't match a lexeme exactly, and doesn't match any
    // words.original text either. Matches lexeme, translit, or
    // short_gloss so "search by lexeme" also works for someone typing a
    // partial spelling, a transliteration, or an English gloss word they
    // remember rather than the exact original-language spelling. Ranked
    // by total occurrences (falling back to 0 for lexicon entries that
    // predate the occurrences index) so common words surface first.
    // LIMIT is deliberately tighter than strongsByLiteralText's 25 — a
    // substring match against a whole lexicon can return far more
    // plausible-looking hits than an exact literal-text match can.
    lexiconFuzzySearch: lexiconReady ? db.prepare(
      'SELECT lw.strong AS strong, lw.lexeme AS lexeme, lw.translit AS translit, ' +
      '       lw.short_gloss AS shortGloss' +
      (occurrencesReady ?
        ', (SELECT SUM(o.occurrence_count) FROM lexicon_word_occurrences o WHERE o.strong = lw.strong) AS totalOccurrences'
        : ', NULL AS totalOccurrences') + ' ' +
      'FROM lexicon_words lw ' +
      'WHERE lw.lexeme LIKE ? ESCAPE \'\\\' ' +
      '   OR lw.translit LIKE ? ESCAPE \'\\\' ' +
      '   OR lw.short_gloss LIKE ? ESCAPE \'\\\' ' +
      'ORDER BY totalOccurrences DESC, lw.lexeme ASC ' +
      'LIMIT 20'
    ) : null,

    // Every distinct verse location a Strong's number occurs at, already
    // deduplicated across translations and ordered in canonical Bible
    // order by build_word_index.py. glossesAtOccurrence is the exact
    // English gloss(es) actually used for that Strong's number at that
    // specific verse in the *requested* translation (an edit's gloss
    // wins over the interlinear word's own gloss, same precedence as
    // bookWords/editGloss above) — not a guess from aggregate frequency
    // data, so the client can filter/highlight exactly rather than by
    // testing whether some plausible-looking gloss word merely appears
    // somewhere in the verse text. Not DISTINCT here — SQLite's
    // GROUP_CONCAT only allows DISTINCT with the single-argument,
    // default-separator form — so getWordOccurrences() below dedupes
    // after splitting instead.
    wordOccurrences: occurrencesReady ? db.prepare(
      'SELECT o.book_name AS book, o.book_order AS bookOrder, o.testament, o.chapter, o.verse, o.occurrence_count AS occurrenceCount, ' +
      "       COALESCE(vt.text, 'not available') AS text, " +
      '       (SELECT GROUP_CONCAT(COALESCE(e.gloss, w.gloss), \'' + GLOSS_SEP + '\') ' +
      '        FROM verses v2 ' +
      '        JOIN words w ON w.verse_id = v2.id ' +
      '        LEFT JOIN edits e ON e.word_id = w.id ' +
      '        WHERE v2.translation_id = ? AND v2.book_id = o.book_id ' +
      '          AND v2.chapter = o.chapter AND v2.verse = o.verse ' +
      '          AND ' + NORMALIZED_WORD_STRONG_W + ' = o.strong' +
      '       ) AS glossesAtOccurrence ' +
      'FROM lexicon_word_occurrences o ' +
      'LEFT JOIN verse_text vt ON vt.book_id = o.book_id AND vt.chapter = o.chapter AND vt.verse = o.verse AND vt.translation_id = ? ' +
      'WHERE o.strong = ? ' +
      'ORDER BY o.book_order, o.chapter, o.verse'
    ) : null,

    // Per-translation gloss frequency for a Strong's number, highest
    // frequency first; ties broken by translation so a given translation's
    // glosses stay grouped together.
    wordGlossStats: occurrencesReady ? db.prepare(
      'SELECT gs.translation_id AS translationId, tr.code AS translationCode, tr.name AS translationName, ' +
      '       gs.gloss AS gloss, gs.occurrence_count AS count ' +
      'FROM lexicon_gloss_stats gs ' +
      'LEFT JOIN translations tr ON tr.id = gs.translation_id ' +
      'WHERE gs.strong = ? ' +
      'ORDER BY gs.translation_id, gs.occurrence_count DESC'
    ) : null,

    // Related words for one Strong's number (see build_word_families.py for
    // what each tier means), limited to members that actually occur in the
    // occurrence index — a family member with no occurrences would be an
    // empty chip. Best part-of-speech source wins: token-derived
    // ('tokens', from the interlinear's own morphology) over Wiktionary.
    // Ordered tier first, then most frequent, so the client can cap chips
    // per tier and still show the most useful ones.
    wordFamily: familyReady ? db.prepare(
      'SELECT f.member_strong AS strong, lw.lexeme AS lexeme, f.tier AS tier, ' +
      '       f.support AS support, f.kind AS kind, f.sources AS sources, ' +
      '       (SELECT SUM(o.occurrence_count) FROM lexicon_word_occurrences o WHERE o.strong = f.member_strong) AS total' +
      (posReady
        ? ', (SELECT p.pos FROM lexicon_word_pos p WHERE p.strong = f.member_strong ' +
          "   ORDER BY CASE p.source WHEN 'tokens' THEN 0 ELSE 1 END LIMIT 1) AS pos"
        : ', NULL AS pos') + ' ' +
      'FROM lexicon_word_family f ' +
      'JOIN lexicon_words lw ON lw.strong = f.member_strong ' +
      'WHERE f.strong = ? ' +
      '  AND EXISTS (SELECT 1 FROM lexicon_word_occurrences o2 WHERE o2.strong = f.member_strong) ' +
      'ORDER BY f.tier, total DESC, lw.lexeme'
    ) : null,

    wordSelfPos: (familyReady && posReady) ? db.prepare(
      'SELECT p.pos AS pos FROM lexicon_word_pos p WHERE p.strong = ? ' +
      "ORDER BY CASE p.source WHEN 'tokens' THEN 0 ELSE 1 END LIMIT 1"
    ) : null,

    // Every recorded versification edge for one book, as { fromTradition,
    // fromChapter, fromVerse, toTradition, toRef, source }. Small (a few
    // dozen to a couple thousand rows per book) — both this server-side
    // use (getCrossReferences below) and the client (compare mode's
    // "Align verse numbers", via ensureVersificationDiffs/
    // buildVersificationIndex in index.html) load a whole book's worth
    // at once and resolve individual verses against it locally.
    //
    // Reads straight from versification_links when it exists (the
    // current, edge-list shape — see build_versification_index.py).
    // Falls back to reading through the versification_diffs compat view
    // ONLY when versification_links doesn't exist at all — i.e. this
    // bible.sqlite hasn't been rebuilt since the edge-list migration —
    // in which case only pivot-anchored (MT->tradition) rows are
    // available; there's no way to expose a direct LXX<->VUL edge
    // through the old 5-column shape.
    versificationLinks: hasVersificationLinks ? db.prepare(
      'SELECT from_tradition AS fromTradition, from_chapter AS fromChapter, from_verse AS fromVerse, ' +
      '       to_tradition AS toTradition, to_ref AS toRef, source AS source ' +
      'FROM versification_links WHERE book = ? ' +
      'ORDER BY (source = \'manual\'), from_chapter, from_verse'
    ) : (hasVersificationDiffsCompat ? db.prepare(
      "SELECT 'MT' AS fromTradition, std_chapter AS fromChapter, std_verse AS fromVerse, " +
      "       tradition AS toTradition, ref AS toRef, 'tvtms' AS source " +
      'FROM versification_diffs WHERE book = ? ORDER BY std_chapter, std_verse'
    ) : null),

    // Outbound: every cross-reference sourced from a verse within
    // [fromVerse, toVerse] of one book/chapter IN ONE DATASET'S OWN
    // NUMBERING, best-voted first. Powers both the Xrefs sidebar's
    // single-verse filtered view (fromVerse === toVerse) and its
    // whole-open-range aggregate view (the full span currently rendered
    // in the pane — see paneOpenChapterRange in index.html).
    //
    // The `cr.versification = ?` filter is new: cross_references can now
    // hold rows in more than one numbering tradition at once (an
    // openbible/MT set alongside a Douay/VUL set), and every number in
    // this query — the from_chapter/from_verse bounds included — has to
    // be in the SAME tradition as the rows being matched, or the BETWEEN
    // test is comparing numbers from two different numbering systems.
    // getCrossReferences below calls this once per dataset present,
    // with fromVerse/toVerse already converted into that dataset's own
    // numbering (see convertVerse), and converts the results back
    // afterward.
    //
    // `sourceVerse` (cr.from_verse — never a range in this schema, same
    // as the inbound side's own from_verse) is the specific verse within
    // [fromVerse, toVerse] this particular row actually comes from,
    // distinct from the queried range itself: lets the client label each
    // card with its own source verse in the whole-open-range case
    // instead of just the whole queried span (see renderXrefCardHtml in
    // index.html) — still in the dataset's own numbering here;
    // getCrossReferences converts it to the pane's numbering before
    // returning. `text` is the target's own verse text in the requested
    // translation, concatenated across to_verse..to_verse_end in order
    // for a range target (e.g. "1 Thessalonians 5:23-24") — NULL when
    // verseTextReady is false, when to_verse is 0 (build_xrefs.py's
    // sentinel for a "loose", chapter-level target with no specific
    // verse — nothing sensible to concatenate), or when this specific
    // translation doesn't carry that verse.
    //
    // This `text` is fetched at the RAW to_chapter/to_verse, still in
    // the dataset's own numbering — fine only when the pane's
    // translation shares that dataset's versification, since verse_text
    // is keyed by each translation's own chapter/verse. getCrossReferences
    // below re-fetches text itself, at the CONVERTED location, whenever
    // dataset versification !== pane versification, and uses this
    // column as-is otherwise (the common case: one dataset, no
    // conversion needed) to save a round trip.
    crossReferencesOutbound: xrefsReady ? db.prepare(
      'SELECT b.name AS book, cr.to_chapter AS chapter, cr.to_verse AS verse, ' +
      '       cr.to_verse_end AS verseEnd, cr.from_verse AS sourceVerse, cr.votes AS votes, cr.source AS source' +
      (verseTextReady ?
        ', (SELECT CASE WHEN cr.to_verse = 0 THEN NULL ELSE (' +
        '     SELECT group_concat(t.text, \' \') FROM (' +
        '       SELECT vt.text AS text FROM verse_text vt ' +
        '       WHERE vt.translation_id = ? AND vt.book_id = b.id AND vt.chapter = cr.to_chapter ' +
        '         AND vt.verse BETWEEN cr.to_verse AND COALESCE(cr.to_verse_end, cr.to_verse) ' +
        '       ORDER BY vt.verse' +
        '     ) t' +
        '   ) END) AS text'
        : ', NULL AS text') + ' ' +
      'FROM cross_references cr ' +
      'JOIN books b ON lower(b.name) = cr.to_book ' +
      'WHERE cr.versification = ? AND cr.from_book = ? AND cr.from_chapter = ? AND cr.from_verse BETWEEN ? AND ? ' +
      'ORDER BY cr.votes DESC'
    ) : null,

    // Inbound ("referenced by"): every cross-reference whose *target*
    // overlaps [fromVerse, toVerse] of one book/chapter IN ONE DATASET'S
    // OWN NUMBERING, best-voted first — the reverse direction from
    // crossReferencesOutbound above, using idx_xrefs_to. Same new
    // `cr.versification = ?` filter and the same reason: every number
    // bound into this query has to already be in the dataset's own
    // numbering (getCrossReferences converts fromVerse/toVerse before
    // calling), or the overlap test below is comparing across two
    // numbering systems.
    //
    // Unlike the outbound side, from_verse is never a range in this
    // schema, so each row's own book/chapter/verse here is simply the
    // citing verse's own location (dataset numbering; converted to the
    // pane's numbering by getCrossReferences before it's returned) — but
    // the row also carries targetVerseStart/targetVerseEnd (the range
    // *within the queried chapter*, dataset numbering, that citing verse
    // actually points at, COALESCE'd to a single value when it's not a
    // range), which is what lets the client tell an exact single-verse
    // match from a broader one that merely includes the requested verse
    // (see getCrossReferences below and renderXrefsSidebar's
    // buildXrefGroups in index.html). A stored to_verse of 0
    // (chapter-level target — see build_xrefs.py) always overlaps every
    // verse of the chapter it targets, so it's included unconditionally
    // rather than via the BETWEEN test.
    crossReferencesInbound: xrefsReady ? db.prepare(
      'SELECT b.name AS book, cr.from_chapter AS chapter, cr.from_verse AS verse, ' +
      '       cr.votes AS votes, cr.source AS source, ' +
      '       cr.to_verse AS targetVerseStart, COALESCE(cr.to_verse_end, cr.to_verse) AS targetVerseEnd' +
      (verseTextReady ?
        ', (SELECT CASE WHEN cr.from_verse = 0 THEN NULL ELSE (' +
        '     SELECT vt.text FROM verse_text vt ' +
        '     WHERE vt.translation_id = ? AND vt.book_id = b.id AND vt.chapter = cr.from_chapter AND vt.verse = cr.from_verse' +
        '   ) END) AS text'
        : ', NULL AS text') + ' ' +
      'FROM cross_references cr ' +
      'JOIN books b ON lower(b.name) = cr.from_book ' +
      'WHERE cr.versification = ? AND cr.to_book = ? AND cr.to_chapter = ? ' +
      '  AND (cr.to_verse = 0 OR (cr.to_verse <= ? AND ? <= COALESCE(cr.to_verse_end, cr.to_verse))) ' +
      'ORDER BY cr.votes DESC'
    ) : null,

    // Every distinct versification tag actually present in
    // cross_references, read once at startup rather than per request —
    // this table is populated by an offline import pipeline
    // (build_xrefs.py), not written to at runtime, so it can't go stale
    // during a server process's lifetime. Drives which datasets
    // getCrossReferences below has to query and convert.
    xrefVersifications: xrefsReady ? db.prepare(
      'SELECT DISTINCT versification FROM cross_references'
    ) : null,

    // A translation's own versification tag, defaulting 'MT' for a NULL
    // or missing value (NT-only translations, or a bible.sqlite built
    // before the column existed) — the overwhelming majority of
    // translations are English/KJV-numbered, so that's the safe default
    // rather than guessing 'LXX' or 'VUL'.
    translationVersification: hasVersificationCol ? db.prepare(
      'SELECT versification FROM translations WHERE id = ?'
    ) : null,

    // Re-fetches a single verse's text at an explicit (book, chapter,
    // verse) — used by getCrossReferences to get the snippet AFTER
    // converting a cross-reference's target into the pane's own
    // numbering, for the (common, once two datasets are loaded) case
    // where that differs from the numbering the row was originally
    // queried in. crossReferencesOutbound/Inbound's own `text` column is
    // only trustworthy when no conversion happened.
    verseTextAt: verseTextReady ? db.prepare(
      'SELECT vt.text AS text FROM verse_text vt JOIN books b ON b.id = vt.book_id ' +
      'WHERE vt.translation_id = ? AND lower(b.name) = ? AND vt.chapter = ? AND vt.verse = ?'
    ) : null,

    // Same, for a converted range target (verse..verseEnd), concatenated
    // in verse order — mirrors crossReferencesOutbound's own inline
    // subquery, just parameterized for reuse after conversion.
    verseTextRange: verseTextReady ? db.prepare(
      'SELECT group_concat(text, \' \') AS text FROM (' +
      '  SELECT vt.text AS text FROM verse_text vt JOIN books b ON b.id = vt.book_id ' +
      '  WHERE vt.translation_id = ? AND lower(b.name) = ? AND vt.chapter = ? ' +
      '    AND vt.verse BETWEEN ? AND ? ' +
      '  ORDER BY vt.verse' +
      ')'
    ) : null
  };

  // The search statement isn't fixed like the others above because an
  // optional book: filter (see extractBookFilters in index.html) needs
  // one "?" placeholder per book in its IN clause, and that count varies
  // call to call. Rather than reprepare from scratch on every search,
  // this caches one prepared statement per distinct book-count seen so
  // far (at most 66 canonical books, plus whatever apocryphal names show
  // up — a small, self-limiting cache for any real bible.sqlite).
  //
  // verses_fts.text / .orig / .orig_x hold *folded* text (diacritics
  // stripped, or — orig_x — encoded; see build_search_index.py), so they're
  // for matching only. The text shown to the user comes from verse_text,
  // which keeps the original spelling, hence the hvt join below. bm25
  // weights are per column in declaration order: text, orig, orig_x — the
  // exact-diacritics copy (orig_x) is match-only and doesn't affect rank.
  //
  // bm25() still ranks purely on text relevance; the book filter narrows
  // which rows verses_fts hands to bm25() in the first place; it's not
  // part of the ranking itself.
  const searchStmtCache = new Map();
  function getSearchStmt(bookCount) {
    if (!searchReady) return null;
    let stmt = searchStmtCache.get(bookCount);
    if (stmt) return stmt;
    const bookClause = bookCount > 0
      ? ' AND lower(book_name) IN (' + new Array(bookCount).fill('?').join(',') + ')'
      : '';
    stmt = db.prepare(
      'WITH hits AS MATERIALIZED (' +
      '  SELECT book_id, book_name, chapter, verse, testament, translation_id, bm25(verses_fts, 1.0, 1.0, 0.0) AS rnk' +
      '  FROM verses_fts WHERE verses_fts MATCH ?' + bookClause +
      '), grouped AS (' +
      '  SELECT book_id, book_name, chapter, verse, testament, MIN(rnk) AS best_rnk' +
      '  FROM hits GROUP BY book_id, chapter, verse' +
      ') ' +
      'SELECT g.book_name AS book, g.chapter AS chapter, g.verse AS verse, g.testament AS testament, ' +
      '       g.best_rnk AS rank, hvt.text AS hitText, tr.code AS hitTranslationCode, ' +
      '       vt.text AS activeText ' +
      'FROM grouped g ' +
      'JOIN hits h ON h.book_id = g.book_id AND h.chapter = g.chapter AND h.verse = g.verse AND h.rnk = g.best_rnk ' +
      'LEFT JOIN translations tr ON tr.id = h.translation_id ' +
      'LEFT JOIN verse_text hvt ON hvt.translation_id = h.translation_id AND hvt.book_id = h.book_id AND hvt.chapter = h.chapter AND hvt.verse = h.verse ' +
      'LEFT JOIN verse_text vt ON vt.book_id = g.book_id AND vt.chapter = g.chapter AND vt.verse = g.verse AND vt.translation_id = ? ' +
      'GROUP BY g.book_id, g.chapter, g.verse ' +
      'ORDER BY g.best_rnk ASC ' +
      'LIMIT 1000'
    );
    searchStmtCache.set(bookCount, stmt);
    return stmt;
  }

  function requireInt(value, label) {
    const n = Number(value);
    if (!Number.isInteger(n)) throw httpError(label + ' must be an integer', 400);
    return n;
  }

  // Cached once — see the comment on the xrefVersifications prepared
  // statement above for why this is safe to compute at startup rather
  // than per request.
  const xrefVersificationsCache = stmts.xrefVersifications
    ? stmts.xrefVersifications.all().map(r => r.versification)
    : [];

  function translationVersification(tid) {
    if (!stmts.translationVersification) return PIVOT_VERSIFICATION;
    const row = stmts.translationVersification.get(tid);
    // The numbering of this translation's own OT text ('LXX/MT' -> 'LXX');
    // the NT-expectation half of the tag only matters to reference links
    // in the client.
    return parseVersificationTag(row && row.versification).ot;
  }

  // Re-fetches verse text at an explicit location, used once a
  // cross-reference's target has been converted into the pane's own
  // numbering and therefore no longer matches whatever the original
  // outbound/inbound query's own inline text subquery fetched.
  function fetchVerseText(tid, bookName, chapter, verseStart, verseEnd) {
    if (!verseTextReady || tid == null) return null;
    const bk = String(bookName).toLowerCase();
    if (verseStart === verseEnd) {
      const row = stmts.verseTextAt.get(tid, bk, chapter, verseStart);
      return row ? row.text : null;
    }
    const row = stmts.verseTextRange.get(tid, bk, chapter, verseStart, verseEnd);
    return row && row.text ? row.text : null;
  }

  // Converts one outbound cross-reference row (queried in dataset `ds`'s
  // own numbering) into the pane's `display` numbering, in place, and
  // returns it. `approxRange` is passed down from the caller's endpoint
  // conversion (getCrossReferences) — if the *query range itself* only
  // approximately maps into this dataset, every row it turned up
  // inherits that uncertainty regardless of how cleanly its own target
  // converts back.
  //
  // A target with NO equivalent in the pane's tradition (the common case
  // for a Douay cross-reference into Esther's additions, viewed from an
  // MT pane) is marked `noEquivalent` rather than dropped — the caller
  // (index.html's renderXrefCardHtml) renders that as "no equivalent
  // verse in this text" with its Verse/Context buttons suppressed,
  // rather than silently losing the card or, worse, opening the wrong
  // passage.
  function convertOutboundRow(r, ds, display, idx, tid, approxRange) {
    const rawChapter = r.chapter, rawVerse = r.verse, rawVerseEnd = r.verseEnd;
    const isChapterLevel = !rawVerse;

    let converted;
    if (isChapterLevel) {
      converted = { chapter: convertChapter(idx, rawChapter, ds, display), verse: 0, approximate: approxRange };
    } else {
      const c = convertVerse(idx, rawChapter, rawVerse, ds, display);
      converted = c ? { chapter: c.chapter, verse: c.verse, approximate: approxRange || c.approximate } : null;
    }

    const sourceLabel = ds !== display
      ? rawChapter + ':' + (rawVerse || '') + (rawVerseEnd && rawVerseEnd !== rawVerse ? '\u2013' + rawVerseEnd : '')
      : null;

    if (!converted) {
      return { ...r, noEquivalent: true, text: null, approximate: approxRange, sourceLabel };
    }

    let verseEnd = null;
    if (!isChapterLevel && rawVerseEnd && rawVerseEnd !== rawVerse) {
      const endConv = convertVerse(idx, rawChapter, rawVerseEnd, ds, display);
      if (endConv && endConv.chapter === converted.chapter) {
        verseEnd = endConv.verse;
      } else {
        // The range's far end doesn't convert cleanly onto the same
        // converted chapter as its start (a range straddling a
        // psalm/Daniel-3-style insertion) — degrade to a single-verse
        // reference rather than show a nonsensical span, and flag it.
        converted.approximate = true;
      }
    }

    let sourceVerse = null;
    if (r.sourceVerse) {
      const svConv = convertVerse(idx, rawChapter, r.sourceVerse, ds, display);
      sourceVerse = svConv ? svConv.verse : r.sourceVerse; // best-effort fallback rather than dropping the label
    }

    const text = ds === display
      ? r.text
      : (isChapterLevel ? null : fetchVerseText(tid, r.book, converted.chapter, converted.verse, verseEnd || converted.verse));

    return {
      ...r,
      chapter: converted.chapter,
      verse: converted.verse,
      verseEnd,
      sourceVerse,
      approximate: converted.approximate,
      sourceLabel,
      noEquivalent: false,
      text
    };
  }

  // Mirror of convertOutboundRow for the inbound ("referenced by") side.
  // Here `chapter`/`verse` are the CITING location (also converted), and
  // targetVerseStart/targetVerseEnd are converted separately since they
  // describe a position within the queried chapter — dsChapter — not
  // within the citing verse's own chapter.
  function convertInboundRow(r, ds, display, idx, tid, approxRange, dsChapter) {
    const rawChapter = r.chapter, rawVerse = r.verse;
    const isChapterLevel = !rawVerse;

    let converted;
    if (isChapterLevel) {
      converted = { chapter: convertChapter(idx, rawChapter, ds, display), verse: 0, approximate: approxRange };
    } else {
      const c = convertVerse(idx, rawChapter, rawVerse, ds, display);
      converted = c ? { chapter: c.chapter, verse: c.verse, approximate: approxRange || c.approximate } : null;
    }

    const sourceLabel = ds !== display ? rawChapter + ':' + (rawVerse || '') : null;

    if (!converted) {
      return { ...r, noEquivalent: true, text: null, approximate: approxRange, sourceLabel };
    }

    const startConv = convertVerse(idx, dsChapter, r.targetVerseStart, ds, display);
    const endConv = r.targetVerseEnd === r.targetVerseStart
      ? startConv
      : convertVerse(idx, dsChapter, r.targetVerseEnd, ds, display);
    const targetVerseStart = startConv ? startConv.verse : r.targetVerseStart;
    const targetVerseEnd = endConv ? endConv.verse : r.targetVerseEnd;

    const text = ds === display
      ? r.text
      : (isChapterLevel ? null : fetchVerseText(tid, r.book, converted.chapter, converted.verse, converted.verse));

    return {
      ...r,
      chapter: converted.chapter,
      verse: converted.verse,
      targetVerseStart,
      targetVerseEnd,
      approximate: converted.approximate,
      sourceLabel,
      noEquivalent: false,
      text
    };
  }

  // Trims whitespace only \u2014 the original-language text itself (accents,
  // breathing marks, Hebrew niqqud) is left exactly as pasted, since
  // those distinguish one lexeme from another rather than being noise
  // to normalize away.
  function normalizeLexicalQuery(q) {
    return String(q == null ? '' : q).trim();
  }

  function looksLikeStrong(q) {
    return STRONG_RE.test(q.toUpperCase());
  }

  // Escapes SQLite LIKE wildcards (% and _) in user-typed text before it
  // goes into a LIKE pattern, so pasting something like "de_us" searches
  // for that literal underscore instead of matching any single character.
  function likeSubstring(q) {
    return '%' + q.replace(/[\\%_]/g, '\\$&') + '%';
  }

  // Shapes one row from lexemeExact/strongsByLiteralText into the
  // candidate form returned to callers: totalOccurrences falls back to
  // the row's own matchCount (how many times *this* literal spelling
  // matched, from strongsByLiteralText) when occurrencesReady is false
  // or the aggregate is otherwise unavailable, so candidates still sort
  // sensibly without the occurrence-index tables.
  function toCandidate(r) {
    return {
      strong: r.strong,
      lexeme: r.lexeme || null,
      translit: r.translit || null,
      shortGloss: r.shortGloss || null,
      totalOccurrences: r.totalOccurrences != null ? r.totalOccurrences : (r.matchCount || 0)
    };
  }

  // The shared resolution behind both searchLexicon and
  // getWordOccurrencesByQuery, so a pasted word always resolves to the
  // same Strong's number (or the same ambiguity) in either sidebar:
  //   1. A Strong's number (e.g. "G26", lowercase/whitespace tolerated)
  //      resolves directly, same as the strong-only lookups always have.
  //   2. Otherwise, an exact match against a dictionary lexeme (the
  //      lemma spelling lexicon_words.lexeme carries) \u2014 what a word
  //      *without* its own Strong's number falls back to (see
  //      db-queries.js module comment / the client's popover), and also
  //      what pasting a lemma copied from a lexicon entry hits.
  //   3. Otherwise, the literal interlinear spelling (words.original)
  //      is searched for that exact text, surfacing whichever Strong's
  //      number(s) it maps to elsewhere in the text \u2014 covers pasting an
  //      inflected form copied straight out of the reading pane, and is
  //      also exactly how a click on a strongless word resolves (its own
  //      words.original text stands in for a lexeme it doesn't have).
  //   4. Otherwise, a substring search across the lexicon's lexeme,
  //      transliteration, and short_gloss columns \u2014 this is what makes
  //      "search by lexeme" actually usable for browsing rather than
  //      requiring an exact spelling: type part of a word, a
  //      transliteration, or a remembered English gloss ("love", "agap")
  //      and get a pick-list back, the same candidate-card UI already
  //      used for homograph disambiguation in steps 2\u20133. Always returned
  //      as candidates (never auto-resolved) since a substring match
  //      inherently can't claim to be the one exact answer the way an
  //      exact lexeme/text match can.
  // Returns { query, resolved, candidates }: resolved is a single
  // Strong's number when the query was unambiguous, null otherwise (with
  // candidates listing 0 or more possibilities, most-attested first).
  function resolveLexicalQuery(rawQuery) {
    const q = normalizeLexicalQuery(rawQuery);
    if (!q) throw httpError('A word, lexeme, or Strong\u2019s number is required', 400);
    if (looksLikeStrong(q)) return { query: q, resolved: q.toUpperCase(), candidates: [] };
    if (!lexiconReady) throw httpError('Lexicon data is not available', 503);

    const lexemeRows = stmts.lexemeExact.all(q);
    if (lexemeRows.length === 1) return { query: q, resolved: lexemeRows[0].strong, candidates: [] };
    if (lexemeRows.length > 1) return { query: q, resolved: null, candidates: lexemeRows.map(toCandidate) };

    const textRows = stmts.strongsByLiteralText.all(q);
    if (textRows.length === 1) return { query: q, resolved: textRows[0].strong, candidates: [] };
    if (textRows.length > 1) return { query: q, resolved: null, candidates: textRows.map(toCandidate) };

    if (q.length >= 2) {
      const pattern = likeSubstring(q);
      const fuzzyRows = stmts.lexiconFuzzySearch.all(pattern, pattern, pattern);
      if (fuzzyRows.length) return { query: q, resolved: null, candidates: fuzzyRows.map(toCandidate) };
    }

    return { query: q, resolved: null, candidates: [] };
  }

  // Built as a named local (rather than an inline object literal returned
  // directly) purely so searchLexicon/getWordOccurrencesByQuery below can
  // call back into getLexiconEntry/getWordOccurrences via `queries.` —
  // safe regardless of whether a caller invokes the returned object's
  // methods directly or destructures them first (`this` wouldn't survive
  // the latter).
  const queries = {
    searchReady,
    lexiconReady,
    occurrencesReady,
    familyReady,
    versificationReady,
    xrefsReady,

    listTranslations() {
      return { translations: stmts.listTranslations.all(), searchReady };
    },

    getBookWords(translationId, bookKey) {
      const tid = requireInt(translationId, 'translationId');
      return stmts.bookWords.all(tid, String(bookKey).toLowerCase());
    },

    // Same rows as getBookWords, but only for chapters fromChapter..toChapter
    // (inclusive). What the reading view actually asks for: a verse or a
    // chapter needs a few hundred words, not every word of Psalms.
    getChapterWords(translationId, bookKey, fromChapter, toChapter) {
      const tid = requireInt(translationId, 'translationId');
      const from = requireInt(fromChapter, 'fromChapter');
      const to = requireInt(toChapter, 'toChapter');
      if (from < 1 || to < from) throw httpError('Invalid chapter range', 400);
      return stmts.chapterWords.all(tid, String(bookKey).toLowerCase(), from, to);
    },

    getAvailableBooks(translationId) {
      const tid = requireInt(translationId, 'translationId');
      return stmts.availableBooks.all(tid).map(r => r.name.toLowerCase());
    },

    // Cheap counterpart to getBookWords for callers that only need to
    // know what chapters/verses exist, not the text itself (the browse
    // popover's chapter grid and verse grid). Returns a flat array of
    // { chapter, verse } pairs, one per verse that actually exists, in
    // canonical order.
    getBookStructure(translationId, bookKey) {
      const tid = requireInt(translationId, 'translationId');
      return stmts.bookStructure.all(tid, String(bookKey).toLowerCase());
    },

    // bookFilters is an optional array of book names (any case; matched
    // case-insensitively against verses_fts.book_name) pulled from
    // book: filters in the query — see extractBookFilters/parseSearchQuery
    // in index.html. Omit it, or pass an empty array, to search every
    // book. Several names are OR'd together (search in Genesis OR Exodus),
    // matching how book: filters are documented in the search box's help
    // tooltip.
    search(matchQuery, translationId, bookFilters) {
      if (!searchReady) throw httpError('Search index is not available', 503);
      if (!matchQuery || typeof matchQuery !== 'string') throw httpError('q is required', 400);
      const tid = requireInt(translationId, 'translationId');
      const books = Array.isArray(bookFilters)
        ? bookFilters.filter(Boolean).map(b => String(b).toLowerCase())
        : [];
      const stmt = getSearchStmt(books.length);
      try {
        return stmt.all(matchQuery, ...books, tid);
      } catch (err) {
        // A verses_fts built before the orig/orig_x columns existed fails
        // with "no such column" — that's a stale index, not a bad query.
        if (/no such column/i.test(err.message)) {
          throw httpError('Search index is out of date \u2014 rerun build_search_index.py', 503);
        }
        // Malformed FTS5 MATCH syntax lands here as a normal SQLite error.
        err.statusCode = 400;
        throw err;
      }
    },

    getLexiconEntry(strong) {
      if (!lexiconReady) throw httpError('Lexicon data is not available', 503);
      const s = String(strong).toUpperCase();
      if (!STRONG_RE.test(s)) throw httpError('Malformed Strong\u2019s number', 400);
      const rows = stmts.lexiconEntry.all(s);
      if (!rows.length) throw httpError('No lexicon entry for ' + s, 404);
      const { strong: normalized, lexeme, translit, pronunciation, shortGloss } = rows[0];
      const entries = rows
        .filter(r => r.entryHtml)
        .map(r => ({ 
          code: r.lexiconCode, 
          name: r.lexiconName, 
          copyright: r.lexiconCopyright, 
          html: r.entryHtml 
        }));
      return { lemma: { strong: normalized, lexeme, translit, pronunciation, shortGloss }, entries };
    },

    // Unified entry point for the sidebar's lexicon search box (see
    // resolveLexicalQuery above for the three-step resolution a pasted
    // word goes through) and for looking up a word that has no Strong's
    // number of its own \u2014 the client passes that word's own literal text
    // in place of a Strong's number, which step 3 of resolveLexicalQuery
    // is exactly built to handle. Returns one of:
    //   { query, mode: 'entry', entry: {lemma, entries}, candidates: [] }
    //   { query, mode: 'candidates', entry: null, candidates: [...] }
    //   { query, mode: 'notFound', entry: null, candidates: [] }
    searchLexicon(rawQuery) {
      if (!lexiconReady) throw httpError('Lexicon data is not available', 503);
      const resolution = resolveLexicalQuery(rawQuery);
      if (resolution.resolved) {
        try {
          return { query: resolution.query, mode: 'entry', entry: queries.getLexiconEntry(resolution.resolved), candidates: [] };
        } catch (err) {
          if (err.statusCode === 404) return { query: resolution.query, mode: 'notFound', entry: null, candidates: [] };
          throw err;
        }
      }
      return {
        query: resolution.query,
        mode: resolution.candidates.length ? 'candidates' : 'notFound',
        entry: null,
        candidates: resolution.candidates
      };
    },

    // Occurrences counterpart of searchLexicon \u2014 same resolution, so the
    // Word Study and Occurrences tabs always agree on what a pasted word
    // or a strongless word's text resolves to. Returns one of:
    //   { query, mode: 'entry', data: {...getWordOccurrences result}, candidates: [] }
    //   { query, mode: 'candidates', data: null, candidates: [...] }
    //   { query, mode: 'notFound', data: null, candidates: [] }
    getWordOccurrencesByQuery(rawQuery, translationId) {
      if (!occurrencesReady) throw httpError('Occurrence index is not available', 503);
      const resolution = resolveLexicalQuery(rawQuery);
      if (resolution.resolved) {
        return { query: resolution.query, mode: 'entry', data: queries.getWordOccurrences(resolution.resolved, translationId), candidates: [] };
      }
      return {
        query: resolution.query,
        mode: resolution.candidates.length ? 'candidates' : 'notFound',
        data: null,
        candidates: resolution.candidates
      };
    },

    // Every occurrence location for a Strong's number, plus per-translation
    // gloss frequency — powers the "All occurrences" word-study sidebar.
    // Returns { strong, totalOccurrences, occurrences, glosses, family }, where
    // each occurrence now also carries a `glosses` array: the exact
    // gloss(es) used for this word at that specific verse in the
    // requested translation (see wordOccurrences above), empty if this
    // translation has no aligned word there.
    getWordOccurrences(strong, translationId) {
      if (!occurrencesReady) throw httpError('Occurrence index is not available', 503);
      const s = String(strong).toUpperCase();
      if (!STRONG_RE.test(s)) throw httpError('Malformed Strong\u2019s number', 400);
      const tid = requireInt(translationId, 'translationId');

      const occurrences = stmts.wordOccurrences.all(tid, tid, s).map(o => {
        const { glossesAtOccurrence, ...rest } = o;
        return {
          ...rest,
          glosses: glossesAtOccurrence
            ? [...new Set(glossesAtOccurrence.split(GLOSS_SEP).map(stripGlossBrackets).filter(Boolean))]
            : []
        };
      });
      const glosses = stmts.wordGlossStats.all(s);
      const totalOccurrences = occurrences.reduce((sum, o) => sum + o.occurrenceCount, 0);
      // Additive field: null when the family tables were never built (or
      // this word has no relatives), so older clients ignore it and the
      // Occurrences sidebar shows no "Include related words" panel.
      // members are { strong, lexeme, tier, support, kind, sources, total,
      // pos }; the client fetches each member's own occurrences through
      // this same function when the person opts in.
      let family = null;
      if (familyReady) {
        const members = stmts.wordFamily.all(s);
        if (members.length) {
          const selfPosRow = stmts.wordSelfPos ? stmts.wordSelfPos.get(s) : null;
          family = { selfPos: selfPosRow ? selfPosRow.pos : null, members };
        }
      }
      return { strong: s, totalOccurrences, occurrences, glosses, family };
    },

    // Every recorded versification edge for one book, as { fromTradition,
    // fromChapter, fromVerse, toTradition, toRef, source }. fromTradition
    // is almost always 'MT' (the pivot — see PIVOT_VERSIFICATION above);
    // a 'source':'manual' row may instead run directly between two
    // non-pivot traditions (e.g. LXX -> VUL) where no pivot address
    // reconciles them at all. toRef is a verbatim string from TVTMS (or
    // a manual file) — most often a plain "6:1b"/"5:32-6:1" form,
    // occasionally free text or '--' for a handful of badly-scrambled or
    // unmatched passages that the client falls back to "not available"
    // for rather than guessing. bookKey is the same lowercase book name
    // used elsewhere. (Was getVersificationDiffs before the table became
    // an edge list — renamed to match; db-client.js/index.html need the
    // matching rename.)
    getVersificationLinks(bookKey) {
      if (!versificationReady) throw httpError('Versification data is not available', 503);
      return stmts.versificationLinks.all(String(bookKey));
    },

    // Cross-references touching a verse range [fromVerse, toVerse] of one
    // book/chapter, IN THE PANE'S OWN NUMBERING (translationId's
    // versification) — pass fromVerse === toVerse for a single verse
    // (the Xrefs sidebar's filtered view), or a wider span for its
    // whole-open-range aggregate view. Returns { outbound, inbound },
    // both already converted into that numbering and re-sorted by votes
    // across datasets — see crossReferencesOutbound/crossReferencesInbound
    // above for what each side means and carries, and
    // convertOutboundRow/convertInboundRow for the conversion itself.
    //
    // WHY THIS QUERIES ONCE PER DATASET: cross_references can hold rows
    // in more than one numbering tradition at once (build_xrefs.py's
    // --versification), and unlike a translation, a single query can't
    // "be" more than one versification. So for each distinct
    // cross_references.versification present (xrefVersificationsCache,
    // computed once at startup), the queried range is converted into
    // that dataset's own numbering first, the dataset is queried in its
    // own numbering, and every result is converted back before being
    // added to the combined list. A dataset that shares the pane's own
    // versification round-trips through identity conversion and costs
    // nothing extra beyond the query itself.
    //
    // KNOWN LIMITATION: only the queried range's two ENDPOINTS are
    // converted (see approxRange below) — not every verse in between —
    // so a wide open-range query (a whole chapter) that straddles a
    // scrambled region (Daniel 3's insertion, a Sirach transposition)
    // queries a verse span in the dataset's numbering that may not
    // precisely correspond verse-for-verse to the pane's own range
    // throughout. Rows are marked `approximate` in that case, which the
    // client already surfaces via the `.xref-altref.approximate` style
    // used in compare mode.
    getCrossReferences(translationId, bookKey, chapter, fromVerse, toVerse) {
      if (!xrefsReady) throw httpError('Cross-reference data is not available', 503);
      const ch = requireInt(chapter, 'chapter');
      const fv = requireInt(fromVerse, 'fromVerse');
      const tv = requireInt(toVerse, 'toVerse');
      const bk = String(bookKey).toLowerCase();
      const tid = requireInt(translationId, 'translationId');

      const display = translationVersification(tid);
      const idx = buildVersificationIndex(versificationReady ? queries.getVersificationLinks(bk) : []);

      const outbound = [];
      const inbound = [];

      xrefVersificationsCache.forEach((ds) => {
        const fromConv = convertVerse(idx, ch, fv, display, ds);
        const toConv = convertVerse(idx, ch, tv, display, ds);
        // Either endpoint has no equivalent at all in this dataset's
        // tradition (querying Esther's additions from an MT pane, say)
        // — nothing in ds's own numbering to bound the query with, so
        // this dataset contributes nothing for this particular range.
        if (!fromConv || !toConv) return;

        const dsChapter = fromConv.chapter;
        const sameChapter = fromConv.chapter === toConv.chapter;
        const loVerse = Math.min(fromConv.verse, toConv.verse);
        let hiVerse = sameChapter ? Math.max(fromConv.verse, toConv.verse) : loVerse;
        // A queried verse that maps onto a short span in the dataset's own
        // numbering (a Psalm superscription split — see parseVersificationRef)
        // needs the WHOLE span queried, not just its first verse, or xrefs
        // attached to the span's later verse(s) are silently missed.
        if (sameChapter) {
          if (fromConv.verseEnd != null) hiVerse = Math.max(hiVerse, fromConv.verseEnd);
          if (toConv.verseEnd != null) hiVerse = Math.max(hiVerse, toConv.verseEnd);
        }
        // The query range approximates the pane's own range whenever
        // either endpoint's conversion itself was approximate (crossed a
        // range/scramble in the versification data) or the two endpoints
        // landed in different converted chapters (see the KNOWN
        // LIMITATION note above) — either way, every row this dataset
        // turns up inherits that uncertainty.
        const approxRange = !!(fromConv.approximate || toConv.approximate || !sameChapter);

        const outRows = verseTextReady
          ? stmts.crossReferencesOutbound.all(tid, ds, bk, dsChapter, loVerse, hiVerse)
          : stmts.crossReferencesOutbound.all(ds, bk, dsChapter, loVerse, hiVerse);
        const inRows = verseTextReady
          ? stmts.crossReferencesInbound.all(tid, ds, bk, dsChapter, hiVerse, loVerse)
          : stmts.crossReferencesInbound.all(ds, bk, dsChapter, hiVerse, loVerse);

        outRows.forEach(r => outbound.push(convertOutboundRow(r, ds, display, idx, tid, approxRange)));
        inRows.forEach(r => inbound.push(convertInboundRow(r, ds, display, idx, tid, approxRange, dsChapter)));
      });

      outbound.sort((a, b) => b.votes - a.votes);
      inbound.sort((a, b) => b.votes - a.votes);

      return { outbound, inbound };
    }
  };

  return queries;
}

module.exports = {
  createQueries,
  STRONG_RE,
  // Pure versification helpers, exported for unit testing (and in case
  // main.js/server.js ever need to convert a reference outside the
  // context of a getCrossReferences call). index.html carries its own
  // copy for compare mode's client-side resolution — see the block
  // comment above PIVOT_VERSIFICATION.
  PIVOT_VERSIFICATION,
  parseVersificationTag,
  parseVersificationRef,
  buildVersificationIndex,
  convertVerse,
  convertChapter
};
