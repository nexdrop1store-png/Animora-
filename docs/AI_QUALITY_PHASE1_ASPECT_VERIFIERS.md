# V2 AI Quality Build Plan — Phase 1: Aspect Verifiers

**Scheme note**: same numbering as `docs/AI_QUALITY_PHASE0_BASELINE.md` (the separate AI-quality
build plan, not repo-internal phases, not `docs/V2_PHASE0_AUDIT.md`). Builds on that baseline's
corrected 14-task run (`phase0_baseline_run.json`, mean 4.29/10).

**Date:** 2026-08-04. **Scope:** `docs/ROADMAP-V2-notes.md` §B2 item 2 — "Implement the artist's-eye
checklist as 8 separate VLM aspect-verifiers... select best-of-N by combined verifier score."
Best-of-N is explicitly deferred to a later phase (needs the verifier split validated first);
this phase is the split + validation only.

---

## 1. What was built

- **`ai-backend/prompts/aspect_verifiers.py`** — 8 fixed, domain-agnostic aspects: silhouette,
  proportion, topology/construction, placement, composition/framing, materials, lighting,
  technical correctness. One prompt template, parameterized per aspect, explicitly instructs the
  grader to judge *only* its own lane ("other graders on this panel cover every other angle... stay
  in your lane").
- **`ai-backend/orchestrator/aspect_verifiers.py`** — fires all 8 as concurrent Sonnet vision calls
  (`asyncio.gather`, not sequential — same wall-clock as one call), returns one `AspectResult` per
  aspect (verdict/reason/fix_suggestion/confidence/token usage). No dependency on `quality.py`
  (avoids a circular import — `quality.py` depends on this module, not the reverse).
- **`ai-backend/orchestrator/quality.py`** — wired in behind `ANIMORA_ASPECT_VERIFIERS` (default
  **off**, same rollout pattern as `ANIMORA_ENABLE_SPEC`/`ANIMORA_ENFORCE_LOOP`). When on, the 8
  `AspectResult`s are folded into the existing `ArtistsEyeVerdict`/`CheckResult` shape so
  `retry.py`, telemetry, and existing tests need zero changes regardless of which path ran.
  Verified: **240 passed, 1 skipped, 0 failed** on the full `ai-backend/tests` suite after this
  change.
- **`ai-backend/eval/render_harness.py`** — added `_score_with_aspect_verifiers` as a second,
  parallel scoring leg alongside the existing holistic VLM call, given all 3 render viewpoints per
  call (multi-view, per the roadmap note). Both legs now run on every task so they're directly
  comparable against the same renders.

## 2. Validation method

Reused `--resume-json` (added in Phase 0) against the already-corrected `phase0_baseline_run.json`
— re-rendered the same 14 tasks' already-captured tool_calls (free, local) and ran both scoring
legs. **No LLM generation re-cost**; only new spend was the vision-scoring calls themselves:
holistic $0.0941 + aspect-verifier $0.8411 = **$0.94 total** for this validation run. Results in
`phase1_aspect_verifier_run.json`.

## 3. Results — holistic score vs. aspect-verifier overall

| task | holistic (1-10) | aspect overall | failed aspects |
|---|---|---|---|
| geometry.cube_dimensions | 7 | pass | — |
| geometry.table_leg | 2 | fail | Silhouette, Topology, Composition, Lighting |
| geometry.stone_archway | 2 | fail | Silhouette, Proportion, Topology, Placement, Materials |
| geometry.low_poly_tree | 2 | fail | Silhouette, Proportion, Topology, Placement, Composition, Technical |
| placement.dining_table_setting | 1 | fail | Silhouette, Lighting |
| placement.bookshelf_arrangement | 1 | fail | Silhouette, Proportion, Topology, Placement, Composition, Materials, Lighting, Technical |
| placement.picnic_scene | 7 | **fail** | Composition/framing only |
| placement.parking_row | 8 | pass | — |
| lighting.three_point_portrait | 6 | **fail** | Composition/framing, Lighting |
| lighting.warm_sunset_interior | 1 | fail | Lighting |
| lighting.moody_spotlight | 7 | **fail** | Composition/framing, Lighting |
| materials.glossy_red_metal | 7 | pass | — |
| materials.rough_wood_plank | 8 | pass | — |
| materials.glass_bottle | 1 | fail | Topology, Materials, Lighting |

**11/14 agree** (both "good" or both "bad"). **3/14 disagree** — all three are cases where the
holistic score landed at a passing 6-8/10 despite the score's own prose *notes* already flagging
the same issue the aspect verifier failed on. This is a **policy difference, not obviously a
correctness difference**: the aspect-verifier overall is a strict AND-gate (any one aspect failing
flips it to fail), while the holistic 1-10 is a weighted impression that can absorb one flaw and
still clear a "pass" threshold. Whether the strict gate is the right policy for a production
quality gate is a judgment call for whoever owns the retry-trigger threshold — flagging the
distinction rather than presenting the aspect verifier as unambiguously "more correct."

## 4. Where decomposition added real, checkable value

This is the actual claim worth trusting or distrusting, so the evidence:

- **`picnic_scene` and `moody_spotlight` independently rediscovered the known camera-framing
  limitation**, in specific and correct language, without being told about it:
  - picnic_scene: *"the picnic set is extremely small relative to the large ground plane... the
    top-down view is better but the subject still occupies only [a small fraction]"*
  - moody_spotlight: *"the cube is rendered very small relative to the frame (occupying roughly
    10-15% of the image area)"*
  Both are the exact ground-plane-inflates-bounds artifact documented as a caveat in the Phase 0
  report — now something the system can flag *automatically*, per-task, instead of relying on a
  human noticing it in a PNG.
- **`glass_bottle` isolated the actual defect precisely**: Silhouette/Proportion/Placement all
  passed (the object's basic shape, scale, and position are fine) while Topology/Materials/Lighting
  failed — matching the holistic note ("glowing white emission object, not a bottle") but adding
  the useful negative information that geometry/placement are NOT the problem, which the holistic
  note didn't establish.
- **`stone_archway` surfaced something the Phase 0 manual investigation missed**: the Materials
  verifier flagged the arch's surface as *"smooth, light-gray/white metallic-looking... not
  stone"* — a real defect (the escape-hatch script's own limestone material apparently isn't
  reading as intended) that wasn't part of the ordering-bug diagnosis in Phase 0 and would have
  gone unnoticed under holistic-only scoring.
- **`moody_spotlight`'s Lighting verifier caught a specific technical detail**: *"the cube's side
  faces are rendered as dark grey rather than near-black, suggesting ambient/fill light
  contamination rather than a single hard spotlight"* — more actionable than the holistic note's
  vaguer "halo somewhat soft."

## 5. A calibration issue the process caught in itself

`lighting.warm_sunset_interior` renders completely black (confirmed in Phase 0 — the orchestrator
dispatched an `execute_animora_code` call with no `script` field at all). Its Topology verifier
returned **"pass"** (confidence 0.40) with reasoning: *"the images are entirely black so no
geometry is visible, but the brief calls for a simple box interior... which is an appropriate
single-part construction for that prompt."* That's the grader reasoning from what the brief implies
rather than from what it can actually see — a real prompt-calibration weakness (a grader should say
`n/a`, not infer a pass, when it has zero visual evidence). It did NOT corrupt the outcome here —
confidence was low and Lighting still correctly failed at 0.98 confidence, so `aspect_overall`
still resolved to the correct "fail" — but it's a known rough edge worth tightening in a future
prompt revision, not something to paper over.

**Infra note:** 3 of 112 aspect-verifier calls (2.7%) timed out under concurrent rate-limiting
during this run (visible in the run log as `anthropic.client.messages_create.rate_limited` retries).
Timeouts are treated as `verdict="error"` and excluded from the pass/fail decision rather than
silently counted as failures or passes — none of the 3 timeouts changed an `aspect_overall` result,
but this is worth monitoring if aspect verifiers go into a higher-throughput path later.

## 6. Cost/latency tradeoff (explicit, not buried)

8 concurrent calls cost ~9x one holistic call in dollars (confirmed: $0.84 vs $0.09 for the same
14 tasks) but **not** ~8x in wall-clock latency, since they run concurrently via `asyncio.gather` —
the eval log shows each task's aspect-verifier step completing in roughly the time of the slowest
single call, not the sum. In a live per-iteration production check (single capture, not 3
viewpoints), this scales to roughly $0.05-0.07/check depending on image size vs. today's ~$0.009 —
non-trivial but bounded, and this is exactly why the flag defaults off pending an explicit decision
to adopt it broadly.

## 7. Recommendation

- Keep `ANIMORA_ASPECT_VERIFIERS` **off** in production for now — this phase validates the split
  works and adds real value, not that the cost tradeoff has been signed off on.
- **Adopt the aspect-verifier leg as the Phase 0 harness's primary "automatic" scoring signal**
  going forward alongside (not replacing) the holistic score — the per-aspect `fix_suggestion`
  fields are exactly the shape Phase 5.5's retry loop already consumes (`retry.py` reads
  `verdict.fix_suggestions`), so this is a natural on-ramp for a later phase, not new plumbing.
- Tighten the aspect-verifier prompt so a grader with zero visual evidence returns `n/a`, not an
  inferred pass from brief text alone (the §5 finding) — small, targeted prompt fix, not urgent
  since it didn't change any outcome in this run.
- Best-of-N selection (roadmap item 3) is now unblocked — the verifier split it depends on is
  built and validated.

**STOPPED here per the plan's governing rule — awaiting explicit "approved, continue" before
Phase 2.**
