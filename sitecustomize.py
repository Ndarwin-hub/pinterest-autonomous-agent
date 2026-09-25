"""Composio transport and Amazon discovery hardening.

Keeps explicit connected-account transport for Pinterest/Gmail and adds a
last-resort direct Amazon catalog-page discovery path when the Composio Search
toolkit is administratively disabled.
"""
from __future__ import annotations
import asyncio, logging, os, re, html
from typing import Any, Dict, Optional, List

logger = logging.getLogger("pinterest-agent.sitecustomize")

def _account_for(slug: str) -> Optional[str]:
    s = slug.upper()
    if s.startswith("PINTEREST_"):
        return os.getenv("COMPOSIO_PINTEREST_ACCOUNT_ID", "").strip() or None
    if s.startswith("GMAIL_"):
        return os.getenv("COMPOSIO_GMAIL_ACCOUNT_ID", "").strip() or None
    return None

async def _direct_execute(tool_slug: str, arguments: Dict[str, Any], retries: int = 2) -> Dict[str, Any]:
    import httpx
    key = os.getenv("COMPOSIO_API_KEY", "").strip()
    if not key:
        raise RuntimeError("COMPOSIO_API_KEY is not set in Railway variables.")
    account = _account_for(tool_slug)
    if not account:
        raise RuntimeError(f"No explicit connected account configured for {tool_slug}.")
    url = f"https://backend.composio.dev/api/v3.1/tools/execute/{tool_slug}"
    headers = {"x-api-key": key, "Content-Type": "application/json"}
    payload = {"connected_account_id": account, "arguments": arguments or {}, "version": "latest"}
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                try: data = resp.json()
                except Exception: data = {"raw": resp.text}
                if resp.status_code >= 400:
                    err = data.get("error") if isinstance(data, dict) else data
                    if isinstance(err, dict): err = err.get("message") or err
                    raise RuntimeError(f"{tool_slug} HTTP {resp.status_code}: {err}")
                if isinstance(data, dict) and data.get("successful") is False:
                    err = data.get("error") or data.get("data", {}).get("message") or str(data)
                    raise RuntimeError(f"{tool_slug} unsuccessful: {err}")
                if isinstance(data, dict) and "data" in data:
                    return data["data"] if data["data"] is not None else {}
                return data if isinstance(data, dict) else {"result": data}
        except Exception as exc:
            last = exc
            if attempt < retries: await asyncio.sleep(min(2 ** attempt, 8))
    raise RuntimeError(str(last) if last else f"{tool_slug} failed")

try:
    import agent as _agent
    _original = _agent.run_composio_tool
    async def _hardened_run(tool_slug: str, arguments: Dict[str, Any], retries: int = 2) -> Dict[str, Any]:
        account = _account_for(tool_slug)
        if account: return await _direct_execute(tool_slug, arguments, retries=retries)
        return await _original(tool_slug, arguments, retries=retries)
    _agent.run_composio_tool = _hardened_run
    logger.info("Composio explicit connected-account transport enabled.")
except Exception:
    logger.exception("Composio transport hardening could not be installed.")

# Amazon discovery resilience: this does not alter the scheduler, ledger,
# Pinterest enqueue, image validation, or publish/verify pipeline.
try:
    import httpx
    from bs4 import BeautifulSoup
    import amazon_composio_discovery as _acd

    _original_amazon_search = _acd._search
    _direct_enabled = os.getenv("AMAZON_DIRECT_HTML_FALLBACK_ENABLED", "1").strip().lower() not in {"0", "false", "no"}

    def _parse_direct_amazon(body: str) -> List[Dict[str, Any]]:
        soup = BeautifulSoup(body, "lxml")
        out: List[Dict[str, Any]] = []
        for card in soup.select('div[data-component-type="s-search-result"][data-asin]'):
            asin = str(card.get("data-asin") or "").strip().upper()
            if not re.fullmatch(r"[A-Z0-9]{10}", asin): continue
            title_el = card.select_one("h2 a span") or card.select_one("h2 span")
            title = html.unescape(title_el.get_text(" ", strip=True)) if title_el else ""
            link_el = card.select_one('h2 a[href]') or card.select_one('a[href*="/dp/"]')
            href = str(link_el.get("href") or "") if link_el else ""
            price_el = card.select_one(".a-price .a-offscreen")
            price_text = price_el.get_text(" ", strip=True) if price_el else ""
            m = re.search(r"([0-9][0-9,]*\\.?[0-9]*)", price_text)
            if not title or not href or not m: continue
            try: price = float(m.group(1).replace(",", ""))
            except Exception: continue
            if href.startswith("/"): href = "https://www.amazon.com" + href
            if "/dp/" not in href and "/gp/product/" not in href: href = f"https://www.amazon.com/dp/{asin}"
            out.append({"asin": asin, "link": href, "title": title, "extracted_price": price,
                        "rating": 0, "reviews": 0, "bought_last_month": "", "badges": [],
                        "position": len(out)+1, "_amazon_domain": "amazon.com", "source": "amazon_html_resilience"})
            if len(out) >= 20: break
        return out

    async def _direct_amazon_search(query: str, page: int = 1) -> List[Dict[str, Any]]:
        if not _direct_enabled: return []
        headers = {"User-Agent": os.getenv("PIN_N_SEARCH_USER_AGENT", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36"),
                   "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9"}
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True, headers=headers) as client:
                response = await client.get("https://www.amazon.com/s", params={"k": query, "page": max(1, int(page))})
                response.raise_for_status()
                products = _parse_direct_amazon(response.text)
                logger.info("Direct Amazon HTML resilience query=%s page=%s products=%s", query, page, len(products))
                return products
        except Exception as exc:
            logger.warning("Direct Amazon HTML resilience failed query=%s page=%s: %s", query, page, str(exc)[:300])
            return []

    async def _resilient_amazon_search(query: str, page: int = 1) -> List[Dict[str, Any]]:
        products = await _original_amazon_search(query, page)
        if products: return products
        return await _direct_amazon_search(query, page)

    _acd._search = _resilient_amazon_search
    logger.info("Amazon discovery resilience installed: Composio/search fallbacks -> direct Amazon HTML")
except Exception:
    logger.exception("Amazon discovery resilience could not be installed.")
