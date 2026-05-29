"""
Composition rules — Quality Plan §5.2.

A short, stable system-prompt fragment attached to every execution turn
AFTER the persona extension and BEFORE the live scene context. Pulls
the seven composition rules out of individual persona prompts into a
single shared section so every persona inherits them uniformly.

## Why shared?

Today these rules are scattered across the per-persona files (rule of
thirds mentioned in environment_artist; hierarchy in master_prompt's
naming convention; grounding implicit in hard_surface_artist; etc.).
A shared module:

  • Guarantees a model on the generalist persona still gets the same
    composition discipline as one on the environment artist.
  • Makes the rules versionable / iteratable as a unit — when we tune
    "rule of thirds" guidance based on eval results, we touch one
    file, not every persona.
  • Is short enough (~600 tokens) to sit in the cached prefix without
    bloating it.

## Why these specific rules?

Exactly the seven from PDF §5.2. Don't add or invent. Each one maps to
a specific failure mode the per-step artist's-eye check and the
whole-scene FINAL REVIEW both look for.

## Cache discipline

Lives BEFORE the {scene_context} split → caches with the master+persona
prefix. Goes into Anthropic's 5-min ephemeral cache; subsequent turns
in the same session hit cache for free. The text is deliberately
identical-on-every-call (no template substitution) so cache hash
matches.
"""

from __future__ import annotations

COMPOSITION_RULES_VERSION = "composition@v1"


COMPOSITION_RULES = """
COMPOSITION RULES (apply to every scene you build — these are not optional)

1. FOCAL HIERARCHY. Every scene has ONE hero element that draws the eye first; supporting elements are clearly subordinate in size, contrast, or position. If three elements compete for attention equally, the scene reads as cluttered — pick one to dominate.

2. RULE OF THIRDS. Place the hero element at or near a one-third or two-thirds point of the frame, not dead center, unless the scene specifically calls for symmetry (formal portrait, ceremonial subject, abstract balance). Horizons land on the lower or upper third, not the middle.

3. INTENTIONAL NEGATIVE SPACE. The frame is NOT to be filled corner-to-corner. Leave room around the subject — the eye needs somewhere to rest. Crammed compositions look amateur even when every element is technically well-modeled.

4. BELIEVABLE GROUNDING. Every object touches a surface with correct contact. Use snap-to-surface logic (or explicit z-coordinate calculation from the surface mesh) so objects sit on the floor / table / ground at the right elevation. Floating-by-a-millimeter looks worse than floating-by-a-meter — both break the illusion. Wheels touch road; furniture legs touch floor; props rest on surfaces with their bounding-box bottom at the surface's z-height.

5. NATURAL VARIATION. Scattered elements (trees, rocks, debris, props in a cluster) use controlled randomness: rotation jitter (±15-45° around vertical), scale jitter (0.7-1.3x), spacing jitter via random offset or Poisson distribution — NEVER a rigid grid unless the scene specifically calls for it (an army formation, a cemetery, a planned orchard). When using Geometry Nodes scatter, set random_rotation, random_scale, and seed appropriately.

6. DEPTH CUES. Foreground, midground, and background are DELIBERATELY separated in the composition. Foreground elements partially occlude the midground; midground partially occludes background. Atmospheric haze (volumetric fog, distance-based desaturation, sky color in distant geometry) breaks up the depth — without it, scenes flatten into a single plane. For environments, the foreground also frames the shot (overhanging branch, edge of a wall, fence post).

7. CAMERA-AWARE LAYOUT. Arrange elements to look right FROM THE ACTUAL CAMERA, not from the default top-down or perspective view. If you're placing a scene for a low three-quarter camera, the hero element needs to read from that angle — not be hidden behind something that's only between the hero and the camera. Set the camera FIRST, then compose; or if the user hasn't set a camera, compose AS IF the camera were at eye-level facing the natural focal point.

When the artist's-eye check or FINAL REVIEW flags a "composition" failure, it is checking against ONE of these seven rules. The fix is to apply that specific rule — not to rebuild the whole scene.
"""
