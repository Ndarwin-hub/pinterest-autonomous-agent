"""Image selection and quality gates for Pinterest product Pins.

This module performs all image validation locally on Railway. It does not
consume Composio calls for HTTP image checks or dimension inspection.
"""
from __future__ import annotations

import io
import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx
from PIL import ImageFile

logger = logging.getLogger("pinterest-agent.image-quality")

MIN_DIMENSION = 600
PREFERRED_MIN_DIMENSION = 1000
MAX_ASPECT = 2.2
MAX_IMAGE_BYTES_TO_INSPECT = 3 * 1024 * 1024


def _quality_gate(width: int, height: int) -> Tuple[bool, str]:
    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        return False, f"too_small:{width}x{height}"
    ratio = max(width, height) / max(1, min(width, height))
    if ratio > MAX_ASPECT:
        return False, f"ultra_wide_or_tall:{width}x{height}"
    return True, "ok"


async def inspect_image_url(url: str) -> Optional[Tuple[int, int]]:
    """Read enough of a remote image to obtain real dimensions.

    Uses a streaming parser so a large product image does not need to be fully
    downloaded. This is ordinary HTTP and does not count as a Composio call.
    """
    if not url or not str(url).startswith(("http://", "https://")):
        return None
    parser = ImageFile.Parser()
    total = 0
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            async with client.stream("GET", url, headers={"User-Agent": "Mozilla/5.0 PinterestAgent/3.4"}) as resp:
                if resp.status_code >= 400:
                    return None
                content_type = (resp.headers.get("content-type") or "").lower()
                if content_type and "image" not in content_type:
                    return None
                async for chunk in resp.aiter_bytes(16384):
                    total += len(chunk)
                    if total > MAX_IMAGE_BYTES_TO_INSPECT:
                        break
                    parser.feed(chunk)
                    if parser.image is not None:
                        return int(parser.image.width), int(parser.image.height)
    except Exception as exc:
        logger.debug("Image inspection failed for %s: %s", url, exc)
    return None


async def validate_candidate(candidate: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    url = candidate.get("url")
    dims = await inspect_image_url(url)
    if not dims:
        return None
    width, height = dims
    ok, reason = _quality_gate(width, height)
    if not ok:
        logger.info("Rejected image %s: %s", url, reason)
        return None
    out = dict(candidate)
    out["width"] = width
    out["height"] = height
    out["quality_gate"] = reason
    return out


def quality_score(candidate: Dict[str, Any], product: Dict[str, Any], strategy_key: str) -> int:
    """Rank validated images for product authenticity and Pinterest usability."""
    provider = (candidate.get("provider") or "").lower()
    source = (candidate.get("source") or "").lower()
    name = (product.get("name") or "").lower()
    brand = (product.get("brand") or "").lower()
    width = int(candidate.get("width") or 0)
    height = int(candidate.get("height") or 0)
    ratio = width / max(1, height)

    score = 40
    if provider == "product_page":
        score += 34
    elif provider == "composio_search_image":
        score += 22
        if brand and brand in source:
            score += 12
        name_tokens = [w for w in name.split() if len(w) > 3]
        if any(w in source for w in name_tokens[:4]):
            score += 7
    elif provider in ("pexels", "pixabay", "unsplash"):
        score += 8

    if min(width, height) >= PREFERRED_MIN_DIMENSION:
        score += 10

    # Pinterest recommends a vertical 2:3 canvas. Product-source images are
    # still allowed in square/4:3 form; vertical receives the strongest bonus.
    if 0.60 <= ratio <= 0.80:
        score += 12
    elif 0.80 < ratio <= 1.05:
        score += 9
    elif 1.05 < ratio <= 1.35:
        score += 6
    elif 1.35 < ratio <= 1.80:
        score += 2

    # Deterministic tie-breaker without external calls.
    score += sum(ord(ch) for ch in (strategy_key + (candidate.get("url") or ""))) % 4
    return min(score, 100)


async def choose_best_image(
    product: Dict[str, Any],
    strategy: Dict[str, Any],
    pin_index: int,
    used_urls: set,
    agent_mod: Any,
) -> Optional[Dict[str, Any]]:
    """Build and validate a small candidate pool, then return its best image.

    At most two COMPOSIO_SEARCH_IMAGE calls are made for a Pin. The caller's
    hard budget wrapper enforces that limit. All dimension/quality checks are
    local HTTP work and therefore do not consume Composio usage.
    """
    name = product.get("name") or "product"
    query = f"{name} {strategy.get('focus') or 'official product photo'}"[:120]
    candidates: List[Dict[str, Any]] = []

    # Authentic product-page images are preferred, but only after real image
    # dimensions are inspected. This is the permanent fix for banner strips.
    for url in product.get("images") or []:
        if url and url not in used_urls:
            candidates.append({"url": url, "provider": "product_page", "source": "product page", "license": "product_page"})

    # Composio Search is the primary external discovery source. Two targeted
    # queries give diversity while remaining below the 22-call ceiling.
    queries = [query, f"{name} official product photo"[:120]]
    for q in queries:
        try:
            found = await agent_mod.search_composio_images(q, num=8)
        except Exception as exc:
            logger.warning("Composio image search failed: %s", exc)
            found = []
        for item in found:
            url = item.get("url")
            if url and url not in used_urls and not any(x.get("url") == url for x in candidates):
                candidates.append(item)

    if not candidates:
        return None

    # Validate concurrently; no Composio calls are made here.
    import asyncio
    checked = await asyncio.gather(*(validate_candidate(c) for c in candidates[:30]))
    valid = [c for c in checked if c]
    if not valid:
        return None

    for candidate in valid:
        candidate["score"] = quality_score(candidate, product, strategy.get("key", ""))

    valid.sort(key=lambda c: c.get("score", 0), reverse=True)
    best = valid[0]
    used_urls.add(best["url"])
    logger.info(
        "Pin %s image selected: provider=%s size=%sx%s score=%s",
        pin_index,
        best.get("provider"),
        best.get("width"),
        best.get("height"),
        best.get("score"),
    )
    return best
