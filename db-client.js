// db-client.js — Data Access Layer
//
// Every platform River runs on exposes the same four operations. Which
// transport answers them depends on where the app is running:
//   - Electron: window.electronAPI, talking to better-sqlite3 in the main
//     process over IPC. Trusted local single-user context, so the main
//     process is free to run whatever SQL it wants.
//   - Website / anything else: HTTP fetch() against server.js's named
//     endpoints.
//
// Deliberately NOT a raw dbQuery(sql, params)/dbRun(sql, params) pass-through:
// the website deployment is public and multi-user, so the client must never
// be able to hand the server an arbitrary SQL string. Every operation below
// is a fixed, named call — the server decides what SQL runs.

async function getJson(res) {
  let data;
  try {
    data = await res.json();
  } catch (err) {
    throw new Error('Server returned a non-JSON response (' + res.status + ')');
  }
  if (!res.ok || !data.ok) {
    throw new Error(data && data.error ? data.error : 'Request failed (' + res.status + ')');
  }
  return data;
}

// Every website-deployment call below goes through here rather than a bare
// fetch(). Plain fetch() has no timeout of its own — if the server stalls
// (a slow query, a dropped connection, anything short of an outright
// network error) the returned promise simply never settles, and every
// caller up the chain (ensureAvailableBooks, ensureBookStructure, the
// browse popover's "Loading…" states, etc.) hangs right along with it,
// with no error and no way to recover short of reloading the page. This
// wraps every request in an AbortController-based timeout so a stalled
// request fails — loudly, with a clear message — instead of hanging
// forever, giving callers a rejected promise they can actually catch and
// show a retry for.
const REQUEST_TIMEOUT_MS = 20000;
async function fetchJson(url) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
  let res;
  try {
    res = await fetch(url, { signal: controller.signal });
  } catch (err) {
    if (err.name === 'AbortError') {
      throw new Error('Request timed out after ' + (REQUEST_TIMEOUT_MS / 1000) + 's \u2014 the server may be slow or unreachable.');
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
  return await getJson(res);
}

// Every translation in bible.sqlite, plus whether full-text search is
// available. Returns { translations, searchReady }.
export async function listTranslations() {
  if (window.electronAPI) {
    return await window.electronAPI.listTranslations();
  }
  const data = await fetchJson('/api/translations');
  return { translations: data.translations, searchReady: data.searchReady };
}

// Every word, for every verse, of one book in one translation.
// bookKey is the lowercase book name. Returns an array of row objects.
export async function getBookWords(translationId, bookKey) {
  if (window.electronAPI) {
    return await window.electronAPI.getBookWords(translationId, bookKey);
  }
  const url = '/api/books/' + encodeURIComponent(translationId) + '/' + encodeURIComponent(bookKey);
  const data = await fetchJson(url);
  return data.rows;
}

// Every book (lowercase name) that has verses in a translation, in
// canonical order. Returns an array of lowercase book names.
export async function getAvailableBooks(translationId) {
  if (window.electronAPI) {
    return await window.electronAPI.getAvailableBooks(translationId);
  }
  const url = '/api/books/' + encodeURIComponent(translationId);
  const data = await fetchJson(url);
  return data.books;
}

// Every (chapter, verse) that exists for one book in one translation —
// the lightweight, text-free counterpart to getBookWords. Used wherever
// only chapter/verse *numbers* are needed (the browse-by-book popover's
// chapter and verse grids), so the browser isn't pulling every word of a
// book across the wire just to find out how many chapters it has.
// bookKey is the lowercase book name, same convention as getBookWords.
// Returns an array of { chapter, verse } objects.
export async function getBookStructure(translationId, bookKey) {
  if (window.electronAPI) {
    return await window.electronAPI.getBookStructure(translationId, bookKey);
  }
  const url = '/api/books/' + encodeURIComponent(translationId) + '/' + encodeURIComponent(bookKey) + '/structure';
  const data = await fetchJson(url);
  return data.rows;
}

// matchQuery is an already-built FTS5 MATCH expression (see
// parseSearchQuery()/renderSearchPlan() in index.html) — this just
// carries it, plus an optional book filter, to whichever backend is
// running. bookFilters is an array of lowercase book names pulled from
// any book: filters in the query (see extractBookFilters() in
// index.html) — omit it, or pass an empty array, to search every book.
// Returns an array of row objects.
export async function search(matchQuery, translationId, bookFilters) {
  if (window.electronAPI) {
    return await window.electronAPI.search(matchQuery, translationId, bookFilters || []);
  }
  const params = new URLSearchParams({ q: matchQuery, translationId: String(translationId) });
  if (bookFilters && bookFilters.length) {
    // Book keys never contain commas, so a simple comma-join is safe and
    // avoids repeated-key query-string handling on the server.
    params.set('books', bookFilters.join(','));
  }
  const data = await fetchJson('/api/search?' + params.toString());
  return data.rows;
}

// One lemma's dictionary data, looked up by Strong's number: lemma-level
// info (lexeme, transliteration, pronunciation, short gloss) plus every
// linked dictionary source's HTML entry (Thayer, BDB, Dodson, LSJ, etc.).
// Returns { lemma, entries }.
export async function getLexiconEntry(strong) {
  if (window.electronAPI) {
    return await window.electronAPI.getLexiconEntry(strong);
  }
  const data = await fetchJson('/api/lexicon/' + encodeURIComponent(strong));
  return { lemma: data.lemma, entries: data.entries };
}

// Every place a Strong's-numbered word occurs in the Bible, plus how
// frequently each translation renders it with each gloss. Backs the
// "All occurrences" word-study sidebar (see build_word_index.py).
// Returns { strong, totalOccurrences, occurrences, glosses }.
export async function getWordOccurrences(strong, translationId) {
  if (window.electronAPI) {
    return await window.electronAPI.getWordOccurrences(strong, translationId);
  }
  const params = new URLSearchParams({ translationId: String(translationId) });
  const data = await fetchJson('/api/lexicon/' + encodeURIComponent(strong) + '/occurrences?' + params.toString());
  return { strong: data.strong, totalOccurrences: data.totalOccurrences, occurrences: data.occurrences, glosses: data.glosses };
}

// Every MT/LXX verse-numbering difference recorded for one book (see
// build_versification_index.py). Backs the compare view's "Align verse
// numbers" option — bookKey is the lowercase book name, same convention
// as getBookWords/getAvailableBooks. Returns an array of
// { stdChapter, stdVerse, lxxRef }.
export async function getVersificationDiffs(bookKey) {
  if (window.electronAPI) {
    return await window.electronAPI.getVersificationDiffs(bookKey);
  }
  const data = await fetchJson('/api/versification/' + encodeURIComponent(bookKey));
  return data.rows;
}

// Search the lexicon by Strong's number, dictionary lexeme, or literal original text.
// Returns { query, mode, entry, candidates }
export async function searchLexicon(query) {
  if (window.electronAPI) {
    return await window.electronAPI.searchLexicon(query);
  }
  const data = await fetchJson('/api/lexicon-search/' + encodeURIComponent(query));
  return { query: data.query, mode: data.mode, entry: data.entry, candidates: data.candidates };
}

// Search word occurrences by Strong's number, dictionary lexeme, or literal original text.
// Returns { query, mode, data, candidates }
export async function getWordOccurrencesByQuery(query, translationId) {
  if (window.electronAPI) {
    return await window.electronAPI.getWordOccurrencesByQuery(query, translationId);
  }
  const params = new URLSearchParams({ translationId: String(translationId) });
  const data = await fetchJson('/api/lexicon-search/occurrences/' + encodeURIComponent(query) + '?' + params.toString());
  return { query: data.query, mode: data.mode, data: data.data, candidates: data.candidates };
}