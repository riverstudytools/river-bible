// preload.js
//
// Runs in an isolated context with access to Node/Electron internals, but
// the renderer (index.html) never gets that access directly — only the
// specific, named functions below, via contextBridge. This mirrors
// server.js's HTTP surface exactly: no raw SQL, no direct database or
// IPC access, just the same fixed operation set db-client.js already
// expects on window.electronAPI.

const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('electronAPI', {
  listTranslations: () => ipcRenderer.invoke('db:listTranslations'),
  getBookWords: (translationId, bookKey) => ipcRenderer.invoke('db:getBookWords', translationId, bookKey),
  getAvailableBooks: (translationId) => ipcRenderer.invoke('db:getAvailableBooks', translationId),
  getBookStructure: (translationId, bookKey) => ipcRenderer.invoke('db:getBookStructure', translationId, bookKey),
  search: (matchQuery, translationId, bookFilters) => ipcRenderer.invoke('db:search', matchQuery, translationId, bookFilters),
  getLexiconEntry: (strong) => ipcRenderer.invoke('db:getLexiconEntry', strong),
  getWordOccurrences: (strong, translationId) => ipcRenderer.invoke('db:getWordOccurrences', strong, translationId),
  getVersificationDiffs: (bookKey) => ipcRenderer.invoke('db:getVersificationDiffs', bookKey),
  searchLexicon: (query) => ipcRenderer.invoke('db:searchLexicon', query),
  getWordOccurrencesByQuery: (query, translationId) => ipcRenderer.invoke('db:getWordOccurrencesByQuery', query, translationId),

  // ---- settings/session persistence ----
  // Both sides treat this as an opaque JSON blob — shape lives entirely
  // in index.html.
  loadSettings: () => ipcRenderer.invoke('settings:load'),
  saveSettings: (data) => ipcRenderer.invoke('settings:save', data),
  loadDefaultSettings: () => ipcRenderer.invoke('settings:loadDefaults'),

  // Opens a brand-new window pre-loaded with a tab's reference/
  // translation/compare state — see main.js's 'window:openTab' handler
  // and index.html's tab context menu / openTab URL-param handling.
  openTabWindow: (payload) => ipcRenderer.invoke('window:openTab', payload),

  // Relays native application menu clicks (see main.js's Menu template)
  // to the renderer — index.html listens for these instead of exposing
  // its own settings through the in-page header bar, which is hidden
  // under Electron. Returns an unsubscribe function, though index.html
  // only ever calls this once for the lifetime of the window.
  onMenuAction: (callback) => {
    const listener = (event, action, value) => callback(action, value);
    ipcRenderer.on('menu:action', listener);
    return () => ipcRenderer.removeListener('menu:action', listener);
  },

  // Hands a URL off to the OS's default browser instead of letting it
  // load inside this window — see index.html's document-level external
  // link click handler, and main.js's 'open-external' listener plus its
  // setWindowOpenHandler backstop.
  openExternal: (url) => ipcRenderer.send('open-external', url)
});
