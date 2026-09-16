"""Production quality patch loaded before main.py.

Repairs product identity when Amazon blocks page HTML and makes SEO/category
content product-specific. It intentionally fails closed instead of publishing
pins built around a generic site name.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse

import agent as _agent

_GENERIC_NAMES = {
    "amazon", "amazon.com", "amazon com", "amazoncom", "product", "item",
    "not found", "product not found", "unknown", "unknown product", "",
}


def _clean_name(value: str) -> str:
    value = unquote(str(value or ""))
    value = value.replace("+", " ").replace("_", " ")
    value = re.sub(r"\s*[-|:]\s*Amazon(?:\.com)?\s*$", "", value, flags=re.I)
    value = re.sub(r"\s+", " ", value).strip(" -|:")
    return value[:200]


def _generic(value: str) -> bool:
    v = _clean_name(value).lower()
    return v in _GENERIC_NAMES or v.startswith("amazon.com ") or v.startswith("amazon ")


def _amazon_slug(url: str) -> str:
    """Extract the human-readable Amazon title slug before /dp/ASIN."""
    try:
        path = unquote(urlparse(url).path)
    except Exception:
        return ""
    m = re.search(r"/([^/]+)/dp/[A-Z0-9]{8,14}(?:[/?]|$)", path, flags=re.I)
    if not m:
        m = re.search(r"/([^/]+)/gp/product/[A-Z0-9]{8,14}(?:[/?]|$)", path, flags=re.I)
    if not m:
        return ""
    slug = _clean_name(m.group(1))
    # Remove common Amazon URL noise while retaining the actual product words.
    slug = re.sub(r"\b(B0[A-Z0-9]{8,12})\b", "", slug, flags=re.I)
    slug = re.sub(r"\s+", " ", slug).strip()
    return slug[:200]


def _brand_from_name(name: str) -> str:
    parts = name.split()
    if not parts:
        return ""
    # Most Amazon product slugs begin with the brand. Avoid treating generic
    # article/descriptor words as brands.
    if parts[0].lower() in {"mens", "women", "womens", "men", "kids", "new", "the"} and len(parts) > 1:
        return parts[1]
    return parts[0]


def _category(product: Dict[str, Any]) -> str:
    try:
        from board_org import detect_product_category
        return detect_product_category(product)
    except Exception:
        return str(product.get("category") or "general")


async def _research(url: str, job_store: Any, job_id: str) -> Dict[str, Any]:
    product = await _agent._quality_original_research(url, job_store, job_id)
    current = _clean_name(product.get("name") or "")
    slug = _amazon_slug(url)
    if slug and (_generic(current) or len(current) < 4):
        current = slug
    if _generic(current):
        # Never let a website/store name become the product identity. This is a
        # deliberate fail-closed condition; publishing an unrelated product is worse
        # than refusing one job.
        raise RuntimeError("Product identity could not be established from the page or Amazon URL slug; refusing to publish generic product content.")
    product["name"] = current
    if not product.get("brand"):
        product["brand"] = _brand_from_name(current)
    product["category"] = _category(product)
    product["url"] = url
    return product


def _angle_copy(category: str, name: str, angle: str) -> tuple[str, str]:
    if category == "fashion":
        lines = {
            "hero": (f"{name} | Product Details", f"Explore {name} and see the product details, styling context, and availability."),
            "problem": (f"Everyday Comfort: {name}", f"Looking for an everyday footwear option? Explore {name} and review its product details before buying."),
            "benefit": (f"{name} for Everyday Wear", f"See {name} up close and compare the product details that matter for everyday wear."),
            "usecase": (f"{name} | Daily Wear Idea", f"Explore {name} as a daily-wear option and review the listing for the exact model and details."),
            "discovery": (f"Discover {name}", f"Discover {name}, review the available details, and visit the product listing for current information."),
        }
        return lines[angle]
    if category in {"audio"}:
        lines = {
            "hero": (f"{name} | Product Details", f"Explore {name} and review the product listing for current specifications and availability."),
            "problem": (f"Explore {name}", f"Considering {name}? Review the product details and current listing information before buying."),
            "benefit": (f"{name} | Key Details", f"See {name} and review the listed features, specifications, and availability."),
            "usecase": (f"{name} | Everyday Use", f"Explore {name} for its intended use and check the listing for the exact product details."),
            "discovery": (f"Discover {name}", f"Discover {name} and review the current product information on the listing."),
        }
        return lines[angle]
    if category == "beauty" or category == "health_beauty_personal":
        prefix = "Beauty & Personal Care"
    elif category in {"health", "health_root"}:
        prefix = "Health & Fitness"
    elif category in {"home"}:
        prefix = "Home & Kitchen"
    elif category in {"electronics", "electronics_root", "electronics_smartphones", "electronics_pc_home"}:
        prefix = "Electronics"
    elif category in {"pets"}:
        prefix = "Pet Supplies"
    else:
        prefix = "Product"
    titles = {
        "hero": f"{name} | Product Details",
        "problem": f"Explore {name}",
        "benefit": f"{name} | Key Details",
        "usecase": f"{name} | Everyday Use",
        "discovery": f"Discover {name}",
    }
    descriptions = {
        "hero": f"Explore {name} and review the current product details and availability.",
        "problem": f"Considering {name}? Review the product information and exact listing before buying.",
        "benefit": f"See {name} and review the listed specifications and product details.",
        "usecase": f"Explore {name} in its intended context and check the listing for exact product information.",
        "discovery": f"Discover {name}, a {prefix.lower()} product, and review the current listing information.",
    }
    return titles[angle], descriptions[angle]


def _seo(product: Dict[str, Any]) -> List[Dict[str, str]]:
    name = _clean_name(product.get("name") or "")
    if _generic(name):
        raise RuntimeError("SEO generation refused because product identity is generic.")
    category = _category(product)
    product["category"] = category
    tokens = [x.lower() for x in re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]+", name)]
    stop = {"the", "and", "for", "with", "from", "amazon", "com"}
    keywords = []
    for token in tokens:
        if len(token) >= 3 and token not in stop and token not in keywords:
            keywords.append(token)
    angles = ["hero", "problem", "benefit", "usecase", "discovery"]
    out: List[Dict[str, str]] = []
    for i, angle in enumerate(angles):
        title, description = _angle_copy(category, name, angle)
        kw = keywords[max(0, i):max(0, i) + 7] or keywords[:7]
        out.append({
            "title": title[:100],
            "description": description[:500],
            "keywords": ", ".join(kw),
            "alt_text": f"{name} — {angle} product view"[:500],
            "strategy": _agent.STRATEGIES[i]["name"],
            "strategy_key": _agent.STRATEGIES[i]["key"],
        })
    return out


# Preserve the original research implementation for the wrapper.
_agent._quality_original_research = _agent.research_product
_agent.research_product = _research
_agent.build_five_seo = _seo
