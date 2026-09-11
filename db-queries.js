// db-queries.js
//
// All of River's actual SQL lives here, in one place, shared by both
// transports: server.js (website, over HTTP) and main.js (Electron, over
// IPC). Neither transport should ever build its own SQL — they just call
// these functions and translate the result (or a thrown error) into
// whatever their own protocol needs.
//
// createQueries(db) takes an already-open better-sqlite3 Database and
// returns { searchReady, lexiconReady, listTranslations, getBookWords,
// getAvailableBooks, getBookStructure, search, getLexiconEntry,
// searchLexicon, getWordOccurrences, getWordOccurrencesByQuery,
// getVersificationDiffs }.
// Errors thrown from these functions carry a `statusCode` — server.js
// maps that straight onto the HTTP response; main.js's IPC handlers can
// ignore it (Electron just rejects the renderer's ipcRenderer.invoke()
// promise with the error's message either way).

function httpError(message, statusCode) {
  const err = new Error(message);
  err.statusCode = statusCode;
  return err;
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
  // 'MT' (Masoretic-tradition, English/KJV-standard chapter:verse — KJV,
  // ESV, NASB, etc.) or 'LXX' (Septuagint-tradition, Greek chapter:verse
  // — Brenton, NETS, etc.). NULL/absent for anything else (NT-only
  // translations, or a bible.sqlite built before this column existed) —
  // the client treats a missing value as 'MT', since that's what the
  // overwhelming majority of translations are.
  const hasVersificationCol = translationCols.includes('versification');

  // Optional per-translation "About this translation" HTML — shown on
  // the new-tab page for whichever translation the new tab defaults to.
  // Same graceful-degradation pattern as the columns above: a
  // bible.sqlite built before this existed just won't have anything to
  // show there, rather than breaking startup.
  const hasInfoHtmlCol = translationCols.includes('info_html');

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

  // Built by build_versification_index.py from STEPBible's TVTMS data
  // (CC BY 4.0) — powers the compare view's "Align verse numbers"
  // option for lining up MT- and LXX-tradition translations whose
  // chapter:verse numbering diverges. Optional like the tables above: a
  // bible.sqlite built before this existed just won't offer the
  // feature.
  const versificationReady = db.prepare(
    "SELECT count(*) AS n FROM sqlite_master WHERE type = 'table' " +
    "AND name = 'versification_diffs'"
  ).get().n === 1;

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

  const stmts = {
    listTranslations: db.prepare(
      'SELECT id, code, name, language' +
      (hasCopyrightCols ? ', copyright_short, license_text' : '') +
      (hasVersificationCol ? ', versification' : '') +
      (hasInfoHtmlCol ? ', info_html' : '') +
      ' FROM translations ORDER BY id'
    ),

    bookWords: db.prepare(
      'SELECT verses.chapter AS chapter, verses.verse AS verse, books.name AS book, ' +
      '       words.lang AS lang, words.original AS original, words.translit AS translit, ' +
      '       words.parsing AS parsing, ' + NORMALIZED_WORD_STRONG + ' AS strong, words.gloss AS gloss, ' +
      '       words.beg_q AS begQ, words.pnc AS pnc, words.end_q AS endQ, words.end_text AS endText, ' +
      '       words.footnote AS footnote, words.hdg AS hdg, words.crossref AS crossref, ' +
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
      'WHERE verses.translation_id = ? AND lower(books.name) = ? ' +
      'ORDER BY verses.chapter, verses.verse, words.position'
    ),

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
      'JOIN books ON books.id = verses.book_id ' +
      'WHERE verses.translation_id = ? AND lower(books.name) = ? ' +
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
      'SELECT o.book_name AS book, o.testament, o.chapter, o.verse, o.occurrence_count AS occurrenceCount, ' +
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

    // Every recorded MT/LXX numbering difference in one book. Small
    // (a few dozen to a couple thousand rows per book) — the client
    // loads a whole book's worth at once and resolves individual verses
    // against it locally, the same lazy-per-book-then-cache pattern
    // db-client.js already uses for getBookWords.
    versificationDiffs: versificationReady ? db.prepare(
      'SELECT std_chapter AS stdChapter, std_verse AS stdVerse, lxx_ref AS lxxRef ' +
      'FROM versification_diffs WHERE book = ? ORDER BY std_chapter, std_verse'
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
      '  SELECT book_id, book_name, chapter, verse, testament, translation_id, text, bm25(verses_fts) AS rnk' +
      '  FROM verses_fts WHERE verses_fts MATCH ?' + bookClause +
      '), grouped AS (' +
      '  SELECT book_id, book_name, chapter, verse, testament, MIN(rnk) AS best_rnk' +
      '  FROM hits GROUP BY book_id, chapter, verse' +
      ') ' +
      'SELECT g.book_name AS book, g.chapter AS chapter, g.verse AS verse, g.testament AS testament, ' +
      '       g.best_rnk AS rank, h.text AS hitText, tr.code AS hitTranslationCode, ' +
      '       vt.text AS activeText ' +
      'FROM grouped g ' +
      'JOIN hits h ON h.book_id = g.book_id AND h.chapter = g.chapter AND h.verse = g.verse AND h.rnk = g.best_rnk ' +
      'LEFT JOIN translations tr ON tr.id = h.translation_id ' +
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
    versificationReady,

    listTranslations() {
      return { translations: stmts.listTranslations.all(), searchReady };
    },

    getBookWords(translationId, bookKey) {
      const tid = requireInt(translationId, 'translationId');
      return stmts.bookWords.all(tid, String(bookKey).toLowerCase());
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
    // Returns { strong, totalOccurrences, occurrences, glosses }, where
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
      return { strong: s, totalOccurrences, occurrences, glosses };
    },

    // Every MT/LXX verse-numbering difference recorded for one book, as
    // { stdChapter, stdVerse, lxxRef }. stdChapter/stdVerse is the
    // English/KJV-standard reference (what River's MT-tradition
    // translations use); lxxRef is a verbatim string from TVTMS — most
    // often a plain "6:1b"/"5:32-6:1" form, occasionally free text for a
    // handful of badly-scrambled passages (Exo.28, parts of Jer/1Ki)
    // that the client falls back to "not available" for rather than
    // guessing. bookKey is the same lowercase book name used elsewhere.
    getVersificationDiffs(bookKey) {
      if (!versificationReady) throw httpError('Versification data is not available', 503);
      return stmts.versificationDiffs.all(String(bookKey));
    }
  };

  return queries;
}

module.exports = { createQueries, STRONG_RE };
