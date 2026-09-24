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
    "Watches & Clocks": "987906936951155598",
    "Beauty & Personal Care": "987906936951153833",
    "Books & Learning": "987906936951145056",
    "Baby & Kids": "987906936951153816",
    "Electronics & Gadgets": "987906936951145057",
    "Everything Else": "987906936951147704",
    "Fashion & Lifestyle": "987906936951147683",
    "Sports, Games & Toys": "987906936951147684",
    "Health & Fitness": "987906936951147682",
    "Home, Kitchen & Dining": "987906936951145744",
    "Office & Productivity": "987906936951148020",
    "Pet Supplies": "987906936951148021",
    "Travel & Camping": "987906936951147708",
    "PC's, Laptops & TV's": "987906936951153890",
    "Smartphones & Tablets": "987906936951153891",
}
BOARD_NAME_ALIASES = {
    "Games, Toys & Sports": "Sports, Games & Toys",
    "Sports Games & Toys": "Sports, Games & Toys",
}

SECTION_IDS = {
    "electronics_smartphones": "3856307780748685888",
    "electronics_pc_home": "3856307171131803008",
    "health_baby_kids": "3856308624642316032",
    "health_beauty_personal": "3856309112490652608",
}
CATEGORY_BOARD_MAP = {
    "electronics_smartphones": "Smartphones & Tablets",
    "electronics_pc_home": "Electronics & Gadgets",
    "electronics_root": "Electronics & Gadgets",
    "health_baby_kids": "Health & Fitness",
    "health_beauty_personal": "Health & Fitness",
    "health_root": "Health & Fitness",
    "beauty": "Health & Fitness",
    "kids": "Health & Fitness",
    "smartphones": "Smartphones & Tablets",
    "pcs": "PC's, Laptops & TV's",
    "electronics": "Electronics & Gadgets",
    "pc_tv": "PC's, Laptops & TV's",
    "smartphones_tablets": "Smartphones & Tablets",
    "health": "Health & Fitness",
    "home": "Home, Kitchen & Dining",
    "appliances": "Home, Kitchen & Dining",
    "watches_clocks": "Watches & Clocks",
    "books": "Books & Learning",
    "games": "Sports, Games & Toys",
    "fashion": "Fashion & Lifestyle",
    "beauty": "Beauty & Personal Care",
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


def _text(product: Dict[str, Any]) -> str:
    """Flatten secondary product fields for classification."""
    parts = []
    for k in ("description", "title", "product_type", "category", "brand", "bullet_points", "features"):
        v = product.get(k)
        if v is None:
            continue
        if isinstance(v, (list, tuple)):
            parts.append(" ".join(str(x) for x in v if x))
        else:
            parts.append(str(v))
    return " ".join(parts).lower()


def _name_text(product: Dict[str, Any]) -> str:
    """Primary product name/title used for classification."""
    return str(product.get("name") or product.get("title") or "").lower()


# Product-family rules: ordered most-specific -> broad. First match wins.
_FAMILY_RULES = [
    ("office", (
        "office chair", "executive chair", "desk chair", "computer chair",
        "ergonomic chair", "task chair", "workstation chair", "office seating",
        "mesh chair", "swivel chair", "lazboy", "la-z-boy", "la z boy",
        "herman miller", "steelcase", "secretlab", "traditions executive",
        "executive office", "desk organizer", "desktop organizer", "file cabinet",
        "filing cabinet", "desk lamp", "monitor arm", "monitor stand", "laptop stand",
        "office supplies", "stapler", "whiteboard", "corkboard", "planner",
        "standing desk", "sit stand desk", "office desk", "computer desk", "desk mat",
    )),
    ("health_beauty_personal", (
        "skincare", "skin care", "face serum", "facial serum", "moisturizer",
        "moisturiser", "shampoo", "conditioner", "face wash", "cleanser", "sunscreen",
        "makeup", "cosmetic", "cosmetics", "lipstick", "foundation", "mascara",
        "concealer", "toner", "body lotion", "personal care", "beauty product",
        "hair care", "haircare", "perfume", "fragrance", "acne patch", "pimple patch",
        "mighty patch", "hydrocolloid", "retinol", "niacinamide", "facial mask",
        "sheet mask", "exfoliant", "body wash", "deodorant",
    )),
    ("health_baby_kids", (
        "baby wipe", "baby wipes", "baby diaper", "baby diapers", "diaper", "diapers",
        "newborn", "infant", "toddler", "baby bottle", "baby food", "baby stroller",
        "baby monitor", "baby carrier", "pacifier", "crib", "onesie", "baby formula",
        "nursing", "breast pump", "changing pad", "kids clothing", "children toy",
        "childrens toy", "children's toy",
    )),
    ("pets", (
        "cat litter", "litter box", "dog food", "cat food", "pet food", "dog leash",
        "dog bed", "cat tree", "pet toy", "pet toys", "pet grooming", "pet supply",
        "pet supplies", "aquarium", "fish tank", "bird cage", "puppy", "kitten",
        "dog treat", "cat treat",
    )),
    ("fashion", (
        "running shoe", "running shoes", "running sneaker", "running sneakers",
        "athletic shoe", "athletic shoes", "jogging shoe", "trainers", "sneakers",
        "sneaker", "shoe", "shoes", "boots", "boot", "sandals", "heels", "loafer",
        "slip-on", "hoodie", "sweatshirt", "t-shirt", "tshirt", "jeans", "leggings",
        "dress", "apparel", "clothing", "jacket", "coat", "socks", "handbag", "purse",
        "wallet", "belt", "sunglasses", "baseball cap", "beanie", "scarf",
    )),
    ("games", (
        "gaming headset", "gaming headphones", "gaming controller", "gaming mouse",
        "gaming keyboard", "xbox", "playstation", "nintendo", "video game", "videogame",
        "steam deck", "board game", "card game", "puzzle", "jigsaw",
        "lcd writing tablet", "drawing tablet kids", "action figure", "lego",
        "building blocks", "basketball", "soccer ball", "football", "tennis racket",
        "baseball bat", "golf club", "sports equipment", "sporting goods", "rc car",
        "stuffed animal", "toy car",
    )),
    ("travel", (
        "camping", "hiking", "travel backpack", "packing cube", "packing cubes",
        "suitcase", "luggage", "carry-on", "carry on", "tent", "sleeping bag",
        "camping stove", "hiking poles", "travel adapter", "neck pillow",
        "air mattress", "air bed", "airbed", "inflatable mattress", "inflatable bed",
        "camping mattress", "camping air mattress", "dura-beam", "dura beam",
        "pillow rest", "intex air", "portable inflatable",
    )),
    ("automotive", (
        "car charger", "car mount", "dash cam", "dashcam", "car vacuum", "automotive",
        "car care", "tire inflator", "jump starter", "car seat cover", "motor oil",
        "windshield", "car accessory", "car accessories", "obd2",
    )),
    ("home", (
        "instant pot", "air fryer", "coffee maker", "coffee machine", "pressure cooker",
        "slow cooker", "multicooker", "cookware", "kitchen", "blender", "toaster",
        "microwave", "dishwasher", "refrigerator", "ice maker", "countertop ice",
        "ice machine", "stanley quencher", "tumbler", "water bottle", "vacuum cleaner",
        "robot vacuum", "roomba", "bedding", "comforter", "pillow", "mattress",
        "sheet set", "knife set", "cutting board", "dinnerware", "frying pan",
        "saucepan", "air purifier", "humidifier", "dehumidifier", "space heater",
        "laundry", "detergent", "trash can", "storage bin",
    )),
    ("health_root", (
        "whey protein", "protein powder", "protein", "creatine", "pre-workout",
        "preworkout", "vitamin", "vitamins", "supplement", "supplements", "yoga mat",
        "yoga", "dumbbell", "dumbbells", "kettlebell", "resistance band",
        "resistance bands", "treadmill", "exercise bike", "rowing machine",
        "weight bench", "fitness", "workout", "gym", "massage gun", "foam roller",
        "ice pack", "reusable ice", "cold pack", "first aid", "thermometer",
        "pulse oximeter", "blood pressure", "blood-pressure", "blood pressure monitor",
        "blood-pressure monitor", "bp monitor", "fitness tracker",
    )),
    ("books", (
        "paperback", "hardcover", "textbook", "novel", "cookbook", "self-help",
        "self help", "audiobook",
    )),
    ("pc_tv", (
        "desktop pc", "desktop computer", "personal computer", "gaming pc", "mini pc", "all-in-one pc",
        "laptop", "notebook computer", "chromebook", "macbook", "computer monitor", "pc monitor",
        "monitor", "television", "smart tv", "led tv", "oled tv", "4k tv", "8k tv", "tv", "tvs",
        "pc case", "computer case", "motherboard", "cpu", "processor", "graphics card", "gpu", "ram",
        "memory module", "ddr4", "ddr5", "ssd", "nvme", "hard drive", "hdd", "pcie", "desktop memory",
        "laptop ram", "laptop ssd", "docking station", "laptop dock", "computer power supply",
        "power supply unit", "psu", "pc cooling", "cpu cooler", "computer keyboard", "computer mouse",
        "webcam for computer", "computer speakers",
    )),
    ("smartphones_tablets", (
        "iphone", "ipad", "smartphone", "android phone", "cell phone", "mobile phone", "android tablet",
        "tablet", "galaxy s", "galaxy note", "galaxy tab", "pixel phone", "oneplus", "phone case",
        "iphone case", "ipad case", "tablet case", "screen protector", "screen guard", "screen film",
        "tempered glass phone", "phone charger", "phone charging cable", "usb-c phone cable", "magsafe",
        "wireless phone charger", "phone holder", "phone stand", "tablet stand", "stylus", "apple pencil",
        "tablet keyboard", "tablet cover", "phone battery", "replacement phone screen",
    )),
    ("watches_clocks", (
        "wristwatch", "wrist watches", "watches", "smartwatch", "smart watch",
        "digital watch", "analog watch", "alarm clock", "wall clock", "desk clock",
        "table clock", "mantel clock", "grandfather clock", "timepiece",
    )),
    ("electronics_root", (
        "airpods", "earbuds", "earbud", "headphones", "headphone", "wireless earbuds",
        "bluetooth earbuds", "true wireless", "kindle", "e-reader", "e reader",
        "ereader", "ebook reader", "e-book reader", "paperwhite", "screen protector",
        "screen guard", "screen film", "phone case", "iphone case", "ipad case",
        "tablet case", "smartphone case", "cell phone case", "protective case",
        "phone cover", "phone shell", "case for iphone", "case for galaxy",
        "charger", "charging cable",
        "usb cable", "usb-c", "usb c", "power bank", "wireless charger", "magsafe",
        "laptop bag", "laptop sleeve", "laptop case", "computer bag", "webcam",
        "smartwatch", "smart watch", "camera", "action camera", "microphone",
        "lavalier", "wireless mic", "usb microphone", "bluetooth speaker",
        "smart speaker", "soundbar", "phone holder", "phone stand", "tablet stand",
        "stylus", "hdmi", "usb hub",
    )),
    ("electronics_smartphones", (
        "iphone", "ipad", "smartphone", "android phone", "cell phone", "mobile phone",
        "galaxy tab", "galaxy s", "pixel phone", "tablet",
    )),
    ("electronics_pc_home", (
        "macbook", "laptop", "notebook computer", "desktop pc", "desktop computer",
        "gaming pc", "personal computer", "television", "smart tv", "led tv", "oled tv",
        "4k tv", "tv", "tvs", "monitor", "pc monitor", "computer monitor", "mini pc",
        "chromebook",
    )),
]

def _has_term(text: str, term: str) -> bool:
    term = term.lower().strip()
    if not term:
        return False
    if term in {"tv", "tvs", "pc", "mic", "toy", "bag", "mat", "pad"}:
        return bool(re.search(rf"\b{re.escape(term)}\b", text))
    if " " in term or "-" in term:
        return term in text
    return bool(re.search(rf"\b{re.escape(term)}\b", text)) or term in text

def detect_product_category(product: Dict[str, Any]) -> str:
    """Return a routing key using ordered product-family rules."""
    text = _text(product)
    name = _name_text(product)
    haystack = f"{name} {text}"
    if re.search(r"\b(iphone|ipad|smartphone|android phone|cell phone|mobile phone|android tablet|tablet|galaxy s|galaxy note|galaxy tab|pixel phone|phone case|iphone case|ipad case|tablet case|screen protector|phone charger|phone charging cable|magsafe|phone holder|phone stand|tablet stand|apple pencil|tablet keyboard|tablet cover|phone battery|replacement phone screen)\b", haystack): return "smartphones_tablets"
    if re.search(r"\b(desktop pc|desktop computer|personal computer|gaming pc|mini pc|laptop|notebook computer|chromebook|macbook|computer monitor|pc monitor|television|smart tv|led tv|oled tv|4k tv|8k tv|pc case|computer case|motherboard|graphics card|gpu|ram|memory module|ddr4|ddr5|ssd|nvme|hard drive|hdd|pcie|laptop ram|laptop ssd|docking station|laptop dock|computer power supply|power supply unit|psu|cpu cooler|computer keyboard|computer mouse|webcam for computer)\b", haystack): return "pc_tv"
    for category, terms in _FAMILY_RULES:
        for term in terms:
            if _has_term(haystack, term):
                return category
    if re.search(r"\b(lazboy|la-z-boy|herman\s*miller|steelcase|secretlab)\b", haystack):
        return "office"
    if re.search(r"\bexecutive\b", haystack) and re.search(
        r"\b(chair|seat|seating|furniture|office|desk|traditions|comfort|leather|recliner)\b",
        haystack,
    ):
        return "office"
    return "general"





def classify_with_confidence(product: Dict[str, Any]) -> Dict[str, Any]:
    """Return category plus confidence for gates."""
    text = _text(product)
    name = _name_text(product)
    haystack = f"{name} {text}"
    for category, terms in _FAMILY_RULES:
        for term in terms:
            if _has_term(haystack, term):
                multi = " " in term or "-" in term
                in_name = _has_term(name, term)
                confidence = "HIGH" if (multi and in_name) else "MEDIUM"
                return {
                    "category": category,
                    "confidence": confidence,
                    "matched_term": term,
                    "match_in_name": in_name,
                }
    if re.search(r"\b(lazboy|la-z-boy|herman\s*miller|steelcase|secretlab)\b", haystack):
        return {"category": "office", "confidence": "HIGH", "matched_term": "brand_office", "match_in_name": True}
    if re.search(r"\bexecutive\b", haystack) and re.search(
        r"\b(chair|seat|seating|furniture|office|desk|traditions|comfort|leather|recliner)\b",
        haystack,
    ):
        return {"category": "office", "confidence": "MEDIUM", "matched_term": "executive+furniture", "match_in_name": True}
    return {"category": "general", "confidence": "LOW", "matched_term": None, "match_in_name": False}


def resolve_pinterest_destination(product: Dict[str, Any]) -> Dict[str, Any]:
    """Canonical destination used by every input path after normalization."""
    info = classify_with_confidence(product)
    category = info["category"]
    board_name = CATEGORY_BOARD_MAP.get(category, DEFAULT_BOARD_NAME)
    section_id = SECTION_IDS.get(category) if category not in ("pc_tv", "smartphones_tablets") else None
    board_id = PERMANENT_BOARD_IDS.get(board_name)
    return {
        "category": category,
        "board_id": board_id,
        "board_name": board_name,
        "section_id": section_id,
        "section_name": {
            "electronics_smartphones": "Smartphones & Tablets",
            "electronics_pc_home": "PC's, Laptops & TV's",
            "health_baby_kids": "Baby & Kids",
            "health_beauty_personal": "Beauty & Personal Care",
        }.get(category),
        "is_root": section_id is None,
        "confidence": info["confidence"],
        "matched_term": info.get("matched_term"),
        "reason": (
            "explicit section whitelist match"
            if section_id
            else "no matching section whitelist; parent-board root"
        ),
    }


def preferred_board_name(category_key: str) -> str:
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
    """Disabled: runtime_hardening owns the canonical publish path."""
    return
    try:  # pragma: no cover
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


# Legacy section publish patch is intentionally NOT auto-installed.
# runtime_hardening.install() owns the single canonical publish+verify path.
