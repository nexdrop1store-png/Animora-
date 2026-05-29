"""
recordings_to_benchmarks.py — Sprint 4 mining tool.

Reads a directory of session recordings (produced by the
ANIMORA_RECORD_SESSIONS=1 path in ai-backend/recorder.py) and emits
DRAFT `Benchmark(...)` entries — one per turn whose `final_review.verdict
== "ship"` — that can be pasted into ai-backend/eval/benchmarks.py
after human review.

The output is intentionally a draft. Every entry needs:

  • A human eyeball pass to confirm the regex patterns match what the
    model actually does on a re-run (the model isn't deterministic).
  • A sensible benchmark name chosen by the operator (the auto-generated
    one is just a hint).
  • Confirmation that the user_message is non-PII and the scene is
    safe to share as eval material.

Usage:

    python scripts/recordings_to_benchmarks.py \\
        --recordings recordings/cofounder_2026_05/ \\
        --out /tmp/draft_benchmarks.py

Then open the .py file, review each Benchmark(...), copy the keepers
into ai-backend/eval/benchmarks.py, and re-baseline.

## Why drafts, not auto-applied

The eval is the regression gate. An auto-added benchmark that happens
to pass the first time it runs would lock in whatever the model
produced on THAT particular call — including any quirks. A human pass
removes the worst of the noise. Cost is small: ~30 seconds per draft
to confirm or discard.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


# Token patterns that tell us the model is doing structured work
# worth capturing as a benchmark requirement. Each captured pattern
# becomes a required_op regex.
#
# Order matters — the first match wins for a given line, so we list
# the most specific patterns first.
_OP_SIGNATURES: tuple[tuple[str, str], ...] = (
    (r"primitive_cube_add\(",     r"primitive_cube_add\\("),
    (r"primitive_uv_sphere_add\(", r"primitive_uv_sphere_add\\("),
    (r"primitive_ico_sphere_add\(", r"primitive_ico_sphere_add\\("),
    (r"primitive_cylinder_add\(", r"primitive_cylinder_add\\("),
    (r"primitive_cone_add\(",     r"primitive_cone_add\\("),
    (r"primitive_torus_add\(",    r"primitive_torus_add\\("),
    (r"primitive_plane_add\(",    r"primitive_plane_add\\("),
    (r"bpy\.data\.lights\.new\(", r"bpy\\.data\\.lights\\.new\\("),
    (r"modifiers\.new\(",         r"modifiers\\.new\\("),
    (r"materials\.new\(",         r"materials\\.new\\("),
    (r"ShaderNodeBsdfPrincipled", r"ShaderNodeBsdfPrincipled"),
    (r"ShaderNodeTexEnvironment", r"ShaderNodeTexEnvironment"),
)


def _extract_required_ops(scripts: list[str]) -> list[str]:
    """Find the bpy operators / API calls the model actually used.
    Returns a list of regex patterns (matching the Benchmark.required_ops
    convention). Dedups to avoid noise."""
    if not scripts:
        return []
    joined = "\n".join(scripts)
    seen: set[str] = set()
    out: list[str] = []
    for needle, pattern in _OP_SIGNATURES:
        if re.search(needle, joined):
            if pattern not in seen:
                seen.add(pattern)
                out.append(pattern)
    return out


def _extract_tool_use_signatures(tool_use_names_per_iter: list[list[str]]) -> list[str]:
    """If the model used a high-signal tool (use_asset, load_asset),
    capture the call signature as a required_op alternative pattern.
    The benchmark scorer's runner adds non-script tool calls to its
    scoring text, so `use_asset\\(` matches even when no bpy script
    was emitted (asset-first path)."""
    flat = {name for it in tool_use_names_per_iter for name in it}
    patterns: list[str] = []
    if "use_asset" in flat or "load_asset" in flat:
        patterns.append(r"use_asset\\(|load_asset\\(")
    return patterns


def _slug(text: str) -> str:
    """Generate a short slug from the user_message for use as the
    benchmark.name suffix. Keeps the first 30 chars of alphanum tokens."""
    tokens = re.findall(r"[a-zA-Z0-9]+", text.lower())
    slug = "_".join(tokens[:5])[:30]
    return slug or "unnamed"


def _category_from_persona(persona: str) -> str:
    """Map persona id → benchmark category prefix. Mirrors the
    existing convention in ai-backend/eval/benchmarks.py."""
    return {
        "environment_artist": "scene",
        "hard_surface_artist": "hard_surface",
        "lighting_td": "lighting",
        "character_artist": "character",
        "generalist": "general",
    }.get(persona, "misc")


def _budget_for(scripts: list[str]) -> int:
    """Pick a sensible token budget from the actual script length(s)
    the model emitted. Adds a 30 % headroom so the next eval run isn't
    immediately over-budget on natural variation."""
    if not scripts:
        return 2000
    longest = max(len(s) for s in scripts)
    # ~3.5 chars per output token
    est_tokens = longest // 3
    return int(est_tokens * 1.3) + 500


def _render_benchmark(turn: dict[str, Any]) -> str:
    """Return a Python source-literal `Benchmark(...)` block for one
    recorded turn. Trailing comments call out fields that need human
    review before merging."""
    user = turn.get("user_message", "").strip()
    persona = turn.get("persona", "")
    intent = turn.get("intent", "")
    iterations = turn.get("iterations", []) or []
    scripts: list[str] = []
    tool_uses: list[list[str]] = []
    for it in iterations:
        scripts.extend(it.get("scripts_emitted") or [])
        tool_uses.append(it.get("tool_use_names") or [])

    required_ops = _extract_required_ops(scripts)
    required_ops += _extract_tool_use_signatures(tool_uses)

    spec = turn.get("spec") or {}
    material_present = any(
        (m.get("type") or "").strip() for m in (spec.get("materials") or [])
    )

    name = f"{_category_from_persona(persona)}.{_slug(user)}"

    lines = ["    Benchmark("]
    lines.append(f"        name={name!r},")
    lines.append(f"        prompt={user!r},")
    lines.append(f"        expected_intent=frozenset({{{intent!r}}}),")
    if required_ops:
        ops_lines = ",\n            ".join(repr(r) for r in required_ops)
        lines.append(f"        required_ops=(\n            {ops_lines},\n        ),")
    else:
        lines.append("        required_ops=(),")
    lines.append("        forbidden_ops=(),")
    lines.append(f"        required_named={bool(scripts)!s},  # only enforce when a script was emitted")
    lines.append(f"        require_material={material_present!s},  # heuristic from SPEC.materials")
    lines.append(f"        budget_tokens={_budget_for(scripts)},")
    notes = (
        f"DRAFT from recording {turn.get('started_at','')} — turn_index={turn.get('turn_index','?')}. "
        "Review regex patterns + required_named/material before merging."
    )
    lines.append(f"        notes={notes!r},")
    lines.append("    ),")
    return "\n".join(lines)


def _load_turns(recordings_dir: Path) -> list[dict[str, Any]]:
    """Load every turn_*.json under any session subdir, in
    session+turn order. Bad JSON is skipped with a warning."""
    out: list[dict[str, Any]] = []
    for session_dir in sorted(recordings_dir.iterdir()):
        if not session_dir.is_dir():
            continue
        for jf in sorted(session_dir.glob("turn_*.json")):
            try:
                out.append(json.loads(jf.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError) as exc:
                print(f"  WARN  skipping {jf}: {exc}", file=sys.stderr)
    return out


def _filter_keepers(turns: list[dict[str, Any]], *, require_ship: bool) -> list[dict[str, Any]]:
    """Drop turns that aren't suitable benchmark material:

    - outcome != "success" — broken or cancelled turns aren't reference
    - require_ship: the final_review must have come back "ship" — only
      art-director-approved turns become benchmarks
    - user_message is empty — defensive
    """
    keep: list[dict[str, Any]] = []
    for t in turns:
        if t.get("outcome") != "success":
            continue
        if not (t.get("user_message") or "").strip():
            continue
        if require_ship:
            fr = t.get("final_review") or {}
            if fr.get("verdict") != "ship":
                continue
        keep.append(t)
    return keep


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--recordings", type=Path, required=True,
        help="Directory containing per-session subdirs of turn_*.json files",
    )
    parser.add_argument(
        "--out", type=Path, required=True,
        help="Path to write the DRAFT Python file (review before pasting)",
    )
    parser.add_argument(
        "--include-all",
        action="store_true",
        help="Include turns whose final_review wasn't 'ship' (default: only ship)",
    )
    args = parser.parse_args()

    if not args.recordings.is_dir():
        print(f"ERROR: --recordings is not a directory: {args.recordings}", file=sys.stderr)
        return 2

    turns = _load_turns(args.recordings)
    keepers = _filter_keepers(turns, require_ship=not args.include_all)
    print(f"Loaded {len(turns)} recorded turns; {len(keepers)} eligible for benchmark draft.")

    body = "\n\n".join(_render_benchmark(t) for t in keepers)
    text = (
        '"""\n'
        f"DRAFT benchmarks auto-extracted by scripts/recordings_to_benchmarks.py.\n"
        f"Source: {args.recordings}\n"
        f"Eligible turns: {len(keepers)} / {len(turns)} total\n"
        '\n'
        "REVIEW each entry, edit the name + required_ops, then paste the\n"
        "keepers into ai-backend/eval/benchmarks.py. Re-baseline after.\n"
        '"""\n'
        "\n"
        "from ai_backend.eval.benchmarks import Benchmark  # noqa\n"
        "\n"
        "DRAFT_BENCHMARKS = (\n"
        f"{body}\n"
        ")\n"
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"Wrote {len(keepers)} draft benchmark(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
