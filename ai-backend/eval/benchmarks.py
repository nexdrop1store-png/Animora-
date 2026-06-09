"""
Benchmark prompts used by the eval harness.

Each benchmark is a self-contained spec describing:
  • prompt          — the literal user message we'd send through the panel
  • expected_intent — which intent the classifier SHOULD return (or any
                      member of the allowed set). None = don't check.
  • required_ops    — bpy operators / API calls the script MUST contain
                      to count as a correct response (regex patterns)
  • forbidden_ops   — operators that SHOULD NOT appear (e.g. a request
                      for a cube must not contain primitive_uv_sphere_add)
  • required_named  — at least one assignment of `<obj>.name = "<...>"`
                      that names the asset meaningfully
  • require_material — must set up at least one Principled BSDF material
                       (test for shader work). True/False.
  • budget_tokens   — approximate output-token ceiling we want the model
                      to fit within. Going over is a soft warning, not a
                      hard fail (the model can still be correct just verbose).

The benchmarks are intentionally biased toward the failure modes we've
seen in production: primitive substitution (cube→sphere, cuboid→ovoid),
truncation on hero vehicles, missing materials, generic naming.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Benchmark:
    name: str
    prompt: str
    expected_intent: frozenset[str] | None = None
    required_ops: tuple[str, ...] = ()
    forbidden_ops: tuple[str, ...] = ()
    required_named: bool = True
    require_material: bool = False
    budget_tokens: int = 4000
    notes: str = ""

    # ── Sprint 2D — aesthetic / composition signals ────────────────────
    # All optional. When 0/False the corresponding scoring axis is
    # skipped, so existing benchmarks keep behaving exactly as before.
    # New composition benchmarks opt in by setting these explicitly.

    min_distinct_objects: int = 0
    """Composition floor for object count. e.g. 3 for a still-life
    triangular composition. 0 = skip the check."""

    min_distinct_positions: int = 0
    """Composition floor for placement variety. Catches the
    "everything at origin" regression. 0 = skip."""

    min_light_sources: int = 0
    """For lighting benchmarks. e.g. 3 for three-point setups. 0 = skip."""

    require_material_variety: bool = False
    """If True, the script must create >= 2 distinct materials. Catches
    the single-grey-on-everything failure."""

    require_modifiers: bool = False
    """If True, the script must add at least one modifier. Raw-primitive
    output rarely meets the quality bar."""


# Global forbidden patterns applied to EVERY benchmark in addition to that
# benchmark's own `forbidden_ops`. Catches deprecated Blender API usage —
# attributes/inputs removed in 4.1 / renamed in 4.0 that would AttributeError
# at runtime on the user's Blender 5.x install.
#
# Regression history: 2026-05-21 — Opus 4.7 wrote `mesh.use_auto_smooth = True`
# inside an otherwise-correct Lamborghini Urus script. Script ran halfway,
# AttributeError'd on the smooth-shading line, user saw "Script failed".
# Master prompt rule #12 + persona update should prevent this; the
# benchmark below verifies it across every prompt.
GLOBAL_FORBIDDEN_OPS: tuple[str, ...] = (
    # Pre-4.1 Mesh attributes (removed)
    r"\.use_auto_smooth\s*=",
    r"\.auto_smooth_angle\s*=",
    # Operator that never existed at this exact name
    r"shade_smooth_by_angle\s*\(",
    # Pre-4.0 Principled BSDF input names (renamed)
    r"inputs\s*\[\s*[\"']Specular[\"']\s*\]",
    r"inputs\s*\[\s*[\"']Subsurface[\"']\s*\]",
    r"inputs\s*\[\s*[\"']Sheen[\"']\s*\]",
    r"inputs\s*\[\s*[\"']Clearcoat[\"']\s*\]",
    r"inputs\s*\[\s*[\"']Clearcoat Roughness[\"']\s*\]",
    r"inputs\s*\[\s*[\"']Transmission[\"']\s*\]",
)


# Note: regex patterns below are matched with re.search, so they don't need
# to be anchored. `\(` is escaped inside raw strings because we're matching
# literal call syntax in the generated Python.
BENCHMARKS: tuple[Benchmark, ...] = (
    Benchmark(
        name="primitive.cube",
        prompt="create a cube",
        expected_intent=frozenset({"hard_surface_model", "simple_edit"}),
        required_ops=(r"primitive_cube_add\(",),
        forbidden_ops=(
            r"primitive_uv_sphere_add\(",
            r"primitive_ico_sphere_add\(",
            r"primitive_cylinder_add\(",
        ),
        # Calibrated (2026-06): a literal single-primitive request may keep
        # the primitive's common name — this benchmark tests primitive
        # SUBSTITUTION + dimensions, not naming. The "no Blender default
        # names" discipline targets multi-part builds (a sofa's parts must
        # be Seat/Armrest, not Cube.001/Cube.002); those keep
        # required_named=True and pass. Naming a requested cube "Cube" is
        # accurate, not throwaway.
        required_named=False,
        require_material=False,
        budget_tokens=1500,
        notes="Regression: model used to substitute sphere.",
    ),
    Benchmark(
        name="primitive.cuboid",
        prompt="create a cuboid 2m wide, 1m tall, 0.5m deep, at origin",
        expected_intent=frozenset({"hard_surface_model", "simple_edit"}),
        required_ops=(r"primitive_cube_add\(",),
        forbidden_ops=(
            r"primitive_uv_sphere_add\(",
            r"primitive_ico_sphere_add\(",
        ),
        required_named=False,  # calibrated — see primitive.cube
        require_material=False,
        budget_tokens=1500,
        notes="Regression: Sonnet turned cuboid into ovoid. Sized box.",
    ),
    Benchmark(
        name="primitive.sphere",
        prompt="add a UV sphere of radius 0.5 m at the origin",
        expected_intent=frozenset({"hard_surface_model", "simple_edit"}),
        required_ops=(r"primitive_uv_sphere_add\(",),
        forbidden_ops=(r"primitive_cube_add\(", r"primitive_ico_sphere_add\("),
        required_named=False,  # calibrated — see primitive.cube
        require_material=False,
        budget_tokens=1500,
    ),
    Benchmark(
        name="primitive.cylinder",
        prompt="add a cylinder with radius 0.3m and height 1m",
        expected_intent=frozenset({"hard_surface_model", "simple_edit"}),
        required_ops=(r"primitive_cylinder_add\(",),
        forbidden_ops=(r"primitive_cone_add\(", r"primitive_cube_add\("),
        required_named=False,  # calibrated — see primitive.cube
        budget_tokens=1500,
    ),
    Benchmark(
        name="furniture.chair.low_poly",
        prompt="Add a low-poly chair",
        expected_intent=frozenset({"hard_surface_model"}),
        required_ops=(
            # Either box-modelled chair or primitive composite is fine — at
            # minimum it must add SOMETHING from the primitive family.
            r"primitive_(cube|cylinder|cone)_add\(",
        ),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        budget_tokens=4000,
    ),
    Benchmark(
        name="vehicle.car.basic",
        prompt="Make a car",
        expected_intent=frozenset({"hard_surface_model"}),
        required_ops=(
            r"primitive_(cube|cylinder|torus)_add\(",  # body + wheels primitives
            r"modifiers\.new\(",                        # has at least one modifier
            r"materials\.new\(|materials\.append\(",   # at least one material
        ),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        budget_tokens=9000,
        notes="Regression: previously truncated at 4k, then 16k. Should fit 9k after persona update.",
    ),
    Benchmark(
        name="vehicle.car.lambo_urus",
        prompt="Build me a hero Lamborghini Urus, studio-shot quality",
        expected_intent=frozenset({"hard_surface_model"}),
        required_ops=(
            r"primitive_(cube|cylinder|torus)_add\(",
            r"modifiers\.new\(",
            r"materials\.new\(|materials\.append\(",
            # Studio-lit signal: accept ANY valid path the model takes.
            # Sprint 2 follow-up: the original benchmark required discrete
            # lights only (light_add / light.energy=), but the Sprint 1
            # SPEC step drives modern Opus to set up HDRI environment
            # lighting instead (legitimate for product viz). Accept all
            # three patterns: discrete light op, direct light datablock,
            # or HDRI environment-texture world setup.
            (
                r"light_add\(|light\.energy\s*=|"
                r"bpy\.data\.lights\.new\(|"
                r"ShaderNodeTexEnvironment|EnvironmentTexture"
            ),
        ),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        budget_tokens=14000,
        notes=(
            "Hero asset stress test. Failed at 16k cap, retesting at 32k. "
            "Sprint 2 follow-up: lighting regex broadened to accept HDRI "
            "(ShaderNodeTexEnvironment) since the SPEC step drives that path "
            "for studio product viz."
        ),
    ),
    Benchmark(
        name="character.dragon",
        prompt="model a low-poly dragon",
        expected_intent=frozenset({"character_sculpt", "hard_surface_model"}),
        required_ops=(),  # creative latitude — just don't crash
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        budget_tokens=8000,
    ),
    Benchmark(
        name="scene.beach",
        prompt="make a beach scene with palm trees and water",
        expected_intent=frozenset({"dense_scene"}),
        required_ops=(
            r"primitive_plane_add\(",
            r"materials\.new\(|materials\.append\(",
        ),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        budget_tokens=12000,
    ),
    Benchmark(
        name="lighting.studio",
        prompt="light this scene like a studio product shot",
        expected_intent=frozenset({"lighting_setup"}),
        # Sprint 3 follow-up: studio product lighting via HDRI (use_asset
        # → load_asset of a studio HDRI) is a legitimate professional
        # path. Accept it as fulfilling the lighting requirement, same
        # broadening that fixed vehicle.car.lambo_urus.
        required_ops=(
            (
                r"light_add\(|lights\.new\(|"
                r"use_asset\(|load_asset\(|"
                r"ShaderNodeTexEnvironment|EnvironmentTexture"
            ),
        ),
        forbidden_ops=(),
        required_named=False,
        require_material=False,
        budget_tokens=3500,
    ),
    Benchmark(
        name="question.bsdf",
        prompt="what is a Principled BSDF node and when should I use it?",
        expected_intent=frozenset({"question"}),
        required_ops=(),
        forbidden_ops=(
            # The model should NOT call execute_blender_script for a pure
            # question — but we can't enforce that here at script-content
            # level. Leave required_ops empty; scoring will record whether
            # a script was generated at all.
        ),
        required_named=False,
        require_material=False,
        budget_tokens=800,
    ),
    Benchmark(
        name="primitive.sphere.creative_safety_net",
        prompt="add something interesting to the scene",
        expected_intent=frozenset({"hard_surface_model", "unknown", "dense_scene"}),
        required_ops=(),  # latitude
        forbidden_ops=(),
        required_named=True,
        require_material=False,
        budget_tokens=4000,
        notes="Tests creative-verb override path when intent ambiguous.",
    ),

    # ── Sprint 2C — composition + character benchmarks ────────────────
    # Each opts into the new aesthetic-signal fields the Sprint 2D scorer
    # consumes. Together with the existing 12, total is 18 benchmarks.

    Benchmark(
        name="composition.still_life",
        prompt="create a still life with three objects in a triangular composition on a table",
        expected_intent=frozenset({"hard_surface_model", "dense_scene"}),
        required_ops=(),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        budget_tokens=5000,
        min_distinct_objects=4,         # table + 3 objects
        min_distinct_positions=3,       # at least 3 distinct positions (the triangle)
        require_material_variety=True,  # not single-grey on everything
        notes="Composition: triangular focal arrangement. Catches single-object-at-origin failure.",
    ),
    Benchmark(
        name="composition.depth_layering",
        prompt="build an outdoor scene with a tree in the foreground, a house in the midground, and mountains in the background",
        expected_intent=frozenset({"dense_scene", "terrain_landscape"}),
        required_ops=(),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        budget_tokens=8000,
        min_distinct_objects=3,
        min_distinct_positions=3,        # foreground/midground/background at different positions
        require_material_variety=True,
        notes="Composition: explicit depth layering. Catches everything-at-origin failure.",
    ),
    Benchmark(
        name="composition.three_point_lighting",
        prompt="set up a three-point lighting rig for a portrait — key, fill, and rim lights",
        expected_intent=frozenset({"lighting_setup"}),
        required_ops=(r"bpy\.data\.lights\.new",),
        forbidden_ops=(),
        required_named=True,
        require_material=False,
        budget_tokens=3500,
        min_light_sources=3,             # the entire point of the benchmark
        min_distinct_positions=3,        # 3 distinct light placements
        notes="Lighting: three-point setup. min_light_sources=3 is the hard floor.",
    ),
    Benchmark(
        name="composition.rule_of_thirds",
        prompt="create a landscape scene with the horizon on the lower third of the frame and a focal subject at a rule-of-thirds intersection",
        expected_intent=frozenset({"dense_scene", "terrain_landscape"}),
        required_ops=(),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        budget_tokens=6000,
        min_distinct_objects=2,
        min_distinct_positions=2,
        require_material_variety=True,
        notes="Composition: rule-of-thirds placement. Verifies non-centered framing.",
    ),
    Benchmark(
        name="composition.grounded_furniture",
        prompt="create an interior room with a desk, a chair, and a lamp — every object grounded on the floor (no floating)",
        expected_intent=frozenset({"hard_surface_model", "architecture"}),
        required_ops=(),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        budget_tokens=7000,
        min_distinct_objects=4,         # room + desk + chair + lamp
        min_distinct_positions=4,        # each grounded at distinct floor position
        require_material_variety=True,
        require_modifiers=True,           # bevels / subsurf on furniture
        notes="Grounding: objects sit on floor at z=0, no floating. Modifiers expected.",
    ),
    Benchmark(
        name="character.standing_figure",
        prompt="model a stylized human standing in T-pose with proper proportions",
        expected_intent=frozenset({"character_sculpt"}),
        required_ops=(),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        budget_tokens=8000,
        min_distinct_objects=1,           # the figure itself (body is one mesh; limbs can be modifier-driven)
        require_modifiers=True,           # Mirror + SubSurf + Skin or Multires expected
        notes="Character persona benchmark. Modifier stack is the proxy for sculpt-ready topology.",
    ),

    # ── Sprint 3D — asset-first benchmarks ────────────────────────────
    # Each requires the model to CALL use_asset rather than building the
    # equivalent from primitives. The required_ops regex matches the
    # tool-use call signature in the captured script-or-text output.
    # The benchmark.expected_intent + the SPEC-driven asset suggestion
    # block in context_builder are what nudge the model toward use_asset.

    Benchmark(
        name="asset.hdri_lighting",
        prompt="set up the scene with a warm golden-hour outdoor HDRI for lighting",
        expected_intent=frozenset({"lighting_setup", "material_authoring"}),
        # required_ops accepts EITHER:
        #   - the use_asset call signature in the captured assistant text
        #   - HDRI environment-texture node fallback (the model still met
        #     the intent if it used a hand-rolled equivalent)
        required_ops=(
            r"use_asset\(|load_asset\(|ShaderNodeTexEnvironment|EnvironmentTexture",
        ),
        forbidden_ops=(),
        required_named=False,  # the asset call itself doesn't create user-visible objects
        require_material=False,
        budget_tokens=3500,
        notes="Asset-first: prefers use_asset for HDRI; accepts hand-rolled env-texture as fallback.",
    ),
    Benchmark(
        name="asset.texture_application",
        prompt="create a square table and apply a realistic weathered wood texture to it",
        expected_intent=frozenset({"hard_surface_model", "material_authoring"}),
        required_ops=(
            r"primitive_(cube|cylinder)_add\(",  # table base
            # Either use_asset for the texture OR a hand-built wood material
            r"use_asset\(|load_asset\(|ShaderNodeBsdfPrincipled|Principled BSDF",
        ),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        budget_tokens=5000,
        notes="Asset-first: prefers use_asset for the wood texture; accepts hand-built PBR fallback.",
    ),
    Benchmark(
        name="asset.reference_mesh",
        prompt="add a modern chair to the scene as a placeholder furniture piece",
        expected_intent=frozenset({"hard_surface_model"}),
        # use_asset for the chair mesh — OR primitives if the model hand-built one
        required_ops=(
            r"use_asset\(|load_asset\(|primitive_(cube|cylinder|torus)_add\(",
        ),
        forbidden_ops=(),
        # Asset-first flips the naming logic: when use_asset loads a
        # vetted mesh, the .blend already contains a named object — no
        # need to require Python-level naming. The benchmark passes
        # whenever ANY chair shows up in the scene, by tool or by hand.
        required_named=False,
        require_material=False,  # PolyHaven meshes ship with their own materials
        budget_tokens=4000,
        notes="Asset-first: prefers use_asset for chair mesh; accepts hand-built primitive chair as fallback.",
    ),

    # ── Sprint 4I — Hero furniture + hero scene benchmarks ──────────────
    # These exist to catch the "stops after 2 atomic calls" regression
    # the cofounder reported on master@v16: "build a wooden chair" →
    # one cube named Chair_Seat + a bevel modifier, then "Build
    # complete". master@v17 introduces iteration discipline (Rule #4
    # rewrite) + hero worked examples (chair, beach, sofa, lamp).
    # These benchmarks should FAIL on master@v16 and PASS on master@v17.
    # Threshold floors are intentionally below the v17 example's call
    # count so a slightly-shorter-but-still-acceptable build counts.
    Benchmark(
        name="furniture.chair.wooden_hero",
        prompt="Build a wooden chair",
        expected_intent=frozenset({"hard_surface_model"}),
        # Either atomic path (create_primitive in the captured trace) or
        # code-fallback (bpy.ops.mesh.primitive_*_add) is accepted; the
        # eval runner captures both. Required: at least one primitive
        # family call + a material apply.
        required_ops=(
            r"primitive_(cube|cylinder)_add\(|create_primitive",
            r"materials\.(new|append)\(|apply_material",
        ),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        min_distinct_objects=8,   # seat + 4 legs + back + ≥2 detail parts
        require_modifiers=True,    # bevel or subdivision_surface on at least one part
        budget_tokens=8000,
        notes=(
            "Hero chair: must produce ≥8 named parts (seat, ≥4 legs, "
            "backrest, ≥2 detail elements). Single-cube + bevel fails."
        ),
    ),
    Benchmark(
        name="furniture.coffee_table.hero",
        prompt="Build a wooden coffee table",
        expected_intent=frozenset({"hard_surface_model"}),
        required_ops=(
            r"primitive_(cube|cylinder)_add\(|create_primitive",
            r"materials\.(new|append)\(|apply_material",
        ),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        min_distinct_objects=5,    # top + 4 legs minimum
        require_modifiers=True,
        budget_tokens=6000,
        notes="Coffee table: top + 4 legs + bevel modifier + wood material.",
    ),
    Benchmark(
        name="furniture.sofa.modern",
        prompt="Build a modern three-seat sofa",
        expected_intent=frozenset({"hard_surface_model"}),
        required_ops=(
            r"primitive_cube_add\(|create_primitive",
            r"materials\.(new|append)\(|apply_material",
        ),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        min_distinct_objects=8,    # base + back + 2 arms + 3 cushions + ≥1 detail
        require_material_variety=True,  # fabric + chrome (feet) is the v17 example
        budget_tokens=8000,
        notes="Sofa: frame + arms + cushions, ≥2 distinct materials (fabric + chrome).",
    ),
    Benchmark(
        name="furniture.floor_lamp",
        prompt="Build a floor lamp with a bulb",
        expected_intent=frozenset({"hard_surface_model", "lighting_setup"}),
        required_ops=(
            r"primitive_(cylinder|cone)_add\(|create_primitive",
            r"light_add\(|lights\.new\(|create_light",
        ),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        min_distinct_objects=3,    # base + pole + shade
        min_light_sources=1,        # the bulb
        budget_tokens=4500,
        notes="Floor lamp: base + pole + shade as primitives + at least 1 light source.",
    ),
    Benchmark(
        name="furniture.bookshelf",
        prompt="Build a wooden bookshelf",
        expected_intent=frozenset({"hard_surface_model"}),
        required_ops=(
            r"primitive_cube_add\(|create_primitive",
            r"materials\.(new|append)\(|apply_material",
            # Either Array modifier OR explicit shelf primitives — both
            # produce the multi-shelf look. The model's choice.
            r"ARRAY|primitive_cube_add\(",
        ),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        min_distinct_objects=6,    # frame + ≥3 shelves + sides
        budget_tokens=6000,
        notes=(
            "Bookshelf: vertical frame + ≥3 horizontal shelves (Array or "
            "explicit) + wood material."
        ),
    ),
    Benchmark(
        name="scene.warm_evening_beach",
        prompt="Build a warm evening beach",
        expected_intent=frozenset({"dense_scene"}),
        required_ops=(
            r"primitive_plane_add\(|create_primitive",
            r"materials\.(new|append)\(|apply_material",
            # Warm-evening lighting signal: a Sun light OR a warm world
            # color OR an HDRI environment — any of these counts.
            (
                r"light_add\(|lights\.new\(|create_light|"
                r"world\.|set_world|"
                r"ShaderNodeTexEnvironment|EnvironmentTexture"
            ),
        ),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        min_distinct_objects=8,    # sand + water + ≥3 palms (trunk+fronds) + camera
        min_light_sources=1,        # at least the sun
        require_material_variety=True,  # sand vs water vs vegetation
        budget_tokens=14000,
        notes=(
            "Hero beach: sand + ocean + ≥3 palms + at least 1 light + "
            "≥2 distinct materials. Two grey planes fails."
        ),
    ),
    Benchmark(
        name="scene.cozy_living_room",
        prompt="Build a cozy living room",
        expected_intent=frozenset({"dense_scene", "hard_surface_model"}),
        required_ops=(
            r"primitive_(cube|cylinder|plane)_add\(|create_primitive",
            r"materials\.(new|append)\(|apply_material",
        ),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        min_distinct_objects=10,   # walls + floor + sofa + table + lamp + ≥2 props
        min_light_sources=2,        # warm key + lamp practical
        require_material_variety=True,
        budget_tokens=16000,
        notes=(
            "Hero room: floor + ≥1 wall + sofa or chair + table + lamp "
            "+ ≥2 lights + ≥3 distinct materials. A single grey plane "
            "fails."
        ),
    ),

    # ── Sprint 1 Deep — Style-adjective benchmarks ──────────────────────
    # These test that the model READ the user's style adjective(s) and
    # applied the STYLE-ADJECTIVE LEXICON from master prompt v18. A
    # generic "sideboard with wood material" passes the wooden chair
    # benchmark above but should FAIL these — the prompts demand a
    # specific style. Expected to fail on master@v17 and pass on @v18.
    Benchmark(
        name="furniture.sideboard.luxury_vintage",
        prompt="Build a luxury vintage wooden sideboard",
        expected_intent=frozenset({"hard_surface_model"}),
        required_ops=(
            r"primitive_(cube|cylinder|torus)_add\(|create_primitive",
            r"materials\.(new|append)\(|apply_material",
            # luxury signature: at least one bevel modifier (ornate detail)
            r"BEVEL|kind=[\"']bevel[\"']",
        ),
        forbidden_ops=(),
        required_named=True,
        require_material=True,
        require_material_variety=True,  # walnut + brass = 2 slots minimum
        require_modifiers=True,          # bevel on cabinet/edges
        min_distinct_objects=10,         # cabinet + 4 legs + 3 doors + 3 handles minimum
        budget_tokens=10000,
        notes=(
            "Luxury vintage signature: distressed walnut PRIMARY + brass "
            "ACCENT hardware + ornate bevels (width 0.008-0.012). The "
            "model must hit ≥2 distinct materials AND ≥1 bevel modifier. "
            "Generic 'single material cube' fails the variety check."
        ),
    ),
    Benchmark(
        name="furniture.chair.modern_minimalist",
        prompt="Build a modern minimalist chair",
        expected_intent=frozenset({"hard_surface_model"}),
        required_ops=(
            r"primitive_(cube|cylinder)_add\(|create_primitive",
            r"materials\.(new|append)\(|apply_material",
        ),
        forbidden_ops=(
            # minimalist forbids ornate bevels. width > 0.005 = ornate.
            # This catches "model interpreted minimalist as luxury".
            r"width=0\.0(08|09|1[0-9])",
            # No metallic accents on a minimalist chair (rejects luxury
            # interpretation that would add brass/chrome).
            r"metallic\s*=\s*1\.0|metallic=1\.0",
        ),
        required_named=True,
        require_material=True,
        # Minimalist intentionally uses ONE shared material slot. Don't
        # require variety — that would penalise correct minimalist output.
        require_material_variety=False,
        min_distinct_objects=4,          # seat + ≥3 legs minimum (backless stool OK)
        budget_tokens=5000,
        notes=(
            "Modern minimalist signature: single matte material slot "
            "reused across all parts, no ornament, micro-chamfer only "
            "(bevel width ≤ 0.005). The forbidden_ops catch "
            "mis-interpretations that add luxury ornament or chrome."
        ),
    ),
    Benchmark(
        name="furniture.shelf.industrial",
        prompt="Build an industrial metal shelf",
        expected_intent=frozenset({"hard_surface_model"}),
        required_ops=(
            r"primitive_cube_add\(|create_primitive",
            r"materials\.(new|append)\(|apply_material",
            # Industrial signature: must include a metallic material.
            r"metallic\s*=\s*1\.0|metallic=1\.0",
        ),
        forbidden_ops=(
            # Industrial banishes warm wood tones. The audit's vintage
            # walnut [0.20, 0.10, 0.05] and oak [0.30, 0.18, 0.10] would
            # trigger these. Note: 0.30 R is borderline; catch obvious
            # warm-brown only.
            r"base_color=\[0\.[2-5][0-9]?\s*,\s*0\.1[0-9]?\s*,\s*0\.0",
        ),
        required_named=True,
        require_material=True,
        min_distinct_objects=5,         # back + ≥3 shelves + side support OR bolts
        budget_tokens=6500,
        notes=(
            "Industrial signature: raw steel (metallic=1.0) primary + "
            "exposed structure (visible bolts as small cubes/cylinders) "
            "+ no warm wood tones. The forbidden_ops catch the "
            "mis-interpretation 'industrial' → 'rustic wood'."
        ),
    ),
)
