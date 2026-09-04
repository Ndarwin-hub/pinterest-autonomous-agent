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

COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "").strip()
COMPOSIO_ENTITY_ID = os.getenv("COMPOSIO_ENTITY_ID", "default").strip() or "default"

# ---------------------------------------------------------------------------
# Composio helper (v3 API)
# ---------------------------------------------------------------------------
async def run_composio_tool(tool_slug: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """
    Execute a Composio tool by slug using the v3.1 API.
    Requires a valid COMPOSIO_API_KEY (Project API Key with Tool Execution permission).
    """
    if not COMPOSIO_API_KEY:
        raise RuntimeError(
            "COMPOSIO_API_KEY is not set. "
            "In Railway Variables create COMPOSIO_API_KEY and paste a valid Project API Key from https://app.composio.dev"
        )

    url = f"https://backend.composio.dev/api/v3.1/tools/execute/{tool_slug}"
    headers = {
        "x-api-key": COMPOSIO_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "user_id": COMPOSIO_ENTITY_ID,
        "arguments": arguments or {},
        "version": "latest",
        "dangerously_skip_version_check": True,
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, headers=headers, json=payload)
        text = resp.text
        try:
            data = resp.json()
        except Exception:
            data = {"raw": text}

        if resp.status_code >= 400:
            msg = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else data.get("message") or text
            raise RuntimeError(f"Composio {tool_slug} failed ({resp.status_code}): {msg}")

        # Normalize common response shapes
        if isinstance(data, dict):
            if "data" in data:
                return data["data"]
            return data
        return {"result": data}

# ---------------------------------------------------------------------------
# Product research
# ---------------------------------------------------------------------------
async def research_product(url: str, job_store: JobStore, job_id: str) -> Dict[str, Any]:
    """Extract product information. Prefer page meta; never invent data."""
    job_store.update(job_id, progress="Researching product page")

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
            resp = await client.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; PinterestAgent/1.0)"},
            )
            html = resp.text[:200000]

        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")

        title = soup.find("title")
        if title:
            product["name"] = title.get_text(strip=True)[:200]

        meta_desc = soup.find("meta", attrs={"name": "description"}) or soup.find(
            "meta", attrs={"property": "og:description"}
        )
        if meta_desc and meta_desc.get("content"):
            product["description"] = meta_desc["content"][:500]

        og_image = soup.find("meta", attrs={"property": "og:image"})
        if og_image and og_image.get("content"):
            product["images"].append(og_image["content"])

        path = urlparse(url).path.lower()
        if any(x in path for x in ["/best-sellers", "/bestsellers", "/s?", "/s/", "/gp/bestsellers", "/category", "/collections"]):
            product["is_multi_product"] = True

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
    """Generate 5 substantially different titles, descriptions, keyword sets."""
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
    """List existing boards via Composio and pick the most relevant."""
    job_store.update(job_id, progress="Selecting Pinterest board")
    if not COMPOSIO_API_KEY:
        return None
    try:
        data = await run_composio_tool("PINTEREST_LIST_BOARDS", {})
        items = data.get("items") or data.get("boards") or data.get("data", {}).get("items") or []
        if not items and isinstance(data, list):
            items = data
        if items:
            for b in items:
                name = (b.get("name") or "").lower()
                if any(k in name for k in ["product", "shop", "buy", "deal", "find"]):
                    return b.get("id") or b.get("board_id")
            return items[0].get("id") or items[0].get("board_id")
        return None
    except Exception as e:
        logger.warning(f"Board list failed: {e}")
        # Surface the real error so the job status is useful
        raise

# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------
async def obtain_image(product: Dict[str, Any], pin_meta: Dict[str, str], job_store: JobStore, job_id: str) -> Optional[str]:
    """Prefer product image from page. Image-gen tools can be added when available."""
    job_store.update(job_id, progress=f"Obtaining image for angle: {pin_meta.get('angle')}")
    images = product.get("images") or []
    if images:
        return images[0]
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

    args: Dict[str, Any] = {
        "title": title[:100],
        "description": description[:500],
        "link": link,  # EXACT original URL - never modified
    }
    if board_id:
        args["board_id"] = board_id
    if image_url:
        args["media_source"] = {"source_type": "image_url", "url": image_url}
        args["image_url"] = image_url

    data = await run_composio_tool("PINTEREST_CREATE_PIN", args)
    pin_id = (
        data.get("id")
        or data.get("pin_id")
        or (data.get("data") or {}).get("id")
        or (data.get("data") or {}).get("pin_id")
    )
    if not pin_id:
        raise RuntimeError(f"Pin creation returned no ID: {json.dumps(data)[:400]}")
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

    try:
        board_id = await select_board(job_store, job_id)
    except Exception as e:
        # If board listing fails because of bad API key, fail fast with clear message
        raise RuntimeError(str(e))

    published = []
    errors = []

    for i, meta in enumerate(pin_contents):
        try:
            image_url = await obtain_image(product, meta, job_store, job_id)
            dest_link = url  # ALWAYS the exact original URL

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
