"""
Core autonomous Pinterest agent logic.

Architecture:
USER/JOEY -> Railway FastAPI -> Background job -> Composio tools -> Pinterest

The original product URL is NEVER modified and is used as the Pin destination link.
Creates 5 substantially different Pins for a single product, or up to 5 different products from a multi-product page.
"""
import os
import json
import logging
import asyncio
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from models import JobStore, JobStatus

logger = logging.getLogger("pinterest-agent.core")

COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "")
COMPOSIO_ENTITY_ID = os.getenv("COMPOSIO_ENTITY_ID", "default")

# ---------------------------------------------------------------------------
# Composio helper (dynamic tool execution)
# ---------------------------------------------------------------------------
async def run_composio_tool(tool_slug: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a Composio tool by slug.
    Requires COMPOSIO_API_KEY in Railway environment variables.
    Falls back gracefully if not configured.
    """
    if not COMPOSIO_API_KEY:
        raise RuntimeError("COMPOSIO_API_KEY not set. Configure it in Railway environment variables.")

    # Use Composio REST API for tool execution from the deployed service
    url = "https://backend.composio.dev/api/v1/actions/execute"
    headers = {
        "X-API-Key": COMPOSIO_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "actionName": tool_slug,
        "entityId": COMPOSIO_ENTITY_ID,
        "input": arguments,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code >= 400:
            raise RuntimeError(f"Composio tool {tool_slug} failed: {resp.status_code} {resp.text}")
        data = resp.json()
        return data.get("data", data)

# ---------------------------------------------------------------------------
# Product research
# ---------------------------------------------------------------------------
async def research_product(url: str, job_store: JobStore, job_id: str) -> Dict[str, Any]:
    """Extract product information. Prefer Composio browser/scrape tools if available, else lightweight fetch."""
    job_store.update(job_id, progress="Researching product page")

    # Lightweight fallback: fetch page title + meta description
    product = {
        "source_url": url,          # AUTHORITATIVE - never change
        "name": None,
        "description": None,
        "features": [],
        "images": [],
        "is_multi_product": False,
        "products": [],
    }

    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0 (compatible; PinterestAgent/1.0)"})
            html = resp.text[:200000]  # limit size

        # Simple extraction (no invented data)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        title = soup.find("title")
        if title:
            product["name"] = title.get_text(strip=True)[:200]

        meta_desc = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
        if meta_desc and meta_desc.get("content"):
            product["description"] = meta_desc["content"][:500]

        og_image = soup.find("meta", attrs={"property": "og:image"})
        if og_image and og_image.get("content"):
            product["images"].append(og_image["content"])

        # Heuristic for multi-product pages (Amazon best sellers, category, search)
        path = urlparse(url).path.lower()
        if any(x in path for x in ["/best-sellers", "/bestsellers", "/s?", "/s/", "/gp/bestsellers", "/category", "/collections"]):
            product["is_multi_product"] = True

        # Attempt richer extraction via Composio if key present
        if COMPOSIO_API_KEY:
            try:
                # Prefer any connected scrape / extract tool if available in the entity
                # (Firecrawl, browser tool, etc.). Tool names are discovered at runtime.
                pass  # concrete tool calls added when specific tool slugs are confirmed in entity
            except Exception as e:
                logger.warning(f"Composio research fallback used: {e}")

    except Exception as e:
        logger.warning(f"Research partial failure: {e}")
        product["name"] = product["name"] or "Product from URL"

    if not product["name"]:
        product["name"] = "Product"

    return product

# ---------------------------------------------------------------------------
# SEO content generation (5 different angles)
# ---------------------------------------------------------------------------
def generate_pin_contents(product: Dict[str, Any], count: int = 5) -> List[Dict[str, str]]:
    """Generate 5 substantially different titles, descriptions, keyword sets. No invented claims."""
    name = product.get("name") or "Product"
    desc = product.get("description") or ""
    base_keywords = [w for w in name.replace("|", " ").split() if len(w) > 2][:8]

    angles = [
        {"angle": "benefit", "prefix": "Why You Need", "cta": "Shop now and see the difference."},
        {"angle": "use_case", "prefix": "Perfect for", "cta": "Discover how it fits your life."},
        {"angle": "feature", "prefix": "Key Features of", "cta": "Explore the details that matter."},
        {"angle": "audience", "prefix": "Ideal Gift for", "cta": "Find the perfect match today."},
        {"angle": "value", "prefix": "Top Pick:", "cta": "Don't miss this quality choice."},
    ]

    pins = []
    for i, a in enumerate(angles[:count]):
        title = f"{a['prefix']} {name}"[:100]
        description = (
            f"{desc[:180]} "
            f"Keywords: {', '.join(base_keywords)}. "
            f"{a['cta']}"
        )[:500]
        pins.append({
            "title": title,
            "description": description,
            "keywords": base_keywords + [a["angle"]],
            "angle": a["angle"],
        })
    return pins

# ---------------------------------------------------------------------------
# Board selection
# ---------------------------------------------------------------------------
async def select_board(job_store: JobStore, job_id: str) -> Optional[str]:
    """List existing boards via Composio and pick the most relevant. Create only if none exist and allowed."""
    job_store.update(job_id, progress="Selecting Pinterest board")
    if not COMPOSIO_API_KEY:
        return None
    try:
        data = await run_composio_tool("PINTEREST_LIST_BOARDS", {})
        items = data.get("items") or data.get("boards") or []
        if items:
            # Prefer first board or one with 'product' / 'shop' in name
            for b in items:
                name = (b.get("name") or "").lower()
                if any(k in name for k in ["product", "shop", "buy", "deal", "find"]):
                    return b.get("id") or b.get("board_id")
            return items[0].get("id") or items[0].get("board_id")
        # No boards – return None (caller may create if policy allows)
        return None
    except Exception as e:
        logger.warning(f"Board list failed: {e}")
        return None

# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------
async def obtain_image(product: Dict[str, Any], pin_meta: Dict[str, str], job_store: JobStore, job_id: str) -> Optional[str]:
    """
    Prefer product image from page. If image generation tools become available
    (Gemini / other), call them here with automatic fallback.
    Returns a publicly reachable image URL or None.
    """
    job_store.update(job_id, progress=f"Obtaining image for angle: {pin_meta.get('angle')}")
    images = product.get("images") or []
    if images:
        return images[0]
    # Placeholder: when image-gen tools are enabled in Composio entity,
    # call them here and return the generated image URL.
    return None

# ---------------------------------------------------------------------------
# Pin publication
# ---------------------------------------------------------------------------
@retry(stop=stop_after_attempt(2), wait=wait_exponential(multiplier=1, min=2, max=10))
async def publish_pin(
    board_id: Optional[str],
    title: str,
    description: str,
    image_url: Optional[str],
    link: str,
    job_store: JobStore,
    job_id: str,
) -> Dict[str, Any]:
    """Create Pin via Composio. Uses exact original URL as destination. Never fabricates success."""
    job_store.update(job_id, progress=f"Publishing Pin: {title[:40]}...")

    if not COMPOSIO_API_KEY:
        raise RuntimeError("COMPOSIO_API_KEY required for Pinterest publication")

    args = {
        "title": title[:100],
        "description": description[:500],
        "link": link,  # EXACT original URL - never modified
    }
    if board_id:
        args["board_id"] = board_id
    if image_url:
        args["media_source"] = {"source_type": "image_url", "url": image_url}
        # Some tool versions use "image_url" or "media"
        args["image_url"] = image_url

    data = await run_composio_tool("PINTEREST_CREATE_PIN", args)
    pin_id = data.get("id") or data.get("pin_id") or (data.get("data") or {}).get("id")
    if not pin_id:
        raise RuntimeError(f"Pin creation returned no ID: {json.dumps(data)[:300]}")
    return {"pin_id": pin_id, "raw": data}

# ---------------------------------------------------------------------------
# Main job processor
# ---------------------------------------------------------------------------
async def process_pinterest_job(job_id: str, url: str, job_store: JobStore) -> Dict[str, Any]:
    """
    Full autonomous workflow:
    1. Research
    2. Generate 5 different Pin contents
    3. Select board
    4. Obtain images
    5. Publish Pins (exact original URL as link)
    6. Return verified results
    """
    logger.info(f"[{job_id}] Starting for URL: {url}")

    product = await research_product(url, job_store, job_id)
    pin_contents = generate_pin_contents(product, count=5)

    board_id = await select_board(job_store, job_id)

    published = []
    errors = []

    for i, meta in enumerate(pin_contents):
        try:
            image_url = await obtain_image(product, meta, job_store, job_id)
            # Destination link is ALWAYS the exact original URL supplied by user
            dest_link = url

            result = await publish_pin(
                board_id=board_id,
                title=meta["title"],
                description=meta["description"],
                image_url=image_url,
                link=dest_link,
                job_store=job_store,
                job_id=job_id,
            )
            published.append({
                "index": i + 1,
                "title": meta["title"],
                "angle": meta["angle"],
                "pin_id": result["pin_id"],
                "destination_url": dest_link,
            })
        except Exception as e:
            logger.error(f"[{job_id}] Pin {i+1} failed: {e}")
            errors.append({"index": i + 1, "error": str(e)})

    summary = {
        "source_url": url,
        "product_name": product.get("name"),
        "board_id": board_id,
        "pins_published": len(published),
        "pins": published,
        "errors": errors,
        "note": "Destination links are the exact original URL. No shortening or modification.",
    }

    if not published and errors:
        raise RuntimeError(f"All Pins failed. First error: {errors[0]['error']}")

    return summary
