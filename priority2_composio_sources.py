"""Priority-2 Composio-connected image-source discovery.

This module is additive: it never publishes a Pin and never changes Pinterest
publishing. It only supplies candidates to the existing hard image validator.

Provider order:
Amazon Product Search -> Web Search -> Walmart Product Search ->
Shopping Search -> DuckDuckGo Search.

Composio Image Search remains a later, separately circuit-breakered fallback.
Pexels is connected but is not treated as an exact-product authority because
stock imagery cannot prove that it represents a specific ASIN.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Awaitable, Callable, Dict, List

Runner = Callable[[str, Dict[str, Any], int], Awaitable[Dict[str, Any]]]
PageExtractor = Callable[[str, Dict[str, Any]], Awaitable[List[Dict[str, Any]]]]

_IMAGE_KEYS = {
    "image", "image_url", "imageurl", "image_link", "original", "original_url",
    "thumbnail", "thumbnail_url", "main_image", "mainimage", "primary_image",
    "product_image", "productimage", "photo", "photo_url",
}
_PAGE_KEYS = {
    "url", "link", "product_url", "producturl", "canonical_url", "source_url",
    "sourceurl", "page_url", "pageurl", "detail_url",
}
_TEXT_KEYS = {"title", "name", "product_name", "productname", "description", "brand", "model", "asin"}
_BLOCKED_HOSTS = {
    "facebook.com", "instagram.com", "pinterest.com", "youtube.com",
    "tiktok.com", "twitter.com", "x.com",
}


def _url(value: Any) -> str:
    value = str(value or "").strip()
    return value if value.startswith(("http://", "https://")) else ""


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url.lower())
    return (m.group(1) if m else "").split(":")[0].replace("www.", "")


def _looks_image_url(url: str) -> bool:
    low = url.lower()
    return bool(
        re.search(r"\.(?:jpg|jpeg|png|webp|gif)(?:[?#]|$)", low)
        or "/images/" in low or "/image/" in low or "image=" in low
    )


def _walk_records(value: Any, trail: str = ""):
    if isinstance(value, dict):
        yield value, trail
        for key, child in value.items():
            yield from _walk_records(child, f"{trail}.{str(key).lower()}")
    elif isinstance(value, list):
        for i, child in enumerate(value):
            yield from _walk_records(child, f"{trail}[{i}]")


def _query_variants(product: Dict[str, Any]) -> Dict[str, str]:
    name = re.sub(r"\s+", " ", str(product.get("name") or "product")).strip()[:180]
    asin = str(product.get("asin") or "").strip().upper()
    brand = str(product.get("brand") or "").strip()
    exact = f'"{asin}" "{name}"' if asin else f'"{name}" exact product'
    return {
        "amazon": exact,
        "web": f'"{name}" official product manufacturer',
        "walmart": f'{brand} {name}'.strip(),
        "shopping": exact,
        "duckduckgo": exact,
    }


def _record_evidence(record: Dict[str, Any]) -> str:
    parts = []
    for key, value in record.items():
        if str(key).lower().replace("-", "_") in _TEXT_KEYS and value not in (None, ""):
            parts.append(str(value))
    return " ".join(parts)[:800]


def _candidate_from_record(
    record: Dict[str, Any],
    provider: str,
    product: Dict[str, Any],
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    asin = str(product.get("asin") or "").lower()
    name = str(product.get("name") or "").lower()
    tokens = [t for t in re.findall(r"[a-z0-9]+", name) if len(t) >= 4][:12]
    evidence = _record_evidence(record)
    hay = f"{evidence} {record}".lower()

    def add(raw: Any, kind: str):
        u = _url(raw)
        if not u or _host(u) in _BLOCKED_HOSTS:
            return
        row = {
            "url": u,
            "provider": provider.lower(),
            "source": evidence or provider,
            "source_url": "",
            "evidence": evidence or f"{provider} result",
            "license": "verify_before_commercial_use",
        }
        if kind == "image":
            row["direct_image"] = True
        else:
            row["source_page"] = True
        out.append(row)

    for key, value in record.items():
        low = str(key).lower().replace("-", "_")
        u = _url(value)
        if not u:
            continue
        if low in _IMAGE_KEYS or _looks_image_url(u):
            add(u, "image")
        elif low in _PAGE_KEYS:
            matches = sum(1 for token in tokens if token in hay)
            asin_hit = bool(asin and asin in hay)
            if asin_hit or matches >= 2 or provider.lower() in {
                "composio_search_amazon", "composio_search_walmart"
            }:
                add(u, "page")
    return out


def _dedupe(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out, seen = [], set()
    for row in rows:
        u = str(row.get("url") or "")
        if u and u not in seen:
            seen.add(u)
            out.append(row)
    return out


async def _run_one(
    provider: str,
    query: str,
    runner: Runner,
    product: Dict[str, Any],
) -> List[Dict[str, Any]]:
    args: Dict[str, Any] = {"query": query}
    if provider == "COMPOSIO_SEARCH_AMAZON":
        args["amazon_domain"] = "amazon.com"
    elif provider == "COMPOSIO_SEARCH_SHOPPING":
        args.update({"gl": "us", "hl": "en"})
    try:
        data = await runner(provider, args, 0)
    except Exception:
        return []
    rows: List[Dict[str, Any]] = []
    for record, _ in _walk_records(data):
        rows.extend(_candidate_from_record(record, provider, product))
    return _dedupe(rows)


async def search_priority2_sources(
    product: Dict[str, Any],
    runner: Runner,
    page_image_extractor: PageExtractor,
) -> List[Dict[str, Any]]:
    """Discover exact-product candidates without touching publication."""
    qs = _query_variants(product)
    providers = [
        ("COMPOSIO_SEARCH_AMAZON", qs["amazon"]),
        ("COMPOSIO_SEARCH_WEB", qs["web"]),
        ("COMPOSIO_SEARCH_WALMART", qs["walmart"]),
        ("COMPOSIO_SEARCH_SHOPPING", qs["shopping"]),
        ("COMPOSIO_SEARCH_DUCK_DUCK_GO", qs["duckduckgo"]),
    ]

    # Limit concurrent calls to two so a burst does not turn provider throttling
    # into a new single point of failure.
    sem = asyncio.Semaphore(2)

    async def call(slug: str, query: str):
        async with sem:
            return slug, await _run_one(slug, query, runner, product)

    results = await asyncio.gather(
        *(call(slug, query) for slug, query in providers),
        return_exceptions=True,
    )

    discovered: List[Dict[str, Any]] = []
    for item in results:
        if isinstance(item, Exception):
            continue
        _, rows = item
        discovered.extend(rows)
    discovered = _dedupe(discovered)

    direct = [x for x in discovered if x.get("direct_image")]
    pages = [x for x in discovered if x.get("source_page")]

    extracted: List[Dict[str, Any]] = []
    for page in pages[:10]:
        try:
            rows = await page_image_extractor(str(page["url"]), product)
            for row in rows:
                row = dict(row)
                row.setdefault("provider", "composio_source_page")
                row.setdefault("source_url", page["url"])
                row.setdefault("evidence", "Composio product/source result page")
                # Preserve provider evidence from the search result so the
                # existing identity scorer can distinguish Amazon/Walmart/etc.
                row["source"] = f"{page.get('source', '')} {row.get('source', '')}".strip()
                extracted.append(row)
        except Exception:
            continue
        if len(extracted) >= 24:
            break

    return _dedupe(extracted + direct)[:48]
