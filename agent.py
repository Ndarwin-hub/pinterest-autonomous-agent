"""
Core autonomous Pinterest agent logic.

Architecture:
USER/JOEY -> Railway FastAPI -> Background job -> Composio tools -> Pinterest

The original product URL is NEVER modified and is used as the Pin destination link.
Creates 5 substantially different Pins for a single product.
"""
import os
import json
import logging
import re
from typing import Dict, Any, List, Optional
from urllib.parse import urlparse

import httpx

from models import JobStore

logger = logging.getLogger("pinterest-agent.core")

COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "").strip()
COMPOSIO_ENTITY_ID = os.getenv("COMPOSIO_ENTITY_ID", "default").strip() or "default"

# ---------------------------------------------------------------------------
# Composio helper (v3 API)
# ---------------------------------------------------------------------------
async def run_composio_tool(tool_slug: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    if not COMPOSIO_API_KEY:
        raise RuntimeError(
            "COMPOSIO_API_KEY is not set. "
            "In Railway Variables set COMPOSIO_API_KEY to a Project API Key with tool_execution write."
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
            msg = (
                data.get("error", {}).get("message")
                if isinstance(data.get("error"), dict)
                else data.get("message") or text
            )
            raise RuntimeError(f"Composio {tool_slug} failed ({resp.status_code}): {msg}")

        # Composio sometimes returns successful=false inside 200
        if isinstance(data, dict) and data.get("successful") is False:
            err = data.get("error") or data.get("data", {}).get("message") or str(data)
            raise RuntimeError(f"Composio {tool_slug} unsuccessful: {err}")

        if isinstance(data, dict):
            if "data" in data:
                return data["data"]
            return data
        return {"result": data}


# ---------------------------------------------------------------------------
# Product research
# ---------------------------------------------------------------------------
async def research_product(url: str, job_store: JobStore, job_id: str) -> Dict[str, Any]:
    job_store.update(job_id, progress="Researching product page")

    product: Dict[str, Any] = {
        "source_url": url,
        "name": None,
        "description": None,
        "features": [],
        "images": [],
        "is_multi_product": False,
        "products": [],
    }

    headers_list = [
        {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        },
        {
            "User-Agent": "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
        },
    ]

    html = ""
    for headers in headers_list:
        try:
            async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code < 400 and len(resp.text) > 500:
                    html = resp.text[:250000]
                    break
        except Exception as e:
            logger.warning(f"Fetch attempt failed: {e}")

    if html:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")

            title = soup.find("title")
            if title:
                name = title.get_text(strip=True)
                # Clean common Amazon suffixes
                name = re.sub(r"\s*[:|]\s*Amazon\.?com.*$", "", name, flags=re.I)
                name = re.sub(r"\s*-\s*Amazon\.com.*$", "", name, flags=re.I)
                if "page not found" not in name.lower() and "not found" not in name.lower():
                    product["name"] = name[:200]

            meta_desc = soup.find("meta", attrs={"name": "description"}) or soup.find(
                "meta", attrs={"property": "og:description"}
            )
            if meta_desc and meta_desc.get("content"):
                product["description"] = meta_desc["content"][:500]

            for prop in ("og:image", "og:image:secure_url"):
                og = soup.find("meta", attrs={"property": prop})
                if og and og.get("content") and og["content"].startswith("http"):
                    product["images"].append(og["content"])
                    break

            # Amazon-style image
            if not product["images"]:
                img = soup.select_one("#landingImage, #imgTagWrapperId img, img[data-old-hires]")
                if img:
                    src = img.get("data-old-hires") or img.get("src") or ""
                    if src.startswith("http"):
                        product["images"].append(src)
        except Exception as e:
            logger.warning(f"Parse failed: {e}")

    # Fallbacks from URL
    if not product["name"] or "not found" in (product["name"] or "").lower():
        path = urlparse(url).path
        # Try ASIN-style path
        parts = [p for p in path.split("/") if p and p not in ("dp", "gp", "product")]
        product["name"] = parts[-1].replace("-", " ")[:80] if parts else "Product"
        product["description"] = product["description"] or f"Discover this product: {url}"

    if not product["images"]:
        # Reliable placeholder so Pinterest accepts the pin
        product["images"].append("https://picsum.photos/seed/" + str(abs(hash(url)) % 10000) + "/1000/1500")

    return product


# ---------------------------------------------------------------------------
# SEO content
# ---------------------------------------------------------------------------
def generate_pin_contents(product: Dict[str, Any], count: int = 5) -> List[Dict[str, str]]:
    name = product.get("name") or "Product"
    desc = product.get("description") or ""
    base_keywords = [w for w in re.split(r"[\s\|\-,]+", name) if len(w) > 2][:8]

    angles = [
        {"angle": "benefit", "prefix": "Why You Need", "cta": "Shop now and see the difference."},
        {"angle": "use_case", "prefix": "Perfect for", "cta": "Discover how it fits your life."},
        {"angle": "feature", "prefix": "Key Features of", "cta": "Explore the details that matter."},
        {"angle": "audience", "prefix": "Ideal Gift for", "cta": "Find the perfect match today."},
        {"angle": "value", "prefix": "Top Pick:", "cta": "Don't miss this quality choice."},
    ]

    pins = []
    for a in angles[:count]:
        title = f"{a['prefix']} {name}"[:100]
        description = (
            f"{desc[:180]} Keywords: {', '.join(base_keywords)}. {a['cta']}"
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
    job_store.update(job_id, progress="Selecting Pinterest board")
    data = await run_composio_tool("PINTEREST_LIST_BOARDS", {})
    items = data.get("items") or data.get("boards") or []
    if not items and isinstance(data, list):
        items = data
    if items:
        for b in items:
            name = (b.get("name") or "").lower()
            if any(k in name for k in ["product", "shop", "buy", "deal", "pin"]):
                return str(b.get("id") or b.get("board_id"))
        return str(items[0].get("id") or items[0].get("board_id"))

    # Create a board
    job_store.update(job_id, progress="Creating Pinterest board")
    created = await run_composio_tool(
        "PINTEREST_CREATE_BOARD",
        {"name": "Product Pins", "description": "Auto-created by Pinterest Agent", "privacy": "PUBLIC"},
    )
    board_id = created.get("id") or (created.get("data") or {}).get("id")
    return str(board_id) if board_id else None


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------
async def publish_pin(
    board_id: Optional[str],
    title: str,
    description: str,
    image_url: Optional[str],
    link: str,
    job_store: JobStore,
    job_id: str,
) -> Dict[str, Any]:
    job_store.update(job_id, progress=f"Publishing Pin: {title[:40]}...")

    if not board_id:
        raise RuntimeError("No board_id available")

    args: Dict[str, Any] = {
        "board_id": board_id,
        "title": title[:100],
        "description": description[:500],
        "link": link,
        "media_source": {
            "source_type": "image_url",
            "url": image_url or "https://picsum.photos/1000/1500",
        },
    }

    data = await run_composio_tool("PINTEREST_CREATE_PIN", args)
    pin_id = (
        data.get("id")
        or data.get("pin_id")
        or (data.get("data") or {}).get("id")
    )
    if not pin_id:
        raise RuntimeError(f"Pin creation returned no ID: {json.dumps(data)[:400]}")
    return {"pin_id": str(pin_id), "raw": data}


# ---------------------------------------------------------------------------
# Main job processor
# ---------------------------------------------------------------------------
async def process_pinterest_job(job_id: str, url: str, job_store: JobStore) -> Dict[str, Any]:
    logger.info(f"[{job_id}] Starting for URL: {url}")

    product = await research_product(url, job_store, job_id)
    pin_contents = generate_pin_contents(product, count=5)
    board_id = await select_board(job_store, job_id)

    published = []
    errors = []
    images = product.get("images") or ["https://picsum.photos/1000/1500"]

    for i, meta in enumerate(pin_contents):
        try:
            image_url = images[i % len(images)]
            result = await publish_pin(
                board_id=board_id,
                title=meta["title"],
                description=meta["description"],
                image_url=image_url,
                link=url,
                job_store=job_store,
                job_id=job_id,
            )
            published.append({
                "index": i + 1,
                "title": meta["title"],
                "angle": meta["angle"],
                "pin_id": result["pin_id"],
                "destination_url": url,
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
        "note": "Destination links are the exact original URL.",
    }

    if not published:
        raise RuntimeError(f"All Pins failed. First error: {errors[0]['error'] if errors else 'unknown'}")

    return summary
