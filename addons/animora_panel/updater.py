"""
In-app update check + one-click auto-update.

Design/security model (founder-confirmed UX: "one-click, mostly
automatic"):
  - The addon checks Supabase's public `app_releases` table directly
    (same anon-key REST pattern auth/supabase.py already uses) for the
    latest PUBLISHED release — no new backend endpoint needed, since
    that table already has a public-read RLS policy for published rows.
  - No code-signing certificates exist yet (see .env.example's
    WINDOWS_CERT_PATH placeholder) — so before executing anything
    downloaded from the internet, this verifies its SHA-256 against
    app_releases.windows_sha256. A release published with no checksum
    on file is refused, never silently trusted.
  - Flow: panel shows "Update available" -> user clicks "Update Now"
    -> download to a temp file -> verify checksum -> launch the
    installer with /VERYSILENT /SUPPRESSMSGBOXES /NORESTART
    /ANIMORAUPDATE (the last flag tells Animora.iss's [Run] section to
    relaunch Animora after a SILENT install, which Inno's default
    `skipifsilent` behavior otherwise suppresses) -> Animora quits,
    handing off to the detached installer process.
  - Windows only for now — Inno Setup / the .exe installer is the only
    production-real distribution channel per the V2 Phase 0 audit
    (docs/V2_PHASE0_AUDIT.md: "no signed installers, no releases,
    updater story absent"); mac/linux packaging scripts exist
    (installer/macos, installer/linux) but aren't a shipped channel
    yet, so there's no windows_sha256-equivalent to verify against
    there. check_latest_release()/update_available() are platform-
    agnostic; only download_and_verify()/launch_installer_and_quit()
    are Windows-specific, and callers should gate the "auto" flow on
    sys.platform == "win32" and fall back to a manual download link
    otherwise.

bpy-free at module level (only launch_installer_and_quit imports bpy,
locally, matching the established repo convention — see
composer_buffer.py/script_guard.py) so version comparison and the
release-check/download/verify logic are unit-testable without a live
Blender.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from . import bl_info
from .auth.supabase import SUPABASE_PUBLISHABLE_KEY, SUPABASE_URL

log = logging.getLogger("animora.updater")

# ── Session-scoped cache — module-level, same convention as operators.py's
# _composer_active/_composer_buffer (simple session state, no undo/redo
# semantics needed, so a bpy WindowManager property would be overkill). ──
_cached_release: dict[str, Any] | None = None
_check_in_flight = False


def get_cached_release() -> dict[str, Any] | None:
    """The most recently fetched release row, or None if no check has
    completed yet (or the last check failed)."""
    return _cached_release


def is_update_pending() -> bool:
    """Whether the cached release (if any) is newer than the running
    Animora. False before the first check completes."""
    return update_available(_cached_release)


def refresh_cache_async() -> None:
    """Kick off a background check-for-update if one isn't already in
    flight, caching the result for the panel to read on its next draw.
    Safe to call every panel redraw — the in-flight guard makes repeat
    calls a no-op until the current check resolves."""
    global _check_in_flight
    if _check_in_flight:
        return
    _check_in_flight = True

    def _on_result(release: dict[str, Any] | None) -> None:
        global _cached_release, _check_in_flight
        _cached_release = release
        _check_in_flight = False
        if update_available(release):
            log.info("updater.update_available version=%s", release.get("version"))

    check_for_update_async(_on_result)

_RELEASES_ENDPOINT = (
    f"{SUPABASE_URL}/rest/v1/app_releases"
    "?select=version,windows_url,windows_sha256,notes"
    "&is_published=eq.true&order=published_at.desc&limit=1"
)
_REQUEST_TIMEOUT_SEC = 10.0
# Installers can be 100+ MB on a slow connection — generous but bounded.
_DOWNLOAD_TIMEOUT_SEC = 300.0
_INSTALLER_FILENAME = "Animora-Update-Setup.exe"


def current_version() -> str:
    """The running Animora's product version, as a dotted string
    ("1.3.0"). Sourced from bl_info["version"] — scripts/animora_config.py's
    module docstring documents the lockstep-bump discipline: ANIMORA_VERSION,
    installer/windows/inno/Animora.iss's MyAppVersion, AND this tuple must
    all move together on every release, or this check silently compares
    against a stale local version."""
    return ".".join(str(p) for p in bl_info["version"])


def parse_version(v: str) -> tuple[int, ...]:
    """Dotted-string -> tuple of ints, for order comparison. Tolerant of
    a malformed/non-numeric component (coerces to 0) rather than raising —
    a bad value in app_releases must never crash the update check."""
    parts: list[int] = []
    for p in v.strip().split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def is_newer(remote: str, local: str) -> bool:
    """True if `remote` is a strictly newer version than `local`. Pads
    the shorter tuple with zeros so "1.3" vs "1.3.0" compare equal
    instead of a mismatched-length surprise."""
    r, loc = parse_version(remote), parse_version(local)
    n = max(len(r), len(loc))
    r = r + (0,) * (n - len(r))
    loc = loc + (0,) * (n - len(loc))
    return r > loc


def _http_get_json(url: str, headers: dict[str, str], timeout: float) -> Any:
    """Try httpx first, fall back to stdlib urllib — same pattern
    api_validator.py already established, so this works whether or
    not httpx happens to be available in this Blender's bundled
    Python."""
    try:
        import httpx
        with httpx.Client(timeout=timeout) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()
            return resp.json()
    except ImportError:
        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8"))


def check_latest_release() -> dict[str, Any] | None:
    """Best-effort: fetch the latest published release row. Returns
    None on ANY failure (network, parse, empty result) — an update
    check must never raise or block anything else in the addon."""
    try:
        rows = _http_get_json(
            _RELEASES_ENDPOINT,
            headers={
                "apikey": SUPABASE_PUBLISHABLE_KEY,
                "Authorization": f"Bearer {SUPABASE_PUBLISHABLE_KEY}",
            },
            timeout=_REQUEST_TIMEOUT_SEC,
        )
        if not rows:
            return None
        return rows[0]
    except Exception as exc:
        log.debug("updater.check_failed: %s", exc)
        return None


def update_available(release: dict[str, Any] | None) -> bool:
    """Whether `release` (as returned by check_latest_release) is
    strictly newer than the running Animora."""
    if not release:
        return False
    remote_version = release.get("version", "")
    if not remote_version:
        return False
    return is_newer(remote_version, current_version())


def _sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_file(url: str, dest_path: Path) -> None:
    try:
        import httpx
        with httpx.Client(timeout=_DOWNLOAD_TIMEOUT_SEC, follow_redirects=True) as client, \
                client.stream("GET", url) as resp:
            resp.raise_for_status()
            with dest_path.open("wb") as f:
                for chunk in resp.iter_bytes(1024 * 64):
                    f.write(chunk)
    except ImportError:
        import urllib.request
        with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_SEC) as resp, \
                dest_path.open("wb") as f:  # noqa: S310
            while True:
                chunk = resp.read(1024 * 64)
                if not chunk:
                    break
                f.write(chunk)


def download_and_verify(url: str, expected_sha256: str, *,
                        dest_dir: Path | None = None) -> Path | None:
    """Download the installer to a temp file and verify its SHA-256
    BEFORE returning it as usable. Returns None (never the path) on
    ANY failure — missing checksum, download error, or mismatch — so
    the caller can never accidentally execute an unverified file.
    The mismatched/failed download is deleted, not left behind."""
    if not expected_sha256:
        log.warning("updater.download_refused: release has no windows_sha256 on file")
        return None
    dest_dir = dest_dir or Path(tempfile.gettempdir())
    dest_path = dest_dir / _INSTALLER_FILENAME
    try:
        _download_file(url, dest_path)
    except Exception as exc:
        log.warning("updater.download_failed: %s", exc)
        return None

    actual = _sha256_of_file(dest_path)
    if actual.lower() != expected_sha256.strip().lower():
        log.error(
            "updater.checksum_mismatch expected=%s actual=%s — refusing to run",
            expected_sha256, actual,
        )
        with suppress(Exception):
            dest_path.unlink()
        return None
    return dest_path


def launch_installer_and_quit(installer_path: Path) -> bool:
    """Launch the downloaded+verified installer as a DETACHED process
    (so it survives Animora quitting) with the silent + auto-relaunch
    flags, then quit Animora on the next timer tick. Windows-only —
    matches the Inno-Setup-only distribution channel this whole module
    targets. Returns False (and does NOT quit) if launching failed, so
    the caller can surface an error instead of silently vanishing."""
    if sys.platform != "win32":
        log.warning("updater.launch_skipped: auto-update is Windows-only")
        return False

    import bpy

    # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP: the installer must
    # outlive this process — Animora is about to quit to let it
    # overwrite files (the installer's own CloseApplications=force
    # additionally guards against timing races).
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    try:
        subprocess.Popen(  # noqa: S603
            [str(installer_path), "/VERYSILENT", "/SUPPRESSMSGBOXES",
             "/NORESTART", "/ANIMORAUPDATE"],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except Exception as exc:
        log.error("updater.launch_failed: %s", exc)
        return False

    def _quit():
        try:
            bpy.ops.wm.quit_blender()
        except Exception as exc:
            log.error("updater.quit_failed: %s", exc)
        return None

    bpy.app.timers.register(_quit, first_interval=0.5)
    return True


# ── Async wrappers — background thread + main-thread callback hop ───────
# Same pattern as api_validator.py's validate_async(): the network/disk
# work happens off the main thread so it can never stall Blender's UI
# (this is precisely the class of bug v1.1's hang-mitigation work
# targeted — a multi-hundred-MB download run synchronously would be
# just as bad as the unbounded exec() that mitigation addressed).

def _schedule_on_main_thread(cb: Callable[..., None], *args: Any) -> None:
    import bpy

    def _call() -> float | None:
        try:
            cb(*args)
        except Exception as exc:
            log.error("updater callback failed: %s", exc)
        return None  # one-shot

    bpy.app.timers.register(_call, first_interval=0.0)


def check_for_update_async(on_result: Callable[[dict | None], None]) -> None:
    """Fire-and-forget: check_latest_release() on a background thread,
    delivering the result back to on_result() on Blender's main thread."""
    def _worker() -> None:
        release = check_latest_release()
        _schedule_on_main_thread(on_result, release)

    threading.Thread(target=_worker, daemon=True, name="animora-update-check").start()


def perform_update_async(
    release: dict[str, Any],
    *,
    on_progress: Callable[[str], None],
    on_error: Callable[[str], None],
) -> None:
    """Fire-and-forget: download + verify + launch on a background
    thread. `on_progress` reports coarse status text (main thread);
    `on_error` fires (main thread) if any step fails, so the panel can
    show what went wrong instead of the button just doing nothing.

    No on_success callback — full success means Animora quits itself
    (launch_installer_and_quit's own main-thread timer), so there is no
    "after" state to report back to."""
    def _worker() -> None:
        _schedule_on_main_thread(on_progress, "Downloading update…")
        installer = download_and_verify(
            release.get("windows_url", ""), release.get("windows_sha256", ""),
        )
        if installer is None:
            _schedule_on_main_thread(
                on_error,
                "Download or verification failed — see the system console "
                "for details, or download the update manually from "
                "animora.tech/download.",
            )
            return
        _schedule_on_main_thread(on_progress, "Launching installer…")
        # launch_installer_and_quit calls bpy.app.timers.register()
        # internally to hop the actual quit onto the main thread — the
        # register() CALL itself is thread-safe from any thread (same
        # as ws_client.py's background receive loop already relies on),
        # so this can run directly from the worker thread.
        ok = launch_installer_and_quit(installer)
        if not ok:
            _schedule_on_main_thread(
                on_error,
                "Could not launch the installer — see the system console "
                "for details, or run the downloaded installer manually.",
            )

    threading.Thread(target=_worker, daemon=True, name="animora-update-perform").start()
