---
name: animora-quality-gates
description: Use when defining or tuning what "good output" means — editing the artist's-eye checklist, composition rules, critic checks, eval scoring, or adding a new quality gate. Triggers include "artist's eye", "composition rubric", "quality check", "why did the eval fail", "critic_score", "grey couch", "default grey", "scene looks flat", "add a quality gate". Turns taste into testable criteria.
---

# Animora quality gates — taste as testable criteria

Quality is enforced at four layers; know which layer a problem belongs to before editing anything.

## Layer 1 — Pre-execution safety (hard block)
`ai-backend/quality_enforcer.py::validate_script()` — AST + regex banlist (imports: os/subprocess/sys/shutil/socket/urllib/requests/httpx/pathlib/importlib/ctypes/multiprocessing/threading/asyncio/pickle/marshal; builtins: open/eval/exec/compile/__import__/getattr/globals/locals/vars/input/breakpoint; methods: read_text/write_text/system/popen/…). Also `max_script_length=160k`, poly-delta and render-sample caps (`config.py`). This layer is SECURITY, not taste — changes need a security review, not an eval run.

## Layer 2 — Deterministic critic (live scene graph)
`ai-backend/orchestrator/critic.py` (+ `first_step_diagnosis`) — code-checked facts, no LLM: object counts, transforms, material presence, lighting/camera existence. Cheap, runs inside the loop (Stage 3A, ≤2 corrections/turn). Add a check here when the failure is **objectively detectable from the scene graph**.

## Layer 3 — Mechanical prompt-gates (loop layer)
Four single-shot gates in `streaming.py` (first-step foundation :1424, scene-floor part-count :1472, material completeness :1524, finished-by-default lighting+camera :1583). Each fires at most once per turn and injects a `[ANIMORA <NAME> GATE]` corrective user message. Add a gate here when the failure is a **pattern of tool-call behavior** (e.g. "built 3 objects for a 'kitchen'") rather than a scene-graph fact.

## Layer 4 — Artist's-eye vision check (LLM judgment)
`prompts/artists_eye.py` checklist applied by `orchestrator/quality.py::run_artists_eye_check()` on the checkpoint capture; verdict drives Phase-5.5 retry. The canonical failure list (also in `personas/base.py`):
- Empty/flat background (no atmosphere, horizon, scatter)
- Single light source (flat look)
- Visible faceting (missing/wrong Subdivision)
- Material reads as Blender-default grey
- Geometry ends abruptly, no transition zone
Composition rubric: `prompts/composition_rules.py` — focal point, depth layering (fore/mid/background), balance, camera framing. The whole-scene art-director pass lives in `orchestrator/final_review.py` + `prompts/final_review.py`.

## Eval scoring (how quality is measured, not enforced)
`ai-backend/eval/scoring.py`: per-benchmark regex/deterministic gates (named datablocks, materials present, token budgets) + `critic_score` (0–1). `eval/benchmarks.py` defines the suite incl. the beat-the-MCP set (composition is the MCP's known weak spot — keep those benchmarks strong). CI regression gate (`.github/workflows/eval.yml`) trips on: newly failing benchmark, category pass-rate −10pp, benchmark critic_score −0.15 ("grey-couch-that-still-emits-primitive_cube_add"), category mean −0.10, or cost up with no quality gain.

## Procedure: adding a quality criterion end-to-end
1. Classify: scene-graph fact → Layer 2; behavior pattern → Layer 3; visual judgment → Layer 4 checklist text.
2. Implement the check (+ single-shot guard if Layer 3, corrective message that names the fix).
3. Add/extend a benchmark in `eval/benchmarks.py` that a violating output FAILS, and a scoring rule in `scoring.py` if deterministic.
4. Add a unit test (`test_stage2_critic.py` / `test_phase5_quality.py` pattern — use `assert`, not `return`).
5. Re-baseline deliberately: run the full suite, review the report, commit `eval/baseline.json` in the same PR with a note. Never let CI re-baseline implicitly.

## Pitfalls
- Prompt-text changes to `master_prompt.py`/personas invalidate the prompt cache AND can shift eval scores — treat as eval-gated changes.
- Vision checks cost Sonnet calls; keep them at checkpoints (batching), never per-iteration.
- A gate that fires repeatedly per turn will loop the model — every Layer-3 gate must stay single-shot.
