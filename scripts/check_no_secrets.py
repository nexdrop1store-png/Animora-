"""
Secrets gate — fail the build if any credential would ship to users.

Run this against the tree that's about to be packaged into the PUBLIC
installer. It scans for API keys, AWS bearer tokens, private keys, and
stray .env files and exits non-zero (with the offending paths) if any are
found. Wire it into the build right before the Inno/packaging step:

    python scripts/check_no_secrets.py build/windows/animora-stage
    python scripts/check_no_secrets.py build/backend-dist   # if a bundle exists

The #1 production rule: the pooled Bedrock/Anthropic key lives ONLY on the
server. The desktop app reaches the cloud backend over an authenticated
WebSocket; it must never carry the key. The recording build (which DOES
embed a key for offline capture) is a cofounder-only artifact — never the
public release. This gate enforces that mechanically.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Patterns that must never appear in a shipped file.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[bytes]]] = [
    ("Bedrock API key (ABSK...)", re.compile(rb"ABSK[A-Za-z0-9+/=]{16,}")),
    ("Anthropic API key (sk-ant-...)", re.compile(rb"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("AWS bearer-token env", re.compile(rb"AWS_BEARER_TOKEN_BEDROCK\s*=\s*\S")),
    ("AWS secret access key", re.compile(rb"aws_secret_access_key\s*=\s*\S", re.IGNORECASE)),
    ("Private key block", re.compile(rb"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----")),
]

# Filenames that should never ship regardless of content.
_FORBIDDEN_NAMES = {".env", ".env.local", ".env.production", ".env.staging",
                    "animora_backend.env"}

# Binary/asset extensions we skip (won't contain text secrets; speeds scan).
_SKIP_EXT = {".exe", ".dll", ".pyd", ".so", ".dylib", ".blend", ".png", ".jpg",
             ".exr", ".dat", ".ico", ".icns", ".zip", ".7z", ".pdb", ".bin",
             ".ttf", ".woff2", ".pak", ".lib", ".obj"}


def scan(root: Path) -> list[str]:
    findings: list[str] = []
    if not root.exists():
        return findings
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if p.name in _FORBIDDEN_NAMES:
            findings.append(f"{p}  ->  forbidden file ({p.name} must never ship)")
            continue
        if p.suffix.lower() in _SKIP_EXT:
            continue
        try:
            data = p.read_bytes()[:1_000_000]  # cap per-file read
        except OSError:
            continue
        for label, pat in _SECRET_PATTERNS:
            if pat.search(data):
                findings.append(f"{p}  ->  {label}")
                break
    return findings


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: check_no_secrets.py <dir> [<dir> ...]", file=sys.stderr)
        return 2
    all_findings: list[str] = []
    for arg in argv[1:]:
        all_findings += scan(Path(arg))
    if all_findings:
        print("SECRETS GATE FAILED — these would ship to users:\n", file=sys.stderr)
        for f in all_findings:
            print(f"  ✗ {f}", file=sys.stderr)
        print("\nThe public installer must contain NO credentials. If this is the "
              "cofounder recording build, package it separately — never publish it.",
              file=sys.stderr)
        return 1
    print("Secrets gate passed: no credentials found in the shipped tree.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
