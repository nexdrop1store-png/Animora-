---
name: animora-personas
description: Use when adding or editing an artist persona, changing intent→persona routing, or writing persona prompt blocks — "add a persona", "materials persona", "persona routing", "intent classifier picks wrong persona", "weak-spot guard", "persona quality checks". Documents the persona anatomy, the registry, the routing table, and the add-a-persona procedure.
---

# Animora personas

## Current roster (registry: `ai-backend/orchestrator/personas.py`)
| id | Module | Covers |
|---|---|---|
| `generalist` | `personas/generalist.py` | Q&A, simple edits, unknown intents — the fallback |
| `environment_artist` | `personas/environment_artist.py` | dense scenes, terrain, architecture, advanced GN |
| `hard_surface_artist` | `personas/hard_surface_artist.py` | vehicles, weapons, mechanical props |
| `lighting_td` | `personas/lighting_td.py` | lighting, render setup, compositing, **material_authoring (temporarily)** |
| `character_artist` | `personas/character_artist.py` | character sculpt |
Plus `personas/base.py::BASE_EXTENSION` (shared workflow principles, ~600 tokens, prepended to every persona) and `personas/mesh_repair_recipes.py` (repair knowledge consumed by quality checks).

**Approved V2 decision (founder, 2026-07-12): build a distinct `materials_artist` persona.** Its routing move: retarget `"material_authoring"` in `_INTENT_TO_PERSONA` (`orchestrator/personas.py:118`) from `lighting_td` to the new persona. Generalist remains fallback.

## Persona anatomy (`Persona` dataclass, `orchestrator/personas.py:36-70`)
- `id` — stable, used in logs and as cache key.
- `display_name` — currently hidden from users (UX decision §5.6).
- `extension` — the prompt block. **2–4k tokens of dense specialist knowledge**; <300 tokens means the persona isn't doing its job. Structure it as: triggers/what-I-own → priorities → workflow selection rules → quality bar (specific, checkable) → weak-spot guard.
- `default_model_hint` — 'haiku'|'sonnet'|'opus'|'auto'; bias the router for inherently complex domains.
- `quality_checks` — names consumed by the artist's-eye stage; every check named here must have a recipe (`test_phase5_quality.py::test_personas_quality_checks_have_recipes` enforces this).
- `knowledge_sections` — reserved for shared knowledge modules; empty today.

## Weak-spot guard pattern
Each persona ends its extension with an explicit guard against its own characteristic failure (e.g. environment: "never ship a floating asset with no ground contact shadow"; hard-surface: "never leave boolean artifacts un-beveled"). When you add a persona, plant a bad case in the eval that the guard must catch — the guard is only real if a benchmark fails without it.

## Routing
1. `orchestrator/intent.py` — Haiku classifier → intent label (`prompts/intent_classifier.py` defines labels; `_VALID_INTENTS` must stay aligned with `router.py`'s `_NON_EXECUTION_INTENTS`).
2. `_INTENT_TO_PERSONA` table (`orchestrator/personas.py:104-136`) → persona id; unknown/unshipped intents fall back to generalist WITH a log warning — watch for warning streaks; they mean a missing persona or a classifier drift.
3. Multi-persona tasks: sequenced sub-tasks, each completing its own loop before the next persona starts (never blend two extensions in one turn — cache and voice both break).

## Add-a-persona procedure
1. `personas/<name>.py` exporting `PERSONA = Persona(...)` (follow `environment_artist.py` structure).
2. Register: add module to `_ensure_registry_loaded()` imports+loop (`orchestrator/personas.py:81-96`).
3. Route: update `_INTENT_TO_PERSONA`; add/adjust intent labels in `prompts/intent_classifier.py` if a new label is needed (then re-run `test_phase4_classifier.py` — API-keyed).
4. Quality: declare `quality_checks`; ensure recipes exist (`mesh_repair_recipes.py` or persona-local).
5. Eval: add representative benchmarks per difficulty + one planted weak-spot case in `eval/benchmarks.py`.
6. Verify: representative prompts trigger it (log line `Registered persona: <id>`), approaches demonstrably differ from generalist, weak-spot benchmark fails with the guard removed.
7. Cache note: a new persona is a new cache prefix — first turn per session re-warms; that's expected, don't "fix" it.
