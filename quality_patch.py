"""Canonical production identity + quality enforcement.

install_identity() MUST run before wire_board_org.apply_agent_wiring so the
wire layer closes over the identity-aware research function.
install_process_gate() runs after wiring to post-validate results.
"""
from __future__ import annotations
import logging, re
from typing import Any, Dict, List
from urllib.parse import unquote, urlparse

logger = logging.getLogger("pinterest-agent.quality")
QUALITY_PATCH_VERSION = "seo-intent-v4"
_GENERIC = {"amazon","amazon.com","amazon com","amazoncom","product","item","not found","product not found","unknown","unknown product","general","everything else","n/a","none","null",""}

def _clean(value: str) -> str:
    value = unquote(str(value or "")).replace("+"," ").replace("_"," ").replace("-"," ")
    # Strip storefront chrome from either end; do not treat real product titles as generic.
    value = re.sub(r"^Amazon(?:\.com)?\s*[:|\-]\s*", "", value, flags=re.I)
    value = re.sub(r"\s*[-|:]\s*Amazon(?:\.com)?\s*$", "", value, flags=re.I)
    value = re.sub(r"\s*[:|\-]\s*Home\s*&\s*Kitchen\s*$", "", value, flags=re.I)
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

def install_identity(agent_mod) -> str:
    """Patch research + SEO on agent BEFORE wire_board_org closes over them."""
    if getattr(agent_mod, "_quality_identity_installed", False):
        return QUALITY_PATCH_VERSION

    orig_research = agent_mod.research_product

    async def _research(url, job_store, job_id):
        # Resolve Amazon short URLs (amzn.to etc.) to canonical US /dp/ASIN
        # BEFORE identity research so slug extraction and page scrape succeed.
        resolved_url = url
        try:
            from amazon_url import resolve_amazon_product_url, is_amazon_short_url, extract_asin_from_url
            if is_amazon_short_url(url) or (
                "amazon." in (url or "").lower() and not extract_asin_from_url(url)
            ):
                resolved_url = resolve_amazon_product_url(url)
                logger.info("QUALITY resolved Amazon URL %s -> %s", url, resolved_url)
            elif extract_asin_from_url(url):
                # Already has ASIN; still canonicalize tag + clean path
                resolved_url = resolve_amazon_product_url(url)
        except Exception as e:
            # Fail closed only when the input was a short/ambiguous Amazon link
            from amazon_url import is_amazon_short_url
            if is_amazon_short_url(url):
                raise RuntimeError(
                    f"Amazon short URL could not be resolved to a product page: {url!r} ({e})"
                ) from e
            logger.warning("Amazon URL resolve skipped/non-fatal for %s: %s", url, e)
            resolved_url = url

        product = await orig_research(resolved_url, job_store, job_id)
        current = _clean(product.get("name") or "")
        slug = _amazon_slug(resolved_url)
        if slug and (_generic(current) or _is_asin(current) or len(current) < 6):
            current = slug
        # Amazon anti-bot/interstitial pages can expose only "Amazon.com" as the
        # scraped title. Recover identity from Composio image-search metadata using
        # the exact ASIN before rejecting the product. This keeps the pipeline fail-closed
        # while avoiding false rejection of a valid Amazon product URL.
        try:
            from amazon_url import extract_asin_from_url
            asin = extract_asin_from_url(resolved_url)
            if asin and (_generic(current) or _is_asin(current) or len(current) < 6):
                data = await agent_mod.run_composio_tool(
                    "COMPOSIO_SEARCH_IMAGE",
                    {"query": f"{asin} Amazon product", "num_images": 5},
                    retries=1,
                )
                results = ((data or {}).get("images_results") or []) if isinstance(data, dict) else []
                for candidate in results:
                    title = _clean(candidate.get("title") or "")
                    link = str(candidate.get("link") or "")
                    if title and asin in link and not _generic(title) and not _is_asin(title):
                        title = re.sub(r"^Amazon(?:\.com)?\s*:\s*", "", title, flags=re.I).strip()
                        if len(title) >= 6 and not _generic(title):
                            current = title
                            break
        except Exception as e:
            logger.warning("QUALITY ASIN identity recovery skipped: %s", e)
        current = assert_valid_identity(current, context="research")
        product["name"] = current
        if not product.get("brand"):
            product["brand"] = _brand_from_name(current)
        product["category"] = _category(product)
        # Pin destination MUST be the canonical affiliate URL
        product["url"] = resolved_url
        product["source_url"] = url
        product["affiliate_url"] = resolved_url
        logger.info("QUALITY identity ok name=%r category=%s url=%s", product["name"], product["category"], resolved_url)
        return product


    # --- search-intent SEO (metadata-driven; independent of board rules) ---
    _PTYPE_MAP = [
        ("office chair", ("office chair","executive chair","desk chair","ergonomic chair","task chair")),
        ("running shoes", ("running shoe","sneaker","sneakers","athletic shoe","trainers")),
        ("wireless earbuds", ("airpods","earbuds","earbud","true wireless")),
        ("e-reader", ("kindle","paperwhite","e-reader","ereader")),
        ("acne patches", ("acne patch","pimple patch","mighty patch","hydrocolloid")),
        ("protein powder", ("whey protein","protein powder")),
        ("tumbler", ("stanley","quencher","tumbler")),
        ("ice maker", ("ice maker","ice machine")),
        ("ice pack", ("ice pack","cold pack","reusable ice")),
        ("baby wipes", ("baby wipe","baby wipes")),
        ("cat litter", ("cat litter","litter box")),
        ("screen protector", ("screen protector","screen guard")),
        ("laptop charger", ("laptop charger","usb-c charger","usb c charger","charging cable")),
        ("gaming headset", ("gaming headset","gaming headphones")),
        ("microphone", ("lavalier","wireless mic","microphone")),
        ("writing tablet", ("lcd writing","writing tablet","drawing tablet")),
    ]
    _PHRASES = {
        "office chair": ["executive office chair","comfortable desk chair for home office","high-back office chair for work","ergonomic office chair for long hours","home office seating for focused work"],
        "running shoes": ["cushioned running shoes for daily miles","everyday athletic sneakers for walking","comfortable running shoes for training","supportive sneakers for active days","lightweight running shoes for errands"],
        "wireless earbuds": ["wireless earbuds for everyday listening","true wireless earbuds for commute","comfortable wireless earbuds for calls","compact earbuds for daily use","wireless audio for music and podcasts"],
        "e-reader": ["e-reader for comfortable reading","lightweight e-reader for travel","digital reading device for books","paper-like e-reader for long sessions","portable e-reader for daily reading"],
        "acne patches": ["acne patches for overnight spot care","hydrocolloid pimple patches","discreet acne patches for daily wear","spot treatment patches for blemishes","overnight pimple patches for clear skin"],
        "protein powder": ["whey protein powder for workouts","protein powder for muscle recovery","everyday protein shake mix","post-workout protein powder","protein powder for fitness goals"],
        "tumbler": ["insulated tumbler for all-day drinks","travel tumbler for hot and cold","large insulated water tumbler","everyday insulated drinkware","portable tumbler for work and travel"],
        "ice maker": ["countertop ice maker for home","compact ice machine for kitchen","portable ice maker for parties","fast countertop ice maker","home ice maker for everyday use"],
        "ice pack": ["reusable ice packs for injuries","cold pack for sports recovery","flexible ice pack for first aid","reusable cold packs for pain relief","ice packs for everyday injuries"],
        "baby wipes": ["gentle baby wipes for sensitive skin","everyday baby wipes for diaper changes","soft baby wipes for newborns","fragrance-free baby wipes","baby wipes for on-the-go care"],
        "cat litter": ["clumping cat litter for easy cleanup","low-dust cat litter for home","odor-control cat litter","everyday cat litter for multi-cat homes","cat litter for reliable odor control"],
        "screen protector": ["screen protector for clear display protection","tempered glass screen protector","phone screen protector for daily use","scratch-resistant screen protector","screen protector for everyday protection"],
        "laptop charger": ["USB-C laptop charger for travel","compact laptop charging cable","reliable USB-C charger for work","portable laptop charger for desk and bag","USB-C charging cable for everyday use"],
        "gaming headset": ["gaming headset for clear communication","comfortable gaming headphones for long sessions","headset for multiplayer gaming","gaming headset with microphone","immersive gaming headset for PC and console"],
        "microphone": ["wireless lavalier microphone for content","clip-on mic for clear audio","portable microphone for video and calls","lavalier mic for creators","wireless mic for everyday recording"],
        "writing tablet": ["LCD writing tablet for kids","reusable drawing tablet for children","portable writing tablet for doodling","kids drawing tablet for travel","erasable writing tablet for practice"],
    }
    _GENERIC_PHRASES = ["practical everyday product for home use","useful option for daily routines","reliable product for home and work","simple choice for everyday needs","everyday product worth considering"]

    def _infer_ptype(name, category):
        nl = name.lower()
        for ptype, hints in _PTYPE_MAP:
            if any(h in nl for h in hints):
                return ptype
        return "product"

    def _phrases(ptype):
        ps = list(_PHRASES.get(ptype, _GENERIC_PHRASES))
        while len(ps) < 5:
            ps.append(_GENERIC_PHRASES[len(ps) % len(_GENERIC_PHRASES)])
        return ps[:5]

    def _short_brand(brand, name):
        b = (brand or "").strip()
        if not b:
            return ""
        if len(b) > 28:
            b = b[:28].rsplit(" ", 1)[0]
        if b.lower() in {"lcd","usb","usb-c","reusable","wireless","portable","countertop","original","mens","womens","kids","new","the","for","with"}:
            return ""
        return b

    def _title(phrase, brand, name, angle):
        b = _short_brand(brand, name)
        title = phrase[0].upper() + phrase[1:] if phrase else name[:60]
        if b and b.lower() not in title.lower():
            sep = " | " if angle in ("hero","problem","usecase") else (" · " if angle == "benefit" else " — ")
            title = f"{title}{sep}{b}"
        title = re.sub(r"\s+", " ", title).strip()[:100]
        if title.strip().lower() == name.strip().lower():
            title = f"{phrase} | product details"[:100]
        return title

    def _desc(phrase, name, brand, angle):
        b = _short_brand(brand, name)
        open_ = {
            "hero": f"Looking for {phrase}? This option is built for everyday use.",
            "problem": f"If you need {phrase}, this is worth a closer look.",
            "benefit": f"{phrase.capitalize()} can make daily routines simpler.",
            "usecase": f"Whether at home or on the go, {phrase} fits real routines.",
            "discovery": f"Discover {phrase} designed for practical everyday needs.",
        }.get(angle, f"Explore {phrase}.")
        mid = f" From {b}." if b else ""
        mid += " Review the full product details and current options on the listing."
        close = " Confirm size, color, and compatibility before you buy."
        return re.sub(r"\s+", " ", open_ + mid + close).strip()[:500]

    def _seo(product):
        name = assert_valid_identity(product.get("name") or "", context="seo")
        category = _category(product)
        product["category"] = category
        brand = (product.get("brand") or _brand_from_name(name) or "").strip()
        ptype = _infer_ptype(name, category)
        phrases = _phrases(ptype)
        angles = ["hero","problem","benefit","usecase","discovery"]
        out = []
        used = set()
        for i, angle in enumerate(angles):
            phrase = phrases[i]
            title = _title(phrase, brand, name, angle)
            n = 0
            while title.lower() in used and n < 3:
                n += 1
                title = f"{phrase} · option {n+1}"[:100]
            used.add(title.lower())
            if _is_asin(title) or re.search(r"\bB0[A-Z0-9]{8}\b", title):
                raise RuntimeError(f"Title contains ASIN; refused: {title!r}")
            description = _desc(phrase, name, brand, angle)
            kw_tokens = re.findall(r"[A-Za-z0-9][A-Za-z0-9\-']+", phrase.lower())
            kw = ", ".join(list(dict.fromkeys(kw_tokens))[:6])
            out.append({
                "title": title[:100],
                "description": description[:800],
                "keywords": kw,
                "alt_text": f"{name} - {angle} product view"[:500],
                "strategy": agent_mod.STRATEGIES[i]["name"],
                "strategy_key": agent_mod.STRATEGIES[i]["key"],
            })
        return out

    agent_mod.research_product = _research
    agent_mod.build_five_seo = _seo
    agent_mod._quality_identity_installed = True
    agent_mod.QUALITY_PATCH_VERSION = QUALITY_PATCH_VERSION
    logger.info("QUALITY identity installed version=%s (before wire)", QUALITY_PATCH_VERSION)
    return QUALITY_PATCH_VERSION


def install_process_gate(agent_mod) -> str:
    """Wrap process after wire so post-validate runs inside completed pipeline."""
    if getattr(agent_mod, "_quality_process_gate_installed", False):
        return QUALITY_PATCH_VERSION
    orig_process = agent_mod.process_pinterest_job

    async def _process(job_id, url, job_store):
        result = await orig_process(job_id, url, job_store)
        pname = (result.get("product_name") or "")
        assert_valid_identity(pname, context="result")
        for p in result.get("pins") or []:
            t = p.get("title") or ""
            if _is_asin(t) or re.search(r"\bB0[A-Z0-9]{8}\b", t):
                raise RuntimeError(f"Published pin has ASIN title; treating job as failed: {t!r}")
        published = int(result.get("pins_published") or 0)
        if published < 1:
            raise RuntimeError("No Pins were successfully published; refusing empty success.")
        cat = (result.get("category") or "").lower()
        board_id = str(result.get("board_id") or "")
        if board_id == "987906936951147704" and cat in {"home", "fashion", "electronics", "electronics_root"}:
            raise RuntimeError(f"Wrong board Everything Else for category {cat!r}")
        titles = [str(p.get("title") or "") for p in (result.get("pins") or [])]
        required_unique_titles = min(4, published)
        if len(set(t.lower() for t in titles if t)) < required_unique_titles:
            raise RuntimeError(f"Pin title diversity too low; {required_unique_titles} unique title(s) required for {published} successful Pin(s).")
        for p in result.get("pins") or []:
            t = p.get("title") or ""
            if t.strip().lower() == (pname or "").strip().lower():
                raise RuntimeError(f"Published pin title is raw product name only; SEO diversity failed: {t!r}")
        result["quality_patch_version"] = QUALITY_PATCH_VERSION
        return result

    agent_mod.process_pinterest_job = _process
    agent_mod._quality_process_gate_installed = True
    logger.info("QUALITY process gate installed version=%s", QUALITY_PATCH_VERSION)
    return QUALITY_PATCH_VERSION


def install(agent_mod=None) -> str:
    """Backward-compatible: if agent_mod given, install identity only. Prefer explicit two-phase."""
    import agent as _agent
    mod = agent_mod or _agent
    install_identity(mod)
    return QUALITY_PATCH_VERSION
