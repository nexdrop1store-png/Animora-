"""
Pure scoring + aggregation + regression-detection logic for the eval
harness.

Split out of runner.py so both the runner CLI and the CI workflow
import the same scoring rules. Everything here is deliberately
side-effect-free: take a benchmark + a generated script + a
BenchmarkResult, return decisions; no I/O, no LLM calls, no logging.

Why a separate module: Phase 9 needs `scoring.py` reused by Phase 5.5's
unit tests (mocking out the LLM but still scoring the resulting script
against benchmark rules), and by the CI workflow which loads a saved
JSON dump (`--skip-llm` mode) and re-scores it without spending API
credits. Keeping these functions pure makes both cases trivial.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .benchmarks import Benchmark, GLOBAL_FORBIDDEN_OPS


# ── Single-script scoring primitives ─────────────────────────────────────

def check_regex_any(pattern: str, text: str) -> bool:
    return re.search(pattern, text) is not None


# Blender's default-named primitives. Asset-quality scripts MUST rename
# at least one of them — generic "Cube"/"Sphere" names are the #1 signal
# of throwaway-quality output.
_DEFAULT_NAMES: frozenset[str] = frozenset({
    "Cube", "Sphere", "UVSphere", "Plane", "Cylinder",
    "Cone", "Torus", "Icosphere", "Suzanne",
})


def has_meaningful_name(script: str) -> bool:
    """True if the script renames at least one object to something other
    than Blender's default primitive names."""
    matches = re.findall(r'\.name\s*=\s*[\'"]([^\'"]+)[\'"]', script)
    for n in matches:
        if n.strip() and n.strip() not in _DEFAULT_NAMES:
            return True
    return False


def has_material_setup(script: str) -> bool:
    """True if the script creates and assigns a material with a shader."""
    creates_mat = bool(re.search(r"materials\.new\(", script))
    uses_principled = bool(re.search(r"ShaderNodeBsdfPrincipled|Principled BSDF", script))
    appends_mat = bool(re.search(r"materials\.append\(", script))
    return creates_mat and (uses_principled or appends_mat)


# ── Sprint 2D: aesthetic-signal helpers ──────────────────────────────────
# The original scoring (above) checks technical correctness: did the script
# use the right ops, name objects, set up materials? It doesn't say
# anything about whether the result is COMPOSITIONALLY good. These helpers
# add cheap aesthetic signals that catch the "MCP donut" failure modes:
# single object at origin, single grey material, no light setup, etc.

_PRIMITIVE_ADD_RE = re.compile(
    r"primitive_(cube|uv_sphere|ico_sphere|cylinder|cone|torus|plane|monkey)_add\("
)
_LIGHT_NEW_RE = re.compile(r"lights\.new\s*\(\s*name\s*=\s*[\"'][^\"']*[\"']\s*,\s*type\s*=\s*[\"']([A-Z_]+)[\"']")
_LIGHT_OBJ_RE = re.compile(r"bpy\.data\.lights\.new")
_LOCATION_TUPLE_RE = re.compile(
    r"location\s*=\s*\(\s*(-?[\d.eE+-]+)\s*,\s*(-?[\d.eE+-]+)\s*,\s*(-?[\d.eE+-]+)\s*\)"
)
_MATERIAL_NEW_RE = re.compile(r"materials\.new\s*\(\s*(?:name\s*=\s*)?[\"']([^\"']+)[\"']")
_MODIFIER_NEW_RE = re.compile(
    r"modifiers\.new\s*\(\s*(?:name\s*=\s*)?[\"'][^\"']+[\"']\s*,\s*(?:type\s*=\s*)?[\"']([A-Z_]+)[\"']"
)


def count_distinct_objects(script: str) -> int:
    """Approximate count of distinct objects added in the script.
    Sums primitive_*_add calls + bpy.data.objects.new direct creations.
    A scene with this count <= 1 means "single object" — almost always
    a quality regression on multi-element benchmarks."""
    primitives = len(_PRIMITIVE_ADD_RE.findall(script))
    direct = len(re.findall(r"bpy\.data\.objects\.new\(", script))
    return primitives + direct


def count_distinct_positions(script: str) -> int:
    """Count distinct `location=(x,y,z)` tuples used. <= 1 means
    "everything at origin" — common failure mode."""
    positions = set()
    for x, y, z in _LOCATION_TUPLE_RE.findall(script):
        try:
            positions.add((round(float(x), 3), round(float(y), 3), round(float(z), 3)))
        except ValueError:
            continue
    return len(positions)


def count_light_sources(script: str) -> int:
    """Count `bpy.data.lights.new(...)` invocations. Three-point benchmarks
    expect >= 3; single-light scenes flatness-fail on lighting checks."""
    return len(_LIGHT_OBJ_RE.findall(script))


def has_material_variety(script: str) -> bool:
    """True if the script creates >= 2 distinct named materials.
    Catches "single grey on everything" — a recurring MCP failure mode
    where the model creates one PBR shader and shares it across every
    surface, producing a flat plastic-doll look."""
    names = set(_MATERIAL_NEW_RE.findall(script))
    return len(names) >= 2


def has_modifiers(script: str) -> bool:
    """True if the script adds at least one modifier (BEVEL, SUBSURF,
    ARRAY, MIRROR, etc.). Modifiers are a proxy for finished work —
    raw primitives with no modifier stack rarely meet the quality bar."""
    return bool(_MODIFIER_NEW_RE.search(script))


def score_against_benchmark(
    bench: Benchmark,
    script: str,
    *,
    output_tokens: int = 0,
    truncated: bool = False,
    script_validator_ok: bool = True,
    script_validator_reason: str = "",
) -> "ScoreVerdict":
    """The single source of truth for benchmark scoring.

    Returns a ScoreVerdict carrying the pass/fail decision plus
    structured detail (missing ops, forbidden ops, etc.) for reporting.
    Caller is responsible for collecting these into BenchmarkResult.

    `script == ""` is allowed (e.g. the model answered with text only
    for a question-intent benchmark). In that case material/naming/op
    checks are skipped — only the validator/truncation gates apply.
    """
    missing_ops: list[str] = []
    forbidden_ops_seen: list[str] = []
    notes: list[str] = []

    if script:
        # Required operators
        for pat in bench.required_ops:
            if not check_regex_any(pat, script):
                missing_ops.append(pat)
                notes.append(f"missing op `{pat}`")
        # Forbidden operators — per-benchmark
        for pat in bench.forbidden_ops:
            if check_regex_any(pat, script):
                forbidden_ops_seen.append(pat)
                notes.append(f"forbidden op `{pat}` present")
        # Forbidden operators — GLOBAL (deprecated Blender API).
        # Applied to every benchmark; catches use_auto_smooth and the
        # renamed Principled BSDF inputs that broke the 2026-05-21
        # Lamborghini script.
        for pat in GLOBAL_FORBIDDEN_OPS:
            if check_regex_any(pat, script):
                forbidden_ops_seen.append(f"GLOBAL: {pat}")
                notes.append(
                    f"deprecated Blender API `{pat}` present "
                    f"(see master prompt rule #12)"
                )

    name_ok = has_meaningful_name(script) if script else False
    if bench.required_named and script and not name_ok:
        notes.append("no meaningful .name= assignment")

    mat_ok = has_material_setup(script) if script else False
    if bench.require_material and script and not mat_ok:
        notes.append("no Principled BSDF material setup")

    over_budget = output_tokens > bench.budget_tokens
    if over_budget:
        notes.append(
            f"over token budget ({output_tokens} > {bench.budget_tokens})"
        )

    if truncated:
        notes.append("output truncated at max_tokens")

    if not script_validator_ok:
        notes.append(f"validator rejected: {script_validator_reason}")

    # ── Sprint 2D — aesthetic-signal checks ──────────────────────────
    # Opt-in via Benchmark fields. A benchmark with min_distinct_objects=3
    # that produces a script with only 1 primitive_*_add fails on this
    # axis. Hard-fails the benchmark when the floor isn't met.
    composition_fail = False

    if script and bench.min_distinct_objects > 0:
        obj_count = count_distinct_objects(script)
        if obj_count < bench.min_distinct_objects:
            notes.append(
                f"only {obj_count} distinct objects "
                f"(need >= {bench.min_distinct_objects})"
            )
            composition_fail = True

    if script and bench.min_distinct_positions > 0:
        pos_count = count_distinct_positions(script)
        if pos_count < bench.min_distinct_positions:
            notes.append(
                f"only {pos_count} distinct positions "
                f"(need >= {bench.min_distinct_positions}) — everything stacked?"
            )
            composition_fail = True

    if script and bench.min_light_sources > 0:
        light_count = count_light_sources(script)
        if light_count < bench.min_light_sources:
            notes.append(
                f"only {light_count} light sources "
                f"(need >= {bench.min_light_sources})"
            )
            composition_fail = True

    if script and bench.require_material_variety and not has_material_variety(script):
        notes.append("single-material setup (need >= 2 distinct materials)")
        composition_fail = True

    if script and bench.require_modifiers and not has_modifiers(script):
        notes.append("no modifiers added (raw primitives only)")
        composition_fail = True

    # Hard-fail conditions (anything in here flips the benchmark to
    # FAIL). Soft-fails (over_budget) are noted but don't flip ok.
    hard_fail = (
        truncated
        or not script_validator_ok
        or bool(missing_ops)
        or bool(forbidden_ops_seen)
        or (bench.required_named and script and not name_ok)
        or (bench.require_material and script and not mat_ok)
        or composition_fail
    )

    return ScoreVerdict(
        ok=not hard_fail,
        missing_ops=missing_ops,
        forbidden_ops_seen=forbidden_ops_seen,
        has_name_assignment=name_ok,
        has_material=mat_ok,
        over_token_budget=over_budget,
        notes=notes,
    )


@dataclass
class ScoreVerdict:
    """The structured output of scoring one benchmark run.

    The runner copies these fields into its BenchmarkResult dataclass;
    we keep this type separate so scoring is testable in isolation
    without depending on the runner's broader BenchmarkResult schema.
    """
    ok: bool
    missing_ops: list[str] = field(default_factory=list)
    forbidden_ops_seen: list[str] = field(default_factory=list)
    has_name_assignment: bool = False
    has_material: bool = False
    over_token_budget: bool = False
    notes: list[str] = field(default_factory=list)


# ── Aggregation ──────────────────────────────────────────────────────────

def category_of(bench_name: str) -> str:
    """Categorize a benchmark by its name prefix.

    Benchmark names follow `category.subname` (e.g. `primitive.cube`,
    `vehicle.lambo_urus`). The category is everything before the first
    dot. A benchmark with no dot becomes its own category.
    """
    return bench_name.split(".", 1)[0] if "." in bench_name else bench_name


@dataclass
class CategoryScore:
    category: str
    passed: int = 0
    total: int = 0

    @property
    def pass_rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


def aggregate_by_category(results: list[dict[str, Any]]) -> dict[str, CategoryScore]:
    """Group results by benchmark category, return per-category pass rate.

    Accepts dict form (the JSON-serializable output of asdict() on
    BenchmarkResult) so callers can hand us a loaded baseline.json
    without needing to reconstruct dataclass instances.
    """
    scores: dict[str, CategoryScore] = {}
    for r in results:
        cat = category_of(r["name"])
        s = scores.setdefault(cat, CategoryScore(category=cat))
        s.total += 1
        if r.get("ok"):
            s.passed += 1
    return scores


# ── Regression detection ────────────────────────────────────────────────

@dataclass
class RegressionReport:
    """Result of comparing a new run against a saved baseline."""
    newly_failing: list[str] = field(default_factory=list)   # name list
    newly_passing: list[str] = field(default_factory=list)
    category_drops: list[tuple[str, float, float]] = field(default_factory=list)
    # (category, baseline_rate, new_rate) — only present when drop >= threshold
    total_baseline: int = 0
    total_new: int = 0
    pass_baseline: int = 0
    pass_new: int = 0

    @property
    def has_regression(self) -> bool:
        """True iff anything got worse. The CI gate trips on this."""
        return bool(self.newly_failing) or bool(self.category_drops)


def compare_to_baseline(
    new_results: list[dict[str, Any]],
    baseline_results: list[dict[str, Any]],
    *,
    category_drop_threshold: float = 0.10,
) -> RegressionReport:
    """Diff a fresh run against a saved baseline.

    A 'regression' is either:
      - A specific benchmark that PASSED in baseline but FAILS now
      - A category whose pass rate dropped by >= `category_drop_threshold`
        (default 10%)

    Newly-passing benchmarks (failed in baseline, pass now) are
    surfaced as good news, not regressions — they should drive the
    baseline being re-frozen on the next merge.
    """
    baseline_by_name = {r["name"]: r for r in baseline_results}
    new_by_name = {r["name"]: r for r in new_results}

    newly_failing = sorted([
        name for name, new in new_by_name.items()
        if not new.get("ok")
        and baseline_by_name.get(name, {}).get("ok") is True
    ])
    newly_passing = sorted([
        name for name, new in new_by_name.items()
        if new.get("ok")
        and baseline_by_name.get(name, {}).get("ok") is False
    ])

    base_cats = aggregate_by_category(baseline_results)
    new_cats = aggregate_by_category(new_results)
    category_drops: list[tuple[str, float, float]] = []
    for cat, new_score in new_cats.items():
        base_score = base_cats.get(cat)
        if base_score is None:
            continue
        if new_score.pass_rate <= base_score.pass_rate - category_drop_threshold:
            category_drops.append((cat, base_score.pass_rate, new_score.pass_rate))
    category_drops.sort()

    return RegressionReport(
        newly_failing=newly_failing,
        newly_passing=newly_passing,
        category_drops=category_drops,
        total_baseline=len(baseline_results),
        total_new=len(new_results),
        pass_baseline=sum(1 for r in baseline_results if r.get("ok")),
        pass_new=sum(1 for r in new_results if r.get("ok")),
    )


def format_regression_report(report: RegressionReport) -> str:
    """Human-readable summary suitable for CI logs / PR comments."""
    lines = [
        "## Eval regression report",
        "",
        f"**Baseline:** {report.pass_baseline}/{report.total_baseline} passed  "
        f"**This run:** {report.pass_new}/{report.total_new} passed",
        "",
    ]
    if not report.has_regression and not report.newly_passing:
        lines.append("No score change vs baseline.")
        return "\n".join(lines)

    if report.newly_failing:
        lines.append(f"### Newly failing ({len(report.newly_failing)})")
        for name in report.newly_failing:
            lines.append(f"- `{name}` — was passing on baseline, fails now")
        lines.append("")

    if report.category_drops:
        lines.append("### Category pass-rate drops")
        for cat, base, new in report.category_drops:
            lines.append(
                f"- **{cat}**: {base:.0%} → {new:.0%} "
                f"({(new - base) * 100:+.0f} pp)"
            )
        lines.append("")

    if report.newly_passing:
        lines.append(f"### Newly passing ({len(report.newly_passing)}) — good news")
        for name in report.newly_passing:
            lines.append(f"- `{name}` — was failing on baseline, passes now")
        lines.append("")
        lines.append(
            "_If these gains are stable, re-freeze the baseline by running "
            "`python -m ai_backend.eval.runner --output-baseline ai-backend/eval/baseline.json`._"
        )

    return "\n".join(lines)
