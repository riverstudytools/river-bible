// main.js — Electron main process
//
// The Electron path never goes over HTTP: the renderer calls the methods
// preload.js exposed on window.electronAPI, those go over IPC to the
// handlers below, and the handlers call straight into db-queries.js — the
// same query module server.js uses for the website. No SQL is duplicated
// between the two transports, and no SQL string ever crosses the IPC
// boundary in either direction.

const { app, BrowserWindow, ipcMain, Menu, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const Database = require('better-sqlite3');
const { createQueries } = require('./db-queries');
const updater = require('./updater');
const { installDesktopIntegration } = require('./desktop-integration');

// AppImage FUSE mounts are nosuid, so chrome-sandbox can never be mode
// 4755 root. Chromium then aborts if that helper is present but
// misconfigured. Disable the setuid sandbox before ready; the renderer
// still uses BrowserWindow webPreferences.sandbox.
if (process.env.APPIMAGE || process.env.APPDIR) {
  app.commandLine.appendSwitch('no-sandbox');
  app.commandLine.appendSwitch('disable-setuid-sandbox');
}

let db;
let queries;

function getDbPath() {
  if (!app.isPackaged) {
    return path.join(__dirname, 'bible.sqlite');
  }
  // extraResource → resources/bible.sqlite. Older/unpacked builds left
  // it next to main.js as resources/app/bible.sqlite.
  const candidates = [
    path.join(process.resourcesPath, 'bible.sqlite'),
    path.join(__dirname, 'bible.sqlite'),
  ];
  return candidates.find((p) => fs.existsSync(p)) || candidates[0];
}

function openDatabase() {
  const dbPath = getDbPath();
  if (!fs.existsSync(dbPath)) {
    throw new Error(`Bible database not found at ${dbPath}`);
  }
  db = new Database(dbPath, { timeout: 5000, readonly: true, fileMustExist: true });
  queries = createQueries(db);
  console.log(`Search index: ${queries.searchReady ? 'ready' : 'NOT FOUND (verses_fts table missing)'}`);
  console.log(`Lexicon data: ${queries.lexiconReady ? 'ready' : 'NOT FOUND (lexicon_words/entries/sources missing)'}`);
  console.log(`Word occurrence index: ${queries.occurrencesReady ? 'ready' : 'NOT FOUND (lexicon_word_occurrences/gloss_stats missing)'}`);
  console.log(`Versification diffs: ${queries.versificationReady ? 'ready' : 'NOT FOUND (versification_diffs missing)'}`);
  console.log(`Cross references: ${queries.xrefsReady ? 'ready' : 'NOT FOUND (cross_references missing)'}`);
}

/* ================================================================
   PERSISTENT SETTINGS
   River's settings — text size, dark mode, red-letter text, showing
   edits, the default translation for new tabs, and the whole session
   (which tabs/panes were open, on which references and translations) —
   live in a single JSON file in Electron's per-user data directory, e.g.
   ~/.config/river-bible/settings.json on Linux. That directory already
   exists and is writable for any packaged Electron app, so no extra
   setup is needed here.

   The renderer (index.html) owns the actual settings *shape* — this
   side just reads/writes whatever blob it's handed, so a new setting
   added on the renderer side never needs a matching change here. The
   one exception is buildMenuTemplate() below, which reads a couple of
   known fields out of that blob to set the native menu's initial
   checked state (since the menu is built once, before the renderer has
   had a chance to say anything).
   ================================================================ */

function settingsFilePath() {
  return path.join(app.getPath('userData'), 'settings.json');
}

// The org-wide starting point for a fresh install — see
// default-settings.json's own "_readme" field for the file's shape.
// Shipped next to main.js (i.e. inside the app itself, not the per-user
// data directory settingsFilePath() above points at), so it travels with
// the app and is the same for every install until whoever builds the app
// edits it.
function defaultSettingsFilePath() {
  return path.join(__dirname, 'default-settings.json');
}
function loadDefaultSettingsSync() {
  try {
    return JSON.parse(fs.readFileSync(defaultSettingsFilePath(), 'utf8'));
  } catch (err) {
    if (err.code !== 'ENOENT') console.error('Could not read default-settings.json:', err);
    return null; // no shipped defaults file — caller falls back to index.html's own hardcoded defaults
  }
}

// The app's visible name, from default-settings.json's "appName" field
// (read fresh each time, so it's never frozen into a user's saved
// settings.json). Used for the window title and the Help > About item;
// index.html reads the same field for the in-page header and its own
// About menu item.
function getAppName() {
  const defaults = loadDefaultSettingsSync();
  const name = defaults && typeof defaults.appName === 'string' ? defaults.appName.trim() : '';
  return name || 'River Bible App';
}

// Synchronous on purpose: this is read once at startup (before the
// window/menu are built, so the menu can reflect saved settings from
// its very first paint) and the file is small (a handful of booleans
// plus a short list of open tabs), so the blocking cost is negligible —
// nowhere near what it'd cost to plumb an async settings load through
// window creation and menu building.
function loadSettingsSync() {
  try {
    const raw = fs.readFileSync(settingsFilePath(), 'utf8');
    return JSON.parse(raw);
  } catch (err) {
    if (err.code !== 'ENOENT') console.error('Could not read settings.json:', err);
    // First run (or the file is missing/corrupt) — fall back to the
    // shipped defaults rather than null, so a fresh install starts from
    // whatever this app's default-settings.json specifies instead of
    // index.html's own bare hardcoded fallbacks.
    return loadDefaultSettingsSync();
  }
}

function saveSettingsSync(data) {
  fs.mkdirSync(path.dirname(settingsFilePath()), { recursive: true });
  fs.writeFileSync(settingsFilePath(), JSON.stringify(data, null, 2), 'utf8');
}

// Update preferences (Settings ▸ Updates) live in settings.json like everything
// else; this is the slice updater.js needs. Missing/unknown values mean the
// default: check this project's GitHub Releases.
function getUpdateConfig() {
  const s = loadSettingsSync() || {};
  const mode = ['github', 'custom', 'off'].includes(s.updateMode) ? s.updateMode : 'github';
  return { mode, url: typeof s.updateUrl === 'string' ? s.updateUrl : '' };
}

// Same six operations as server.js's HTTP routes, over IPC instead.
// Thrown errors reject the renderer's ipcRenderer.invoke() promise
// automatically — db-client.js's try/catch handles that exactly like an
// HTTP error from the fetch path.
function registerIpcHandlers() {
  ipcMain.handle('db:listTranslations', () => queries.listTranslations());
  ipcMain.handle('db:getBookWords', (event, translationId, bookKey) => queries.getBookWords(translationId, bookKey));
  ipcMain.handle('db:getChapterWords', (event, translationId, bookKey, fromChapter, toChapter) => queries.getChapterWords(translationId, bookKey, fromChapter, toChapter));
  ipcMain.handle('db:getAvailableBooks', (event, translationId) => queries.getAvailableBooks(translationId));
  ipcMain.handle('db:getBookStructure', (event, translationId, bookKey) => queries.getBookStructure(translationId, bookKey));
  ipcMain.handle('db:search', (event, matchQuery, translationId, bookFilters) => queries.search(matchQuery, translationId, bookFilters));
  ipcMain.handle('db:getLexiconEntry', (event, strong) => queries.getLexiconEntry(strong));
  ipcMain.handle('db:getWordOccurrences', (event, strong, translationId) => queries.getWordOccurrences(strong, translationId));
  ipcMain.handle('db:getVersificationLinks', (event, bookKey) => queries.getVersificationLinks(bookKey));
  ipcMain.handle('db:getCrossReferences', (event, translationId, bookKey, chapter, fromVerse, toVerse) => queries.getCrossReferences(translationId, bookKey, chapter, fromVerse, toVerse));
  ipcMain.handle('db:searchLexicon', (event, query) => queries.searchLexicon(query));
  ipcMain.handle('db:getWordOccurrencesByQuery', (event, query, translationId) => queries.getWordOccurrencesByQuery(query, translationId));
  
  // The renderer is the source of truth for what the settings object
  // looks like (see the big comment above) — this side just persists
  // whatever it's handed and hands back whatever's on disk.
  ipcMain.handle('settings:load', () => loadSettingsSync());
  ipcMain.handle('settings:save', (event, data) => {
    try {
      saveSettingsSync(data);
      return true;
    } catch (err) {
      console.error('Could not write settings.json:', err);
      return false;
    }
  });
  // A fresh, independent read of default-settings.json — used by the
  // Settings dialog's "Restore Default Settings" button, as opposed to
  // loadSettingsSync's one-time first-run fallback above.
  ipcMain.handle('settings:loadDefaults', () => loadDefaultSettingsSync());

  // Settings ▸ Updates. The renderer passes its live (possibly not yet saved)
  // choice for "Check now"; background checks read settings.json instead.
  ipcMain.handle('updater:getStatus', () => updater.getStatus());
  ipcMain.handle('updater:check', (event, config) => {
    const mode = config && ['github', 'custom', 'off'].includes(config.mode) ? config.mode : 'github';
    return updater.checkForUpdates({ mode, url: config && typeof config.url === 'string' ? config.url : '' }, { manual: true });
  });

  // Right-click a tab > Open in New Window (index.html): opens a fresh
  // window pointed at the same reference/translation/compare state as
  // the tab it was opened from — see createWindow's openTabPayload
  // handling and initApp's own openTab URL-param check for the other
  // half of this.
  ipcMain.handle('window:openTab', (event, payload) => {
    createWindow(loadSettingsSync(), payload);
  });

  // index.html's document-level external-link click handler routes any
  // real <a href> (translation/lexicon copyright & license text pulled
  // from bible.sqlite, mostly) here instead of navigating the window
  // itself. See also createWindow's setWindowOpenHandler below, which
  // catches anything that slips past the renderer-side handler.
  ipcMain.on('open-external', (event, url) => {
    shell.openExternal(url);
  });

  // In-page find (Ctrl/Cmd+F) — index.html's find bar. Both go straight
  // to event.sender (the webContents that actually sent them) rather
  // than the focused window, so find state always tracks whichever
  // window's find bar the renderer opened, even with more than one
  // window on screen. Results come back via the 'found-in-page' listener
  // registered on each window's webContents in createWindow below.
  ipcMain.on('find-in-page', (event, text, options) => {
    event.sender.findInPage(text, options);
  });
  ipcMain.on('stop-find-in-page', (event, action) => {
    event.sender.stopFindInPage(action);
  });
}

function createWindow(settings, openTabPayload) {
  const startDark = !settings || settings.darkMode !== false; // matches index.html's own default (dark unless explicitly turned off before)
  const win = new BrowserWindow({
    width: 1280,
    height: 900,
    title: getAppName(), // Explicit window title fallback for Linux frame
    icon: path.join(__dirname, 'assets', 'icon.svg'), // Explicit window icon path for taskbar/dock frame// Matches index.html's default theme for whichever mode we're about
    // to start in. Without this, Electron defaults to a white window
    // background, which shows through as a visible flash/gap around the
    // web contents during resize — most noticeably as a 1px sliver under
    // the menu bar when maximizing. Kept in sync with the Dark Mode menu
    // toggle below via win.setBackgroundColor().
    backgroundColor: startDark ? '#242424' : '#fafafb',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      // The renderer (index.html) gets no Node access and no direct IPC
      // access — only the narrow API preload.js explicitly exposes via
      // contextBridge. sandbox further restricts what the preload script
      // itself can do (it can still require('electron') and use
      // contextBridge, but not arbitrary Node built-ins).
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true
    }
  });
  win.loadFile('index.html', openTabPayload ? { query: { openTab: JSON.stringify(openTabPayload) } } : undefined);
  // Dynamically reflect page title updates on the native OS window frame
  win.on('page-title-updated', (event, title) => {
    win.setTitle(title);
  });

  // Belt-and-suspenders: index.html's click handler already routes
  // external links through the 'open-external' IPC channel above, but
  // anything that still tries to open a new BrowserWindow/tab from
  // within this window (e.g. window.open on a link that handler missed,
  // or a target="_blank" from content we don't control) gets sent to the
  // OS browser instead of spawning a second Electron window.
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });
  Menu.setApplicationMenu(Menu.buildFromTemplate(buildMenuTemplate(win, settings)));

  // Forwards Chromium's own find-in-page results back to index.html's
  // find bar, which sent the search in the first place via the
  // 'find-in-page' IPC listener above — see preload.js's findInPage/
  // onFoundInPage.
  win.webContents.on('found-in-page', (event, result) => {
    win.webContents.send('found-in-page', result);
  });

  // Works around a known Electron/GTK issue on Linux where maximizing
  // leaves a 1px gap between the native menu bar and the web contents —
  // Chromium's compositor and GTK's window frame briefly disagree on the
  // exact content bounds right after the maximize event. Nudging the
  // height by 1px and immediately back forces both sides to recompute
  // and resync. Harmless on platforms/window managers that don't have
  // this bug — it's a no-op resize there.
  win.on('maximize', () => {
    const bounds = win.getBounds();
    win.setBounds({ ...bounds, height: bounds.height + 1 });
    setImmediate(() => win.setBounds(bounds));
  });

  return win;
}

// River's own settings (text size, dark mode, red-letter text, showing
// edits, default translation) live in this native application menu
// rather than the in-page header bar — see index.html's .electron-app
// CSS rule and its onMenuAction listener, which this talks to over the
// 'menu:action' channel. Everything else here is Electron's normal
// default menu (role-based items), just extended rather than replaced,
// so the app still gets standard behavior — copy/paste, reload, zoom,
// quit — for free.
//
// `settings` is whatever loadSettingsSync() returned at startup (or
// null on a first run) — used only to set each item's initial checked
// state/selection so the menu already reflects last session's choices
// the moment it appears, rather than snapping to defaults and then
// jumping once the renderer finishes loading its own copy of the same
// file a moment later.
function buildMenuTemplate(win, settings) {
  const isMac = process.platform === 'darwin';
  // Targets whichever window currently has focus rather than always the
  // one this particular menu template happened to be built for — with
  // more than one window open (see "Open in New Window" in index.html's
  // tab context menu), the menu itself is still one shared, global
  // object (Electron only ever has one application menu at a time on
  // Windows/Linux, and it's always global on macOS), so without this a
  // second window's Preferences/Dark Mode/etc. would silently keep
  // acting on the first window instead of whichever one is actually in
  // front. `win` only serves as a fallback for the unusual case where
  // no window is currently focused.
  const sendAction = (action, value) => (BrowserWindow.getFocusedWindow() || win).webContents.send('menu:action', action, value);

  // Translations live in bible.sqlite, not in settings.json — read
  // straight from the (synchronous) database connection opened in
  // openDatabase(), the same way the IPC handler above does. This runs
  // once, at startup, before the window exists.
  let translations = [];
  try {
    const info = queries.listTranslations();
    translations = (info && info.translations) || [];
  } catch (err) {
    console.error('Could not list translations for the menu:', err);
  }
  const savedDefaultId = settings && settings.defaultTranslationId;
  const defaultTranslationSubmenu = translations.length
    ? translations.map((t, i) => ({
        label: t.code + ' \u2014 ' + t.name,
        type: 'radio',
        checked: savedDefaultId ? t.id === savedDefaultId : i === 0,
        click: () => sendAction('default-translation', t.id)
      }))
    : [{ label: 'No translations loaded', enabled: false }];

  const settingsItems = [
    // Increase/Decrease Text Size below use CmdOrCtrl+=/− — the same
    // defaults Electron's built-in zoomIn/zoomOut roles use, which is
    // why those roles are deliberately left out of the View menu further
    // down: two menu items bound to the same accelerator is undefined
    // behavior, and Chromium's whole-page zoom (scaling every UI element
    // uniformly) is not what "Text Size" is supposed to do here — it's
    // meant to scale only the reading text, via --text-scale in
    // index.html.
    { label: 'Increase Text Size', accelerator: 'CmdOrCtrl+=', click: () => sendAction('font-inc') },
    { label: 'Decrease Text Size', accelerator: 'CmdOrCtrl+-', click: () => sendAction('font-dec') },
    { type: 'separator' },
    {
      label: 'Dark Mode', type: 'checkbox', checked: !settings || settings.darkMode !== false, // matches index.html's default (<html class="dark">) unless a saved setting says otherwise
      click: (item) => {
        // Keeps the window's own background color (see createWindow) in
        // step with the page theme, so the resize/maximize gap fix above
        // still applies correctly after switching to light mode. Same
        // focused-window targeting as sendAction above.
        (BrowserWindow.getFocusedWindow() || win).setBackgroundColor(item.checked ? '#242424' : '#fafafb');
        sendAction('dark-mode', item.checked);
      }
    },
    {
      label: 'Words of Christ in Red', type: 'checkbox', checked: !!(settings && settings.redLetter),
      click: (item) => sendAction('red-letter', item.checked)
    },
    { type: 'separator' },
    { label: 'Default Translation', submenu: defaultTranslationSubmenu },
    { type: 'separator' }
  ];

  return [
    // File and Window used to be here — File only ever held Close/Quit
    // (both still reachable via their normal OS shortcuts, Cmd/Ctrl+Q and
    // Cmd/Ctrl+W, without a menu) and Window is Electron's default
    // window-list menu, which isn't useful in a single-window app — both
    // were more clutter than function, so they've been dropped.
    ...(isMac ? [{ role: 'appMenu' }] : []),
    {
      label: 'Edit',
      // Built manually (rather than { role: 'editMenu' }) so Preferences
      // can live here too, next to the standard cut/copy/paste items —
      // this list of roles is the same set Electron's own editMenu role
      // would otherwise expand to.
      submenu: [
        { role: 'undo' },
        { role: 'redo' },
        { type: 'separator' },
        { role: 'cut' },
        { role: 'copy' },
        { role: 'paste' },
        { role: 'pasteAndMatchStyle' },
        { role: 'delete' },
        { role: 'selectAll' },
        { type: 'separator' },
        { label: 'Find in Page\u2026', accelerator: 'CmdOrCtrl+F', click: () => sendAction('find-in-page') },
        { type: 'separator' },
        // Prints just the Bible text of whichever pane has focus — see
        // index.html's printPaneText and its 'print' menu-action case.
        { label: 'Print\u2026', accelerator: 'CmdOrCtrl+P', click: () => sendAction('print') },
        { type: 'separator' },
        { label: 'Preferences\u2026', accelerator: 'CmdOrCtrl+,', click: () => sendAction('open-settings') }
      ]
    },
    {
      label: 'View',
      submenu: [
        ...settingsItems,
        { role: 'reload' },
        { role: 'toggleDevTools' },
        { type: 'separator' },
        { role: 'togglefullscreen' }
      ]
    },
    {
      role: 'help',
      submenu: [
        {
          label: 'About ' + getAppName(),
          click: () => sendAction('about')
        },
        {
          label: 'Check for Updates\u2026',
          click: () => updater.checkForUpdates(getUpdateConfig(), { manual: true })
        },
        {
          label: 'River on GitHub',
          click: () => shell.openExternal('https://github.com/riverstudytools/river-bible')
        }
      ]
    }
  ];
}

app.whenReady().then(() => {
  openDatabase();
  registerIpcHandlers();
  // Read once, synchronously, before the window/menu are built — see
  // the settings section above for why. The renderer will separately
  // (and asynchronously) load the same file over IPC once index.html
  // finishes booting, to apply the rest of it (session tabs, etc.) —
  // this copy is only used for the native menu's initial checked state.
  const initialSettings = loadSettingsSync();
  createWindow(initialSettings);
  installDesktopIntegration(); // AppImage only: app-menu entry + icon
  updater.startBackgroundChecks(getUpdateConfig);

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow(loadSettingsSync());
  });
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
  if (db) db.close();
});
