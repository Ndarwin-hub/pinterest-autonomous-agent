"""
Autonomous multi-pin Pinterest affiliate agent (upgrade of existing system).

Preserves:
- Composio v3 tool execution
- Pinterest create/list/get pin + boards
- Exact affiliate URL as destination
- Railway job store integration

Adds:
- Resource registry (detect available providers)
- 5 unique pin strategies
- Image router: product page -> Pexels (Composio) -> OpenAI (if key) -> Pillow card
- Simple quality scoring + validation
- Publish + verify each pin independently
"""
from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import httpx

from models import JobStore

logger = logging.getLogger("pinterest-agent.core")

COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "").strip()
COMPOSIO_ENTITY_ID = os.getenv("COMPOSIO_ENTITY_ID", "default").strip() or "default"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
PIXABAY_API_KEY = os.getenv("PIXABAY_API_KEY", "").strip()
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "").strip()  # optional direct; Composio Pexels preferred
UNSPLASH_ACCESS_KEY = os.getenv("UNSPLASH_ACCESS_KEY", "").strip()

DEFAULT_BOARD_NAME = "Product Pins"
MAX_IMAGE_ATTEMPTS = 6
TARGET_IMAGE_SCORE = 70

STRATEGIES = [
    {"id": 1, "key": "hero", "name": "Product Hero", "focus": "clear product-focused presentation"},
    {"id": 2, "key": "problem", "name": "Problem / Solution", "focus": "everyday problem this product helps with"},
    {"id": 3, "key": "benefit", "name": "Key Benefit", "focus": "one verified benefit or feature"},
    {"id": 4, "key": "usecase", "name": "Audience / Use Case", "focus": "real-world use case and audience"},
    {"id": 5, "key": "discovery", "name": "Discovery / Inspiration", "focus": "inspiration and discovery angle"},
]


# ---------------------------------------------------------------------------
# Composio
# ---------------------------------------------------------------------------
async def run_composio_tool(tool_slug: str, arguments: Dict[str, Any], retries: int = 2) -> Dict[str, Any]:
    if not COMPOSIO_API_KEY:
        raise RuntimeError("COMPOSIO_API_KEY is not set in Railway variables.")

    url = f"https://backend.composio.dev/api/v3.1/tools/execute/{tool_slug}"
    headers = {"x-api-key": COMPOSIO_API_KEY, "Content-Type": "application/json"}
    payload = {
        "user_id": COMPOSIO_ENTITY_ID,
        "arguments": arguments or {},
        "version": "latest",
        "dangerously_skip_version_check": True,
    }

    last_err: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                try:
                    data = resp.json()
                except Exception:
                    data = {"raw": resp.text}

                if resp.status_code >= 400:
                    msg = (
                        data.get("error", {}).get("message")
                        if isinstance(data.get("error"), dict)
                        else data.get("message") or resp.text
                    )
                    raise RuntimeError(f"{tool_slug} HTTP {resp.status_code}: {msg}")

                if isinstance(data, dict) and data.get("successful") is False:
                    err = data.get("error") or data.get("data", {}).get("message") or str(data)
                    raise RuntimeError(f"{tool_slug} unsuccessful: {err}")

                if isinstance(data, dict) and "data" in data:
                    return data["data"] if data["data"] is not None else {}
                return data if isinstance(data, dict) else {"result": data}
        except Exception as e:
            last_err = e
            logger.warning(f"{tool_slug} attempt {attempt + 1} failed: {e}")
            if attempt >= retries:
                break
    raise RuntimeError(str(last_err) if last_err else f"{tool_slug} failed")


# ---------------------------------------------------------------------------
# Resource registry
# ---------------------------------------------------------------------------
def build_resource_registry() -> Dict[str, Any]:
    """Detect configured resources without exposing secrets."""
    reg = {
        "composio": {"available": bool(COMPOSIO_API_KEY), "capabilities": ["tool_execution"]},
        "pinterest": {"available": bool(COMPOSIO_API_KEY), "capabilities": ["create_pin", "list_boards", "get_pin"]},
        "pexels_composio": {"available": bool(COMPOSIO_API_KEY), "capabilities": ["image_search"]},
        "pexels_direct": {"available": bool(PEXELS_API_KEY), "capabilities": ["image_search"]},
        "pixabay": {"available": bool(PIXABAY_API_KEY), "capabilities": ["image_search"]},
        "unsplash": {"available": bool(UNSPLASH_ACCESS_KEY), "capabilities": ["image_search"]},
        "openai_images": {"available": bool(OPENAI_API_KEY), "capabilities": ["image_generation"]},
        "pillow": {"available": True, "capabilities": ["image_card"]},
        "product_page_images": {"available": True, "capabilities": ["product_reference"]},
    }
    return reg


# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------
async def research_product(url: str, job_store: JobStore, job_id: str) -> Dict[str, Any]:
    job_store.update(job_id, progress="Researching product page")

    product: Dict[str, Any] = {
        "source_url": url,
        "name": None,
        "description": None,
        "images": [],
        "category": "general",
        "site": urlparse(url).netloc.replace("www.", ""),
        "brand": None,
    }

    header_sets = [
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
        {"User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"},
    ]

    html = ""
    for headers in header_sets:
        try:
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code < 400 and len(resp.text) > 400:
                    html = resp.text[:300000]
                    break
        except Exception as e:
            logger.warning(f"Fetch failed: {e}")

    if html:
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(html, "lxml")

            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    ld = json.loads(script.string or "")
                    candidates = ld if isinstance(ld, list) else [ld]
                    for obj in candidates:
                        if not isinstance(obj, dict):
                            continue
                        t = obj.get("@type")
                        if t == "Product" or (isinstance(t, list) and "Product" in t):
                            if obj.get("name") and not product["name"]:
                                product["name"] = str(obj["name"])[:200]
                            if obj.get("description") and not product["description"]:
                                product["description"] = str(obj["description"])[:500]
                            brand = obj.get("brand")
                            if isinstance(brand, dict):
                                product["brand"] = brand.get("name")
                            elif isinstance(brand, str):
                                product["brand"] = brand
                            img = obj.get("image")
                            if img:
                                if isinstance(img, list) and img:
                                    img = img[0]
                                if isinstance(img, dict):
                                    img = img.get("url") or img.get("contentUrl")
                                if isinstance(img, str) and img.startswith("http"):
                                    product["images"].append(img)
                            cat = obj.get("category")
                            if isinstance(cat, str) and cat:
                                product["category"] = cat.lower()[:80]
                except Exception:
                    pass

            title = soup.find("title")
            if title and not product["name"]:
                name = title.get_text(strip=True)
                name = re.sub(r"\s*[:\|]\s*Amazon\.?com.*$", "", name, flags=re.I)
                name = re.sub(r"\s*-\s*Amazon\.com.*$", "", name, flags=re.I)
                name = re.sub(r"\s*\|\s*.*$", "", name)
                if "page not found" not in name.lower() and "not found" not in name.lower():
                    product["name"] = name[:200]

            meta_desc = soup.find("meta", attrs={"name": "description"}) or soup.find(
                "meta", attrs={"property": "og:description"}
            )
            if meta_desc and meta_desc.get("content") and not product["description"]:
                product["description"] = meta_desc["content"][:500]

            og_title = soup.find("meta", attrs={"property": "og:title"})
            if og_title and og_title.get("content") and (not product["name"] or len(product["name"]) < 8):
                product["name"] = og_title["content"][:200]

            for prop in ("og:image", "og:image:secure_url", "twitter:image"):
                tag = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
                if tag and tag.get("content") and tag["content"].startswith("http"):
                    product["images"].append(tag["content"])

            if not product["images"]:
                img = soup.select_one(
                    "#landingImage, #imgTagWrapperId img, img[data-old-hires], "
                    "meta[itemprop=image], img.primary-image, img.product-image"
                )
                if img:
                    src = img.get("data-old-hires") or img.get("content") or img.get("src") or ""
                    if src.startswith("//"):
                        src = "https:" + src
                    if src.startswith("http"):
                        product["images"].append(src)
        except Exception as e:
            logger.warning(f"Parse failed: {e}")

    if not product["name"] or "not found" in (product["name"] or "").lower():
        path = urlparse(url).path.strip("/")
        parts = [p for p in path.split("/") if p and p.lower() not in ("dp", "gp", "product", "listing", "p")]
        guess = parts[-1].replace("-", " ").replace("_", " ") if parts else product["site"]
        product["name"] = re.sub(r"\s+", " ", guess)[:80] or "Product"

    if not product["description"]:
        product["description"] = f"Discover {product['name']} — available now."

    # category heuristic from name
    name_l = (product["name"] or "").lower()
    for key, cat in [
        ("headphone", "audio"),
        ("earbud", "audio"),
        ("speaker", "audio"),
        ("phone", "electronics"),
        ("laptop", "electronics"),
        ("kitchen", "home"),
        ("cook", "home"),
        ("fashion", "fashion"),
        ("shoe", "fashion"),
        ("beauty", "beauty"),
        ("skin", "beauty"),
        ("fitness", "fitness"),
        ("yoga", "fitness"),
    ]:
        if key in name_l:
            product["category"] = cat
            break

    seen = set()
    clean = []
    for im in product["images"]:
        if im not in seen:
            seen.add(im)
            clean.append(im)
    product["images"] = clean[:8]
    return product


# ---------------------------------------------------------------------------
# SEO for 5 strategies
# ---------------------------------------------------------------------------
def build_five_seo(product: Dict[str, Any]) -> List[Dict[str, str]]:
    name = (product.get("name") or "Product").strip()
    desc = (product.get("description") or "").strip()
    site = product.get("site") or ""
    brand = product.get("brand") or ""

    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-']+", f"{name} {desc} {brand}")
    stop = {
        "the", "and", "for", "with", "from", "this", "that", "your", "you", "are",
        "was", "were", "have", "has", "been", "will", "can", "our", "not", "but",
        "all", "any", "out", "about", "into", "than", "then", "them", "they",
        "http", "https", "www", "com", "html", "amazon",
    }
    keywords: List[str] = []
    for w in words:
        lw = w.lower()
        if len(lw) < 3 or lw in stop or lw in keywords:
            continue
        keywords.append(lw)
        if len(keywords) >= 10:
            break

    short_name = name[:70]
    body_base = re.sub(r"\s+", " ", desc[:220]).strip() or f"Explore {short_name}."

    templates = [
        {
            "title": f"{short_name}"[:100],
            "description": f"{body_base} Shop the product page for full details."[:500],
            "angle": "hero",
        },
        {
            "title": f"Tired of settling? Try {short_name[:50]}"[:100],
            "description": f"Looking for a better everyday option? {body_base}"[:500],
            "angle": "problem",
        },
        {
            "title": f"Why people choose {short_name[:55]}"[:100],
            "description": f"Focus on what matters: {body_base}"[:500],
            "angle": "benefit",
        },
        {
            "title": f"Ideal for daily use: {short_name[:50]}"[:100],
            "description": f"Built for real routines. {body_base}"[:500],
            "angle": "usecase",
        },
        {
            "title": f"Discover {short_name[:60]}"[:100],
            "description": f"Save this for later. {body_base}"
            + (f" Available via {site}." if site else "")[:500],
            "angle": "discovery",
        },
    ]

    out = []
    for i, t in enumerate(templates):
        kw = keywords[i : i + 5] if keywords else []
        if not kw:
            kw = keywords[:5]
        d = t["description"]
        if kw:
            d = (d + f" Ideas: {', '.join(kw)}.")[:500]
        out.append(
            {
                "title": t["title"][:100],
                "description": d[:800],
                "keywords": ", ".join(kw),
                "alt_text": f"{name} — {t['angle']}"[:500],
                "strategy": STRATEGIES[i]["name"],
                "strategy_key": STRATEGIES[i]["key"],
            }
        )
    return out


# ---------------------------------------------------------------------------
# Image providers
# ---------------------------------------------------------------------------
async def _url_ok(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.head(url)
            if r.status_code < 400:
                return True
            r = await client.get(url)
            return r.status_code < 400 and "image" in r.headers.get("content-type", "")
    except Exception:
        return False


async def search_pexels_composio(query: str, orientation: str = "portrait") -> List[Dict[str, Any]]:
    try:
        data = await run_composio_tool(
            "PEXELS_SEARCH_PHOTOS",
            {"query": query[:80], "orientation": orientation, "per_page": 8, "page": 1},
            retries=1,
        )
        photos = data.get("photos") or data.get("items") or []
        results = []
        for p in photos:
            src = p.get("src") or {}
            url = src.get("large") or src.get("original") or src.get("portrait") or src.get("medium")
            if not url and isinstance(p.get("url"), str) and p["url"].startswith("http"):
                url = p["url"]
            if url:
                results.append(
                    {
                        "url": url,
                        "provider": "pexels_composio",
                        "id": str(p.get("id") or ""),
                        "photographer": p.get("photographer"),
                        "license": "Pexels License",
                    }
                )
        return results
    except Exception as e:
        logger.warning(f"Pexels Composio search failed: {e}")
        return []


async def search_pixabay(query: str) -> List[Dict[str, Any]]:
    if not PIXABAY_API_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                "https://pixabay.com/api/",
                params={
                    "key": PIXABAY_API_KEY,
                    "q": query[:100],
                    "image_type": "photo",
                    "orientation": "vertical",
                    "safesearch": "true",
                    "per_page": 10,
                },
            )
            if r.status_code >= 400:
                return []
            hits = r.json().get("hits") or []
            out = []
            for h in hits:
                url = h.get("largeImageURL") or h.get("webformatURL")
                if url:
                    out.append(
                        {
                            "url": url,
                            "provider": "pixabay",
                            "id": str(h.get("id") or ""),
                            "license": "Pixabay License",
                        }
                    )
            return out
    except Exception as e:
        logger.warning(f"Pixabay failed: {e}")
        return []


async def search_unsplash(query: str) -> List[Dict[str, Any]]:
    if not UNSPLASH_ACCESS_KEY:
        return []
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(
                "https://api.unsplash.com/search/photos",
                headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"},
                params={"query": query[:80], "orientation": "portrait", "per_page": 8},
            )
            if r.status_code >= 400:
                return []
            results = r.json().get("results") or []
            out = []
            for h in results:
                urls = h.get("urls") or {}
                url = urls.get("regular") or urls.get("full")
                if url:
                    out.append(
                        {
                            "url": url,
                            "provider": "unsplash",
                            "id": str(h.get("id") or ""),
                            "license": "Unsplash License",
                        }
                    )
            return out
    except Exception as e:
        logger.warning(f"Unsplash failed: {e}")
        return []


async def openai_generate(prompt: str) -> Optional[Dict[str, Any]]:
    if not OPENAI_API_KEY:
        return None
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/images/generations",
                headers={
                    "Authorization": f"Bearer {OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "dall-e-3",
                    "prompt": prompt[:1000],
                    "size": "1024x1792",
                    "n": 1,
                    "response_format": "url",
                },
            )
            if resp.status_code >= 400:
                return None
            url = resp.json()["data"][0]["url"]
            return {"url": url, "provider": "openai_dalle", "id": "", "license": "generated"}
    except Exception as e:
        logger.warning(f"OpenAI image failed: {e}")
        return None


def pillow_card(product: Dict[str, Any], strategy_key: str) -> Dict[str, Any]:
    from PIL import Image, ImageDraw, ImageFont

    w, h = 1000, 1500
    palettes = {
        "hero": ((245, 240, 235), (30, 30, 30)),
        "problem": ((236, 242, 248), (20, 40, 70)),
        "benefit": ((240, 248, 240), (20, 60, 30)),
        "usecase": ((248, 242, 236), (60, 35, 20)),
        "discovery": ((244, 240, 250), (40, 25, 60)),
    }
    bg, ink = palettes.get(strategy_key, ((245, 240, 235), (30, 30, 30)))
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w, 220], fill=ink)
    draw.rectangle([0, h - 140, w, h], fill=ink)

    name = (product.get("name") or "Product")[:60]
    site = product.get("site") or ""
    try:
        font_lg = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 44)
        font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 26)
    except Exception:
        font_lg = ImageFont.load_default()
        font_sm = font_lg

    def wrap(text: str, max_chars: int) -> str:
        words = text.split()
        lines, cur = [], ""
        for word in words:
            trial = (cur + " " + word).strip()
            if len(trial) <= max_chars:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return "\n".join(lines[:5])

    draw.multiline_text((60, 560), wrap(name, 26), fill=ink, font=font_lg, spacing=10)
    if site:
        draw.text((60, h - 90), f"Shop on {site}", fill=(230, 230, 230), font=font_sm)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"mode": "base64", "value": b64, "provider": "pillow_card", "id": strategy_key, "score": 55}


def score_candidate(candidate: Dict[str, Any], product: Dict[str, Any], strategy_key: str) -> int:
    score = 50
    provider = candidate.get("provider") or ""
    if provider == "product_page":
        score += 25  # real product visual accuracy
    if provider in ("pexels_composio", "pixabay", "unsplash"):
        score += 15
    if provider == "openai_dalle":
        score += 10
    if candidate.get("url") or candidate.get("mode") == "base64":
        score += 10
    if candidate.get("license"):
        score += 5
    # slight diversity bonus by strategy hash
    score += (hash(strategy_key + provider) % 7)
    return min(score, 100)


async def get_best_pin_image(
    product: Dict[str, Any],
    strategy: Dict[str, Any],
    pin_index: int,
    job_store: JobStore,
    job_id: str,
    used_urls: set,
) -> Dict[str, Any]:
    job_store.update(job_id, progress=f"Pin {pin_index}/5: finding best image ({strategy['name']})")
    name = product.get("name") or "product"
    query = f"{name} {strategy['focus']}"[:80]
    candidates: List[Dict[str, Any]] = []

    # 1) Real product images (prefer for hero / benefit)
    for img_url in product.get("images") or []:
        if img_url in used_urls:
            continue
        if await _url_ok(img_url):
            candidates.append(
                {"url": img_url, "provider": "product_page", "id": "", "license": "product_page"}
            )

    # 2) Configured stock APIs / Composio
    for searcher in (search_pexels_composio, search_pixabay, search_unsplash):
        try:
            found = await searcher(query)
            for f in found:
                if f.get("url") and f["url"] not in used_urls:
                    candidates.append(f)
        except Exception as e:
            logger.warning(f"Search error: {e}")

    # 3) OpenAI generation if available
    if OPENAI_API_KEY and len(candidates) < 2:
        prompt = (
            f"Professional vertical Pinterest image (2:3) for product: {name}. "
            f"Concept: {strategy['focus']}. Clean commercial style, no fake prices or ratings, "
            f"no watermarks, accurate product category look."
        )
        gen = await openai_generate(prompt)
        if gen:
            candidates.append(gen)

    # Score and pick best unused
    best = None
    best_score = -1
    for c in candidates:
        if c.get("url") in used_urls:
            continue
        s = score_candidate(c, product, strategy["key"])
        c["score"] = s
        if s > best_score:
            best_score = s
            best = c

    if best and best_score >= 50 and best.get("url"):
        if await _url_ok(best["url"]):
            used_urls.add(best["url"])
            return {
                "mode": "url",
                "value": best["url"],
                "provider": best.get("provider"),
                "id": best.get("id"),
                "score": best_score,
                "license": best.get("license"),
            }

    # Fallback pillow card
    card = pillow_card(product, strategy["key"])
    return card


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------
async def select_or_create_board(product: Dict[str, Any], job_store: JobStore, job_id: str) -> str:
    job_store.update(job_id, progress="Selecting Pinterest board")
    data = await run_composio_tool("PINTEREST_LIST_BOARDS", {})
    items = data.get("items") or data.get("boards") or []
    if isinstance(data, list):
        items = data

    category = (product.get("category") or "general").lower()
    keywords = [category, "product", "shop", "buy", "deal", "affiliate", "pin"]

    for b in items:
        name = (b.get("name") or "").lower()
        if any(k in name for k in keywords if k):
            return str(b.get("id") or b.get("board_id"))
    if items:
        return str(items[0].get("id") or items[0].get("board_id"))

    job_store.update(job_id, progress="Creating category board")
    board_name = {
        "audio": "Audio Gear",
        "electronics": "Electronics Finds",
        "home": "Home Essentials",
        "fashion": "Style Finds",
        "beauty": "Beauty Picks",
        "fitness": "Fitness Gear",
    }.get(category, DEFAULT_BOARD_NAME)[:50]

    created = await run_composio_tool(
        "PINTEREST_CREATE_BOARD",
        {"name": board_name, "description": f"Curated {category} product pins", "privacy": "PUBLIC"},
    )
    board_id = created.get("id") or (created.get("data") or {}).get("id")
    if not board_id:
        raise RuntimeError(f"Could not create board: {created}")
    return str(board_id)


# ---------------------------------------------------------------------------
# Publish + verify
# ---------------------------------------------------------------------------
async def publish_and_verify(
    board_id: str,
    title: str,
    description: str,
    alt_text: str,
    image_mode: str,
    image_value: str,
    link: str,
    job_store: JobStore,
    job_id: str,
    pin_index: int,
) -> Dict[str, Any]:
    job_store.update(job_id, progress=f"Publishing Pin {pin_index}/5")

    if image_mode == "base64":
        media_source = {
            "source_type": "image_base64",
            "content_type": "image/jpeg",
            "data": image_value,
        }
    else:
        media_source = {"source_type": "image_url", "url": image_value}

    args = {
        "board_id": board_id,
        "title": title[:100],
        "description": description[:800],
        "alt_text": alt_text[:500],
        "link": link,
        "media_source": media_source,
    }

    data = await run_composio_tool("PINTEREST_CREATE_PIN", args, retries=2)
    pin_id = str(data.get("id") or data.get("pin_id") or (data.get("data") or {}).get("id") or "")
    if not pin_id:
        raise RuntimeError(f"Pin created but no ID: {json.dumps(data)[:400]}")

    pin_url = f"https://www.pinterest.com/pin/{pin_id}/"
    verified = False
    try:
        verified_data = await run_composio_tool("PINTEREST_GET_PIN", {"pin_id": pin_id}, retries=1)
        if verified_data and verified_data.get("id"):
            verified = True
    except Exception as e:
        logger.warning(f"Verify failed for {pin_id}: {e}")

    return {
        "pin_id": pin_id,
        "pin_url": pin_url,
        "verified": verified,
        "destination_url": link,
        "board_id": board_id,
        "title": title,
    }


# ---------------------------------------------------------------------------
# Main workflow — 5 pins
# ---------------------------------------------------------------------------
async def process_pinterest_job(job_id: str, url: str, job_store: JobStore) -> Dict[str, Any]:
    logger.info(f"[{job_id}] Start URL={url}")

    url = url.strip()
    m = re.search(r"https?://\S+", url)
    if m:
        url = m.group(0).rstrip(").,]")
    if not url.startswith("http"):
        raise RuntimeError("A valid product/affiliate URL is required.")

    registry = build_resource_registry()
    available = [k for k, v in registry.items() if v.get("available")]
    job_store.update(job_id, progress=f"Resources: {', '.join(available)}")

    product = await research_product(url, job_store, job_id)
    seo_list = build_five_seo(product)
    board_id = await select_or_create_board(product, job_store, job_id)

    used_urls: set = set()
    published: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    resources_used: set = set()

    for i, strategy in enumerate(STRATEGIES):
        pin_no = i + 1
        try:
            image = await get_best_pin_image(product, strategy, pin_no, job_store, job_id, used_urls)
            resources_used.add(image.get("provider") or "unknown")
            seo = seo_list[i]
            result = await publish_and_verify(
                board_id=board_id,
                title=seo["title"],
                description=seo["description"],
                alt_text=seo["alt_text"],
                image_mode=image["mode"],
                image_value=image["value"],
                link=url,
                job_store=job_store,
                job_id=job_id,
                pin_index=pin_no,
            )
            published.append(
                {
                    "pin_number": pin_no,
                    "strategy": strategy["name"],
                    "image_provider": image.get("provider"),
                    "image_id": image.get("id"),
                    "image_score": image.get("score"),
                    "license": image.get("license"),
                    "title": seo["title"],
                    "keywords": seo.get("keywords"),
                    **result,
                }
            )
        except Exception as e:
            logger.error(f"Pin {pin_no} failed: {e}")
            errors.append({"pin_number": pin_no, "strategy": strategy["name"], "error": str(e)})

    if not published:
        raise RuntimeError(
            f"All pins failed. First error: {errors[0]['error'] if errors else 'unknown'}"
        )

    return {
        "product_name": product.get("name"),
        "source_url": url,
        "category": product.get("category"),
        "resources_available": available,
        "resources_used": sorted(resources_used),
        "pins_planned": 5,
        "pins_published": len(published),
        "board_id": board_id,
        "pins": published,
        "errors": errors,
        "note": "Destination links are the exact original URL for every pin.",
        "summary": f"{len(published)}/5 pins published",
    }
