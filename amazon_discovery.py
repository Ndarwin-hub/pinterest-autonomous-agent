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
 "Electronics & Gadgets":"Electronics","Smartphones & Tablets":"Cell Phones & Accessories",
 "PCs, Laptops & Home Electronics":"Computers & Accessories","Health & Fitness":"Health & Household",
 "Beauty & Personal Care":"Beauty","Home, Kitchen & Dining":"Home & Kitchen",
 "Sports, Games & Toys":"Toys & Games","Fashion & Lifestyle":"Clothing/Shoes",
 "Pet Supplies":"Pet Supplies","Baby & Kids":"Baby","Automotive & Tools":"Electronics",
 "Office & Productivity":"Computers & Accessories","Travel & Camping":"Clothing/Shoes","Books & Learning":"Home & Kitchen"}
def is_dormant()->bool:return not composio_ready()
async def discover_for_board(board_name:str,*,exclude_asins:Optional[Set[str]]=None,client=None):
 category=_BOARD_CATEGORY.get(board_name,"Electronics")
 api_candidate=await _discover_api(category,exclude_asins)
 return api_candidate or await composio_discover_category(category,exclude_asins=exclude_asins)
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
