#!/usr/bin/env bash
# Package the built Animora Linux tree into an AppImage.
#   build_appimage.sh <build-bin-dir> <dist-dir>
# <build-bin-dir> is the cmake output bin/ containing the `animora` binary
# plus its datafiles (the 5.1/ tree). Run on Linux only.
set -euo pipefail

BIN_DIR="${1:?usage: build_appimage.sh <bin-dir> <dist-dir>}"
DIST_DIR="${2:?usage: build_appimage.sh <bin-dir> <dist-dir>}"
mkdir -p "$DIST_DIR"

if [ ! -x "$BIN_DIR/animora" ] && [ ! -f "$BIN_DIR/animora" ]; then
  echo "ERROR: 'animora' binary not found in $BIN_DIR" >&2
  exit 1
fi

VERSION="$(python3 -c "import sys; sys.path.insert(0,'scripts'); import animora_config as c; print(c.ANIMORA_VERSION)" 2>/dev/null || echo "1.1.0")"

# AppDir layout.
APPDIR="$(mktemp -d)/Animora.AppDir"
mkdir -p "$APPDIR/usr/bin" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/icons/hicolor/256x256/apps"
cp -R "$BIN_DIR"/* "$APPDIR/usr/bin/"

# Icon (Animora 256 from assets/branding) + desktop entry.
cp assets/branding/animora_256.png "$APPDIR/usr/share/icons/hicolor/256x256/apps/animora.png" 2>/dev/null || true
cp "$APPDIR/usr/share/icons/hicolor/256x256/apps/animora.png" "$APPDIR/animora.png" 2>/dev/null || true
cat > "$APPDIR/animora.desktop" <<EOF
[Desktop Entry]
Name=Animora
Exec=animora
Icon=animora
Type=Application
Categories=Graphics;3DGraphics;
EOF
cp "$APPDIR/animora.desktop" "$APPDIR/usr/share/applications/animora.desktop"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/animora" "$@"
EOF
chmod +x "$APPDIR/AppRun"

# Fetch appimagetool if not present.
TOOL="$(command -v appimagetool || true)"
if [ -z "$TOOL" ]; then
  TOOL="$(mktemp -d)/appimagetool"
  curl -fSL -o "$TOOL" \
    "https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage"
  chmod +x "$TOOL"
fi

OUT="$DIST_DIR/Animora-${VERSION}-x86_64.AppImage"
ARCH=x86_64 "$TOOL" "$APPDIR" "$OUT"
echo "Built $OUT"
