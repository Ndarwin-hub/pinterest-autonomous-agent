"""Canonical Pinterest destination resolver with strict section whitelists.

A section is an explicit whitelist, never a broad category. Products that do not
clearly belong to a section are intentionally routed to the parent board root.
"""
from __future__ import annotations
import contextvars
import re
from typing import Any, Dict, List, Optional

DEFAULT_BOARD_NAME = "Everything Else"
LEGACY_BOARD_NAMES = {"General Pins", "Stuff to buy", "Product Pins"}
LEGACY_BOARD_IDS = {"987906936951142945", "987906936951144580"}
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

_destination_section = contextvars.ContextVar("pinterest_destination_section", default=None)
def current_section_id() -> Optional[str]:
    return _destination_section.get()
def _set_section(section_id: Optional[str]) -> None:
    _destination_section.set(section_id)

# Explicit accessory exclusions run before device/product whitelists. This prevents
# "iPhone case" or "laptop bag" from inheriting the primary device's section.
ELECTRONICS_ROOT_TERMS = (
    "screen protector", "screen guard", "phone case", "smartphone case", "cell phone case",
    "iphone case", "ipad case", "tablet case", "laptop bag", "laptop sleeve", "laptop case",
    "computer bag", "headphones", "headphone", "earbuds", "earbud", "airpods",
    "usb cable", "charging cable", "charge cable", "charger", "power bank", "mouse",
    "keyboard", "webcam", "smartwatch", "smart watch", "camera", "gaming accessory",
    "phone holder", "phone stand", "tablet stand", "stylus", "screen film",
)
BABY_TERMS = (
    "baby diaper", "baby diapers", "newborn diaper", "newborn diapers", "baby bottle",
    "baby feeding", "baby stroller", "baby monitor", "baby carrier", "baby food",
    "infant", "newborn", "toddler", "kids toy", "kids clothing", "children's toy",
    "children toy", "childrens toy", "kids product",
)
BEAUTY_TERMS = (
    "skincare", "skin care", "face serum", "facial serum", "moisturizer", "moisturiser",
    "shampoo", "conditioner", "face wash", "cleanser", "sunscreen", "makeup", "cosmetic",
    "cosmetics", "lipstick", "foundation", "mascara", "concealer", "toner", "body lotion",
    "personal care", "beauty product", "hair care", "haircare", "perfume", "fragrance",
)
HEALTH_ROOT_TERMS = (
    "yoga", "dumbbell", "dumbbells", "resistance band", "resistance bands", "exercise",
    "fitness", "workout", "gym", "protein powder", "protein", "vitamin", "vitamins",
    "supplement", "supplements", "blood pressure", "blood-pressure", "blood pressure monitor",
    "fitness tracker", "treadmill", "exercise bike", "rowing machine", "weight bench",
    "health monitor", "thermometer", "pulse oximeter", "first aid", "massage gun",
)

# General board rules are intentionally conservative. Section routing is handled by
# explicit whitelists above; unrelated products remain on the parent board/root.
GENERAL_RULES = (
    ("book", "books"), ("novel", "books"), ("textbook", "books"),
    ("cookware", "home"), ("kitchen", "home"), ("air fryer", "home"), ("coffee maker", "home"),
    ("instant pot", "home"), ("pressure cooker", "home"), ("multicooker", "home"), ("slow cooker", "home"),
    ("fashion", "fashion"), ("shoes", "fashion"), ("shoe", "fashion"), ("sneakers", "fashion"), ("sneaker", "fashion"), ("clothing", "fashion"),
    ("apparel", "fashion"), ("travel", "travel"), ("camping", "travel"), ("hiking", "travel"),
    ("pet supplies", "pets"), ("dog food", "pets"), ("cat food", "pets"),
    ("automotive", "automotive"), ("car accessories", "automotive"),
    ("office", "office"), ("planner", "office"), ("desk organizer", "office"),
)

def _text(product: Dict[str, Any]) -> str:
    fields = ("name", "title", "description", "product_type", "productType", "category", "subcategory", "brand")
    return " ".join(str(product.get(k) or "") for k in fields).lower()

def _name_text(product: Dict[str, Any]) -> str:
    return " ".join(str(product.get(k) or "") for k in ("name", "title", "product_type", "productType")).lower()

def _has_term(text: str, term: str) -> bool:
    if term in ("tv", "pc"):
        return bool(re.search(rf"\b{re.escape(term)}\b", text))
    return term in text

def detect_product_category(product: Dict[str, Any]) -> str:
    """Return a routing key; section keys are strict whitelists."""
    text = _text(product)
    name = _name_text(product)

    # Accessories must never inherit a compatible primary device's section.
    if any(_has_term(text, t) for t in ELECTRONICS_ROOT_TERMS):
        return "electronics_root"

    # Actual smartphones/tablets only.
    smartphone_terms = ("iphone", "ipad", "smartphone", "android phone", "cell phone", "mobile phone", "galaxy tab", "tablet")
    if any(_has_term(name, t) for t in smartphone_terms):
        return "electronics_smartphones"

    # Actual PC/laptop/TV/home-electronics products in this defined section.
    pc_terms = ("macbook", "laptop", "notebook computer", "desktop pc", "desktop computer", "gaming pc", "personal computer", "television", " tv ", "monitor")
    if any(_has_term(name, t) for t in pc_terms):
        return "electronics_pc_home"

    # Health & Fitness sections are also strict whitelists.
    if any(_has_term(name, t) for t in BABY_TERMS):
        return "health_baby_kids"
    if any(_has_term(name, t) for t in BEAUTY_TERMS):
        return "health_beauty_personal"
    if any(_has_term(text, t) for t in HEALTH_ROOT_TERMS):
        return "health_root"

    for term, category in GENERAL_RULES:
        if _has_term(text, term):
            return category
    return "general"


def resolve_pinterest_destination(product: Dict[str, Any]) -> Dict[str, Any]:
    """Canonical destination used by every input path after normalization."""
    category = detect_product_category(product)
    board_name = CATEGORY_BOARD_MAP.get(category, DEFAULT_BOARD_NAME)
    section_id = SECTION_IDS.get(category)
    board_id = PERMANENT_BOARD_IDS.get(board_name)
    return {
        "category": category,
        "board_id": board_id,
        "board_name": board_name,
        "section_id": section_id,
        "section_name": {
            "electronics_smartphones": "Smartphones & Tablets",
            "electronics_pc_home": "PCs, Laptops & Home Electronics",
            "health_baby_kids": "Baby & Kids",
            "health_beauty_personal": "Beauty & Personal Care",
        }.get(category),
        "is_root": section_id is None,
        "reason": "explicit section whitelist match" if section_id else "no matching section whitelist; parent-board root",
    }


def preferred_board_name(category_key: str) -> str:
    # Backward-compatible API used by the existing runtime wiring.
    _set_section(SECTION_IDS.get(category_key))
    return CATEGORY_BOARD_MAP.get(category_key, DEFAULT_BOARD_NAME)


def _normalize_board_name(name: str) -> str:
    return " ".join((name or "").lower().replace("&", "and").split())


def find_matching_board(items: List[Any], preferred_name: str) -> Optional[str]:
    if not items:
        return None
    target = _normalize_board_name(preferred_name)
    permanent = PERMANENT_BOARD_IDS.get(preferred_name)
    for b in items:
        bid = str(b.get("id") or b.get("board_id") or "")
        name = (b.get("name") or "").strip()
        if bid in LEGACY_BOARD_IDS or name in LEGACY_BOARD_NAMES:
            continue
        if permanent and bid == permanent:
            return bid
    for b in items:
        bid = str(b.get("id") or b.get("board_id") or "")
        name = (b.get("name") or "").strip()
        if bid in LEGACY_BOARD_IDS or name in LEGACY_BOARD_NAMES:
            continue
        if _normalize_board_name(name) == target:
            return bid or None
    return None


def _install_section_publish_patch():
    """Legacy compatibility patch: inject the current section into Pin creation."""
    try:
        import agent, json
        original = getattr(agent, "publish_and_verify", None)
        if not original or getattr(original, "_section_aware", False):
            return
        runner = getattr(agent, "run_composio_tool")
        async def section_aware_publish(board_id, title, description, alt_text, image_mode, image_value, link, job_store, job_id, pin_index):
            section_id = current_section_id()
            job_store.update(job_id, progress=f"Publishing Pin {pin_index}/5")
            media_source = ({"source_type":"image_base64","content_type":"image/jpeg","data":image_value} if image_mode == "base64" else {"source_type":"image_url","url":image_value})
            args = {"board_id":board_id,"title":title[:100],"description":description[:800],"alt_text":alt_text[:500],"link":link,"media_source":media_source}
            if section_id:
                args["board_section_id"] = section_id
            data = await runner("PINTEREST_CREATE_PIN", args, retries=2)
            pin_id = str(data.get("id") or data.get("pin_id") or (data.get("data") or {}).get("id") or "")
            if not pin_id:
                raise RuntimeError(f"Pin created but no ID: {json.dumps(data)[:400]}")
            verified = await runner("PINTEREST_GET_PIN", {"pin_id":pin_id}, retries=1)
            actual_board = str(verified.get("board_id") or ((verified.get("board") or {}).get("id") if isinstance(verified.get("board"),dict) else "") or "")
            actual_section = str(verified.get("board_section_id") or ((verified.get("board_section") or {}).get("id") if isinstance(verified.get("board_section"),dict) else "") or "")
            if actual_board and actual_board != str(board_id):
                raise RuntimeError(f"Pin {pin_index}: board verification mismatch; intended {board_id}, actual {actual_board}")
            if section_id and actual_section != str(section_id):
                raise RuntimeError(f"Pin {pin_index}: section verification mismatch; intended {section_id}, actual {actual_section}")
            return {"pin_id":pin_id,"pin_url":f"https://www.pinterest.com/pin/{pin_id}/","verified":bool(verified and verified.get("id")),"destination_url":link,"board_id":board_id,"board_section_id":section_id,"section_verified_independently":bool(section_id and actual_section==str(section_id)),"title":title}
        section_aware_publish._section_aware = True
        agent.publish_and_verify = section_aware_publish
    except Exception:
        pass

_install_section_publish_patch()
