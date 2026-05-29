"""
Recording-build "bundle mode" — auto-launch the local engine + auto-connect.

Activated ONLY when a `bundle_config.json` ships next to this module (the
Inno installer places it there for the cofounder recording build). In a
normal dev checkout or a production install the file is absent and every
function here short-circuits — the addon behaves exactly as before.

When active, on addon register we:
  1. Check whether the engine already answers on http://localhost:8000/health.
  2. If not, spawn the bundled `engine/animora-backend.exe` (resolved
     relative to the Animora install dir) with CREATE_NO_WINDOW so no
     console flashes.
  3. Poll /health until it comes up (background thread, bounded timeout).
  4. On the main thread (via bpy.app.timers): force dev-mode, `dev_signin()`,
     and `_connect_ws()` — the same two calls the "Connect (Dev)" button
     makes — so the panel opens straight into a working session with no
     sign-in.

The panel reads `get_status()` to show "starting engine…/connected/…"
instead of a sign-in button (see panel.py).

This file imports `bpy` only inside functions so it stays importable for
offline syntax/unit checks.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("animora.bundle")

_ADDON_DIR = Path(__file__).resolve().parent
_CONFIG_NAME = "bundle_config.json"

_config_cache: dict | None = None          # None = not loaded; {} = loaded-but-absent
_backend_proc: "subprocess.Popen | None" = None
_startup_thread: threading.Thread | None = None

# High-level phase shown in the panel. One of:
#   "off"        — not a bundle build (no config)
#   "starting"   — launching the engine exe
#   "waiting"    — engine launched, polling /health
#   "connecting" — health ok, establishing the WS session
#   "ready"      — connected
#   "failed"     — gave up; detail holds the reason
_status: dict[str, str] = {"phase": "off", "detail": ""}


def get_status() -> tuple[str, str]:
    """(phase, detail) for the panel. See _status docstring above."""
    return _status["phase"], _status["detail"]


def _set(phase: str, detail: str = "") -> None:
    _status["phase"] = phase
    _status["detail"] = detail
    log.info("bundle status: %s %s", phase, detail)
    _redraw()


def _redraw() -> None:
    try:
        import bpy
        if bpy.context.screen is None:
            return
        for area in bpy.context.screen.areas:
            if area.type == "ANIMORA":
                area.tag_redraw()
    except Exception:
        pass


# ── Config ──────────────────────────────────────────────────────────────

def load_config() -> dict | None:
    global _config_cache
    if _config_cache is not None:
        return _config_cache or None
    cfg_path = _ADDON_DIR / _CONFIG_NAME
    if not cfg_path.is_file():
        _config_cache = {}
        return None
    try:
        _config_cache = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("bundle_config.json present but unparseable: %s", exc)
        _config_cache = {}
        return None
    return _config_cache or None


def is_bundle_mode() -> bool:
    cfg = load_config()
    return bool(cfg and cfg.get("mode") == "recording")


# ── Engine launch ─────────────────────────────────────────────────────────

def _resolve_backend_exe(cfg: dict) -> Path | None:
    """The config carries the exe path relative to the Animora install dir
    (e.g. 'engine/animora-backend.exe'). Walk up from the addon dir until a
    parent contains it — robust to the exact install-dir depth."""
    rel = cfg.get("backend_exe", "engine/animora-backend.exe")
    for base in (_ADDON_DIR, *_ADDON_DIR.parents):
        candidate = base / rel
        if candidate.is_file():
            return candidate
    return None


def _health_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as resp:
            return getattr(resp, "status", resp.getcode()) == 200
    except Exception:
        return False


def _spawn_backend(exe: Path) -> None:
    global _backend_proc
    creationflags = 0
    if sys.platform == "win32":
        # No console window for the spawned engine.
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log.info("spawning engine: %s", exe)
    _backend_proc = subprocess.Popen(
        [str(exe)],
        cwd=str(exe.parent),
        creationflags=creationflags,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _startup_worker(cfg: dict) -> None:
    health = cfg.get("health_url", "http://localhost:8000/health")
    timeout = float(cfg.get("startup_timeout_sec", 40))

    # 1. Already up (engine left running from a previous session)? Just connect.
    if not _health_ok(health):
        exe = _resolve_backend_exe(cfg)
        if exe is None:
            _set("failed", "engine not found in install folder")
            return
        _set("starting")
        try:
            _spawn_backend(exe)
        except Exception as exc:
            log.warning("engine spawn failed: %s", exc)
            _set("failed", f"could not start engine: {exc}")
            return

    # 2. Poll until healthy.
    _set("waiting")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _health_ok(health):
            break
        time.sleep(0.5)
    else:
        _set("failed", "engine did not respond in time")
        return

    # 3. Connect on the main thread.
    _set("connecting")
    _schedule_connect()


def _schedule_connect() -> None:
    """Marshal the dev-signin + WS connect onto Blender's main thread."""
    import bpy

    def _do() -> None:
        try:
            from . import auth
            from .operators import _connect_ws
            from .preferences import get_prefs

            prefs = get_prefs()
            # Point the WS + auth URLs at the local engine.
            prefs.dev_mode = True
            auth.dev_signin()
            _connect_ws()
            _set("ready")
        except Exception as exc:
            log.warning("bundle auto-connect failed: %s", exc)
            _set("failed", str(exc)[:140])
        return None  # one-shot timer

    bpy.app.timers.register(_do, first_interval=0.1)


# ── Blender registration hooks ──────────────────────────────────────────

def register() -> None:
    global _startup_thread
    if not is_bundle_mode():
        _set("off")
        return
    _set("starting")
    cfg = load_config() or {}
    _startup_thread = threading.Thread(
        target=_startup_worker, args=(cfg,),
        daemon=True, name="animora-bundle-startup",
    )
    _startup_thread.start()


def unregister() -> None:
    global _backend_proc
    if _backend_proc is not None:
        try:
            _backend_proc.terminate()
            log.info("engine terminated on addon unregister")
        except Exception as exc:
            log.debug("engine terminate failed: %s", exc)
        _backend_proc = None
    _set("off")
