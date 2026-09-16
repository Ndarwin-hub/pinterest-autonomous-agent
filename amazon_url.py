"""Amazon URL resolution and canonicalization for short links and affiliate tags.

Resolves amzn.to (and similar) short URLs to the final Amazon US product-detail
URL BEFORE identity research. Preserves or injects tag=desiredplus-20.
Fails closed when resolution cannot produce a reliable US /dp/ASIN product URL.
"""
from __future__ import annotations

import logging
import re
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import httpx

logger = logging.getLogger("pinterest-agent.amazon_url")

AFFILIATE_TAG = "desiredplus-20"
ASIN_RE = re.compile(r"(?:/dp/|/gp/product/|/product/)([A-Z0-9]{10})", re.I)
ASIN_QUERY_RE = re.compile(r"[?&]asin=([A-Z0-9]{10})", re.I)
SHORT_HOSTS = {"amzn.to", "a.co", "amzn.com"}
AMAZON_US_HOSTS = {"amazon.com", "www.amazon.com", "smile.amazon.com"}

_NON_PRODUCT_PATH_PREFIXES = (
    "/s",
    "/gp/bestsellers",
    "/gp/new-releases",
    "/gp/most-wished-for",
    "/gp/movers-and-shakers",
    "/b/",
    "/stores/",
    "/shop/",
    "/hz/",
    "/gp/cart",
    "/gp/buy",
    "/ap/",
    "/customer-preferences",
)


def extract_asin_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    m = ASIN_RE.search(url) or ASIN_QUERY_RE.search(url)
    return m.group(1).upper() if m else None


def is_amazon_us_product_url(url: str) -> bool:
    try:
        p = urlparse(url)
    except Exception:
        return False
    host = (p.netloc or "").lower().replace("www.", "")
    if host not in {"amazon.com", "smile.amazon.com"}:
        return False
    path = p.path or ""
    if any(path.startswith(pref) for pref in _NON_PRODUCT_PATH_PREFIXES):
        return False
    return bool(extract_asin_from_url(url))


def is_amazon_short_url(url: str) -> bool:
    try:
        host = (urlparse(url).netloc or "").lower()
    except Exception:
        return False
    return host in SHORT_HOSTS or host.endswith(".amzn.to")


def canonicalize_amazon_product_url(url: str, tag: str = AFFILIATE_TAG) -> str:
    asin = extract_asin_from_url(url)
    if not asin:
        raise ValueError(f"Cannot canonicalize: no ASIN in URL: {url!r}")
    return f"https://www.amazon.com/dp/{asin}?tag={tag}"


def _safe_follow(url: str, timeout: float = 20.0) -> Tuple[str, int]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    limits = httpx.Limits(max_connections=5, max_keepalive_connections=2)
    with httpx.Client(
        follow_redirects=True,
        timeout=timeout,
        headers=headers,
        limits=limits,
        max_redirects=8,
    ) as client:
        try:
            r = client.head(url)
            final = str(r.url)
            if is_amazon_us_product_url(final) or extract_asin_from_url(final):
                return final, r.status_code
        except Exception as e:
            logger.debug("HEAD resolve failed for %s: %s", url, e)

        r = client.get(url)
        return str(r.url), r.status_code


def resolve_amazon_product_url(url: str, tag: str = AFFILIATE_TAG) -> str:
    if not url or not str(url).strip().startswith("http"):
        raise RuntimeError(f"Invalid URL for Amazon resolution: {url!r}")

    original = url.strip()
    asin = extract_asin_from_url(original)

    if asin and is_amazon_us_product_url(original):
        canonical = canonicalize_amazon_product_url(original, tag=tag)
        logger.info("Amazon URL already product detail; canonicalized to %s", canonical)
        return canonical

    if asin and "amazon." in (urlparse(original).netloc or "").lower():
        try:
            return canonicalize_amazon_product_url(original, tag=tag)
        except ValueError:
            pass

    try:
        final_url, status = _safe_follow(original)
    except Exception as e:
        raise RuntimeError(
            f"Failed to resolve Amazon URL {original!r}: {type(e).__name__}: {e}"
        ) from e

    logger.info(
        "Amazon short/redirect resolved status=%s final=%s",
        status,
        final_url[:180],
    )

    if not extract_asin_from_url(final_url):
        raise RuntimeError(
            f"Resolved URL has no ASIN (not a product page): {final_url!r} "
            f"(from {original!r})"
        )

    host = (urlparse(final_url).netloc or "").lower()
    if not any(h in host for h in ("amazon.com", "smile.amazon.com")):
        raise RuntimeError(
            f"Resolved URL is not Amazon US: host={host!r} final={final_url!r}"
        )

    if any((urlparse(final_url).path or "").startswith(p) for p in _NON_PRODUCT_PATH_PREFIXES):
        raise RuntimeError(
            f"Resolved URL is a non-product Amazon page: {final_url!r}"
        )

    canonical = canonicalize_amazon_product_url(final_url, tag=tag)
    if not is_amazon_us_product_url(canonical):
        raise RuntimeError(f"Canonicalization produced non-product URL: {canonical!r}")

    if f"tag={tag}" not in canonical:
        raise RuntimeError(f"Affiliate tag missing after canonicalize: {canonical!r}")

    return canonical
