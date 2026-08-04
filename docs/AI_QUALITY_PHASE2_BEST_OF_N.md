# V2 AI Quality Build Plan — Phase 2: Best-of-N Selection

**Scheme note**: same numbering as `docs/AI_QUALITY_PHASE0_BASELINE.md` and
`docs/AI_QUALITY_PHASE1_ASPECT_VERIFIERS.md`. Builds directly on Phase 1's aspect verifiers, which
this phase depends on for candidate scoring.

**Date:** 2026-08-04. **Scope:** `docs/ROADMAP-V2-notes.md` §B2 item 3 (test-time scaling / best-
of-N) plus the tail of item 2 ("select best-of-N by combined verifier score").

---

## 1. Headline

Best-of-N was built and piloted on 5 tasks (N=3 each, 15 independent fresh orchestrator runs).
**Its value is real but conditional, not a blanket win.** It substantially rescued one task that
was failing due to run-to-run variance (1/10 → 6/10), did nothing for three tasks that failed
*consistently* the same way across all 3 independent attempts (systematic capability gaps, not
variance), and — most importantly for trusting any of these numbers — exposed that the eval
harness's known `load_asset` gap (documented as a minor caveat in Phase 0) is a bigger, more
score-distorting confound than previously understood. A smoke test also caught a real safety bug
in the first version of the candidate-selection scoring formula before it could contaminate a
real pilot run — worth reading before the results table, since it's the part most likely to matter
for whoever builds on this next.

## 2. What was built

- **`ai-backend/eval/render_harness.py`**: `--best-of-n N` (default 1 = today's behavior). For
  N>1, runs N fully independent orchestrator passes per task (fresh `session_id` each — not N
  variations of one run), scores every candidate with both existing legs (holistic VLM + Phase 1's
  8 aspect verifiers), and keeps the highest-`_combined_aspect_score` candidate. All N candidates'
  summary scores are recorded on the winner's `candidates` field for audit; the winner's renders
  are copied to the task-level output path so downstream tooling doesn't need to know it was a
  best-of-N run. Mutually exclusive with `--resume-json` (best-of-N needs fresh generation, not
  reused captured tool_calls).

## 3. A real bug found before it could contaminate results

The first version of `_combined_aspect_score` ranked candidates by aspect-verifier pass-fraction
alone. A 1-task, N=2 smoke test on `geometry.cube_dimensions` (done deliberately before spending on
the full pilot) caught it selecting the **worse** candidate: one build scored 7/10 holistically
("technically competent... proportions and scale match the brief"), the other scored 4/10
("reads more as a tiny decorative element... proportions and scale do not match the brief" — it
had built something proportioned correctly but sized like a wall-mounted light fixture, not a
2m×1m×0.5m block). **All 8 aspect verifiers passed the wrong-scale candidate.** Root cause: none
of the 8 aspects (silhouette, proportion, topology, placement, composition, materials, lighting,
technical) is actually responsible for checking a build's *absolute* scale against the brief's
stated numbers — "Proportion" only judges *relative* ratios between parts, which a
correctly-proportioned-but-wrong-size object satisfies just as well as a correctly-sized one. A
vision model has no ruler in the frame; only the holistic check — which explicitly compares
against the brief's text — has a real shot at catching that failure class.

**Fix**: `_combined_aspect_score` is now a genuine even blend (`0.5 * vlm_score/10 + 0.5 *
aspect_pass_fraction`), with a hard-ish cap (0.35) applied whenever the holistic judge's
`matches_brief` is `False` — not zeroed, since if every candidate fails to match the brief,
ranking among them by remaining quality still beats a coin flip. Re-ran the same smoke test after
the fix: correctly selected the 7/10, `matches_brief=True` candidate over a 3/10 one. This is
worth flagging prominently for whoever extends this: **an aspect-verifier-only selection
criterion is not safe to ship** — it structurally cannot catch a whole class of real failures
(anything about absolute quantities/counts/scale stated in the brief), because none of the 8
aspects are scoped to check them.

## 4. Pilot: 5 tasks, N=3

Chosen deliberately: 4 previously low-scoring tasks (to see if resampling helps) plus 1
previously high-scoring task as a control (`materials.rough_wood_plank`, 8/10 in Phase 0 — to
check best-of-N doesn't *hurt* an already-good case). Total pilot cost (generation + both scoring
legs, all 15 candidates): **$17.04**.

| task | Phase 0 (single-shot) | best-of-3 selected | all 3 candidate scores |
|---|---|---|---|
| geometry.table_leg | 2 | 1 | [1, 1, 1] |
| geometry.low_poly_tree | 2 | 2 | [2, 2, 2] |
| lighting.warm_sunset_interior | 1 | **6** | [6, 2, 1] |
| materials.rough_wood_plank (control) | 8 | 2 | [2, 2, 1] |
| materials.glass_bottle | 1 | 1 | [1, 1, 1] |

## 5. Reading the results honestly, task by task

**table_leg, low_poly_tree, glass_bottle — best-of-N did nothing, and that's a real finding, not
a null result.** All 3 independent candidates for each task failed in *nearly identical ways*
(same failed aspects each time). None of these three winning candidates used `load_asset`, so
they're a clean read, unconfounded by the harness gap discussed below. Resampling the same broken
approach three times doesn't produce a good one to select — these look like genuine, reproducible
capability gaps (the model doesn't currently know how to build a good table leg or low-poly tree
reliably), not bad luck. **Best-of-N cannot fix a systematic failure**, only a variance-driven one.

**warm_sunset_interior — the clear positive case, and a real one.** Phase 0 flagged this task's
1/10 as caused by the orchestrator dispatching `execute_animora_code` with a missing `script`
field (a truncation-shaped bug, inherently stochastic). This pilot's 3 candidates scored 6, 2, 1 —
exactly the kind of high-variance spread that predicts best-of-N should help, and it did: the
selected 6/10 candidate is a legitimately sophisticated build (full room with floor/ceiling/walls/
windows sized for "sunset through one wall," a golden-hour HDRI plus a properly angled sun lamp, an
emissive "exterior glow" plane to sell the light source, volumetric scatter for god rays, explicit
Filmic color management). This is the case for wiring test-time scaling into exactly the failure
mode Phase 0 identified: truncation/generation-variance bugs, not capability gaps.

**rough_wood_plank (control) — a regression, but not a real one.** All 3 fresh candidates scored
1-2/10 despite the task scoring 8/10 in Phase 0. Traced the actual tool_calls: **every fresh
candidate** called `load_asset("texture.weathered_oak")` for the wood texture, then ran an
`execute_animora_code` script that tries to *adjust* that texture's material (e.g. "set roughness
to 0.9") — but the harness deliberately doesn't implement `load_asset` (documented in Phase 0 §3.3
as a network-dependent gap), so there's no texture to adjust, and the plank falls back to
Blender's default smooth gray material — exactly what the holistic score described ("bright
specular hotspot... no visible wood texture... smooth, grey, highly reflective"). **The original
Phase 0 8/10 run took the same `load_asset` path but then self-corrected**: after a `get_scene_info`
check, it called a plain `apply_material` with an explicit hand-chosen brown color and roughness —
a real, resilient fallback behavior. None of the 3 fresh candidates in this pilot happened to
self-correct that way.

This means the `load_asset` harness gap is worse than Phase 0's framing suggested — it's not just
"missing content" for a couple of asset-heavy tasks, it's a **coin flip on whether the agent's own
fallback behavior happens to fire**, and that coin flip can swing a task from 8/10 to 1/10 in this
eval harness regardless of the live product's actual (uninvestigated here) behavior, where
`load_asset` presumably works. **Any comparative scoring involving material/texture tasks should
be treated with real skepticism until the harness has at least a stub/placeholder texture path for
`load_asset`.**

## 6. Cost/latency

$17.04 for 5 tasks × N=3 ≈ **$1.14/candidate average**, roughly in line with 3× a single task's
Phase 0 generation+scoring cost (expected, since it's 3 independent full orchestrator runs, not a
cheap trick). Latency is genuinely 3×, unlike Phase 1's aspect verifiers (which parallelize) —
these are 3 sequential full agentic loops, run sequentially in this harness to avoid a 3×
concurrent-rate-limit storm across already-concurrent aspect-verifier calls.

## 7. Recommendation

- **Don't blanket-apply N=3 to everything.** It was free money for `warm_sunset_interior`
  (variance-driven) and pure waste for `table_leg`/`low_poly_tree`/`glass_bottle` (systematic —
  3× the cost for the identical bad result). This is exactly why roadmap item 3 specifies a
  *tunable generation:verification ratio*, not a fixed N — the natural next step is triggering
  extra candidates conditionally (e.g., only when a first attempt's aspect-verifier fraction is
  borderline, not uniformly), rather than this phase's flat N for every task.
- **Fix the `load_asset` harness gap before running more comparative scoring on material/texture
  tasks.** Even a local placeholder texture (doesn't need to be the real PolyHaven asset, just
  *something* non-default so the agent's material adjustments have a real node tree to act on)
  would remove a confound that's currently large enough to flip a task's apparent quality by 7
  points depending on which code path the agent happens to take.
- The safety fix in §3 (blend holistic + aspect scores, don't rank on aspects alone) should be
  treated as a hard requirement for any future best-of-N or selection work, not specific to this
  phase's implementation.

**STOPPED here per the plan's governing rule — awaiting explicit "approved, continue" before
Phase 3.**
