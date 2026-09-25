"""Amazon discovery facade using Composio only for scheduled discovery."""
from __future__ import annotations
import asyncio
import os
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
def is_dormant()->bool:
    # Amazon-native HTML, Best Sellers, and the durable reservoir are valid
    # discovery paths even when Composio/API credentials are unavailable.
    return os.getenv("AMAZON_DISCOVERY_DISABLED","0").strip().lower() in {"1","true","yes"}
BOARD_CATEGORY_KEYS={
 "Appliances & Home":"home","Watches & Clocks":"watches_clocks","Automotive & Tools":"automotive","Baby & Kids":"health_baby_kids",
 "Beauty & Personal Care":"health_beauty_personal","Books & Learning":"books","Electronics & Gadgets":"electronics_root",
 "Everything Else":"general","Fashion & Lifestyle":"fashion","Health & Fitness":"health_root","Home, Kitchen & Dining":"home",
 "Office & Productivity":"office","PC's, Laptops & TV's":"pc_tv","Pet Supplies":"pets","Smartphones & Tablets":"smartphones_tablets",
 "Sports, Games & Toys":"games","Travel & Camping":"travel"
}
# New/current discovery is intentionally first. Amazon search does not expose a
# reliable publication date in every discovery response, so "new/current" is
# implemented with explicit new-release/new-arrival/latest queries first, then
# popularity/quality scoring within those result sets. Older generic queries are
# only fallback candidates.
BOARD_SEARCH_PROFILES={
 "Appliances & Home":["new countertop ice maker","new air fryer","new coffee maker","new blender appliance","new toaster oven","countertop ice maker","air fryer","coffee maker","blender appliance","toaster oven"],
 "Watches & Clocks":["new wristwatch","new smartwatch","new digital watch","new analog watch","new alarm clock","new wall clock","wristwatch","smartwatch","digital watch","analog watch","alarm clock","wall clock"],
 "Automotive & Tools":["new car emergency tools","new automotive tools garage","new car accessories","car emergency tools","automotive tools garage","car accessories"],
 "Baby & Kids":["new baby wipes","new diapers","new baby bottle feeding","baby wipes","diapers","baby bottle feeding"],
 "Beauty & Personal Care":["new Mighty Patch acne patches","new skincare serum moisturizer","new hair care shampoo","Mighty Patch acne patches","skincare serum moisturizer","hair care shampoo"],
 "Books & Learning":["new bestselling paperback book","new self improvement book","new educational textbook","bestselling paperback book","self improvement book","educational textbook"],
 "Electronics & Gadgets":["new surge protector power strip","new wireless headphones earbuds","new USB-C charger cable","surge protector power strip","wireless headphones earbuds","USB-C charger cable"],
 "Fashion & Lifestyle":["new running shoes","new sneakers women men","new everyday handbag","running shoes","sneakers women men","everyday handbag"],
 "Health & Fitness":["new whey protein powder","new reusable ice packs","new resistance bands fitness","whey protein powder","reusable ice packs","resistance bands fitness"],
 "Home, Kitchen & Dining":["new Stanley Quencher tumbler","new kitchen cookware set","new home storage organizer","Stanley Quencher tumbler","kitchen cookware set","home storage organizer"],
 "Office & Productivity":["new desk organizer office","new planner productivity","new monitor arm desk","desk organizer office","planner productivity","monitor arm desk"],
 "PC's, Laptops & TV's":["new gaming laptop","new desktop PC","new 4K smart TV","new computer monitor","new PC SSD RAM","gaming laptop","desktop PC","4K smart TV","computer monitor","PC SSD RAM"],
 "Pet Supplies":["new cat litter","new dog grooming supplies","new pet toys","cat litter","dog grooming supplies","pet toys"],
 "Smartphones & Tablets":["new iPhone smartphone","new Android smartphone","new iPad tablet","new phone screen protector","new tablet case","iPhone smartphone","Android smartphone","iPad tablet","phone screen protector","tablet case"],
 "Sports, Games & Toys":["new LCD writing tablet kids","new gaming headset","new sports equipment","LCD writing tablet kids","gaming headset","sports equipment"],
 "Travel & Camping":["new travel backpack","new camping tent","new hiking gear","travel backpack","camping tent","hiking gear"],
 "Everything Else":["new Amazon products","new useful products","Amazon best selling new products","popular useful product"]
}
def _query_priority(query:str)->int:
    q=str(query or "").lower()
    # Lower is better. Explicit new/current intent outranks generic queries.
    if any(term in q for term in ("new releases","new release","new arrivals","new arrival","latest","new ")):
        return 0
    return 1

def _discovery_score(candidate:dict, query:str)->tuple:
    # Query priority is the primary selection criterion; existing score remains
    # the tie-breaker so current demand/quality still matters.
    return (_query_priority(query), -int(candidate.get("score") or 0), str(candidate.get("asin") or ""))

async def discover_for_board(board_name:str,*,exclude_asins:Optional[Set[str]]=None,client=None):
    key=BOARD_CATEGORY_KEYS.get(board_name,"general")
    excluded={x.upper() for x in (exclude_asins or set())}|registry.all_published_asins()
    queries=BOARD_SEARCH_PROFILES.get(board_name,[f"new {board_name}",board_name])
    from board_org import detect_product_category
    all_candidates=[]
    # Search the highest-priority query tier first and stop as soon as a usable
    # candidate exists. This preserves new/current-first selection while avoiding
    # serially querying every generic fallback before one product can run.
    prioritized=sorted(queries,key=_query_priority)
    for q in prioritized:
        try:
            for page in (1,2):
                products=await asyncio.wait_for(_search(q,page),timeout=45)
                for raw in products:
                    c=_candidate(raw,key)
                    if not c or c["asin"] in excluded:
                        continue
                    detected=detect_product_category({"name":c.get("title",""),"title":c.get("title","")})
                    if (board_name=="Everything Else" and detected!="general") or (board_name!="Everything Else" and detected!=key):
                        continue
                    c["target_board_name"]=board_name
                    c["_discovery_query"]=q
                    all_candidates.append(c)
            if all_candidates and _query_priority(q)==0:
                break
        except Exception as e:
            logger.warning("Exact board discovery failed board=%s query=%s: %s",board_name,q,e)
    if all_candidates:
        unique={}
        for c in all_candidates:
            unique.setdefault(c["asin"],c)
        return sorted(unique.values(),key=lambda x:_discovery_score(x,x.get("_discovery_query","")))[0]
    api_category=_BOARD_CATEGORY.get(board_name,"Electronics")
    api_candidate=await _discover_api(api_category,exclude_asins)
    if api_candidate:
        detected=detect_product_category({"name":api_candidate.get("title",""),"title":api_candidate.get("title","")})
        if (board_name=="Everything Else" and detected=="general") or (board_name!="Everything Else" and detected==key):
            api_candidate["target_board_name"]=board_name
            return api_candidate
    # Last-resort mapped category discovery preserves the board mapping while
    # allowing a sparse board profile to obtain a replacement candidate.
    try:
        fallback=await composio_discover_category(api_category,exclude_asins=excluded)
        if fallback:
            fallback["target_board_name"]=board_name
            return fallback
    except Exception as e:
        logger.warning("Mapped Amazon category fallback failed board=%s category=%s: %s",board_name,api_category,e)
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
