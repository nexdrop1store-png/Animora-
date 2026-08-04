"""
Aspect-verifier prompts — Phase 1 of the V2 AI-quality build plan
(`docs/ROADMAP-V2-notes.md` §B2 item 2).

The original artist's-eye check (`prompts/artists_eye.py`) asks one Sonnet
call to self-report pass/fail across every persona-declared check AND give
an overall verdict in the same response. That's cheap (one call) but asks
a single judgment pass to hold 5-8 independent aesthetic questions in mind
at once — exactly the failure mode the Phase 0 baseline surfaced: a single
holistic score of "1/10" doesn't say WHICH thing is wrong (missing
geometry vs. bad lighting vs. wrong material), so nothing downstream
(retry, fix_suggestions, telemetry) can target the actual problem.

This file instead defines ONE narrowly-scoped prompt per fixed aspect,
each independently call-able so a single image gets judged 8 separate
times by 8 single-purpose "graders" who are explicitly told to ignore
everything outside their lane. Decomposed grading is well-established to
produce more reliable individual judgments than one pass trying to cover
everything (each grader has a much smaller thing to be right about).

The 8 aspects are fixed and domain-agnostic (unlike persona.quality_checks,
which vary per persona) — persona context is still passed into each
aspect's prompt so e.g. the "materials" grader knows it's judging a
hard-surface prop differently than an environment scatter, but the aspect
list itself doesn't change per persona.
"""

from __future__ import annotations

ASPECT_VERIFIERS_VERSION = "aspect_verifiers@v1"

# (aspect_id, label, rubric) — rubric is what makes each grader "single
# purpose": it tells the model exactly what to look at and, just as
# importantly, what NOT to weigh in this pass (that belongs to a different
# aspect's grader).
ASPECTS: tuple[tuple[str, str, str], ...] = (
    (
        "silhouette",
        "Silhouette",
        "Does the object/scene read clearly as what it's supposed to be from its outline "
        "alone, even ignoring color/material/lighting? A recognizable silhouette is the "
        "first thing a viewer's eye resolves. Ignore surface detail, color, and lighting "
        "quality entirely for this check — judge ONLY the outline/shape read.",
    ),
    (
        "proportion",
        "Proportion",
        "Are the relative dimensions and scale of parts believable and internally "
        "consistent (e.g. a table leg isn't thicker than the tabletop; a chair seat "
        "height is human-scale; a tree's canopy isn't smaller than its trunk radius)? "
        "Ignore surface materials and lighting — judge ONLY relative size/scale "
        "relationships between parts.",
    ),
    (
        "topology",
        "Topology / construction",
        "Does the underlying geometry look deliberately built rather than a single "
        "unmodified primitive standing in for a real form? Look for signs of actual "
        "construction: multiple distinct parts where the brief implies multiple parts, "
        "reasonable edge flow, no obviously missing pieces (e.g. a brief calling for a "
        "trunk AND a canopy where only one exists). Ignore color/lighting.",
    ),
    (
        "placement",
        "Placement",
        "Are objects positioned where the brief implies they should be, relative to each "
        "other and to any ground/floor (resting on surfaces, not floating or "
        "intersecting; correct relative positions like 'around' or 'on top of' or "
        "'facing' if the brief specifies them)? Ignore material/lighting quality — judge "
        "ONLY spatial relationships.",
    ),
    (
        "composition",
        "Composition / framing",
        "Within the given image(s), is the subject reasonably framed and readable — not "
        "so small/distant it's hard to make out, not awkwardly cropped, camera angle "
        "shows the subject's defining features? Note: an overly small subject can be "
        "either a real build-scale problem OR a rendering/camera-framing artifact — say "
        "which one it looks like if you can tell (e.g. a huge ground plane and a tiny "
        "correct-looking object suggests framing, not build quality).",
    ),
    (
        "materials",
        "Materials",
        "Do applied materials match what the brief/persona calls for (right base color "
        "family, appropriate roughness/metallic for the described material — e.g. glossy "
        "metal isn't matte, rough wood isn't shiny, glass is transparent) and are they "
        "actually visibly applied (not default gray)? Ignore geometry and lighting setup.",
    ),
    (
        "lighting",
        "Lighting",
        "Is the scene lit with intentional key/fill/rim structure (or whatever the brief "
        "specifically calls for — e.g. 'warm sunset', 'hard spotlight shadows') rather "
        "than flat/single-source/default lighting? Is exposure reasonable — not blown "
        "out to white, not crushed to black? Ignore geometry and material color choices.",
    ),
    (
        "technical",
        "Technical correctness",
        "Are there any technical defects visible: obviously missing geometry (holes where "
        "something should be), z-fighting/flickering overlap, inverted normals (visibly "
        "black/wrong-shaded faces), objects clipping through each other in a way that "
        "looks like an error rather than an intentional overlap? This is a defect-spotting "
        "pass, not an aesthetic one.",
    ),
)

ASPECT_ID_TO_LABEL = {aspect_id: label for aspect_id, label, _ in ASPECTS}


# {aspect_label} / {aspect_rubric} — this aspect's identity and rubric, the
# only thing this call should judge.
# {user_intent} / {persona_display_name} / {persona_quality_checks} /
# {scene_diff_summary} / {execution_outcome} — same context fields
# artists_eye.py's single-call prompt uses, passed through unchanged so
# persona-specific nuance (e.g. "Environment Artist" checks) still reaches
# each aspect grader that can use it.
ASPECT_VERIFIER_PROMPT = """You are a specialist grader on a 3D art review panel. Your ONLY job on this pass: {aspect_label}.

{aspect_rubric}

Do NOT comment on or let your verdict be influenced by anything outside that description above — other graders on this panel cover every other angle (there are 8 of you total, each single-purpose). If the thing you're judging looks fine BUT something else about the image is bad, that's not your problem this pass — stay in your lane.

CONTEXT (for reference only — judge strictly the {aspect_label} aspect)

User's request: {user_intent}
Active specialist: {persona_display_name}
Persona's declared quality checks (may or may not be relevant to your specific aspect): {persona_quality_checks}
What just changed in the scene: {scene_diff_summary}
Execution outcome: {execution_outcome}

If execution_outcome is an error (not "OK"), and your aspect cannot be meaningfully judged because of it, verdict = "n/a".

OUTPUT (strict JSON, no markdown fences, no commentary outside the object):

{{
  "verdict": "pass|fail|n/a",
  "reason": "<one sentence — what you saw, specific to {aspect_label} only>",
  "fix_suggestion": "<one actionable instruction to fix THIS aspect specifically, or empty string if verdict is pass/n/a>",
  "confidence": <0.0-1.0 — how sure are you of this verdict>
}}

Respond NOW with ONLY the JSON object.
"""
