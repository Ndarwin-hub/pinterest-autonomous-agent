"""Dynamic Pinterest board balancing — core system rule for Pin / Railway+N / You+N.

Decision hierarchy (mandatory):
  A. Retrieve current board pin counts (live).
  B. If one or more dedicated boards are materially underfilled → prioritize least-filled
     appropriate board(s) and select products that match those boards.
  C. If dedicated boards are approximately balanced → prioritize a product that does NOT
     fit a dedicated category and assign it to Everything Else (if that board exists).
  D. If counts cannot be retrieved → do not invent counts; report unavailable.

Relevance always beats pure count: never force an unrelated product onto a dedicated board.
Duplicate/history protection is never overridden.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

from board_org import (
    DEFAULT_BOARD_NAME,
    LEGACY_BOARD_IDS,
    LEGACY_BOARD_NAMES,
    PERMANENT_BOARD_IDS,
    CATEGORY_BOARD_MAP,
    classify_with_confidence,
    detect_product_category,
)

logger = logging.getLogger("pinterest-agent.board_balance")

# Material underfill: board is underfilled if pin_count <= (median - UNDERFILL_DELTA)
# or pin_count is among the lowest and gap from max is >= UNDERFILL_DELTA.
UNDERFILL_DELTA = 5
# Balanced state: max(dedicated) - min(dedicated) <= BALANCE_SPREAD
BALANCE_SPREAD = 8

# Board name → Amazon discovery category keys (amazon_composio_discovery.CATEGORY_QUERIES)
BOARD_TO_AMAZON_CATEGORIES: Dict[str, List[str]] = {
    "Electronics & Gadgets": ["Electronics", "Cell Phones & Accessories", "Computers & Accessories", "Musical Instruments"],
    "Fashion & Lifestyle": ["Clothing/Shoes"],
    "Health & Fitness": ["Beauty", "Health & Household", "Sports & Outdoors"],
    "Home, Kitchen & Dining": ["Home & Kitchen", "Appliances"],
    "Sports, Games & Toys": ["Toys & Games", "Video Games"],
    "Baby & Kids": ["Baby"],
    "Pet Supplies": ["Pet Supplies"],
    "Automotive & Tools": ["Electronics"],  # discovery keywords still filtered by relevance later
    "Office & Productivity": ["Computers & Accessories"],
    "Books & Learning": ["Home & Kitchen"],  # soft; relevance gate still applies
    "Travel & Camping": ["Sports & Outdoors", "Clothing/Shoes"],
    DEFAULT_BOARD_NAME: [],  # external / non-category products only
}

DEDICATED_BOARD_NAMES: Set[str] = {
    n for n in PERMANENT_BOARD_IDS.keys() if n != DEFAULT_BOARD_NAME
}


def extract_board_rows(live_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize live PINTEREST_LIST_BOARDS items into countable rows."""
    rows: List[Dict[str, Any]] = []
    for b in live_items or []:
        bid = str(b.get("id") or b.get("board_id") or "")
        name = (b.get("name") or "").strip()
        if not bid or not name:
            continue
        if bid in LEGACY_BOARD_IDS or name in LEGACY_BOARD_NAMES:
            continue
        pin_count = b.get("pin_count")
        if pin_count is None:
            pin_count = b.get("pins_count")
        try:
            pin_count_i = int(pin_count) if pin_count is not None else None
        except (TypeError, ValueError):
            pin_count_i = None
        rows.append(
            {
                "id": bid,
                "name": name,
                "pin_count": pin_count_i,
                "is_everything_else": name == DEFAULT_BOARD_NAME
                or bid == PERMANENT_BOARD_IDS.get(DEFAULT_BOARD_NAME),
                "is_dedicated": name in DEDICATED_BOARD_NAMES
                or bid in {PERMANENT_BOARD_IDS[n] for n in DEDICATED_BOARD_NAMES if n in PERMANENT_BOARD_IDS},
                "is_permanent": bid in set(PERMANENT_BOARD_IDS.values()) or name in PERMANENT_BOARD_IDS,
            }
        )
    return rows


def counts_available(rows: List[Dict[str, Any]]) -> bool:
    """True only when every permanent/dedicated board we care about has a numeric pin_count."""
    if not rows:
        return False
    permanent = [r for r in rows if r.get("is_permanent")]
    if not permanent:
        return False
    return all(r.get("pin_count") is not None for r in permanent)


def rank_by_pin_count(rows: List[Dict[str, Any]], *, dedicated_only: bool = True) -> List[Dict[str, Any]]:
    """Lowest pin_count first. Boards without counts sort last."""
    pool = [r for r in rows if (r.get("is_dedicated") if dedicated_only else True)]
    if not dedicated_only:
        pool = [r for r in rows if r.get("is_permanent") or r.get("is_dedicated") or r.get("is_everything_else")]
    return sorted(
        pool,
        key=lambda r: (
            r.get("pin_count") is None,
            r.get("pin_count") if r.get("pin_count") is not None else 10**9,
            r.get("name") or "",
        ),
    )


def balance_state(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compute balance snapshot from live rows.

    Returns keys:
      available, state ('underfilled'|'balanced'|'unknown'),
      ranked_dedicated, underfilled, everything_else, spread, message
    """
    if not counts_available(rows):
        return {
            "available": False,
            "state": "unknown",
            "ranked_dedicated": [],
            "underfilled": [],
            "everything_else": next((r for r in rows if r.get("is_everything_else")), None),
            "spread": None,
            "message": "Board balancing could not be verified because current Pinterest counts were unavailable.",
            "snapshot": [],
        }

    dedicated = rank_by_pin_count(rows, dedicated_only=True)
    counts = [int(r["pin_count"]) for r in dedicated if r.get("pin_count") is not None]
    if not counts:
        return {
            "available": False,
            "state": "unknown",
            "ranked_dedicated": dedicated,
            "underfilled": [],
            "everything_else": next((r for r in rows if r.get("is_everything_else")), None),
            "spread": None,
            "message": "Board balancing could not be verified because current Pinterest counts were unavailable.",
            "snapshot": [{"name": r["name"], "id": r["id"], "pin_count": r.get("pin_count")} for r in dedicated],
        }

    mx, mn = max(counts), min(counts)
    spread = mx - mn
    # Underfilled: boards at or near the minimum when spread is material
    underfilled: List[Dict[str, Any]] = []
    if spread >= UNDERFILL_DELTA:
        threshold = mn + max(0, UNDERFILL_DELTA - 1)
        underfilled = [r for r in dedicated if r.get("pin_count") is not None and int(r["pin_count"]) <= threshold]

    state = "balanced" if spread <= BALANCE_SPREAD and not underfilled else "underfilled"
    # If spread is small, treat as balanced even if underfilled list has edge cases
    if spread <= BALANCE_SPREAD:
        state = "balanced"
        # still expose lowest boards for soft prioritization
        underfilled = dedicated[: max(1, min(3, len(dedicated)))] if dedicated else []

    ee = next((r for r in rows if r.get("is_everything_else")), None)
    return {
        "available": True,
        "state": state,
        "ranked_dedicated": dedicated,
        "underfilled": underfilled if state == "underfilled" else [],
        "soft_priority": dedicated[:3],  # always the current lowest
        "everything_else": ee,
        "spread": spread,
        "min_count": mn,
        "max_count": mx,
        "message": (
            f"Dedicated boards balanced (spread={spread}). Prefer external products → Everything Else when relevant."
            if state == "balanced"
            else f"Underfilled boards detected (spread={spread}). Prioritize least-filled appropriate boards."
        ),
        "snapshot": [{"name": r["name"], "id": r["id"], "pin_count": r.get("pin_count")} for r in dedicated]
        + ([{"name": ee["name"], "id": ee["id"], "pin_count": ee.get("pin_count")}] if ee else []),
    }


def discovery_board_order(balance: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Ordered discovery targets for Railway+N / You+N / automated selection.

    Each item: {board_name, board_id, pin_count, amazon_categories, mode}
    mode = 'dedicated' | 'everything_else'
    """
    targets: List[Dict[str, Any]] = []
    if not balance.get("available"):
        # Fallback: fixed permanent dedicated order without inventing counts
        for name in sorted(DEDICATED_BOARD_NAMES):
            bid = PERMANENT_BOARD_IDS.get(name)
            cats = BOARD_TO_AMAZON_CATEGORIES.get(name) or []
            if bid and cats:
                targets.append(
                    {
                        "board_name": name,
                        "board_id": bid,
                        "pin_count": None,
                        "amazon_categories": cats,
                        "mode": "dedicated",
                    }
                )
        return targets

    if balance.get("state") == "balanced":
        # Prefer Everything Else first for external products, then soft-priority dedicated
        ee = balance.get("everything_else")
        if ee:
            targets.append(
                {
                    "board_name": ee["name"],
                    "board_id": ee["id"],
                    "pin_count": ee.get("pin_count"),
                    "amazon_categories": [],  # non-category discovery
                    "mode": "everything_else",
                }
            )
        for r in balance.get("ranked_dedicated") or []:
            cats = BOARD_TO_AMAZON_CATEGORIES.get(r["name"]) or []
            if not cats:
                continue
            targets.append(
                {
                    "board_name": r["name"],
                    "board_id": r["id"],
                    "pin_count": r.get("pin_count"),
                    "amazon_categories": cats,
                    "mode": "dedicated",
                }
            )
        return targets

    # Underfilled: least-filled first
    for r in balance.get("ranked_dedicated") or []:
        cats = BOARD_TO_AMAZON_CATEGORIES.get(r["name"]) or []
        if not cats:
            continue
        targets.append(
            {
                "board_name": r["name"],
                "board_id": r["id"],
                "pin_count": r.get("pin_count"),
                "amazon_categories": cats,
                "mode": "dedicated",
            }
        )
    ee = balance.get("everything_else")
    if ee:
        targets.append(
            {
                "board_name": ee["name"],
                "board_id": ee["id"],
                "pin_count": ee.get("pin_count"),
                "amazon_categories": [],
                "mode": "everything_else",
            }
        )
    return targets


def product_matches_board(product: Dict[str, Any], board_name: str) -> bool:
    """Relevance check: product's classified board must equal board_name (or EE for general)."""
    info = classify_with_confidence(product)
    cat = info.get("category") or "general"
    mapped = CATEGORY_BOARD_MAP.get(cat, DEFAULT_BOARD_NAME)
    if board_name == DEFAULT_BOARD_NAME:
        return mapped == DEFAULT_BOARD_NAME or cat == "general" or info.get("confidence") == "LOW"
    return mapped == board_name


def resolve_board_with_balance(
    product: Dict[str, Any],
    live_items: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Single-product board assignment for Pin /submit path.

    Relevance is primary. Balance is recorded and only influences Everything Else
    eligibility when the product is general/low-confidence.
    """
    rows = extract_board_rows(live_items)
    balance = balance_state(rows)
    info = classify_with_confidence(product)
    category = (info.get("category") or product.get("category") or "general").lower()
    confidence = (info.get("confidence") or "LOW").upper()
    preferred = CATEGORY_BOARD_MAP.get(category, DEFAULT_BOARD_NAME)

    # If product clearly belongs to a dedicated board → that board (relevance wins)
    if preferred != DEFAULT_BOARD_NAME and confidence in ("HIGH", "MEDIUM"):
        bid = PERMANENT_BOARD_IDS.get(preferred)
        # resolve against live items
        live_id = None
        for r in rows:
            if r["name"] == preferred or r["id"] == bid:
                live_id = r["id"]
                break
        return {
            "board_id": live_id or bid,
            "board_name": preferred,
            "category": category,
            "confidence": confidence,
            "balance": balance,
            "reason": "product_relevance_dedicated",
            "balance_applied": False,
        }

    # General / low-confidence product
    ee = balance.get("everything_else") or next(
        (r for r in rows if r.get("is_everything_else")), None
    )
    if preferred == DEFAULT_BOARD_NAME or confidence == "LOW":
        if ee:
            return {
                "board_id": ee["id"],
                "board_name": ee["name"],
                "category": category,
                "confidence": confidence,
                "balance": balance,
                "reason": (
                    "everything_else_balanced_external"
                    if balance.get("state") == "balanced"
                    else "everything_else_general_product"
                ),
                "balance_applied": bool(balance.get("available")),
            }

    # Fallback preferred permanent id
    bid = PERMANENT_BOARD_IDS.get(preferred) or (ee["id"] if ee else None)
    return {
        "board_id": bid,
        "board_name": preferred if bid else DEFAULT_BOARD_NAME,
        "category": category,
        "confidence": confidence,
        "balance": balance,
        "reason": "fallback_preferred",
        "balance_applied": False,
    }


def apply_virtual_increment(
    balance: Dict[str, Any],
    board_name: str,
    pins_per_product: int = 5,
) -> Dict[str, Any]:
    """
    After assigning a product (expected +5 pins), recalculate ranking for the next selection.
    Does not mutate live Pinterest — only the in-run planning snapshot.
    """
    if not balance.get("available"):
        return balance
    snap = []
    for row in balance.get("snapshot") or []:
        r = dict(row)
        if r.get("name") == board_name and r.get("pin_count") is not None:
            r["pin_count"] = int(r["pin_count"]) + pins_per_product
        snap.append(r)
    # Rebuild rows-like structure for balance_state
    fake_items = [
        {"id": s["id"], "name": s["name"], "pin_count": s.get("pin_count")} for s in snap
    ]
    return balance_state(extract_board_rows(fake_items))
