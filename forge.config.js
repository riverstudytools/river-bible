const fs = require('fs');
const path = require('path');

module.exports = {
  packagerConfig: {
    asar: true,
    executableName: 'river-bible',
    icon: './assets/icon',
    // bible.sqlite is opened by better-sqlite3's native binding, which reads
    // files at the OS level and can't see inside app.asar's virtual filesystem.
    // extraResource puts it in resources/ next to (not inside) the asar.
    extraResource: ['./bible.sqlite'],
    ignore: [
      /\/import($|\/)/,
      /^\/bible\.sqlite$/,
    ],
    // The AppImage is assembled by build.sh (uruntime + appimagetool) from the
    // output of `electron-forge package`, so there is no AppImage maker below.
    //
    // AppImage (and most FUSE) mounts are nosuid, so a shipped
    // chrome-sandbox can never be the setuid-root helper Chromium wants.
    // Removing it lets the namespace sandbox / --no-sandbox path work.
    //
    // @electron/packager >= 19 calls hooks with ONE options object
    // ({ buildPath, electronVersion, platform, arch }); older versions used
    // positional arguments plus a `done` callback. Handle both.
    afterExtract: [
      (...args) => {
        const first = args[0];
        const opts = first && typeof first === 'object'
          ? first
          : { buildPath: args[0], platform: args[2] };
        if (opts.platform === 'linux') {
          fs.rmSync(path.join(opts.buildPath, 'chrome-sandbox'), { force: true });
        }
        const done = args[args.length - 1];
        if (typeof done === 'function') done();
      },
    ],
  },
  rebuildConfig: {},
  makers: [
    {
      name: '@electron-forge/maker-squirrel',
      config: {},
    },
    {
      name: '@electron-forge/maker-zip',
      platforms: ['darwin', 'linux'],
    },
    {
      name: '@electron-forge/maker-deb',
      config: {},
    },
    {
      name: '@electron-forge/maker-rpm',
      config: {},
    },
  ],
  plugins: [
    {
      name: '@electron-forge/plugin-auto-unpack-natives',
      config: {},
    },
  ],
};
