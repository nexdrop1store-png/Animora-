"""
Asset fetcher — downloads PolyHaven assets to a local on-disk cache.

Called from the orchestrator when the model emits `use_asset`. First
fetch downloads from PolyHaven's CDN to `~/.animora/assets/<id>/<file>`;
subsequent calls hit the cache (zero network). The addon receives an
absolute local path it can open via `bpy.ops.image.open()` (HDRI),
`bpy.ops.wm.append()` (mesh / .blend datablock), or by attaching to
a texture node (texture set).

## Design choices

  - **HTTP, not boto3.** PolyHaven is a public CDN. boto3 would only
    add weight for S3-style auth we don't need.
  - **httpx async client.** Already a backend dep (FastAPI ecosystem).
    Single connection pool shared across the process.
  - **Atomic writes.** Download to `<file>.partial`, rename on
    success. Partial files never confuse the addon.
  - **No retry / backoff here.** PolyHaven CDN is reliable; transient
    failures bubble up as `AssetFetchError` and the orchestrator
    decides whether to surface to user or fall back to hand-built.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from .catalog import Asset, AssetKind, by_id

log = logging.getLogger("animora.assets")

# Per-user cache dir. Cross-platform: `Path.home()` works on Windows
# (C:\Users\<user>) and Unix (~). The Animora addon and the backend
# share the same machine in dev (BYOK) and not in prod (Fargate), so
# this cache is BACKEND-LOCAL on Fargate and per-user on dev boxes.
# That's the right behavior — assets are fungible bytes, no per-user
# identity tied to them.
ASSET_CACHE_ROOT = Path(os.environ.get(
    "ANIMORA_ASSET_CACHE",
    str(Path.home() / ".animora" / "assets"),
))


# Default fetch resolution. PolyHaven offers 1k/2k/4k/8k. 2k is the
# right balance: HDRI ~6 MB, texture set ~15 MB, mesh blend ~5 MB.
# Studio renders can request 4k via the optional `resolution` arg.
DEFAULT_RESOLUTION = "2k"


# PolyHaven CDN URL patterns by asset kind. Verified live against
# polyhaven.org 2026-05-25 with HEAD requests on `studio_small_03`,
# `weathered_planks`, and `ArmChair_01`. The asymmetric pattern
# (textures + models have `<slug>/<slug>_<res>.<ext>` while HDRIs
# don't) is what PolyHaven actually uses.
_URL_TEMPLATES: dict[AssetKind, str] = {
    AssetKind.HDRI: (
        "https://dl.polyhaven.org/file/ph-assets/HDRIs/hdr/"
        "{res}/{slug}_{res}.hdr"
    ),
    AssetKind.TEXTURE: (
        "https://dl.polyhaven.org/file/ph-assets/Textures/blend/"
        "{res}/{slug}/{slug}_{res}.blend"
    ),
    AssetKind.MESH: (
        # Models need the extra <slug>/ directory layer
        "https://dl.polyhaven.org/file/ph-assets/Models/blend/"
        "{res}/{slug}/{slug}_{res}.blend"
    ),
}


class AssetFetchError(Exception):
    """Asset couldn't be fetched. Orchestrator catches this and falls
    back gracefully — never let a fetch failure kill the user's turn."""


@dataclass
class FetchedAsset:
    """Result of a successful fetch."""
    asset: Asset
    local_path: Path
    resolution: str
    cached: bool  # True when served from local cache (no network)


def _url_for(asset: Asset, resolution: str) -> str:
    template = _URL_TEMPLATES.get(asset.kind)
    if template is None:
        raise AssetFetchError(f"No URL template for asset kind {asset.kind}")
    return template.format(slug=asset.polyhaven_id, res=resolution)


def _local_path_for(asset: Asset, resolution: str) -> Path:
    """Where the fetched file ultimately lives. One directory per
    asset id so HDRI + texture + thumbnail can sit together if we
    ever fetch multiple files per asset."""
    ext = {
        AssetKind.HDRI: "hdr",
        AssetKind.TEXTURE: "blend",
        AssetKind.MESH: "blend",
    }[asset.kind]
    return ASSET_CACHE_ROOT / asset.id / f"{resolution}.{ext}"


async def fetch_asset(
    asset_id: str,
    *,
    resolution: str = DEFAULT_RESOLUTION,
    timeout_sec: float = 30.0,
    client: httpx.AsyncClient | None = None,
) -> FetchedAsset:
    """Resolve an asset id, ensure it's on disk, return the local path.

    Cache hit: <1 ms. Cache miss: 100ms-5s depending on size.
    Re-entrant safe: two concurrent fetches of the same asset will
    both download (small race window, idempotent outcome).

    `client` is optional — pass one from the caller for connection
    pooling across many fetches. We create one on demand otherwise.
    """
    asset = by_id(asset_id)
    if asset is None:
        raise AssetFetchError(f"Unknown asset id: {asset_id}")

    local = _local_path_for(asset, resolution)
    if local.is_file() and local.stat().st_size > 0:
        log.debug("asset.cache.hit id=%s path=%s", asset_id, local)
        return FetchedAsset(asset=asset, local_path=local, resolution=resolution, cached=True)

    url = _url_for(asset, resolution)
    local.parent.mkdir(parents=True, exist_ok=True)
    partial = local.with_suffix(local.suffix + ".partial")

    log.info("asset.fetch.start id=%s url=%s", asset_id, url)
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_sec)
    try:
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise AssetFetchError(
                    f"HTTP {resp.status_code} fetching {asset_id} from {url}"
                )
            # Atomic write: stream to .partial, rename on success.
            with partial.open("wb") as f:
                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                    f.write(chunk)
        partial.replace(local)
    except (httpx.HTTPError, OSError) as exc:
        # Clean up partial if it exists
        try:
            partial.unlink(missing_ok=True)
        except OSError:
            pass
        raise AssetFetchError(f"Fetch failed for {asset_id}: {exc}") from exc
    finally:
        if owns_client:
            await client.aclose()

    size = local.stat().st_size
    log.info("asset.fetch.complete id=%s bytes=%d path=%s", asset_id, size, local)
    return FetchedAsset(asset=asset, local_path=local, resolution=resolution, cached=False)


def is_cached(asset_id: str, *, resolution: str = DEFAULT_RESOLUTION) -> bool:
    """Fast existence check without touching the network. Useful for
    the orchestrator deciding whether to mention warm-cache assets
    in the SPEC suggestions vs cold-cache ones (cold means 1-5s
    latency on first use; warm is instant)."""
    asset = by_id(asset_id)
    if asset is None:
        return False
    local = _local_path_for(asset, resolution)
    return local.is_file() and local.stat().st_size > 0
