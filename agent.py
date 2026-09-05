"""
Autonomous Pinterest affiliate agent.

Flow:
  URL -> research -> SEO title/desc/keywords -> board -> image -> publish -> verify

Preserves the exact affiliate URL as the Pin destination link.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import io
import base64
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse, urljoin

import httpx

from models import JobStore

logger = logging.getLogger("pinterest-agent.core")

COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "").strip()
COMPOSIO_ENTITY_ID = os.getenv("COMPOSIO_ENTITY_ID", "default").strip() or "default"
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

DEFAULT_BOARD_NAME = "Product Pins"


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
# Research
# ---------------------------------------------------------------------------
async def research_product(url: str, job_store: JobStore, job_id: str) -> Dict[str, Any]:
    job_store.update(job_id, progress="Researching product page")

    product: Dict[str, Any] = {
        "source_url": url,
        "name": None,
        "description": None,
        "images": [],
        "site": urlparse(url).netloc.replace("www.", ""),
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

            # JSON-LD Product
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
                            img = obj.get("image")
                            if img:
                                if isinstance(img, list) and img:
                                    img = img[0]
                                if isinstance(img, dict):
                                    img = img.get("url") or img.get("contentUrl")
                                if isinstance(img, str) and img.startswith("http"):
                                    product["images"].append(img)
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
            if og_title and og_title.get("content") and (
                not product["name"] or len(product["name"]) < 8
            ):
                product["name"] = og_title["content"][:200]

            for prop in ("og:image", "og:image:secure_url", "twitter:image"):
                tag = soup.find("meta", attrs={"property": prop}) or soup.find(
                    "meta", attrs={"name": prop}
                )
                if tag and tag.get("content") and tag["content"].startswith("http"):
                    product["images"].append(tag["content"])
                    break

            if not product["images"]:
                img = soup.select_one(
                    "#landingImage, #imgTagWrapperId img, img[data-old-hires], "
                    "meta[itemprop=image], img.primary-image, img.product-image"
                )
                if img:
                    src = (
                        img.get("data-old-hires")
                        or img.get("content")
                        or img.get("src")
                        or ""
                    )
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

    # de-dupe images
    seen = set()
    clean_images = []
    for im in product["images"]:
        if im not in seen:
            seen.add(im)
            clean_images.append(im)
    product["images"] = clean_images

    return product


# ---------------------------------------------------------------------------
# SEO content (honest, non-invented)
# ---------------------------------------------------------------------------
def build_seo_content(product: Dict[str, Any]) -> Dict[str, str]:
    name = (product.get("name") or "Product").strip()
    desc = (product.get("description") or "").strip()
    site = product.get("site") or ""

    # Title: benefit-oriented but grounded in real name
    title = name
    if len(title) < 20:
        title = f"Shop {name}"
    if len(title) > 100:
        title = title[:97] + "..."

    # Keywords from real words only
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-']+", f"{name} {desc}")
    stop = {
        "the", "and", "for", "with", "from", "this", "that", "your", "you", "are",
        "was", "were", "have", "has", "been", "will", "can", "our", "not", "but",
        "all", "any", "out", "about", "into", "than", "then", "them", "they",
        "http", "https", "www", "com", "html",
    }
    keywords: List[str] = []
    for w in words:
        lw = w.lower()
        if len(lw) < 3 or lw in stop or lw in keywords:
            continue
        keywords.append(lw)
        if len(keywords) >= 8:
            break

    # Description: natural, useful, not stuffed
    body = desc[:280] if desc else f"Explore {name}."
    body = re.sub(r"\s+", " ", body).strip()
    if site:
        body = f"{body} Available via {site}."
    if keywords:
        body = f"{body} Ideas: {', '.join(keywords[:5])}."
    body = body[:500]

    return {
        "title": title[:100],
        "description": body,
        "keywords": ", ".join(keywords),
        "alt_text": f"{name} product image"[:500],
    }


# ---------------------------------------------------------------------------
# Image: product image preferred; else generate vertical card
# ---------------------------------------------------------------------------
async def obtain_image(product: Dict[str, Any], job_store: JobStore, job_id: str) -> Tuple[str, str]:
    """Returns (mode, value) where mode is 'url' or 'base64' and value is url or base64 data."""
    job_store.update(job_id, progress="Preparing Pinterest image")

    # 1) Prefer real product image
    for img_url in product.get("images") or []:
        try:
            async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
                r = await client.head(img_url)
                if r.status_code < 400:
                    return "url", img_url
                r = await client.get(img_url)
                if r.status_code < 400 and r.headers.get("content-type", "").startswith("image"):
                    return "url", img_url
        except Exception:
            continue

    # 2) Try OpenAI image if key present
    if OPENAI_API_KEY:
        try:
            job_store.update(job_id, progress="Generating image via OpenAI")
            name = product.get("name") or "product"
            prompt = (
                f"Professional vertical Pinterest marketing image (2:3) for: {name}. "
                f"Clean modern product-style composition, soft lighting, no watermarks, "
                f"no fake prices or fake ratings, high quality, commercial look."
            )
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
                if resp.status_code < 400:
                    data = resp.json()
                    url = data["data"][0]["url"]
                    return "url", url
        except Exception as e:
            logger.warning(f"OpenAI image failed: {e}")

    # 3) Generate simple vertical card with Pillow
    try:
        from PIL import Image, ImageDraw, ImageFont

        job_store.update(job_id, progress="Generating vertical card image")
        w, h = 1000, 1500
        img = Image.new("RGB", (w, h), (245, 240, 235))
        draw = ImageDraw.Draw(img)

        # Soft header band
        draw.rectangle([0, 0, w, 280], fill=(30, 30, 30))
        draw.rectangle([0, h - 160, w, h], fill=(30, 30, 30))

        name = (product.get("name") or "Product")[:60]
        site = product.get("site") or ""

        try:
            font_lg = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 48)
            font_sm = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
        except Exception:
            font_lg = ImageFont.load_default()
            font_sm = font_lg

        # Word-wrap title
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
            return "\n".join(lines[:4])

        title_text = wrap(name, 28)
        # Center-ish text
        draw.multiline_text((60, 600), title_text, fill=(25, 25, 25), font=font_lg, spacing=12)
        if site:
            draw.text((60, h - 100), f"Shop on {site}", fill=(220, 220, 220), font=font_sm)

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=90)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return "base64", b64
    except Exception as e:
        logger.warning(f"Pillow card failed: {e}")

    # 4) Last resort public placeholder
    seed = abs(hash(product.get("source_url") or "pin")) % 10000
    return "url", f"https://picsum.photos/seed/{seed}/1000/1500"


# ---------------------------------------------------------------------------
# Board
# ---------------------------------------------------------------------------
async def select_or_create_board(job_store: JobStore, job_id: str) -> str:
    job_store.update(job_id, progress="Selecting Pinterest board")
    data = await run_composio_tool("PINTEREST_LIST_BOARDS", {})
    items = data.get("items") or data.get("boards") or []
    if isinstance(data, list):
        items = data

    for b in items:
        name = (b.get("name") or "").lower()
        if any(k in name for k in ("product", "shop", "buy", "deal", "affiliate", "pin")):
            return str(b.get("id") or b.get("board_id"))
    if items:
        return str(items[0].get("id") or items[0].get("board_id"))

    job_store.update(job_id, progress="Creating Pinterest board")
    created = await run_composio_tool(
        "PINTEREST_CREATE_BOARD",
        {"name": DEFAULT_BOARD_NAME, "description": "Affiliate product pins", "privacy": "PUBLIC"},
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
) -> Dict[str, Any]:
    job_store.update(job_id, progress="Publishing Pin to Pinterest")

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
        raise RuntimeError(f"Pin created but no ID returned: {json.dumps(data)[:400]}")

    # Verify
    job_store.update(job_id, progress="Verifying published Pin")
    pin_url = f"https://www.pinterest.com/pin/{pin_id}/"
    verified = False
    try:
        verified_data = await run_composio_tool("PINTEREST_GET_PIN", {"pin_id": pin_id}, retries=1)
        if verified_data and (verified_data.get("id") or verified_data.get("link") is not None):
            verified = True
            # Prefer link from response if present for confirmation
            if verified_data.get("link"):
                # Do not replace affiliate URL; only confirm pin exists
                pass
    except Exception as e:
        logger.warning(f"Verify GET_PIN failed (pin may still exist): {e}")

    return {
        "pin_id": pin_id,
        "pin_url": pin_url,
        "verified": verified,
        "destination_url": link,
        "board_id": board_id,
        "title": title,
        "description": description,
    }


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------
async def process_pinterest_job(job_id: str, url: str, job_store: JobStore) -> Dict[str, Any]:
    """
    Autonomous single-pin workflow.
    Exact affiliate URL is always used as destination link.
    """
    logger.info(f"[{job_id}] Start URL={url}")

    # Normalize: allow "Pinterest https://..." style input
    url = url.strip()
    m = re.search(r"https?://\S+", url)
    if m:
        url = m.group(0).rstrip(").,]")
    if not url.startswith("http"):
        raise RuntimeError("A valid product/affiliate URL is required.")

    product = await research_product(url, job_store, job_id)
    content = build_seo_content(product)

    # Quality gate: require a name and preserved URL
    if not product.get("name"):
        raise RuntimeError("Could not extract product name; aborting to avoid low-quality pin.")

    board_id = await select_or_create_board(job_store, job_id)
    image_mode, image_value = await obtain_image(product, job_store, job_id)

    result = await publish_and_verify(
        board_id=board_id,
        title=content["title"],
        description=content["description"],
        alt_text=content["alt_text"],
        image_mode=image_mode,
        image_value=image_value,
        link=url,  # exact affiliate URL
        job_store=job_store,
        job_id=job_id,
    )

    return {
        "source_url": url,
        "product_name": product.get("name"),
        "keywords": content.get("keywords"),
        "image_mode": image_mode,
        "pins_published": 1,
        "pin": result,
        "pinterest_pin_url": result["pin_url"],
        "verified": result["verified"],
        "note": "Destination link is the exact affiliate/product URL provided.",
    }
