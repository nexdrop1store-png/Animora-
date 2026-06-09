#!/usr/bin/env bash
# Package the built Animora.app into a distributable .dmg.
#   build_dmg.sh <build-output-dir> <dist-dir>
# Expects the cmake build to have produced Animora.app under the build dir.
# Run on macOS only (uses hdiutil). Signing/notarization happens in CI after.
set -euo pipefail

BUILD_DIR="${1:?usage: build_dmg.sh <build-dir> <dist-dir>}"
DIST_DIR="${2:?usage: build_dmg.sh <build-dir> <dist-dir>}"
APP_NAME="Animora.app"
VOL_NAME="Animora"

mkdir -p "$DIST_DIR"

# Locate the .app the build produced (bin/Animora.app is the usual spot).
APP_PATH="$(find "$BUILD_DIR" -maxdepth 4 -name "$APP_NAME" -type d | head -1 || true)"
if [ -z "$APP_PATH" ]; then
  echo "ERROR: $APP_NAME not found under $BUILD_DIR — did the build produce it?" >&2
  exit 1
fi

# Read the product version (Animora 1.x, NOT the Blender base version).
VERSION="$(python3 -c "import sys; sys.path.insert(0,'scripts'); import animora_config as c; print(c.ANIMORA_VERSION)" 2>/dev/null || echo "1.1.0")"
DMG_PATH="$DIST_DIR/Animora-${VERSION}-macos.dmg"

# Stage a folder with the .app + an /Applications symlink (drag-to-install).
STAGE="$(mktemp -d)"
cp -R "$APP_PATH" "$STAGE/"
ln -s /Applications "$STAGE/Applications"

rm -f "$DMG_PATH"
hdiutil create -volname "$VOL_NAME" -srcfolder "$STAGE" -ov -format UDZO "$DMG_PATH"
rm -rf "$STAGE"

echo "Built $DMG_PATH"
