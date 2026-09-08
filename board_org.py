"""Professional Pinterest board organization helpers (additive)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pinterest-agent.board_org")

DEFAULT_BOARD_NAME = "Product Pins"

CATEGORY_BOARD_MAP = {
    "books": "Books & Reading",
    "electronics": "Electronics & Gadgets",
    "home": "Home & Kitchen",
    "beauty": "Beauty & Personal Care",
    "fitness": "Fitness & Wellness",
    "general": DEFAULT_BOARD_NAME,
}

# Longer / more specific phrases first
CATEGORY_KEYWORDS = [
    ("self improvement", "books"),
    ("personal development", "books"),
    ("self-help", "books"),
    ("self help", "books"),
    ("personality", "books"),
    ("psychology", "books"),
    ("paperback", "books"),
    ("hardcover", "books"),
    ("workbook", "books"),
    ("biography", "books"),
    ("memoir", "books"),
    ("literature", "books"),
    ("reading", "books"),
    ("novel", "books"),
    ("books", "books"),
    ("book", "books"),
    ("screen protector", "electronics"),
    ("headphones", "electronics"),
    ("headphone", "electronics"),
    ("earbuds", "electronics"),
    ("earbud", "electronics"),
    ("smartphone", "electronics"),
    ("bluetooth", "electronics"),
    ("electronics", "electronics"),
    ("charging", "electronics"),
    ("charger", "electronics"),
    ("speaker", "electronics"),
    ("iphone", "electronics"),
    ("android", "electronics"),
    ("keyboard", "electronics"),
    ("tablet", "electronics"),
    ("laptop", "electronics"),
    ("computer", "electronics"),
    ("gadget", "electronics"),
    ("phone", "electronics"),
    ("mouse", "electronics"),
    ("household", "home"),
    ("cookware", "home"),
    ("appliance", "home"),
    ("storage", "home"),
    ("kitchen", "home"),
    ("home", "home"),
    ("personal care", "beauty"),
    ("skin care", "beauty"),
    ("skincare", "beauty"),
    ("hair care", "beauty"),
    ("cosmetic", "beauty"),
    ("makeup", "beauty"),
    ("beauty", "beauty"),
    ("sports equipment", "fitness"),
    ("workout", "fitness"),
    ("exercise", "fitness"),
    ("fitness", "fitness"),
    ("yoga", "fitness"),
    ("gym", "fitness"),
]

BOARD_ALIASES = {
    "Books & Reading": [
        "books & reading", "books and reading", "books", "book", "reading",
        "self-help", "self help", "personal development", "psychology",
    ],
    "Electronics & Gadgets": [
        "electronics & gadgets", "electronics and gadgets", "electronics",
        "gadgets", "tech", "phone accessories", "iphone accessories",
        "audio", "headphones",
    ],
    "Home & Kitchen": [
        "home & kitchen", "home and kitchen", "home", "kitchen", "household",
    ],
    "Beauty & Personal Care": [
        "beauty & personal care", "beauty and personal care", "beauty",
        "personal care", "skincare", "skin care",
    ],
    "Fitness & Wellness": [
        "fitness & wellness", "fitness and wellness", "fitness", "wellness",
        "workout", "gym",
    ],
    DEFAULT_BOARD_NAME: [
        "product pins", "products", "product pin",
    ],
}


def detect_product_category(product: Dict[str, Any]) -> str:
    name = (product.get("name") or "").lower()
    desc = (product.get("description") or "").lower()
    url = (product.get("url") or product.get("source_url") or "").lower()
    blob = f"{name} {desc} {url}"
    scores: Dict[str, int] = {}
    for key, cat in CATEGORY_KEYWORDS:
        if key in blob:
            weight = len(key.split()) * 3
            if key in name:
                weight += 5
            scores[cat] = scores.get(cat, 0) + weight
    if not scores:
        return "general"
    return max(scores.items(), key=lambda kv: kv[1])[0]


def preferred_board_name(category_key: str) -> str:
    return CATEGORY_BOARD_MAP.get(category_key, DEFAULT_BOARD_NAME)


def _normalize_board_name(name: str) -> str:
    return " ".join((name or "").lower().replace("&", "and").split())


def find_matching_board(items: List[Any], preferred_name: str) -> Optional[str]:
    if not items:
        return None
    preferred_norm = _normalize_board_name(preferred_name)
    aliases = BOARD_ALIASES.get(preferred_name, [])
    alias_norms = {_normalize_board_name(a) for a in aliases}
    alias_norms.add(preferred_norm)
    generic_norms = {_normalize_board_name(DEFAULT_BOARD_NAME), "product pins", "products"}

    for b in items:
        bname = (b.get("name") or "").strip()
        if _normalize_board_name(bname) == preferred_norm:
            return str(b.get("id") or b.get("board_id") or "") or None

    for b in items:
        bname = (b.get("name") or "").strip()
        bn = _normalize_board_name(bname)
        if bn in generic_norms and preferred_name != DEFAULT_BOARD_NAME:
            continue
        if bn in alias_norms:
            return str(b.get("id") or b.get("board_id") or "") or None
        if preferred_norm in bn or bn in preferred_norm:
            if len(bn) >= 4 and preferred_name != DEFAULT_BOARD_NAME:
                return str(b.get("id") or b.get("board_id") or "") or None
    return None
