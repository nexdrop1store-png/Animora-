"""
Eval harness runner.

Calls the orchestrator's stream_response directly (no WebSocket, no
addon) against each benchmark, captures the generated script + token
usage + intent + model + truncation status, and scores against the
benchmark's expected behaviour.

Usage:
    python -m ai_backend.eval.runner                        # run all benchmarks
    python -m ai_backend.eval.runner --filter vehicle       # run subset by name substring
    python -m ai_backend.eval.runner --output report.md     # write markdown report
    python -m ai_backend.eval.runner --skip-llm             # static checks only (offline)

Cost note: each non-skipped benchmark = one Anthropic API call. The full
suite is 12 calls. Budget at Opus rates ≈ $0.50 — cheaper than discovering
the same regressions by clicking around in the panel.

The runner depends on the same env config as dev_server.py — set
ANIMORA_ENV=dev and have ANTHROPIC_API_KEY in your .env. To bill runs
to AWS instead, set ANIMORA_LLM_PROVIDER=bedrock + AWS_BEARER_TOKEN_BEDROCK
(see docs/BEDROCK.md; Opus calls run on Opus 4.6 there, so keep separate
baselines per provider).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ── Package bootstrap ──────────────────────────────────────────────────
# This script is meant to be run as `python -m ai_backend.eval.runner`
# from the repo root. When run directly (python eval/runner.py from
# inside ai-backend/), we register the parent dir as a package.
os.environ.setdefault("ANIMORA_ENV", "dev")

if "ai_backend" not in sys.modules:
    _PKG_DIR = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(
        "ai_backend", _PKG_DIR / "__init__.py",
        submodule_search_locations=[str(_PKG_DIR)],
    )
    pkg = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["ai_backend"] = pkg
    spec.loader.exec_module(pkg)  # type: ignore[union-attr]

from ai_backend.anthropic_client import AnthropicClient
from ai_backend.config import settings
from ai_backend.eval.benchmarks import BENCHMARKS, Benchmark
from ai_backend.eval.scoring import (
    aggregate_by_category,
    aggregate_cost_by_category,
    aggregate_critic_by_category,
    compare_to_baseline,
    estimate_cost_usd,
    evaluate_targets,
    format_regression_report,
    render_tool_calls_as_bpy,
    score_against_benchmark,
    total_cost_usd,
)
from ai_backend.llm_provider import LLMProvider, provider_from_env
from ai_backend.observability import configure
from ai_backend.orchestrator.critic import reconstruct_scene_graph
from ai_backend.orchestrator.events import bus
from ai_backend.orchestrator.streaming import stream_response
from ai_backend.orchestrator.tool_result_coordinator import ToolResultCoordinator
from ai_backend.quality_enforcer import validate_script


# ── Result types ───────────────────────────────────────────────────────

@dataclass
class BenchmarkResult:
    name: str
    prompt: str
    ok: bool
    # Classifier + router observations
    intent: str = ""
    persona: str = ""
    model: str = ""
    routing_reason: str = ""
    intent_ok: bool = True
    # Stream-level observations
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    truncated: bool = False
    elapsed_ms: int = 0
    # Generated script observations
    script_length: int = 0
    script_validator_ok: bool = True
    script_validator_reason: str = ""
    missing_ops: list[str] = field(default_factory=list)
    forbidden_ops_seen: list[str] = field(default_factory=list)
    has_name_assignment: bool = False
    has_material: bool = False
    over_token_budget: bool = False
    # Free-form notes from scoring
    notes: list[str] = field(default_factory=list)
    # The raw script in case we want to inspect later
    script_excerpt: str = ""
    # Stage 3B — deterministic critic score on the reconstructed scene
    # (0–1; -1.0 = not computed / escape-hatch build). Lets the eval
    # report structural quality alongside the regex pass/fail.
    critic_score: float = -1.0
    critic_passed: bool = False
    critic_errors: list[str] = field(default_factory=list)
    # Stage 7 — was the FIRST executed step a sound foundation? (sane
    # scale, valid parent). The brief: "the first action must establish
    # the correct foundation." None = no execution step / not applicable.
    first_step_ok: bool | None = None
    # Stage 8 — estimated USD cost of this benchmark from model + tokens
    # (list prices, cold cache → conservative upper bound). The secondary
    # axis: optimised only after quality, never at its expense.
    cost_usd: float = 0.0

    def score_summary(self) -> str:
        if self.ok:
            return "PASS"
        return "FAIL"


# ── Scoring shim ───────────────────────────────────────────────────────
# The scoring logic itself lives in scoring.py so the same pure functions
# are reachable from CI workflows and Phase 5.5's retry tests. _apply_verdict
# is a thin adapter that copies a ScoreVerdict's fields into a BenchmarkResult.

def _apply_verdict(bench: Benchmark, result: BenchmarkResult, script: str) -> None:
    verdict = score_against_benchmark(
        bench,
        script,
        output_tokens=result.output_tokens,
        truncated=result.truncated,
        script_validator_ok=result.script_validator_ok,
        script_validator_reason=result.script_validator_reason,
    )
    result.ok = verdict.ok
    result.missing_ops = verdict.missing_ops
    result.forbidden_ops_seen = verdict.forbidden_ops_seen
    result.has_name_assignment = verdict.has_name_assignment
    result.has_material = verdict.has_material
    result.over_token_budget = verdict.over_token_budget
    result.notes = verdict.notes


# ── The actual runner ──────────────────────────────────────────────────

# ── Phase B — real token usage (finding C fix) ──────────────────────────
# stream_response emits an `llm.stream_completed` bus event per iteration
# carrying the SDK's real input/output token counts + stop_reason. We
# subscribe once and accumulate per session_id, so the eval reports REAL
# tokens (and real truncation) instead of the old char-count estimate —
# no signature change to stream_response, which keeps production untouched.
_TOKEN_TOTALS: dict[str, dict[str, Any]] = {}
_usage_listener_registered = False


async def _accumulate_usage(payload: dict[str, Any]) -> None:
    sid = payload.get("session_id", "")
    if not sid:
        return
    acc = _TOKEN_TOTALS.setdefault(
        sid, {"input": 0, "output": 0, "truncated": False, "model": ""})
    acc["input"] += int(payload.get("input_tokens", 0) or 0)
    acc["output"] += int(payload.get("output_tokens", 0) or 0)
    if payload.get("stop_reason") == "max_tokens":
        acc["truncated"] = True
    # Capture the routed model so cost uses the right tier (Opus vs Sonnet)
    # rather than the default fallback.
    if payload.get("model"):
        acc["model"] = payload["model"]


def _ensure_usage_listener() -> None:
    global _usage_listener_registered
    if not _usage_listener_registered:
        bus.on("llm.stream_completed", _accumulate_usage)
        _usage_listener_registered = True


class _HeadlessExecutor(ToolResultCoordinator):
    """Eval-only stand-in for the Blender addon.

    In production the addon runs each tool_use in Blender and POSTs a
    tool_result back; main.py feeds it to `coordinator.resolve()`, and the
    agentic loop awaits those before continuing. The eval has no Blender,
    so without a coordinator `stream_response` takes its single-shot path
    and EXITS after iteration 0 — which is exactly why the harness only
    ever saw one slice of a build and complex multi-part scenes looked
    empty (the keystone limitation this fixes).

    This synthesizes a SUCCESS tool_result for every emitted tool_use so
    the loop runs the full multi-iteration build. Read-only inspections
    (get_scene_info / get_object_info) return a scene reconstructed from
    the calls so far, giving the model coherent continuity across
    iterations; viewport_screenshot returns a headless marker (offline
    eval has no pixels — vision quality is still only measurable live).
    """

    def __init__(self, registry: dict[str, dict[str, Any]],
                 captured_calls: list[dict[str, Any]],
                 session_id: str = "eval") -> None:
        super().__init__(session_id)
        self._registry = registry        # tool_use_id → {name, input}
        self._captured = captured_calls   # grows as _on_tool_call fires

    async def await_results(self, tool_use_ids, *, timeout_sec=180.0,
                            cancel_event=None):
        # Resolve every still-pending id with a synthetic success BEFORE
        # delegating, so the parent's gather returns immediately (there is
        # no real addon to wait on). Already-resolved ids (e.g. rejected by
        # the validator) are left untouched.
        for tid in tool_use_ids:
            fut = self._futures.get(tid)
            if fut is not None and not fut.done():
                self.resolve(tid, self._synthesize(tid))
        return await super().await_results(
            tool_use_ids, timeout_sec=timeout_sec, cancel_event=cancel_event)

    def _synthesize(self, tid: str) -> dict[str, Any]:
        meta = self._registry.get(tid, {})
        name = meta.get("name", "")
        inp = meta.get("input") or {}
        if name in ("get_scene_info", "get_object_info"):
            scene = reconstruct_scene_graph(self._captured)
            return {"tool_use_id": tid, "is_error": False,
                    "output": json.dumps(scene)[:4000], "error": ""}
        if name == "viewport_screenshot":
            return {"tool_use_id": tid, "is_error": False, "error": "",
                    "output": "[headless eval — screenshot taken; pixels "
                              "unavailable offline, continue building]"}
        # Mutations + everything else: report success with a short echo so
        # the model knows the step landed and advances to the next part.
        label = inp.get("name") or inp.get("kind") or name
        return {"tool_use_id": tid, "is_error": False, "error": "",
                "output": f"{name} succeeded ({label})."}


async def _run_one(client: AnthropicClient, bench: Benchmark) -> BenchmarkResult:
    """Execute one benchmark and return its scored result."""
    started = time.monotonic()

    result = BenchmarkResult(name=bench.name, prompt=bench.prompt, ok=False)

    captured_script: list[str] = []
    captured_tool_calls: list[dict[str, Any]] = []
    tool_registry: dict[str, dict[str, Any]] = {}  # tool_use_id → {name, input}

    async def _on_token(_tok: str) -> None:
        pass  # don't print streaming — too noisy for harness output

    async def _on_tool_call(name: str, _id: str, inp: dict[str, Any],
                            **_kwargs: Any) -> None:
        # stream_response passes extra kwargs (iteration, user_intent) the
        # live WS path uses; the harness ignores them. **_kwargs keeps this
        # callback forward-compatible so a new orchestrator arg can't crash
        # the eval (the TypeError this replaced).
        captured_tool_calls.append({"name": name, "input": inp})
        tool_registry[_id] = {"name": name, "input": inp}  # for the executor
        # Both the renamed escape hatch and any legacy execute_blender_script
        # carry the bpy body in `script`. Capture either for scoring.
        if name in ("execute_animora_code", "execute_blender_script"):
            captured_script.append(str(inp.get("script", "")))

    # We need a unique session_id per call so observability events stay
    # separate across runs.
    session_id = f"eval-{bench.name}-{int(time.time())}"
    _ensure_usage_listener()
    _TOKEN_TOTALS.pop(session_id, None)  # clean slate for this benchmark

    # Keystone: a headless executor stands in for the Blender addon so the
    # agentic loop runs MULTI-iteration (without it, stream_response exits
    # after iteration 0 and complex builds can't be measured at all).
    executor = _HeadlessExecutor(tool_registry, captured_tool_calls, session_id)

    try:
        output = await stream_response(
            user_message=bench.prompt,
            conversation_history=[],
            scene_context_str="",  # legacy slot, ignored
            plan="trial",  # eval runs in dev mode; trial path = same as paid now
            scene_graph={},
            send_token_cb=_on_token,
            send_tool_call_cb=_on_tool_call,
            anthropic_client=client,
            prev_scene_graph=None,
            hd_capture=None,
            session_id=session_id,
            coordinator=executor,
        )
    except Exception as exc:
        result.notes.append(f"orchestrator raised {type(exc).__name__}: {exc}")
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    result.elapsed_ms = int((time.monotonic() - started) * 1000)

    if os.getenv("ANIMORA_EVAL_DEBUG"):
        from collections import Counter
        _names = Counter(tc.get("name") for tc in captured_tool_calls)
        _scene = reconstruct_scene_graph(captured_tool_calls)
        print(f"[debug] {bench.name}: {len(captured_tool_calls)} tool calls "
              f"{dict(_names)} | reconstructed objects="
              f"{len(_scene.get('objects', []))}", file=sys.stderr)

    # Phase B — REAL token usage + truncation from the bus events that
    # fired during this benchmark's stream (finding C fix). Replaces the
    # old char-count estimate; feeds estimate_cost_usd a true number.
    usage = _TOKEN_TOTALS.pop(
        session_id, {"input": 0, "output": 0, "truncated": False, "model": ""})
    result.input_tokens = int(usage["input"])
    result.output_tokens = int(usage["output"])
    result.truncated = bool(usage["truncated"])  # real stop_reason == max_tokens
    result.model = usage.get("model", "") or result.model

    if captured_script:
        script = captured_script[-1]
        result.script_length = len(script)
        result.script_excerpt = script[:400]
        v = validate_script(script)
        result.script_validator_ok = v.ok
        result.script_validator_reason = v.reason
    else:
        # No bpy script captured — atomic build or pure-question turn.
        result.script_length = 0
        result.script_validator_ok = True  # nothing to validate

    # Build a unified scoring text: the real bpy script (escape-hatch
    # builds) PLUS a bpy-equivalent rendering of every atomic tool call.
    # render_tool_calls_as_bpy is the MCP-pivot bridge — it lets the
    # benchmark regexes + structural counters (all written for the legacy
    # bpy-script era) score an atomic build correctly. Without it, every
    # atomic build false-failed on "missing op primitive_cube_add(" etc.
    real_script = captured_script[-1] if captured_script else ""
    scoring_text = (real_script + "\n"
                    + render_tool_calls_as_bpy(captured_tool_calls)).strip()

    _apply_verdict(bench, result, scoring_text)

    # Stage 3B — score the reconstructed scene with the deterministic
    # critic. Gives the eval a structural-quality signal (materials,
    # part count, placement) independent of the regex pass/fail. The
    # benchmark's own aesthetic floors inform the critic params.
    try:
        # Absolute import: the runner is bootstrapped as a top-level
        # 'ai_backend' package (see header), so a relative '..orchestrator'
        # import has no parent package and raises ImportError. This is the
        # path that silently zeroed every critic_score in a direct run.
        from ai_backend.orchestrator.critic import first_step_ok, score_tool_calls
        critic_report = score_tool_calls(
            captured_tool_calls,
            require_materials=bool(getattr(bench, "require_material", False)),
            require_light=(getattr(bench, "min_light_sources", 0) > 0),
            expected_min_objects=max(1, getattr(bench, "min_distinct_objects", 0)),
        )
        result.critic_score = critic_report.score
        result.critic_passed = critic_report.passed
        result.critic_errors = [f.check_id for f in critic_report.errors]
        # Stage 7 — first-step soundness.
        result.first_step_ok = first_step_ok(captured_tool_calls)
    except Exception as exc:  # critic is advisory — never fail the run
        result.notes.append(f"critic_score_failed: {exc}")

    # Stage 8 — estimate cost from the recorded model + token usage.
    result.cost_usd = estimate_cost_usd(
        result.model, result.input_tokens, result.output_tokens)

    return result


def _format_report(results: list[BenchmarkResult]) -> str:
    """Return a Markdown summary suitable for piping into a file."""
    n_pass = sum(1 for r in results if r.ok)
    n_total = len(results)
    dict_results = [asdict(r) for r in results]

    # Stage 7 — overall quality rollups.
    scored = [r for r in results if r.critic_score >= 0]
    overall_critic = (round(sum(r.critic_score for r in scored) / len(scored), 3)
                      if scored else None)
    fs_judged = [r for r in results if r.first_step_ok is not None]
    first_step_acc = (sum(1 for r in fs_judged if r.first_step_ok) / len(fs_judged)
                      if fs_judged else None)
    crit_by_cat = aggregate_critic_by_category(dict_results)
    comp_score = crit_by_cat.get("composition")
    # Stage 8 — cost rollups.
    cost_by_cat = aggregate_cost_by_category(dict_results)
    run_cost = total_cost_usd(dict_results)
    mean_cost = round(run_cost / n_total, 6) if n_total else 0.0
    # Quality-per-dollar (efficiency): mean critic score earned per dollar
    # of run spend. Higher = more quality for the money. Only meaningful
    # when we have both a critic score and a non-zero cost.
    qpd = (round(overall_critic / run_cost, 1)
           if overall_critic is not None and run_cost > 0 else None)

    lines = [
        "# Animora eval scorecard",
        "",
        f"**Result: {n_pass}/{n_total} passed**",
    ]
    if overall_critic is not None:
        lines.append(f"**Mean critic score: {overall_critic:.2f}**")
    if first_step_acc is not None:
        lines.append(f"**First-step accuracy: {first_step_acc:.0%}** "
                     f"({sum(1 for r in fs_judged if r.first_step_ok)}/{len(fs_judged)})")
    if comp_score is not None:
        lines.append(f"**Composition critic score: {comp_score:.2f}** "
                     f"(the MCP's known weak spot)")
    lines.append(f"**Run cost: ${run_cost:.4f}** (mean ${mean_cost:.4f}/benchmark, "
                 f"list prices, cold cache)")
    if qpd is not None:
        lines.append(f"**Quality per dollar: {qpd:.1f}** (mean critic ÷ run cost)")
    lines.append("")

    # Per-category aggregate — pass rate + mean critic + target + mean cost.
    cat_scores = aggregate_by_category(dict_results)
    targets = evaluate_targets(dict_results)
    if cat_scores:
        lines.append("## By category")
        lines.append("")
        lines.append("| category | pass rate | mean critic | target | mean cost | passed / total |")
        lines.append("|---|---|---|---|---|---|")
        for cat in sorted(cat_scores):
            s = cat_scores[cat]
            mc = crit_by_cat.get(cat)
            mc_str = f"{mc:.2f}" if mc is not None else "—"
            tgt = targets.get(cat)
            tgt_str = "—"
            if tgt is not None:
                tgt_str = "MET" if tgt.met else f"BELOW ({tgt.reason})"
            cc = cost_by_cat.get(cat)
            cc_str = f"${cc:.4f}" if cc is not None else "—"
            lines.append(
                f"| {cat} | {s.pass_rate:.0%} | {mc_str} | {tgt_str} | {cc_str} | "
                f"{s.passed} / {s.total} |")
        lines.append("")

    lines.append("## All benchmarks")
    lines.append("")
    lines.append("| benchmark | result | critic | first step | output toks | cost | issues |")
    lines.append("|---|---|---|---|---|---|---|")
    for r in results:
        issues = "; ".join(r.notes) if r.notes else "—"
        crit = f"{r.critic_score:.2f}" if r.critic_score >= 0 else "—"
        fs = "—" if r.first_step_ok is None else ("ok" if r.first_step_ok else "BAD")
        lines.append(
            f"| {r.name} | {r.score_summary()} | {crit} | {fs} | "
            f"{r.output_tokens} | ${r.cost_usd:.4f} | {issues} |"
        )

    lines.append("")
    lines.append("## Per-benchmark detail")
    for r in results:
        lines.append("")
        lines.append(f"### {r.name} — {r.score_summary()}")
        lines.append(f"- prompt: `{r.prompt}`")
        lines.append(f"- output_tokens (est): {r.output_tokens}")
        lines.append(f"- script_length: {r.script_length} chars")
        lines.append(f"- truncated: {r.truncated}")
        lines.append(f"- validator: {'ok' if r.script_validator_ok else r.script_validator_reason}")
        lines.append(f"- named: {r.has_name_assignment}, material: {r.has_material}")
        if r.notes:
            lines.append(f"- issues:")
            for note in r.notes:
                lines.append(f"  - {note}")
    return "\n".join(lines)


def _rescore_from_dump(dump_path: Path) -> list[BenchmarkResult]:
    """Load a previous JSON results dump and re-apply current scoring
    rules without spending API credits. Used by --skip-llm to validate
    scoring.py changes against captured runs."""
    data = json.loads(dump_path.read_text(encoding="utf-8"))
    bench_by_name = {b.name: b for b in BENCHMARKS}

    results: list[BenchmarkResult] = []
    for row in data:
        bench = bench_by_name.get(row["name"])
        if bench is None:
            continue  # benchmark was removed; drop from rescored output
        result = BenchmarkResult(
            name=row["name"], prompt=row["prompt"], ok=False,
            intent=row.get("intent", ""), persona=row.get("persona", ""),
            model=row.get("model", ""), routing_reason=row.get("routing_reason", ""),
            intent_ok=row.get("intent_ok", True),
            input_tokens=row.get("input_tokens", 0),
            output_tokens=row.get("output_tokens", 0),
            stop_reason=row.get("stop_reason", ""),
            truncated=row.get("truncated", False),
            elapsed_ms=row.get("elapsed_ms", 0),
            script_length=row.get("script_length", 0),
            script_validator_ok=row.get("script_validator_ok", True),
            script_validator_reason=row.get("script_validator_reason", ""),
            script_excerpt=row.get("script_excerpt", ""),
        )
        _apply_verdict(bench, result, row.get("script_excerpt", ""))
        results.append(result)
    return results


async def _main(args: argparse.Namespace) -> int:
    configure()

    # --skip-llm: re-score a previously captured run without spending
    # API credits. Useful for validating scoring.py changes in CI and
    # for unit-testing the regression detector.
    if args.skip_llm:
        if not args.input_json:
            print("--skip-llm requires --input-json <path-to-prior-dump>", file=sys.stderr)
            return 2
        results = _rescore_from_dump(Path(args.input_json))
        print(f"Re-scored {len(results)} benchmark(s) from {args.input_json}")
    else:
        # Filter benchmarks by name substring if requested
        benches = list(BENCHMARKS)
        if args.filter:
            benches = [b for b in benches if args.filter in b.name]
            if not benches:
                print(f"No benchmarks match filter {args.filter!r}", file=sys.stderr)
                return 2

        # Credential guard is provider-aware: on Bedrock the Anthropic key
        # is ignored by AnthropicClient and auth is the AWS bearer token.
        if provider_from_env() is LLMProvider.BEDROCK:
            if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
                print(
                    "ANIMORA_LLM_PROVIDER=bedrock but AWS_BEARER_TOKEN_BEDROCK "
                    "is missing — see docs/BEDROCK.md, or use --skip-llm with "
                    "--input-json to rescore a captured dump.",
                    file=sys.stderr,
                )
                return 2
            api_key = ""
        else:
            api_key = settings.anthropic_api_key
            if not api_key:
                print(
                    "ANTHROPIC_API_KEY missing — set it in .env or use --skip-llm "
                    "with --input-json to rescore a captured dump.",
                    file=sys.stderr,
                )
                return 2

        client = AnthropicClient(api_key=api_key, session_id="eval-harness")

        best_of = max(1, int(getattr(args, "best_of", 1) or 1))
        results = []
        for bench in benches:
            print(f"running {bench.name} ... ", end="", flush=True)
            if best_of == 1:
                result = await _run_one(client, bench)
            else:
                # Stage 3B — best-of-N: run the benchmark N times, keep
                # the candidate with the highest critic score (richer,
                # better-structured build). Reports best/mean/worst so
                # we can see the model's consistency on this prompt.
                candidates = []
                for _ in range(best_of):
                    candidates.append(await _run_one(client, bench))
                scores = [c.critic_score for c in candidates]
                # Pick the highest critic score; tie-break on regex pass.
                best_idx = max(
                    range(len(candidates)),
                    key=lambda i: (candidates[i].critic_score,
                                   1 if candidates[i].ok else 0, -i),
                )
                result = candidates[best_idx]
                result.notes.append(
                    f"best_of_{best_of}: best={max(scores):.2f} "
                    f"mean={sum(scores)/len(scores):.2f} worst={min(scores):.2f} "
                    f"(picked candidate {best_idx})"
                )
            results.append(result)
            cs = (f" critic={result.critic_score:.2f}"
                  if result.critic_score >= 0 else "")
            print(result.score_summary(),
                  f"({result.elapsed_ms} ms, {result.output_tokens} tok{cs})")

    report = _format_report(results)

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"\nReport written to {args.output}")
    else:
        print()
        print(report)

    if args.json:
        # Emit a JSON file with the full results — re-loadable via --skip-llm
        Path(args.json).write_text(
            json.dumps([asdict(r) for r in results], indent=2),
            encoding="utf-8",
        )
        print(f"JSON dump written to {args.json}")

    # --output-baseline: freeze current pass/fail per benchmark as the
    # baseline future CI runs are checked against. Distinct from --json
    # in that we drop debug fields (token counts, ms timing) that would
    # cause spurious diffs in version control.
    if args.output_baseline:
        baseline = [
            {
                "name": r.name,
                "ok": r.ok,
                "notes": r.notes,
                # Stage 7 — freeze the quality signals so the gate can
                # detect critic-score + first-step regressions, not just
                # pass/fail flips. -1.0 / null where not computed.
                "critic_score": r.critic_score,
                "first_step_ok": r.first_step_ok,
                # Stage 8 — freeze the estimated cost so the gate can flag
                # quality-neutral cost increases (waste). Raw token counts
                # stay out of the baseline (they churn every run); the
                # derived cost is the comparable signal.
                "cost_usd": r.cost_usd,
            }
            for r in results
        ]
        Path(args.output_baseline).write_text(
            json.dumps(baseline, indent=2),
            encoding="utf-8",
        )
        print(f"Baseline written to {args.output_baseline}")

    # --baseline + optional --fail-on-regress: compare against a frozen
    # baseline and surface regressions. This is the CI gate.
    if args.baseline:
        baseline_data = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        new_data = [asdict(r) for r in results]
        regression = compare_to_baseline(new_data, baseline_data)
        print()
        print(format_regression_report(regression))
        if args.fail_on_regress and regression.has_regression:
            print("\n❌ Regressions detected — failing the gate.", file=sys.stderr)
            return 3

    n_pass = sum(1 for r in results if r.ok)
    # Exit 0 if all benchmarks pass (or if we have a baseline and no
    # regression — handled above). Exit 1 if some fail but no baseline.
    if args.baseline:
        return 0  # baseline handled the gate above
    return 0 if n_pass == len(results) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Animora eval harness.")
    parser.add_argument("--filter", help="Run only benchmarks whose name contains this substring")
    parser.add_argument("--output", help="Path to write the markdown report")
    parser.add_argument("--json", help="Path to write the full JSON results dump (reloadable via --skip-llm)")
    parser.add_argument("--output-baseline", help="Freeze current pass/fail as the regression baseline at this path")
    parser.add_argument("--baseline", help="Compare run results against this saved baseline.json")
    parser.add_argument(
        "--fail-on-regress", action="store_true",
        help="Exit non-zero if --baseline comparison finds new failures or category drops (>=10pp)",
    )
    parser.add_argument(
        "--skip-llm", action="store_true",
        help="Don't call the API; re-score the dump from --input-json",
    )
    parser.add_argument("--input-json", help="Prior JSON dump to rescore (used with --skip-llm)")
    parser.add_argument(
        "--best-of", type=int, default=1,
        help="Stage 3B: run each benchmark N times and keep the highest "
             "critic-scoring build. Reports best/mean/worst per benchmark.",
    )
    args = parser.parse_args()

    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
