"""
Persona loader — maps an intent classification to a system-prompt extension.

The master prompt (prompts/master_prompt.py) carries the absolute rules
and quality standards. Each persona prompt layers domain-specific
expertise ON TOP. Per docs/AI_ARCHITECTURE.md §5, the layered prompt is:

  Layer 1: master_prompt        — identity, 7 absolute rules, quality bar
  Layer 2: Anthropic tool defs  — passed via `tools=`
  Layer 3: persona.extension    — this module's output
  Layer 4: session memory       — Phase 7
  Layer 5: live scene context   — substituted into {scene_context}

For Phase 4 alpha we ship 3 deep personas + a generalist fallback:
  • Environment Artist   — dense scenes, scatter, terrain, atmosphere
  • Hard Surface Artist  — weapons, vehicles, mechanical props
  • Lighting TD          — lighting setup, mood, render config
  • Generalist           — Q&A, simple edits, unknown intents

Other 6 personas (Character, Animator, VFX, Game Dev, Compositor) ship
in a later round per the roadmap §12.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # Persona modules import this; keep cycle-free

log = logging.getLogger("animora.personas")


@dataclass(frozen=True)
class Persona:
    """A loadable system-prompt extension with all its operational metadata."""

    id: str
    """Stable identifier used in logs and as a cache key."""

    display_name: str
    """Human-readable name, e.g. 'Environment Artist'. Currently invisible
    to end users (per the UX decision in §5.6) but available for opt-in
    display via the 'show persona' setting."""

    extension: str
    """The persona-specific system prompt text. Concatenated after the
    base persona prompt (personas/base.py BASE_EXTENSION) and inserted
    just before CURRENT SCENE in the master prompt. Should be 2-4k tokens
    of dense specialist knowledge; very small extensions (<300 tokens)
    suggest the persona isn't doing its job."""

    default_model_hint: str = "auto"
    """One of 'haiku' | 'sonnet' | 'opus' | 'auto'. 'auto' lets the
    router decide based on user plan + task complexity. Personas for
    inherently complex work (rigging, multi-stage simulations) can
    declare 'opus' here to bias the router."""

    quality_checks: tuple[str, ...] = field(default_factory=tuple)
    """Names of post-execution checks the artist's-eye stage should
    apply for this persona's output. Consumed by Phase 5; declared
    here so Phase 4 can ship the persona library independently."""

    knowledge_sections: tuple[str, ...] = field(default_factory=tuple)
    """Names of reusable knowledge modules (future) appended to the
    extension. Empty in Phase 4 — the persona extensions inline their
    own knowledge. Phase 7 may extract common sections (PBR primer,
    geometry-nodes patterns, etc.) into shared modules."""


# ── Lazy registry ───────────────────────────────────────────────────────
# We populate this on first access from the persona modules. Importing
# them eagerly at module-load time would create a circular dependency
# with streaming.py (which imports this module).

_REGISTRY: dict[str, Persona] = {}


def _ensure_registry_loaded() -> None:
    if _REGISTRY:
        return
    # Late imports — each persona module exports `PERSONA = Persona(...)`
    from ..personas import (
        base as _base,          # noqa: F401 — referenced via base.BASE_EXTENSION elsewhere
        character_artist,
        environment_artist,
        generalist,
        hard_surface_artist,
        lighting_td,
    )
    for mod in (generalist, environment_artist, hard_surface_artist, lighting_td, character_artist):
        p = mod.PERSONA
        _REGISTRY[p.id] = p
        log.debug("Registered persona: %s (%d-char extension)", p.id, len(p.extension))


# ── Intent → Persona routing table ──────────────────────────────────────
# Per docs/AI_ARCHITECTURE.md §5.1. Phase 4 ships only the four personas
# below — intents that should map to a not-yet-built persona fall back
# to the generalist with a log warning. Phase 4-part-2 will fill these in.

_INTENT_TO_PERSONA: dict[str, str] = {
    # Environment Artist
    "dense_scene":        "environment_artist",
    "terrain_landscape":  "environment_artist",
    "architecture":       "environment_artist",
    "geometry_nodes_advanced": "environment_artist",

    # Hard Surface Artist
    "hard_surface_model": "hard_surface_artist",

    # Lighting TD (also takes render/compositing for now)
    "lighting_setup":     "lighting_td",
    "render_setup":       "lighting_td",
    "compositing":        "lighting_td",
    "material_authoring": "lighting_td",

    # Generalist (fallback / simple)
    "simple_edit":        "generalist",
    "question":           "generalist",
    "unknown":            "generalist",

    # Character Artist (Quality Plan Sprint 2A — ships 5th persona)
    "character_sculpt":      "character_artist",

    # Not-yet-shipped personas — fall back to generalist with a warning
    "character_animation":   "generalist",  # waiting on Animator persona
    "rig_setup":             "generalist",  # waiting on Rigger persona
    "cloth_sim":             "generalist",
    "fluid_water":           "generalist",
    "destruction_explosion": "generalist",
    "game_export":           "generalist",
    "2d_grease_pencil":      "generalist",
}


# ── Public API ──────────────────────────────────────────────────────────

def load_persona_extension(intent: str | None = None) -> Persona:
    """Return the persona for the given intent.

    Callers come from orchestrator/streaming.py. Phase 1-2 called this
    with intent=None and got the generalist; Phase 4+ passes a real
    intent from the classifier.
    """
    _ensure_registry_loaded()

    if not intent:
        return _REGISTRY["generalist"]

    persona_id = _INTENT_TO_PERSONA.get(intent, "generalist")
    persona = _REGISTRY.get(persona_id)
    if persona is None:
        log.warning("Persona %r not registered for intent %r — falling back to generalist",
                    persona_id, intent)
        return _REGISTRY["generalist"]
    return persona


def all_personas() -> list[Persona]:
    """Used by /personas debug endpoint and eval harness."""
    _ensure_registry_loaded()
    return list(_REGISTRY.values())


# Back-compat for Phase 1-2 callers that imported GENERALIST directly.
# `load_persona_extension(None)` is the modern path.
def _get_generalist() -> Persona:
    _ensure_registry_loaded()
    return _REGISTRY["generalist"]


# Module-level GENERALIST reference resolved lazily on first access.
# Some callers import this name; defer creation until the registry loads.
class _GeneralistProxy:
    def __getattr__(self, name: str):
        return getattr(_get_generalist(), name)


GENERALIST = _GeneralistProxy()  # type: ignore[assignment]
