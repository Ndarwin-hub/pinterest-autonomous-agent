"""Pin N-specific Amazon discovery.

This module is intentionally separate from Pin A's board-balanced scheduler.
Pin N means "find N fresh eligible Amazon US products"; board balancing is
not allowed to become a discovery bottleneck. Accepted products still enter
the existing shared enqueue/publish/verify pipeline.
"""
from __future__ import annotations

import logging, os, json
from typing import Any, Dict, List, Optional, Set

from published_registry import registry
from amazon_url import AFFILIATE_TAG
from amazon_composio_discovery import (
    CATEGORY_QUERIES,
    BOARD_SEARCH_PROFILES,
    _search,
    _candidate,
)
from batch_submit import validate_and_canonicalize, MAX_BATCH

logger = logging.getLogger("pinterest-agent.pin_n_discovery")
PIN_N_CACHE_PATH = os.getenv("PIN_N_CANDIDATE_CACHE", os.path.join(os.getenv("DATA_DIR","/data" if os.path.exists("/data") else "/tmp"), "pin_n_candidate_cache.json"))
PIN_N_CACHE_MAX = 200

def _load_candidate_cache() -> List[Dict[str, Any]]:
    try:
        with open(PIN_N_CACHE_PATH, "r", encoding="utf-8") as f:
            data=json.load(f)
        return data if isinstance(data,list) else []
    except Exception:
        return []

def _save_candidate_cache(items: List[Dict[str, Any]]) -> None:
    try:
        os.makedirs(os.path.dirname(PIN_N_CACHE_PATH), exist_ok=True)
        tmp=PIN_N_CACHE_PATH+".tmp"
        with open(tmp,"w",encoding="utf-8") as f: json.dump(items[-PIN_N_CACHE_MAX:],f,default=str,separators=(",",":"))
        os.replace(tmp,PIN_N_CACHE_PATH)
    except Exception as exc:
        logger.warning("Pin N candidate cache write failed: %s",exc)


# Keep the synchronous Pin N request safely below the Railway proxy timeout.
# Pin N remains independent from Pin A; broader discovery can be resumed by
# another Pin N request if this bounded pool is exhausted.
PIN_N_BROAD_QUERIES = [
    "new Amazon products",
    "new releases Amazon",
    "new arrivals Amazon",
    "Amazon best sellers",
    "electronics gadgets best sellers",
    "home kitchen products",
    "running shoes",
    "wireless headphones",
    "USB C charger",
    "gaming headset",
    "wireless microphone",
    "fitness accessories",
    "travel camping gear",
    "pet supplies",
    "baby products",
    "office productivity products",
    "smartphone accessories",
    "computer accessories",
    "countertop appliances",
    "personal care products",
]

def _priority(query: str) -> int:
    q = str(query or "").lower()
    if any(x in q for x in ("new release", "new releases", "new arrival", "new arrivals", "latest", "new ")):
        return 0
    if "best seller" in q or "bestseller" in q or "popular" in q or "trending" in q:
        return 1
    return 2

def _score(c: Dict[str, Any]) -> tuple:
    return (
        _priority(c.get("_discovery_query", "")),
        -int(c.get("score") or 0),
        str(c.get("asin") or ""),
    )

def _assign_board(title: str) -> Dict[str, Optional[str]]:
    """Best-effort board assignment after product acceptance."""
    try:
        from board_org import classify_with_confidence, CATEGORY_BOARD_MAP, DEFAULT_BOARD_NAME
        info = classify_with_confidence({"title": title or "", "name": title or ""})
        category = info.get("category") or "general"
        board = CATEGORY_BOARD_MAP.get(category, DEFAULT_BOARD_NAME)
        return {
            "board": board,
            "confidence": info.get("confidence"),
            "category": category,
        }
    except Exception:
        return {"board": "Everything Else", "confidence": None, "category": "general"}

async def discover_pin_n_products(
    n: int,
    *,
    exclude_asins: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """Find exactly up to N fresh, distinct, valid Amazon US products.

    Unlike Pin A, this function never requires a particular board to have a
    candidate before accepting a product. It uses a deliberately bounded fast
    discovery pass so the synchronous /amazon/pin-count endpoint stays within
    the production proxy timeout.
    """
    n = max(1, min(int(n), MAX_BATCH))
    excluded = {str(a).upper() for a in (exclude_asins or set()) if a}
    excluded |= registry.all_published_asins()
    chosen: Dict[str, Dict[str, Any]] = {}
    cache=_load_candidate_cache()
    for cached in cache:
        c=_candidate(cached, "Pin N cache")
        if not c: continue
        asin=str(c.get("asin") or "").upper()
        if not asin or asin in excluded or asin in chosen: continue
        c["balance_mode"]="pin_n_cache"
        chosen[asin]=c
        if len(chosen)>=n: break
    queries: List[str] = list(PIN_N_BROAD_QUERIES)

    # Add a small deterministic fallback pool without allowing the full Pin A
    # board/category expansion to become a latency bottleneck.
    fallback_queries: List[str] = []
    for board_queries in BOARD_SEARCH_PROFILES.values():
        for q in board_queries:
            if q not in queries and q not in fallback_queries:
                fallback_queries.append(q)
    for category_queries in CATEGORY_QUERIES.values():
        for q in category_queries:
            if q not in queries and q not in fallback_queries:
                fallback_queries.append(q)
    queries.extend(fallback_queries[:4])

    # One page per query keeps the synchronous request bounded. Fresh requests
    # can be repeated if the current bounded pool is exhausted.
    for query in queries:
        if len(chosen) >= n:
            break
        try:
            raws = await _search(query, 1)
        except Exception as exc:
            logger.warning("Pin N search failed query=%s: %s", query, exc)
            continue

        for raw in raws:
            c = _candidate(raw, "Pin N")
            if not c:
                continue
            asin = str(c.get("asin") or "").upper()
            if not asin or asin in excluded or asin in chosen:
                continue

            validated = validate_and_canonicalize(
                c.get("affiliate_url") or c.get("product_url") or ""
            )
            if not validated.get("ok"):
                logger.info(
                    "Pin N candidate rejected asin=%s reason=%s",
                    asin, validated.get("error")
                )
                continue

            final_asin = str(validated.get("asin") or asin).upper()
            if final_asin in excluded or final_asin in chosen:
                continue

            c["asin"] = final_asin
            c["affiliate_url"] = validated["affiliate_url"]
            c["product_url"] = validated["product_url"]
            c["_discovery_query"] = query
            board = _assign_board(str(c.get("title") or ""))
            c["target_board_name"] = board.get("board")
            c["balance_mode"] = "pin_n_independent"
            c["pin_n_category"] = board.get("category")
            c["pin_n_confidence"] = board.get("confidence")
            chosen[final_asin] = c
            cache.append(c)

            if len(chosen) >= n:
                break

    _save_candidate_cache(cache)
    selected = sorted(chosen.values(), key=_score)[:n]
    logger.info(
        "Pin N discovery requested=%s eligible=%s queries=%s published_exclusions=%s",
        n, len(selected), len(queries), len(excluded),
    )
    return selected
