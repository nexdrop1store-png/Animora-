"""
ROOT-CAUSE regression guard — persona + composition rules MUST reach the model.

context_builder.build() used to inject the persona extension and the shared
composition rules by string-replacing the literal anchor "CURRENT SCENE"
inside MASTER_PROMPT. Master prompt v20 deleted that heading, so the replace
became a SILENT NO-OP: from v20 until this test was written, **not one word
of any persona extension or of COMPOSITION_RULES was ever sent to the model.**

Everything the personas encode — the worked wooden-chair / sofa / floor-lamp
examples, the "10+ distinct named parts" hero bar, the bevel rule, "a lamp
without a lit bulb is a failed build" — was dark. That single bug accounts
for the eval failures (floor_lamp with 0 lights, sideboard with 9 parts,
grounded_furniture with raw primitives, shelf.industrial missing metallic=1.0).

These tests fail loudly if persona text ever stops reaching the system prompt
again, regardless of how the prompt is assembled.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

os.environ.setdefault("ANIMORA_ENV", "dev")
os.environ.setdefault("ANIMORA_LLM_PROVIDER", "anthropic")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-placeholder")

_PKG_DIR = Path(__file__).resolve().parent.parent
if "ai_backend" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "ai_backend", _PKG_DIR / "__init__.py",
        submodule_search_locations=[str(_PKG_DIR)],
    )
    _pkg = importlib.util.module_from_spec(_spec)
    sys.modules["ai_backend"] = _pkg
    _spec.loader.exec_module(_pkg)  # type: ignore[union-attr]

from ai_backend.orchestrator.context_builder import build  # noqa: E402
from ai_backend.orchestrator.personas import all_personas  # noqa: E402
from ai_backend.prompts.composition_rules import COMPOSITION_RULES  # noqa: E402


def _system_text(persona) -> str:
    """Flatten build()'s system prompt (string or content-block list)."""
    out = build(
        user_message="build a wooden chair",
        conversation_history=[],
        scene_graph={},
        prev_scene_graph=None,
        persona=persona,
    )
    system = out.get("system")
    if isinstance(system, str):
        return system
    return "".join(block.get("text", "") for block in (system or []))


def test_every_persona_extension_reaches_the_model():
    """The load-bearing guard. Each persona's own text must be present."""
    failures = []
    for persona in all_personas():
        extension = (persona.extension or "").strip()
        if not extension:
            continue  # a persona with no extension has nothing to deliver
        # Use a distinctive line from the persona's own text as the sentinel.
        sentinel = extension.splitlines()[0].strip()
        if len(sentinel) < 12:  # too generic to be a reliable marker
            sentinel = extension[:80].strip()
        if sentinel not in _system_text(persona):
            failures.append(persona.display_name)
    assert not failures, (
        "persona extension NOT delivered to the model for: "
        f"{failures} — the prompt assembly dropped it (this is the v20 "
        "'CURRENT SCENE' anchor bug class)."
    )


def test_composition_rules_reach_the_model():
    persona = all_personas()[0]
    text = _system_text(persona)
    marker = COMPOSITION_RULES.strip().splitlines()[0].strip()
    assert marker in text, (
        "COMPOSITION_RULES not delivered to the model — shared taste rules "
        "are dark."
    )


def test_hard_surface_worked_examples_reach_the_model():
    """The concrete examples are the highest-value quality content in the
    repo (exact tool sequences, part counts, numeric material values). If
    they don't ship, hero builds regress to grey blockouts."""
    hard_surface = next(
        (p for p in all_personas() if "hard" in p.display_name.lower()), None,
    )
    assert hard_surface is not None, "hard-surface persona missing"
    text = _system_text(hard_surface)
    # The floor-lamp example exists specifically so a lamp ships WITH a light.
    assert "create_light" in text, (
        "hard-surface worked examples (incl. the floor-lamp example whose "
        "whole point is a mandatory create_light) are not reaching the model."
    )


def test_master_prompt_still_present():
    """Sanity: the persona fix must not displace the master prompt."""
    text = _system_text(all_personas()[0])
    assert "ANIMORA" in text.upper()
    assert len(text) > 5000, "system prompt suspiciously short"
