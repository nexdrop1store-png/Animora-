---
name: animora-product-loop
description: Use when touching the agentic loop, loop enforcer, critic-correction cycle, or quality retry — "loop enforcer", "blind chaining", "why was my mutation deferred", "forced screenshot", "critic correction", "quality retry", "agentic iterations", "request_final_review", "checkpoint". Specifies the inspect→execute→capture→critique→correct loop as built, the enforcer contract, and what must remain impossible.
---

# The Animora product loop (Loop 2) — as built

The loop the AI must run: inspect scene → (spec) → plan → execute ONE step → capture viewport → critique vs artist's-eye → correct → re-read scene → advance → final review. It is enforced **in code** in `ai-backend/orchestrator/streaming.py`, not merely requested in the prompt.

## The contract — what must be IMPOSSIBLE
1. **Chaining two refinement mutations without seeing the result of the first.** Enforced by the Stage-1 loop enforcer (below). Do not add mutation paths that bypass `_on_tool_call`'s gate.
2. **A mutating iteration with no capture.** The enforcer force-injects a viewport screenshot into the next model turn after any mutating iteration (`enforcer.screenshot.injected`).
3. **Dispatching an LLM script that fails the safety gate.** `quality_enforcer.validate_script()` runs before dispatch of `execute_animora_code` — never route around it.
4. **The user seeing a "finished" result that skipped the artist's-eye check** when a checkpoint was signaled (`request_final_review`) or gates fired.
Any PR that could weaken one of these must say so explicitly and add a test proving the property still holds.

## Loop enforcer mechanics (`streaming.py:100–127, 719–760, 1189–1370`)
- Flag: `_ENFORCE_LOOP` = `ANIMORA_ENFORCE_LOOP`, **default ON**. `=0` is a debug-only escape hatch — never ship code that requires it off.
- Mutation tool set: `_LOOP_ENFORCER_MUTATION_TOOLS` (`streaming.py:127`). Inspect tools + backend signals (`request_final_review`, `use_asset` routing) are NOT gated.
- Granularity (Phase-A revision): **foundation/blockout mutations may batch freely; REFINEMENT mutations are gated to ONE per iteration.** Rationale: the original strict gate starved 22-mutation hero builds (~3 mutations/turn). Refinement correctness depends on SEEING the current state; blockout doesn't.
- Blocked mutations are not errors: the loop synthesizes a "deferred" `tool_result` (`streaming.py:1189-1200`, message prefix `[Animora loop enforcer] Deferred — …`) so the model re-issues them next iteration after the capture.
- After a mutating iteration, the enforcer fetches the latest viewport frame and injects it as a user-role image block (`streaming.py:1288-1360`). If the frame fetch fails it emits `enforcer.screenshot.unavailable` — investigate those in logs; a silent streak means the loop is running blind.
- Telemetry events: `enforcer.enabled` (once/turn), `enforcer.mutation.dispatched`, `enforcer.mutation.deferred`, `enforcer.screenshot.injected`.

## Critic → correct (Stage 3A, `streaming.py:525-534, 1628+`)
Deterministic critic (`orchestrator/critic.py`) inspects the **live scene graph** (`get_live_scene_graph` callback) after mutations; on findings it injects a corrective user message. Bounded: `_MAX_CRITIC_CORRECTIONS = 2` per turn — a model that ignores the critic can't loop forever.

## Mechanical gates (single-shot per turn, corrective message injection)
| Gate | Fires when | Where |
|---|---|---|
| First-step foundation (Stage 6) | first real action of the turn is judged wrong-foundation | `streaming.py:1424` |
| Scene-floor part count | scene-class request produced too few parts | `streaming.py:1472` |
| Material completeness | built objects would ship default-grey | `streaming.py:1524` |
| Finished-by-default | build ended with no lighting/camera | `streaming.py:1583` |

## Quality retry (Phase 5.5, artist's-eye)
- Checkpoint: model calls `request_final_review` → artist's-eye vision check runs that iteration (Sonnet call on the capture).
- On FAIL with retries left (`ANIMORA_QUALITY_RETRIES`, default 2, budget **per user message**, only FAIL-following iterations decrement): append `orchestrator/retry.build_revision_user_message()`, emit `quality.retrying`; on pass `quality.retry_succeeded`; exhausted → `quality_notice` banner (never a hard block).
- `main.py`'s background quality check is skipped when the inline path ran (avoid double vision spend).

## Iteration bounds
`_MAX_AGENT_ITERATIONS = 8` (headroom for gated refinements); execution turns capped at 8k output tokens (`ANIMORA_EXEC_MAX_TOKENS`), non-execution 16k. Do not raise bounds to "fix" a quality issue — fix the gate or the prompt.

## Tests
`ai-backend/tests/test_stage1_harness.py` (enforcer block/defer; 3 cases need Blender), `test_stage2_critic.py`, `test_stage3_correction.py`, `test_stage6_first_step.py`, `test_phase5_5_retry.py`. A change to enforcer/critic/gates without a matching test does not merge.
