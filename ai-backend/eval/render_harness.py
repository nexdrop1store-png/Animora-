"""Phase 0 render-based eval harness — AI quality workstream.

Sibling to eval/runner.py, NOT a replacement: runner.py answers "did the
emitted script/tool-calls contain the right regex patterns" (cheap, fast,
CI-gated). This answers "does the FINAL BUILT SCENE actually look right",
by rendering what the agent built with a real headless Blender process and
scoring the render with a real vision call — the grounding rule the whole
AI-quality workstream runs on ("critique must be anchored to a real
artifact... renders and execution results are the only truth").

Reuses runner.py's exact bootstrap + `_HeadlessExecutor` (so the SAME real
multi-iteration stream_response loop runs — SPEC step, rescue gates, critic
corrections, the inline Phase 5.5 retry path if a real HD capture existed)
and only adds what was missing: turning the captured tool calls into a real
render (atomic_to_bpy.py + render_worker.py) and a genuine 1-10 vision
judgment against the goal prompt.

Usage:
    python -m ai_backend.eval.render_harness --tasks-file seed_tasks.json
    python -m ai_backend.eval.render_harness --tasks-file seed_tasks.json --output baseline_report.md --json baseline_run.json

Requires the SAME env as runner.py (ANIMORA_ENV=dev + ANTHROPIC_API_KEY, or
ANIMORA_LLM_PROVIDER=bedrock + AWS_BEARER_TOKEN_BEDROCK) PLUS an installed,
launchable Animora/Blender executable (--animora-exe, or set
ANIMORA_EVAL_BLENDER_EXE) for the render step.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

os.environ.setdefault("ANIMORA_ENV", "dev")

if "ai_backend" not in sys.modules:
    import importlib.util
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
from ai_backend.eval.atomic_to_bpy import tool_calls_to_bpy_script
from ai_backend.eval.benchmarks import Benchmark
from ai_backend.eval.scoring import estimate_cost_usd
from ai_backend.orchestrator.aspect_verifiers import run_all_aspect_verifiers
from ai_backend.orchestrator.image_media import sniff_image_media_type
from ai_backend.llm_provider import LLMProvider, provider_from_env
from ai_backend.observability import configure
from ai_backend.orchestrator.events import bus
from ai_backend.orchestrator.streaming import stream_response
# Reuse runner.py's headless-executor + usage-accumulation exactly — same
# synthesis of tool_results (the mechanism that lets the loop run multiple
# iterations at all), no need to re-derive it.
from ai_backend.eval.runner import _HeadlessExecutor, _ensure_usage_listener, _TOKEN_TOTALS


_WORKER_SCRIPT = Path(__file__).resolve().parent / "render_worker.py"
_DEFAULT_ANIMORA_EXE = (
    os.environ.get("ANIMORA_EVAL_BLENDER_EXE")
    or r"C:\Users\Administrator\AppData\Local\Programs\Animora\Animora.exe"
)
# Same Mesa workaround env vars this whole session's manual testing needed —
# render_worker.py uses Cycles/CPU so these mostly matter for bpy/window
# init, not the render itself, but cheap to carry through regardless.
_RENDER_SUBPROCESS_ENV_EXTRA = {
    "GALLIUM_DRIVER": "llvmpipe",
    "GALLIUM_OVERRIDE_CPU_CAPS": "sse2",
    "LP_NUM_THREADS": "0",
}
_RENDER_TIMEOUT_SEC = 120.0
_VLM_SCORE_MODEL = "claude-sonnet-4-6"
_VLM_MAX_TOKENS = 500


@dataclass
class RenderTaskResult:
    name: str
    category: str
    prompt: str
    ok: bool = False
    # Orchestrator observations
    tool_call_count: int = 0
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    orchestrator_elapsed_ms: int = 0
    orchestrator_error: str = ""
    # Render observations
    render_ok: bool = False
    render_paths: list[str] = field(default_factory=list)
    render_errors: list[str] = field(default_factory=list)
    render_elapsed_ms: int = 0
    # VLM judgment (Phase 0's "automatic" scoring leg — one holistic call)
    vlm_score: int = 0  # 1-10, 0 = not scored (no renders / vlm call failed)
    vlm_matches_brief: bool = False
    vlm_notes: str = ""
    vlm_error: str = ""
    # Aspect-verifier judgment (Phase 1 — 8 independent single-aspect calls)
    aspect_overall: str = ""  # "pass" | "fail" | "" (not run / total failure)
    aspect_results: list[dict[str, Any]] = field(default_factory=list)
    aspect_cost_usd: float = 0.0
    # Cost (orchestrator tokens + VLM scoring calls)
    cost_usd: float = 0.0
    vlm_cost_usd: float = 0.0
    # Debug/audit trail — what the agent actually called, so a surprising
    # render can be traced to "agent chose bad values" vs "the translator
    # mistranslated a correct call". Not scored on; purely diagnostic.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_cost_usd(self) -> float:
        return round(self.cost_usd + self.vlm_cost_usd + self.aspect_cost_usd, 6)


def _load_seed_tasks(path: Path) -> list[Benchmark]:
    """Seed tasks are plain JSON: [{"name","category","prompt"}, ...] —
    deliberately NOT reusing the full Benchmark dataclass's regex-scoring
    fields (required_ops etc.) since this harness doesn't score those; it
    only needs name/prompt. Wrapped in Benchmark for compatibility with the
    imported executor/bus-listener plumbing, which key on bench.name."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    tasks = []
    for row in raw:
        b = Benchmark(name=row["name"], prompt=row["prompt"], required_named=False)
        b.notes = row.get("category", "")  # stash category cheaply; read back via .notes
        tasks.append(b)
    return tasks


async def _run_orchestrator(client: AnthropicClient, bench: Benchmark, result: RenderTaskResult
                             ) -> tuple[list[dict[str, Any]], str]:
    """Run the real agentic loop for one task. Returns (captured_tool_calls,
    captured_real_script) — empty on any orchestrator-level failure (result
    is annotated in place; caller decides whether to still attempt a render
    of whatever was captured before the failure)."""
    captured_tool_calls: list[dict[str, Any]] = []
    captured_script: list[str] = []

    async def _on_token(_tok: str) -> None:
        pass

    async def _on_tool_call(name: str, _id: str, inp: dict[str, Any], **_kwargs: Any) -> None:
        captured_tool_calls.append({"name": name, "input": inp})
        if name in ("execute_animora_code", "execute_blender_script"):
            captured_script.append(str(inp.get("script", "")))

    session_id = f"render-eval-{bench.name}-{int(time.time())}"
    _ensure_usage_listener()
    _TOKEN_TOTALS.pop(session_id, None)

    tool_registry: dict[str, dict[str, Any]] = {}

    async def _on_tool_call_with_registry(name: str, _id: str, inp: dict[str, Any], **kw: Any) -> None:
        tool_registry[_id] = {"name": name, "input": inp}
        await _on_tool_call(name, _id, inp, **kw)

    executor = _HeadlessExecutor(tool_registry, captured_tool_calls, session_id)

    started = time.monotonic()
    try:
        await stream_response(
            user_message=bench.prompt,
            conversation_history=[],
            scene_context_str="",
            plan="trial",
            scene_graph={},
            send_token_cb=_on_token,
            send_tool_call_cb=_on_tool_call_with_registry,
            anthropic_client=client,
            prev_scene_graph=None,
            hd_capture=None,
            session_id=session_id,
            coordinator=executor,
        )
    except Exception as exc:
        result.orchestrator_error = f"{type(exc).__name__}: {exc}"

    result.orchestrator_elapsed_ms = int((time.monotonic() - started) * 1000)
    result.tool_call_count = len(captured_tool_calls)

    usage = _TOKEN_TOTALS.pop(session_id, {"input": 0, "output": 0, "model": ""})
    result.input_tokens = int(usage.get("input", 0))
    result.output_tokens = int(usage.get("output", 0))
    result.model = usage.get("model", "") or ""
    result.cost_usd = estimate_cost_usd(result.model, result.input_tokens, result.output_tokens)

    real_script = captured_script[-1] if captured_script else ""
    return captured_tool_calls, real_script


def _render_task(task_name: str, tool_calls: list[dict[str, Any]], real_script: str,
                  output_dir: Path, animora_exe: str, result: RenderTaskResult) -> None:
    """Translate + render via a real headless Animora/Blender subprocess.
    Populates result.render_* fields in place."""
    script_text = tool_calls_to_bpy_script(tool_calls, real_script)
    if not script_text.strip():
        result.render_errors.append("nothing to render — no mutating tool calls or script captured")
        return

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", prefix=f"eval_{task_name}_", delete=False, encoding="utf-8",
    ) as f:
        f.write(script_text)
        script_path = f.name

    started = time.monotonic()
    env = {**os.environ, **_RENDER_SUBPROCESS_ENV_EXTRA}
    try:
        proc = subprocess.run(
            [animora_exe, "--background", "--python", str(_WORKER_SCRIPT),
             "--", script_path, str(output_dir), task_name],
            capture_output=True, text=True, timeout=_RENDER_TIMEOUT_SEC, env=env,
        )
    except subprocess.TimeoutExpired:
        result.render_errors.append(f"render subprocess timed out after {_RENDER_TIMEOUT_SEC}s")
        return
    finally:
        result.render_elapsed_ms = int((time.monotonic() - started) * 1000)
        with __import__("contextlib").suppress(Exception):
            Path(script_path).unlink()

    # The worker prints exactly one JSON line as its LAST stdout line;
    # everything above it is bpy/addon log noise (font warnings, the known
    # SpaceAnimora registration messages, etc.) — scan from the end.
    parsed = None
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if parsed is None:
        result.render_errors.append(
            f"worker produced no parseable result (exit={proc.returncode}); "
            f"stderr tail: {proc.stderr[-500:]}"
        )
        return

    result.render_ok = bool(parsed.get("ok", False))
    result.render_paths = list(parsed.get("renders", []))
    result.render_errors.extend(parsed.get("errors", []))


async def _score_with_vlm(client: AnthropicClient, prompt: str, render_paths: list[str],
                          result: RenderTaskResult) -> None:
    if not render_paths:
        result.vlm_error = "no renders to score"
        return

    content: list[dict[str, Any]] = []
    for p in render_paths:
        try:
            data = Path(p).read_bytes()
        except OSError as exc:
            result.vlm_error = f"could not read render {p}: {exc}"
            continue
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": sniff_image_media_type(data),
                "data": base64.b64encode(data).decode("ascii"),
            },
        })
    if not content:
        return

    scoring_prompt = (
        "You are a senior 3D art director evaluating a build against a brief.\n\n"
        f"BRIEF: {prompt}\n\n"
        f"You are shown {len(content)} render(s) of the final result from different "
        "viewpoints (the object may be partially cut off at frame edges — that's a "
        "framing artifact of the eval harness, not the build itself; judge the content, "
        "not the crop).\n\n"
        "Rate how well the result satisfies the brief and reads as competent 3D work, "
        "1-10 (10 = professional, matches the brief exactly, well-composed; "
        "1 = broken, missing, unrecognizable, or contradicts the brief).\n\n"
        "Output EXACTLY one JSON object and nothing else:\n"
        '{"score": <1-10 integer>, "matches_brief": <bool>, "notes": "<1-2 sentences>"}'
    )
    content.append({"type": "text", "text": scoring_prompt})

    try:
        response = await asyncio.wait_for(
            client.messages_create(
                model=_VLM_SCORE_MODEL,
                max_tokens=_VLM_MAX_TOKENS,
                system="You are a strict JSON-only senior art-director. Output exactly one JSON object and nothing else.",
                messages=[{"role": "user", "content": content}],
            ),
            timeout=60.0,
        )
    except Exception as exc:
        result.vlm_error = f"{type(exc).__name__}: {exc}"
        return

    text = "".join(
        getattr(block, "text", "") for block in response.content if getattr(block, "type", "") == "text"
    )
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        parsed = json.loads(text[start:end])
        result.vlm_score = int(parsed.get("score", 0))
        result.vlm_matches_brief = bool(parsed.get("matches_brief", False))
        result.vlm_notes = str(parsed.get("notes", ""))
    except (ValueError, json.JSONDecodeError) as exc:
        result.vlm_error = f"unparseable VLM response: {exc} — raw: {text[:200]}"

    usage = getattr(response, "usage", None)
    if usage is not None:
        result.vlm_cost_usd = estimate_cost_usd(
            _VLM_SCORE_MODEL, getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0),
        )


async def _score_with_aspect_verifiers(client: AnthropicClient, prompt: str, render_paths: list[str],
                                        result: RenderTaskResult) -> None:
    """Phase 1 scoring leg — the 8 independent single-aspect verifiers,
    given ALL render viewpoints per call (multi-view, per the roadmap
    note), run alongside the existing holistic _score_with_vlm so the two
    can be compared directly against the same Phase 0 renders."""
    images: list[bytes] = []
    for p in render_paths:
        try:
            images.append(Path(p).read_bytes())
        except OSError as exc:
            result.render_errors.append(f"aspect verifier could not read render {p}: {exc}")
    if not images:
        return

    results = await run_all_aspect_verifiers(
        images, client, user_intent=prompt,
        persona_display_name="Generalist (eval harness — no live persona)",
        persona_quality_checks="(none declared — scoring against the brief only)",
        scene_diff_summary="n/a (single-shot eval build, no before/after scene graph)",
        execution_outcome="OK",
    )

    result.aspect_results = [
        {"aspect": r.aspect, "label": r.label, "verdict": r.verdict, "reason": r.reason,
         "fix_suggestion": r.fix_suggestion, "confidence": r.confidence}
        for r in results
    ]
    judged = [r for r in results if r.verdict != "error"]
    result.aspect_overall = "fail" if any(r.verdict == "fail" for r in judged) else ("pass" if judged else "")
    result.aspect_cost_usd = round(sum(
        estimate_cost_usd(_VLM_SCORE_MODEL, r.input_tokens, r.output_tokens) for r in results
    ), 6)


async def _run_one(client: AnthropicClient, bench: Benchmark, output_dir: Path, animora_exe: str
                    ) -> RenderTaskResult:
    result = RenderTaskResult(name=bench.name, category=bench.notes, prompt=bench.prompt)
    print(f"  [{bench.name}] running orchestrator...", end=" ", flush=True)
    tool_calls, real_script = await _run_orchestrator(client, bench, result)
    result.tool_calls = tool_calls
    print(f"{result.tool_call_count} tool calls, {result.orchestrator_elapsed_ms}ms"
          f"{' (ERROR: ' + result.orchestrator_error + ')' if result.orchestrator_error else ''}")

    await _render_and_score(client, bench, tool_calls, real_script=real_script, output_dir=output_dir,
                             animora_exe=animora_exe, result=result)
    return result


def _resumed_result(row: dict[str, Any]) -> RenderTaskResult:
    """Rehydrate a RenderTaskResult from a prior --json dump, keeping the
    orchestrator-side fields (tool_calls, tokens, cost, model, timing) and
    resetting only the render/VLM fields so _render_and_score recomputes
    them against the current (fixed) translator."""
    result = RenderTaskResult(
        name=row["name"], category=row.get("category", ""), prompt=row.get("prompt", ""),
        tool_call_count=row.get("tool_call_count", 0), model=row.get("model", ""),
        input_tokens=row.get("input_tokens", 0), output_tokens=row.get("output_tokens", 0),
        orchestrator_elapsed_ms=row.get("orchestrator_elapsed_ms", 0),
        orchestrator_error=row.get("orchestrator_error", ""),
        cost_usd=row.get("cost_usd", 0.0),
        tool_calls=row.get("tool_calls", []),
    )
    return result


async def _render_and_score(client: AnthropicClient, bench: Benchmark,
                             tool_calls: list[dict[str, Any]], real_script: str, output_dir: Path,
                             animora_exe: str, result: RenderTaskResult) -> None:
    print(f"  [{bench.name}] rendering...", end=" ", flush=True)
    _render_task(bench.name, tool_calls, real_script, output_dir, animora_exe, result)
    print(f"ok={result.render_ok} ({result.render_elapsed_ms}ms)"
          f"{' errors=' + str(result.render_errors) if result.render_errors else ''}")

    if result.render_ok:
        print(f"  [{bench.name}] VLM scoring...", end=" ", flush=True)
        await _score_with_vlm(client, bench.prompt, result.render_paths, result)
        print(f"score={result.vlm_score}/10 matches_brief={result.vlm_matches_brief}")

        print(f"  [{bench.name}] aspect verifiers...", end=" ", flush=True)
        await _score_with_aspect_verifiers(client, bench.prompt, result.render_paths, result)
        failed = [r["label"] for r in result.aspect_results if r["verdict"] == "fail"]
        print(f"overall={result.aspect_overall or 'n/a'} failed={failed or '—'}")

    result.ok = result.render_ok and result.vlm_score >= 6 and not result.orchestrator_error


async def _run_one_resumed(client: AnthropicClient, row: dict[str, Any], output_dir: Path,
                            animora_exe: str) -> RenderTaskResult:
    result = _resumed_result(row)
    bench = Benchmark(name=result.name, prompt=result.prompt, required_named=False)
    bench.notes = result.category
    await _render_and_score(client, bench, result.tool_calls, real_script="", output_dir=output_dir,
                             animora_exe=animora_exe, result=result)
    return result


def _format_report(results: list[RenderTaskResult]) -> str:
    n = len(results)
    scored = [r for r in results if r.vlm_score > 0]
    mean_score = round(sum(r.vlm_score for r in scored) / len(scored), 2) if scored else 0.0
    total_cost = round(sum(r.total_cost_usd for r in results), 4)
    mean_cost = round(total_cost / n, 4) if n else 0.0
    error_count = sum(1 for r in results if r.orchestrator_error or r.render_errors or r.vlm_error)

    by_cat: dict[str, list[RenderTaskResult]] = {}
    for r in results:
        by_cat.setdefault(r.category or "uncategorized", []).append(r)

    lines = [
        "# Phase 0 — Baseline render eval",
        "",
        f"**Tasks: {n}** | **Mean VLM score: {mean_score}/10** | "
        f"**Total cost: ${total_cost}** (mean ${mean_cost}/task) | **Errors: {error_count}**",
        "",
        "## By category",
        "",
        "| category | tasks | mean score | mean cost |",
        "|---|---|---|---|",
    ]
    for cat, rows in sorted(by_cat.items()):
        cat_scored = [r for r in rows if r.vlm_score > 0]
        cat_mean = round(sum(r.vlm_score for r in cat_scored) / len(cat_scored), 2) if cat_scored else 0.0
        cat_cost = round(sum(r.total_cost_usd for r in rows) / len(rows), 4) if rows else 0.0
        lines.append(f"| {cat} | {len(rows)} | {cat_mean} | ${cat_cost} |")

    lines += ["", "## Per-task detail", "", "| task | score | matches | tool calls | cost | issues |", "|---|---|---|---|---|---|"]
    for r in results:
        issues = []
        if r.orchestrator_error:
            issues.append(f"orch: {r.orchestrator_error}")
        issues.extend(r.render_errors)
        if r.vlm_error:
            issues.append(f"vlm: {r.vlm_error}")
        issue_str = "; ".join(issues) if issues else "—"
        lines.append(
            f"| {r.name} | {r.vlm_score}/10 | {r.vlm_matches_brief} | {r.tool_call_count} | "
            f"${r.total_cost_usd} | {issue_str} |"
        )

    lines.append("")
    lines.append("## Notes per task")
    for r in results:
        if r.vlm_notes:
            lines.append(f"- **{r.name}**: {r.vlm_notes}")

    return "\n".join(lines)


async def _main(args: argparse.Namespace) -> int:
    configure()

    resumed_rows: list[dict[str, Any]] | None = None
    if args.resume_json:
        resumed_rows = json.loads(Path(args.resume_json).read_text(encoding="utf-8"))
        if args.filter:
            terms = [t.strip() for t in args.filter.split(",") if t.strip()]
            resumed_rows = [r for r in resumed_rows if any(t2 in r["name"] for t2 in terms)]
        tasks = []
    else:
        tasks = _load_seed_tasks(Path(args.tasks_file))
        if args.filter:
            terms = [t.strip() for t in args.filter.split(",") if t.strip()]
            tasks = [t for t in tasks if any(t2 in t.name for t2 in terms)]

    if provider_from_env() is LLMProvider.BEDROCK:
        if not os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
            print("ANIMORA_LLM_PROVIDER=bedrock but AWS_BEARER_TOKEN_BEDROCK is missing.", file=sys.stderr)
            return 2
        api_key = ""
    else:
        api_key = settings.anthropic_api_key
        if not api_key:
            print("ANTHROPIC_API_KEY missing — set it in ai-backend/.env.", file=sys.stderr)
            return 2

    if not Path(args.animora_exe).exists():
        print(f"Animora/Blender executable not found: {args.animora_exe}", file=sys.stderr)
        return 2

    # Resolve to an absolute path BEFORE handing it to the render_worker
    # subprocess — render_worker.py runs inside Animora.exe, which may not
    # share this process's working directory, so a relative path here can
    # silently resolve to a different location in each process (exactly
    # what happened in the single-task dry run: renders were written
    # somewhere real but this process then looked for them in the wrong
    # place and found nothing).
    output_dir = Path(args.render_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    client = AnthropicClient(api_key=api_key, session_id="render-eval-harness")

    results: list[RenderTaskResult] = []
    if resumed_rows is not None:
        print(f"Resuming {len(resumed_rows)} task(s) from {args.resume_json} "
              "(re-render + re-score only, no orchestrator/LLM generation cost)...")
        for row in resumed_rows:
            print(f"\n=== {row['name']} ===")
            result = await _run_one_resumed(client, row, output_dir, args.animora_exe)
            results.append(result)
            if args.json:
                Path(args.json).write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
    else:
        print(f"Running {len(tasks)} task(s)...")
        for bench in tasks:
            print(f"\n=== {bench.name} ===")
            result = await _run_one(client, bench, output_dir, args.animora_exe)
            results.append(result)
            if args.json:
                Path(args.json).write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")

    report = _format_report(results)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"\nReport written to {args.output}")
    else:
        print("\n" + report)

    if args.json:
        Path(args.json).write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")
        print(f"JSON dump written to {args.json}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 0 render-based eval harness")
    parser.add_argument("--tasks-file", help="Path to seed_tasks.json (required unless --resume-json)")
    parser.add_argument("--resume-json", help="Path to a prior --json dump; re-render + re-score its "
                         "captured tool_calls against the current translator instead of re-running the "
                         "orchestrator (no LLM generation cost — for validating render-pipeline fixes)")
    parser.add_argument("--filter", help="Run only tasks whose name contains this substring")
    parser.add_argument("--output", help="Path to write the markdown report")
    parser.add_argument("--json", help="Path to write the full JSON results dump")
    parser.add_argument("--render-dir", default="eval_renders", help="Directory to write rendered PNGs")
    parser.add_argument("--animora-exe", default=_DEFAULT_ANIMORA_EXE, help="Path to Animora.exe / blender.exe")
    args = parser.parse_args()
    if not args.tasks_file and not args.resume_json:
        parser.error("either --tasks-file or --resume-json is required")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
