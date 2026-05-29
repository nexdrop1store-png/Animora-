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
ANIMORA_ENV=dev and have ANTHROPIC_API_KEY in your .env.
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
    compare_to_baseline,
    format_regression_report,
    score_against_benchmark,
)
from ai_backend.observability import configure
from ai_backend.orchestrator.streaming import stream_response
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

async def _run_one(client: AnthropicClient, bench: Benchmark) -> BenchmarkResult:
    """Execute one benchmark and return its scored result."""
    started = time.monotonic()

    result = BenchmarkResult(name=bench.name, prompt=bench.prompt, ok=False)

    captured_script: list[str] = []
    captured_tool_calls: list[dict[str, Any]] = []

    async def _on_token(_tok: str) -> None:
        pass  # don't print streaming — too noisy for harness output

    async def _on_tool_call(name: str, _id: str, inp: dict[str, Any]) -> None:
        captured_tool_calls.append({"name": name, "input": inp})
        # Both the renamed escape hatch and any legacy execute_blender_script
        # carry the bpy body in `script`. Capture either for scoring.
        if name in ("execute_animora_code", "execute_blender_script"):
            captured_script.append(str(inp.get("script", "")))

    # We need a unique session_id per call so observability events stay
    # separate across runs.
    session_id = f"eval-{bench.name}-{int(time.time())}"

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
        )
    except Exception as exc:
        result.notes.append(f"orchestrator raised {type(exc).__name__}: {exc}")
        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        return result

    result.elapsed_ms = int((time.monotonic() - started) * 1000)

    # Pull observations from the orchestrator's bus events? Easier to just
    # introspect the last stream result via a private attribute — but the
    # orchestrator doesn't return it. So we extract what we can from the
    # captured callbacks + a follow-up direct call would be wasteful. The
    # streaming function already logged the relevant numbers; for the
    # harness we re-derive what we can from the captured script.

    if captured_script:
        script = captured_script[-1]
        result.script_length = len(script)
        result.script_excerpt = script[:400]
        v = validate_script(script)
        result.script_validator_ok = v.ok
        result.script_validator_reason = v.reason

        # Rough output_tokens estimate from script length (≈ 3.5 chars/tok).
        # We don't have direct access to the SDK's usage object here without
        # plumbing it through stream_response. For now, char-based heuristic.
        result.output_tokens = len(script) // 3 + len(output) // 3
    else:
        # No tool_call captured — either it's a pure question (intentional)
        # or the model never produced a script (regression).
        result.script_length = 0
        result.script_validator_ok = True  # nothing to validate
        result.output_tokens = len(output) // 3

    # Truncation heuristic — if the captured assistant text or script ends
    # mid-line / mid-string-literal, it's probably truncated. Loose check
    # for the eval; stop_reason would be more precise but that requires
    # plumbing the StreamResult through stream_response.
    if result.output_tokens >= 32000:
        result.truncated = True

    # Build a unified scoring text: the bpy script (if any) plus a
    # textual representation of every non-script tool call. This lets
    # benchmark.required_ops regexes match patterns like `use_asset(`
    # or `request_final_review(` even when the model never emitted a
    # bpy script. Without this, asset-first benchmarks (Sprint 3D)
    # would pass vacuously when the model called use_asset directly.
    scoring_text_parts: list[str] = []
    if captured_script:
        scoring_text_parts.append(captured_script[-1])
    for tc in captured_tool_calls:
        name = tc.get("name", "")
        if name in ("execute_animora_code", "execute_blender_script"):
            continue  # already captured above
        # Render as `<tool_name>(key1="val1", key2="val2")` so regexes
        # matching the call shape work uniformly.
        inp = tc.get("input", {}) or {}
        kv = ", ".join(
            f'{k}="{str(v)[:80]}"' for k, v in inp.items() if isinstance(k, str)
        )
        scoring_text_parts.append(f"# tool_use: {name}({kv})")
    scoring_text = "\n".join(scoring_text_parts)

    _apply_verdict(bench, result, scoring_text)
    return result


def _format_report(results: list[BenchmarkResult]) -> str:
    """Return a Markdown summary suitable for piping into a file."""
    n_pass = sum(1 for r in results if r.ok)
    n_total = len(results)
    lines = [
        "# Animora eval scorecard",
        "",
        f"**Result: {n_pass}/{n_total} passed**",
        "",
    ]

    # Per-category aggregate — first thing readers want to see. Categories
    # are derived from the benchmark name prefix (primitive.*, vehicle.*).
    cat_scores = aggregate_by_category([asdict(r) for r in results])
    if cat_scores:
        lines.append("## By category")
        lines.append("")
        lines.append("| category | pass rate | passed / total |")
        lines.append("|---|---|---|")
        for cat in sorted(cat_scores):
            s = cat_scores[cat]
            lines.append(f"| {cat} | {s.pass_rate:.0%} | {s.passed} / {s.total} |")
        lines.append("")

    lines.append("## All benchmarks")
    lines.append("")
    lines.append("| benchmark | result | output toks | script len | issues |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        issues = "; ".join(r.notes) if r.notes else "—"
        lines.append(
            f"| {r.name} | {r.score_summary()} | {r.output_tokens} | "
            f"{r.script_length} | {issues} |"
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

        api_key = settings.anthropic_api_key
        if not api_key:
            print(
                "ANTHROPIC_API_KEY missing — set it in .env or use --skip-llm "
                "with --input-json to rescore a captured dump.",
                file=sys.stderr,
            )
            return 2

        client = AnthropicClient(api_key=api_key, session_id="eval-harness")

        results = []
        for bench in benches:
            print(f"running {bench.name} ... ", end="", flush=True)
            result = await _run_one(client, bench)
            results.append(result)
            print(result.score_summary(), f"({result.elapsed_ms} ms, {result.output_tokens} tok)")

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
            {"name": r.name, "ok": r.ok, "notes": r.notes}
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
    args = parser.parse_args()

    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
