---
name: animora-orchestrator
description: Use when changing how requests flow through the backend — prompt assembly, prompt caching, model routing/tiering, session memory, streaming loop wiring, or the Anthropic/Bedrock provider layer. Triggers include "system prompt assembly", "cache hit ratio dropped", "prompt cache", "router picks wrong model", "Haiku triage", "session memory", "context builder", "add a model", "Bedrock".
---

# Animora orchestrator — runtime assembly, caching, tiering

## Per-turn prompt assembly (`orchestrator/context_builder.py`)
Layered, in order — STATIC first for cache alignment:
1. `prompts/master_prompt.py` — identity, 7 absolute rules, quality standards (STATIC)
2. Persona extension (`personas/base.py` BASE_EXTENSION + specialist block) (STATIC per session)
3. Session memory summary (`orchestrator/memory.py`) (slow-changing)
4. Live scene context — `scene_intelligence.py` (compressed graph) + `scene_diff.py` (JSON-patch since last turn) (per-turn)
5. Optional vision frame from `vision_buffer` (per-turn)
Live state is refreshed **before every model call** — never cache scene context across iterations.

## Cache discipline (the money rule)
`cache_control` breakpoints live in `context_builder.py` — the ONLY place they're set. Master prompt + persona must be byte-identical across turns; second-turn `cache_hit_ratio` should be ~0.99 (logged in `anthropic.client.stream.completed`). Any edit to `master_prompt.py` or `personas/*.py` is cache-invalidating: expect a one-turn re-warm per session and eval-gate scrutiny. Never interleave dynamic content above the static layers.

## Model tiering (`orchestrator/router.py`)
- Logical names only: `claude-opus-4-7` / `claude-sonnet-4-6` / `claude-haiku-4-5-20251001`.
- **Execution intents → Opus on every plan** (trial included — Sonnet confused "cuboid"→"ovoid"; model downgrade is NOT the pricing lever, rate/budget limits are).
- Non-execution → size-routed: short low-complexity question (<1k ctx tokens, <120 chars, complexity <0.3) → Haiku; else Sonnet.
- Defensive creation-verb override (`_CREATION_VERB_PATTERN`): classifier said non-execution but message says "create/add/make/…" → force Opus. Keeps "create a cube" from routing cheap and producing a sphere.
- Intent labels here mirror `orchestrator/intent.py::_VALID_INTENTS` — change both together.

## Session memory (`orchestrator/memory.py`)
Rolling Haiku-written summary: keep `_KEEP_VERBATIM_TURNS` recent turns verbatim; fold older into `memory_summary` (built scene facts, user preferences, rejected attempts, in-progress goals); prune folded turns. Runs as a background task after a turn persists; Haiku failure = no-op (never fails the session). The summary sits in the cached prefix — it changing slightly is fine (infrequent), churning every turn is a bug.

## Streaming loop (`orchestrator/streaming.py`) — wiring contract
`stream_response()` owns: intent classify → persona load → context build → router select → agentic loop (≤8 iters) with tool dispatch through `ToolResultCoordinator` (register BEFORE emitting tool_use; idle-aware await: 45 s idle reset by `tool_progress`, 180 s hard ceiling; user interrupt via cancel_event). Output caps: execution 8k (`ANIMORA_EXEC_MAX_TOKENS`), other 16k. Thinking uses the adaptive API (`thinking={"type":"adaptive"}` + `output_config.effort`) — the old `budget_tokens` shape is rejected by 4.7-class models.

## Provider abstraction (`llm_provider.py`, `anthropic_client.py`)
- `ANIMORA_LLM_PROVIDER=anthropic|bedrock` (Bedrock maps logical names via `_BEDROCK_MODEL_MAP`, `us.` cross-region profiles; BYOK ignored on Bedrock).
- Every non-streamed call MUST go through `anthropic_client.messages_create(...)` — `client._sdk.messages.create(...)` bypasses translation and silently breaks on Bedrock.
- `TokenUsage` (input/output/cache_create/cache_read) is extracted per call and emitted as `usage.recorded` — this is the hook per-user metering builds on (see animora-metering-billing).

## Feature flags (current defaults)
`ANIMORA_ENFORCE_LOOP=1`, `ANIMORA_QUALITY_RETRIES=2`, `ANIMORA_ENABLE_SPEC=0` (spec/brief builder built but dark — enabling it is a deliberate quality-plan step with eval before/after), `ANIMORA_EXEC_MAX_TOKENS=8000`.

## Pitfalls
- Don't add per-session state as module globals in streaming/coordinator — everything is per-session objects owned by `main.py:websocket_endpoint`.
- dev_server.py monkey-patches redis+auth; never import from it in production paths (`stage_for_installer.py` excludes it from bundles).
- A new always-on LLM call inside the loop multiplies per-task cost — batch at checkpoints (the artist's-eye pattern) or triage to Haiku.
