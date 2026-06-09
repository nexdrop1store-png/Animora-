#!/usr/bin/env python3
"""
Standalone animora:// forwarder.

The OS launches this (registered by deep_link.register_scheme) when the
browser hits animora://auth/callback?code=..&state=.. . It does ONE thing:
atomically write the callback URL to ~/.animora/auth_callback.txt and exit,
so the already-running Animora addon can pick it up on its poll tick.

Deliberately self-contained — NO addon/bpy imports — because the OS runs it
as a fresh, isolated process with no knowledge of the addon's environment.
"""

import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    # Scan argv for the animora:// URL. It is argv[1] when launched via a
    # plain Python interpreter, or somewhere after "--" when launched via the
    # Blender binary's `--background --python this.py -- <url>` fallback.
    url = ""
    for arg in sys.argv[1:]:
        if arg.strip().startswith("animora://"):
            url = arg.strip()
            break
    if not url:
        return 1
    target = Path.home() / ".animora" / "auth_callback.txt"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".cb_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(url)
        os.replace(tmp, target)  # atomic
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
