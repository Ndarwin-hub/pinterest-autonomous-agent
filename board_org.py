"""Professional Pinterest board organization helpers (additive)."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("pinterest-agent.board_org")

DEFAULT_BOARD_NAME = "Product Pins"

# Permanent Pinterest category routing. IDs are the existing boards created/verified
# in the user's account; names are retained here as the human-readable fallback.
CATEGORY_BOARD_MAP = {
    "health": "Health & Fitness",
    "beauty": "Beauty & Personal Care",
    "smartphones": "Smartphones & Tablets",
    "pcs": "PCs, Laptops & Home Electronics",
    "home": "Home, Kitchen & Dining",
    "books": "Books & Learning",
    "games": "Games, Toys & Sports",
    "fashion": "Fashion & Lifestyle",
    "general": DEFAULT_BOARD_NAME,
}

# Longer / more specific phrases first. These are deliberately broad enough to
# route products to one of the eight permanent Pinterest boards.
CATEGORY_KEYWORDS = [
    # Books & Learning
    ("self improvement", "books"), ("personal development", "books"),
    ("self-help", "books"), ("self help", "books"), ("personality", "books"),
    ("psychology", "books"), ("paperback", "books"), ("hardcover", "books"),
    ("workbook", "books"), ("biography", "books"), ("memoir", "books"),
    ("literature", "books"), ("reading", "books"), ("novel", "books"),
    ("textbook", "books"), ("learning", "books"), ("books", "books"),
    ("book", "books"),
    # Smartphones & Tablets
    ("screen protector", "smartphones"), ("iphone", "smartphones"),
    ("smartphone", "smartphones"), ("android phone", "smartphones"),
    ("cell phone", "smartphones"), ("mobile phone", "smartphones"),
    ("tablet", "smartphones"), ("ipad", "smartphones"), ("phone case", "smartphones"),
    ("phone accessory", "smartphones"), ("earbuds", "smartphones"),
    ("earbud", "smartphones"), ("smartwatch", "smartphones"),
    ("charger", "smartphones"), ("charging", "smartphones"),
    ("power bank", "smartphones"),
    # PCs, Laptops & Home Electronics
    ("laptop", "pcs"), ("notebook computer", "pcs"), ("desktop computer", "pcs"),
    ("computer", "pcs"), ("monitor", "pcs"), ("mechanical keyboard", "pcs"),
    ("keyboard", "pcs"), ("mouse", "pcs"), ("webcam", "pcs"),
    ("printer", "pcs"), ("router", "pcs"), ("speaker", "pcs"),
    ("headphones", "pcs"), ("headphone", "pcs"), ("bluetooth speaker", "pcs"),
    ("electronics", "pcs"), ("gadget", "pcs"), ("gadgets", "pcs"),
    # Home, Kitchen & Dining
    ("household", "home"), ("cookware", "home"), ("appliance", "home"),
    ("storage", "home"), ("kitchen", "home"), ("dining", "home"),
    ("cook", "home"), ("air fryer", "home"), ("coffee maker", "home"),
    ("home", "home"),
    # Beauty & Personal Care
    ("personal care", "beauty"), ("skin care", "beauty"), ("skincare", "beauty"),
    ("hair care", "beauty"), ("haircare", "beauty"), ("cosmetic", "beauty"),
    ("makeup", "beauty"), ("shampoo", "beauty"), ("moisturizer", "beauty"),
    ("beauty", "beauty"),
    # Health & Fitness
    ("sports equipment", "health"), ("workout", "health"), ("exercise", "health"),
    ("fitness", "health"), ("yoga", "health"), ("gym", "health"),
    ("running", "health"), ("protein shaker", "health"), ("resistance band", "health"),
    ("dumbbell", "health"), ("health", "health"),
    # Games, Toys & Sports
    ("video game", "games"), ("gaming", "games"), ("board game", "games"),
    ("toy", "games"), ("toys", "games"), ("puzzle", "games"),
    ("lego", "games"), ("sporting", "games"), ("sports", "games"),
    ("football", "games"), ("basketball", "games"), ("soccer", "games"),
    # Fashion & Lifestyle
    ("clothing", "fashion"), ("apparel", "fashion"), ("fashion", "fashion"),
    ("shoes", "fashion"), ("sneakers", "fashion"), ("dress", "fashion"),
    ("jacket", "fashion"), ("handbag", "fashion"), ("backpack", "fashion"),
    ("wallet", "fashion"), ("jewelry", "fashion"), ("lifestyle", "fashion"),
]

BOARD_ALIASES = {
    "Health & Fitness": ["health & fitness", "health and fitness", "fitness & wellness", "fitness", "health", "wellness"],
    "Beauty & Personal Care": ["beauty & personal care", "beauty and personal care", "beauty", "personal care", "skincare"],
    "Smartphones & Tablets": ["smartphones & tablets", "smartphones and tablets", "smartphones", "tablets", "phone accessories", "iphone accessories"],
    "PCs, Laptops & Home Electronics": ["pcs, laptops & home electronics", "pcs laptops and home electronics", "electronics & gadgets", "electronics", "gadgets", "tech", "computers"],
    "Home, Kitchen & Dining": ["home, kitchen & dining", "home kitchen and dining", "home & kitchen", "home and kitchen", "home", "kitchen", "household"],
    "Books & Learning": ["books & learning", "books and learning", "books & reading", "books and reading", "books", "book", "reading", "learning"],
    "Games, Toys & Sports": ["games, toys & sports", "games toys and sports", "games", "toys", "sports", "gaming"],
    "Fashion & Lifestyle": ["fashion & lifestyle", "fashion and lifestyle", "fashion", "clothing", "lifestyle"],
    DEFAULT_BOARD_NAME: ["product pins", "products", "product pin"],
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
