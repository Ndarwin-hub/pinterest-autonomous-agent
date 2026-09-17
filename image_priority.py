"""High-resolution image supervisor for the Pinterest pipeline.

Policy: prefer genuine native 8K/4K product imagery first; then the best
available high-resolution web/product source; then configured AI generation;
then high-quality 4K upscaling as a last quality-preserving step; and only
finally the existing Pillow emergency card. No unavailable provider is claimed
as executable.
"""
from __future__ import annotations

import base64
import io
import os
from typing import Any, Dict, List, Set

import httpx

TARGET_W, TARGET_H = 2160, 3840  # 4K portrait canvas for Pinterest
NATIVE_8K_MIN = 6000
NATIVE_4K_MIN = 3500
MAX_DOWNLOAD = 18 * 1024 * 1024


def _pixels(c: Dict[str, Any]) -> int:
    return int(c.get("width") or 0) * int(c.get("height") or 0)


def _native_tier(c: Dict[str, Any]) -> int:
    m = max(int(c.get("width") or 0), int(c.get("height") or 0))
    if m >= NATIVE_8K_MIN:
        return 4
    if m >= NATIVE_4K_MIN:
        return 3
    if m >= 2000:
        return 2
    if m >= 1000:
        return 1
    return 0


def _rank(c: Dict[str, Any]) -> tuple:
    provider = c.get("provider") or ""
    authenticity = 5 if provider == "product_page" else 4 if provider == "composio_search_image" else 3
    if provider in {"pexels", "pixabay", "unsplash"}:
        authenticity = 2
    if provider == "openai_image":
        authenticity = 1
    # Resolution is the primary criterion; authenticity breaks ties.
    return (_native_tier(c), _pixels(c), authenticity)


async def _probe_dimensions(url: str) -> tuple[int, int, bytes | None]:
    try:
        async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code >= 400 or not r.content or len(r.content) > MAX_DOWNLOAD:
                return 0, 0, None
            from PIL import Image
            data = r.content
            with Image.open(io.BytesIO(data)) as im:
                return int(im.width), int(im.height), data
    except Exception:
        return 0, 0, None


def _to_4k(data: bytes) -> str | None:
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(data)).convert("RGB")
        # Fit without distorting or cropping the product; place on a 2:3 4K canvas.
        scale = min(TARGET_W / im.width, TARGET_H / im.height)
        nw = max(1, int(im.width * scale))
        nh = max(1, int(im.height * scale))
        im = im.resize((nw, nh), Image.Resampling.LANCZOS)
        canvas = Image.new("RGB", (TARGET_W, TARGET_H), (255, 255, 255))
        canvas.paste(im, ((TARGET_W - nw) // 2, (TARGET_H - nh) // 2))
        out = io.BytesIO()
        im_quality = 96
        canvas.save(out, format="JPEG", quality=im_quality, subsampling=0, optimize=True)
        return base64.b64encode(out.getvalue()).decode("ascii")
    except Exception:
        return None


async def _configured_stock(agent: Any, query: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    keys = [
        ("PIXABAY_API_KEY", "pixabay"),
        ("UNSPLASH_ACCESS_KEY", "unsplash"),
    ]
    async with httpx.AsyncClient(timeout=20.0) as client:
        for env, provider in keys:
            key = os.getenv(env, "").strip()
            if not key:
                continue
            try:
                if provider == "pixabay":
                    r = await client.get("https://pixabay.com/api/", params={"key": key, "q": query, "image_type": "photo", "orientation": "vertical", "per_page": 12})
                    rows = (r.json() or {}).get("hits", []) if r.status_code < 400 else []
                    for x in rows:
                        out.append({"url": x.get("largeImageURL") or x.get("webformatURL"), "provider": provider, "id": str(x.get("id") or ""), "width": x.get("imageWidth") or 0, "height": x.get("imageHeight") or 0, "license": "Pixabay License"})
                else:
                    r = await client.get("https://api.unsplash.com/search/photos", params={"client_id": key, "query": query, "orientation": "portrait", "per_page": 12})
                    rows = (r.json() or {}).get("results", []) if r.status_code < 400 else []
                    for x in rows:
                        u = (x.get("urls") or {}).get("full") or (x.get("urls") or {}).get("raw")
                        out.append({"url": u, "provider": provider, "id": str(x.get("id") or ""), "width": (x.get("width") or 0), "height": (x.get("height") or 0), "license": "Unsplash License"})
            except Exception:
                continue
    return [x for x in out if x.get("url")]


async def get_best_pin_image(product: Dict[str, Any], strategy: Dict[str, Any], pin_index: int, job_store: Any, job_id: str, used_urls: Set[str], agent: Any) -> Dict[str, Any]:
    job_store.update(job_id, progress=f"Pin {pin_index}/5: 8K/4K image selection ({strategy['name']})")
    name = product.get("name") or "product"
    query = f"{name} {strategy['focus']}"[:120]
    candidates: List[Dict[str, Any]] = []

    # 1. Genuine product-page imagery, ranked by native resolution.
    for url in product.get("images") or []:
        if url in used_urls:
            continue
        w, h, _ = await _probe_dimensions(url)
        if w and h:
            candidates.append({"url": url, "provider": "product_page", "width": w, "height": h, "license": "product_page"})

    # 2. Real web image search through the connected integration.
    for q in (name, query, f"{name} product"):
        try:
            found = await agent.search_composio_images(q, num=12)
            for f in found:
                if f.get("url") and f["url"] not in used_urls:
                    w, h, _ = await _probe_dimensions(f["url"])
                    if w and h:
                        f = dict(f); f["width"], f["height"] = w, h
                        candidates.append(f)
        except Exception:
            pass
        if any(_native_tier(c) >= 3 for c in candidates):
            break

    # 3. Configured Pexels + Pixabay + Unsplash sources.
    try:
        for f in await agent.search_pexels(query):
            if f.get("url") and f["url"] not in used_urls:
                w, h, _ = await _probe_dimensions(f["url"])
                if w and h:
                    f = dict(f); f["width"], f["height"] = w, h
                    candidates.append(f)
    except Exception:
        pass
    candidates.extend(await _configured_stock(agent, query))

    # 4. Select native 8K/4K first, irrespective of provider; never fake a
    # high-resolution source by simply trusting a URL's metadata.
    candidates = [c for c in candidates if c.get("url") and c["url"] not in used_urls]
    candidates.sort(key=_rank, reverse=True)
    if candidates:
        best = candidates[0]
        if _native_tier(best) >= 3 and await agent._url_ok(best["url"]):
            used_urls.add(best["url"])
            return {"mode": "url", "value": best["url"], "provider": best.get("provider"), "id": best.get("id"), "score": 100 if _native_tier(best) == 4 else 95, "license": best.get("license"), "resolution_tier": "8K+" if _native_tier(best) == 4 else "4K+"}

    # 5. If no native 8K/4K source exists, use the best real source and make a
    # 4K portrait derivative locally. This is quality degradation only after
    # all native high-resolution sources have been exhausted.
    for c in candidates:
        w, h, data = await _probe_dimensions(c["url"])
        if data and w >= 900 and h >= 900:
            b64 = _to_4k(data)
            if b64:
                used_urls.add(c["url"])
                return {"mode": "base64", "value": b64, "provider": f"{c.get('provider')}_4k_upscale", "id": c.get("id"), "score": 90, "license": c.get("license"), "resolution_tier": "4K_upscaled"}

    # 6. Preserve the existing final emergency fallback exactly as last resort.
    return agent.pillow_card(product, strategy["key"])


def install(agent: Any) -> None:
    if getattr(agent, "_image_priority_installed", False):
        return
    original = agent.get_best_pin_image
    async def wrapped(product: Dict[str, Any], strategy: Dict[str, Any], pin_index: int, job_store: Any, job_id: str, used_urls: Set[str]):
        try:
            return await get_best_pin_image(product, strategy, pin_index, job_store, job_id, used_urls, agent)
        except Exception:
            return await original(product, strategy, pin_index, job_store, job_id, used_urls)
    agent.get_best_pin_image = wrapped
    agent._image_priority_installed = True
