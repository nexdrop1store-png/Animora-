"""
Direct test of the intent classifier (no WebSocket needed).

Verifies that the Haiku-powered classifier picks the correct intent +
persona for a battery of domain-specific test messages. This is the
core of Phase 4 — if classification is right, the persona system works.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

# Opt into dev mode so config._enforce_secrets_safety doesn't refuse to
# start when JWT_SECRET is the local dev placeholder. Must come before
# importing ai_backend.config.
os.environ.setdefault("ANIMORA_ENV", "dev")


# Bootstrap the package (this file lives in ai-backend/tests/)
_PKG_DIR = Path(__file__).resolve().parent.parent
if "ai_backend" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "ai_backend", _PKG_DIR / "__init__.py",
        submodule_search_locations=[str(_PKG_DIR)],
    )
    _pkg = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    sys.modules["ai_backend"] = _pkg
    _spec.loader.exec_module(_pkg)  # type: ignore[union-attr]

from ai_backend.anthropic_client import AnthropicClient
from ai_backend.config import settings
from ai_backend.observability import configure
from ai_backend.orchestrator.intent import classify


# Each tuple: (user_message, expected_persona, expected_intent_set, name)
TEST_CASES = [
    ("Make a misty pine forest with scattered trees and rolling hills.",
     "environment_artist", {"dense_scene", "terrain_landscape"},
     "Forest environment"),

    ("Create a beach scene with palm trees and water.",
     "environment_artist", {"dense_scene"},
     "Beach environment"),

    ("Build a modular sci-fi corridor with panel detail.",
     "environment_artist", {"architecture", "dense_scene"},
     "Architecture"),

    ("Model a sci-fi pistol with panel lines and emissive accents.",
     "hard_surface_artist", {"hard_surface_model"},
     "Sci-fi weapon"),

    ("Create a stylized car body with chrome trim.",
     "hard_surface_artist", {"hard_surface_model"},
     "Vehicle"),

    ("Light my scene for a moody cinematic dusk.",
     "lighting_td", {"lighting_setup"},
     "Lighting setup"),

    ("Set up Cycles render at 256 samples with denoising for the final shot.",
     "lighting_td", {"render_setup", "lighting_setup"},
     "Render config"),

    ("Build a PBR shader for brushed aluminum.",
     "lighting_td", {"material_authoring"},
     "PBR material"),

    ("What's the difference between subsurface and translucency in Cycles?",
     "generalist", {"question"},
     "Q&A"),

    ("Move the cube to z=2.",
     "generalist", {"simple_edit"},
     "Simple edit"),

    ("Rig this character with IK arms.",
     "generalist", {"rig_setup"},
     "Rigging (not-yet-shipped → generalist)"),
]


async def main() -> int:
    configure("WARNING")  # quiet the noise — we'll print results ourselves

    if not settings.anthropic_api_key:
        print("FAIL: no ANTHROPIC_API_KEY in .env")
        return 1

    client = AnthropicClient(settings.anthropic_api_key, session_id="phase4-classifier")

    print("=" * 78)
    print("Phase 4 — Intent classifier accuracy")
    print("=" * 78)
    print()

    passes = 0
    fails = []
    total_elapsed = 0

    for message, expected_persona, expected_intents, name in TEST_CASES:
        result = await classify(
            user_message=message,
            anthropic_client=client,
            scene_summary="(empty scene)",
            recent_context="",
        )

        persona_ok = result.recommended_persona == expected_persona
        intent_ok = result.intent in expected_intents
        verdict = "PASS" if (persona_ok and intent_ok) else "FAIL"

        total_elapsed += result.elapsed_ms

        marker = "OK " if verdict == "PASS" else "XX "
        print(f"{marker} {name}")
        print(f"     msg:    {message[:70]!r}")
        print(f"     intent: {result.intent} (confidence={result.confidence:.2f}, complexity={result.complexity_estimate:.2f})")
        print(f"     persona:{result.recommended_persona:25}  (expected {expected_persona})")
        print(f"     elapsed:{result.elapsed_ms}ms")
        if not persona_ok or not intent_ok:
            print(f"     reason: {result.rationale}")
        print()

        if verdict == "PASS":
            passes += 1
        else:
            fails.append(name)

    print("=" * 78)
    n = len(TEST_CASES)
    print(f"Results: {passes}/{n} pass ({100*passes//n}%)")
    print(f"Total classification time: {total_elapsed} ms ({total_elapsed/n:.0f} ms/call avg)")
    if fails:
        print(f"Failures: {fails}")
    print("=" * 78)
    return 0 if passes == n else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
