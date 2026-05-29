"""
Spec-builder prompt — Quality Plan §5.1 (the SPECIFY step).

The single biggest quality lever (per the PDF and every external review of
prompt-driven 3D tools) is the quality of the SPECIFICATION the model is
given to execute against, NOT the model itself. So the orchestrator never
hands a vague user prompt like "make a beach scene" straight to the
execution loop. It first builds a structured creative brief that locks in:
subject, framing, lighting plan, palette, foreground-midground-background
plan, materials, and density — the way a senior artist mentally would
before opening Blender.

The brief lands in `accumulated_messages` as a system-role addendum that
every subsequent iteration of the agentic loop refers back to. Master
prompt rule #20 makes the brief the CONTRACT — deviation requires
explicit re-planning.

## Why Sonnet (not Haiku, not Opus)

  Haiku is too thin for the multi-modal-of-the-mind reasoning this step
  needs (composing a coherent brief, not just classifying intent). Opus
  is overkill — we're not executing a scene, just outlining one — and
  the latency would push the user's first visible response well past
  the "AI is thinking" tolerance.

  Sonnet 4.6 is the right floor: ~$0.02 per call, ~3-5s round-trip,
  visibly improved coherence over Haiku on multi-field structured
  output.

## Why JSON output

  The orchestrator + the model + the final-review step all consume
  fields by name. Free-text would force parsing or re-prompting.
  JSON-schema-shaped output also makes it trivial to detect "model
  punted on the brief" (field is empty / null / placeholder text).

## Caching

  The brief is built ONCE per user message. Retry iterations reuse it
  (same brief, no re-call). The brief is appended to
  `accumulated_messages`, which is part of the messages array, not the
  system prompt — so it doesn't invalidate the master+persona cache
  prefix. Cost = $0.02 per user message, not per iteration.
"""

from __future__ import annotations

SPEC_BUILDER_VERSION = "spec@v1"


# Bounded JSON schema — every field is required, but empty/short values
# are allowed for trivial requests (e.g. "create a cube"). The model
# decides per request whether to fill out richly or stay terse.
SPEC_SCHEMA_DOC = """The SPEC is a JSON object with exactly these top-level keys:

  subject          string — one-line focal subject ("a 1970s muscle car",
                   "a sandstone canyon at sunset", "a wooden chair").
                   Must be concrete — not "a thing" or "an object".

  framing          object {
                     camera:       string — "low three-quarter front", "eye-level wide", "top-down orthographic", etc.
                     lens_mm:      integer — 35 / 50 / 85 etc.  (use 50 if user gave no signal)
                     angle:        string — "hero shot", "documentary", "product reveal", etc.
                   }

  lighting         object {
                     time_of_day:  string — "golden hour", "noon overcast", "studio neutral", "moonlit", etc.
                     key:          string — "warm sun from camera-left, 5500K"
                     fill:         string — "cool sky bounce, 8000K, half the key intensity"
                     rim:          string — "subtle backlight separating subject from background" (or "none")
                     mood:         string — "intimate", "dramatic", "neutral product", "ominous", "joyful"
                   }

  palette          object {
                     dominant:     string — "warm sand and amber" or "#D4A66E-ish"
                     accent:       string — "deep teal water" or "#1B3A4B-ish"
                     neutral:      string — "soft grey sky / midtone"
                   }

  composition      object {
                     foreground:   string — what's in the front third of the frame
                     midground:    string — the focal layer (usually the subject sits here)
                     background:   string — horizon / sky / depth context
                     hero:         string — the single element the eye lands on first
                   }

  materials        array of objects [{
                     on:           string — which element/object the material lives on
                     type:         string — "polished metal", "wet sand", "stained oak",
                                   "translucent water with foam", etc.
                     notes:        string — roughness / scale / displacement / decals
                   }]

  density          object {
                     scattered:    string — what's scattered (rocks, grass, trees, debris)
                                   and how dense ("sparse", "moderate", "dense carpet")
                     control:      string — natural variation in rotation/scale/spacing;
                                   no grid layout unless intentional
                   }

  scale_notes      string — real-world dimensions. "Car is ~4.6m long; trees ~8m tall;
                   the whole scene reads at 12m wide from camera." Catches the floating-
                   3D / wrong-scale failure mode early.
"""


SPEC_BUILDER_PROMPT = """You are the Animora pre-production planner. Before any modeling, you build the creative brief a senior 3D artist would write in their head — the SPEC that every subsequent execution step serves.

Your output is ONE valid JSON object matching the schema below. No prose. No markdown fences. No commentary.

User's request:
{user_message}

Active specialist persona: {persona_display_name}
Persona discipline: {persona_discipline_brief}

{scene_summary_block}

THE SPEC YOU OUTPUT

{spec_schema_doc}

GUIDANCE

- The user often gives a vague hint ("make a beach"). Your job is to fill in the professional decisions the user is implicitly trusting Animora to make: time of day, lens, palette, scale.
- DO NOT add things the user didn't ask for. "Make a cube" should produce a SPEC where the SUBJECT is "a cube" — not "a 1970s steamer-trunk lid sitting on a windswept dune." The SPEC's job is to lock in *professional defaults that serve the request*, not to invent a different request.
- If the user's request is conversational ("what's a Principled BSDF?") rather than creative ("make X"), return a minimal SPEC: subject = the topic, framing/lighting/palette/composition/materials/density = all empty strings, scale_notes = "". The orchestrator will skip the execution loop on conversational intents.
- Be specific. "Lighting: bright" is useless. "Lighting: warm 5500K key from camera-left, cool 8000K sky bounce as fill, no rim" is a contract.
- Stay within ~600 output tokens. The SPEC is a reference document the execution loop consults — it must be skimmable, not exhaustive.

Respond NOW with ONLY the JSON object.
"""


def build_prompt(
    *,
    user_message: str,
    persona_display_name: str,
    persona_discipline_brief: str,
    scene_summary: str = "",
) -> str:
    """Format the spec-builder prompt for one call. Pure formatting —
    no side effects, no API call. The orchestrator owns the SDK call."""
    if scene_summary.strip():
        scene_block = (
            "Current scene (relevant context only):\n"
            f"{scene_summary}\n"
        )
    else:
        scene_block = "Current scene: (empty / starting from default)"

    return SPEC_BUILDER_PROMPT.format(
        user_message=user_message[:1200],
        persona_display_name=persona_display_name,
        persona_discipline_brief=persona_discipline_brief[:300],
        scene_summary_block=scene_block,
        spec_schema_doc=SPEC_SCHEMA_DOC,
    )


# Empty / fallback SPEC. Used when:
#   • the user intent is non-execution (question, simple_edit) — no point planning a scene
#   • the spec builder call itself failed (timeout, JSON parse error, etc.)
# The downstream loop checks `spec.get("subject")` to decide whether to
# inject the SPEC block into accumulated_messages or skip it.
EMPTY_SPEC: dict = {
    "subject": "",
    "framing": {"camera": "", "lens_mm": 0, "angle": ""},
    "lighting": {"time_of_day": "", "key": "", "fill": "", "rim": "", "mood": ""},
    "palette": {"dominant": "", "accent": "", "neutral": ""},
    "composition": {"foreground": "", "midground": "", "background": "", "hero": ""},
    "materials": [],
    "density": {"scattered": "", "control": ""},
    "scale_notes": "",
}


def render_spec_for_assistant(spec: dict) -> str:
    """Format a built SPEC as the user-role message body that gets
    appended to accumulated_messages BEFORE iteration 0. The model
    sees this as the contract for the turn.

    Format choice: deliberately not JSON. We want the model to READ
    this as English-like guidance, not parse it. JSON would invite the
    model to treat it as machine data to mirror, not creative direction
    to internalize.
    """
    if not spec.get("subject"):
        return ""

    lines: list[str] = ["[ANIMORA PRE-PRODUCTION SPEC — your contract for this turn]"]
    lines.append("")
    lines.append(f"SUBJECT: {spec.get('subject', '').strip()}")

    framing = spec.get("framing") or {}
    if any(framing.values()):
        lens = framing.get("lens_mm") or ""
        lens_str = f", {lens}mm" if lens else ""
        lines.append(
            f"FRAMING: {framing.get('camera', '').strip()}{lens_str} — "
            f"{framing.get('angle', '').strip()}"
        )

    lighting = spec.get("lighting") or {}
    if any(lighting.values()):
        lines.append(
            f"LIGHTING ({lighting.get('time_of_day', '').strip()}, "
            f"mood: {lighting.get('mood', '').strip()})"
        )
        for slot in ("key", "fill", "rim"):
            val = (lighting.get(slot) or "").strip()
            if val:
                lines.append(f"  - {slot}: {val}")

    palette = spec.get("palette") or {}
    if any(palette.values()):
        lines.append(
            f"PALETTE: {palette.get('dominant', '')} (dominant) | "
            f"{palette.get('accent', '')} (accent) | "
            f"{palette.get('neutral', '')} (neutral)"
        )

    comp = spec.get("composition") or {}
    if any(comp.values()):
        lines.append("COMPOSITION:")
        for layer in ("foreground", "midground", "background"):
            val = (comp.get(layer) or "").strip()
            if val:
                lines.append(f"  - {layer}: {val}")
        hero = (comp.get("hero") or "").strip()
        if hero:
            lines.append(f"  - hero element: {hero}")

    materials = spec.get("materials") or []
    if materials:
        lines.append("MATERIALS:")
        for m in materials[:12]:  # cap so the SPEC stays skimmable
            on = (m.get("on") or "").strip()
            mtype = (m.get("type") or "").strip()
            notes = (m.get("notes") or "").strip()
            line = f"  - {on}: {mtype}"
            if notes:
                line += f" ({notes})"
            lines.append(line)

    density = spec.get("density") or {}
    if any(density.values()):
        scat = (density.get("scattered") or "").strip()
        ctrl = (density.get("control") or "").strip()
        line = "SCATTER/DENSITY: "
        if scat:
            line += scat
        if ctrl:
            line += f" — {ctrl}"
        lines.append(line)

    scale = (spec.get("scale_notes") or "").strip()
    if scale:
        lines.append(f"SCALE: {scale}")

    lines.append("")
    lines.append(
        "Every script you emit this turn must serve this SPEC. If your "
        "result diverges from it, the artist's-eye and final-review "
        "stages will flag it. If you discover the SPEC is wrong "
        "mid-build, say so explicitly in your text reply BEFORE writing "
        "code, so the user can correct it."
    )
    return "\n".join(lines)
