"""
Curated PolyHaven asset catalog.

Every entry is CC0 (PolyHaven licensing). The list is intentionally
small to start — ~50 HDRIs, ~30 textures, ~20 meshes. Growth path:
when an eval benchmark or a real session shows the model would have
used an asset we don't have, add it here. Don't try to be exhaustive
up front; let the catalog grow from observed need.

## Asset ID format

  `<kind>.<short_slug>` — `hdri.studio_neutral`, `texture.weathered_oak`,
  `mesh.cc0_chair_01`. Stable across releases; the catalog can grow
  but IDs once published don't change (so saved sessions keep working).

## PolyHaven URL conventions

  HDRIs:    https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/<RES>/<id>_<RES>.hdr
  Textures: https://dl.polyhaven.org/file/ph-assets/Textures/blend/<RES>/<id>/<id>_<RES>.blend
  Models:   https://dl.polyhaven.org/file/ph-assets/Models/blend/<RES>/<id>_<RES>.blend

  `<RES>` is one of `1k`, `2k`, `4k`, `8k`. Default to `2k` — best
  quality/size trade-off for production work.

The `polyhaven_id` field is the canonical PolyHaven slug (the one that
goes in the URL). The `id` field is OUR internal id which can differ
(stable contract with the LLM-facing tool).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class AssetKind(str, Enum):
    HDRI = "hdri"        # environment lighting (.hdr file, world background)
    TEXTURE = "texture"  # PBR texture set (color/roughness/normal/displacement)
    MESH = "mesh"        # 3D model (.blend file)


@dataclass(frozen=True)
class Asset:
    """One catalog entry. Immutable — the catalog is the contract with
    the LLM, and we don't mutate at runtime."""

    id: str
    """Our internal stable ID. e.g. 'hdri.studio_neutral'."""

    kind: AssetKind
    """One of HDRI / TEXTURE / MESH — determines fetch path and addon handler."""

    name: str
    """Human-readable name surfaced to the user / the model."""

    polyhaven_id: str
    """The PolyHaven slug (used to construct the download URL)."""

    tags: frozenset[str]
    """Keywords used by query.relevant_assets to match against SPEC fields.
    Tag vocabulary is open — common ones: warm, cool, golden_hour, studio,
    outdoor, indoor, day, night, wood, metal, stone, fabric, character,
    furniture, vehicle, prop."""

    summary: str
    """One-sentence description shown to the model in asset suggestions."""

    default_resolution: str = "2k"
    """Default PolyHaven resolution. Override per request via fetcher."""


# ── HDRIs ────────────────────────────────────────────────────────────
# Environment lighting. CC0 PolyHaven HDRIs cover most lighting moods
# the model will request — pick by `tags` matching the SPEC's
# lighting.time_of_day + lighting.mood + composition.background hints.

_HDRIS: tuple[Asset, ...] = (
    Asset(
        id="hdri.studio_neutral",
        kind=AssetKind.HDRI,
        name="Brown Photostudio Neutral",
        polyhaven_id="brown_photostudio_02",
        tags=frozenset({"studio", "indoor", "neutral", "product", "even"}),
        summary="Soft neutral studio lighting — classic product-viz lookdev base.",
    ),
    Asset(
        id="hdri.studio_high_key",
        kind=AssetKind.HDRI,
        name="Studio Small",
        polyhaven_id="studio_small_03",
        tags=frozenset({"studio", "indoor", "bright", "high_key", "product"}),
        summary="Bright, even high-key studio for clean product shots.",
    ),
    Asset(
        id="hdri.golden_hour_field",
        kind=AssetKind.HDRI,
        name="Sunset in the Chalk Quarry",
        polyhaven_id="sunset_in_the_chalk_quarry",
        tags=frozenset({"outdoor", "golden_hour", "warm", "sunset", "field"}),
        summary="Warm golden-hour outdoor lighting with long shadows.",
    ),
    Asset(
        id="hdri.blue_hour",
        kind=AssetKind.HDRI,
        name="Kloofendal Misty Morning",
        polyhaven_id="kloofendal_misty_morning_puresky",
        tags=frozenset({"outdoor", "blue_hour", "cool", "morning", "misty"}),
        summary="Cool misty morning — soft blue hour with low key intensity.",
    ),
    Asset(
        id="hdri.noon_overcast",
        kind=AssetKind.HDRI,
        name="Studio Country Hall",
        polyhaven_id="studio_country_hall",
        tags=frozenset({"indoor", "overcast", "neutral", "soft", "daylight"}),
        summary="Soft daylight through windows — diffused indoor neutral.",
    ),
    Asset(
        id="hdri.night_city",
        kind=AssetKind.HDRI,
        name="Dikhololo Night",
        polyhaven_id="dikhololo_night",
        tags=frozenset({"outdoor", "night", "dark", "moonlit", "blue"}),
        summary="Moonlit night sky — low light with subtle blue tone.",
    ),
    Asset(
        id="hdri.warm_interior",
        kind=AssetKind.HDRI,
        name="Lythwood Lounge",
        polyhaven_id="lythwood_lounge",
        tags=frozenset({"indoor", "warm", "interior", "evening", "amber"}),
        summary="Warm amber interior with practical lamp sources.",
    ),
    Asset(
        id="hdri.beach_day",
        kind=AssetKind.HDRI,
        name="Belfast Sunset",
        polyhaven_id="belfast_sunset_puresky",
        tags=frozenset({"outdoor", "beach", "sunset", "warm", "horizon"}),
        summary="Sunset over open horizon — wide beach / coastline lighting.",
    ),
    Asset(
        id="hdri.forest_canopy",
        kind=AssetKind.HDRI,
        name="Dreifaltigkeitsberg",
        polyhaven_id="dreifaltigkeitsberg",
        tags=frozenset({"outdoor", "forest", "green", "diffused", "day"}),
        summary="Diffused forest canopy lighting with green ambient cast.",
    ),
    Asset(
        id="hdri.industrial",
        kind=AssetKind.HDRI,
        name="Autoshop",
        polyhaven_id="autoshop_01",
        tags=frozenset({"indoor", "industrial", "garage", "workshop", "metallic"}),
        summary="Industrial garage / autoshop — bays of practical lights.",
    ),
    Asset(
        id="hdri.snowy_field",
        kind=AssetKind.HDRI,
        name="Snowy Field",
        polyhaven_id="snowy_field",
        tags=frozenset({"outdoor", "snow", "cold", "winter", "bright"}),
        summary="Bright snowy outdoor scene — high-contrast cold light.",
    ),
    Asset(
        id="hdri.dramatic_sky",
        kind=AssetKind.HDRI,
        name="Spruit Sunrise",
        polyhaven_id="spruit_sunrise",
        tags=frozenset({"outdoor", "sunrise", "dramatic", "warm", "clouds"}),
        summary="Dramatic sunrise with bold cloud silhouettes.",
    ),
)


# ── Textures ─────────────────────────────────────────────────────────
# PBR texture sets (color / roughness / normal / displacement). Surface
# scale ranges 0.5m-2m unless noted; the model is expected to use
# appropriate UV scale for the asset it's texturing.

_TEXTURES: tuple[Asset, ...] = (
    Asset(
        id="texture.weathered_oak",
        kind=AssetKind.TEXTURE,
        name="Weathered Oak Wood",
        polyhaven_id="weathered_planks",
        tags=frozenset({"wood", "warm", "natural", "weathered", "floor", "table"}),
        summary="Weathered oak planks — warm tones, visible grain, light wear.",
    ),
    Asset(
        id="texture.polished_marble",
        kind=AssetKind.TEXTURE,
        name="White Marble",
        polyhaven_id="marble_01",
        tags=frozenset({"stone", "marble", "polished", "white", "veined", "luxury"}),
        summary="Polished white marble with subtle gray veining.",
    ),
    Asset(
        id="texture.red_brick",
        kind=AssetKind.TEXTURE,
        name="Red Brick Wall",
        polyhaven_id="red_brick_03",
        tags=frozenset({"brick", "wall", "red", "outdoor", "industrial"}),
        summary="Classic red brick wall with mortar lines.",
    ),
    Asset(
        id="texture.concrete_floor",
        kind=AssetKind.TEXTURE,
        name="Polished Concrete",
        polyhaven_id="concrete_floor_02",
        tags=frozenset({"concrete", "floor", "industrial", "gray", "modern"}),
        summary="Polished concrete floor — slight roughness, neutral gray.",
    ),
    Asset(
        id="texture.brushed_steel",
        kind=AssetKind.TEXTURE,
        name="Brushed Steel",
        polyhaven_id="metal_plate",
        tags=frozenset({"metal", "steel", "brushed", "industrial", "panel"}),
        summary="Brushed steel panel with directional anisotropic highlight.",
    ),
    Asset(
        id="texture.fabric_linen",
        kind=AssetKind.TEXTURE,
        name="Linen Fabric",
        polyhaven_id="fabric_pattern_05",
        tags=frozenset({"fabric", "cloth", "linen", "natural", "weave"}),
        summary="Natural linen weave — visible thread pattern, matte finish.",
    ),
    Asset(
        id="texture.rocky_ground",
        kind=AssetKind.TEXTURE,
        name="Rock Ground",
        polyhaven_id="rocky_terrain_02",
        tags=frozenset({"rock", "ground", "outdoor", "rough", "natural", "terrain"}),
        summary="Rough rocky outdoor ground — high displacement detail.",
    ),
    Asset(
        id="texture.sand_dune",
        kind=AssetKind.TEXTURE,
        name="Desert Sand",
        polyhaven_id="aerial_beach_01",
        tags=frozenset({"sand", "beach", "desert", "ripples", "outdoor"}),
        summary="Fine sand with wind ripples — for beach and desert ground.",
    ),
)


# ── Meshes ───────────────────────────────────────────────────────────
# Pre-built CC0 reference meshes. Use when the user asks for a generic
# instance of something (a chair, a coffee cup, a tree) where a vetted
# model dramatically beats a hand-built primitive approximation.

#
# Mesh catalog is intentionally small to start. PolyHaven's model
# library is far smaller than its HDRI / texture libraries, so we ship
# only verified-real slugs. Growth path: query api.polyhaven.com/assets?t=models
# to pull the real list, then add entries here with confirmed
# `polyhaven_id` values.

_MESHES: tuple[Asset, ...] = (
    Asset(
        id="mesh.modern_chair",
        kind=AssetKind.MESH,
        name="Modern Chair",
        polyhaven_id="ArmChair_01",
        tags=frozenset({"chair", "furniture", "indoor", "modern", "seat"}),
        summary="Modern upholstered chair — neutral fabric, ready to drop in.",
    ),
)


# ── The catalog ──────────────────────────────────────────────────────
# Flat tuple every consumer iterates. Order doesn't affect lookup
# (`query.relevant_assets` re-sorts by tag-match strength), but groups
# above are kept for readability + diffability.

ASSETS: tuple[Asset, ...] = _HDRIS + _TEXTURES + _MESHES


def by_id(asset_id: str) -> Asset | None:
    """O(N) lookup. Catalog is small (<100 entries) so a dict isn't
    needed; keeping it as a tuple lets us treat ASSETS as a stable
    iteration order in tests."""
    for a in ASSETS:
        if a.id == asset_id:
            return a
    return None


def by_kind(kind: AssetKind) -> tuple[Asset, ...]:
    """All assets of a given kind. Used by query.py to scope tag
    matching when the SPEC explicitly asks for one kind (e.g. lighting
    intent → HDRIs only)."""
    return tuple(a for a in ASSETS if a.kind is kind)
