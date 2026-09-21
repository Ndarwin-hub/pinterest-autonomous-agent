"""Canonical serial Pinterest-board scheduler map and exact product scopes."""
from __future__ import annotations
from typing import Any,Dict,List,Optional,Tuple
from board_org import LEGACY_BOARD_IDS,LEGACY_BOARD_NAMES,PERMANENT_BOARD_IDS,DEFAULT_BOARD_NAME

BOARD_SCOPES={
 "Appliances & Home":"Kitchen and household appliances: countertop ice makers, air fryers, coffee makers, blenders, toaster ovens, humidifiers, air purifiers, vacuums, dehumidifiers and other powered home appliances.",
 "Automotive & Tools":"Automotive products and components: car emergency tools, garage tools, tire inflators, jump starters, OBD tools, car accessories and DIY hardware specifically for vehicles or garages.",
 "Baby & Kids":"Baby and child products: diapers, baby wipes, bottles, feeding supplies, pacifiers, strollers, carriers, baby monitors, nursery essentials and children-specific products.",
 "Beauty & Personal Care":"Beauty and personal-care products: skincare, acne/pimple patches, cosmetics, makeup, hair care, shampoo, conditioner, moisturizers, sunscreen, fragrance and grooming products.",
 "Books & Learning":"Books and learning materials: physical books, ebooks, textbooks, workbooks, educational resources and self-development books.",
 "Electronics & Gadgets":"General electronics not specifically belonging to smartphones/tablets or PCs/laptops/TVs: headphones, earbuds, speakers, microphones, surge protectors, generic chargers/cables, power banks and electronic gadgets.",
 "Everything Else":"Only products that genuinely match none of the other defined board scopes. This is the sole fallback board.",
 "Fashion & Lifestyle":"Clothing, footwear and fashion accessories: running shoes, sneakers, boots, apparel, bags, wallets, belts, hats, sunglasses and lifestyle fashion items.",
 "Health & Fitness":"Health, wellness and fitness products: whey/protein, supplements, exercise equipment, resistance bands, yoga gear, reusable ice packs and non-device health essentials.",
 "Home, Kitchen & Dining":"Non-appliance home and kitchen goods: cookware, kitchen tools, tumblers/water bottles, dinnerware, storage, bedding, furniture and household organization products.",
 "Office & Productivity":"Office and productivity products: desk organizers, planners, office supplies, monitor arms, desk accessories and work/study organization tools.",
 "PC's, Laptops & TV's":"Desktop PCs, laptops, Chromebooks, computer monitors, televisions/smart TVs, and exact components/accessories such as RAM, SSDs, GPUs, motherboards, PC cases, PSUs, docks and computer-specific peripherals.",
 "Pet Supplies":"Pet-specific products: cat litter, litter boxes, pet food accessories, grooming supplies, pet beds, leashes, toys and other dog/cat/fish/bird supplies.",
 "Smartphones & Tablets":"Smartphones and tablets plus exact device components/accessories: phone/tablet cases, screen protectors, device chargers/cables, MagSafe accessories, tablet keyboards, styluses and replacement screens/batteries.",
 "Sports, Games & Toys":"Sports, games and toys: gaming headsets/controllers, video games, LCD writing tablets for kids, puzzles, toys, balls, rackets and sports equipment.",
 "Travel & Camping":"Travel and outdoor adventure products: travel backpacks, luggage, packing cubes, tents, sleeping bags, camping stoves, hiking gear and travel accessories.",
}

def ordered_primary_boards(live_items:List[Dict[str,Any]])->List[Dict[str,Any]]:
    by_name={str(b.get("name") or "").strip():dict(b) for b in live_items if str(b.get("id") or "") and str(b.get("name") or "").strip()}
    # Pinterest's board-list feed is the live serial source. Newly-created boards
    # may take time to appear in that feed, so canonical configured boards are
    # merged temporarily and sorted by the same A-Z board presentation order.
    for name,bid in PERMANENT_BOARD_IDS.items():
        if name!=DEFAULT_BOARD_NAME and name not in by_name:
            by_name[name]={"id":bid,"name":name,"_fresh_configured":True}
    boards=[b for name,b in by_name.items() if name in BOARD_SCOPES and name!=DEFAULT_BOARD_NAME and str(b.get("id") or "") not in LEGACY_BOARD_IDS and name not in LEGACY_BOARD_NAMES]
    return sorted(boards,key=lambda b:(str(b.get("name") or "").casefold(),str(b.get("id") or "")))

CATEGORY_SLOTS=[]
BOARD_SEARCH_PROFILES={name:{"keywords":[scope]} for name,scope in BOARD_SCOPES.items()}
REQUIRED_PRIMARY_SLOTS=len(BOARD_SCOPES)-1

def classify_live_boards(live_items:List[Dict[str,Any]])->Dict[str,Any]:
    primary=ordered_primary_boards(live_items)
    fallback={"id":PERMANENT_BOARD_IDS[DEFAULT_BOARD_NAME],"name":DEFAULT_BOARD_NAME}
    return {
        "approved_boards":primary+[fallback],
        "primary_boards":primary,
        "legacy_boards":[{"id":str(b.get("id") or ""),"name":str(b.get("name") or "")} for b in live_items if str(b.get("id") or "") in LEGACY_BOARD_IDS or str(b.get("name") or "") in LEGACY_BOARD_NAMES],
        "unknown_non_legacy":[],
        "primary_count":len(primary),
        "required_primary_slots":REQUIRED_PRIMARY_SLOTS,
        "scheduler_ready":len(primary)>=REQUIRED_PRIMARY_SLOTS and all(str(b.get("id") or "") for b in primary),
        "missing_primary_slots":max(0,REQUIRED_PRIMARY_SLOTS-len(primary)),
        "serial_order":[b["name"] for b in primary],
        "board_scopes":BOARD_SCOPES,
    }

def build_slot_specs(live_items:List[Dict[str,Any]])->Tuple[Optional[List[Dict[str,Any]]],Dict[str,Any]]:
    info=classify_live_boards(live_items)
    if not info["scheduler_ready"]:
        return None,info
    cycle=info["primary_boards"]+[{"id":PERMANENT_BOARD_IDS[DEFAULT_BOARD_NAME],"name":DEFAULT_BOARD_NAME}]
    specs=[]
    for slot in range(1,51):
        target=cycle[(slot-1)%len(cycle)]
        serial=cycle.index(target)+1
        specs.append({
            "slot":slot,
            "slot_kind":"board_serial",
            "target_board_name":target["name"],
            "target_board_id":str(target["id"]),
            "amazon_category":target["name"],
            "board_serial":serial,
            "board_scope":BOARD_SCOPES[target["name"]],
        })
    return specs,info
