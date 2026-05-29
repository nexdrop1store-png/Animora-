# Animora AI quality eval

The eval harness is how we tell whether a prompt edit, persona change,
or model bump *actually improved* Animora's output quality — or quietly
regressed it.

## TL;DR

```bash
# Run all benchmarks (~$0.60, ~5-15 min)
python ai-backend/eval/runner.py --json run.json --output report.md

# Run a single benchmark or category
python ai-backend/eval/runner.py --filter primitive.cube
python ai-backend/eval/runner.py --filter vehicle

# Compare against baseline (CI does this automatically)
python ai-backend/eval/runner.py --baseline ai-backend/eval/baseline.json --fail-on-regress

# Re-score a saved run without spending API credits
python ai-backend/eval/runner.py --skip-llm --input-json prior_run.json
```

## What it does

For each benchmark in [`ai-backend/eval/benchmarks.py`](../ai-backend/eval/benchmarks.py):

1. Sends the benchmark prompt through the real orchestrator
   (`stream_response` — same code path the panel uses).
2. Captures the `bpy` script the model emits via the
   `execute_blender_script` tool call.
3. Validates the script through [`quality_enforcer.validate_script`](../ai-backend/quality_enforcer.py)
   — the same AST + regex gate that runs in production.
4. Scores the script against the benchmark's:
   - `required_ops` — regex patterns the script MUST contain
   - `forbidden_ops` — regex patterns the script MUST NOT contain
     (plus `GLOBAL_FORBIDDEN_OPS` — deprecated Blender API calls
     applied to every benchmark)
   - `required_named` — at least one `obj.name = "..."` assignment to
     a non-default name (not "Cube"/"Sphere"/...)
   - `require_material` — Principled BSDF material setup present
   - `budget_tokens` — output stayed within reasonable token budget
   - Truncation, validator rejection, intent classifier accuracy

A benchmark PASSES if every hard-fail condition holds. Soft-fails
(over budget) are noted but don't flip the result.

## Categories

Benchmarks are named `<category>.<subname>` so they aggregate:

| Category | What it tests |
|---|---|
| `primitive` | Simple shape creation — the model shouldn't substitute (cube → sphere) |
| `vehicle` | Hero asset generation — proportions, no truncation on the Lamborghini scenario |
| `character` | Figure construction — anatomy basics |
| `furniture` | Multi-part assembly — naming hierarchy |
| `lighting` | Scene lighting — three-point setup, color temperature |
| `scene` | Multi-element compositions — placement, scale relationships |
| `question` | Non-execution intents — the model should answer, not run code |

## The baseline + regression gate

[`ai-backend/eval/baseline.json`](../ai-backend/eval/baseline.json) is the
frozen pass/fail per benchmark for the version on `main`. CI runs the
harness on every PR that touches `ai-backend/`, `addons/animora_panel/`,
or `patches/` and compares the result against this baseline.

A **regression** is either:
- A specific benchmark that PASSED on baseline but FAILS on the PR, OR
- A category whose pass rate dropped by ≥10 percentage points

Either condition fails the build. The PR comment shows the scorecard.

### Newly-passing benchmarks (good news)

If your change makes a previously-failing benchmark pass, the report
calls that out and instructs you to re-freeze the baseline:

```bash
python ai-backend/eval/runner.py --output-baseline ai-backend/eval/baseline.json
git add ai-backend/eval/baseline.json
git commit -m "Re-freeze eval baseline after Phase 5.5 retry wire-up"
```

**Only re-freeze after the merge.** Re-freezing in the PR itself defeats
the gate — anyone could lower the baseline alongside their own
regression. Convention: a separate one-line PR after the feature PR
merges, with a link to the run that shows the gains.

## Adding a benchmark

Edit [`ai-backend/eval/benchmarks.py`](../ai-backend/eval/benchmarks.py)
and append a `Benchmark(...)` entry. The dataclass is documented at the
top of that file. Then run the harness once to capture current behavior
and update the baseline.

Bias new benchmarks toward **failure modes you've actually seen** —
the existing 12 each map to a specific regression we've shipped fixes
for. Don't add aspirational checks; add safety nets for known bugs.

## Running offline (no API credits)

If you've previously captured a run with `--json run.json`, you can
re-score it with current scoring rules without re-running the LLM:

```bash
python ai-backend/eval/runner.py --skip-llm --input-json run.json
```

This is the right tool for:
- Validating a change to `scoring.py` (your scoring update can be
  proven correct against a known dump)
- Reproducing a CI failure locally (download the CI artifact, rescore)

## Cost

| Run shape | Cost | Time |
|---|---|---|
| Full suite (12 benchmarks, Opus 4.7 + extended thinking) | ~$0.60 | 5-15 min |
| Single benchmark (`--filter primitive.cube`) | ~$0.05 | ~15 s |
| Single category (`--filter primitive` = 5 benchmarks) | ~$0.25 | 1-3 min |
| `--skip-llm` rescore | $0 | <1 s |

The CI workflow ([.github/workflows/eval.yml](../.github/workflows/eval.yml))
runs on PRs that touch AI surface paths only, not on doc/website PRs.

## What the harness can and cannot measure

✅ **Can measure** — single-shot quality. Each benchmark sends one prompt
to the orchestrator, captures the bpy script the model emits on
iteration 0, and scores it. This is the right tool for catching
regressions in the master prompt, persona prompts, intent classifier,
router, and `quality_enforcer` AST gate.

❌ **Cannot measure (yet)** — the Phase 5.5 auto-retry contribution.
The harness invokes `stream_response` without a `ToolResultCoordinator`,
which means the agentic loop exits after iteration 0 without ever
running the inline artist's-eye check or building a revision-context
message. As a result, an eval run with `ANIMORA_QUALITY_RETRIES=2`
produces the same scores as a run with `ANIMORA_QUALITY_RETRIES=0`.

To validate Phase 5.5 today, run the **manual panel smoke**:

1. Launch `python ai-backend/dev_server.py`.
2. Open Animora, point it at `ws://localhost:8000/ws`.
3. Pick a prompt that historically fails the artist's-eye check on
   first attempt (e.g., a complex hero asset with materials).
4. Send it. Watch the panel for the `quality.retrying` status pill,
   and confirm the second result is meaningfully better than the first.
5. Tail `dev_server` logs for `quality.retry_succeeded` events.

**Future work** to put retry on the regression gate: add a tool_result
simulator to `runner.py` that stubs the addon's response (a no-op
"OK" outcome + a synthetic HD capture). Then artist's-eye can run
inside the eval loop and we can measure retry's score lift in CI.

## When to NOT re-run the eval

- Doc-only changes
- Branding/installer/build pipeline changes
- Auth / billing / website changes
- Anything that doesn't change the prompt, persona, router, or
  quality-enforcer

When in doubt, run it — $0.60 is cheaper than discovering the
regression in production.
