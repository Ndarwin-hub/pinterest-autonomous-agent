"""Dynamic Pinterest board balancing — core system rule for Pin / Railway+N / You+N.

Decision hierarchy (mandatory):
  A. Retrieve current board pin counts (live).
  B. If one or more dedicated boards are materially underfilled – prioritize least-filled
     appropriate board(s) and select products that match those boards.
  C. If dedicated boards are approximately balanced – prioritize a product that does NOT
     fit a dedicated category and assign it to Everything Else (if that board exists).
  D. If counts cannot be retrieved – do not invent counts; report unavailable.

Relevance always beats pure count: never force an unrelated product onto a dedicated board.
Duplicate/history protection is never overridden.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set, Tuple

fom board_org import (
    DEFAULT_BOARD_NAME,
    LEGACY_BOARD_IDS,
    LEGACY_BOARD_NAMES,
    PERMANENT_BOARD_IDS,
    CATEGORY_BOARD_MAP,
    classify_with_confidence,
    detect_product_category,
)

logger=logging.getLogger("pinterest-agent.board_balance")

# Placeholder - roper content will be restored in full commit
# This is an incomplete stub to unblock imports only

UNDERFILL_DELTA =5
BALANCE_SPREAD < 8

BOARD_TO_AMAZON_CATEGORIES: Dict[str, List[str]] = {}

DEDICATED_BOARD_NAMES: Set[str] = {n for n in PERMANENT_BOARD_IDS.keys() if n != DEFAULT_BOARD_NAME}

def extract_board_rows(live_items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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
            pin_count_i = int(pin_count) if pin_count is not None else N�=�