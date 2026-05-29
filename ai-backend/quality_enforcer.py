"""
Quality Enforcement Layer.

Validates LLM-generated bpy scripts before execution.

Security model
--------------
The script runs INSIDE the user's Animora process via `exec()` in the
addon's operators.py. That's a real security boundary — a malicious or
buggy script can read any file the user can read, talk to the network,
launch subprocesses, etc.  THIS module is the gate.

The defense is a single layer that's airtight on its own:

  **AST analysis (authoritative)** — walks the parse tree, blocking:
    1. Import of any banned module (os, subprocess, pathlib, ctypes, …)
    2. Bare-name call to any banned builtin (eval, exec, getattr, …)
    3. Reference to `__builtins__` by name

  Why no regex pre-filter? Earlier versions had a regex denylist as a
  "fast pre-filter" — in practice it produced false positives that
  blocked legitimate bpy code. Examples that previously failed:
    • `bpy.ops.image.open(filepath=...)` — caught by a word-boundary
      open() regex even though it is not the builtin open().
    • `material.image.open(...)` — same false-positive.
  The AST distinguishes a bare-name call (`open(...)`) from an attribute
  call (`x.open(...)`) without ambiguity, so it gets it right where the
  regex didn't. Removing the redundant regex eliminated a whole class
  of false rejections.

  Why no method-name denylist? Earlier versions blocked method calls by
  name (`.unlink`, `.replace`, `.rename`, `.read_text`, etc.) as
  "defense in depth" against `pathlib.Path(…).unlink()` style file-I/O
  bypasses. But the AST import check ALREADY blocks `import pathlib` —
  so a script literally cannot construct a Path object. The method-name
  check was redundant defense that produced very expensive false
  positives:
    • `collection.objects.unlink(obj)` — STANDARD bpy operation for
      removing an object from a collection (NOT pathlib).
    • `name.replace(' ', '_')` — every-day string method.
    • `bpy.data.objects.rename(...)` — bpy data API.
  None of these are file-I/O. The original method-name block was
  catching the wrong things.

Attack chains and where they break
----------------------------------
  Goal: read /etc/passwd from a script
    `pathlib.Path('/etc/passwd').read_text()`
        → BLOCKED at `import pathlib`
    `open('/etc/passwd').read()`
        → BLOCKED at bare-name call `open()`
    `getattr(__builtins__, 'open')('/etc/passwd')`
        → BLOCKED at bare-name call `getattr()` AND at `__builtins__`
          name reference
    `globals()['__builtins__']['open']('/etc/passwd')`
        → BLOCKED at bare-name call `globals()`
    `__import__('os').open('/etc/passwd', 0)`
        → BLOCKED at bare-name call `__import__()`

  Goal: spawn a subprocess
    `subprocess.run(['ls'])`           → BLOCKED at `import subprocess`
    `os.system('ls')`                  → BLOCKED at `import os`
    `os.popen('ls').read()`            → BLOCKED at `import os`
    `__import__('os').system('ls')`    → BLOCKED at `__import__()`

  Goal: network egress
    `socket.socket(…)`                 → BLOCKED at `import socket`
    `urllib.request.urlopen(…)`        → BLOCKED at `import urllib`
    `httpx.get(…)`                     → BLOCKED at `import httpx`

Resource heuristics (NOT security; UX safety nets):
  - Script length cap (config.max_script_length)
  - Subdivision level cap (≤ 8, otherwise it crashes Blender)
  - Render samples cap (config.max_render_samples)
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass

from .config import settings

log = logging.getLogger("animora.quality")

# Imports that are NEVER OK in an LLM-generated bpy script. Anything that
# can do file I/O, network egress, subprocess spawning, dynamic-import, or
# bytecode loading is here. Standard lib utilities a 3D script legitimately
# needs (math, random, itertools, statistics, json, functools, time, re,
# colorsys, datetime, hashlib, dataclasses, typing) are intentionally NOT
# in here — they don't open new attack surface.
BANNED_IMPORTS = {
    "os", "subprocess", "sys", "shutil", "socket",
    "urllib", "requests", "httpx", "http",
    "pathlib",     # file I/O via Path(...).read_text/write_text/etc.
    "importlib",   # dynamic import
    "ctypes",      # native code loading
    "multiprocessing",  # process spawning
    "threading",   # not exploitable directly but lets a script deadlock the addon
    "asyncio",     # similar — Blender's main thread should never await
    "pickle",      # arbitrary code on unpickle
    "marshal",     # similar
    "code",        # interactive interpreter
    "codeop",      # code compilation helpers
    "runpy",       # runs modules as scripts
    "builtins",    # explicit-import bypass to reach banned builtins
}

# Builtins whose CALL by bare name is never legitimate here. Note: we only
# block bare-name calls. `x.open(...)` and `x.eval(...)` etc. are fine —
# those are attribute calls on user objects, not the builtin functions.
BANNED_CALLS = {
    "open",
    "eval",
    "exec",
    "compile",
    "__import__",
    "getattr",     # bypass: getattr(__builtins__, 'exec')
    "globals",     # bypass: globals()['__builtins__']
    "locals",      # bypass: locals()
    "vars",        # bypass: vars(__builtins__)
    "input",       # interactive prompt would hang the addon
    "breakpoint",  # would drop into a debugger inside Animora
}


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""


def validate_script(script: str) -> ValidationResult:
    """Validate an LLM-generated bpy script. Returns (ok, reason)."""
    # Length cap — protects against runaway output, NOT a security check.
    if len(script) > settings.max_script_length:
        return ValidationResult(
            ok=False,
            reason=f"Script too long ({len(script)} chars, max {settings.max_script_length})",
        )

    # AST parse + walk. Authoritative; no regex pre-filter that produces
    # false positives on legitimate bpy attribute calls.
    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        return ValidationResult(ok=False, reason=f"Syntax error: {exc}")

    for node in ast.walk(tree):
        # ── Banned imports ─────────────────────────────────────────────
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in BANNED_IMPORTS:
                    return ValidationResult(
                        ok=False, reason=f"Banned import: {alias.name}"
                    )
        if isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in BANNED_IMPORTS:
                    return ValidationResult(
                        ok=False, reason=f"Banned import from: {node.module}"
                    )

        # ── Banned bare-name calls (eval, exec, open, getattr, ...) ────
        # Attribute calls like `x.open(...)` are NOT blocked here — those
        # are calls on user objects (bpy collections, etc.), not the
        # builtin functions of the same name.
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in BANNED_CALLS:
                return ValidationResult(
                    ok=False, reason=f"Banned call: {func.id}()"
                )

        # ── Direct reference to __builtins__ ──────────────────────────
        # Catches `__builtins__['exec']`, `__builtins__.exec`, etc.
        if isinstance(node, ast.Name) and node.id == "__builtins__":
            return ValidationResult(
                ok=False, reason="Reference to __builtins__ is not allowed",
            )

    # ── Resource heuristics (NOT security; just sanity limits) ──────────

    # Subdivision level >8 causes RAM exhaustion / Blender crashes.
    subdiv_match = re.search(r"levels\s*=\s*(\d+)", script)
    if subdiv_match:
        levels = int(subdiv_match.group(1))
        if levels > 8:
            return ValidationResult(
                ok=False, reason=f"Subdivision level {levels} too high (max 8)",
            )

    # Render samples cap (default 10,000) — anti-DoS on the GPU.
    samples_match = re.search(r"samples\s*=\s*(\d+)", script)
    if samples_match:
        samples = int(samples_match.group(1))
        if samples > settings.max_render_samples:
            return ValidationResult(
                ok=False,
                reason=f"Render samples {samples} too high (max {settings.max_render_samples})",
            )

    return ValidationResult(ok=True)
