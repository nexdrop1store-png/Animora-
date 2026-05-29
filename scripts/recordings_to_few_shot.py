"""
recordings_to_few_shot.py — Sprint 4 mining tool.

Extracts (user request → SPEC → bpy script → verdict) triples from
session recordings and renders them as multi-line worked examples
suitable for inclusion in a persona prompt. Used to seed the
Continuous workstream: the recorded sessions become the demonstration
dataset the model learns "the loop" from.

The MCP literature emphasizes this is how AI 3D tools actually improve:
not by tuning weights, but by showing the model 2-3 worked examples
of the desired loop in each persona's system prompt. The eval is the
reward signal; the few-shot examples are the curriculum.

## Usage

    python scripts/recordings_to_few_shot.py \\
        --recordings recordings/cofounder_2026_05/ \\
        --persona environment_artist \\
        --out /tmp/env_artist_few_shot.txt

Then open the .txt, pick the 2-3 strongest examples, paste them into
the relevant persona module (e.g. `ai-backend/personas/environment_artist.py`)
inside the WORKED EXAMPLES section.

## What this is NOT

- This is NOT an auto-trainer. Few-shot examples in prompts steer the
  model in-context — they do not modify Claude's weights.
- This is NOT a quick win. Adding too many or unfocused examples
  bloats the persona prompt and slows down every turn. Pick the BEST
  examples, not just any examples.
- This is NOT a substitute for the eval. The eval is the truth
  source; few-shot examples are how we move the score.

## What makes a good few-shot example

A keeper has all of:
  • A USER MESSAGE that exercises a quality dimension the persona
    currently struggles with (per the eval results).
  • A SPEC that is precise — full lighting plan, palette, composition
    layers. Generic "subject = a thing" briefs aren't worth keeping.
  • A SCRIPT that is BOTH technically correct AND has the polish moves
    we want the model to imitate (modifiers, named objects, varied
    materials, considered composition).
  • A `final_review.verdict == "ship"` from the art director.
  • A reasonable length — under ~3 KB of script content per example.
    Hero-asset scripts (Lambo Urus, etc.) blow the prompt budget if
    pasted whole.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


# Hard cap on per-example script length. Scripts above this get
# elided with a `# ... (truncated for brevity)` marker so the
# persona prompt stays under control.
_SCRIPT_MAX_CHARS = 3000


def _load_turns(recordings_dir: Path) -> list[dict[str, Any]]:
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


def _is_keeper(turn: dict[str, Any], persona_filter: str | None) -> bool:
    if turn.get("outcome") != "success":
        return False
    fr = turn.get("final_review") or {}
    if fr.get("verdict") != "ship":
        return False
    spec = turn.get("spec") or {}
    if not (spec.get("subject") or "").strip():
        return False  # need a populated SPEC
    if persona_filter and turn.get("persona") != persona_filter:
        return False
    # Need at least one execute_blender_script call with non-trivial content
    scripts: list[str] = []
    for it in turn.get("iterations") or []:
        scripts.extend(it.get("scripts_emitted") or [])
    if not any(len(s.strip()) > 100 for s in scripts):
        return False
    return True


def _format_spec(spec: dict[str, Any]) -> str:
    """Render the SPEC as the same text shape the model SAW during the
    turn (via render_spec_for_assistant). Reusing the same rendering
    keeps the few-shot example formally equivalent to live runtime."""
    # Avoid importing the live module — recordings_to_few_shot.py is
    # a standalone tool. Inline a tight equivalent.
    lines: list[str] = []
    if (subject := (spec.get("subject") or "").strip()):
        lines.append(f"SUBJECT: {subject}")
    framing = spec.get("framing") or {}
    if any(framing.values()):
        cam = (framing.get("camera") or "").strip()
        lens = framing.get("lens_mm") or ""
        angle = (framing.get("angle") or "").strip()
        lens_str = f", {lens}mm" if lens else ""
        lines.append(f"FRAMING: {cam}{lens_str} — {angle}")
    lighting = spec.get("lighting") or {}
    if any(lighting.values()):
        tod = (lighting.get("time_of_day") or "").strip()
        mood = (lighting.get("mood") or "").strip()
        lines.append(f"LIGHTING ({tod}, mood: {mood})")
        for slot in ("key", "fill", "rim"):
            val = (lighting.get(slot) or "").strip()
            if val:
                lines.append(f"  - {slot}: {val}")
    palette = spec.get("palette") or {}
    if any(palette.values()):
        dom = palette.get("dominant", "")
        acc = palette.get("accent", "")
        neu = palette.get("neutral", "")
        lines.append(f"PALETTE: {dom} | {acc} | {neu}")
    comp = spec.get("composition") or {}
    if any(comp.values()):
        for layer in ("foreground", "midground", "background"):
            val = (comp.get(layer) or "").strip()
            if val:
                lines.append(f"  - {layer}: {val}")
    materials = spec.get("materials") or []
    if materials:
        lines.append("MATERIALS:")
        for m in materials[:8]:
            on = (m.get("on") or "").strip()
            mtype = (m.get("type") or "").strip()
            lines.append(f"  - {on}: {mtype}")
    scale = (spec.get("scale_notes") or "").strip()
    if scale:
        lines.append(f"SCALE: {scale}")
    return "\n".join(lines)


def _format_example(turn: dict[str, Any], idx: int) -> str:
    """Render one turn as a few-shot block."""
    user = (turn.get("user_message") or "").strip()
    spec = turn.get("spec") or {}
    iterations = turn.get("iterations") or []
    fr = turn.get("final_review") or {}

    # Pick the LONGEST emitted script — most likely the hero output
    # (some iterations have throwaway probe scripts; the final big one
    # is what we want).
    all_scripts: list[str] = []
    for it in iterations:
        all_scripts.extend(it.get("scripts_emitted") or [])
    if not all_scripts:
        return ""
    script = max(all_scripts, key=len)
    if len(script) > _SCRIPT_MAX_CHARS:
        script = script[:_SCRIPT_MAX_CHARS] + "\n# ... (truncated for brevity)\n"

    lines: list[str] = [
        f"### Worked example {idx}",
        "",
        "USER:",
        f"  {user}",
        "",
        "SPEC (the brief the planner produced):",
    ]
    spec_block = _format_spec(spec)
    for ln in spec_block.split("\n"):
        lines.append(f"  {ln}")
    lines.append("")
    lines.append("BPY SCRIPT emitted (execute_blender_script call):")
    lines.append("```python")
    lines.append(script.rstrip())
    lines.append("```")

    verdict = (fr.get("verdict") or "").strip()
    works = (fr.get("what_works") or "").strip()
    lines.append("")
    lines.append(f"ART DIRECTOR VERDICT: {verdict}")
    if works:
        lines.append(f"WHAT WORKS: {works}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--recordings", type=Path, required=True,
        help="Directory containing per-session subdirs of turn_*.json files",
    )
    parser.add_argument(
        "--persona", default="",
        help="Filter to recordings whose persona matches (e.g. environment_artist). Empty = all.",
    )
    parser.add_argument(
        "--out", type=Path, required=True,
        help="Path to write the rendered few-shot file",
    )
    parser.add_argument(
        "--max-examples", type=int, default=6,
        help="Cap on how many examples to emit. 2-3 is the sweet spot for prompts.",
    )
    args = parser.parse_args()

    if not args.recordings.is_dir():
        print(f"ERROR: --recordings is not a directory: {args.recordings}", file=sys.stderr)
        return 2

    persona_filter = args.persona or None
    turns = _load_turns(args.recordings)
    keepers = [t for t in turns if _is_keeper(t, persona_filter)]
    print(f"Loaded {len(turns)} recorded turns; {len(keepers)} eligible for few-shot (persona={persona_filter or 'any'}).")

    chosen = keepers[: args.max_examples]
    body = "\n\n".join(
        _format_example(t, i + 1) for i, t in enumerate(chosen)
    )
    header = (
        f"# Few-shot worked examples extracted from {args.recordings}\n"
        f"# Persona filter: {persona_filter or '(none)'}\n"
        f"# Examples included: {len(chosen)} / {len(keepers)} eligible\n"
        "#\n"
        "# Review each block and pick the 2-3 strongest before pasting\n"
        "# into the persona's WORKED EXAMPLES section. Don't paste them\n"
        "# all — prompt bloat slows every turn.\n"
        "\n"
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(header + body + "\n", encoding="utf-8")
    print(f"Wrote {len(chosen)} few-shot example(s) to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
