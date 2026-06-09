"""
animora:// deep-link receiver for the Animora desktop (Blender) app.

Blender has no native custom-URL-scheme handling, so we use a file-drop
hand-off (chosen design):

  1. At addon enable, register `animora://` with the OS so the scheme is
     routed to a tiny standalone forwarder (`animora_url_handler.py`).
  2. When the website redirects to `animora://auth/callback?code=..&state=..`,
     the OS launches the forwarder, which atomically writes the URL to
     `~/.animora/auth_callback.txt` and exits immediately.
  3. The already-running addon polls that file on a `bpy.app.timers` tick,
     consumes it, verifies `state`, and completes the token exchange.

This module owns the file primitives (pure, unit-tested) and the per-OS
scheme registration (Windows registry / Linux xdg / macOS handler .app).
No bpy import here — the polling timer lives in operators.py.
"""

from __future__ import annotations

import logging
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

log = logging.getLogger("animora.deep_link")

_HANDLER_SCRIPT = "animora_url_handler.py"


def animora_dir() -> Path:
    d = Path.home() / ".animora"
    d.mkdir(parents=True, exist_ok=True)
    return d


def callback_file() -> Path:
    return animora_dir() / "auth_callback.txt"


# ── File primitives (pure; shared by the forwarder + the addon poll) ────
def write_callback(url: str, target: Path | None = None) -> None:
    """Atomically write the callback URL so a concurrent reader never sees a
    partial line. Used by the standalone forwarder process."""
    target = target or callback_file()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".cb_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(url.strip())
        os.replace(tmp, target)  # atomic on the same filesystem
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def read_and_consume_callback(target: Path | None = None) -> str | None:
    """Return the pending callback URL and delete the file, or None if no
    callback is waiting. Single-use by construction."""
    target = target or callback_file()
    try:
        if not target.exists():
            return None
        url = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        target.unlink()
    except OSError:
        pass
    return url or None


# ── Scheme registration (per-OS) ────────────────────────────────────────
def _handler_path() -> Path:
    return Path(__file__).resolve().parent / _HANDLER_SCRIPT


def _find_python() -> str | None:
    """Locate a real Python interpreter that can run the forwarder directly.

    In Blender, sys.executable is the Blender BINARY (it can't run a plain
    .py), so we look for the interpreter Blender ships under sys.prefix.
    Returns a path, or None to signal the Blender-binary fallback."""
    base = getattr(sys, "_base_executable", "") or ""
    if base and "blender" not in os.path.basename(base).lower() and os.path.exists(base):
        return base
    candidates: list[str] = []
    if platform.system() == "Windows":
        candidates += [os.path.join(sys.prefix, "bin", "python.exe"),
                       os.path.join(sys.prefix, "python.exe")]
    else:
        import glob
        candidates += sorted(glob.glob(os.path.join(sys.prefix, "bin", "python3*")))
        candidates += [os.path.join(sys.prefix, "bin", "python")]
    for c in candidates:
        if c and os.path.exists(c):
            return c
    return None


def _interp_prefix() -> list[str]:
    """argv prefix to launch the forwarder, BEFORE the URL placeholder.
    Prefers Blender's bundled Python; falls back to the Blender binary run
    headless (`--background --python handler.py --`)."""
    handler = str(_handler_path())
    py = _find_python()
    if py:
        return [py, handler]
    return [sys.executable, "--background", "--python", handler, "--"]


def register_scheme() -> bool:
    """Register animora:// with the OS. Best-effort: returns True on success,
    logs + returns False otherwise (sign-in still works once the scheme is
    registered by the installer; this is the runtime fallback)."""
    system = platform.system()
    try:
        if system == "Windows":
            return _register_windows()
        if system == "Linux":
            return _register_linux()
        if system == "Darwin":
            return _register_macos()
    except Exception as exc:  # never let registration crash addon enable
        log.warning("animora:// registration failed on %s: %s", system, exc)
        return False
    log.warning("animora:// registration unsupported on %s", system)
    return False


def unregister_scheme() -> None:
    system = platform.system()
    try:
        if system == "Windows":
            _unregister_windows()
        elif system == "Linux":
            _unregister_linux()
        # macOS handler .app is left in place; LaunchServices ignores it
        # once the app is gone. Harmless.
    except Exception as exc:
        log.debug("animora:// unregister cleanup failed: %s", exc)


# ── Windows: HKCU\Software\Classes\animora ──────────────────────────────
_WIN_KEY = r"Software\Classes\animora"


def _register_windows() -> bool:
    import winreg
    cmd = " ".join(f'"{a}"' for a in _interp_prefix()) + ' "%1"'
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _WIN_KEY) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "URL:Animora Protocol")
        winreg.SetValueEx(k, "URL Protocol", 0, winreg.REG_SZ, "")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _WIN_KEY + r"\shell\open\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, cmd)
    log.info("Registered animora:// (Windows, HKCU)")
    return True


def _unregister_windows() -> None:
    import winreg
    for sub in (_WIN_KEY + r"\shell\open\command", _WIN_KEY + r"\shell\open",
                _WIN_KEY + r"\shell", _WIN_KEY):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, sub)
        except FileNotFoundError:
            pass


# ── Linux: xdg desktop entry + mime default ─────────────────────────────
_LINUX_DESKTOP = "animora-url-handler.desktop"


def _register_linux() -> bool:
    apps = Path.home() / ".local" / "share" / "applications"
    apps.mkdir(parents=True, exist_ok=True)
    entry = apps / _LINUX_DESKTOP
    entry.write_text(
        "[Desktop Entry]\n"
        "Name=Animora URL Handler\n"
        f"Exec={' '.join(_interp_prefix())} %u\n"
        "Type=Application\n"
        "Terminal=false\n"
        "NoDisplay=true\n"
        "MimeType=x-scheme-handler/animora;\n",
        encoding="utf-8",
    )
    subprocess.run(["xdg-mime", "default", _LINUX_DESKTOP, "x-scheme-handler/animora"],
                   check=False, timeout=10)
    subprocess.run(["update-desktop-database", str(apps)], check=False, timeout=10)
    log.info("Registered animora:// (Linux, xdg-mime)")
    return True


def _unregister_linux() -> None:
    entry = Path.home() / ".local" / "share" / "applications" / _LINUX_DESKTOP
    if entry.exists():
        entry.unlink()


# ── macOS: a tiny handler .app + LaunchServices register ────────────────
# Note: the most reliable macOS path is declaring CFBundleURLTypes in the
# Animora .app's Info.plist at BUILD time (rebrand.py / installer). This
# runtime handler .app is the addon-side fallback so sign-in works even on
# a dev build whose bundle doesn't declare the scheme.
def _register_macos() -> bool:
    app = Path.home() / "Applications" / "AnimoraURLHandler.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True, exist_ok=True)
    (app / "Contents" / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0"><dict>\n'
        '  <key>CFBundleIdentifier</key><string>tech.animora.urlhandler</string>\n'
        '  <key>CFBundleName</key><string>AnimoraURLHandler</string>\n'
        '  <key>CFBundleExecutable</key><string>handler</string>\n'
        '  <key>LSUIElement</key><true/>\n'
        '  <key>CFBundleURLTypes</key><array><dict>\n'
        '    <key>CFBundleURLName</key><string>tech.animora</string>\n'
        '    <key>CFBundleURLSchemes</key><array><string>animora</string></array>\n'
        '  </dict></array>\n'
        '</dict></plist>\n',
        encoding="utf-8",
    )
    shim = macos / "handler"
    shim.write_text(
        "#!/bin/bash\n"
        + "exec " + " ".join(f'"{a}"' for a in _interp_prefix()) + ' "$@"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)
    subprocess.run(
        ["/System/Library/Frameworks/CoreServices.framework/Frameworks/"
         "LaunchServices.framework/Support/lsregister", "-f", str(app)],
        check=False, timeout=10,
    )
    log.info("Registered animora:// (macOS handler .app)")
    return True
