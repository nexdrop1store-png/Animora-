"""
Bug 3b/3c regression — the recording-build engine must be spawned fully
detached from Animora's console + process group.

Root cause (recording build only): _spawn_backend used CREATE_NO_WINDOW,
which merely HIDES the child window while leaving it in Animora's console
session + process group. A console control event (CTRL_CLOSE_EVENT) to that
shared console is broadcast to every attached process, so closing the
engine's console also signalled Animora — "closing that window kills
Animora." The fix spawns with DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
(no shared console, own process group), matching updater.py's proven combo.

bundle.py imports bpy only inside functions, so _spawn_backend is importable
and testable in plain Python (no live Blender needed).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest import mock

_ADDON_DIR = Path(__file__).resolve().parent.parent / "animora_panel"

_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000


def _load_bundle():
    spec = importlib.util.spec_from_file_location(
        "animora_panel_bundle", _ADDON_DIR / "bundle.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def test_spawn_backend_detaches_console_and_process_group_on_win32():
    bundle = _load_bundle()
    with mock.patch.object(bundle.sys, "platform", "win32"), \
         mock.patch.object(bundle.subprocess, "Popen") as popen:
        bundle._spawn_backend(Path("C:/Animora/engine/animora-backend.exe"))

    assert popen.called
    flags = popen.call_args.kwargs["creationflags"]
    # Isolated: no shared console, own process group.
    assert flags & _DETACHED_PROCESS
    assert flags & _CREATE_NEW_PROCESS_GROUP
    # The old, insufficient flag must NOT be used (it's mutually exclusive
    # with DETACHED_PROCESS and only hid the window).
    assert not (flags & _CREATE_NO_WINDOW)


def test_spawn_backend_no_special_flags_off_win32():
    bundle = _load_bundle()
    with mock.patch.object(bundle.sys, "platform", "linux"), \
         mock.patch.object(bundle.subprocess, "Popen") as popen:
        bundle._spawn_backend(Path("/opt/animora/engine/animora-backend"))

    assert popen.called
    assert popen.call_args.kwargs["creationflags"] == 0


def test_spawn_backend_stdio_silenced():
    # Regression guard: stdout/stderr must stay DEVNULL so a detached
    # engine can never block on a full pipe with no reader.
    bundle = _load_bundle()
    with mock.patch.object(bundle.sys, "platform", "win32"), \
         mock.patch.object(bundle.subprocess, "Popen") as popen:
        bundle._spawn_backend(Path("C:/Animora/engine/animora-backend.exe"))

    assert popen.call_args.kwargs["stdout"] == bundle.subprocess.DEVNULL
    assert popen.call_args.kwargs["stderr"] == bundle.subprocess.DEVNULL
