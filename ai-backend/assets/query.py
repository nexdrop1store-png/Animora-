"""
Spec-driven asset matcher — Quality Plan §6.6.

After orchestrator/spec.py builds the creative brief, this module
finds catalog assets whose tags overlap the brief's intent. The result
is a small ranked list the context_builder injects into the agentic
loop's system context, so the model sees "assets available for this
turn: hdri.golden_hour_field; texture.weathered_oak; ..." and can call
`use_asset(asset_id="hdri.golden_hour_field")` to drop one in.

## Why simple tag matching (not semantic search)

  Catalog is small (<100 entries). Tag-vocabulary is curated. A real
  retrieval engine (FAISS, an embedding model) would add infra cost
  for accuracy we don't need at this size. Semantic upgrade path stays
  open — `relevant_assets()` is a pure function with a stable signature,
  swap the implementation when the catalog hits ~1000 entries.

## Scoring formula

  For each candidate Asset, count the number of tags that match a
  token extracted from the SPEC's lighting / palette / composition /
  materials / scale_notes / subject. Tags that appear in multiple
  SPEC fields count multiple times — a "warm" tag in both lighting
  AND palette scores 2 for that asset. Top-N by score, ties broken
  by catalog order (stable).

  No score → not suggested. The model sees only relevant assets, not
  a dump of the whole catalog. Surface ~6 suggestions max — enough
  to give the model choice, few enough to keep the context tight.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .catalog import ASSETS, Asset, AssetKind


# How many suggestions to surface to the model per turn. 6 is enough
# to cover HDRI + a couple of textures + a couple of meshes for a
# typical scene without bloating the context window.
DEFAULT_MAX_SUGGESTIONS = 6


# A `Spec` from orchestrator/spec.py is a dict with these top-level
# keys (see prompts/spec_builder.SPEC_SCHEMA_DOC). We pluck text from
# the ones that contain searchable intent vocabulary.
_SEARCHABLE_SPEC_FIELDS: tuple[str, ...] = (
    "subject",
    "scale_notes",
)
_SEARCHABLE_NESTED: tuple[tuple[str, tuple[str, ...]], ...] = (
    # (top_key, (sub_keys))
    ("framing", ("camera", "angle")),
    ("lighting", ("time_of_day", "key", "fill", "rim", "mood")),
    ("palette", ("dominant", "accent", "neutral")),
    ("composition", ("foreground", "midground", "background", "hero")),
    ("density", ("scattered", "control")),
)


_TOKEN_RE = re.compile(r"[a-z]{3,}")


def _spec_tokens(spec: dict) -> list[str]:
    """Extract a flat list of lowercased tokens from the SPEC fields
    that contain intent vocabulary. Duplicates intentional: a tag
    appearing in multiple SPEC fields scores higher."""
    text_parts: list[str] = []
    for k in _SEARCHABLE_SPEC_FIELDS:
        v = spec.get(k)
        if isinstance(v, str) and v:
            text_parts.append(v)
    for top_key, sub_keys in _SEARCHABLE_NESTED:
        sub = spec.get(top_key)
        if isinstance(sub, dict):
            for sk in sub_keys:
                v = sub.get(sk)
                if isinstance(v, str) and v:
                    text_parts.append(v)
    # Materials is a list of {on, type, notes}
    materials = spec.get("materials")
    if isinstance(materials, list):
        for m in materials:
            if not isinstance(m, dict):
                continue
            for sk in ("on", "type", "notes"):
                v = m.get(sk)
                if isinstance(v, str) and v:
                    text_parts.append(v)

    blob = " ".join(text_parts).lower()
    return _TOKEN_RE.findall(blob)


@dataclass
class ScoredAsset:
    asset: Asset
    score: int  # 1+ tag matches
    matched_tags: tuple[str, ...]


def relevant_assets(
    spec: dict,
    *,
    max_suggestions: int = DEFAULT_MAX_SUGGESTIONS,
    kind_filter: AssetKind | None = None,
) -> list[ScoredAsset]:
    """Rank the catalog by tag overlap with the SPEC. Returns at most
    `max_suggestions` entries with score >= 1. Empty list when the SPEC
    is empty or no catalog asset matches.

    `kind_filter` scopes to a single asset kind — useful when the
    caller wants HDRI-only or texture-only suggestions for a focused
    sub-context."""
    if not spec:
        return []

    tokens = _spec_tokens(spec)
    if not tokens:
        return []
    # Dedupe tokens but keep multiplicity (we WANT the warm-warm
    # double-mention to score higher). Cheapest way: keep the list
    # as-is and let collections.Counter resolve below.
    from collections import Counter
    tok_counts = Counter(tokens)

    pool = ASSETS if kind_filter is None else tuple(a for a in ASSETS if a.kind is kind_filter)

    scored: list[ScoredAsset] = []
    for asset in pool:
        matched: list[str] = []
        score = 0
        for tag in asset.tags:
            # Tag is matched if ANY token contains the tag or vice versa.
            # We use `in` rather than equality so multi-word tags like
            # "golden_hour" match "golden" tokens too (PolyHaven tags
            # are short single-word so this is conservative).
            tag_lower = tag.lower()
            tag_score = 0
            for tok, cnt in tok_counts.items():
                if tag_lower == tok or tag_lower in tok or tok in tag_lower:
                    tag_score += cnt
            if tag_score > 0:
                matched.append(tag)
                score += tag_score
        if score > 0:
            scored.append(ScoredAsset(asset=asset, score=score, matched_tags=tuple(matched)))

    # Sort by score desc, then by id for stable order.
    scored.sort(key=lambda s: (-s.score, s.asset.id))
    return scored[:max_suggestions]


def format_for_model(suggestions: list[ScoredAsset]) -> str:
    """Render the suggestion list as a compact text block the master
    prompt + context_builder injects after the SPEC. Stays small:
    one line per asset, max 6 assets. The model sees IDs, kinds, and
    one-sentence summaries — enough signal to call `use_asset` with
    confidence."""
    if not suggestions:
        return ""

    lines = ["[AVAILABLE ASSETS for this turn — prefer use_asset over hand-built when one fits]"]
    for s in suggestions:
        kind = s.asset.kind.value
        lines.append(
            f"  • {s.asset.id} ({kind}): {s.asset.summary}"
        )
    lines.append(
        "Call use_asset(asset_id=\"<id>\") to drop one in. Animora fetches "
        "the file from PolyHaven's CDN (cached after first use) and the "
        "addon applies it to the active scene."
    )
    return "\n".join(lines)
