"""Browserless Amazon US discovery through Composio."""
from __future__ import annotations
import logging, os, re
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from published_registry import registry
logger = logging.getLogger("pinterest-agent.amazon_composio_discovery")
AMAZON_DISCOVERY_DOMAINS = ["amazon.com"]
AMAZON_DOMAIN = AMAZON_DISCOVERY_DOMAINS[0] if AMAZON_DISCOVERY_DOMAINS else "amazon.com"
_domain_cursor = 0
AFFILIATE_TAG = "desiredplus-20"
ASIN_RE = re.compile(r"(?:/dp/|/gp/product/)([A-Z0-9]{10})(?:[/?]|$)", re.I)
CATEGORY_QUERIES = {
 "Electronics":["surge protector power strip"],
 "Clothing/Shoes":["running shoes"],
 "Beauty":["Mighty Patch acne patches"],
 "Home & Kitchen":["Stanley Quencher tumbler"],
 "Health & Household":["whey protein powder"],
 "Toys & Games":["LCD writing tablet kids"],
 "Sports & Outdoors":["reusable ice packs"],
 "Baby":["baby wipes"],
 "Pet Supplies":["cat litter"],
 "Appliances":["countertop ice maker"],
 "Cell Phones & Accessories":["phone screen protector"],
 "Computers & Accessories":["USB-C charger"],
 "Video Games":["gaming headset"],
 "Musical Instruments":["wireless lavalier microphone"],
}
def _asin(value: Any)->Optional[str]:
 m=ASIN_RE.search(str(value or "")); return m.group(1).upper() if m else None
def _detail_url(link:str)->Optional[str]:
 p=urlsplit(str(link or ""))
 host=p.netloc.lower().replace("www.","")
 allowed={d.lower().replace("www.","") for d in AMAZON_DISCOVERY_DOMAINS}
 if p.scheme.lower() not in {"http","https"} or host not in allowed: return None
 if not re.search(r"/(?:dp|gp/product)/[A-Z0-9]{10}(?:[/?]|$)",p.path,re.I): return None
 q=dict(parse_qsl(p.query,keep_blank_values=True)); q["tag"]=AFFILIATE_TAG
 return urlunsplit(("https",host,p.path.rstrip("/"),urlencode(q),""))
def _bought(v:Any)->int:
 m=re.search(r"([0-9][0-9,]*)\s*([KkMm])?",str(v or ""))
 if not m:return 0
 n=int(m.group(1).replace(",","")); s=(m.group(2) or "").lower()
 return n*(1000 if s=="k" else 1000000 if s=="m" else 1)
def _candidate(raw:Dict[str,Any],category:str)->Optional[Dict[str,Any]]:
 asin=str(raw.get("asin") or _asin(raw.get("link")) or "").upper(); link=_detail_url(raw.get("link","")); title=str(raw.get("title") or "").strip(); price=raw.get("extracted_price")
 if not asin or not link or len(title)<6 or price in (None,"") or registry.is_published(asin=asin,url=link): return None
 badges=raw.get("badges") or []; badges=[badges] if isinstance(badges,str) else badges; bt=" ".join(map(str,badges)).lower()
 if "unavailable" in bt:return None
 bought=_bought(raw.get("bought_last_month")); rating=float(raw.get("rating") or 0); reviews=int(raw.get("reviews") or 0); deal=50000 if "deal" in bt else 0
 return {"asin":asin,"affiliate_url":link,"product_url":link,"title":title,"category":category,"price":price,"rating":rating,"reviews":reviews,"bought_last_month":raw.get("bought_last_month"),"score":bought*1000+reviews+rating*100+deal-int(raw.get("position") or 999),"source":"composio_amazon","raw":raw}
async def _search(query:str,page:int=1)->List[Dict[str,Any]]:
 domain="amazon.com"
 # Primary: direct Composio search-tool execution. Search tools are auth-free and
 # this path avoids Tool Router session hangs while preserving the same provider.
 try:
  from agent import run_composio_tool
  data=await run_composio_tool("COMPOSIO_SEARCH_AMAZON",{"query":query,"amazon_domain":domain,"page":page},retries=2)
  if isinstance(data,dict) and isinstance(data.get("data"),dict):
   data=data["data"]
  products=list(data.get("products") or []) if isinstance(data,dict) else []
  if products:
   for p in products:
    if isinstance(p,dict): p.setdefault("_amazon_domain",domain)
   return products
  logger.warning("Direct Composio Amazon search returned no products domain=%s query=%s page=%s; trying Tool Router fallback",domain,query,page)
 except Exception as e:
  logger.warning("Direct Composio Amazon search failed domain=%s query=%s page=%s: %s; trying Tool Router fallback",domain,query,page,e)
 try:
  from mcp_bridge import composio_router_search_amazon
  data=await composio_router_search_amazon(query,domain,page)
  if isinstance(data,dict) and isinstance(data.get("data"),dict): data=data["data"]
  products=list(data.get("products") or []) if isinstance(data,dict) else []
  if not products and isinstance(data,dict):
   for item in (data.get("results") or []):
    response=item.get("response") if isinstance(item,dict) else None
    payload=response.get("data") if isinstance(response,dict) else None
    if isinstance(payload,dict):
     products.extend(payload.get("products") or [])
  for p in products:
   if isinstance(p,dict): p.setdefault("_amazon_domain",domain)
  return products
 except Exception as e:
  logger.warning("Composio Amazon Tool Router fallback failed domain=%s query=%s page=%s: %s",domain,query,page,e)
  return []
async def discover_category(category:str,exclude_asins:Optional[Set[str]]=None)->Optional[Dict[str,Any]]:
 excluded={x.upper() for x in (exclude_asins or set())}|registry.all_published_asins(); candidates=[]
 for q in CATEGORY_QUERIES.get(category,[category]):
  try:candidates += [c for c in (_candidate(x,category) for x in await _search(q,1)) if c and c["asin"] not in excluded]
  except Exception as e:logger.warning("Composio Amazon search failed for %s: %s",category,e)
 return sorted(candidates,key=lambda x:(-x["score"],x["asin"]))[0] if candidates else None
async def discover_fifteen(exclude_asins:Optional[Set[str]]=None)->List[Dict[str,Any]]:
 excluded={x.upper() for x in (exclude_asins or set())}|registry.all_published_asins(); selected=[]
 for category in CATEGORY_QUERIES:
  c=await discover_category(category,excluded)
  if c:selected.append(c); excluded.add(c["asin"])
 if len(selected)<15:
  extras=[]
  for category,q in CATEGORY_QUERIES.items():
   try:extras += [c for c in (_candidate(x,category) for x in await _search(q[0],2)) if c and c["asin"] not in excluded]
   except Exception as e:logger.warning("Composio Amazon fallback failed for %s: %s",category,e)
  for c in sorted(extras,key=lambda x:(-x["score"],x["asin"])):
   if c["asin"] in excluded:continue
   selected.append(c); excluded.add(c["asin"])
   if len(selected)==15:break
 if len(selected)!=15:raise RuntimeError(f"Composio Amazon discovery produced {len(selected)}/15 unique US products")
 return selected
def composio_ready()->bool:return bool(os.getenv("COMPOSIO_API_KEY","").strip() and os.getenv("COMPOSIO_ENTITY_ID","").strip())
