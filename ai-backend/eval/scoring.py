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


# ── MCP-pivot bridge: render atomic tool calls as bpy-equivalent text ───
# The benchmarks + every structural check above were written for the
# pre-MCP era when the model emitted a bpy SCRIPT (primitive_cube_add(),
# materials.new(), ...). The system now emits ATOMIC TOOL CALLS
# (create_primitive, apply_material, ...), which those regexes never
# match — so a perfect atomic build scored as a total failure.
#
# Rather than rewrite all 31 benchmarks + duplicate every counter, we
# translate the atomic calls into the bpy-equivalent text the existing
# regexes already understand. ONE function, here, makes the whole scorer
# architecture-agnostic; escape-hatch (real bpy script) builds are scored
# by concatenating their actual script, so both paths work.
_KIND_TO_BPY_OP: dict[str, str] = {
    "cube": "primitive_cube_add", "cuboid": "primitive_cube_add",
    "box": "primitive_cube_add",
    "sphere": "primitive_uv_sphere_add", "uv_sphere": "primitive_uv_sphere_add",
    "ico_sphere": "primitive_ico_sphere_add", "icosphere": "primitive_ico_sphere_add",
    "cylinder": "primitive_cylinder_add", "cone": "primitive_cone_add",
    "torus": "primitive_torus_add", "plane": "primitive_plane_add",
    "monkey": "primitive_monkey_add", "circle": "primitive_circle_add",
}
_MODIFIER_KIND_TO_BPY: dict[str, str] = {
    "bevel": "BEVEL", "subdivision_surface": "SUBSURF", "subsurf": "SUBSURF",
    "array": "ARRAY", "mirror": "MIRROR", "solidify": "SOLIDIFY",
    "decimate": "DECIMATE", "screw": "SCREW", "wireframe": "WIREFRAME",
}


def _fmt_xyz(vec: Any) -> str:
    try:
        x, y, z = (float(v) for v in list(vec)[:3])
        return f"{x}, {y}, {z}"
    except (TypeError, ValueError):
        return "0.0, 0.0, 0.0"


def _material_key(inp: dict[str, Any]) -> str:
    """Identity for material-variety counting: an explicit name if given,
    else the base_color rounded — so distinct colours read as distinct
    materials and 'one grey on everything' reads as a single material."""
    nm = inp.get("name")
    if nm:
        return str(nm)
    bc = inp.get("base_color")
    if isinstance(bc, (list, tuple)):
        return "color_" + "_".join(f"{round(float(c), 2)}" for c in bc if isinstance(c, (int, float)))
    return "Mat"


def render_tool_calls_as_bpy(tool_calls: list[dict[str, Any]]) -> str:
    """Translate atomic tool calls into bpy-equivalent text so the
    benchmark regexes + structural counters score an atomic build exactly
    as they would the equivalent legacy script. See block comment above."""
    lines: list[str] = []
    for tc in tool_calls:
        name = tc.get("name", "")
        inp = tc.get("input") or {}
        if name == "create_primitive":
            kind = str(inp.get("kind") or "cube").lower()
            op = _KIND_TO_BPY_OP.get(kind, "primitive_cube_add")
            lines.append(f"bpy.ops.mesh.{op}(location=({_fmt_xyz(inp.get('location'))}))")
            if inp.get("name"):
                lines.append(f'obj.name = "{inp["name"]}"')
        elif name == "duplicate_object":
            lines.append('dup = bpy.data.objects.new("dup", None)')
            if inp.get("name") or inp.get("new_name"):
                lines.append(f'dup.name = "{inp.get("name") or inp.get("new_name")}"')
        elif name == "apply_material":
            lines.append(f'mat = bpy.data.materials.new("{_material_key(inp)}")')
            lines.append("bsdf = nodes.new('ShaderNodeBsdfPrincipled')  # Principled BSDF")
            # Render the numeric PBR params so benchmarks that assert a
            # specific finish (e.g. `metallic=1.0` for industrial shelving)
            # match an atomic apply_material call, not just the escape hatch.
            for prop in ("metallic", "roughness", "alpha", "emission_strength"):
                if inp.get(prop) is not None:
                    try:
                        lines.append(f"{prop}={float(inp[prop])}")
                    except (TypeError, ValueError):
                        pass
            lines.append("obj.data.materials.append(mat)")
        elif name == "create_light":
            lt = str(inp.get("kind") or inp.get("type") or "point").upper()
            lines.append(f'light = bpy.data.lights.new(name="{inp.get("name", "Light")}", type="{lt}")')
            if inp.get("location"):
                lines.append(f"light_obj.location = ({_fmt_xyz(inp.get('location'))})")
        elif name == "create_camera":
            lines.append(f'cam = bpy.data.cameras.new("{inp.get("name", "Camera")}")')
            if inp.get("location"):
                lines.append(f"cam_obj.location = ({_fmt_xyz(inp.get('location'))})")
        elif name == "add_modifier":
            mt = _MODIFIER_KIND_TO_BPY.get(str(inp.get("kind") or "").lower(),
                                           str(inp.get("kind") or "BEVEL").upper())
            lines.append(f'mod = obj.modifiers.new("{inp.get("kind", "mod")}", type="{mt}")')
        elif name == "set_transform":
            if inp.get("location"):
                lines.append(f"obj.location = ({_fmt_xyz(inp.get('location'))})")
        elif name == "set_world":
            lines.append("world.use_nodes = True  # set_world")
        elif name in ("execute_animora_code", "execute_blender_script"):
            continue  # the real bpy script is concatenated by the caller
        else:
            # use_asset / load_asset / request_final_review / set_parent /
            # delete_object / get_* / viewport_screenshot — keep call syntax
            # so required_ops that match tool NAMES (e.g. `use_asset\(`) hit.
            kv = ", ".join(
                f'{k}="{str(v)[:60]}"' for k, v in inp.items() if isinstance(k, str)
            )
            lines.append(f"{name}({kv})")
    return "\n".join(lines)


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


def aggregate_critic_by_category(
    results: list[dict[str, Any]],
) -> dict[str, float]:
    """Mean deterministic-critic score per category (Stage 7).

    Only entries with a real critic_score (>= 0; -1.0 means not
    computed / escape-hatch build) are averaged. Categories whose
    entries all lack a score are omitted. Accepts dict form like
    aggregate_by_category."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for r in results:
        score = r.get("critic_score", -1.0)
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        if score < 0:
            continue
        cat = category_of(r["name"])
        sums[cat] = sums.get(cat, 0.0) + score
        counts[cat] = counts.get(cat, 0) + 1
    return {cat: round(sums[cat] / counts[cat], 3) for cat in sums}


# ── Stage 8 — Cost model (the SECONDARY axis, optimised after quality) ──
# Stage 7 made quality measurable + gated. Stage 8 adds cost so we can see
# — and block — WASTE: spending more without a quality gain. It never
# lowers the quality floor (the product's "maximum quality always" rule);
# a change that costs more AND raises quality is not a regression here.
#
# List prices in USD per 1M tokens for the logical model names the router
# emits (orchestrator/router.py). These are Anthropic public list prices;
# update if your contract differs. Cache discounts are intentionally NOT
# modelled: the eval is single-shot against a cold cache, so this is a
# conservative UPPER bound — exactly what a "are we wasting money" signal
# wants. An unknown model falls back to the Sonnet tier (the orchestrator
# default) so a cost is never silently zero.
_MODEL_PRICING: dict[str, tuple[float, float]] = {
    # logical name:               (input_per_mtok, output_per_mtok)
    "claude-opus-4-7":            (15.0, 75.0),
    "claude-sonnet-4-6":          (3.0, 15.0),
    "claude-haiku-4-5-20251001":  (1.0, 5.0),
}
_DEFAULT_PRICING = (3.0, 15.0)  # Sonnet tier — the orchestrator default


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate one benchmark's USD cost from its model + token usage."""
    in_price, out_price = _MODEL_PRICING.get(model or "", _DEFAULT_PRICING)
    try:
        it = max(0, int(input_tokens))
        ot = max(0, int(output_tokens))
    except (TypeError, ValueError):
        return 0.0
    return round((it * in_price + ot * out_price) / 1_000_000, 6)


def _result_cost(r: dict[str, Any]) -> float:
    """Cost for one result dict: prefer a stored `cost_usd` (the runner
    writes it), else estimate on the fly from model + tokens so a loaded
    baseline that predates the field still aggregates."""
    c = r.get("cost_usd")
    if c is not None:
        try:
            return max(0.0, float(c))
        except (TypeError, ValueError):
            pass
    return estimate_cost_usd(
        r.get("model", ""), r.get("input_tokens", 0), r.get("output_tokens", 0))


def aggregate_cost_by_category(results: list[dict[str, Any]]) -> dict[str, float]:
    """Mean estimated USD cost per category. Accepts dict form like the
    other aggregators."""
    sums: dict[str, float] = {}
    counts: dict[str, int] = {}
    for r in results:
        cat = category_of(r["name"])
        sums[cat] = sums.get(cat, 0.0) + _result_cost(r)
        counts[cat] = counts.get(cat, 0) + 1
    return {cat: round(sums[cat] / counts[cat], 6) for cat in sums}


def total_cost_usd(results: list[dict[str, Any]]) -> float:
    """Total estimated USD cost of a run."""
    return round(sum(_result_cost(r) for r in results), 6)


# ── Stage 7 — Quality targets (the "beat-the-MCP" bar) ──────────────────
# The MCP can't run in CI, so instead of comparing against it directly we
# encode the bar it struggles to clear — composition, organic forms,
# first-try correctness — as per-category targets: a minimum regex pass
# rate AND a minimum mean deterministic-critic score. These are ADVISORY
# (reported, not a hard CI gate) so we don't block every PR while quality
# climbs; the hard stop is the regression gate above. Tighten over time.
#
# Categories come from the benchmark-name prefix (see category_of). A
# category with no target uses the default.
QUALITY_TARGETS: dict[str, tuple[float, float]] = {
    # category:      (min_pass_rate, min_mean_critic_score)
    "primitive":     (1.00, 0.90),
    "furniture":     (0.80, 0.85),
    "scene":         (0.70, 0.75),
    "composition":   (0.70, 0.80),
    "vehicle":       (0.60, 0.75),
    "character":     (0.50, 0.70),
    "lighting":      (0.70, 0.75),
    "material":      (0.80, 0.80),
    "asset":         (0.70, 0.70),
}
_DEFAULT_TARGET = (0.60, 0.70)


@dataclass
class TargetStatus:
    category: str
    pass_rate: float
    mean_critic: float
    min_pass_rate: float
    min_mean_critic: float
    met: bool
    reason: str = ""


def evaluate_targets(results: list[dict[str, Any]]) -> dict[str, TargetStatus]:
    """Report, per category, whether the run clears its quality target.
    Advisory: surfaced in the scorecard, not a hard CI gate. A category
    is BELOW if either its pass rate or its mean critic score is under
    the target."""
    cats = aggregate_by_category(results)
    crit = aggregate_critic_by_category(results)
    out: dict[str, TargetStatus] = {}
    for cat, cs in cats.items():
        min_pass, min_crit = QUALITY_TARGETS.get(cat, _DEFAULT_TARGET)
        pass_rate = cs.pass_rate
        mean_critic = crit.get(cat, -1.0)
        below_bits: list[str] = []
        if pass_rate < min_pass:
            below_bits.append(f"pass {pass_rate:.0%} < {min_pass:.0%}")
        # Only judge critic score when we actually computed one for the
        # category (mean_critic >= 0); escape-hatch-only categories skip.
        if mean_critic >= 0 and mean_critic < min_crit:
            below_bits.append(f"critic {mean_critic:.2f} < {min_crit:.2f}")
        met = not below_bits
        out[cat] = TargetStatus(
            category=cat, pass_rate=pass_rate, mean_critic=mean_critic,
            min_pass_rate=min_pass, min_mean_critic=min_crit, met=met,
            reason="" if met else "; ".join(below_bits),
        )
    return out


# ── Regression detection ────────────────────────────────────────────────

# Stage 7 — regression thresholds for critic scores. A benchmark's
# critic_score dropping more than _SCORE_DROP_THRESHOLD is a regression
# even if it still passes the regex gate (the grey-couch-that-still-
# emits-primitive_cube_add failure mode). Category mean drops use a
# tighter band since they're averaged over multiple benchmarks.
_SCORE_DROP_THRESHOLD = 0.15
_CATEGORY_SCORE_DROP = 0.10

# Stage 8 — cost-regression thresholds. A benchmark's estimated cost is a
# regression when it rises by BOTH >= _COST_REL_INCREASE (relative) AND
# >= _COST_ABS_FLOOR (absolute — so a cheap benchmark doubling from
# $0.001 doesn't trip noise) AND its critic_score did NOT improve. The
# quality guard is the whole point: spending more to get MORE quality is
# not waste; spending more for the same-or-worse quality is. Cost gating
# only fires where we can CONFIRM quality stayed flat (both critic scores
# present) — otherwise it stays silent, so it never blocks a change we
# can't prove is wasteful.
_COST_REL_INCREASE = 0.30
_COST_ABS_FLOOR = 0.01
_CATEGORY_COST_REL_INCREASE = 0.25
_QUALITY_GAIN_EPS = 0.02  # critic-score gain above this counts as "improved"


@dataclass
class RegressionReport:
    """Result of comparing a new run against a saved baseline."""
    newly_failing: list[str] = field(default_factory=list)   # name list
    newly_passing: list[str] = field(default_factory=list)
    category_drops: list[tuple[str, float, float]] = field(default_factory=list)
    # (category, baseline_rate, new_rate) — only present when drop >= threshold
    # Stage 7 — critic-score regressions (additive to the pass/fail gate).
    score_drops: list[tuple[str, float, float]] = field(default_factory=list)
    # (benchmark_name, baseline_score, new_score)
    category_score_drops: list[tuple[str, float, float]] = field(default_factory=list)
    # (category, baseline_mean, new_mean)
    # Stage 8 — cost regressions (quality-neutral cost increases = waste).
    cost_regressions: list[tuple[str, float, float]] = field(default_factory=list)
    # (benchmark_name, baseline_cost_usd, new_cost_usd)
    category_cost_increases: list[tuple[str, float, float]] = field(default_factory=list)
    # (category, baseline_mean_cost, new_mean_cost)
    total_baseline: int = 0
    total_new: int = 0
    pass_baseline: int = 0
    pass_new: int = 0

    @property
    def has_regression(self) -> bool:
        """True iff anything got worse. The CI gate trips on this.
        Stage 7: includes per-benchmark + per-category critic-score drops
        (a quality regression that keeps the regex passing still fails).
        Stage 8: includes quality-neutral cost increases (pure waste)."""
        return bool(self.newly_failing) or bool(self.category_drops) \
            or bool(self.score_drops) or bool(self.category_score_drops) \
            or bool(self.cost_regressions) or bool(self.category_cost_increases)


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

    # Stage 7 — per-benchmark critic-score drops. Only compare entries
    # where BOTH baseline and new have a real score (>= 0). An old
    # baseline lacking critic_score safe-no-ops here until re-frozen.
    score_drops: list[tuple[str, float, float]] = []
    for name, new in new_by_name.items():
        base = baseline_by_name.get(name)
        if base is None:
            continue
        try:
            base_s = float(base.get("critic_score", -1.0))
            new_s = float(new.get("critic_score", -1.0))
        except (TypeError, ValueError):
            continue
        if base_s < 0 or new_s < 0:
            continue  # not comparable
        if new_s <= base_s - _SCORE_DROP_THRESHOLD:
            score_drops.append((name, round(base_s, 3), round(new_s, 3)))
    score_drops.sort()

    # Stage 7 — per-category mean-critic-score drops.
    base_crit = aggregate_critic_by_category(baseline_results)
    new_crit = aggregate_critic_by_category(new_results)
    category_score_drops: list[tuple[str, float, float]] = []
    for cat, new_mean in new_crit.items():
        base_mean = base_crit.get(cat)
        if base_mean is None:
            continue
        if new_mean <= base_mean - _CATEGORY_SCORE_DROP:
            category_score_drops.append((cat, base_mean, new_mean))
    category_score_drops.sort()

    # Stage 8 — per-benchmark cost regressions (quality-neutral only). A
    # cost rise is flagged ONLY when we can confirm the critic score did
    # not improve — paying more for the same/less quality is the waste we
    # want to block; paying more for better quality is allowed.
    cost_regressions: list[tuple[str, float, float]] = []
    for name, new in new_by_name.items():
        base = baseline_by_name.get(name)
        if base is None:
            continue
        base_c = _result_cost(base)
        new_c = _result_cost(new)
        if base_c <= 0:
            continue
        abs_inc = new_c - base_c
        if abs_inc < _COST_ABS_FLOOR or abs_inc / base_c < _COST_REL_INCREASE:
            continue
        try:
            base_s = float(base.get("critic_score", -1.0))
            new_s = float(new.get("critic_score", -1.0))
        except (TypeError, ValueError):
            continue
        if base_s < 0 or new_s < 0:
            continue  # can't confirm quality stayed flat → don't flag
        if new_s > base_s + _QUALITY_GAIN_EPS:
            continue  # paid more, got more quality → not waste
        cost_regressions.append((name, round(base_c, 6), round(new_c, 6)))
    cost_regressions.sort()

    # Stage 8 — per-category mean-cost increases, same quality guard on
    # the category mean critic score.
    base_cost = aggregate_cost_by_category(baseline_results)
    new_cost = aggregate_cost_by_category(new_results)
    category_cost_increases: list[tuple[str, float, float]] = []
    for cat, new_mean_c in new_cost.items():
        base_mean_c = base_cost.get(cat)
        if base_mean_c is None or base_mean_c <= 0:
            continue
        abs_inc = new_mean_c - base_mean_c
        if abs_inc < _COST_ABS_FLOOR or abs_inc / base_mean_c < _CATEGORY_COST_REL_INCREASE:
            continue
        bc = base_crit.get(cat)
        nc = new_crit.get(cat)
        if bc is not None and nc is not None and nc > bc + _QUALITY_GAIN_EPS:
            continue  # category got more quality for the spend → allowed
        category_cost_increases.append((cat, round(base_mean_c, 6), round(new_mean_c, 6)))
    category_cost_increases.sort()

    return RegressionReport(
        newly_failing=newly_failing,
        newly_passing=newly_passing,
        category_drops=category_drops,
        score_drops=score_drops,
        category_score_drops=category_score_drops,
        cost_regressions=cost_regressions,
        category_cost_increases=category_cost_increases,
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

    if report.score_drops:
        lines.append(f"### Critic-score regressions ({len(report.score_drops)})")
        lines.append(
            "_These still pass the regex gate but their structural "
            "quality dropped — the grey-couch failure mode._")
        for name, base, new in report.score_drops:
            lines.append(f"- `{name}`: critic {base:.2f} → {new:.2f} "
                         f"({(new - base):+.2f})")
        lines.append("")

    if report.category_score_drops:
        lines.append("### Category mean-critic-score drops")
        for cat, base, new in report.category_score_drops:
            lines.append(f"- **{cat}**: {base:.2f} → {new:.2f} "
                         f"({(new - base):+.2f})")
        lines.append("")

    if report.cost_regressions:
        lines.append(f"### Cost regressions ({len(report.cost_regressions)})")
        lines.append(
            "_Cost rose with no quality gain — pure waste. Spending more "
            "for BETTER quality is allowed and is not listed here._")
        for name, base, new in report.cost_regressions:
            pct = (new - base) / base * 100 if base else 0.0
            lines.append(f"- `{name}`: ${base:.4f} → ${new:.4f} ({pct:+.0f}%)")
        lines.append("")

    if report.category_cost_increases:
        lines.append("### Category mean-cost increases")
        for cat, base, new in report.category_cost_increases:
            pct = (new - base) / base * 100 if base else 0.0
            lines.append(f"- **{cat}**: ${base:.4f} → ${new:.4f} ({pct:+.0f}%)")
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
