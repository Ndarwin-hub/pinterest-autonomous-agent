"""Canonical production identity + quality enforcement.
Loaded explicitly from main.py AFTER wire_board_org.
"""
from __future__ import annotations
import logging, re
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse
import agent as _agent
logger = logging.getLogger("pinterest-agent.quality")
QUALITY_PATCH_VERSION = "identity-gate-v2"
_GENERIC = {"amazon","amazon.com","amazon com","amazoncom","product","item","not found","product not found","unknown","unknown product","general","everything else","n/a","none","null",""}

def _clean(value: str) -> str:
    value = unquote(str(value or "")).replace("+"," ").replace("_"," ").replace("-"," ")
    value = re.sub(r"\s*[-|:]\s*Amazon(?:\.com)?\s*$", "", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip(" -|:")[:200]

def _is_asin(value: str) -> bool:
    v = re.sub(r"\s+", "", str(value or "").strip())
    return bool(re.fullmatch(r"[A-Z0-9]{10}", v, flags=re.I))

def _generic(value: str) -> bool:
    v = _clean(value).lower()
    if not v or v in _GENERIC or _is_asin(v): return True
    if v.startswith("amazon.com") or v.startswith("amazon "): return True
    if v in {"electronics","fashion","home","kitchen","shoes","sneakers"}: return True
    return False

def _amazon_slug(url: str) -> str:
    try: path = unquote(urlparse(url).path)
    except Exception: return ""
    m = re.search(r"/([^/]+)/dp/[A-Z0-9]{8,14}(?:[/?]|$)", path, flags=re.I)
    if not m: m = re.search(r"/([^/]+)/gp/product/[A-Z0-9]{8,14}(?:[/?]|$)", path, flags=re.I)
    if not m: return ""
    slug = _clean(m.group(1))
    slug = re.sub(r"\bB0[A-Z0-9]{8,12}\b", "", slug, flags=re.I)
    slug = re.sub(r"\s+", " ", slug).strip()
    if _is_asin(slug) or _generic(slug): return ""
    return slug[:200]

def _brand_from_name(name: str) -> str:
    parts = name.split()
    if not parts: return ""
    if parts[0].lower() in {"mens","women","womens","men","kids","new","the"} and len(parts)>1: return parts[1]
    return parts[0]

def _category(product):
    try:
        from board_org import detect_product_category
        return detect_product_category(product)
    except Exception:
        return str(product.get("category") or "general")

def assert_valid_identity(name: str, context: str = "") -> str:
    cleaned = _clean(name)
    if _generic(cleaned) or _is_asin(cleaned):
        raise RuntimeError(f"Product identity rejected ({context}): {name!r}. ASIN-only or generic names cannot be published.")
    if len(cleaned) < 6:
        raise RuntimeError(f"Product identity too short ({context}): {cleaned!r}")
    return cleaned

async def _research(url, job_store, job_id):
    product = await _agent._quality_original_research(url, job_store, job_id)
    current = _clean(product.get("name") or "")
    slug = _amazon_slug(url)
    if slug and (_generic(current) or _is_asin(current) or len(current) < 6):
        current = slug
    current = assert_valid_identity(current, context="research")
    product["name"] = current
    if not product.get("brand"): product["brand"] = _brand_from_name(current)
    product["category"] = _category(product)
    product["url"] = url
    product["identity_source"] = "url_slug" if slug and current == slug else "page_or_resolved"
    logger.info("QUALITY identity ok name=%r category=%s", product["name"], product["category"])
    return product

def _angle_copy(category, name, angle):
    cat = (category or "").lower()
    nl = name.lower()
    if cat == "fashion" or any(k in nl for k in ("shoe","sneaker","boot")):
        lines = {
            "hero": (name, f"{name} — everyday cushioned footwear. See sizes and details on the product listing."),
            "problem": (f"Need softer steps? Try {name[:50]}", f"Looking for more underfoot comfort? Explore {name} on the product listing."),
            "benefit": (f"Why shoppers pick {name[:45]}", f"Cushioning-focused design for all-day wear. Review {name} on the listing."),
            "usecase": (f"{name[:55]} for daily miles", f"From errands to easy movement — see {name} on the product page."),
            "discovery": (f"Discover {name[:60]}", f"Discover {name}. Check colors and sizes on the product listing."),
        }
        return lines[angle]
    if cat in {"audio"} or any(k in nl for k in ("airpods","headphone","earbud")):
        lines = {
            "hero": (name, f"{name} — wireless audio details on the product listing."),
            "problem": (f"Upgrading your earbuds? {name[:45]}", f"Considering a wireless audio upgrade? Review {name} on the listing."),
            "benefit": (f"Key details: {name[:55]}", f"See listed features for {name} on the product page."),
            "usecase": (f"{name[:50]} for everyday listening", f"Everyday wireless listening — check {name} on the listing."),
            "discovery": (f"Discover {name[:60]}", f"Discover {name}. Confirm the model on the product listing."),
        }
        return lines[angle]
    if cat in {"home"} or any(k in nl for k in ("instant pot","cooker","kitchen","air fryer")):
        lines = {
            "hero": (name, f"{name} — kitchen multi-cooker style appliance. See details on the product listing."),
            "problem": (f"One pot, fewer dishes: {name[:45]}", f"Want simpler weeknight cooking? Explore {name} on the listing."),
            "benefit": (f"Why consider {name[:50]}", f"Review listed features for {name} on the product page."),
            "usecase": (f"{name[:50]} for home cooking", f"Home cooking workflows — check {name} on the listing."),
            "discovery": (f"Discover {name[:60]}", f"Discover {name}. Confirm the exact model on the listing."),
        }
        return lines[angle]
    lines = {
        "hero": (name, f"Explore {name} and review current product details on the listing."),
        "problem": (f"Looking for {name[:50]}?", f"Considering {name}? Review the product listing before you buy."),
        "benefit": (f"Key details: {name[:55]}", f"See listed details for {name} on the product page."),
        "usecase": (f"{name[:55]} for everyday use", f"Everyday use for {name} — confirm details on the listing."),
        "discovery": (f"Discover {name[:60]}", f"Discover {name} and review the current listing information."),
    }
    return lines[angle]

def _seo(product):
    name = assert_valid_identity(product.get("name") or "", context="seo")
    category = _category(product)
    product["category"] = category
    tokens = [x.lower() for x in re.findall(r"[A-Za-z0-9][A-Za-z0-9'\-]+", name)]
    stop = {"the","and","for","with","from","amazon","com"}
    keywords = []
    for token in tokens:
        if len(token)>=3 and token not in stop and token not in keywords: keywords.append(token)
    out = []
    for i, angle in enumerate(["hero","problem","benefit","usecase","discovery"]):
        title, description = _angle_copy(category, name, angle)
        if _is_asin(title) or re.search(r"\bB0[A-Z0-9]{8}\b", title):
            raise RuntimeError(f"Title contains ASIN; refused: {title!r}")
        kw = keywords[max(0,i):max(0,i)+7] or keywords[:7]
        out.append({"title": title[:100], "description": (description + (f" Keywords: {', '.join(kw)}." if kw else ""))[:500], "keywords": ", ".join(kw), "alt_text": f"{name} — {angle} product view"[:500], "strategy": _agent.STRATEGIES[i]["name"], "strategy_key": _agent.STRATEGIES[i]["key"]})
    return out

def pre_publish_gate(product, seo_list, board_id, url):
    name = assert_valid_identity(product.get("name") or "", context="pre_publish")
    if not board_id: raise RuntimeError("Quality gate: missing board_id")
    cat = (product.get("category") or "").lower()
    if str(board_id) == "987906936951147704" and cat in {"home","fashion","electronics","electronics_root","health","health_root"}:
        raise RuntimeError(f"Quality gate: category {cat!r} must not use Everything Else board.")
    if not url or "amazon." not in url.lower(): raise RuntimeError("Quality gate: destination URL missing or not Amazon")
    if len(seo_list) != 5: raise RuntimeError(f"Quality gate: expected 5 SEO variants, got {len(seo_list)}")
    for i, seo in enumerate(seo_list):
        t = seo.get("title") or ""
        if _is_asin(t) or re.search(r"\bB0[A-Z0-9]{8}\b", t):
            raise RuntimeError(f"Quality gate: pin {i+1} title is ASIN-like: {t!r}")
    logger.info("QUALITY pre_publish_gate PASS name=%r board=%s", name, board_id)

async def _process(job_id, url, job_store):
    original_process = _agent._quality_original_process
    product = await _agent.research_product(url, job_store, job_id)
    assert_valid_identity(product.get("name") or "", context="process")
    seo_list = _agent.build_five_seo(product)
    board_id = await _agent.select_or_create_board(product, job_store, job_id)
    pre_publish_gate(product, seo_list, str(board_id), url)
    result = await original_process(job_id, url, job_store)
    pname = (result.get("product_name") or product.get("name") or "")
    assert_valid_identity(pname, context="result")
    for p in result.get("pins") or []:
        t = p.get("title") or ""
        if _is_asin(t) or re.search(r"\bB0[A-Z0-9]{8}\b", t):
            raise RuntimeError(f"Published pin has ASIN title; treating job as failed: {t!r}")
    if int(result.get("pins_published") or 0) < 5:
        raise RuntimeError(f"Incomplete publish: {result.get('pins_published')}/5 pins; refusing partial success.")
    result["quality_patch_version"] = QUALITY_PATCH_VERSION
    result["product_name"] = product["name"]
    result["category"] = product.get("category")
    return result

def install() -> str:
    if getattr(_agent, "_quality_patch_installed", False):
        logger.info("QUALITY patch already installed version=%s", QUALITY_PATCH_VERSION)
        return QUALITY_PATCH_VERSION
    if not hasattr(_agent, "_quality_original_research"):
        _agent._quality_original_research = _agent.research_product
    if not hasattr(_agent, "_quality_original_process"):
        _agent._quality_original_process = _agent.process_pinterest_job
    _agent.research_product = _research
    _agent.build_five_seo = _seo
    _agent.process_pinterest_job = _process
    _agent._quality_patch_installed = True
    _agent.QUALITY_PATCH_VERSION = QUALITY_PATCH_VERSION
    logger.info("QUALITY patch installed version=%s", QUALITY_PATCH_VERSION)
    return QUALITY_PATCH_VERSION
