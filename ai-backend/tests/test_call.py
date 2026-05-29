"""
End-to-end smoke test of the Animora Claude integration.

Exercises the REAL `AnthropicClient` wrapper (the production path —
retry, timeout, cancel, token tracking, structured logging) with the
key from `.env`. Confirms the AI panel's eventual call path will work.

Run:
    cd ai-backend
    python -m test_call

(Or from the repo root: `python -m ai_backend.test_call` once the package
is importable that way.)

Prints:
  • Anthropic identity check (Haiku ping)
  • A streamed response to a sample user message using Sonnet, with the
    full master prompt + scene context attached (same path the WS uses)
  • Token usage stats from the production wrapper
  • Cache-hit ratio (will be 0 on first call; >0 on subsequent within 5min)

Does NOT require Redis, FastAPI, or the addon to be running. Just the
key in .env and an internet connection to Anthropic.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

# Opt into dev mode so config._enforce_secrets_safety allows the local
# dev JWT placeholder. Must come before importing ai_backend.config.
os.environ.setdefault("ANIMORA_ENV", "dev")

# The directory name has a hyphen ("ai-backend") which Python's normal
# import machinery rejects. Bootstrap it as `ai_backend` (underscore) via
# importlib so the rest of this file uses clean module paths.
# This file lives in ai-backend/tests/, so PKG_DIR is __file__.parent.parent.
_PKG_DIR = Path(__file__).resolve().parent.parent
if "ai_backend" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "ai_backend", _PKG_DIR / "__init__.py",
        submodule_search_locations=[str(_PKG_DIR)],
    )
    _pkg = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    sys.modules["ai_backend"] = _pkg
    _spec.loader.exec_module(_pkg)  # type: ignore[union-attr]

from ai_backend.anthropic_client import AnthropicClient, StreamCancelled, fingerprint_key
from ai_backend.config import settings
from ai_backend.observability import configure, logger
from ai_backend.orchestrator.context_builder import build as build_ctx
from ai_backend.orchestrator.personas import GENERALIST


SAMPLE_SCENE = {
    "scene_name": "Untitled",
    "frame_current": 1,
    "mode": "OBJECT",
    "active_object": "Cube",
    "objects": [
        {
            "name": "Cube", "type": "MESH",
            "location": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1],
            "visible": True, "selected": True,
            "modifiers": [], "materials": ["Material"],
            "vertex_count": 8, "polygon_count": 6,
        },
        {
            "name": "Camera", "type": "CAMERA",
            "location": [7.36, -6.93, 4.96], "rotation": [1.11, 0, 0.81], "scale": [1, 1, 1],
            "visible": True, "selected": False, "modifiers": [],
        },
        {
            "name": "Light", "type": "LIGHT",
            "location": [4.08, 1.01, 5.90], "rotation": [0.65, 0.06, -1.87], "scale": [1, 1, 1],
            "visible": True, "selected": False, "modifiers": [],
        },
    ],
    "render": {"engine": "CYCLES", "resolution_x": 1920, "resolution_y": 1080},
}


async def main() -> int:
    configure("INFO")
    log = logger("animora.test_call")

    key = settings.anthropic_api_key
    if not key:
        print("FAIL: ANTHROPIC_API_KEY not set in .env (or env vars)")
        return 1

    print(f"=== Animora Claude integration — end-to-end test ===")
    print(f"Key fingerprint: {fingerprint_key(key)}")
    print()

    client = AnthropicClient(key, session_id="test_call")

    # ── Test 1: validate (lightweight Haiku ping) ──────────────────────
    print("Test 1: validate() — Haiku ping…")
    result = await client.validate()
    if result.ok:
        print(f"  OK in {result.elapsed_ms} ms (model: {result.model_pinged})")
    else:
        print(f"  FAIL: [{result.error_code}] {result.error_message}")
        return 1
    print()

    # ── Test 2: real streamed Sonnet call with master prompt + scene ──
    print("Test 2: stream() — Sonnet 4.6 with master prompt + sample scene…")
    print("        (using the same context_builder the WS endpoint uses)")
    print()

    ctx_kwargs = build_ctx(
        user_message="Quickly introduce yourself in one sentence, in character.",
        conversation_history=[],
        scene_graph=SAMPLE_SCENE,
        prev_scene_graph=None,
        persona=GENERALIST,
        hd_capture=None,
    )
    ctx_kwargs.pop("_meta")

    print("Response (streaming):")
    print("  " + "-" * 70)
    print("  | ", end="", flush=True)

    output_chars = [0]

    async def on_token(t: str) -> None:
        sys.stdout.write(t)
        sys.stdout.flush()
        output_chars[0] += len(t)
        if t.endswith("\n"):
            sys.stdout.write("  | ")
            sys.stdout.flush()

    try:
        stream_result = await client.stream(
            model="claude-sonnet-4-6",
            max_tokens=200,
            on_token=on_token,
            **ctx_kwargs,
        )
    except StreamCancelled:
        print("\n  [CANCELLED]")
        return 1
    except Exception as exc:
        print(f"\n  FAIL: {type(exc).__name__}: {exc}")
        return 1

    print()
    print("  " + "-" * 70)
    print()

    print("Stream result:")
    print(f"  model:        {stream_result.model}")
    print(f"  stop_reason:  {stream_result.stop_reason}")
    print(f"  elapsed:      {stream_result.elapsed_ms} ms")
    print(f"  attempts:     {stream_result.attempts}")
    print(f"  tokens:       in={stream_result.usage.input_tokens}, out={stream_result.usage.output_tokens}")
    print(f"  cache:        created={stream_result.usage.cache_creation_input_tokens}, "
          f"read={stream_result.usage.cache_read_input_tokens}, "
          f"hit_ratio={stream_result.usage.cache_hit_ratio:.2%}")
    print(f"  output_chars: {output_chars[0]}")
    print()

    # ── Brand check ─────────────────────────────────────────────────────
    response_text = stream_result.output_text.lower()
    is_animora = "animora" in response_text
    leaks_claude = "i am claude" in response_text or "i'm claude" in response_text
    leaks_anthropic = "anthropic" in response_text

    print("Brand check:")
    print(f"  identifies as Animora: {is_animora}")
    print(f"  leaks 'Claude' name:   {leaks_claude}")
    print(f"  leaks 'Anthropic':     {leaks_anthropic}")
    if is_animora and not leaks_claude and not leaks_anthropic:
        print("  PASS — response is in-character as Animora AI")
    else:
        print("  WARN — branding leak detected. May need to tighten master prompt.")
    print()

    print("=== End-to-end test complete ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
