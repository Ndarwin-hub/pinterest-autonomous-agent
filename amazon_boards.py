"""Approved Pinterest board gate plus 14 logical Amazon categories and one extra."""
from __future__ import annotations
from typing import Any,Dict,List,Optional,Tuple
from board_org import LEGACY_BOARD_IDS,LEGACY_BOARD_NAMES,PERMANENT_BOARD_IDS,DEFAULT_BOARD_NAME
REQUIRED_PRIMARY_SLOTS=10
APPROVED_PRIMARY_BOARD_IDS={bid for name,bid in PERMANENT_BOARD_IDS.items() if name!=DEFAULT_BOARD_NAME}
BOARD_SEARCH_PROFILES={
 "Health & Fitness":{"keywords":["fitness equipment","whey protein powder","reusable ice packs"]},"Electronics & Gadgets":{"keywords":["surge protector power strip"]},"Home, Kitchen & Dining":{"keywords":["Stanley Quencher tumbler","countertop ice maker"]},"Sports, Games & Toys":{"keywords":["LCD writing tablet kids","gaming headset"]},"Fashion & Lifestyle":{"keywords":["running shoes"]},"Pet Supplies":{"keywords":["cat litter"]},"Baby & Kids":{"keywords":["baby wipes"]},"Automotive & Tools":{"keywords":["automotive tools"]},"Office & Productivity":{"keywords":["desk accessories"]},"Books & Learning":{"keywords":["bestselling paperback"]},"Everything Else":{"keywords":["popular new releases"]}}
CATEGORY_SLOTS=[
 ("Electronics","Electronics & Gadgets"),("Clothing/Shoes","Fashion & Lifestyle"),("Beauty","Health & Fitness"),
 ("Home & Kitchen","Home, Kitchen & Dining"),("Health & Household","Health & Fitness"),("Toys & Games","Sports, Games & Toys"),
 ("Sports & Outdoors","Health & Fitness"),("Baby","Baby & Kids"),("Pet Supplies","Pet Supplies"),("Appliances","Home, Kitchen & Dining"),
 ("Cell Phones & Accessories","Electronics & Gadgets"),("Computers & Accessories","Electronics & Gadgets"),("Video Games","Sports, Games & Toys"),("Musical Instruments","Electronics & Gadgets")]
def classify_live_boards(live_items:List[Dict[str,Any]])->Dict[str,Any]:
 by_id={v:k for k,v in PERMANENT_BOARD_IDS.items()};approved=[];legacy=[];unknown=[]
 for b in live_items:
  bid=str(b.get("id") or b.get("board_id") or "");name=(b.get("name") or "").strip()
  if bid in LEGACY_BOARD_IDS or name in LEGACY_BOARD_NAMES:legacy.append({"id":bid,"name":name});continue
  if bid in by_id and bid in APPROVED_PRIMARY_BOARD_IDS|{PERMANENT_BOARD_IDS.get(DEFAULT_BOARD_NAME)}:approved.append({"id":bid,"name":by_id[bid]})
  elif bid in APPROVED_PRIMARY_BOARD_IDS:approved.append({"id":bid,"name":name})
  elif bid and name:unknown.append({"id":bid,"name":name})
 seen=set();approved=[x for x in approved if not(x["id"] in seen or seen.add(x["id"]))];primary=[x for x in approved if x["name"]!=DEFAULT_BOARD_NAME]
 return {"approved_boards":approved,"primary_boards":primary,"legacy_boards":legacy,"unknown_non_legacy":unknown,"primary_count":len(primary),"required_primary_slots":REQUIRED_PRIMARY_SLOTS,"scheduler_ready":len(primary)>=REQUIRED_PRIMARY_SLOTS,"missing_primary_slots":max(0,REQUIRED_PRIMARY_SLOTS-len(primary))}
def build_slot_specs(live_items:List[Dict[str,Any]])->Tuple[Optional[List[Dict[str,Any]]],Dict[str,Any]]:
 info=classify_live_boards(live_items)
 if not info["scheduler_ready"]:return None,info
 by_name={b["name"]:b for b in info["primary_boards"]};specs=[]
 for slot,(category,board_name) in enumerate(CATEGORY_SLOTS,1):
  board=by_name.get(board_name)
  if not board:return None,info
  specs.append({"slot":slot,"slot_kind":"category","target_board_name":board_name,"target_board_id":board["id"],"amazon_category":category})
 specs.append({"slot":15,"slot_kind":"global","target_board_name":None,"target_board_id":None,"amazon_category":"Extra"})
 return specs,info
