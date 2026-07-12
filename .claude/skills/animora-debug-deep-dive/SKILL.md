---
name: animora-debug-deep-dive
description: Use when something misbehaves and the cause isn't obvious — "AI freezes", "panel hangs", "tool_result never arrives", "quality suddenly worse", "cache hit ratio dropped", "works locally fails in prod", "reproduce this bug", "intermittent". The discipline: reproduce first, isolate the layer, write the regression test, then fix.
---

# Animora debug deep-dive

## The discipline (non-negotiable order)
1. **Reproduce first.** No fix lands from theory. Capture the exact prompt/session/scene that fails.
2. **Isolate the layer** (see map below) — most "AI is broken" reports are one specific seam.
3. **Write the regression test that fails** (unit if possible, eval benchmark if behavioral).
4. **Fix until that test passes**, run the full suite + targeted eval, then commit fix+test together.

## Layer map — where to look by symptom
| Symptom | First suspect | Instrument |
|---|---|---|
| Panel hangs mid-task | coordinator await | logs: `coordinator.await.timeout/cancelled/complete`, `elapsed_ms`; check addon sent `tool_progress` (45 s idle clock, 180 s ceiling) |
| Mutation "ignored" | loop enforcer deferral (working as designed?) | `enforcer.mutation.deferred` events; `[Animora loop enforcer] Deferred` tool_results |
| Model builds blind / worse spatially | screenshot injection failing | `enforcer.screenshot.unavailable` streaks; vision buffer state |
| Quality regressed after a prompt/persona edit | cache + eval | `anthropic.client.stream.completed` `cache_hit_ratio` (2nd turn should be ~0.99); run `eval/runner.py --baseline eval/baseline.json --fail-on-regress` |
| Wrong model picked ("made a sphere for 'cube'") | router/classifier | router reason string in logs (`execution-default` / `creation-verb-override` / …); `test_phase4_classifier.py` |
| Works on dev_server, fails deployed | env/provider seam | `ANIMORA_ENV`, JWT iss/aud path, provider=bedrock model map, secrets safety boot refusal |
| Script rejected/failed | enforcer vs runtime | enforcer verdict (pre-dispatch) vs addon stderr in tool_result; AST-split runner statement index |
| Viewport frozen during AI work | main-thread abuse | anything bpy off the main thread? EEVEE shader compile on first material view is a known stall |

## Repro harnesses (cheapest first)
- **Unit**: `pytest ai-backend/tests -k <area> -x` (238 tests, ~5 s, no API key needed).
- **Bare smokes**: `python ai-backend/tests/test_phase5_5_retry.py` (no API), `test_call.py` (API), `test_ws.py` (needs dev_server).
- **dev_server loop**: `cd ai-backend && python dev_server.py` (in-memory redis, any token) + addon pointed at `ws://127.0.0.1:8000` — full product loop without prod.
- **Eval single benchmark**: `python ai-backend/eval/runner.py --filter <name>` (cents); rescore old runs free: `--skip-llm --input-json prior_run.json`.
- **Session recordings**: `ai-backend/recorder.py` + `docs/SESSION_FORMAT.md` — record a failing live session, replay the exact frames.
- **Addon fast iteration**: edit → `python scripts/sync_addon.py` → toggle addon off/on. Addon logs: Blender system console shows print() + WARNING and above only — use `log.warning` while debugging handlers, or watch the backend side.

## Environment gotchas (verified the hard way)
- Windows MAX_PATH: deep venv paths break modern `anthropic` wheels (92-char module filename) — keep venvs at short paths (`C:\av`) or enable long paths.
- `redis>=8` dropped the `asyncio` extra name — harmless warning, extra not needed.
- CI eval failing in ~1 s = missing `ANTHROPIC_API_KEY` secret (runner exits with that message, `eval/runner.py:529-538`), not a quality regression.
- Legacy tests that `return bool` always "pass" — a green legacy test proves less than it looks; convert to `assert` when touching one.

## When the bug is the MODEL's behavior
Prompt/persona bugs get eval benchmarks, not unit tests: add the failing case to `eval/benchmarks.py`, confirm it fails, fix the prompt layer, confirm suite holds, re-baseline deliberately in the same PR.
