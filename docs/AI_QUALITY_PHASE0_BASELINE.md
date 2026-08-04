# V2 AI Quality Build Plan — Phase 0 Baseline Report

**Scheme note** (see CLAUDE.md numbering glossary): this is **Phase 0 of the separate AI-quality
build plan** the founder pasted in this session — not repo-internal Phase 0, not the public
roadmap, and distinct from `docs/V2_PHASE0_AUDIT.md` (the whole-repo V2 build-plan Phase 0 audit,
already closed 2026-07-12). This document reconciles with that audit's Finding #9 ("Eval suite:
PARTIAL — suite real, gate dead, CI never green") rather than duplicating it.

**Date:** 2026-08-04. **Provider:** Bedrock (`us.anthropic.claude-opus-4-6-v1` translation),
credential validated this session. **Method:** real headless render + real Claude vision scoring —
per the grounding rule, no score in this report is a self-reflection; every number traces to an
actual rendered PNG and an actual VLM judgment against it.

---

## 1. Headline

The baseline run surfaced two **real, confirmed, fixed bugs in the eval harness itself**
(not the AI) that were silently corrupting a majority of task scores, plus one **strong,
grounded finding about the production orchestrator loop** (not just the harness) worth carrying
into later phases. After both harness fixes, mean score moved from **3.43/10 → 4.29/10** across
the same 14 tasks, using the same captured agent behavior — i.e., roughly a quarter of the
apparent "AI quality gap" in the first pass was actually a measurement artifact, not the agent.
The corrected 4.29/10 is the number to treat as this workstream's actual starting baseline.

---

## 2. What was built

New, harness-only additions (nothing here ships — matches the standing "local/experimental only,
commit for safety" instruction):

- `ai-backend/eval/atomic_to_bpy.py` — translates a captured agent tool-call sequence into one
  directly-executable bpy script (mirrors the addon's real atomic operator bodies).
- `ai-backend/eval/render_worker.py` — runs inside `Animora.exe --background --python`, does the
  actual Cycles/CPU render from 3 fixed viewpoints, prints one JSON result line.
- `ai-backend/eval/render_harness.py` — orchestrates: run the real agentic loop (same
  `stream_response` / `_HeadlessExecutor` path as `runner.py`) → translate → render → score with a
  real Claude vision call against the brief. Now supports `--resume-json` (added this session) to
  re-render + re-score a prior run's captured tool_calls against a fixed translator **without**
  re-spending on LLM generation — used below to validate both harness fixes cheaply ($0.09 vs.
  the ~$22 the original 14-task generation cost).
- `ai-backend/eval/seed_tasks.json` — 14 tasks, 4 categories (geometry, placement, lighting,
  materials), disjoint from the existing 31-benchmark regex-scored suite in `eval/benchmarks.py`.

This is a genuinely new evaluation leg, not a replacement: `runner.py` answers "did the script
contain the right patterns" (cheap, CI-gated); this answers "does the final built scene actually
look right" (expensive, vision-judged) — the leg the AI-quality workstream needs and didn't have.

---

## 3. Two harness bugs found and fixed (grounded in real execution, not inspection)

### 3.1 Escape-hatch script ordering + data-loss bug (`atomic_to_bpy.py`)

**Symptom:** `geometry.stone_archway` scored 1/10 — the render showed pillars and floor but no
arch, despite the agent's tool-call log clearly building one (`apply_material`, `set_parent`,
`add_modifier` all reference an object named `"Arch"`).

**Root cause, confirmed by reading the actual captured tool calls:** the translator emitted every
atomic tool call first (in order), then appended the escape-hatch `execute_animora_code` script
**unconditionally at the end** — regardless of where it actually occurred in the agent's real
sequence. The agent's real plan was sound: build pillars atomically, then use a script for the
arch (bmesh has no curved-arch primitive), then apply materials/parenting to the result. Under the
old translator, `apply_material(Arch)` ran *before* the script that creates `"Arch"` ever executed
— a guaranteed `KeyError`, silently swallowed by the per-call try/except.

**Worse than ordering — real data loss:** 9 of the 14 tasks used at least one escape-hatch script
call, and several used *multiple* (`materials.glossy_red_metal`: 4 calls, `placement.picnic_scene`:
3, `materials.rough_wood_plank` / `materials.glass_bottle`: 2 each, `geometry.stone_archway`: 2).
The old code captured `real_script = captured_script[-1]` — **only the last script call** — so
every earlier script in a multi-script task was discarded entirely, not merely reordered.

**Fix:** tool calls are now translated in strict chronological order; each
`execute_animora_code`/`execute_blender_script` call is inlined using its own `input.script` at
its true position, so later atomic calls that reference what a script created now run after that
script, and no earlier script is ever dropped in favor of a later one.

**Verified by real re-render**, not just code inspection: post-fix, `stone_archway`'s generated
script provably creates the `"Arch"` bmesh object (checked the generated script text directly),
and the arch is now visible in the render.

### 3.2 `IndentationError` on translator skip-branches (`atomic_to_bpy.py`)

**Symptom, found while validating fix 3.1 via a full re-render:** `placement.parking_row` — a task
with *no* escape-hatch scripts at all — failed with `IndentationError: expected an indented block
after 'try' statement`, dropping its score to 1/10 despite the agent's tool calls looking
reasonable.

**Root cause:** all 8 atomic translators (`create_primitive`, `create_light`, `set_transform`,
`add_modifier`, `apply_material`, `set_parent`, `delete_object`, `duplicate_object`) return a
comment-only string (e.g. `"# skipped set_parent: missing child/parent"`) when a call is
malformed. The wrapping code puts every translator's output inside `try: <body> except:` — a
`try:` block containing only a comment has no real statement, which is a Python syntax error, not
a runtime exception, so it couldn't even be caught by the try/except it broke.

**Fix:** the wrapper now always appends a trailing `pass` inside the try body, guaranteeing at
least one real statement regardless of what a translator returns.

**Verified by real re-render:** `parking_row` went from a hard script-execution failure (1/10) to
a clean render (8/10) with zero render errors, confirmed via the fixed harness's actual stdout.

### 3.3 Known, deliberate, documented limitation: `load_asset`

Corrected a wrong comment while in this code: `load_asset` was labeled "read-only/meta" and
skipped. It is not read-only — it downloads and links a real PolyHaven HDRI/texture/mesh
(`addons/animora_panel/operators.py:_load_asset`). It remains intentionally unimplemented in the
harness (would require the render worker to hit the network), but the comment now says so
honestly. `geometry.stone_archway` calls it twice (an HDRI-like "Studio Country Hall" environment
and a "Rock Ground" texture); its post-fix render is real and correct for what the harness *can*
reproduce, but is missing whatever those two assets would have added — treat its 2/10 as a
low-confidence signal specifically for lighting/ground presentation, not for the arch geometry
itself (which is now confirmed correctly built).

---

## 4. A finding about the production loop, not just the harness

Two tasks — `placement.dining_table_setting` and `lighting.warm_sunset_interior` — rendered
**completely black, zero geometry**, both with an unusually short interaction: exactly 2 tool
calls total (`get_scene_info` + one `execute_animora_code`). Inspecting the actual captured input
for that second call in both cases: `input_keys=['intent_summary']` — **no `script` key at all**,
not even an empty string.

Tracing this against `orchestrator/streaming.py:879-893`: the pre-dispatch gate runs
`validate_script(tool_input.get("script", ""))` before anything reaches the addon.
`validate_script("")` trivially passes (nothing to reject), so a tool call missing its `script`
field entirely is dispatched as a **silent no-op** — no error surfaced to the model, no retry
triggered, no signal to the user. Both affected tasks tried to build their entire scene (a full
room; a full table+chairs arrangement) in one large script call, which is consistent with the
model's tool-call generation being truncated before the `script` field was ever emitted.

This is not an eval-harness artifact — the empty input was already missing by the time
`_on_tool_call` received it in the real orchestrator loop, meaning the *production* loop has the
same exposure: a sufficiently large single-script call can silently vanish with no error and no
retry. Flagging as a candidate finding for a later phase (quality gates / retry), not fixing now —
out of Phase 0's scope and this session's "ONE phase at a time" rule. Both tasks' 1/10 scores are
real (nothing rendered), but the underlying cause is this gap, not bad 3D judgment.

---

## 5. Genuine (non-harness) agent-quality finding, for contrast

`placement.bookshelf_arrangement` (1/10, unchanged by both fixes — no escape-hatch scripts, no
translator errors) was checked by cross-referencing every object created against every later
reference (`apply_material`/`add_modifier`/`set_parent`) — **all resolved correctly**, ruling out
a translation bug. The scene is well-proportioned on paper (correct panel/shelf dimensions,
correct parenting). The render shows a blown-out white floor and a thin dark silhouette. Cause:
light energies (300/100/200 for key/fill/rim) are roughly a third of the well-scoring
`cube_dimensions` task's (800/250/400), combined with a bright ambient world (strength 0.3,
near-white color) and an apparently unmaterialed/light floor — plausible overexposure, a genuine
agent lighting-calibration miss, not a measurement problem. Kept as the contrast case: proof the
investigation didn't just excuse every low score as a harness bug.

---

## 6. Results — corrected baseline (post both fixes)

**14 tasks | Mean VLM score: 4.29/10 | Total cost: $22.61** (≈$22.42 real generation, already
spent in the original run, + $0.09 for this session's re-scoring pass) | **Render errors: 0**

### By category

| category | tasks | mean score |
|---|---|---|
| geometry | 4 | 3.25 |
| lighting | 3 | 4.67 |
| materials | 3 | 5.33 |
| placement | 4 | 4.25 |

### Per-task, before → after both fixes

| task | v1 (buggy harness) | v2 (fixed harness) | Δ | notes |
|---|---|---|---|---|
| geometry.cube_dimensions | 7 | 7 | 0 | no scripts used; unaffected, as expected |
| geometry.table_leg | 2 | 2 | 0 | not deep-dived — see §7 |
| geometry.stone_archway | 1 | 2 | +1 | §3.1 — arch now correctly built; pillars/floor still underlit, see §3.3 |
| geometry.low_poly_tree | 2 | 2 | 0 | not deep-dived — see §7 |
| placement.dining_table_setting | 1 | 1 | 0 | §4 — orchestrator-level empty-script finding |
| placement.bookshelf_arrangement | 1 | 1 | 0 | §5 — genuine agent lighting miss, confirmed not a harness bug |
| placement.picnic_scene | 2 | 7 | +5 | §3.1 — multiple lost scripts recovered |
| placement.parking_row | 1 | 8 | +7 | §3.2 — IndentationError fixed |
| lighting.three_point_portrait | 6 | 6 | 0 | no scripts used; unaffected |
| lighting.warm_sunset_interior | 1 | 1 | 0 | §4 — same orchestrator-level finding as dining_table_setting |
| lighting.moody_spotlight | 7 | 7 | 0 | single script, already correctly ordered by luck |
| materials.glossy_red_metal | 7 | 7 | 0 | 4 scripts recovered but net score unchanged |
| materials.rough_wood_plank | 8 | 8 | 0 | 2 scripts recovered but net score unchanged |
| materials.glass_bottle | 2 | 1 | -1 | script(s) now actually execute — went down; likely more accurate, not investigated further |

---

## 7. Honest caveats on this baseline

- **Not every low score was deep-dived.** Investigation effort went to the tasks that revealed
  systemic (harness-wide) bugs — the highest-value use of time, per the grounding rule. `table_leg`,
  `low_poly_tree`, and `glass_bottle`'s remaining low scores are real VLM judgments against real
  renders, but the *reason* behind each hasn't been individually root-caused the way §3–§5 were.
- **Camera framing with ground planes** remains a known limitation carried over from earlier
  session work: `_scene_bounds()` includes any ground/floor plane in the bounding-sphere
  calculation, which can zoom the camera out further than a "hero object" framing would.
- **`load_asset` is unimplemented** (§3.3) — any task leaning on it for environment/texture content
  will underrepresent what a real Animora session would produce.
- **This run used `claude-opus-4-7` → Bedrock's `us.anthropic.claude-opus-4-6-v1`** (the 4.7→4.6
  translation already documented in CLAUDE.md/`llm_provider.py`), not native Anthropic Opus 4.7 —
  worth remembering when comparing against future native-Anthropic runs.

---

## 8. Comparison to existing eval infrastructure (per the plan's "reconcile, don't duplicate")

The existing regex/critic suite (`eval/runner.py`, `eval_v1_report.md`, 2026 run): **26/31 passed,
mean critic 0.92, $20.70 total ($0.67/benchmark)**. That suite measures "did the emitted script
contain the right operations" — single-shot, no render, no vision judgment. It is not directly
comparable to this report's 4.29/10 (different scale, different question). The gap between a 0.92
mean *critic* score and renders that a vision model rates ~4/10 on average is itself a finding:
passing the deterministic/critic checks does not mean the final scene looks right when actually
rendered and judged — exactly the blind spot this render-based leg exists to catch.

Per `docs/V2_PHASE0_AUDIT.md` Finding #9, the regex-suite's CI gate has never gone green (100%
failure rate, all 8 recorded runs) — so today neither eval leg is actually gating anything in CI.
That's a pre-existing, separately-tracked gap, not something this session touched.

---

## 9. Recommendation / next step

This baseline (4.29/10 mean, corrected) is a reasonable Phase 0 starting point *given the harness
is now provably correct enough to trust its ordering and script-capture behavior*. The §4 finding
(orchestrator can silently drop a truncated `execute_animora_code` call) is worth carrying into
whichever later phase covers quality gates/retries — it's a real gap in the shipped product's loop,
not just the eval.

**STOPPED here per the plan's governing rule — awaiting explicit "approved, continue" before
Phase 1.**
