"""
Animora asset library — Quality Plan §6.6 (asset-first building).

Wires a curated catalog of CC0 PolyHaven assets (HDRIs, textures, meshes)
into the agentic loop so the model can call `use_asset` instead of
hand-building details from primitives. The catalog raises the quality
floor immediately — a model that drops in a real wood texture beats a
model that hand-codes a Principled BSDF wood approximation every time.

## Module layout

  catalog.py  — the curated `ASSETS` list. Static dataclasses with
                PolyHaven IDs, tags, dimensions. ~50 HDRIs + ~30 textures
                + ~20 meshes (CC0 baseline).
  fetcher.py  — async HTTP fetch + local disk cache at
                `~/.animora/assets/<id>/<file>`. Fetches once per
                machine, then serves from cache.
  query.py    — `relevant_assets(spec) -> list[Asset]` — keyword/tag
                matching against the SPEC built by orchestrator/spec.py.
                Returns top-N matches the master-prompt context_builder
                injects as suggestions.

## Why CC0 (PolyHaven specifically)

  License risk vanishes. Every asset can be redistributed, embedded
  in user scenes, sold as part of finished work. No license compliance
  thread in the product. PolyHaven also provides 1k/2k/4k/8k variants
  per asset — we fetch 2k by default (good quality, ~5 MB per HDRI)
  with the option to upgrade per-request.

## How the model uses it

  At turn start, orchestrator/spec.py builds the creative brief. The
  context builder then queries `query.relevant_assets(spec)` and lists
  the top suggestions in the user-role SPEC message. The model sees
  "available assets for this turn: hdri.golden_hour_field (warm
  outdoor); texture.weathered_oak (medium-grain wood)" and can call
  `use_asset(asset_id="hdri.golden_hour_field")` instead of writing
  a hand-rolled HDRI shader.
"""

from __future__ import annotations
