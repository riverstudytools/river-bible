const express = require('express');
const Database = require('better-sqlite3');
const path = require('path');
const fs = require('fs');
const zlib = require('zlib');
const crypto = require('crypto');
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
   Response cache for the heavy, read-only endpoints (chapter-range
   words, whole-book words, and book structure).

   Why: better-sqlite3 is synchronous, so while one of these queries
   runs, the whole server — every other visitor included — waits. A big
   chunk of text (Psalms, Isaiah) is also a multi-megabyte JSON payload. The
   database is opened readonly and never changes while the server is
   running, so each answer only needs to be computed once. Each entry
   is stored already gzipped, which also means we never re-compress it,
   and carries an ETag so a returning visitor's browser can revalidate
   with a tiny 304 instead of re-downloading the book.

   Restart the server after replacing bible.sqlite (you'd have to
   anyway — the open handle points at the old file).
   ================================================================ */
const DB_VERSION = fs.statSync(dbPath).mtimeMs.toString(36);
const CACHE_MAX_BYTES = 200 * 1024 * 1024; // gzipped bytes kept in memory
const responseCache = new Map();            // insertion order = LRU order
let responseCacheBytes = 0;

function sendCached(req, res, key, build) {
  let entry = responseCache.get(key);
  if (entry) {
    responseCache.delete(key);              // bump to most-recently-used
    responseCache.set(key, entry);
  } else {
    const payload = build();                // may throw — errors are never cached
    const gz = zlib.gzipSync(Buffer.from(JSON.stringify(payload)), { level: 6 });
    entry = {
      gz,
      etag: '"' + DB_VERSION + '-' + crypto.createHash('md5').update(gz).digest('hex').slice(0, 16) + '"'
    };
    // Only cache real content, so nonsense URLs can't fill the cache.
    if (payload.rows && payload.rows.length) {
      responseCache.set(key, entry);
      responseCacheBytes += gz.length;
      for (const [oldKey, old] of responseCache) {
        if (responseCacheBytes <= CACHE_MAX_BYTES) break;
        responseCache.delete(oldKey);
        responseCacheBytes -= old.gz.length;
      }
    }
  }
  res.set({
    'ETag': entry.etag,
    'Cache-Control': 'public, max-age=0, must-revalidate',
    'Vary': 'Accept-Encoding',
    'Content-Type': 'application/json; charset=utf-8'
  });
  if (req.headers['if-none-match'] === entry.etag) return res.status(304).end();
  if (/\bgzip\b/.test(req.headers['accept-encoding'] || '')) {
    res.set('Content-Encoding', 'gzip');
    return res.end(entry.gz);
  }
  return res.end(zlib.gunzipSync(entry.gz));
}

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
    sendCached(req, res, 'words:' + req.params.translationId + ':' + req.params.bookKey.toLowerCase(),
      () => ({ ok: true, rows: queries.getBookWords(req.params.translationId, req.params.bookKey) }));
  } catch (err) {
    sendError(res, err);
  }
});

// GET /api/books/:translationId/:bookKey/chapters/:from/:to
// Same rows as the whole-book route above, but only for chapters
// from..to (inclusive) — what the reading view asks for, since showing
// one verse or one chapter shouldn't mean sending every word of the book.
// The response echoes the range so a client can tell exactly which
// chapters it now has.
app.get('/api/books/:translationId/:bookKey/chapters/:from/:to', (req, res) => {
  try {
    const from = parseInt(req.params.from, 10);
    const to = parseInt(req.params.to, 10);
    if (!(from >= 1) || !(to >= from) || to > 999) {
      return res.status(400).json({ ok: false, error: 'Invalid chapter range' });
    }
    sendCached(req, res,
      'chapters:' + req.params.translationId + ':' + req.params.bookKey.toLowerCase() + ':' + from + '-' + to,
      () => ({ ok: true, rows: queries.getChapterWords(req.params.translationId, req.params.bookKey, from, to), fromChapter: from, toChapter: to }));
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
    sendCached(req, res, 'structure:' + req.params.translationId + ':' + req.params.bookKey.toLowerCase(),
      () => ({ ok: true, rows: queries.getBookStructure(req.params.translationId, req.params.bookKey) }));
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
// Every recorded versification edge in one book (MT/LXX/VUL, plus any
// hand-curated direct edges between two non-pivot traditions). The
// route path is unchanged from when this returned diffs-against-a-
// single-pivot rows; only the row shape and the queries.js method name
// changed (getVersificationDiffs -> getVersificationLinks).
app.get('/api/versification/:bookKey', (req, res) => {
  try {
    const rows = queries.getVersificationLinks(req.params.bookKey);
    res.json({ ok: true, rows });
  } catch (err) {
    sendError(res, err);
  }
});

// GET /api/xrefs/:translationId/:bookKey/:chapter/:fromVerse/:toVerse
// Outbound + inbound cross-references touching verse range [fromVerse,
// toVerse] of one book/chapter — pass fromVerse === toVerse for a single
// verse. Readable text comes back in :translationId where available.
app.get('/api/xrefs/:translationId/:bookKey/:chapter/:fromVerse/:toVerse', (req, res) => {
  try {
    const { outbound, inbound } = queries.getCrossReferences(
      req.params.translationId, req.params.bookKey, req.params.chapter, req.params.fromVerse, req.params.toVerse
    );
    res.json({ ok: true, outbound, inbound });
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
  console.log(`Cross references: ${queries.xrefsReady ? 'ready' : 'NOT FOUND (cross_references missing)'}`);
});
