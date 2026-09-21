"""Amazon discovery facade using Composio only for scheduled discovery."""
from __future__ import annotations
from typing import Optional, Set
import logging
logger=logging.getLogger("pinterest-agent.amazon_discovery")
from amazon_composio_discovery import CATEGORY_QUERIES, discover_category as composio_discover_category, _candidate, _search, composio_ready
from amazon_client import AmazonCreatorsClient, amazon_credentials_present, extract_detail_page_url, extract_asin_from_item, extract_title, is_buyable_offer
from published_registry import registry
MAX_REPLACEMENTS_PER_SLOT=5

def _api_candidate(item, category):
    asin=extract_asin_from_item(item)
    url=extract_detail_page_url(item)
    title=extract_title(item)
    if not asin or not url or len(title)<6 or not is_buyable_offer(item):
        return None
    if registry.is_published(asin=asin,url=url):
        return None
    return {"asin":asin,"affiliate_url":url,"product_url":url,"title":title,"category":category,
            "price":None,"rating":0,"reviews":0,"bought_last_month":None,
            "score":1,"source":"amazon_api","raw":item}

async def _discover_api(category, exclude_asins=None):
    if not amazon_credentials_present():
        return None
    excluded={x.upper() for x in (exclude_asins or set())}|registry.all_published_asins()
    try:
        client=AmazonCreatorsClient()
    except Exception as e:
        logger.warning("Amazon API client unavailable: %s",e)
        return None
    for q in CATEGORY_QUERIES.get(category,[category]):
        try:
            items=await client.search_items(q,item_count=10)
        except Exception as e:
            logger.warning("Amazon API discovery failed for %s: %s",category,e)
            continue
        candidates=[c for c in (_api_candidate(i,category) for i in items) if c and c["asin"] not in excluded]
        if candidates:
            return candidates[0]
    return None
_BOARD_CATEGORY={
 "Electronics & Gadgets":"Electronics","Smartphones & Tablets":"Cell Phones & Accessories","PC's, Laptops & TV's":"Computers & Accessories",
 "PCs, Laptops & Home Electronics":"Computers & Accessories","Health & Fitness":"Health & Household",
 "Beauty & Personal Care":"Beauty","Home, Kitchen & Dining":"Home & Kitchen",
 "Sports, Games & Toys":"Toys & Games","Fashion & Lifestyle":"Clothing/Shoes",
 "Pet Supplies":"Pet Supplies","Baby & Kids":"Baby","Automotive & Tools":"Electronics",
 "Office & Productivity":"Computers & Accessories","Travel & Camping":"Clothing/Shoes","Books & Learning":"Home & Kitchen"}
def is_dormant()->bool:return not (amazon_credentials_present() or composio_ready())
BOARD_CATEGORY_KEYS={
 "Appliances & Home":"home","Automotive & Tools":"automotive","Baby & Kids":"health_baby_kids",
 "Beauty & Personal Care":"health_beauty_personal","Books & Learning":"books","Electronics & Gadgets":"electronics_root",
 "Everything Else":"general","Fashion & Lifestyle":"fashion","Health & Fitness":"health_root","Home, Kitchen & Dining":"home",
 "Office & Productivity":"office","PC's, Laptops & TV's":"pc_tv","Pet Supplies":"pets","Smartphones & Tablets":"smartphones_tablets",
 "Sports, Games & Toys":"games","Travel & Camping":"travel"
}
BOARD_SEARCH_PROFILES={
 "Appliances & Home":["countertop ice maker","air fryer","coffee maker","blender appliance","toaster oven"],
 "Automotive & Tools":["car emergency tools","automotive tools garage","car accessories"],
 "Baby & Kids":["baby wipes","diapers","baby bottle feeding"],
 "Beauty & Personal Care":["Mighty Patch acne patches","skincare serum moisturizer","hair care shampoo"],
 "Books & Learning":["bestselling paperback book","self improvement book","educational textbook"],
 "Electronics & Gadgets":["surge protector power strip","wireless headphones earbuds","USB-C charger cable"],
 "Fashion & Lifestyle":["running shoes","sneakers women men","everyday handbag"],
 "Health & Fitness":["whey protein powder","reusable ice packs","resistance bands fitness"],
 "Home, Kitchen & Dining":["Stanley Quencher tumbler","kitchen cookware set","home storage organizer"],
 "Office & Productivity":["desk organizer office","planner productivity","monitor arm desk"],
 "PC's, Laptops & TV's":["gaming laptop","desktop PC","4K smart TV","computer monitor","PC SSD RAM"],
 "Pet Supplies":["cat litter","dog grooming supplies","pet toys"],
 "Smartphones & Tablets":["iPhone smartphone","Android smartphone","iPad tablet","phone screen protector","tablet case"],
 "Sports, Games & Toys":["LCD writing tablet kids","gaming headset","sports equipment"],
 "Travel & Camping":["travel backpack","camping tent","hiking gear"],
 "Everything Else":["Amazon best selling new products","popular useful product"]
}
async def discover_for_board(board_name:str,*,exclude_asins:Optional[Set[str]]=None,client=None):
    key=BOARD_CATEGORY_KEYS.get(board_name,"general")
    excluded={x.upper() for x in (exclude_asins or set())}|registry.all_published_asins()
    queries=BOARD_SEARCH_PROFILES.get(board_name,[board_name])
    from board_org import detect_product_category
    for q in queries:
        try:
            for page in (1,2):
                products=await _search(q,page)
                candidates=[]
                for raw in products:
                    c=_candidate(raw,key)
                    if not c or c["asin"] in excluded:
                        continue
                    detected=detect_product_category({"name":c.get("title",""),"title":c.get("title","")})
                    if (board_name=="Everything Else" and detected!="general") or (board_name!="Everything Else" and detected!=key):
                        continue
                    c["target_board_name"]=board_name
                    candidates.append(c)
                if candidates:
                    return sorted(candidates,key=lambda x:(-x["score"],x["asin"]))[0]
        except Exception as e:
            logger.warning("Exact board discovery failed board=%s query=%s: %s",board_name,q,e)
    api_candidate=await _discover_api(_BOARD_CATEGORY.get(board_name,"Electronics"),exclude_asins)
    if api_candidate:
        detected=detect_product_category({"name":api_candidate.get("title",""),"title":api_candidate.get("title","")})
        if (board_name=="Everything Else" and detected=="general") or (board_name!="Everything Else" and detected==key):
            api_candidate["target_board_name"]=board_name
            return api_candidate
    return None

async def discover_global(*,exclude_asins:Optional[Set[str]]=None,client=None):
 excluded={x.upper() for x in (exclude_asins or set())}|registry.all_published_asins(); candidates=[]
 for category,q in (("Electronics",["best selling new electronics"]),("Home & Kitchen",["best selling home kitchen"]),("Toys & Games",["popular new toys games"])):
  try:
   api_candidate=await _discover_api(category,excluded)
   if api_candidate and api_candidate["asin"] not in excluded:
    candidates.append(api_candidate)
   else:
    candidates += [c for c in (_candidate(x,category) for x in await _search(q[0],1)) if c and c["asin"] not in excluded]
  except Exception:
   pass
 return sorted(candidates,key=lambda x:(-x["score"],x["asin"]))[0] if candidates else None
def assert_url_unmodified(original,candidate):return (original or "")== (candidate or "")
