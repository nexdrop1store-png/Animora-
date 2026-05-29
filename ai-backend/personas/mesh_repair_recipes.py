"""
Per-persona mesh-repair recipes.

Declarative-only in Phase 5 v1: each recipe is a short description of
the deterministic bmesh fix that the artist's-eye check (Phase 5) might
suggest, mapped from the named quality check that failed. The retry
loop (Phase 5.5) will consume these by:

  1. Reading the verdict's `failed_checks` list
  2. Looking up the corresponding recipe(s) here
  3. Asking the LLM to generate a bpy script using `recipe.bmesh_pattern`
     as a starting point
  4. Re-executing and re-checking

Why declarative-only first:
  • The artist's-eye check is the load-bearing piece for Phase 5; the
    automated repair only makes sense once verdicts are trustworthy
  • Each recipe is a documentation artifact today AND a programmatic
    input to Phase 5.5 — same source of truth
  • The LLM can already invoke these patterns via execute_blender_script;
    the recipes just give Phase 5.5's auto-retry a structured hook

Blueprint reference: §6.2 (Automatic Mesh Repair).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RepairRecipe:
    """One automated fix for a specific kind of quality failure."""

    check_name: str
    """Must match the name a persona declares in its quality_checks tuple
    AND what artist's-eye returns in its `failed_checks[*].name`."""

    description: str
    """One-sentence explanation of what this fix does. Shown to the LLM
    as guidance when retry kicks in."""

    bmesh_pattern: str
    """A short bpy/bmesh snippet (NOT a complete script — just the key
    operation). The LLM is expected to integrate it with whatever else
    is needed for the specific object/context."""


# ── Per-check fix recipes ──────────────────────────────────────────────
# Indexed by check_name for O(1) lookup. Multiple recipes per check name
# (when there are alternative strategies) live in a tuple.

REPAIRS: dict[str, tuple[RepairRecipe, ...]] = {
    # ── Environment Artist checks ─────────────────────────────────────
    "horizon_treatment": (
        RepairRecipe(
            check_name="horizon_treatment",
            description="Add distant tree silhouettes and atmospheric fog to break up an empty horizon.",
            bmesh_pattern=(
                "# World volume haze for distance fade\n"
                "world = bpy.context.scene.world\n"
                "world.use_nodes = True\n"
                "nt = world.node_tree\n"
                "vol = nt.nodes.new('ShaderNodeVolumeScatter')\n"
                "vol.inputs['Density'].default_value = 0.003\n"
                "vol.inputs['Anisotropy'].default_value = 0.5\n"
                "out = nt.nodes['World Output']\n"
                "nt.links.new(vol.outputs['Volume'], out.inputs['Volume'])"
            ),
        ),
    ),
    "scatter_density": (
        RepairRecipe(
            check_name="scatter_density",
            description="Increase Geometry Nodes scatter density and add scale/rotation variation.",
            bmesh_pattern=(
                "# Bump the distribute-points density on the scatter modifier\n"
                "obj = bpy.data.objects['<scatter_emitter>']\n"
                "mod = obj.modifiers['GeometryNodes']\n"
                "# In the GN tree, increase Density on the Distribute Points node\n"
                "# and add Rotate Instances / Scale Instances if missing"
            ),
        ),
    ),
    "atmospheric_perspective": (
        RepairRecipe(
            check_name="atmospheric_perspective",
            description="Add distance-based color shift so distant geometry desaturates toward sky color.",
            bmesh_pattern=(
                "# Volumetric scatter at low density (the cheap path)\n"
                "world.use_nodes = True\n"
                "bg = world.node_tree.nodes['Background']\n"
                "vol = world.node_tree.nodes.new('ShaderNodeVolumeScatter')\n"
                "vol.inputs['Density'].default_value = 0.0015"
            ),
        ),
    ),

    # ── Hard Surface Artist checks ────────────────────────────────────
    "edge_integrity": (
        RepairRecipe(
            check_name="edge_integrity",
            description="Add a Bevel modifier with angle-based limit so silhouette edges read crisply.",
            bmesh_pattern=(
                "obj = bpy.context.active_object\n"
                "bev = obj.modifiers.new('Bevel', 'BEVEL')\n"
                "bev.limit_method = 'ANGLE'\n"
                "bev.angle_limit = 0.5236  # 30 degrees in radians\n"
                "bev.width = 0.002\n"
                "bev.segments = 2\n"
                "bev.profile = 0.5"
            ),
        ),
    ),
    "panel_seam_presence": (
        RepairRecipe(
            check_name="panel_seam_presence",
            description="Cut shallow panel seams via small Boolean cutters then bevel the resulting edges.",
            bmesh_pattern=(
                "# Create a thin cutter aligned to the panel line\n"
                "bpy.ops.mesh.primitive_cube_add(size=0.001)\n"
                "cutter = bpy.context.active_object\n"
                "cutter.name = 'PanelCutter'\n"
                "# Then on the parent: add Boolean DIFFERENCE pointing at cutter\n"
                "# and move cutter to a hidden collection."
            ),
        ),
    ),
    "edge_wear": (
        RepairRecipe(
            check_name="edge_wear",
            description="Add Pointiness-driven edge highlight to the material so corners show wear.",
            bmesh_pattern=(
                "mat = bpy.context.active_object.active_material\n"
                "nt = mat.node_tree\n"
                "geom = nt.nodes.new('ShaderNodeNewGeometry')\n"
                "ramp = nt.nodes.new('ShaderNodeValToRGB')\n"
                "# Connect geom.Pointiness -> ramp.Fac, then mix the ramp\n"
                "# output into Base Color via a MixShader"
            ),
        ),
    ),

    # ── Lighting TD checks ────────────────────────────────────────────
    "depth_separation": (
        RepairRecipe(
            check_name="depth_separation",
            description="Add a rim light from behind the subject to separate it from the background.",
            bmesh_pattern=(
                "bpy.ops.object.light_add(type='AREA', location=(0, -5, 3))\n"
                "rim = bpy.context.active_object\n"
                "rim.name = 'Rim_Light'\n"
                "rim.data.energy = 80\n"
                "rim.data.size = 1.5\n"
                "# Aim it toward the subject"
            ),
        ),
    ),
    "color_temperature_balance": (
        RepairRecipe(
            check_name="color_temperature_balance",
            description="Set key light to warm (3200K) and fill to cool (6500K) for cinematic depth.",
            bmesh_pattern=(
                "import mathutils\n"
                "# Use a Blackbody node to drive color from temperature\n"
                "key = bpy.data.lights['Key_Light']\n"
                "key.use_nodes = True\n"
                "bb = key.node_tree.nodes.new('ShaderNodeBlackbody')\n"
                "bb.inputs['Temperature'].default_value = 3200"
            ),
        ),
    ),
    "key_fill_rim_ratio": (
        RepairRecipe(
            check_name="key_fill_rim_ratio",
            description="Set key:fill:rim energy ratio to roughly 4:1:2 — the cinematography default.",
            bmesh_pattern=(
                "bpy.data.lights['Key_Light'].energy = 400\n"
                "bpy.data.lights['Fill_Light'].energy = 100\n"
                "bpy.data.lights['Rim_Light'].energy = 200"
            ),
        ),
    ),

    # ── Generalist / cross-cutting ────────────────────────────────────
    "no_default_grey": (
        RepairRecipe(
            check_name="no_default_grey",
            description="Replace default grey material with a proper Principled BSDF setup.",
            bmesh_pattern=(
                "obj = bpy.context.active_object\n"
                "mat = bpy.data.materials.new(name=f'{obj.name}_Material')\n"
                "mat.use_nodes = True\n"
                "bsdf = mat.node_tree.nodes['Principled BSDF']\n"
                "bsdf.inputs['Base Color'].default_value = (0.45, 0.42, 0.4, 1.0)\n"
                "bsdf.inputs['Roughness'].default_value = 0.45\n"
                "obj.data.materials.append(mat)"
            ),
        ),
    ),
    "topology_clean": (
        RepairRecipe(
            check_name="topology_clean",
            description="Run Merge by Distance + Recalculate Outside on the active mesh to fix common topology issues.",
            bmesh_pattern=(
                "bpy.ops.object.mode_set(mode='EDIT')\n"
                "bpy.ops.mesh.select_all(action='SELECT')\n"
                "bpy.ops.mesh.remove_doubles(threshold=0.0001)\n"
                "bpy.ops.mesh.normals_make_consistent(inside=False)\n"
                "bpy.ops.object.mode_set(mode='OBJECT')"
            ),
        ),
    ),

    # ── Character Artist checks (Sprint 2A) ───────────────────────────

    "proportion_anatomy": (
        RepairRecipe(
            check_name="proportion_anatomy",
            description=(
                "Re-scale limbs against canonical head-count proportions: "
                "8 heads heroic, 7.5 realistic, 5-6 chibi."
            ),
            bmesh_pattern=(
                "# Measure head height first, then scale body parts to match.\n"
                "import bpy\n"
                "head = bpy.data.objects.get('Head')\n"
                "if head:\n"
                "    head_h = head.dimensions.z\n"
                "    target_total_height = 8.0 * head_h  # heroic ratio\n"
                "    body = bpy.data.objects.get('Body')\n"
                "    if body:\n"
                "        body.scale.z = target_total_height / body.dimensions.z"
            ),
        ),
    ),
    "edge_flow_clean": (
        RepairRecipe(
            check_name="edge_flow_clean",
            description=(
                "Add concentric edge loops at deformation joints (shoulder, elbow, "
                "knee) and around facial features."
            ),
            bmesh_pattern=(
                "# Add 2-3 edge loops at each joint to support deformation.\n"
                "import bpy\n"
                "bpy.ops.object.mode_set(mode='EDIT')\n"
                "bpy.ops.mesh.select_all(action='DESELECT')\n"
                "# Loop cut at shoulder/elbow/knee — number_cuts=2 gives 2 new loops\n"
                "bpy.ops.mesh.loopcut_slide(\n"
                "    MESH_OT_loopcut={'number_cuts': 2, 'smoothness': 0.0,\n"
                "                      'falloff': 'INVERSE_SQUARE'},\n"
                "    TRANSFORM_OT_edge_slide={'value': 0.0},\n"
                ")\n"
                "bpy.ops.object.mode_set(mode='OBJECT')"
            ),
        ),
    ),
    "no_pinching": (
        RepairRecipe(
            check_name="no_pinching",
            description=(
                "Subdivide pinch areas + use Corrective Smooth modifier "
                "with rest-pose mesh as reference."
            ),
            bmesh_pattern=(
                "# Add Corrective Smooth to soften pinching at joints\n"
                "import bpy\n"
                "obj = bpy.context.active_object\n"
                "cs = obj.modifiers.new('CorrectiveSmooth', 'CORRECTIVE_SMOOTH')\n"
                "cs.factor = 0.5\n"
                "cs.iterations = 5\n"
                "cs.use_only_smooth = True\n"
                "cs.rest_source = 'ORCO'"
            ),
        ),
    ),
    "sculpt_density": (
        RepairRecipe(
            check_name="sculpt_density",
            description=(
                "Add a Multires modifier and subdivide to level 4-5 for detail "
                "sculpting on top of the base mesh."
            ),
            bmesh_pattern=(
                "import bpy\n"
                "obj = bpy.context.active_object\n"
                "mr = obj.modifiers.new('Multires', 'MULTIRES')\n"
                "for _ in range(5):\n"
                "    bpy.ops.object.multires_subdivide(modifier='Multires', mode='CATMULL_CLARK')"
            ),
        ),
    ),
    "articulation_ready": (
        RepairRecipe(
            check_name="articulation_ready",
            description=(
                "Apply scale + rotation, set origin to feet on ground plane, "
                "ensure A-pose with arms slightly out."
            ),
            bmesh_pattern=(
                "import bpy\n"
                "obj = bpy.context.active_object\n"
                "bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)\n"
                "# Origin to bottom of mesh (feet on Z=0)\n"
                "bbox_min_z = min(c[2] for c in obj.bound_box)\n"
                "obj.location.z -= bbox_min_z"
            ),
        ),
    ),

    # ── Composition checks (Quality Plan §4.2) ────────────────────────
    # These two recipes map to the spacing/balance + depth-cues axes the
    # PDF specifically calls out. The fixes are positional / camera /
    # GN-scatter tweaks, not bmesh edits, so the bmesh_pattern field is
    # really "fix snippet" for these — the dataclass name is historical.

    "composition_balance": (
        RepairRecipe(
            check_name="composition_balance",
            description=(
                "Move the hero to a rule-of-thirds line, offset supporting elements "
                "asymmetrically, and open up negative space."
            ),
            bmesh_pattern=(
                "# Move the hero to a rule-of-thirds point relative to the camera frame.\n"
                "import bpy\n"
                "cam = bpy.context.scene.camera\n"
                "hero = bpy.data.objects.get('<hero_name>')\n"
                "if cam and hero:\n"
                "    # Place hero ~1/3 in from camera-left along the cam's right-axis.\n"
                "    cam_right = cam.matrix_world.to_3x3() @ Vector((1, 0, 0))\n"
                "    hero.location -= cam_right * 1.5  # shift left in camera space\n"
                "# Then reposition any objects clustered at the centre to break up\n"
                "# the dense middle — use Object > Transform > Distribute Objects\n"
                "# or a Geometry Nodes Distribute Points On Faces with min-distance."
            ),
        ),
    ),
    "depth_hierarchy": (
        RepairRecipe(
            check_name="depth_hierarchy",
            description=(
                "Pull foreground objects closer (partial occlusion of midground), "
                "push background further, add volumetric haze for atmospheric depth."
            ),
            bmesh_pattern=(
                "# 1. Push background plane / horizon objects further from camera\n"
                "for name in ('<bg_objects>',):\n"
                "    obj = bpy.data.objects.get(name)\n"
                "    if obj: obj.location.y += 30  # adjust along scene depth axis\n"
                "\n"
                "# 2. Add volumetric haze for atmospheric perspective\n"
                "world = bpy.context.scene.world\n"
                "world.use_nodes = True\n"
                "nt = world.node_tree\n"
                "vol = nt.nodes.get('VolumeScatter') or nt.nodes.new('ShaderNodeVolumeScatter')\n"
                "vol.inputs['Density'].default_value = 0.002\n"
                "vol.inputs['Anisotropy'].default_value = 0.4\n"
                "out = nt.nodes['World Output']\n"
                "nt.links.new(vol.outputs['Volume'], out.inputs['Volume'])\n"
                "\n"
                "# 3. Place a foreground element that partially frames the hero —\n"
                "# overhanging branch, edge of a wall, fence post, etc."
            ),
        ),
    ),
}


def recipes_for(check_name: str) -> tuple[RepairRecipe, ...]:
    """Return the repair recipes for a failed check name. Empty tuple if
    no recipe exists yet — that's a signal we should add one when the
    user sees that failure mode in practice."""
    return REPAIRS.get(check_name, ())


def all_recipes() -> tuple[RepairRecipe, ...]:
    """Flatten all recipes for export to the eval harness."""
    out: list[RepairRecipe] = []
    for r in REPAIRS.values():
        out.extend(r)
    return tuple(out)
