"""Canonical Pinterest destination resolver.

Sections are strict whitelists: a product enters a section only when it
belongs to that section's defined product group. Everything else remains on
the parent board root.
"""
from __future__ import annotations
import contextvars
from typing import Any, Dict, List, Optional

DEFAULT_BOARD_NAME = "Everything Else"
LEGACY_BOARD_NAMES = {"General Pins", "Stuff to buy", "Product Pins"}
LEGACY_BOARD_IDS = {"987906936951142945", "987906936951144580"}

# Live Pinterest structure verified 2026-09-14.
PERMANENT_BOARD_IDS = {
    "Automotive & Tools": "987906936951148022",
    "Books & Learning": "987906936951145056",
    "Electronics & Gadgets": "987906936951145057",
    "Everything Else": "987906936951147704",
    "Fashion & Lifestyle": "987906936951147683",
    "Games, Toys & Sports": "987906936951147684",
    "Health & Fitness": "987906936951147682",
    "Home, Kitchen & Dining": "987906936951145744",
    "Office & Productivity": "987906936951148020",
    "Pet Supplies": "987906936951148021",
    "Travel & Camping": "987906936951147708",
}

SECTION_IDS = {
    "electronics_smartphones": "3856307780748685888",
    "electronics_pc_home": "3856307171131803008",
    "health_baby_kids": "3856308624642316032",
    "health_beauty_personal": "3856309112490652608",
}

# Context is consumed by the existing publish path so the current 5-Pin
# workflow can pass board_section_id without replacing its architecture.
_destination_section = contextvars.ContextVar("pinterest_destination_section", default=None)

def current_section_id() -> Optional[str]:
    return _destination_section.get()

def _set_section(section_id: Optional[str]) -> None:
    _destination_section.set(section_id)

CATEGORY_BOARD_MAP = {
    "electronics_smartphones": "Electronics & Gadgets",
    "electronics_pc_home": "Electronics & Gadgets",
    "electronics_root": "Electronics & Gadgets",
    "health_baby_kids": "Health & Fitness",
    "health_beauty_personal": "Health & Fitness",
    "health_root": "Health & Fitness",
    "beauty": "Health & Fitness",
    "kids": "Health & Fitness",
    "smartphones": "Electronics & Gadgets",
    "pcs": "Electronics & Gadgets",
    "electronics": "Electronics & Gadgets",
    "health": "Health & Fitness",
    "home": "Home, Kitchen & Dining",
    "books": "Books & Learning",
    "games": "Games, Toys & Sports",
    "fashion": "Fashion & Lifestyle",
    "travel": "Travel & Camping",
    "pets": "Pet Supplies",
    "automotive": "Automotive & Tools",
    "office": "Office & Productivity",
    "general": DEFAULT_BOARD_NAME,
}

# Ordered rules. Specific products/accessories are deliberately separated so
# accessories never inherit the section of the device they support.
RULES = [
    ("screen protector", "electronics_root"), ("phone case", "electronics_root"),
    ("smartphone case", "electronics_root"), ("cell phone case", "electronics_root"),
    ("headphones", "electronics_root"), ("headphone", "electronics_root"),
    ("earbuds", "electronics_root"), ("earbud", "electronics_root"),
    ("airpods", "electronics_root"), ("usb cable", "electronics_root"),
    ("charging cable", "electronics_root"), ("charger", "electronics_root"),
    ("power bank", "electronics_root"), ("mouse", "electronics_root"),
    ("keyboard", "electronics_root"), ("webcam", "electronics_root"),
    ("smartwatch", "electronics_root"), ("smart watch", "electronics_root"),
    ("camera", "electronics_root"), ("gaming accessory", "electronics_root"),
    ("gaming accessories", "electronics_root"),
    ("iphone", "electronics_smartphones"), ("ipad", "electronics_smartphones"),
    ("smartphone", "electronics_smartphones"), ("android phone", "electronics_smartphones"),
    ("cell phone", "electronics_smartphones"), ("mobile phone", "electronics_smartphones"),
    ("galaxy tab", "electronics_smartphones"), ("tablet", "electronics_smartphones"),
    ("macbook", "electronics_pc_home"), ("laptop", "electronics_pc_home"),
    ("notebook computer", "electronics_pc_home"), ("desktop pc", "electronics_pc_home"),
    ("desktop computer", "electronics_pc_home"), ("gaming pc", "electronics_pc_home"),
    ("personal computer", "electronics_pc_home"), ("television", "electronics_pc_home"),
    (" tv ", "electronics_pc_home"), ("monitor", "electronics_pc_home"),
    ("baby diapers", "health_baby_kids"), ("baby diaper", "health_baby_kids"),
    ("baby bottle", "health_baby_kids"), ("baby stroller", "health_baby_kids"),
    ("baby", "health_baby_kids"), ("toddler", "health_baby_kids"),
    ("kids", "health_baby_kids"), ("children", "health_baby_kids"),
    ("skincare", "health_beauty_personal"), ("skin care", "health_beauty_personal"),
    ("face serum", "health_beauty_personal"), ("serum", "health_beauty_personal"),
    ("moisturizer", "health_beauty_personal"), ("shampoo", "health_beauty_personal"),
    ("conditioner", "health_beauty_personal"), ("makeup", "health_beauty_personal"),
    ("cosmetic", "health_beauty_personal"), ("beauty", "health_beauty_personal"),
    ("personal care", "health_beauty_personal"),
    ("yoga", "health_root"), ("dumbbell", "health_root"), ("dumbbells", "health_root"),
    ("protein powder", "health_root"), ("protein", "health_root"),
    ("resistance band", "health_root"), ("exercise", "health_root"),
    ("fitness", "health_root"), ("workout", "health_root"), ("gym", "health_root"),
    ("blood pressure", "health_root"), ("blood-pressure", "health_root"),
    ("vitamin", "health_root"), ("supplement", "health_root"),
    ("health", "health_root"),
    ("book", "books"), ("novel", "books"), ("textbook", "books"),
    ("cookware", "home"), ("kitchen", "home"), ("air fryer", "home"),
    ("coffee maker", "home"), ("fashion", "fashion"), ("shoes", "fashion"),
    ("sneakers", "fashion"), ("clothing", "fashion"), ("apparel", "fashion"),
    ("travel", "travel"), ("camping", "travel"), ("hiking", "travel"),
    ("pet supplies", "pets"), ("dog", "pets"), ("cat", "pets"),
    ("automotive", "automotive"), ("car accessories", "automotive"),
    ("office", "office"), ("planner", "office"), ("desk organizer", "office"),
]

def detect_product_category(product: Dict[str, Any]) -> str:
    name = str(product.get("name") or "").lower()
    desc = str(product.get("description") or "").lower()
    blob = f" {name} {desc} "
    # Product title gets priority; then description. First specific match wins.
    for term, category in RULES:
        needle = term.lower()
        if needle in blob:
            return category
    return "general"

def preferred_board_name(category_key: str) -> str:
    _set_section(None)
    if category_key == "electronics_smartphones":
        _set_section(SECTION_IDS["electronics_smartphones"])
    elif category_key == "electronics_pc_home":
        _set_section(SECTION_IDS["electronics_pc_home"])
    elif category_key == "health_baby_kids":
        _set_section(SECTION_IDS["health_baby_kids"])
    elif category_key == "health_beauty_personal":
        _set_section(SECTION_IDS["health_beauty_personal"])
    return CATEGORY_BOARD_MAP.get(category_key, DEFAULT_BOARD_NAME)

def _normalize_board_name(name: str) -> str:
    return " ".join((name or "").lower().replace("&", "and").split())

def find_matching_board(items: List[Any], preferred_name: str) -> Optional[str]:
    if not items:
        return None
    permanent_id = PERMANENT_BOARD_IDS.get(preferred_name)
    for b in items:
        bid = str(b.get("id") or b.get("board_id") or "")
        name = (b.get("name") or "").strip()
        if bid in LEGACY_BOARD_IDS or name in LEGACY_BOARD_NAMES:
            continue
        if permanent_id and bid == permanent_id:
            return bid
    target = _normalize_board_name(preferred_name)
    for b in items:
        bid = str(b.get("id") or b.get("board_id") or "")
        name = (b.get("name") or "").strip()
        if bid in LEGACY_BOARD_IDS or name in LEGACY_BOARD_NAMES:
            continue
        if _normalize_board_name(name) == target:
            return bid or None
    return None

# Backward-compatible names used by older callers.
CATEGORY_BOARD_MAP.update({
    "smartphones": "Electronics & Gadgets", "pcs": "Electronics & Gadgets",
    "beauty": "Health & Fitness", "kids": "Health & Fitness",
})
