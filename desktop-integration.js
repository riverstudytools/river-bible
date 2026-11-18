// desktop-integration.js — put River Bible in the app menu when run as an AppImage
//
// On every start of a packaged AppImage this makes sure two files exist and are current:
//   ~/.local/share/applications/river-bible.desktop   (Exec points at the AppImage that is running)
//   ~/.local/share/icons/hicolor/512x512/apps/river-bible.png
// Nothing happens in development (`npm start`) or for non-AppImage builds.
//
// Because Exec is rewritten whenever the AppImage's path changes, moving the file is
// fixed up the next time you run it. Set RIVER_NO_DESKTOP_INTEGRATION=1 to opt out.

const { app } = require('electron');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { execFile } = require('child_process');

const APP_ID = 'river-bible';

// Desktop Entry spec, two layers: inside a quoted Exec argument \ " ` $ are backslash-escaped and a
// literal % is %%; then the file's string-value layer doubles every backslash again (so $ becomes \\$).
function quoteExecArg(p) {
  const arg = p.replace(/([\\"`$])/g, '\\$1').replace(/%/g, '%%');
  return '"' + arg.replace(/\\/g, '\\\\') + '"';
}

function buildDesktopEntry(appImagePath) {
  return [
    '[Desktop Entry]',
    'Type=Application',
    'Name=River Bible',
    'Comment=Open source Bible study app',
    `Exec=${quoteExecArg(appImagePath)} %U`,
    `TryExec=${appImagePath}`,
    `Icon=${APP_ID}`,
    'Terminal=false',
    'Categories=Education;Literature;',
    `StartupWMClass=${APP_ID}`,
    '',
  ].join('\n');
}

// Writes only when the content differs, via a temp file + rename so a crash never leaves half a file.
function writeIfChanged(dest, data) {
  try {
    const existing = fs.readFileSync(dest);
    if (Buffer.compare(existing, Buffer.from(data)) === 0) return false;
  } catch { /* missing — write it */ }
  fs.mkdirSync(path.dirname(dest), { recursive: true });
  const tmp = `${dest}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, data);
  fs.renameSync(tmp, dest);
  return true;
}

function installDesktopIntegration() {
  const appImage = process.env.APPIMAGE;
  if (!app.isPackaged || !appImage || process.platform !== 'linux') return;
  if (process.env.RIVER_NO_DESKTOP_INTEGRATION) return;

  try {
    const dataHome = process.env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share');

    // The icon sits at the root of the AppImage, next to the executable (see build.sh).
    const iconSrc = path.join(path.dirname(process.execPath), `${APP_ID}.png`);
    const iconDest = path.join(dataHome, 'icons', 'hicolor', '512x512', 'apps', `${APP_ID}.png`);
    let iconChanged = false;
    if (fs.existsSync(iconSrc)) iconChanged = writeIfChanged(iconDest, fs.readFileSync(iconSrc));

    const desktopDest = path.join(dataHome, 'applications', `${APP_ID}.desktop`);
    writeIfChanged(desktopDest, buildDesktopEntry(appImage));

    // Best effort: nudge the icon cache so the icon shows up without logging out. Failure is harmless.
    if (iconChanged) {
      execFile('gtk-update-icon-cache', ['-f', '-t', path.join(dataHome, 'icons', 'hicolor')],
        { timeout: 10000 }, () => {});
    }
  } catch (err) {
    console.warn('Desktop integration skipped:', err && err.message ? err.message : err);
  }
}

module.exports = { installDesktopIntegration, buildDesktopEntry, quoteExecArg };
