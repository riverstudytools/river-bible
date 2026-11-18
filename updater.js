// updater.js — AppImage auto-update for the Electron main process
//
// Three modes, chosen in Settings ▸ Updates (stored in settings.json as
// `updateMode` / `updateUrl`; main.js hands them to us via getConfig):
//
//   'github' (default)  Check this project's GitHub Releases.
//   'custom'            Check a release page the user supplies:
//                         • https://github.com/<owner>/<repo>[/releases…]  → that repo's Releases
//                         • any other https:// URL → a folder that contains latest-linux.yml
//                           (build.sh writes that file next to the AppImage it uploads)
//   'off'               Never check.
//
// Updating only applies to the AppImage build (electron-updater swaps the
// running .AppImage file in place). Dev runs and other package formats just
// report "unsupported" and never touch the network.

const { app, dialog, BrowserWindow } = require('electron');

const DEFAULT_OWNER = 'riverstudytools';
const DEFAULT_REPO = 'river-bible';
const RECHECK_MS = 6 * 60 * 60 * 1000; // re-check every 6 hours while the app stays open
const STARTUP_DELAY_MS = 8000;         // let the window finish loading first

let autoUpdater = null;
let checking = false;
let manualCheck = false;
let status = { state: 'idle' };

function isSupported() {
  return app.isPackaged && !!process.env.APPIMAGE;
}

function setStatus(next) {
  status = next;
  for (const win of BrowserWindow.getAllWindows()) {
    if (!win.isDestroyed()) win.webContents.send('updater:status', status);
  }
}

// Turns Settings' "custom release page" text into an electron-updater feed
// config, or null if it isn't something we can use.
function parseCustomUrl(raw) {
  const text = String(raw || '').trim();
  if (!text) return null;
  let url;
  try { url = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(text) ? text : 'https://' + text); }
  catch { return null; }
  const localhost = url.hostname === 'localhost' || url.hostname === '127.0.0.1';
  if (url.protocol !== 'https:' && !(url.protocol === 'http:' && localhost)) return null;

  if (url.hostname === 'github.com' || url.hostname === 'www.github.com') {
    const [owner, repo] = url.pathname.split('/').filter(Boolean);
    if (!owner || !repo) return null;
    return { provider: 'github', owner, repo: repo.replace(/\.git$/, '') };
  }
  // Generic server: accept either the folder or a direct link to latest-linux.yml.
  const base = url.origin + url.pathname.replace(/\/[^/]*\.ya?ml$/i, '').replace(/\/+$/, '');
  return { provider: 'generic', url: base };
}

function resolveFeed(config) {
  const mode = config && config.mode;
  if (mode === 'off') return null;
  if (mode === 'custom') return parseCustomUrl(config.url); // null → invalid, handled by caller
  return { provider: 'github', owner: DEFAULT_OWNER, repo: DEFAULT_REPO };
}

function getUpdater() {
  if (autoUpdater) return autoUpdater;
  // Required lazily so a dev run (or a build without the module) never pays for it.
  ({ autoUpdater } = require('electron-updater'));
  autoUpdater.autoDownload = true;
  autoUpdater.autoInstallOnAppQuit = true; // "Later" still installs on the next quit
  autoUpdater.allowPrerelease = false;
  autoUpdater.logger = console;

  autoUpdater.on('checking-for-update', () => setStatus({ state: 'checking' }));
  autoUpdater.on('update-available', (info) => setStatus({ state: 'downloading', version: info.version, percent: 0 }));
  autoUpdater.on('download-progress', (p) => setStatus({ state: 'downloading', version: status.version, percent: Math.round(p.percent) }));
  autoUpdater.on('update-not-available', () => {
    checking = false;
    setStatus({ state: 'current', version: app.getVersion() });
    if (manualCheck) {
      dialog.showMessageBox({ type: 'info', message: 'River Bible is up to date.', detail: `You have version ${app.getVersion()}.` });
    }
    manualCheck = false;
  });
  autoUpdater.on('update-downloaded', (info) => {
    checking = false;
    setStatus({ state: 'downloaded', version: info.version });
    manualCheck = false;
    dialog.showMessageBox({
      type: 'info',
      buttons: ['Restart Now', 'Later'],
      defaultId: 0,
      cancelId: 1,
      message: `River Bible ${info.version} is ready to install.`,
      detail: 'Restart to finish updating. If you choose Later, it installs the next time you quit.',
    }).then(({ response }) => { if (response === 0) autoUpdater.quitAndInstall(); });
  });
  autoUpdater.on('error', (err) => {
    checking = false;
    const message = (err && err.message ? err.message : String(err)).split('\n')[0];
    setStatus({ state: 'error', message });
    if (manualCheck) dialog.showMessageBox({ type: 'error', message: 'Could not check for updates.', detail: message });
    manualCheck = false;
  });
  return autoUpdater;
}

// config = { mode: 'github' | 'custom' | 'off', url: string }
// manual = true when the person asked (menu item / Settings button): results
// are shown in dialogs, and a check runs even in "off" mode.
async function checkForUpdates(config, { manual = false } = {}) {
  if (!isSupported()) {
    setStatus({ state: 'unsupported' });
    if (manual) dialog.showMessageBox({ type: 'info', message: 'Updates are only available in the AppImage version of River Bible.' });
    return status;
  }
  if (checking) return status;

  const effective = manual && config && config.mode === 'off' ? { mode: 'github' } : config;
  if (!manual && config && config.mode === 'off') { setStatus({ state: 'disabled' }); return status; }

  const feed = resolveFeed(effective);
  if (!feed) {
    setStatus({ state: 'error', message: 'The custom release page is not a valid URL.' });
    if (manual) dialog.showMessageBox({ type: 'error', message: 'Could not check for updates.', detail: 'The custom release page is not a valid URL.' });
    return status;
  }

  checking = true;
  manualCheck = manual;
  try {
    const updater = getUpdater();
    updater.setFeedURL(feed);
    await updater.checkForUpdates(); // outcomes arrive through the events above
  } catch (err) {
    // 'error' event normally covers this; this catches setFeedURL/require failures.
    checking = false;
    const message = (err && err.message ? err.message : String(err)).split('\n')[0];
    setStatus({ state: 'error', message });
    if (manual) dialog.showMessageBox({ type: 'error', message: 'Could not check for updates.', detail: message });
    manualCheck = false;
  }
  return status;
}

// Called once from main.js after the first window exists. getConfig is read
// fresh each time so a change made in Settings applies to the next check.
function startBackgroundChecks(getConfig) {
  if (!isSupported()) { setStatus({ state: 'unsupported' }); return; }
  const run = () => checkForUpdates(getConfig(), { manual: false }).catch(() => {});
  setTimeout(run, STARTUP_DELAY_MS);
  setInterval(run, RECHECK_MS).unref();
}

function getStatus() {
  return isSupported() ? status : { state: 'unsupported' };
}

module.exports = { checkForUpdates, startBackgroundChecks, getStatus, parseCustomUrl };
