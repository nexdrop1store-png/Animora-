"""
Stage 2 — Rubrics & the Automatic Critic (deterministic scene-data layer).

The training brief splits verification into two layers:

  • VISION layer — `orchestrator/quality.py` (`run_artists_eye_check`) calls
    Claude Sonnet vision on a viewport screenshot. It judges things only
    pixels reveal: blown/crushed lighting, muddy sculpt, material read,
    silhouette. Expensive (one Sonnet call), already shipped.

  • SCENE-DATA layer — THIS module. A deterministic critic that scores the
    live scene graph (the dict from `vision.serialize_scene_graph()`) with
    zero LLM calls. It catches the structural defects that don't need a
    picture: extreme scale, floating objects, ungrounded placement, missing
    materials (the grey-couch failure), default Blender names, too-few
    elements for a scene, no light, and flat (origin-clustered) composition.

Why deterministic matters for training (Stages 4-5): the scene-data critic
is FREE and INSTANT, so it can run as a dense reward signal on every step
without burning API budget, and it never hallucinates a verdict. The vision
critic remains the taste layer; this is the correctness floor.

What this layer CANNOT see (documented, stays in the vision layer):
  • n-gons / topology quality — `serialize_scene_graph` exposes
    vertex_count + polygon_count but NOT per-face vertex counts, so we
    can't count n-gons from scene data. Topology is a vision/script check.
  • blown or crushed lighting — needs the rendered pixels.
  • material *read* (does it look like oak?) — needs pixels; we only check
    that a material EXISTS and isn't default grey.

Usage:
    from .critic import run_scene_critic
    report = run_scene_critic(scene_graph, require_materials=True,
                              require_light=False, expected_min_objects=1)
    if not report.passed:
        # report.errors gives the actionable findings; feed them to the
        # CORRECT step or use report.score as a reward.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# ── Severity ladder ────────────────────────────────────────────────────
SEVERITY_ERROR = "error"      # gates "passed"; must be fixed before shipping
SEVERITY_WARNING = "warning"  # lowers score; should be fixed
SEVERITY_INFO = "info"        # observation; no score impact


# ── Per-discipline rubric definitions ──────────────────────────────────
# Encodes the brief's PER-DISCIPLINE QUALITY METHOD as a structured table.
# Each entry: check id → (discipline, default severity, one-line standard).
# The scene-data layer implements the subset checkable WITHOUT pixels; the
# `vision_only` entries are declared here for completeness so the rubric is
# a single source of truth, but they're scored by quality.py, not here.
@dataclass(frozen=True)
class RubricCheck:
    check_id: str
    discipline: str
    severity: str
    standard: str          # what "correct" means, from the brief
    scene_data: bool       # True = this module scores it; False = vision layer


RUBRICS: tuple[RubricCheck, ...] = (
    # ── MODELING ──
    RubricCheck("scale_sanity", "MODELING", SEVERITY_ERROR,
                "Objects use sane real-world scale; no exploded or "
                "microscopic transforms.", True),
    RubricCheck("grounded_placement", "MODELING", SEVERITY_WARNING,
                "Objects are grounded / sit on surfaces; nothing floats "
                "far above the ground with no support.", True),
    RubricCheck("meaningful_names", "MODELING", SEVERITY_WARNING,
                "Objects are named for what they are, not left as 'Cube' / "
                "'Sphere' defaults.", True),
    RubricCheck("topology_clean", "MODELING", SEVERITY_WARNING,
                "Quad-dominant topology, no n-gons on curved/deforming "
                "areas.", False),  # vision/script — no per-face data in graph
    # ── MATERIALS ──
    RubricCheck("materials_present", "MATERIALS", SEVERITY_ERROR,
                "Every visible mesh surface has a material applied — no "
                "default grey.", True),
    RubricCheck("material_read", "MATERIALS", SEVERITY_WARNING,
                "Materials read unmistakably as the intended surface under "
                "the scene's lighting.", False),  # vision
    # ── LIGHTING ──
    RubricCheck("light_present", "LIGHTING", SEVERITY_WARNING,
                "A finished asset/scene has at least one motivated light.",
                True),
    RubricCheck("lighting_exposure", "LIGHTING", SEVERITY_WARNING,
                "No blown or crushed values; clear motivated intent.",
                False),  # vision — needs pixels
    # ── COMPOSITION ──
    RubricCheck("scene_element_count", "COMPOSITION", SEVERITY_ERROR,
                "A scene is composed of multiple distinct elements across "
                "FG/MG/BG, not one primitive.", True),
    RubricCheck("placement_variety", "COMPOSITION", SEVERITY_WARNING,
                "Deliberate spacing; elements not all clustered at the "
                "origin in a flat heap.", True),
)

# Fast lookup for the scene-data checks' default severities.
_RUBRIC_BY_ID: dict[str, RubricCheck] = {r.check_id: r for r in RUBRICS}


# ── Finding + report ───────────────────────────────────────────────────
@dataclass
class CriticFinding:
    check_id: str
    discipline: str
    severity: str
    passed: bool
    detail: str
    objects: list[str] = field(default_factory=list)


@dataclass
class CriticReport:
    findings: list[CriticFinding] = field(default_factory=list)
    score: float = 1.0          # 0.0–1.0 aggregate (reward signal)
    passed: bool = True         # True iff no ERROR-severity finding failed
    summary: str = ""

    @property
    def errors(self) -> list[CriticFinding]:
        return [f for f in self.findings if not f.passed and f.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[CriticFinding]:
        return [f for f in self.findings if not f.passed and f.severity == SEVERITY_WARNING]

    @property
    def failed(self) -> list[CriticFinding]:
        return [f for f in self.findings if not f.passed]

    def actionable_text(self) -> str:
        """Render the failed findings as a corrective brief for the CORRECT
        step. Errors first, then warnings."""
        lines: list[str] = []
        for f in self.errors + self.warnings:
            objs = f" [{', '.join(f.objects[:5])}]" if f.objects else ""
            lines.append(f"  • ({f.severity}) {f.check_id}: {f.detail}{objs}")
        return "\n".join(lines)


# ── Helpers ─────────────────────────────────────────────────────────────
_DEFAULT_NAMES: frozenset[str] = frozenset({
    "Cube", "Sphere", "UVSphere", "Plane", "Cylinder", "Cone", "Torus",
    "Icosphere", "Ico Sphere", "Suzanne", "Empty", "Circle", "Grid",
})

_MESH_TYPES: frozenset[str] = frozenset({"MESH"})
_LIGHT_TYPES: frozenset[str] = frozenset({"LIGHT"})
# Objects we never expect to carry a material / be "grounded" etc.
_NON_GEOMETRY_TYPES: frozenset[str] = frozenset({"CAMERA", "LIGHT", "EMPTY",
                                                  "SPEAKER", "LIGHT_PROBE"})

# Scale beyond this on any axis (or below its reciprocal) is almost always
# an accident — a 500× cube or a 0.001× plane. Tuned loose so legitimate
# big-but-intentional builds (a 20 m wall) don't trip it.
_SCALE_MAX = 250.0
_SCALE_MIN = 1.0 / _SCALE_MAX

# An object whose origin sits this far above z=0 with no parent is treated
# as a floating-placement candidate. Heuristic — the scene graph has no
# bounding boxes, only the object origin. Parented objects are exempt
# (their parent positions them deliberately).
_FLOAT_Z_THRESHOLD = 5.0


def _objects(scene_graph: dict) -> list[dict]:
    objs = scene_graph.get("objects")
    return objs if isinstance(objs, list) else []


def _mesh_objects(scene_graph: dict) -> list[dict]:
    return [o for o in _objects(scene_graph) if o.get("type") in _MESH_TYPES]


def _is_visible(o: dict) -> bool:
    # Default True when the field is absent (older addon payloads).
    return bool(o.get("visible", True))


# ── Deterministic scene-data checks ─────────────────────────────────────
def check_scale_sanity(scene_graph: dict) -> CriticFinding:
    bad: list[str] = []
    for o in _objects(scene_graph):
        scale = o.get("scale") or [1, 1, 1]
        for axis in scale[:3]:
            try:
                a = abs(float(axis))
            except (TypeError, ValueError):
                continue
            if a > _SCALE_MAX or (0 < a < _SCALE_MIN):
                bad.append(o.get("name", "?"))
                break
    passed = not bad
    return CriticFinding(
        "scale_sanity", "MODELING", SEVERITY_ERROR, passed,
        ("All object scales are within sane bounds."
         if passed else
         f"{len(bad)} object(s) have extreme scale "
         f"(>{_SCALE_MAX:.0f}× or <{_SCALE_MIN:.4f}×) — likely an accident."),
        bad,
    )


def check_grounded_placement(scene_graph: dict) -> CriticFinding:
    floating: list[str] = []
    for o in _mesh_objects(scene_graph):
        if o.get("parent"):
            continue  # parented → positioned deliberately by its parent
        loc = o.get("location") or [0, 0, 0]
        try:
            z = float(loc[2])
        except (TypeError, ValueError, IndexError):
            continue
        if z > _FLOAT_Z_THRESHOLD:
            floating.append(o.get("name", "?"))
    passed = not floating
    return CriticFinding(
        "grounded_placement", "MODELING", SEVERITY_WARNING, passed,
        ("Objects are grounded."
         if passed else
         f"{len(floating)} unparented mesh(es) sit far above the ground "
         f"(z > {_FLOAT_Z_THRESHOLD:.0f} m) — verify they aren't floating."),
        floating,
    )


def check_meaningful_names(scene_graph: dict) -> CriticFinding:
    default_named: list[str] = []
    for o in _mesh_objects(scene_graph):
        name = (o.get("name") or "").strip()
        # Strip Blender's ".001" numeric suffix before comparing.
        base = name.split(".")[0]
        if base in _DEFAULT_NAMES:
            default_named.append(name)
    passed = not default_named
    return CriticFinding(
        "meaningful_names", "MODELING", SEVERITY_WARNING, passed,
        ("Objects are named meaningfully."
         if passed else
         f"{len(default_named)} object(s) keep Blender default names "
         f"(Cube / Sphere / …) — rename to what they represent."),
        default_named,
    )


def check_materials_present(scene_graph: dict) -> CriticFinding:
    grey: list[str] = []
    for o in _mesh_objects(scene_graph):
        if not _is_visible(o):
            continue
        mats = o.get("materials")
        # materials is a list of names; None entries are empty slots.
        has_real = isinstance(mats, list) and any(m for m in mats)
        if not has_real:
            grey.append(o.get("name", "?"))
    passed = not grey
    return CriticFinding(
        "materials_present", "MATERIALS", SEVERITY_ERROR, passed,
        ("Every visible mesh has a material."
         if passed else
         f"{len(grey)} visible mesh(es) have NO material — they render as "
         f"default grey. Apply a material to each."),
        grey,
    )


def check_light_present(scene_graph: dict, *, required: bool) -> CriticFinding:
    lights = [o.get("name", "?") for o in _objects(scene_graph)
              if o.get("type") in _LIGHT_TYPES]
    passed = (len(lights) > 0) or (not required)
    sev = SEVERITY_WARNING if required else SEVERITY_INFO
    return CriticFinding(
        "light_present", "LIGHTING", sev, passed,
        (f"{len(lights)} light(s) in the scene."
         if lights else
         "No light in the scene — a finished render needs at least one "
         "motivated light."),
        lights,
    )


def check_scene_element_count(scene_graph: dict, *, min_objects: int) -> CriticFinding:
    meshes = _mesh_objects(scene_graph)
    n = len(meshes)
    passed = n >= min_objects
    return CriticFinding(
        "scene_element_count", "COMPOSITION", SEVERITY_ERROR, passed,
        (f"{n} mesh element(s) (≥ {min_objects} required)."
         if passed else
         f"Only {n} mesh element(s); a scene of this kind needs at least "
         f"{min_objects}. This is a blockout, not a finished result."),
        [o.get("name", "?") for o in meshes],
    )


def check_placement_variety(scene_graph: dict, *, min_objects_for_check: int = 3) -> CriticFinding:
    meshes = _mesh_objects(scene_graph)
    if len(meshes) < min_objects_for_check:
        # Too few objects to judge spread; not applicable.
        return CriticFinding(
            "placement_variety", "COMPOSITION", SEVERITY_INFO, True,
            "Too few objects to assess placement variety.", [],
        )
    locs: list[tuple[float, float, float]] = []
    for o in meshes:
        loc = o.get("location") or [0, 0, 0]
        try:
            locs.append((float(loc[0]), float(loc[1]), float(loc[2])))
        except (TypeError, ValueError, IndexError):
            continue
    if not locs:
        return CriticFinding(
            "placement_variety", "COMPOSITION", SEVERITY_INFO, True,
            "No usable locations.", [],
        )
    # Spread = mean pairwise distance proxy via per-axis stdev. If every
    # object sits at (almost) the same point, all stdevs ≈ 0.
    def _stdev(vals: list[float]) -> float:
        m = sum(vals) / len(vals)
        return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))
    sx = _stdev([p[0] for p in locs])
    sy = _stdev([p[1] for p in locs])
    sz = _stdev([p[2] for p in locs])
    spread = sx + sy + sz
    # Below this, the objects are effectively heaped at one point.
    passed = spread > 0.25
    return CriticFinding(
        "placement_variety", "COMPOSITION", SEVERITY_WARNING, passed,
        ("Objects are spread with deliberate spacing."
         if passed else
         "Objects are clustered at nearly the same position — flat "
         "composition, no FG/MG/BG depth. Spread them out."),
        [] if passed else [o.get("name", "?") for o in meshes],
    )


# ── Entry point ─────────────────────────────────────────────────────────
def run_scene_critic(
    scene_graph: dict,
    *,
    require_materials: bool = True,
    require_light: bool = False,
    expected_min_objects: int = 1,
) -> CriticReport:
    """Score a scene graph against the deterministic rubric checks.

    Args:
        scene_graph: the dict from `vision.serialize_scene_graph()`.
        require_materials: gate the materials check as an ERROR (default).
            The grey-couch failure makes this on-by-default the right call.
        require_light: when True, a scene with no light fails the lighting
            check at WARNING severity (set True for "finished" hero builds,
            False for in-progress / single-object edits).
        expected_min_objects: minimum mesh count for the scene-element
            check. 1 for a single asset; 6+ for a scene noun (beach, room).

    Returns:
        A CriticReport. `passed` is True iff no ERROR-severity finding
        failed. `score` is a 0–1 aggregate suitable as a training reward.
    """
    findings: list[CriticFinding] = [
        check_scale_sanity(scene_graph),
        check_grounded_placement(scene_graph),
        check_meaningful_names(scene_graph),
        check_scene_element_count(scene_graph, min_objects=expected_min_objects),
        check_placement_variety(scene_graph),
        check_light_present(scene_graph, required=require_light),
    ]
    if require_materials:
        findings.append(check_materials_present(scene_graph))

    # Score: weighted by severity. Errors cost more than warnings; info
    # never affects score. A perfectly clean scene scores 1.0.
    weights = {SEVERITY_ERROR: 1.0, SEVERITY_WARNING: 0.4, SEVERITY_INFO: 0.0}
    total_weight = sum(weights[f.severity] for f in findings) or 1.0
    lost = sum(weights[f.severity] for f in findings if not f.passed)
    score = max(0.0, 1.0 - lost / total_weight)

    passed = not any(
        (not f.passed) and f.severity == SEVERITY_ERROR for f in findings
    )

    n_err = sum(1 for f in findings if not f.passed and f.severity == SEVERITY_ERROR)
    n_warn = sum(1 for f in findings if not f.passed and f.severity == SEVERITY_WARNING)
    if passed and n_warn == 0:
        summary = "Scene passes the deterministic critic with no findings."
    elif passed:
        summary = f"Scene passes (no errors) with {n_warn} warning(s)."
    else:
        summary = f"Scene FAILS: {n_err} error(s), {n_warn} warning(s)."

    return CriticReport(findings=findings, score=round(score, 3),
                        passed=passed, summary=summary)


# ── Offline scene reconstruction ────────────────────────────────────────
# The deterministic critic needs a scene graph, which normally only
# exists in a live Blender session. To score builds OFFLINE — in the
# eval harness, in best-of-N sampling, in demonstration capture — we
# reconstruct a predicted scene graph from the captured atomic tool
# calls. The reconstruction is faithful for the atomic surface
# (create_*, set_transform, add_modifier, apply_material, set_parent,
# delete_object, duplicate_object, set_world). The escape hatch
# (execute_animora_code) is opaque — its bpy script could create
# anything — so a build that used it is marked `_reconstruction_partial`
# and the caller should fall back to the live critic / vision layer for
# those.

_PRIMITIVE_TOOL_TYPE = {
    "create_primitive": "MESH",
    "create_light": "LIGHT",
    "create_camera": "CAMERA",
}


def reconstruct_scene_graph(tool_calls: list[dict]) -> dict:
    """Build a predicted scene-graph dict from captured atomic tool
    calls. Each call is `{"name": str, "input": dict}` (the shape the
    eval runner and the recorder capture).

    Returns a dict with the same `objects` shape `run_scene_critic`
    expects, plus `_reconstruction_partial: True` if an
    execute_animora_code call was seen (the critic's verdict is then
    advisory, since the script's effects aren't modelled).
    """
    objects: dict[str, dict] = {}  # name → object entry, insertion-ordered
    partial = False

    def _ensure(name: str, otype: str) -> dict:
        o = objects.get(name)
        if o is None:
            o = {
                "name": name, "type": otype,
                "location": [0.0, 0.0, 0.0],
                "rotation": [0.0, 0.0, 0.0],
                "scale": [1.0, 1.0, 1.0],
                "visible": True, "selected": False,
                "modifiers": [], "parent": None, "materials": [],
            }
            objects[name] = o
        return o

    for call in tool_calls:
        name = call.get("name", "")
        inp = call.get("input") or {}
        if name in ("execute_animora_code", "execute_blender_script",
                    "execute_blender_code"):
            partial = True
            continue
        if name in _PRIMITIVE_TOOL_TYPE:
            obj_name = str(inp.get("name", "")).strip()
            if not obj_name:
                continue
            o = _ensure(obj_name, _PRIMITIVE_TOOL_TYPE[name])
            if inp.get("location") is not None:
                o["location"] = list(inp["location"])[:3]
            if inp.get("rotation") is not None:
                o["rotation"] = list(inp["rotation"])[:3]
            if inp.get("scale") is not None:
                o["scale"] = list(inp["scale"])[:3]
        elif name == "set_transform":
            obj_name = str(inp.get("name", "")).strip()
            if not obj_name:
                continue
            o = _ensure(obj_name, "MESH")
            if inp.get("location") is not None:
                o["location"] = list(inp["location"])[:3]
            if inp.get("rotation") is not None:
                o["rotation"] = list(inp["rotation"])[:3]
            if inp.get("scale") is not None:
                o["scale"] = list(inp["scale"])[:3]
        elif name == "apply_material":
            target = str(inp.get("object", "")).strip()
            if not target:
                continue
            o = _ensure(target, "MESH")
            mat_name = str(inp.get("name", "")).strip() or f"Mat_{target}"
            o["materials"] = [mat_name]
        elif name == "add_modifier":
            target = str(inp.get("object", "")).strip()
            if not target:
                continue
            o = _ensure(target, "MESH")
            kind = str(inp.get("kind", "")).upper()
            o["modifiers"].append({"type": kind, "name": kind.title()})
        elif name == "set_parent":
            child = str(inp.get("child", "")).strip()
            parent = str(inp.get("parent", "")).strip()
            if child and parent:
                _ensure(child, "MESH")["parent"] = parent
        elif name == "delete_object":
            objects.pop(str(inp.get("name", "")).strip(), None)
        elif name == "duplicate_object":
            src = str(inp.get("source", "")).strip()
            new = str(inp.get("new_name", "")).strip()
            if src in objects and new:
                clone = dict(objects[src])
                clone["name"] = new
                offset = inp.get("location_offset") or [0, 0, 0]
                base_loc = objects[src].get("location", [0, 0, 0])
                clone["location"] = [
                    float(base_loc[i]) + float(offset[i]) for i in range(3)
                ]
                clone["materials"] = list(objects[src].get("materials", []))
                objects[new] = clone
        # use_asset / load_asset / set_world / get_* — not geometry the
        # mesh-count + material checks act on; skipped for reconstruction.

    graph: dict = {
        "scene_name": "ReconstructedScene",
        "mode": "OBJECT",
        "objects": list(objects.values()),
    }
    if partial:
        graph["_reconstruction_partial"] = True
    return graph


def score_tool_calls(
    tool_calls: list[dict],
    *,
    require_materials: bool = True,
    require_light: bool = False,
    expected_min_objects: int = 1,
) -> CriticReport:
    """Convenience: reconstruct a scene graph from captured tool calls
    and score it. Used by the eval harness + best-of-N sampling to get
    a critic verdict offline (no live Blender). If the build used the
    escape hatch, the reconstruction is partial and the report's
    summary notes it."""
    graph = reconstruct_scene_graph(tool_calls)
    report = run_scene_critic(
        graph,
        require_materials=require_materials,
        require_light=require_light,
        expected_min_objects=expected_min_objects,
    )
    if graph.get("_reconstruction_partial"):
        report.summary = (
            "[partial — build used execute_animora_code; script effects "
            "not modelled] " + report.summary
        )
    return report


# ── First-step soundness (Stage 7) ──────────────────────────────────────
def first_step_diagnosis(tool_calls: list[dict]) -> tuple[bool | None, str]:
    """Was the build's FIRST executed step a sound foundation, and why?

    The brief (Stage 6): "the very first action must establish the
    correct foundation (scale, proportion, layout)." From scene-data
    alone we judge the first real (mutating) call:
      • it exists (the model actually started building), AND
      • it CREATES geometry (not a material/parent/transform on a
        not-yet-created object), AND
      • its scale is within the sane bounds (no 900× exploded first
        cube, no microscopic 0.001× first plane).

    Returns a (verdict, reason) pair:
      • (True, "")            — sound foundation.
      • (False, reason)       — bad foundation; `reason` is a short
                                human phrase the runtime gate feeds into
                                its correction message.
      • (None, "")            — no judgable foundation step (pure-question
                                turns, or the build went straight to the
                                opaque escape hatch we can't introspect).
    """
    for call in tool_calls:
        name = call.get("name", "")
        inp = call.get("input") or {}
        if name in ("execute_animora_code", "execute_blender_script",
                    "execute_blender_code"):
            # Opaque — can't judge the foundation from a bpy script here.
            return None, ""
        if name in _PRIMITIVE_TOOL_TYPE:  # create_primitive/light/camera
            scale = inp.get("scale") or [1, 1, 1]
            for axis in list(scale)[:3]:
                try:
                    a = abs(float(axis))
                except (TypeError, ValueError):
                    continue
                if a > _SCALE_MAX:
                    return False, (
                        f"the first object was created at an exploded "
                        f"scale ({a:.0f}× on one axis, over the {_SCALE_MAX:.0f}× "
                        f"sanity limit)")
                if 0 < a < _SCALE_MIN:
                    return False, (
                        f"the first object was created microscopically "
                        f"small ({a:.4f}× on one axis, under the "
                        f"{_SCALE_MIN:.4f}× sanity limit)")
            return True, ""  # first create with sane scale
        if name == "set_parent":
            return False, (
                "the build opened with set_parent — you cannot parent "
                "before the objects exist")
        # set_transform / apply_material / etc. as the very first call
        # means there's no created object to act on → invalid first step.
        if name in ("set_transform", "apply_material", "add_modifier",
                    "delete_object", "duplicate_object"):
            return False, (
                f"the build opened with {name} before any geometry "
                f"existed — there was nothing to act on")
        # Read-only first calls (get_scene_info, viewport_screenshot)
        # are fine — keep scanning for the first real action.
    return None, ""  # no executable foundation step found


def first_step_ok(tool_calls: list[dict]) -> bool | None:
    """Thin verdict-only wrapper over `first_step_diagnosis` (the metric
    surface Stage 7's eval scoreboard reports). Returns True / False /
    None; the `reason` is dropped here."""
    verdict, _reason = first_step_diagnosis(tool_calls)
    return verdict


# ── Best-of-N selection ─────────────────────────────────────────────────
@dataclass
class BestOfNResult:
    best_index: int                 # index of the winning candidate
    best_report: CriticReport       # the winner's critic report
    all_reports: list[CriticReport] = field(default_factory=list)
    best_score: float = 0.0
    mean_score: float = 0.0
    worst_score: float = 0.0
    n: int = 0


def select_best_candidate(
    candidates: list[list[dict]],
    *,
    require_materials: bool = True,
    require_light: bool = False,
    expected_min_objects: int = 1,
) -> BestOfNResult:
    """Best-of-N selection. Each candidate is a captured tool-call list
    (one full build attempt). Score every candidate offline via the
    deterministic critic and return the highest-scoring one, plus
    summary stats (best / mean / worst) that quantify how consistent
    the model is on this request.

    Tie-break: among equal scores, prefer the candidate that PASSED
    (no errors), then the one with more mesh objects (richer build),
    then the lowest index (stable).

    This is the offline form of best-of-N — used by the eval harness
    and demonstration capture. Pure + deterministic: no LLM, no live
    Blender, fully unit-testable.
    """
    if not candidates:
        return BestOfNResult(
            best_index=-1,
            best_report=CriticReport(summary="no candidates"),
            n=0,
        )

    reports = [
        score_tool_calls(
            c,
            require_materials=require_materials,
            require_light=require_light,
            expected_min_objects=expected_min_objects,
        )
        for c in candidates
    ]

    def _mesh_count(idx: int) -> int:
        graph = reconstruct_scene_graph(candidates[idx])
        return sum(1 for o in graph.get("objects", [])
                   if o.get("type") == "MESH")

    # Rank key: (score, passed, mesh_count) descending; index ascending.
    best_index = max(
        range(len(reports)),
        key=lambda i: (
            reports[i].score,
            1 if reports[i].passed else 0,
            _mesh_count(i),
            -i,  # lower index wins ties
        ),
    )

    scores = [r.score for r in reports]
    return BestOfNResult(
        best_index=best_index,
        best_report=reports[best_index],
        all_reports=reports,
        best_score=max(scores),
        mean_score=round(sum(scores) / len(scores), 3),
        worst_score=min(scores),
        n=len(candidates),
    )
