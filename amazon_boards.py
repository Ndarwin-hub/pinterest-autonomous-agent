"""Fixed Amazon scheduler board universe. Unknown live boards are never auto-approved."""
from __future__ import annotations
from typing import Any, Dict, List, Optional, Tuple
from board_org import LEGACY_BOARD_IDS, LEGACY_BOARD_NAMES, PERMANENT_BOARD_IDS, DEFAULT_BOARD_NAME
REQUIRED_PRIMARY_SLOTS=14
APPROVED_PRIMARY_BOARD_IDS={bid for name,bid in PERMANENT_BOARD_IDS.items() if name!=DEFAULT_BOARD_NAME}
BOARD_SEARCH_PROFILES={
"Health & Fitness":{"keywords":["fitness equipment","resistance bands","yoga mat","workout gear"],"commission_proxy_pct":3.0},
"Beauty & Personal Care":{"keywords":["skincare","moisturizer","hair care","beauty tools"],"commission_proxy_pct":3.0},
"Smartphones & Tablets":{"keywords":["phone case","screen protector","tablet stand","phone charger"],"commission_proxy_pct":2.0},
"PCs, Laptops & Home Electronics":{"keywords":["usb hub","webcam","laptop stand","mechanical keyboard"],"commission_proxy_pct":2.5},
"Electronics & Gadgets":{"keywords":["gadgets","tech accessories","wireless earbuds","power bank"],"commission_proxy_pct":2.0},
"Home, Kitchen & Dining":{"keywords":["kitchen gadgets","cookware","home organizer","air fryer accessories"],"commission_proxy_pct":4.5},
"Books & Learning":{"keywords":["bestselling paperback","self help book","cookbook","productivity book"],"commission_proxy_pct":4.5},
"Games, Toys & Sports":{"keywords":["board game","puzzle","sports equipment","outdoor toys"],"commission_proxy_pct":3.0},
"Fashion & Lifestyle":{"keywords":["fashion accessories","travel wallet","sunglasses","everyday bag"],"commission_proxy_pct":4.0},
"Travel & Camping":{"keywords":["travel backpack","camping gear","packing cubes","hiking accessories"],"commission_proxy_pct":3.0},
"Pet Supplies":{"keywords":["dog supplies","cat supplies","pet toys","pet grooming"],"commission_proxy_pct":3.0},
"Baby & Kids":{"keywords":["baby gear","baby supplies","toddler toys","kids essentials"],"commission_proxy_pct":3.0},
"Automotive & Tools":{"keywords":["car accessories","automotive tools","car care","auto accessories"],"commission_proxy_pct":3.0},
"Office & Productivity":{"keywords":["office supplies","desk accessories","home office","productivity tools"],"commission_proxy_pct":3.0},
"Everything Else":{"keywords":["useful gadgets","home office essentials","popular new releases"],"commission_proxy_pct":4.0}}

def classify_live_boards(live_items:List[Dict[str,Any]])->Dict[str,Any]:
    by_id={v:k for k,v in PERMANENT_BOARD_IDS.items()}; approved=[]; legacy=[]; unknown=[]
    for b in live_items:
        bid=str(b.get("id") or b.get("board_id") or ""); name=(b.get("name") or "").strip()
        if bid in LEGACY_BOARD_IDS or name in LEGACY_BOARD_NAMES: legacy.append({"id":bid,"name":name}); continue
        if bid in by_id and bid in APPROVED_PRIMARY_BOARD_IDS|{PERMANENT_BOARD_IDS.get(DEFAULT_BOARD_NAME)}: approved.append({"id":bid,"name":by_id[bid]})
        elif bid in APPROVED_PRIMARY_BOARD_IDS: approved.append({"id":bid,"name":name})
        elif bid and name: unknown.append({"id":bid,"name":name})
    seen=set(); approved=[x for x in approved if not (x["id"] in seen or seen.add(x["id"]))]
    primary=[x for x in approved if x["name"]!=DEFAULT_BOARD_NAME]
    return {"approved_boards":approved,"primary_boards":primary,"legacy_boards":legacy,"unknown_non_legacy":unknown,"primary_count":len(primary),"required_primary_slots":REQUIRED_PRIMARY_SLOTS,"scheduler_ready":len(primary)>=REQUIRED_PRIMARY_SLOTS,"missing_primary_slots":max(0,REQUIRED_PRIMARY_SLOTS-len(primary))}

def build_slot_specs(live_items:List[Dict[str,Any]])->Tuple[Optional[List[Dict[str,Any]]],Dict[str,Any]]:
    info=classify_live_boards(live_items)
    if not info["scheduler_ready"]: return None,info
    primary=info["primary_boards"][:REQUIRED_PRIMARY_SLOTS]
    specs=[{"slot":i,"slot_kind":"board","target_board_name":b["name"],"target_board_id":b["id"]} for i,b in enumerate(primary,1)]
    specs.append({"slot":15,"slot_kind":"global","target_board_name":None,"target_board_id":None})
    return specs,info
