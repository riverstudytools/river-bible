const express = require('express');
const Database = require('better-sqlite3');
const path = require('path');
const { createQueries } = require('./db-queries');

const app = express();
const PORT = process.env.PORT || 3000;

// Open local bible.sqlite directly with a busy timeout
const dbPath = path.join(__dirname, 'bible.sqlite');
const db = new Database(dbPath, { timeout: 5000, readonly: true });

const queries = createQueries(db);

app.use(express.json());
app.use(express.static(__dirname));

/* ================================================================
   Public API — named, parameterized endpoints only.

   All actual SQL lives in db-queries.js, shared with main.js (Electron).
   This file's only job is translating HTTP requests into calls against
   that module and thrown errors into HTTP status codes — River's website
   deployment is public and multi-user, so the client must never be able
   to hand this server a raw SQL string.
   ================================================================ */

function sendError(res, err) {
  res.status(err.statusCode || 500).json({ ok: false, error: err.message });
}

// GET /api/translations
// Returns every translation plus whether full-text search is available.
app.get('/api/translations', (req, res) => {
  try {
    const { translations, searchReady } = queries.listTranslations();
    res.json({ ok: true, translations, searchReady });
  } catch (err) {
    sendError(res, err);
  }
});

// GET /api/books/:translationId/:bookKey
// Every word, for every verse, of one book in one translation.
app.get('/api/books/:translationId/:bookKey', (req, res) => {
  try {
    const rows = queries.getBookWords(req.params.translationId, req.params.bookKey);
    res.json({ ok: true, rows });
  } catch (err) {
    sendError(res, err);
  }
});

// GET /api/books/:translationId
// Every book (lowercase name) that has verses in a translation, in canonical order.
app.get('/api/books/:translationId', (req, res) => {
  try {
    const books = queries.getAvailableBooks(req.params.translationId);
    res.json({ ok: true, books });
  } catch (err) {
    sendError(res, err);
  }
});

// GET /api/books/:translationId/:bookKey/structure
// Every (chapter, verse) that exists for one book in one translation —
// the lightweight, text-free counterpart to getBookWords above. Backs
// the browse-by-book popover's chapter/verse grids.
app.get('/api/books/:translationId/:bookKey/structure', (req, res) => {
  try {
    const rows = queries.getBookStructure(req.params.translationId, req.params.bookKey);
    res.json({ ok: true, rows });
  } catch (err) {
    sendError(res, err);
  }
});

// GET /api/search?q=<fts5 match query>&translationId=<id>&books=<comma-separated book names>
// q is expected to already be a well-formed FTS5 MATCH expression built
// client-side by renderSearchPlan() (see parseSearchQuery() in
// index.html) — it's passed as a bound parameter, never interpolated
// into SQL, so free-text input here is safe. books is optional — a
// comma-separated list of book names pulled from any book: filters in
// the query (book names never contain commas, so a plain split is safe)
// — omitted entirely, it searches every book.
app.get('/api/search', (req, res) => {
  try {
    const books = typeof req.query.books === 'string'
      ? req.query.books.split(',').map(b => b.trim()).filter(Boolean)
      : [];
    const rows = queries.search(req.query.q, req.query.translationId, books);
    res.json({ ok: true, rows });
  } catch (err) {
    sendError(res, err);
  }
});

// GET /api/lexicon/:strong
// One lemma's dictionary data: lemma-level info plus every source's
// entry_html (Thayer, BDB, Dodson, LSJ, etc.) linked to it.
app.get('/api/lexicon/:strong', (req, res) => {
  try {
    const { lemma, entries } = queries.getLexiconEntry(req.params.strong);
    res.json({ ok: true, lemma, entries });
  } catch (err) {
    sendError(res, err);
  }
});

// GET /api/lexicon/:strong/occurrences
app.get('/api/lexicon/:strong/occurrences', (req, res) => {
  try {
    const { strong, totalOccurrences, occurrences, glosses } = queries.getWordOccurrences(req.params.strong, req.query.translationId);
    res.json({ ok: true, strong, totalOccurrences, occurrences, glosses });
  } catch (err) {
    sendError(res, err);
  }
});

// GET /api/versification/:bookKey
// Every recorded MT/LXX numbering difference in one book.
app.get('/api/versification/:bookKey', (req, res) => {
  try {
    const rows = queries.getVersificationDiffs(req.params.bookKey);
    res.json({ ok: true, rows });
  } catch (err) {
    sendError(res, err);
  }
});

// GET /api/lexicon-search/:query
app.get('/api/lexicon-search/:query', (req, res) => {
  try {
    const result = queries.searchLexicon(req.params.query);
    res.json({ ok: true, ...result });
  } catch (err) {
    sendError(res, err);
  }
});

// GET /api/lexicon-search/occurrences/:query
app.get('/api/lexicon-search/occurrences/:query', (req, res) => {
  try {
    const result = queries.getWordOccurrencesByQuery(req.params.query, req.query.translationId);
    res.json({ ok: true, ...result });
  } catch (err) {
    sendError(res, err);
  }
});

app.listen(PORT, () => {
  console.log(`Server running at http://localhost:${PORT}`);
  console.log(`Search index: ${queries.searchReady ? 'ready' : 'NOT FOUND (verses_fts table missing)'}`);
  console.log(`Lexicon data: ${queries.lexiconReady ? 'ready' : 'NOT FOUND (lexicon_words/entries/sources missing)'}`);
  console.log(`Versification diffs: ${queries.versificationReady ? 'ready' : 'NOT FOUND (versification_diffs missing)'}`);
});
