"""Amazon discovery facade using Composio only for scheduled discovery."""
from __future__ import annotations
from typing import Optional, Set
from amazon_composio_discovery import CATEGORY_QUERIES, discover_category, _candidate, _search, composio_ready
from published_registry import registry
MAX_REPLACEMENTS_PER_SLOT=5
_BOARD_CATEGORY={
 "Electronics & Gadgets":"Electronics","Smartphones & Tablets":"Cell Phones & Accessories",
 "PCs, Laptops & Home Electronics":"Computers & Accessories","Health & Fitness":"Health & Household",
 "Beauty & Personal Care":"Beauty","Home, Kitchen & Dining":"Home & Kitchen",
 "Sports, Games & Toys":"Toys & Games","Fashion & Lifestyle":"Clothing/Shoes",
 "Pet Supplies":"Pet Supplies","Baby & Kids":"Baby","Automotive & Tools":"Electronics",
 "Office & Productivity":"Computers & Accessories","Travel & Camping":"Clothing/Shoes","Books & Learning":"Home & Kitchen"}
def is_dormant()->bool:return not composio_ready()
async def discover_for_board(board_name:str,*,exclude_asins:Optional[Set[str]]=None,client=None):
 return await discover_category(_BOARD_CATEGORY.get(board_name,"Electronics"),exclude_asins=exclude_asins)
async def discover_global(*,exclude_asins:Optional[Set[str]]=None,client=None):
 excluded={x.upper() for x in (exclude_asins or set())}|registry.all_published_asins(); candidates=[]
 for category,q in (("Electronics",["best selling new electronics"]),("Home & Kitchen",["best selling home kitchen"]),("Toys & Games",["popular new toys games"])):
  try:candidates += [c for c in (_candidate(x,category) for x in await _search(q[0],1)) if c and c["asin"] not in excluded]
  except Exception:pass
 return sorted(candidates,key=lambda x:(-x["score"],x["asin"]))[0] if candidates else None
def assert_url_unmodified(original,candidate):return (original or "")== (candidate or "")
