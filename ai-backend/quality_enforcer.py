"""
Quality Enforcement Layer.

Validates LLM-generated bpy scripts before execution:
- Static security analysis (banned imports/calls)
- Geometry runaway detection
- Render abuse detection
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass

from .config import settings

log = logging.getLogger("animora.quality")

BANNED_IMPORTS = {"os", "subprocess", "sys", "shutil", "socket", "urllib", "requests", "httpx"}
BANNED_CALLS = {"open", "__import__", "eval", "exec", "compile", "importlib"}
BANNED_PATTERNS = [
    r"import\s+os\b",
    r"import\s+subprocess\b",
    r"__import__\s*\(",
    r"open\s*\(",
    r"socket\.",
]


@dataclass
class ValidationResult:
    ok: bool
    reason: str = ""


def validate_script(script: str) -> ValidationResult:
    # Length check
    if len(script) > settings.max_script_length:
        return ValidationResult(ok=False, reason=f"Script too long ({len(script)} chars, max {settings.max_script_length})")

    # Regex pattern scan (fast path before AST)
    for pattern in BANNED_PATTERNS:
        if re.search(pattern, script):
            return ValidationResult(ok=False, reason=f"Banned pattern: {pattern}")

    # AST analysis
    try:
        tree = ast.parse(script)
    except SyntaxError as exc:
        return ValidationResult(ok=False, reason=f"Syntax error: {exc}")

    for node in ast.walk(tree):
        # Banned imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in BANNED_IMPORTS:
                    return ValidationResult(ok=False, reason=f"Banned import: {alias.name}")
        if isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in BANNED_IMPORTS:
                return ValidationResult(ok=False, reason=f"Banned import from: {node.module}")

        # Banned calls
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in BANNED_CALLS:
                return ValidationResult(ok=False, reason=f"Banned call: {node.func.id}()")

    # Heuristic: detect runaway geometry (subdivide > threshold)
    subdiv_match = re.search(r"levels\s*=\s*(\d+)", script)
    if subdiv_match:
        levels = int(subdiv_match.group(1))
        if levels > 8:
            return ValidationResult(ok=False, reason=f"Subdivision level {levels} too high (max 8)")

    # Heuristic: detect abusive render samples
    samples_match = re.search(r"samples\s*=\s*(\d+)", script)
    if samples_match:
        samples = int(samples_match.group(1))
        if samples > settings.max_render_samples:
            return ValidationResult(ok=False, reason=f"Render samples {samples} too high (max {settings.max_render_samples})")

    return ValidationResult(ok=True)
