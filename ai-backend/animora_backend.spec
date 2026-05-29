# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the Animora bundled backend (recording build).

Invoked by `scripts/freeze_backend.py`, NOT directly. That script first
copies the hyphenated `ai-backend/` source tree to an importable
`build/backend-build/ai_backend/` and writes a `run_backend.py` entry
point there — so PyInstaller's static analysis can follow real
`import ai_backend.*` statements and discover the transitive dependency
graph (fastapi, anthropic, redis, jose, …) automatically. We then layer on
collectors for the packages whose data files / lazily-imported submodules
PyInstaller's analysis can't see on its own:

  • botocore / boto3 — ship the service-model JSON + endpoint data the
    anthropic Bedrock client's signer pulls in at runtime.
  • uvicorn — protocols.*, lifespan.*, and the websockets/httptools loops
    are imported by string name, so collect every submodule.
  • anthropic, redis, jose, pydantic — collect submodules so nothing the
    app imports lazily goes missing.

One-dir build (not one-file): faster cold start for the addon's auto-launch
and far easier to diagnose if a DLL/data file is missing.
"""

import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

# SPECPATH is the directory containing this spec (ai-backend/). The freeze
# script places the importable copy + entry under build/backend-build/.
_REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))
_BUILD_BUILD = os.path.join(_REPO_ROOT, "build", "backend-build")
_ENTRY = os.path.join(_BUILD_BUILD, "run_backend.py")

hiddenimports: list[str] = []
datas: list = []
binaries: list = []

# Data-bearing / lazily-imported packages PyInstaller can't fully trace.
for pkg in ("botocore", "boto3"):
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

for pkg in ("uvicorn", "anthropic", "redis", "jose", "pydantic",
            "pydantic_settings", "starlette", "fastapi", "websockets"):
    hiddenimports += collect_submodules(pkg)

# The app package itself — make sure every submodule is pulled in even if
# something is imported dynamically (orchestrator wires modules by name).
hiddenimports += collect_submodules("ai_backend")

# anthropic ships a couple of data files (version, tokenizer assets on some
# versions); collect_all is cheap insurance.
_a_d, _a_b, _a_h = collect_all("anthropic")
datas += _a_d
binaries += _a_b
hiddenimports += _a_h


a = Analysis(
    [_ENTRY],
    pathex=[_BUILD_BUILD],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # The recording build never runs these; excluding them slims the
        # bundle and avoids dragging in heavy optional deps.
        "tkinter",
        "matplotlib",
        "numpy.distutils",
        "pytest",
        "IPython",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="animora-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,            # console exe, but the addon spawns it with
                             # CREATE_NO_WINDOW so no window ever appears;
                             # stdout still flows to animora_engine.log.
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="animora-backend",
)
