#!/usr/bin/env bash
#
# build.sh — build the River Bible AppImage and publish it to GitHub Releases.
#
#   ./build.sh                  interactive: shows the current version, asks for the next one,
#                               builds, then commits + tags + pushes + creates the GitHub release
#   ./build.sh --no-publish     build only (still asks for / bumps a version, then restores it)
#   ./build.sh --version 1.2.0  skip the version prompt
#   ./build.sh --yes            skip the confirmation prompt
#
# Prerequisites: bible.sqlite (run ./build_database.sh first), node + npm, git, curl,
# and — for publishing — the GitHub CLI (`gh auth login`). No FUSE needed: the AppImage is
# built with appimagetool in extract-and-run mode and uses the uruntime runtime
# (https://github.com/VHSgunzo/uruntime), which bundles its own FUSE support.
#
# Output (in out/release/):
#   River-Bible-<version>-x86_64.AppImage
#   latest-linux.yml            <- what the in-app updater reads; must be uploaded with the AppImage

set -euo pipefail
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"

REPO="riverstudytools/river-bible"
APP_NAME="River Bible"
APP_ID="river-bible"
ARCH="x86_64"
CACHE=".build-cache"
RELEASE_DIR="out/release"
APPDIR="out/AppDir"
APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-${ARCH}.AppImage"
URUNTIME_URL="https://github.com/VHSgunzo/uruntime/releases/latest/download/uruntime-appimage-squashfs-lite-${ARCH}"

PUBLISH=1
ASSUME_YES=0
NEXT=""

while [ $# -gt 0 ]; do
  case "$1" in
    --no-publish) PUBLISH=0 ;;
    --yes|-y)     ASSUME_YES=1 ;;
    --version)    NEXT="${2:-}"; shift ;;
    -h|--help)    sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)            echo "Unknown option: $1 (try --help)" >&2; exit 1 ;;
  esac
  shift
done

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die()  { printf '\033[31mError: %s\033[0m\n' "$*" >&2; exit 1; }
confirm() { # prompt
  [ "$ASSUME_YES" = 1 ] && return 0
  local ans; read -r -p "$1 [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]]
}

# ---------------------------------------------------------------- preflight
for tool in node npm git curl; do
  command -v "$tool" >/dev/null || die "'$tool' is not installed (on Silverblue, run this inside your toolbox/distrobox)."
done
if [ "$PUBLISH" = 1 ]; then
  command -v gh >/dev/null || die "GitHub CLI 'gh' is not installed. Install it, or use --no-publish."
  gh auth status >/dev/null 2>&1 || die "Not logged in to GitHub. Run: gh auth login"
fi
[ -f bible.sqlite ] || die "bible.sqlite not found. Run ./build_database.sh first."

if [ "$PUBLISH" = 1 ]; then
  if ! git diff --quiet || ! git diff --cached --quiet; then
    die "You have uncommitted changes. Commit or stash them first — the release tag must match what gets built (or use --no-publish)."
  fi
  branch="$(git rev-parse --abbrev-ref HEAD)"
  if [ "$branch" != "main" ]; then
    confirm "You are on '$branch', not 'main'. Release from here anyway?" || exit 1
  fi
  git fetch --quiet origin
  if [ -n "$(git rev-list 'HEAD..@{u}' 2>/dev/null || true)" ]; then
    die "Your branch is behind origin. Pull first."
  fi
fi

# ---------------------------------------------------------------- version
say "Installing dependencies"
npm ci

CURRENT="$(node -p "require('./package.json').version")"
LATEST_RELEASE="none yet"
if [ "$PUBLISH" = 1 ]; then
  LATEST_RELEASE="$(gh release view --repo "$REPO" --json tagName -q .tagName 2>/dev/null || echo 'none yet')"
fi

bump() { node -e "const s=require('semver');console.log(s.inc(process.argv[1],process.argv[2])||'')" "$CURRENT" "$1"; }
PATCH="$(bump patch)"; MINOR="$(bump minor)"; MAJOR="$(bump major)"

say "Version"
echo "  package.json version : $CURRENT"
echo "  latest GitHub release: $LATEST_RELEASE"

if [ -z "$NEXT" ]; then
  echo
  echo "  What should the next version be?"
  echo "    1) patch  -> $PATCH"
  echo "    2) minor  -> $MINOR"
  echo "    3) major  -> $MAJOR"
  echo "    or type a version, e.g. 1.2.0 or 1.2.0-beta.1 (pre-releases are not offered as updates)"
  read -r -p "  Next version: " choice
  case "$choice" in
    1) NEXT="$PATCH" ;;
    2) NEXT="$MINOR" ;;
    3) NEXT="$MAJOR" ;;
    *) NEXT="${choice#v}" ;;
  esac
fi

node -e "
  const s=require('semver'); const [cur,next]=process.argv.slice(1);
  if(!s.valid(next)){console.error('\"'+next+'\" is not a valid version (expected MAJOR.MINOR.PATCH).');process.exit(1)}
  if(!s.gt(next,cur)){console.error('Version '+next+' is not higher than the current '+cur+'.');process.exit(1)}
" "$CURRENT" "$NEXT" || exit 1

TAG="v$NEXT"
if [ "$PUBLISH" = 1 ]; then
  git rev-parse -q --verify "refs/tags/$TAG" >/dev/null && die "Tag $TAG already exists locally."
  git ls-remote --exit-code --tags origin "$TAG" >/dev/null 2>&1 && die "Tag $TAG already exists on GitHub."
  confirm "Build and publish $APP_NAME $NEXT to GitHub?" || exit 1
else
  confirm "Build $APP_NAME $NEXT (no publish)?" || exit 1
fi

# Bump package.json + package-lock.json with npm itself (no git tag yet — we tag after a good build).
# If anything fails before the release is committed, put the version files back.
RELEASED=0
restore_version() {
  if [ "$RELEASED" = 0 ]; then
    git checkout -- package.json package-lock.json 2>/dev/null || true
  fi
}
trap restore_version EXIT
npm version "$NEXT" --no-git-tag-version >/dev/null
echo "  package.json is now at $(node -p "require('./package.json').version")"

# ---------------------------------------------------------------- tools
mkdir -p "$CACHE"
fetch_tool() { # url dest
  if [ ! -x "$2" ]; then
    say "Downloading $(basename "$2")"
    curl -fL --retry 3 -o "$2.part" "$1"
    chmod +x "$2.part"
    mv "$2.part" "$2"
  fi
}
fetch_tool "$APPIMAGETOOL_URL" "$CACHE/appimagetool"
fetch_tool "$URUNTIME_URL"     "$CACHE/uruntime"

# ---------------------------------------------------------------- build
say "Packaging the Electron app"
rm -rf out
npx electron-forge package --platform linux --arch x64
PKG_DIR="$(ls -d out/*-linux-x64 | head -n1)"
[ -d "$PKG_DIR" ] || die "electron-forge did not produce out/*-linux-x64"

say "Assembling AppDir"
mkdir -p "$APPDIR" "$RELEASE_DIR"
cp -a "$PKG_DIR"/. "$APPDIR"/
# AppImage mounts are nosuid, so Chromium's setuid helper can never work there; if it is present,
# Chromium aborts with "The SUID sandbox helper binary was found, but is not configured correctly".
# forge.config.js removes it; this is a safety net so a packager change can't ship it again.
rm -f "$APPDIR/chrome-sandbox"
[ ! -e "$APPDIR/chrome-sandbox" ] || die "chrome-sandbox is still in the AppDir."
# appimagetool wants a PNG icon (it derives .DirIcon from it); an SVG .DirIcon is rejected.
[ -f assets/icon.png ] || die "assets/icon.png is missing (it is rendered from assets/icon.svg and committed)."
cp assets/icon.png "$APPDIR/$APP_ID.png"

cat > "$APPDIR/$APP_ID.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
Comment=Open source Bible study app
Exec=$APP_ID %U
Icon=$APP_ID
Terminal=false
Categories=Education;Literature;
StartupWMClass=$APP_ID
EOF

# --no-sandbox has to be on the command line: Electron sets up Chromium's sandbox before any JS runs,
# so app.commandLine.appendSwitch in main.js is too late. AppImage mounts are nosuid, which rules out the
# setuid helper, and the namespace sandbox isn't reliably available inside the AppImage's own namespaces.
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/river-bible" --no-sandbox "$@"
EOF
chmod +x "$APPDIR/AppRun"
# Other users must be able to read/run the AppImage; your umask may have made these 0700.
chmod -R a+rX "$APPDIR"
chmod a+rx "$APPDIR" "$APPDIR/AppRun"

say "Building the AppImage (uruntime)"
OUT_NAME="River-Bible-${NEXT}-${ARCH}.AppImage"
OUT="$RELEASE_DIR/$OUT_NAME"
rm -f "$OUT"
APPIMAGE_EXTRACT_AND_RUN=1 ARCH="$ARCH" "$CACHE/appimagetool" \
  --no-appstream \
  --runtime-file "$CACHE/uruntime" \
  "$APPDIR" "$OUT"
chmod +x "$OUT"
[ -s "$OUT" ] || die "appimagetool produced no output."

# The in-app updater (electron-updater) reads this file to learn the newest version and verify the download.
say "Writing latest-linux.yml"
node -e "
  const fs=require('fs'), path=require('path'), crypto=require('crypto');
  const [file, name, version]=process.argv.slice(1);
  const buf=fs.readFileSync(file);
  const sha512=crypto.createHash('sha512').update(buf).digest('base64');
  fs.writeFileSync(path.join(path.dirname(file),'latest-linux.yml'),
    'version: '+version+'\n'+
    'files:\n'+
    '  - url: '+name+'\n'+
    '    sha512: '+sha512+'\n'+
    '    size: '+buf.length+'\n'+
    'path: '+name+'\n'+
    'sha512: '+sha512+'\n'+
    'releaseDate: \''+new Date().toISOString()+'\'\n');
" "$OUT" "$OUT_NAME" "$NEXT"

say "Built $OUT ($(du -h "$OUT" | cut -f1))"

if [ "$PUBLISH" = 0 ]; then
  echo "Skipping publish (--no-publish). package.json restored to $CURRENT."
  exit 0
fi

# ---------------------------------------------------------------- publish
say "Publishing $TAG"
git add package.json package-lock.json
git commit -m "Release $TAG"
git tag -a "$TAG" -m "$APP_NAME $NEXT"
RELEASED=1   # from here on, don't roll package.json back — the commit exists

PRE_FLAG=()
[[ "$NEXT" == *-* ]] && PRE_FLAG=(--prerelease)

git push origin HEAD "$TAG"
gh release create "$TAG" \
  "$OUT" "$RELEASE_DIR/latest-linux.yml" \
  --repo "$REPO" --verify-tag \
  --title "$APP_NAME $NEXT" --generate-notes \
  ${PRE_FLAG[@]+"${PRE_FLAG[@]}"}

say "Done: https://github.com/$REPO/releases/tag/$TAG"
