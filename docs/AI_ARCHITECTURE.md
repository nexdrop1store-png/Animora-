# Animora AI System — Engineering Architecture & Roadmap

**Status:** Plan. Not yet implementation.
**Authoritative source:** Animora Master Product Blueprint (PDF, shared by user 2026-05-18). All decisions in this document defer to the blueprint; where this document is silent, fall back to the blueprint.
**Audience:** Engineers building the AI layer; reviewers validating the plan against the blueprint.

---

## 0. Three Load-Bearing Constraints

Every decision in this document is checked against these. If a proposed design violates one, the design is wrong, not the constraint.

1. **Standalone software.** Animora is a desktop app the user installs. The fork-of-Blender relationship is invisible to the end user. The AI lives inside `Animora.exe` (the in-process layer) and in a cloud service the app connects to automatically — no separate setup, no plugin marketplace, no "enable extension" step.
2. **Real-time continuous vision.** The AI receives a live 5–15 fps delta-compressed viewport stream, event-triggered HD captures, and continuous scene-graph sync. "Take a screenshot if needed" is forbidden. The AI sees every change as it happens.
3. **Maximum quality always.** No draft mode. No low-poly first pass. No "quick" option. Every output is film/AAA-grade from the first execution. If the quality gate fails, the AI auto-corrects and re-executes — the user only sees the passing result.

---

## 0.5 What "training the AI" means in this product

> **TL;DR:** We do **not** fine-tune. We call Anthropic's hosted Claude models via API and steer them with prompts, personas, tools, and live scene context. Claude runs on Anthropic's GPUs; AWS Fargate hosts our orchestrator that *calls* Claude. No model weights are ever modified by Animora.

When this document or the blueprint says "training the AI", it means one of these activities — **never weight training**:

- **Persona prompt iteration** — writing and refining the system-prompt extensions per domain (Environment Artist, Lighting TD, Hard Surface Artist, etc.) so Claude behaves like a senior specialist for each domain. Lives in `ai-backend/personas/` (Phase 4).
- **Few-shot example curation** — collecting good `(user request → ideal response)` pairs and embedding them into persona prompts so the model imitates the desired output shape.
- **Quality enforcer tuning** — deciding which post-execution checks apply per persona, and how strict the retry threshold is. Lives in `ai-backend/quality_enforcer.py` + `ai-backend/prompts/artists_eye.py` (Phase 5).
- **Tool refinement** — which bpy operations to expose to the model, and how to describe them so it picks the right tool. Lives in `ai-backend/orchestrator/tools.py`.
- **Evaluation harness** — benchmark scenes with rubric-based scoring; regression CI to catch drops when we change a prompt. Lives in `ai-backend/evals/` (Phase 9).

Reasons we don't fine-tune:
1. **Anthropic doesn't offer fine-tuning for Sonnet 4.6 or Opus 4.5** (our flagship models). Only Claude 3 Haiku has limited beta access. The door isn't open.
2. **Fine-tuned models are locked to one base version.** Every Claude upgrade Anthropic ships, we'd lose access to until we re-trained.
3. **Operational cost** (training infra + eval pipeline + retraining cadence) does not justify the marginal quality gain over a well-engineered prompt + persona system.
4. **Blueprint §0 + §1 say "100% native Blender tools"** and treat Claude as a steerable expert via prompt, consistent with this. The Cursor analogy in blueprint §1 is exact — Cursor didn't fine-tune; they wrapped OpenAI/Anthropic with elaborate prompts and tool use. Same pattern.

### What runs where (deployment topology)

| Component | Where it runs | Owner |
|---|---|---|
| Claude Haiku 4.5 / Sonnet 4.6 / Opus 4.5 | Anthropic's datacenters | Anthropic |
| `ai-backend/main.py` orchestrator | AWS Fargate (`us-east-1`, +`eu-west-1` later) | Animora |
| `AnthropicClient` wrapper (retry / cancel / token tracking) | Same Fargate task | Animora |
| `context_builder` + master prompt + persona prompts | Same Fargate task | Animora |
| `quality_enforcer` (pre + post execution checks) | Same Fargate task | Animora |
| Session memory (Redis) | AWS ElastiCache | Animora |
| Project memory (Postgres) | Supabase (shared with website auth) | Animora |
| `animora_panel` addon (chat UI + vision capture + bpy executor) | User's machine, inside Animora.exe | User |

The "intelligence" lives on Anthropic's side. The *steering* lives on our side. Fargate hosts the steering, not the intelligence.

### How "training" the AI looks day-to-day

A practical iteration loop (this is the work the user is signing up for):

1. Identify a weakness (e.g. "the model picks billboards instead of full geometry for distant trees").
2. Open the relevant persona prompt (e.g. `ai-backend/personas/environment_artist.py`).
3. Add a worked example or strengthen a rule in the prompt body.
4. Run the eval harness against the benchmark scenes for that persona.
5. If rubric scores improve, ship the prompt change. If they regress, revert and try a different approach.
6. Repeat. No GPUs. No retraining time. Edits take effect on the next request.

This is fast and cheap compared to fine-tuning, and it's how every commercial AI-native product (Cursor, GitHub Copilot Chat, Anthropic Claude Code, Notion AI, etc.) is actually built today.

---

## 1. System-Level Architecture

### 1.1 The three components (fixed by blueprint §5)

```
   ┌───────────────────── Animora.exe (user's desktop) ─────────────────────┐
   │                                                                       │
   │    SPACE_ANIMORA editor area  ←→  animora_panel addon (Python)        │
   │                                          │                            │
   │         ┌────────────────────────────────┴───────────────────────┐    │
   │         │                                                        │    │
   │         ▼ HANDS                                       EYES ▼     │    │
   │   Python Executor                                Scene Intel     │    │
   │   (sandboxed bpy)                                  Engine        │    │
   │         │                                                        │    │
   │         │   ┌─────────────── WebSocket (TLS) ──────────────┐     │    │
   │         └──▶│                                              │◀────┘    │
   │             │  outbound: scripts, scene state, vision      │          │
   │             │  inbound:  tokens, tool_calls, suggestions   │          │
   └─────────────┼──────────────────────────────────────────────┼──────────┘
                 │                                              │
                 ▼                                              ▼
   ┌──────────────────── ai-backend (FastAPI, cloud) ─────────────────────┐
   │                                                                      │
   │   ┌─── BRAIN ───────────────────────────────────────────────────┐    │
   │   │   Orchestrator → Persona Selector → Workflow Planner        │    │
   │   │   Anthropic SDK: Haiku 4.5 | Sonnet 4.6 | Opus 4.5         │    │
   │   └──┬──────────────────────────────────────────────────────────┘    │
   │      │                                                               │
   │   ┌──▼── QUALITY ENFORCER ──────────────────────────────────────┐    │
   │   │   Pre-exec: AST scan, banned-import check, scope check      │    │
   │   │   Post-exec: artist's-eye vision check, mesh repair         │    │
   │   └──┬──────────────────────────────────────────────────────────┘    │
   │      │                                                               │
   │   ┌──▼── MEMORY ────────────────────────────────────────────────┐    │
   │   │   Redis: session state, rate limits                         │    │
   │   │   Postgres: project memory, conversation history            │    │
   │   │   Anthropic prompt cache: persona prompts (90% discount)    │    │
   │   └─────────────────────────────────────────────────────────────┘    │
   │                                                                      │
   └──────────────────────────────────────────────────────────────────────┘
```

### 1.2 Why this split (local + cloud, not pure local)

- **Cloud Brain** so we can swap models, A/B-test prompts, ship persona updates without shipping a new installer, and never expose API keys to the desktop binary.
- **Local Eyes** so we never pipe raw GPU framebuffer to the cloud (cost + latency); the local capture compresses to ~5–20 KB per JPEG before egress.
- **Local Hands** so script execution touches `bpy` directly — no round-trip to a remote Blender, no missing state, undo stack stays intact, the user can interrupt at any time.
- **Cloud Quality Enforcer** because the post-execution artist's-eye check is a Claude vision call (Sonnet 4.6 with image input). Doing it locally would require shipping a vision model in the installer.

### 1.3 Cross-component contract

A single WebSocket (one per session) carries everything. Message types:

| Direction | type                | payload                                                    | rate            |
|-----------|---------------------|------------------------------------------------------------|-----------------|
| → server  | `hello`             | `{session_id, user_id, plan, animora_version, fingerprint}` | once on connect |
| → server  | `user_message`      | `{text, context_flags}`                                    | user-triggered  |
| → server  | `viewport_frame`    | binary: 17B header + JPEG payload                          | ≤15 Hz          |
| → server  | `hd_capture`        | `{trigger, png_base64, w, h, ts}`                          | event-triggered |
| → server  | `scene_graph`       | `{graph: {...}, ts}`                                       | debounced 500ms |
| → server  | `tool_result`       | `{tool_use_id, output, error, scene_diff}`                 | per tool call   |
| → server  | `interrupt`         | `{reason}`                                                 | user-triggered  |
| ← client  | `stream_token`      | `{text}`                                                   | LLM-driven      |
| ← client  | `tool_call`         | `{tool, tool_use_id, input}`                               | LLM-driven      |
| ← client  | `quality_notice`    | `{stage, severity, message}`                               | enforcer-driven |
| ← client  | `suggested_steps`   | `{steps: [...]}`                                           | LLM-driven      |
| ← client  | `error`             | `{code, message}`                                          | as needed       |

Vision frames go **binary** (not JSON) — JPEG payload, no base64 overhead. Everything else is JSON. The existing `ws_client.py` already supports `send_binary` and `send_json`.

### 1.4 What already exists vs what's missing

| Component | What's in repo | Lines | What's missing |
|---|---|---|---|
| Addon: vision capture | `blender-fork/scripts/addons_core/animora_panel/vision.py` | 243 | Backpressure, frame coalescing, GPU fallback when PIL absent |
| Addon: WS client | `…/animora_panel/ws_client.py` | 199 | Reconnect-with-resume, binary frame buffering |
| Addon: operators | `…/animora_panel/operators.py` | 337 | Tool-execution sandbox, mid-execution interrupt |
| Backend: FastAPI | `ai-backend/main.py` | 190 | Multi-tenant rate limiting, structured logging |
| Backend: orchestrator | `ai-backend/orchestrator.py` | 181 | Persona system, vision input, workflow planner, quality-fail retry loop |
| Backend: scene intel | `ai-backend/scene_intelligence.py` | 93 | Visual context, change-detection between graph snapshots |
| Backend: quality enforcer | `ai-backend/quality_enforcer.py` | 83 | Post-execution artist's-eye check, mesh repair generator |
| Backend: session mgr | `ai-backend/session_manager.py` | 78 | Long-term project memory, context summarization |
| Backend: tools | `ai-backend/tools/blender_ops.py` | — | Most domain-specific tools (material, modifier, render, scatter) |

The current `SYSTEM_PROMPT_BASE` in `orchestrator.py` directly **contradicts the blueprint**:

> "Keep scripts focused and minimal — do exactly what was asked"

The blueprint mandates the opposite — when the user says "beach with trees", the AI must build the full sculpted-terrain + PBR-sand + ocean-modifier + scattered-palms scene. The first thing to replace is this prompt (Phase 4 / Phase 6).

---

## 2. Phase 1 — Core AI Architecture Foundations

**Goal:** harden the existing scaffolding into a production-grade orchestration backbone before adding personas/quality/etc. on top.

### 2.1 Service boundaries

- **Animora desktop** owns: UI, vision capture, script execution, OS-level secrets (tokens via `keyring`), undo stack.
- **`ai-backend`** owns: LLM calls, persona prompts, quality verdicts, session/project memory, billing-tier enforcement.
- **`auth-server`** owns: identity, device binding, token issuance. Already scaffolded; out of scope for this AI plan.
- **Redis** owns: ephemeral session state (conversation history, rate-limit counters, in-flight tool calls).
- **Postgres** owns: durable project memory (per-user scene history, named projects, persona preferences).

### 2.2 Data flow for one user message

```
[User types "add a forest"]
   │
   ▼
[addon] OT_AnimoraSendMessage → ws_client.send_message
   │
   ▼
[ws] user_message{ text, context_flags }
   │                                           ┌─ scene_graph (last sync, <500ms old)
[backend] WS handler                           ├─ viewport_frame (last frame, <100ms old)
   │  ├─ assemble context  ────────────────────┤
   │  │                                        ├─ conversation history (last N turns from Redis)
   │  │                                        └─ project memory (relevant past scenes from Postgres)
   │  │
   │  ├─ classify intent  (Haiku) → workflow type ("environment_scatter")
   │  │
   │  ├─ load persona  ("Environment Artist" — system prompt module)
   │  │
   │  ├─ select model     (router: Sonnet for most, Opus if complexity > 0.8)
   │  │
   │  ├─ stream LLM call  ──→ tokens stream back via stream_token
   │  │                  └──→ tool_use blocks collected
   │  │
   │  ├─ for each tool_use:
   │  │     │
   │  │     ├─ if execute_blender_script:
   │  │     │     ├─ pre-exec validate (AST + scope) → reject or pass
   │  │     │     ├─ send tool_call to addon
   │  │     │     │
   │  │     │     ▼
   │  │     │  [addon] sandboxed exec → tool_result{output, scene_diff, hd_capture}
   │  │     │     │
   │  │     │     ▼
   │  │     │  [backend] artist's-eye check (Sonnet vision call on hd_capture)
   │  │     │     │
   │  │     │     ├─ pass → send "✓ done" + next-step suggestions
   │  │     │     └─ fail → auto-correct (LLM with failure context) → retry up to 2×
   │  │     │
   │  │     └─ if other tool: dispatch to appropriate handler
   │  │
   │  ▼
   │  persist to Redis (session), optionally to Postgres (project memory)
   │
   ▼
[addon] receives final state, redraws panel
```

### 2.3 Orchestration layer

A new file `ai-backend/orchestrator/__init__.py` (replacing the single-file `orchestrator.py`) split into:

- `orchestrator/router.py` — model selection (Haiku/Sonnet/Opus) based on plan + complexity + intent. Already partially implemented in `_select_model`.
- `orchestrator/intent.py` — Haiku-powered classifier that maps the user message to one of ~20 workflow types (see §5.1).
- `orchestrator/personas.py` — loads the correct persona prompt module (§5).
- `orchestrator/planner.py` — for multi-step workflows, expands the intent into an ordered list of sub-tasks before generating the first script. Opus-tier.
- `orchestrator/streaming.py` — the existing `stream_response` function, renamed and stripped of routing logic.
- `orchestrator/retry.py` — quality-fail retry loop (§6).

The dispatch chain: **intent → persona → planner (if multi-step) → streaming → quality → retry**.

### 2.4 Event system

Add an in-process event bus on the backend (e.g., `pyee.EventEmitter`) so quality enforcer / memory / telemetry can subscribe without coupling. Events:

- `message.received`
- `tool.executed`, `tool.failed`
- `quality.passed`, `quality.failed`
- `persona.switched`
- `session.resumed`, `session.idle`

These also feed structured logs (JSON to stdout, scraped by whatever observability stack we pick — open question, §11).

### 2.5 Open architectural questions (need user input)

1. **Hosting:** where does `ai-backend` actually deploy? (Options: Fly.io, Railway, AWS Fargate, self-managed on a single VPS during alpha.) Determines TLS termination, secrets storage, scaling story.
2. **Redis topology:** single instance for alpha, or managed cluster? Affects session-resume on backend restart.
3. **Postgres for project memory:** is the Supabase already provisioned for the website usable here, or do we need a separate Postgres? Schemas are independent — could share.
4. **Telemetry / observability:** Datadog, Honeycomb, Grafana Cloud, or roll-our-own with Loki? Affects how we emit metrics from the orchestrator.

---

## 3. Phase 2 — Scene Intelligence System

**Goal:** the Eyes. Make sure the Brain *never operates blind*.

### 3.1 Vision channels (blueprint §3, three levels)

#### Level 1 — Continuous viewport stream

- **Capture rate:** target 10 fps, throttle to 5 fps if backend reports backpressure.
- **Resolution:** 640×360, JPEG quality 60 — already implemented at `vision.py:capture_viewport_jpeg`.
- **Delta encoding:** currently a hash-equality check (`_should_send_frame`). Upgrade to a perceptual diff (compare a small thumbnail's mean-squared-error against the previous frame); if MSE < threshold, skip.
- **Wire format:** binary frame, 17-byte header (`type | width | height | timestamp | sequence`) followed by JPEG.
- **Backpressure protocol:** backend sends a `pause_stream` / `resume_stream` control message when its in-memory frame buffer exceeds 5 frames; addon honors it.

**Files:** `blender-fork/scripts/addons_core/animora_panel/vision.py` (extend), `ai-backend/vision_buffer.py` (new — ring buffer per session).

#### Level 2 — Event-triggered HD capture

- **Triggers (already wired):** `render_complete`, `selection_change`. **Add:** post-script-execution (mandatory — fuels the artist's-eye check), mode-change (Object↔Edit↔Sculpt etc.), every 30s heartbeat.
- **Resolution:** 1920×1080 PNG (lossless — Claude vision is sensitive to JPEG artifacts at small scales).
- **Wire format:** JSON, base64-encoded payload. (Acceptable here because frequency is low — 1–5 per minute.)
- **Backend handling:** stored briefly in Redis (TTL 5 min), passed as image input to Claude vision calls.

#### Level 3 — Scene graph sync

- **Trigger:** debounced 500ms after any `depsgraph_update_post`. Already wired at `vision.py:_schedule_scene_graph_send`.
- **Serialization:** extend `serialize_scene_graph` to include modifier *parameters* (not just types), shader node summary (active node tree → flattened), keyframe count per action, NLA tracks.
- **Diff layer:** backend computes a JSON-patch between consecutive graphs so the LLM can be told "user just enabled subsurf on Suzanne" instead of resending the full 50-object graph.

**Files:** `ai-backend/scene_intelligence.py` (already 93 lines — extend), `ai-backend/scene_diff.py` (new).

### 3.2 Context assembly for LLM calls

At the moment the orchestrator stuffs a stringified scene graph into the system prompt (`SYSTEM_PROMPT_BASE.format(scene_context=…)`). Replace with a structured **ContextBuilder**:

```
ContextBuilder
  ├─ system: master prompt + persona prompt + tool prompt   ← cached
  ├─ system: compressed knowledge base sections (relevant)   ← cached
  ├─ history: last N user/assistant turns
  ├─ user message attachments:
  │     ├─ scene_graph_summary (text, ~500 tokens)
  │     ├─ scene_graph_diff (only changed objects, JSON)
  │     ├─ latest_hd_capture (image_url block)
  │     └─ latest_viewport_frame (image_url block, if recent)
  └─ user message text
```

**Cache strategy:** persona + master prompt + knowledge sections form a stable prefix → set `cache_control` on the last block of the system prompt. Per blueprint §8 cost model, this is what drops effective input cost from $3/MTok to ~$0.30/MTok on Sonnet.

### 3.3 Visual understanding pipeline

The HD capture is fed into the artist's-eye check via Claude Sonnet vision (`messages.create` with `image` content block). The check prompt is structured (see §6.1). For deeper analysis we can optionally also send the previous HD capture so the model can verbalize the visual diff, but that doubles vision token cost — defer until quality regressions are observed.

### 3.4 Performance budget

| Stage | Budget | How |
|---|---|---|
| Local capture (640×360 JPEG) | < 20 ms | GPUOffScreen → PIL JPEG, already fast |
| Local → backend WS RTT | < 50 ms (US/EU), < 200 ms (intercontinental) | regional backend deployment |
| Backend frame ingestion | < 5 ms | binary parse, ring-buffer push |
| Frame → LLM availability | next request only | not embedded into every LLM call; pulled on demand |
| HD capture (1920×1080 PNG) | < 200 ms | acceptable; happens 1–5×/min |
| Scene graph serialization | < 50 ms | for ≤100 objects; > 100 → background thread |

### 3.5 Open questions

1. **Vision retention:** keep last N HD captures? How many? Affects Redis memory.
2. **Frame storage cost:** at 10 fps × 8 hours/day × thousands of users, even ~10 KB frames sum to TB/month if we persist. Decision: **don't persist** vision frames after session ends; keep last-5-min window for artist's-eye check only.
3. **Privacy:** the viewport stream may contain user IP. Need explicit consent in onboarding + clear retention policy (≤5 min). Handled in compliance pass, not here.

---

## 4. Phase 3 — Python Execution Framework

**Goal:** the Hands. Safe, fast, undoable bpy execution with rich feedback.

### 4.1 Execution model

A tool call arrives at the addon as `{type: "tool_call", tool: "execute_blender_script", tool_use_id, input: {script}}`. The addon:

1. Pushes an undo step: `bpy.ops.ed.undo_push(message="Animora: {persona}: {one-line-intent}")`. The label is what the user sees in the undo history — must be descriptive.
2. Snapshots the scene graph (for diff).
3. Constructs an execution namespace seeded with `bpy`, `bmesh`, `mathutils`, `math`, `random` — **nothing else**. No `__builtins__` except a curated whitelist (`len`, `range`, `enumerate`, `min`, `max`, etc.).
4. Compiles the script with `compile(source, "<animora:{persona}:{tool_use_id}>", "exec")`. The filename is the traceback breadcrumb — makes errors greppable.
5. Executes inside a try/except that captures stdout, stderr, and exceptions to strings.
6. Snapshots the scene graph again, diffs.
7. Triggers an HD capture.
8. Returns `tool_result{output, error, scene_diff, hd_capture_id}` to backend.

### 4.2 Sandboxing layers

| Layer | What it blocks | Where |
|---|---|---|
| Pre-exec (backend) | `import os`, `subprocess`, `open()`, `eval()`, `exec()`, network libs | `quality_enforcer.py:validate_script` (already 83 lines) |
| Pre-exec (backend) | unbounded loops, deep recursion | extend `validate_script` with cyclomatic check on AST |
| Pre-exec (backend) | poly count > 10M, render samples > 10000 | already partially implemented |
| Exec (addon) | `__builtins__` minimization | new — `operators.py:_execute_script` |
| Exec (addon) | wall-clock timeout (default 30s, 120s for sim/render) | thread + `bpy.app.timers` poll |
| Exec (addon) | memory ceiling (Python `resource` module on Unix, `psutil` on Windows) | new |

### 4.3 Undo & non-destructive guarantees

From blueprint §6.3, **every AI action must be on the undo stack** and **non-destructive by default**:

- Modifiers are added, never applied (unless the user says "apply"). Enforced by a post-exec lint: walk the scene-diff for `modifier_applied` events → flag as quality fail unless intent was "apply".
- Original mesh topology preserved. Sculpt + retopo flow keeps the original as a hidden backup mesh.
- Animation stored in named Actions (no manual bone-by-bone keying).
- Physics baked to cache, not converted to static mesh.

These are not *enforced* by the executor — they're enforced by the **system prompt** plus the post-execution mesh-repair pass (§6).

### 4.4 Async execution

Some operations (Cycles preview render, fluid bake) take seconds-to-minutes. The addon needs an async execution wrapper:

- Spawn the operation, return `tool_result{status: "in_progress", task_id}` immediately.
- Backend tells the user "rendering preview…" via `stream_token`.
- When complete, addon sends `tool_result{task_id, status: "complete", output}`.
- User can `interrupt` (the addon calls `bpy.ops.render.cancel()` or sim cancel equivalent).

**Files:** new `blender-fork/scripts/addons_core/animora_panel/executor.py` for the sandbox + async wrapper; refactor `operators.py:_execute_script` to delegate to it.

### 4.5 Error feedback to the LLM

When a script fails, the LLM gets back the *exact* traceback plus the scene-diff up to the failure point. This is what enables auto-correction:

```json
{
  "tool_use_id": "toolu_xyz",
  "error": "RuntimeError: Operator bpy.ops.mesh.bridge_edge_loops.poll() failed, context is incorrect",
  "scene_diff": { "added": [...], "modified": [...] },
  "hd_capture_id": "hd_2026..."
}
```

The orchestrator then sends a follow-up Claude message with the failure + repair instruction, and re-runs.

---

## 5. Phase 4 — Expert Persona System

**Goal:** the personas from blueprint §5.2 — Senior Hard Surface Artist, Character Artist, Environment Artist, Technical Animator, Character Animator, Lighting TD, VFX Artist, Game Dev Artist, Compositor.

### 5.1 Intent classification → workflow → persona

Intent classifier (Haiku, single call per user message, ~200 tokens out) maps natural-language input to one of these workflow types (from blueprint §7.3 + extensions):

| Intent | Persona | Default model |
|---|---|---|
| `hard_surface_model` (vehicle, weapon, prop) | Senior Hard Surface Artist | Sonnet |
| `character_sculpt` | Character Artist | Sonnet/Opus |
| `architecture` | Environment Artist | Sonnet |
| `terrain_landscape` | Environment Artist | Sonnet |
| `dense_scene` (forest, beach, city) | Environment Artist | Opus |
| `cloth_sim` | VFX Artist | Sonnet |
| `fluid_water` | VFX Artist | Sonnet |
| `destruction_explosion` | VFX Artist | Opus |
| `rig_setup` | Technical Animator | Opus |
| `character_animation` | Character Animator | Sonnet |
| `lighting_setup` | Lighting TD | Sonnet |
| `material_authoring` | (persona of surrounding intent) | Sonnet |
| `geometry_nodes_advanced` | Environment Artist or specialist | Opus |
| `render_setup` | Lighting TD / Compositor | Sonnet |
| `compositing` | Compositor | Sonnet |
| `2d_grease_pencil` | (own persona TBD) | Sonnet |
| `game_export` | Game Dev Artist | Sonnet |
| `simple_edit` (move/scale/recolor) | (no persona — base prompt) | Haiku |
| `question` (no execution) | Senior Artist generalist | Haiku |
| `unknown` | Senior Artist generalist (asks clarification) | Sonnet |

The classifier returns `{intent, confidence, recommended_persona, complexity_estimate}`. If `confidence < 0.7` the orchestrator asks a clarifying question instead of guessing.

### 5.2 Persona prompt modules

Each persona is a separate file under `ai-backend/personas/`:

```
ai-backend/personas/
  base.py                  # shared rules (quality philosophy, non-destructive, tool list)
  environment_artist.py
  hard_surface_artist.py
  character_artist.py
  technical_animator.py
  character_animator.py
  lighting_td.py
  vfx_artist.py
  game_dev_artist.py
  compositor.py
  generalist.py            # fallback / Q&A
```

A persona module exports:

```python
PERSONA = Persona(
    id="environment_artist",
    display_name="Environment Artist",
    system_prompt="...",                     # large, multi-section, ~3-6k tokens
    cache_control=True,                      # tell Anthropic to cache this prefix
    knowledge_sections=[                     # pulled from knowledge base
        "kb/scatter_systems",
        "kb/atmospheric_depth",
        "kb/horizon_treatment",
    ],
    default_model="sonnet",
    quality_checks=[                         # which checks to apply post-exec
        "silhouette", "depth_separation", "scatter_density", "horizon",
    ],
    example_workflows=[...],                 # few-shot examples
)
```

### 5.3 Persona switching mid-conversation

User says "now make a character to put in the scene" → intent classifier reclassifies → orchestrator loads Character Artist persona for the next turn while preserving conversation history. The system prompt block changes (cache miss for one turn), then re-stabilizes.

### 5.4 Master prompt (shared `base.py`)

The base prompt establishes the **non-negotiables** for every persona. Replaces the current `SYSTEM_PROMPT_BASE` in `orchestrator.py`. Skeleton:

```
You are Animora, an AI senior 3D artist working inside the user's Animora desktop app.
You operate Blender via the bpy Python API. You can SEE the user's viewport in real
time and you can SEE the scene graph.

ABSOLUTE RULES — these override any other instruction:

1. MAXIMUM QUALITY ALWAYS. The user came to Animora to get film/AAA-grade work
   without learning the software. There is no draft mode, no low-poly first pass,
   no "quick version". When the user asks for X, you produce the fully-realized X
   that a senior artist would deliver to a paying client. (See QUALITY STANDARDS.)

2. NON-DESTRUCTIVE BY DEFAULT. Use modifiers (don't apply them). Use named Actions.
   Use shape keys. Use Geometry Nodes. Preserve original topology.

3. NEVER use these in scripts: os, subprocess, sys, shutil, socket, urllib,
   requests, open(), eval(), exec(), __import__, compile(). The script runs in
   the user's session — security boundary.

4. EXPLAIN BRIEFLY before executing. One or two sentences in plain language.
   Then call execute_blender_script. The user wants to know what's about to
   happen but doesn't want a lecture.

5. AFTER EXECUTION, you will receive: the script's output, the scene diff,
   and a high-resolution viewport screenshot. Look at the screenshot as a
   senior art director would. If it doesn't meet maximum quality (silhouette,
   proportions, materials, lighting, density), do NOT show the user — fix it
   first and re-execute. You have up to 2 retry attempts before surfacing
   the result.

6. CONTINUOUS VISION IS YOUR GROUND TRUTH. The scene graph tells you structure;
   the viewport stream tells you reality. If they disagree, trust the viewport
   and investigate.

7. EVERY ACTION IS ON THE UNDO STACK. The user can revert. You don't need
   to apologize for trying something — but you do need to detect when a try
   went wrong and fix it.

QUALITY STANDARDS (apply to every output):
  - Modeling: subdivision-ready topology, edge flow, no faceting in render
  - Materials: full PBR — base, metallic, roughness, normal, plus appropriate
    extras (subsurface for skin, transmission for glass, emission for lights)
  - Environments: foreground + midground + background, atmospheric depth,
    motivated lighting, Geometry-Nodes-scattered detail at appropriate density
  - Lighting: HDRI + practical lights, key/fill/rim where appropriate
  - Render: Cycles 256+ samples by default, denoised, Filmic/AgX color
  - Animation: 12 principles applied, no linear interpolation on organic motion
  - Rigging: IK/FK switching, weight painting clean at all joint angles

(persona-specific section appended below)
```

Then each persona file appends 2–4k tokens of specialist knowledge plus 1–3 worked examples.

### 5.5 Few-shot examples

For each persona, include 1–3 worked examples of the form:

```
USER: <typical request>
ASSISTANT: <brief explanation>
[tool call: execute_blender_script with the full professional-grade script]
TOOL_RESULT: <ok, with scene diff and HD capture>
ASSISTANT: <what was made, what decisions, suggested next steps>
```

These are gold for steering output quality. Store as separate fixtures in `ai-backend/personas/examples/`.

### 5.6 Open question

- **Persona attribution to the user.** Does the UI show "Animora (Environment Artist)" when a persona is active, or is the persona invisible chrome? Recommendation: invisible by default, optional "show persona" toggle in settings — keeps the chat feel uncluttered.

---

## 6. Phase 5 — Quality Enforcement System

**Goal:** the user never sees a sub-professional result.

### 6.1 The artist's-eye check (mandatory after every execution)

A Claude Sonnet vision call with a structured prompt and the HD capture image:

```
You are reviewing a 3D scene as a senior art director.

CONTEXT
User asked: <intent>
Persona used: <persona>
Quality checks for this persona: <list>

SCENE STATE
<compressed scene graph diff>

VIEWPORT CAPTURE
<image>

For each quality check, output a JSON object:
  {
    "check": "silhouette" | "proportions" | "topology" | "materials" |
             "lighting" | "depth_separation" | "scatter_density" | ...,
    "verdict": "pass" | "fail" | "n/a",
    "reason": "<one sentence — what's wrong if fail, what's right if pass>"
  }

After all checks, output:
  {"overall": "pass" | "fail", "fix_suggestions": ["...", "..."]}
```

Response is parsed; if `overall == "fail"`, the orchestrator triggers the retry loop (§6.4).

### 6.2 Automatic mesh repair

Blueprint §6.2 lists deterministic checks runnable via bmesh:

| Check | Fix | bmesh API |
|---|---|---|
| Non-manifold geometry | Grid Fill / F2 | `bmesh.ops.holes_fill` |
| Flipped normals | Recalc outside | `bmesh.ops.recalc_face_normals` |
| Loose vertices | Dissolve | `bmesh.ops.dissolve_verts` |
| Zero-length faces | Merge by distance | `bmesh.ops.remove_doubles` |
| Missing UVs | Smart UV Project | `bpy.ops.uv.smart_project` |
| Overlapping UVs | Pack islands | `bpy.ops.uv.pack_islands` |
| Excess geo on flat surfaces | Decimate | modifier |

These are NOT triggered automatically on every execution (too aggressive). They're triggered when the artist's-eye check flags topology/materials/UV issues. The repair script is generated by the LLM (with hints from the failure), validated, and executed via the normal tool path.

**Implementation:** add `mesh_repair_recipes.py` to `ai-backend/personas/` (yes, it's persona-adjacent — the persona owns its repair playbook).

### 6.3 Pre-execution quality

Already implemented in `quality_enforcer.py`:
- Banned imports / calls
- Subdivision level > 8 rejected
- Render samples > N rejected

**Extend with:**
- Cyclomatic complexity cap (no `for` nested deeper than 4)
- Recursion depth limit
- Reject `bpy.context.scene.frame_set` in a loop without `bpy.context.view_layer.update()` (common cause of broken animation eval)
- Reject `bpy.ops.*` calls that require Edit Mode without an explicit mode toggle before them

### 6.4 The auto-retry loop

```
attempt = 0
while attempt < MAX_RETRIES (default 2):
    result = execute(script)
    if result.error:
        feedback = f"Script raised: {result.error}\nFix and re-execute."
        new_script = await llm.followup(feedback)
        attempt += 1
        continue

    verdict = await artists_eye_check(result.hd_capture, intent, persona)
    if verdict.overall == "pass":
        break

    fix_prompt = f"Quality check failed: {verdict.fix_suggestions}.\nProduce a corrected script."
    new_script = await llm.followup(fix_prompt)
    attempt += 1

if attempt == MAX_RETRIES and verdict.overall == "fail":
    # Show the user with a "quality not optimal — want me to keep trying?" notice
    await send_quality_notice(...)
```

The user never sees an intermediate fail unless we hit max retries (which surfaces as a soft notice — "I made progress but it's not quite there. Want me to try a different approach?").

**Status (2026-05-20):** **Phase 5 v1 ships the artist's-eye check + telemetry + the soft `quality_notice` WS surface, but NOT the auto-correction loop.** The retry loop is deferred to **Phase 5.5** because:
  • Multi-turn coordination requires the orchestrator to keep `stream_response` alive across multiple LLM/tool round-trips within one user message. That's a real architectural step beyond the current "one user_message → one stream_response → tool_call → tool_result → done" model.
  • Phase 5 v1 gives us the verdict + the user-visible signal, which is enough to test prompt quality and persona accuracy in production without the additional auto-fix infrastructure.
  • The `mesh_repair_recipes.py` declarative library is in place (`ai-backend/personas/mesh_repair_recipes.py`) so when Phase 5.5 lands, the auto-fix loop has a structured input ready.

What Phase 5 v1 DOES today:
  1. After every `tool_result`, `main.py` fires `_run_quality_check(...)` (async, non-blocking).
  2. `orchestrator/quality.py:run_artists_eye_check` polls `vision_buffer` for the `post_script` HD capture (Phase 2 trigger), then calls Sonnet vision with the persona-aware `prompts/artists_eye.py` prompt.
  3. The verdict is parsed into `ArtistsEyeVerdict`. Both pass and fail emit telemetry on the event bus (`quality.passed` / `quality.failed`).
  4. On fail, a `quality_notice` WS message is sent to the addon with the failed checks, fix suggestions, and confidence — the user sees what was flagged.
  5. The user can then ask the AI to fix it; the next user message goes through the same loop with the verdict's context implicit in the conversation history.

What Phase 5.5 will add (next round if user asks):
  • The orchestrator wraps tool dispatch in a retry loop that, on failed verdicts, sends a follow-up "fix this: <suggestions>" prompt to the LLM and re-executes — invisibly to the user, up to MAX_RETRIES.
  • `mesh_repair_recipes` becomes a structured input to the LLM follow-up (current persona + failed_check → recipe.bmesh_pattern → guidance in the fix prompt).

### 6.5 Telemetry

Every retry is logged with: intent, persona, model, attempt count, verdict, fix categories. Used to tune persona prompts and identify systemic weak spots.

---

## 7. Phase 6 — System Prompt Architecture

**Goal:** modular, cacheable, A/B-testable prompts.

### 7.1 Layered composition

System prompt = concatenation of layers, all cacheable up to and including the persona:

```
┌─ Layer 1: Identity & non-negotiables ────────────────────┐  ← shared, cached
│  "You are Animora, …"                                    │
│  Absolute rules 1–7 (from §5.4)                          │
│  Quality standards                                       │
├─ Layer 2: Tool catalog ─────────────────────────────────┤  ← shared, cached
│  Description of execute_blender_script, get_object_info, │
│  render_preview, suggest_next_steps, etc.                │
├─ Layer 3: Persona ──────────────────────────────────────┤  ← per-persona, cached
│  Environment Artist body + knowledge sections +          │
│  workflow patterns + few-shot examples                   │
├─ Layer 4: Session memory summary ───────────────────────┤  ← NOT cached
│  "User is working on project 'Greek temple ruin'.        │
│   Previously created: hero column, fractured base.       │
│   Stylistic preference: warm sunset palette."            │
└─ Layer 5: Live scene context ───────────────────────────┘  ← NOT cached
   (scene graph diff + HD capture as image block)
```

The Anthropic prompt cache TTL is 5 minutes — Layers 1+2+3 stay hot across a continuous session, costing ~$0.30/MTok effective on input (vs $3 list).

### 7.2 Versioning

Each layer is versioned (`base@v3`, `tool_catalog@v2`, `environment_artist@v5`). Version IDs are logged with every request. Lets us A/B test prompt changes safely — flip a percentage of traffic to the new version, compare quality verdicts.

### 7.3 Vision-analysis prompts

The artist's-eye check (§6.1) is its own prompt template. Lives at `ai-backend/prompts/artists_eye.py`.

Similarly:
- `ai-backend/prompts/intent_classifier.py` — Haiku-tier classifier
- `ai-backend/prompts/workflow_planner.py` — Opus-tier multi-step decomposer
- `ai-backend/prompts/clarifying_question.py` — when confidence is low
- `ai-backend/prompts/error_repair.py` — when execution fails

### 7.4 Tool prompts

Each tool's description is part of the system prompt (Anthropic tool format). Keep these descriptions terse and consistent — verbose tool descriptions waste cached tokens.

### 7.5 Open question

- **Where do prompt templates live in source control?** Recommendation: `ai-backend/prompts/*.py` as Python modules (allows interpolation, type-checking, easy import). Avoid `.txt` files because they invite ad-hoc edits without code review.

---

## 8. Phase 7 — Memory & Context System

**Goal:** the AI remembers the project across the session and (selectively) across sessions.

### 8.1 Three memory tiers

| Tier | Lives in | Lifetime | What it holds |
|---|---|---|---|
| Short-term (conversation) | Redis `session:{id}` | session + 30 days (Standard) / 180 days (Studio) / 3 days (Free) | every user/assistant turn, tool calls, results |
| Mid-term (project) | Postgres `projects` | indefinite, per-user | named scenes, recurring stylistic preferences, asset library refs |
| Long-term (org knowledge) | n/a for v1 | — | (future: shared templates across a Studio team) |

Schema sketch for `projects`:

```sql
CREATE TABLE projects (
  id              uuid PRIMARY KEY,
  user_id         uuid NOT NULL REFERENCES users(id),
  name            text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  scene_summary   jsonb,             -- compressed scene description
  preferences     jsonb              -- {"palette":"warm","style":"realistic","render_engine":"cycles"}
);

CREATE TABLE project_events (
  id              bigserial PRIMARY KEY,
  project_id      uuid NOT NULL REFERENCES projects(id),
  ts              timestamptz NOT NULL DEFAULT now(),
  kind            text NOT NULL,     -- 'create','modify','render','quality_pass','quality_fail'
  summary         text NOT NULL,
  payload         jsonb
);
```

### 8.2 Context compression

When conversation history exceeds 50 turns or 100k tokens, summarize the older half into a single "memory" block:

```
SESSION MEMORY (auto-summarized, 47 prior turns)
- Built hero palm tree (geometry nodes wind animation, PBR bark)
- User preferred warmer sun angle (HDRI rotated 30° east)
- Beach terrain extended to 50m × 50m with sand displacement
- Failed twice on water foam — settled on vertex paint approach
- Currently working on: secondary palm cluster, far-distance fog
```

Summarization is a Haiku call, cached aggressively, runs in background between turns.

### 8.3 Prompt cache strategy

- **Persona prompts** → `cache_control: {"type": "ephemeral"}` on the last block. ~3–6k tokens × 0.1× cost = big savings.
- **Project-memory block** → also cached (changes slowly).
- **Scene graph block** → NOT cached (changes every turn).
- **Vision image block** → NOT cached (always fresh).

Expected cache hit ratio: 60–80% of input tokens.

### 8.4 Session resume

Already partially scaffolded in `session_manager.py`. When the WS reconnects with `{type:"resume", session_id}`, the addon receives the last N turns and any in-flight tool calls. Critical for handling Wi-Fi blips during long sessions.

---

## 9. Phase 8 — Real-Time AI Interaction

**Goal:** the chat feels alive, not request/response.

### 9.1 Streaming responses

Already implemented at the SDK level (`messages.stream`). Frontend behavior:

- Tokens arrive → append to last assistant message in `wm.animora_chat_history`, trigger `area.tag_redraw()` on the ANIMORA area.
- Tool calls arrive → render an inline "running script…" indicator below the streamed text; replace with ✓ or ⚠ when result arrives.
- Quality notices → render as subtle inline cards ("polished this twice — looks good now").

Already partially in `operators.py:_on_stream_token`.

### 9.2 Mid-execution feedback

For long-running tool calls (renders, sims), the addon sends progress updates via a new WS message type `tool_progress{tool_use_id, pct, message}`. The frontend renders these as a live progress bar inside the assistant message.

### 9.3 Interrupt

User clicks a "stop" button (or hits Esc) → addon sends `interrupt{reason: "user_cancel"}` → backend aborts the LLM stream and any pending tool calls → addon cancels any running bpy operation (`bpy.ops.render.cancel()`, sim cancel, etc.).

### 9.4 Voice input (blueprint §8 — Deepgram Nova-3, $0.006/min)

- Mic button in panel header → `OT_AnimoraStartRecording` (already stubbed in `operators.py:OT_AnimoraStartRecording`).
- Stream raw audio via WebSocket to `wss://api.deepgram.com/v1/listen?model=nova-3`. Token is fetched from `ai-backend/voice/token` (short-lived; backend holds the master Deepgram key).
- Partial transcripts populate the input field live. Final transcript on silence → auto-submit.

Deferred to Phase 9 of the original blueprint roadmap — not a Phase 1 priority.

### 9.5 Collaborative workflows (later)

Two artists, same project, both with Animora open. AI sees both viewports, both selections. Conflict resolution + presence indicators. Out of scope for v1.

---

## 10. Phase 9 — Evaluation Framework

**Goal:** prove the AI works. Catch regressions. Tune personas. (Naming note: previous drafts called this "Training & Evaluation". There is no model training in this phase or anywhere in Animora — see §0.5. Every activity here is prompt iteration + benchmark scoring on Anthropic's hosted models.)

### 10.1 Benchmark tasks

A curated set of test prompts spanning every persona, each with a rubric:

```
ai-backend/evals/
  benchmarks/
    environment/
      beach_with_trees.yaml
      forest_clearing.yaml
      modular_city_block.yaml
    character/
      stylized_orc.yaml
      humanoid_topology_check.yaml
    hard_surface/
      sci_fi_blaster.yaml
      modular_spaceship.yaml
    ... (~50 benchmarks across all personas)
```

Each YAML defines:

```yaml
name: beach_with_trees
intent: dense_scene
persona: environment_artist
prompt: "Create a beach environment with palm trees, late afternoon."
rubric:
  - id: terrain_present
    weight: 1
    auto_check: "object exists with displacement modifier and bake-able plane"
  - id: water_present
    weight: 1
    auto_check: "object with Ocean modifier OR mesh with water shader"
  - id: trees_scattered
    weight: 2
    auto_check: "≥3 palm objects with geometry-nodes scatter or array"
  - id: hdri_lighting
    weight: 1
    auto_check: "world has HDRI environment texture"
  - id: pbr_materials
    weight: 1
    vision_check: "sand material reads as sand, water reads as water"
  - id: composition
    weight: 2
    vision_check: "scene has fore/mid/background depth"
max_attempts: 2
target_score: 0.85
```

Eval runner: spins up a headless Animora instance, points at a staging `ai-backend`, runs each benchmark, captures the final HD frame + scene state, computes the rubric score (auto checks via scene-diff scripts, vision checks via Sonnet vision call), persists scores to a results DB.

### 10.2 Regression testing

CI job runs the benchmark suite on every backend deploy (or persona-prompt PR). Flags any rubric score that dropped > 5% vs last green run.

### 10.3 Cost regression

Every benchmark run also records: input/output tokens per model, cache hit ratio, retry attempts. Surface in a dashboard; alert if cost-per-task creeps up > 20%.

### 10.4 Quality scoring metrics

- **First-try pass rate** — how often does the artist's-eye check pass on attempt 1? (Target: 70%)
- **Two-try pass rate** — how often within attempts 1–2? (Target: 95%)
- **User satisfaction proxy** — was the response edited/undone by the user within 30 seconds? (Implicit signal of poor quality.)
- **Persona accuracy** — was the right persona chosen? (Spot-check via human eval.)

### 10.5 Open question

- **Reference scenes:** we'll want a small set of "ground-truth" reference scenes that the AI tries to reproduce. Building these is real artist time. Estimate: 2 weeks of one senior 3D artist to author ~20 reference scenes covering all personas.

---

## 11. Cross-Cutting: Telemetry, Cost, Security

### 11.1 Telemetry

Every backend → LLM call emits a structured log line:

```json
{
  "ts": "...", "session_id": "...", "user_id": "...",
  "intent": "dense_scene", "persona": "environment_artist", "model": "sonnet",
  "input_tokens": 4823, "output_tokens": 612, "cache_hit_tokens": 3902,
  "tool_calls": 1, "attempts": 1, "quality_verdict": "pass",
  "elapsed_ms": 2840, "cost_usd": 0.0093
}
```

Aggregated into dashboards: cost per session, per-persona quality, model usage mix.

### 11.2 Cost guardrails

- Per-session ceiling enforced in `auth_middleware.py`: trial $0.50/day, Standard $5/day soft cap, Studio uncapped.
- When ceiling hit: degrade to Haiku-only + notify user.
- All cost numbers logged in cents to avoid float drift.

### 11.3 Security model

- WS connection requires JWT (already scaffolded in `auth_middleware.py`).
- JWT carries `{user_id, plan, device_fingerprint, exp}`. Validated per message.
- Bpy script execution is sandboxed locally (§4.2) — backend can't directly exfiltrate user data.
- Vision frames TTL = 5 min in Redis. HD captures TTL = 1 hour. No persistent storage of user scene content (except their own project saves, which are local).
- Anthropic API key never leaves the backend. Same for Deepgram, Stripe.

---

## 12. Phased Implementation Roadmap

The blueprint's 9 phases map to ~12 weeks of focused engineering effort. Suggested sequencing (parallelizable where independent):

| Wk | Phase | Deliverable | Critical files |
|---|---|---|---|
| 1 | 1 | Replace `SYSTEM_PROMPT_BASE` with master prompt + non-negotiables. Split orchestrator into modules. | `ai-backend/orchestrator/` |
| 1-2 | 2 | Vision: backpressure protocol, post-script HD trigger, scene-graph diff. | `vision.py`, `scene_intelligence.py`, `scene_diff.py` |
| 2-3 | 3 | Execution: sandboxed namespace, async wrapper, timeouts, structured tool_result. | `executor.py` (new), `quality_enforcer.py` extensions |
| 3-5 | 4 | Personas: intent classifier + 3 personas (Environment, Hard Surface, Lighting). Adds the rest in week 6-7. | `ai-backend/personas/`, `prompts/intent_classifier.py` |
| 5-6 | 5 | Quality enforcer: artist's-eye check + retry loop + mesh-repair recipes. | `prompts/artists_eye.py`, `orchestrator/retry.py` |
| 6 | 6 | Prompt versioning, A/B harness. | `prompts/` module + version registry |
| 7 | 7 | Memory: Redis schema, Postgres projects table, summarization on overflow. | `session_manager.py` extensions, new `project_memory.py` |
| 8 | 8 | Streaming polish: progress, interrupt, panel UI for inline tool indicators. | `operators.py`, `panel.py` |
| 9 | 4 (rest) | Add Character, Animator, VFX, Game Dev, Compositor personas. | `ai-backend/personas/` |
| 10-11 | 9 | Eval framework, 20 reference scenes, regression CI. | `ai-backend/evals/` |
| 11 | 8 (voice) | Deepgram voice input + Animora panel mic flow. | `voice/`, `OT_AnimoraStartRecording` |
| 12 | — | Hardening, observability dashboards, cost tuning. | Telemetry pipeline |

Dependencies that must resolve before week 1:
- Anthropic API key + billing account
- Hosting decision for `ai-backend` (§2.5)
- Redis instance (any plan), Postgres instance (Supabase or new)
- Deepgram account (can defer to week 11)

---

## 13. Decisions

### Resolved by user (2026-05-18)

1. **Backend hosting target → AWS Fargate.** Maximum scalability up front; accept the higher ops complexity. Implication: need a `Dockerfile` for `ai-backend`, an ECR repo, an ALB in front of the Fargate service (sticky sessions on WS), Secrets Manager for the Anthropic key. Region: start `us-east-1`; add `eu-west-1` once non-US users appear.
2. **Database → reuse Supabase.** Project-memory tables (`projects`, `project_events`) live in the same Postgres as the website's auth/billing tables, separate schemas. Use Supabase's connection pooler from `ai-backend`. No new DB to operate.
3. **Persona depth at launch → 3 deep + generalist.** Environment Artist, Hard Surface Artist, Lighting TD, plus a generalist fallback for `unknown` / Q&A intents. Other 6 personas added post-alpha.
4. **Eval scene authoring → seed with public CC scenes.** Use Blender demo files + BlenderKit CC0 + Polyhaven HDRIs as the v1 benchmark set. Cheaper and faster than commissioning; accept that coverage is less tailored. Backfill with custom scenes when product is in user hands.

### Resolved by user (2026-05-18, second round)

5. **Telemetry → Grafana Cloud.** Free tier for alpha (10k metrics, 50 GB logs). OpenTelemetry SDK in `ai-backend`. Source: CloudWatch (Fargate logs) → forwarded to Grafana via OTel collector. Dashboards: per-persona quality verdict, per-model cost mix, retry rate, p50/p95 latency by intent.
6. **Render strategy → two-pass.** 32-sample denoised preview (~5 s) for the artist's-eye check, then 256+ sample final once the check passes. Implementation: the executor exposes `render_preview` (fast, for quality check) and `render_final` (full, for user-facing output) as separate tool calls. Cuts vision-token spend by ~70%. Blueprint-compliant because the *user-visible* output is still 256+ sample film-grade — only the QA-only render is fast.
7. **Cost ceiling for trial → $1.00/day.** ≈ $3.00 per 3-day trial user. Generous enough that the trial can build a full project end-to-end without hitting limits mid-session. Enforcement lives in `auth_middleware.py` (decrements a Redis-tracked daily counter per session message).

---

## 14. Out of scope for this plan

- Authentication / OAuth / device binding — covered by existing `auth-server/` plan, not blocked by AI work.
- Billing — Stripe integration is independent.
- Website — `website/` is its own track.
- macOS / Linux installers — Windows-first; the AI architecture is platform-agnostic.
- 3D-generation external APIs (Meshy, TripoSR, etc.) — explicitly forbidden by blueprint §1: "100% native Blender tools".
- Distillation / fine-tuned local model — Claude API only. Local inference is a separate (much later) cost-optimization play.

---

## 15. Glossary

- **The Brain** — the Claude-powered cloud orchestrator that plans and decides.
- **The Hands** — the in-Animora Python executor that runs bpy scripts.
- **The Eyes** — the Scene Intelligence Engine (vision stream + HD captures + scene graph).
- **Persona** — a specialist system-prompt module that biases the Brain toward a discipline.
- **Artist's-eye check** — post-execution Claude vision call that decides whether the output meets maximum-quality bar.
- **Workflow Selection Matrix** — blueprint §7.3, the table that maps intent to bpy-tool approach.
- **SPACE_ANIMORA** — the C++ editor type that hosts the AI panel inside Animora. Already implemented.
