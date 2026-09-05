#!/usr/bin/env bash
# Build "DuckSwarm Editor.app", a Dock-clickable launcher for scripts/edit.sh.
#
#   ./scripts/make_launcher_app.sh                 # builds dist/DuckSwarm Editor.app
#   ./scripts/make_launcher_app.sh --install       # ...and copies it to ~/Applications
#   ./scripts/make_launcher_app.sh --install /Applications
#
# Then drag the app to the Dock. Clicking it opens a Terminal window running
# edit.sh and then the editor in the browser; closing that window stops the
# server. No Xcode project and nothing installed: the app is a shell script in
# a bundle (scripts/launcher/launch.sh, run.command), and its icon is drawn by
# scripts/launcher/make_icon.swift with AppKit, then packed by sips and
# iconutil, all of which ship with macOS and the Xcode command line tools.
#
# The repository path is baked into run.command at build time, so rebuild
# after moving the checkout, or write the new path into ~/.duckswarm/editor-repo.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
APP_NAME="DuckSwarm Editor"
OUT_DIR="$REPO/dist"
INSTALL_DIR=""

while [ $# -gt 0 ]; do
  case "$1" in
    --install) INSTALL_DIR="${2:-}"; if [ -n "$INSTALL_DIR" ] && [ "${INSTALL_DIR#-}" = "$INSTALL_DIR" ]; then shift; else INSTALL_DIR="$HOME/Applications"; fi ;;
    --out) OUT_DIR="$2"; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unexpected argument: $1" >&2; exit 1 ;;
  esac
  shift
done

for tool in swift sips iconutil plutil; do
  command -v "$tool" >/dev/null 2>&1 || { echo "need $tool (part of macOS or the Xcode command line tools)" >&2; exit 1; }
done

APP="$OUT_DIR/$APP_NAME.app"
CONTENTS="$APP/Contents"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# The real MicroDuck, cut out by scripts/launcher/render-duck.html from the
# meshes, when that render has been made on this machine (it is gitignored:
# a derivative of Pollen's CC BY-SA-NC assets); the emoji stands in otherwise.
# The Swift script reports which of the two it actually drew, so that line is
# the one that says it, after the fact, rather than a guess made here before.
DUCK_PNG="$REPO/scripts/launcher/duck.png"
echo "rendering icon..."
if [ -f "$DUCK_PNG" ]; then
  swift "$REPO/scripts/launcher/make_icon.swift" "$WORK/icon-1024.png" "$DUCK_PNG"
else
  swift "$REPO/scripts/launcher/make_icon.swift" "$WORK/icon-1024.png"
  echo "  (no scripts/launcher/duck.png; see scripts/launcher/render-duck.html for the real one)"
fi

# .iconset: every size macOS asks for, at 1x and 2x, from the one 1024 render.
ICONSET="$WORK/AppIcon.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$WORK/icon-1024.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$WORK/icon-1024.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$WORK/AppIcon.icns"

echo "assembling $APP"
rm -rf "$APP"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources"
cp "$WORK/AppIcon.icns" "$CONTENTS/Resources/AppIcon.icns"
install -m 755 "$REPO/scripts/launcher/launch.sh" "$CONTENTS/MacOS/launch"
# Bake the repository path in. sed with | as the delimiter, since the path
# has slashes and may have spaces; it must not contain a literal |.
case "$REPO" in *'|'*) echo "repository path contains '|', which this build cannot bake in" >&2; exit 1 ;; esac
sed "s|@REPO@|$REPO|" "$REPO/scripts/launcher/run.command" > "$CONTENTS/Resources/run.command"
chmod 755 "$CONTENTS/Resources/run.command"

cat > "$CONTENTS/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>org.microduckswarm.editor-launcher</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>launch</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHumanReadableCopyright</key><string>MicroDuckSwarm</string>
</dict>
</plist>
PLIST
plutil -lint "$CONTENTS/Info.plist" >/dev/null
bash -n "$CONTENTS/MacOS/launch"
bash -n "$CONTENTS/Resources/run.command"

if [ -n "$INSTALL_DIR" ]; then
  mkdir -p "$INSTALL_DIR"
  rm -rf "$INSTALL_DIR/$APP_NAME.app"
  cp -R "$APP" "$INSTALL_DIR/"
  echo "installed  $INSTALL_DIR/$APP_NAME.app"
  echo "next       drag it from there to the Dock"
else
  echo "built      $APP"
  echo "next       ./scripts/make_launcher_app.sh --install   (copies it to ~/Applications)"
fi
