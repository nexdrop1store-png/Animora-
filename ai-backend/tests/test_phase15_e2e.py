"""
Phase 1→5 end-to-end validation.

Connects to the dev_server, sends real prompts as the addon would, and
validates that the full pipeline produces sensible output:

  Phase 1: master prompt is loaded, AI responds in-character as Animora
  Phase 2: scene-graph context flows through
  Phase 3: validator-cleared scripts dispatch
  Phase 4: intent classifier routes to the right persona; persona-specific
           vocabulary appears in responses
  Phase 5: tool_calls carry intent_summary; quality notice may arrive

For each scenario we collect:
  • all stream tokens
  • all tool_call frames + their scripts
  • any quality_notice frames
  • timing

Then we check the bpy script in the tool_call would actually do what
was asked. We don't execute it (that needs Blender) — but the validator
checks like "did the script touch the right bpy.data namespace, did it
create the right object type, did it set the right attributes" tell us
whether the AI is responding correctly to the prompt's intent.

Run with dev_server already up:
    cd ai-backend
    python tests/test_phase15_e2e.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

os.environ.setdefault("ANIMORA_ENV", "dev")

# Bootstrap
_PKG_DIR = Path(__file__).resolve().parent.parent
if "ai_backend" not in sys.modules:
    _spec = importlib.util.spec_from_file_location(
        "ai_backend", _PKG_DIR / "__init__.py",
        submodule_search_locations=[str(_PKG_DIR)],
    )
    _pkg = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
    sys.modules["ai_backend"] = _pkg
    _spec.loader.exec_module(_pkg)  # type: ignore[union-attr]


def _load_key() -> str:
    env = _PKG_DIR / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.startswith("ANTHROPIC_API_KEY="):
            return line.split("=", 1)[1].strip()
    return ""


@dataclass
class ScenarioResult:
    name: str
    response_text: str = ""
    tool_calls: list[dict] = field(default_factory=list)
    quality_notice: dict | None = None
    elapsed_ms: int = 0
    error: str = ""

    def first_script(self) -> str:
        for tc in self.tool_calls:
            inp = tc.get("input", {}) or {}
            if tc.get("tool") == "execute_blender_script":
                return inp.get("script", "")
        return ""

    def first_intent_summary(self) -> str:
        for tc in self.tool_calls:
            inp = tc.get("input", {}) or {}
            if "intent_summary" in inp:
                return inp["intent_summary"]
        return ""


# ── Scenarios ──────────────────────────────────────────────────────────
# Each scenario validates a specific phase capability via real LLM call.

SCENARIOS = [
    {
        "name": "S1 — Create a cube (basic generation)",
        "prompt": "Create a single cube at the world origin.",
        "checks": [
            ("text_response", lambda r: len(r.response_text) > 10,
             "Animora responded with text"),
            ("tool_dispatched", lambda r: any(
                tc.get("tool") == "execute_blender_script" for tc in r.tool_calls),
             "execute_blender_script tool was called"),
            ("script_creates_cube", lambda r: "primitive_cube_add" in r.first_script()
                or ("Cube" in r.first_script() and "bpy.data" in r.first_script()),
             "Script creates a cube via bpy"),
            ("has_intent_summary", lambda r: len(r.first_intent_summary()) > 3,
             "tool_call carries an intent_summary"),
        ],
    },
    {
        "name": "S2 — Add a light (lighting persona)",
        "prompt": "Add a sun light to the scene aimed at the cube.",
        "checks": [
            ("text_response", lambda r: len(r.response_text) > 10, "Got a response"),
            ("script_creates_light", lambda r: any(
                kw in r.first_script().lower()
                for kw in ("light_add", "type='sun'", 'type="sun"', "lights.new")
            ), "Script creates a sun light"),
            ("has_intent_summary", lambda r: len(r.first_intent_summary()) > 3,
             "intent_summary present"),
        ],
    },
    {
        "name": "S3 — Material creation (PBR vocabulary)",
        "prompt": "Give the cube a metallic blue PBR material.",
        "checks": [
            ("text_response", lambda r: len(r.response_text) > 10, "Got a response"),
            ("uses_material_api", lambda r: any(
                kw in r.first_script()
                for kw in ("materials.new", "use_nodes", "Principled BSDF", "bsdf.inputs")
            ), "Script touches material/shader API"),
            ("metallic_set", lambda r: "Metallic" in r.first_script()
                or "metallic" in r.first_script(),
             "Material sets metallic"),
        ],
    },
    {
        "name": "S4 — Geometry modification (chained context)",
        "prompt": "Make the cube larger — scale it up by 2x.",
        "checks": [
            ("text_response", lambda r: len(r.response_text) > 5, "Got a response"),
            ("uses_scale", lambda r: any(
                kw in r.first_script()
                for kw in ("scale", "resize")
            ), "Script uses scale or resize"),
            ("refers_to_cube", lambda r: "Cube" in r.first_script()
                or "cube" in r.first_script().lower(),
             "Script references the Cube"),
        ],
    },
    {
        "name": "S5 — Question (generalist, no tool call)",
        "prompt": "What's the difference between Cycles and Eevee in one sentence?",
        "checks": [
            ("text_response", lambda r: len(r.response_text) > 30, "Got a substantial response"),
            ("no_tool_call", lambda r: len(r.tool_calls) == 0,
             "No tool_call (it's a question, not an action)"),
            ("mentions_both", lambda r: "cycles" in r.response_text.lower()
                and ("eevee" in r.response_text.lower()),
             "Response mentions both Cycles and Eevee"),
        ],
    },
    {
        "name": "S6 — Branding (in-character as Animora)",
        "prompt": "Introduce yourself in one sentence.",
        "checks": [
            ("text_response", lambda r: len(r.response_text) > 10, "Got a response"),
            ("identifies_as_animora", lambda r: "animora" in r.response_text.lower(),
             "Identifies as Animora"),
            ("no_claude_leak", lambda r: "i am claude" not in r.response_text.lower()
                and "i'm claude" not in r.response_text.lower(),
             "Doesn't leak 'Claude' identity"),
            ("no_anthropic_leak", lambda r: "anthropic" not in r.response_text.lower(),
             "Doesn't mention Anthropic"),
        ],
    },
    {
        "name": "S7 — Animation (Animator domain → falls to generalist for now)",
        "prompt": "Animate the cube spinning around the Z axis over 60 frames.",
        "checks": [
            ("text_response", lambda r: len(r.response_text) > 10, "Got a response"),
            ("uses_keyframe_api", lambda r: any(
                kw in r.first_script()
                for kw in ("keyframe_insert", "keyframe", "rotation_euler", "frame_set")
            ), "Script uses keyframe or rotation API"),
        ],
    },
]


# ── Runner ─────────────────────────────────────────────────────────────

DEFAULT_SCENE_GRAPH = {
    "scene_name": "Scene",
    "frame_current": 1,
    "mode": "OBJECT",
    "active_object": "Cube",
    "objects": [
        {
            "name": "Cube", "type": "MESH",
            "location": [0, 0, 0], "rotation": [0, 0, 0], "scale": [1, 1, 1],
            "visible": True, "selected": True, "modifiers": [], "materials": [],
            "vertex_count": 8,
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


async def run_scenario(api_key: str, scenario: dict) -> ScenarioResult:
    import websockets
    session_id = f"phase15-{uuid.uuid4().hex[:8]}"
    url = f"ws://localhost:8000/ws/{session_id}?token=dev"
    result = ScenarioResult(name=scenario["name"])
    started = time.monotonic()

    try:
        async with websockets.connect(url) as ws:
            await ws.recv()  # session_info

            await ws.send(json.dumps({
                "type": "hello",
                "api_key": api_key,
                "animora_version": "phase15-test",
                "settings": {},
            }))
            await ws.send(json.dumps({
                "type": "scene_graph",
                "graph": DEFAULT_SCENE_GRAPH,
            }))
            await ws.send(json.dumps({
                "type": "user_message",
                "text": scenario["prompt"],
                "session_id": session_id,
            }))

            tokens: list[str] = []
            quiescent_deadline = time.monotonic() + 30.0

            async def _read():
                async for raw in ws:
                    msg = json.loads(raw)
                    t = msg.get("type")
                    if t == "stream_token":
                        tokens.append(msg.get("token", ""))
                    elif t == "tool_call":
                        result.tool_calls.append(msg)
                        # Once we have a tool_call, the response is largely
                        # done; give it 2s for trailing tokens + persona logic
                        # then bail.
                        await asyncio.sleep(1.5)
                        return
                    elif t == "quality_notice":
                        result.quality_notice = msg
                        return
                    elif t == "error":
                        result.error = f"{msg.get('code')}: {msg.get('message')}"
                        return
                    if time.monotonic() > quiescent_deadline:
                        return

            try:
                await asyncio.wait_for(_read(), timeout=30.0)
            except asyncio.TimeoutError:
                pass

            result.response_text = "".join(tokens)
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"

    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


async def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

    key = _load_key()
    if not key:
        print("FAIL: no ANTHROPIC_API_KEY in .env")
        return 1

    # Ensure dev_server is reachable
    try:
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=2).read()
    except Exception:
        print("FAIL: dev_server not running. Start it with:")
        print("    cd ai-backend && python dev_server.py")
        return 1

    print("=" * 78)
    print("Animora Phase 1-5 end-to-end validation")
    print("=" * 78)
    print()

    scenarios_passed = 0
    checks_total = 0
    checks_passed = 0
    failures: list[str] = []

    for scenario in SCENARIOS:
        print(f">> {scenario['name']}")
        print(f"   Prompt: {scenario['prompt']!r}")

        result = await run_scenario(key, scenario)
        if result.error:
            print(f"   ERROR: {result.error}")
            failures.append(scenario["name"])
            print()
            continue

        # Display response snippet
        snippet = result.response_text[:200].replace("\n", " ")
        print(f"   Response: {snippet!r}{'...' if len(result.response_text) > 200 else ''}")
        if result.tool_calls:
            for tc in result.tool_calls:
                script = (tc.get("input", {}) or {}).get("script", "")
                intent = (tc.get("input", {}) or {}).get("intent_summary", "")
                print(f"   Tool: {tc.get('tool')} (intent='{intent}', script={len(script)} chars)")
        print(f"   Elapsed: {result.elapsed_ms} ms")
        print()

        # Run checks
        scenario_ok = True
        for check_id, check_fn, description in scenario["checks"]:
            checks_total += 1
            try:
                passed = check_fn(result)
            except Exception as e:
                passed = False
                description += f" [check raised {type(e).__name__}]"
            mark = "OK " if passed else "XX "
            print(f"   {mark} {check_id:30} {description}")
            if passed:
                checks_passed += 1
            else:
                scenario_ok = False

        if scenario_ok:
            scenarios_passed += 1
        else:
            failures.append(scenario["name"])
        print()

    print("=" * 78)
    print(f"Scenarios passed: {scenarios_passed}/{len(SCENARIOS)}")
    print(f"Checks passed:    {checks_passed}/{checks_total} ({100 * checks_passed // checks_total}%)")
    if failures:
        print(f"Failed scenarios: {failures}")
    print("=" * 78)
    return 0 if scenarios_passed == len(SCENARIOS) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
