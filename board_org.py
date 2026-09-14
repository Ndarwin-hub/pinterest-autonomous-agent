"""Canonical Pinterest destination resolver with strict section whitelists."""
from __future__ import annotations
import contextvars
from typing import Any, Dict, List, Optional

DEFAULT_BOARD_NAME="Everything Else"
LEGACY_BOARD_NAMES={"General Pins","Stuff to buy","Product Pins"}
LEGACY_BOARD_IDS={"987906936951142945","987906936951144580"}
PERMANENT_BOARD_IDS={"Automotive & Tools":"987906936951148022","Books & Learning":"987906936951145056","Electronics & Gadgets":"987906936951145057","Everything Else":"987906936951147704","Fashion & Lifestyle":"987906936951147683","Games, Toys & Sports":"987906936951147684","Health & Fitness":"987906936951147682","Home, Kitchen & Dining":"987906936951145744","Office & Productivity":"987906936951148020","Pet Supplies":"987906936951148021","Travel & Camping":"987906936951147708"}
SECTION_IDS={"electronics_smartphones":"3856307780748685888","electronics_pc_home":"3856307171131803008","health_baby_kids":"3856308624642316032","health_beauty_personal":"3856309112490652608"}
_destination_section=contextvars.ContextVar("pinterest_destination_section",default=None)
def current_section_id()->Optional[str]: return _destination_section.get()
def _set_section(section_id:Optional[str])->None: _destination_section.set(section_id)
CATEGORY_BOARD_MAP={"electronics_smartphones":"Electronics & Gadgets","electronics_pc_home":"Electronics & Gadgets","electronics_root":"Electronics & Gadgets","health_baby_kids":"Health & Fitness","health_beauty_personal":"Health & Fitness","health_root":"Health & Fitness","beauty":"Health & Fitness","kids":"Health & Fitness","smartphones":"Electronics & Gadgets","pcs":"Electronics & Gadgets","electronics":"Electronics & Gadgets","health":"Health & Fitness","home":"Home, Kitchen & Dining","books":"Books & Learning","games":"Games, Toys & Sports","fashion":"Fashion & Lifestyle","travel":"Travel & Camping","pets":"Pet Supplies","automotive":"Automotive & Tools","office":"Office & Productivity","general":DEFAULT_BOARD_NAME}
RULES=[
("screen protector","electronics_root"),("phone case","electronics_root"),("smartphone case","electronics_root"),("cell phone case","electronics_root"),("headphones","electronics_root"),("headphone","electronics_root"),("airpods","electronics_root"),("earbuds","electronics_root"),("earbud","electronics_root"),("usb cable","electronics_root"),("charging cable","electronics_root"),("charger","electronics_root"),("power bank","electronics_root"),("mouse","electronics_root"),("keyboard","electronics_root"),("webcam","electronics_root"),("smartwatch","electronics_root"),("smart watch","electronics_root"),("camera","electronics_root"),("gaming accessory","electronics_root"),("iphone","electronics_smartphones"),("ipad","electronics_smartphones"),("smartphone","electronics_smartphones"),("android phone","electronics_smartphones"),("cell phone","electronics_smartphones"),("mobile phone","electronics_smartphones"),("galaxy tab","electronics_smartphones"),("tablet","electronics_smartphones"),("macbook","electronics_pc_home"),("laptop","electronics_pc_home"),("notebook computer","electronics_pc_home"),("desktop pc","electronics_pc_home"),("desktop computer","electronics_pc_home"),("gaming pc","electronics_pc_home"),("personal computer","electronics_pc_home"),("television","electronics_pc_home"),(" tv ","electronics_pc_home"),("monitor","electronics_pc_home"),("baby diapers","health_baby_kids"),("baby diaper","health_baby_kids"),("baby bottle","health_baby_kids"),("baby stroller","health_baby_kids"),("baby","health_baby_kids"),("toddler","health_baby_kids"),("kids","health_baby_kids"),("children","health_baby_kids"),("skincare","health_beauty_personal"),("skin care","health_beauty_personal"),("face serum","health_beauty_personal"),("serum","health_beauty_personal"),("moisturizer","health_beauty_personal"),("shampoo","health_beauty_personal"),("conditioner","health_beauty_personal"),("makeup","health_beauty_personal"),("cosmetic","health_beauty_personal"),("beauty","health_beauty_personal"),("personal care","health_beauty_personal"),("yoga","health_root"),("dumbbell","health_root"),("dumbbells","health_root"),("protein powder","health_root"),("protein","health_root"),("resistance band","health_root"),("exercise","health_root"),("fitness","health_root"),("workout","health_root"),("gym","health_root"),("blood pressure","health_root"),("blood-pressure","health_root"),("vitamin","health_root"),("supplement","health_root"),("health","health_root"),("book","books"),("novel","books"),("textbook","books"),("cookware","home"),("kitchen","home"),("air fryer","home"),("coffee maker","home"),("fashion","fashion"),("shoes","fashion"),("sneakers","fashion"),("clothing","fashion"),("apparel","fashion"),("travel","travel"),("camping","travel"),("hiking","travel"),("pet supplies","pets"),("dog","pets"),("cat","pets"),("automotive","automotive"),("car accessories","automotive"),("office","office"),("planner","office"),("desk organizer","office")]
def detect_product_category(product:Dict[str,Any])->str:
 blob=f" {(product.get('name') or '')} {(product.get('description') or '')} ".lower()
 for term,cat in RULES:
  if term in blob:return cat
 return "general"
def preferred_board_name(category_key:str)->str:
 _set_section(None)
 section={"electronics_smartphones":SECTION_IDS["electronics_smartphones"],"electronics_pc_home":SECTION_IDS["electronics_pc_home"],"health_baby_kids":SECTION_IDS["health_baby_kids"],"health_beauty_personal":SECTION_IDS["health_beauty_personal"]}.get(category_key)
 if section:_set_section(section)
 return CATEGORY_BOARD_MAP.get(category_key,DEFAULT_BOARD_NAME)
def _normalize_board_name(name:str)->str:return " ".join((name or "").lower().replace("&","and").split())
def find_matching_board(items:List[Any],preferred_name:str)->Optional[str]:
 target=_normalize_board_name(preferred_name); permanent=PERMANENT_BOARD_IDS.get(preferred_name)
 for b in items or []:
  bid=str(b.get("id") or b.get("board_id") or ""); name=(b.get("name") or "").strip()
  if bid in LEGACY_BOARD_IDS or name in LEGACY_BOARD_NAMES:continue
  if permanent and bid==permanent:return bid
 for b in items or []:
  bid=str(b.get("id") or b.get("board_id") or ""); name=(b.get("name") or "").strip()
  if bid in LEGACY_BOARD_IDS or name in LEGACY_BOARD_NAMES:continue
  if _normalize_board_name(name)==target:return bid or None
 return None

# The existing wire layer captures agent.publish_and_verify when it starts. Patch
# the agent before that capture so the established workflow gains board_section_id
# without replacing the image/retry/5-Pin pipeline.
def _install_section_publish_patch():
 try:
  import agent
  original=getattr(agent,"publish_and_verify",None)
  if not original or getattr(original,"_section_aware",False):return
  import json
  async def section_aware_publish(board_id,title,description,alt_text,image_mode,image_value,link,job_store,job_id,pin_index):
   section_id=current_section_id()
   job_store.update(job_id,progress=f"Publishing Pin {pin_index}/5")
   if image_mode=="base64": media_source={"source_type":"image_base64","content_type":"image/jpeg","data":image_value}
   else: media_source={"source_type":"image_url","url":image_value}
   args={"board_id":board_id,"title":title[:100],"description":description[:800],"alt_text":alt_text[:500],"link":link,"media_source":media_source}
   if section_id:args["board_section_id"]=section_id
   data=await agent.run_composio_tool("PINTEREST_CREATE_PIN",args,retries=2)
   pin_id=str(data.get("id") or data.get("pin_id") or (data.get("data") or {}).get("id") or "")
   if not pin_id:raise RuntimeError(f"Pin created but no ID: {json.dumps(data)[:400]}")
   verified_data=await agent.run_composio_tool("PINTEREST_GET_PIN",{"pin_id":pin_id},retries=1)
   actual_board=str(verified_data.get("board_id") or ((verified_data.get("board") or {}).get("id") if isinstance(verified_data.get("board"),dict) else "") or "")
   actual_section=str(verified_data.get("board_section_id") or ((verified_data.get("board_section") or {}).get("id") if isinstance(verified_data.get("board_section"),dict) else "") or "")
   if actual_board and actual_board!=str(board_id):raise RuntimeError(f"Pin {pin_index}: board verification mismatch; intended {board_id}, actual {actual_board}")
   if section_id and actual_section!=str(section_id):raise RuntimeError(f"Pin {pin_index}: section verification mismatch; intended {section_id}, actual {actual_section}")
   return {"pin_id":pin_id,"pin_url":f"https://www.pinterest.com/pin/{pin_id}/","verified":bool(verified_data and verified_data.get("id")),"destination_url":link,"board_id":board_id,"board_section_id":section_id,"section_verified_independently":bool(section_id and actual_section==str(section_id)),"title":title}
  section_aware_publish._section_aware=True
  agent.publish_and_verify=section_aware_publish
 except Exception:
  pass
_install_section_publish_patch()
