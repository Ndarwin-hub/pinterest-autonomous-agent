"""Thin multi-product ingest that feeds the existing single-URL /submit pipeline.

Additive only. Does not replace, bypass, or weaken the 5-Pin quality gates.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from amazon_url import (
    AFFILIATE_TAG,
    canonicalize_amazon_product_url,
    extract_asin_from_url,
    is_amazon_us_product_url,
    resolve_amazon_product_url,
)
from published_registry import registry

logger = logging.getLogger("pinterest-agent.batch_submit")

MAX_BATCH = 50


def _ensure_affiliate_tag(url: str, tag: str = AFFILIATE_TAG) -> str:
    """Preserve path/ASIN; force or keep the approved affiliate tag."""
    p = urlsplit(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q["tag"] = tag
    return urlunsplit((p.scheme, p.netloc, p.path, urlencode(q), ""))


def validate_and_canonicalize(url: str) -> Dict[str, Any]:
    """
    Validate one URL as Amazon US product detail and return canonical tagged form.

    Returns dict with keys: ok, input_url, affiliate_url, product_url, asin, error
    """
    raw = (url or "").strip()
    out: Dict[str, Any] = {
        "ok": False,
        "input_url": raw,
        "affiliate_url": None,
        "product_url": None,
        "asin": None,
        "error": None,
    }
    if not raw.startswith(("http://", "https://")):
        out["error"] = "not_http_url"
        return out
    try:
        resolved = resolve_amazon_product_url(raw, tag=AFFILIATE_TAG)
    except Exception as e:
        # Fallback: if already a clear US product URL, canonicalize without network
        if is_amazon_us_product_url(raw):
            try:
                resolved = canonicalize_amazon_product_url(raw, tag=AFFILIATE_TAG)
            except Exception as e2:
                out["error"] = f"resolve_failed:{type(e).__name__}:{e}; canonicalize:{e2}"
                return out
        else:
            out["error"] = f"resolve_failed:{type(e).__name__}:{e}"
            return out

    if not is_amazon_us_product_url(resolved):
        out["error"] = "not_amazon_us_product"
        return out

    asin = extract_asin_from_url(resolved)
    if not asin:
        out["error"] = "no_asin"
        return out

    tagged = _ensure_affiliate_tag(resolved, AFFILIATE_TAG)
    if f"tag={AFFILIATE_TAG}" not in tagged:
        out["error"] = "affiliate_tag_missing"
        return out

    out.update(
        {
            "ok": True,
            "affiliate_url": tagged,
            "product_url": tagged,  # same canonical form used as product destination
            "asin": asin,
            "error": None,
        }
    )
    return out


def check_duplicate(asin: Optional[str], url: Optional[str]) -> Optional[str]:
    """Return a reason string if already published / blocked, else None."""
    if asin and registry.is_published(asin=asin):
        return "already_published_asin"
    if url and registry.is_published(url=url):
        return "already_published_url"
    return None


async def prepare_batch_items(
    urls: List[str],
    *,
    exclude_asins: Optional[Set[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Validate, canonicalize, and dedupe a list of input URLs.

    Does not enqueue. Caller feeds accepted affiliate_url into existing enqueue_job.
    """
    exclude: Set[str] = {a.upper() for a in (exclude_asins or set())}
    seen_asins: Set[str] = set()
    seen_urls: Set[str] = set()
    items: List[Dict[str, Any]] = []

    for raw in urls:
        item = validate_and_canonicalize(raw)
        item["status"] = "rejected"
        item["outcome"] = "automation_rejection"
        item["job_id"] = None
        item["message"] = None

        if not item["ok"]:
            item["message"] = item["error"] or "validation_failed"
            items.append(item)
            continue

        asin = (item["asin"] or "").upper()
        aff = item["affiliate_url"] or ""

        if asin in exclude or asin in seen_asins:
            item["ok"] = False
            item["status"] = "skipped"
            item["outcome"] = "intentional_skip"
            item["error"] = "duplicate_asin_in_batch_or_exclude"
            item["message"] = item["error"]
            items.append(item)
            continue

        if aff in seen_urls:
            item["ok"] = False
            item["status"] = "skipped"
            item["outcome"] = "intentional_skip"
            item["error"] = "duplicate_url_in_batch"
            item["message"] = item["error"]
            items.append(item)
            continue

        dup = check_duplicate(asin, aff)
        if dup:
            item["ok"] = False
            item["status"] = "skipped"
            item["outcome"] = "intentional_skip"
            item["error"] = dup
            item["message"] = dup
            items.append(item)
            continue

        seen_asins.add(asin)
        seen_urls.add(aff)
        item["status"] = "accepted"
        item["outcome"] = "accepted"
        item["message"] = "validated_amazon_us_with_affiliate_tag"
        items.append(item)

    return items


async def discover_n_products(
    n: int,
    *,
    exclude_asins: Optional[Set[str]] = None,
    live_boards: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Discover up to N distinct Amazon US products using live board-balance ordering.

    Core rule order:
      CURRENT BOARD COUNTS → least-filled eligible board → product matching that board
      → duplicate validation → assignment → recalculate balance for next product.

    When dedicated boards are approximately balanced, prioritizes external/non-category
    products for the Everything Else board (if it exists). Never forces an unrelated
    product onto a dedicated board. Duplicate/history protection is never overridden.
    """
    from amazon_composio_discovery import CATEGORY_QUERIES, discover_category, _search, _candidate
    from board_balance import (
        extract_board_rows,
        balance_state,
        discovery_board_order,
        apply_virtual_increment,
        product_matches_board,
    )
    from board_org import DEFAULT_BOARD_NAME

    n = max(1, min(int(n), MAX_BATCH))
    excluded: Set[str] = {a.upper() for a in (exclude_asins or set())} | registry.all_published_asins()
    selected: List[Dict[str, Any]] = []

    rows = extract_board_rows(live_boards or [])
    balance = balance_state(rows)
    if not balance.get("available"):
        logger.warning(balance.get("message") or "Board counts unavailable; using category fallback order")

    targets = discovery_board_order(balance)
    # Flatten into ordered amazon category attempts interleaved with EE mode
    plan: List[Dict[str, Any]] = []
    for t in targets:
        if t.get("mode") == "everything_else":
            plan.append({**t, "amazon_category": None})
        else:
            for cat in t.get("amazon_categories") or []:
                plan.append({**t, "amazon_category": cat})

    if not plan:
        # Absolute fallback: original CATEGORY_QUERIES order
        for cat in CATEGORY_QUERIES.keys():
            plan.append(
                {
                    "board_name": None,
                    "board_id": None,
                    "amazon_category": cat,
                    "mode": "dedicated",
                    "pin_count": None,
                }
            )

    attempts = 0
    max_attempts = max(n * 4, len(plan) * 2)
    plan_idx = 0
    while len(selected) < n and attempts < max_attempts:
        attempts += 1
        step = plan[plan_idx % len(plan)]
        plan_idx += 1
        mode = step.get("mode") or "dedicated"
        board_name = step.get("board_name")
        amazon_cat = step.get("amazon_category")

        candidate = None
        try:
            if mode == "everything_else":
                # External / non-category: search broad popular items, accept only
                # products that do NOT map to a dedicated board.
                for q in ("popular new releases", "best sellers amazon gadgets home"):
                    try:
                        raws = await _search(q, 1)
                    except Exception as e:
                        logger.warning("EE search failed %s: %s", q, e)
                        continue
                    for raw in raws:
                        c = _candidate(raw, "Everything Else")
                        if not c or c["asin"] in excluded:
                            continue
                        title_product = {"title": c.get("title") or "", "name": c.get("title") or ""}
                        if board_name and not product_matches_board(title_product, board_name):
                            # Must be genuinely external
                            from board_org import classify_with_confidence
                            info = classify_with_confidence(title_product)
                            if info.get("category") not in ("general",) and info.get("confidence") in ("HIGH", "MEDIUM"):
                                continue
                        candidate = c
                        break
                    if candidate:
                        break
            else:
                if not amazon_cat:
                    continue
                candidate = await discover_category(amazon_cat, exclude_asins=excluded)
                if candidate and board_name:
                    title_product = {"title": candidate.get("title") or "", "name": candidate.get("title") or ""}
                    # Soft relevance: prefer match; if clearly wrong dedicated board, skip
                    from board_org import classify_with_confidence, CATEGORY_BOARD_MAP
                    info = classify_with_confidence(title_product)
                    mapped = CATEGORY_BOARD_MAP.get(info.get("category") or "general", DEFAULT_BOARD_NAME)
                    if (
                        info.get("confidence") in ("HIGH", "MEDIUM")
                        and mapped != board_name
                        and mapped != DEFAULT_BOARD_NAME
                    ):
                        logger.info(
                            "Skip product %s for board %s (maps to %s)",
                            candidate.get("asin"),
                            board_name,
                            mapped,
                        )
                        excluded.add(candidate["asin"])
                        candidate = None
        except Exception as e:
            logger.warning("balance-aware discover failed board=%s cat=%s: %s", board_name, amazon_cat, e)
            continue

        if not candidate:
            continue
        asin = (candidate.get("asin") or "").upper()
        if not asin or asin in excluded:
            continue
        aff = candidate.get("affiliate_url") or candidate.get("product_url")
        if not aff:
            continue
        v = validate_and_canonicalize(aff)
        if not v["ok"]:
            excluded.add(asin)
            continue
        excluded.add(asin)
        selected.append(
            {
                "asin": v["asin"],
                "affiliate_url": v["affiliate_url"],
                "product_url": v["product_url"],
                "title": candidate.get("title"),
                "category": candidate.get("category") or amazon_cat or "Everything Else",
                "target_board_name": board_name,
                "target_board_id": step.get("board_id"),
                "balance_mode": mode,
                "source": "composio_amazon_balance",
            }
        )
        # Recalculate expected balance after this assignment (+5 pins)
        if board_name and balance.get("available"):
            balance = apply_virtual_increment(balance, board_name, pins_per_product=5)
            targets = discovery_board_order(balance)
            plan = []
            for t in targets:
                if t.get("mode") == "everything_else":
                    plan.append({**t, "amazon_category": None})
                else:
                    for cat in t.get("amazon_categories") or []:
                        plan.append({**t, "amazon_category": cat})
            if plan:
                plan_idx = 0
    return selected
