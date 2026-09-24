"""Amazon US discovery with Composio primary and direct-Amazon fallback."""
from __future__ import annotations
import logging, os, re, html
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, quote_plus
import httpx
from bs4 import BeautifulSoup
from published_registry import registry

logger = logging.getLogger("pinterest-agent.amazon_composio_discovery")
AMAZON_DISCOVERY_DOMAINS = ["amazon.com"]
AMAZON_DOMAIN = AMAZON_DISCOVERY_DOMAINS[0]
AFFILIATE_TAG = "desiredplus-20"
ASIN_RE = re.compile(r"(?:/dp/|/gp/product/)([A-Z0-9]{10})(?:[/?]|$)", re.I)
CATEGORY_QUERIES = {
 "Electronics":["surge protector power strip"], "Clothing/Shoes":["running shoes"],
 "Beauty":["Mighty Patch acne patches"], "Home & Kitchen":["Stanley Quencher tumbler"],
 "Health & Household":["whey protein powder"], "Toys & Games":["LCD writing tablet kids"],
 "Sports & Outdoors":["reusable ice packs"], "Baby":["baby wipes"], "Pet Supplies":["cat litter"],
 "Appliances":["countertop ice maker"], "Cell Phones & Accessories":["phone screen protector"],
 "Computers & Accessories":["USB-C charger"], "Video Games":["gaming headset"],
 "Musical Instruments":["wireless lavalier microphone"],
}

# Backward-compatible board query profiles used by the independent Pin N discovery layer.
# Keep this additive: Pin A continues to use its dedicated board scheduler.
BOARD_SEARCH_PROFILES = {k:list(v) for k,v in CATEGORY_QUERIES.items()}
def _asin(value: Any)->Optional[str]:
 m=ASIN_RE.search(str(value or "")); return m.group(1).upper() if m else None
def _detail_url(link:str)->Optional[str]:
 p=urlsplit(str(link or "")); host=p.netloc.lower().replace("www.","")
 if p.scheme.lower() not in {"http","https"} or host not in {"amazon.com"}: return None
 if not re.search(r"/(?:dp|gp/product)/[A-Z0-9]{10}(?:[/?]|$)",p.path,re.I): return None
 q=dict(parse_qsl(p.query,keep_blank_values=True)); q["tag"]=AFFILIATE_TAG
 return urlunsplit(("https","amazon.com",p.path.rstrip("/"),urlencode(q),""))
def _bought(v:Any)->int:
 m=re.search(r"([0-9][0-9,]*)\s*([KkMm])?",str(v or ""))
 if not m:return 0
 n=int(m.group(1).replace(",","")); s=(m.group(2) or "").lower()
 return n*(1000 if s=="k" else 1000000 if s=="m" else 1)
def _candidate(raw:Dict[str,Any],category:str)->Optional[Dict[str,Any]]:
 asin=str(raw.get("asin") or _asin(raw.get("link")) or "").upper()
 link=_detail_url(raw.get("link","")); title=str(raw.get("title") or "").strip(); price=raw.get("extracted_price")
 if not asin or not link or len(title)<6 or price in (None,"") or registry.is_published(asin=asin,url=link): return None
 badges=raw.get("badges") or []; badges=[badges] if isinstance(badges,str) else badges; bt=" ".join(map(str,badges)).lower()
 if "unavailable" in bt:return None
 bought=_bought(raw.get("bought_last_month")); rating=float(raw.get("rating") or 0); reviews=int(raw.get("reviews") or 0)
 deal=50000 if "deal" in bt else 0
 return {"asin":asin,"affiliate_url":link,"product_url":link,"title":title,"category":category,"price":price,
 "rating":rating,"reviews":reviews,"bought_last_month":raw.get("bought_last_month"),
 "score":bought*1000+reviews+rating*100+deal-int(raw.get("position") or 999),
 "source":raw.get("source") or "amazon_discovery","raw":raw}
def _parse_price(text:str)->Optional[float]:
 m=re.search(r"([0-9][0-9,]*\.?[0-9]*)",text or "")
 try:return float(m.group(1).replace(",","")) if m else None
 except Exception:return None
def _parse_amazon_html(body:str,page:int)->List[Dict[str,Any]]:
 soup=BeautifulSoup(body,"lxml"); out=[]; position=0
 for card in soup.select('div[data-component-type="s-search-result"][data-asin]'):
  asin=(card.get("data-asin") or "").strip().upper()
  if not re.fullmatch(r"[A-Z0-9]{10}",asin): continue
  title_el=card.select_one("h2 a span") or card.select_one("h2 span")
  title=html.unescape(title_el.get_text(" ",strip=True)) if title_el else ""
  link_el=card.select_one('h2 a[href]') or card.select_one('a[href*="/dp/"]'); href=link_el.get("href") if link_el else ""
  price_el=card.select_one(".a-price .a-offscreen"); price=_parse_price(price_el.get_text(" ",strip=True) if price_el else "")
  rating_el=card.select_one(".a-icon-alt"); rating_text=rating_el.get_text(" ",strip=True) if rating_el else ""
  try: rating=float(re.search(r"([0-5](?:\.[0-9])?)",rating_text).group(1))
  except Exception: rating=0
  review_el=card.select_one("span.a-size-base.s-underline-text"); reviews_text=review_el.get_text(" ",strip=True) if review_el else ""
  try: reviews=int(re.sub(r"[^0-9]","",reviews_text) or 0)
  except Exception: reviews=0
  if not href or price is None or not title: continue
  position+=1
  out.append({"asin":asin,"link":("https://www.amazon.com"+href if href.startswith("/") else href),"title":title,
    "extracted_price":price,"rating":rating,"reviews":reviews,"bought_last_month":"","badges":[],"position":position,
    "_amazon_domain":"amazon.com","source":"amazon_html"})
 return out
async def _amazon_html_search(query:str,page:int=1)->List[Dict[str,Any]]:
 url=f"https://www.amazon.com/s?k={quote_plus(query)}&page={max(1,int(page))}"
 headers={"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
          "Accept":"text/html,application/xhtml+xml","Accept-Language":"en-US,en;q=0.9","Cache-Control":"no-cache"}
 try:
  async with httpx.AsyncClient(timeout=25,follow_redirects=True,headers=headers) as client:
   r=await client.get(url); r.raise_for_status(); products=_parse_amazon_html(r.text,page)
   logger.info("Direct Amazon HTML fallback query=%s page=%s products=%s",query,page,len(products))
   return products
 except Exception as e:
  logger.warning("Direct Amazon HTML fallback failed query=%s page=%s: %s",query,page,e); return []
async def _search(query:str,page:int=1)->List[Dict[str,Any]]:
 domain="amazon.com"
 try:
  from agent import run_composio_tool
  data=await run_composio_tool("COMPOSIO_SEARCH_AMAZON",{"query":query,"amazon_domain":domain,"page":page},retries=2)
  if isinstance(data,dict) and isinstance(data.get("data"),dict): data=data["data"]
  products=list(data.get("products") or []) if isinstance(data,dict) else []
  if products:
   for p in products:
    if isinstance(p,dict): p.setdefault("_amazon_domain",domain)
   return products
  logger.warning("Direct Composio Amazon search returned no products; trying Tool Router")
 except Exception as e: logger.warning("Direct Composio Amazon search failed: %s; trying Tool Router",e)
 try:
  from mcp_bridge import composio_router_search_amazon
  data=await composio_router_search_amazon(query,domain,page)
  if isinstance(data,dict) and isinstance(data.get("data"),dict): data=data["data"]
  products=list(data.get("products") or []) if isinstance(data,dict) else []
  if not products and isinstance(data,dict):
   for item in (data.get("results") or []):
    response=item.get("response") if isinstance(item,dict) else None; payload=response.get("data") if isinstance(response,dict) else None
    if isinstance(payload,dict): products.extend(payload.get("products") or [])
  if products:
   for p in products:
    if isinstance(p,dict): p.setdefault("_amazon_domain",domain)
   return products
  logger.warning("Composio Tool Router Amazon search returned no products; using direct Amazon")
 except Exception as e: logger.warning("Composio Amazon Tool Router fallback failed: %s; using direct Amazon",e)
 return await _amazon_html_search(query,page)
async def discover_category(category:str,exclude_asins:Optional[Set[str]]=None)->Optional[Dict[str,Any]]:
 excluded={x.upper() for x in (exclude_asins or set())}|registry.all_published_asins(); candidates=[]
 for q in CATEGORY_QUERIES.get(category,[category]):
  for page in (1,2):
   try:
    page_products=await _search(q,page)
    page_candidates=[c for c in (_candidate(x,category) for x in page_products) if c and c["asin"] not in excluded]
    candidates += page_candidates
    if page_candidates: break
   except Exception as e: logger.warning("Amazon discovery failed for %s: %s",category,e)
 return sorted(candidates,key=lambda x:(-x["score"],x["asin"]))[0] if candidates else None
async def discover_fifteen(exclude_asins:Optional[Set[str]]=None)->List[Dict[str,Any]]:
 excluded={x.upper() for x in (exclude_asins or set())}|registry.all_published_asins(); selected=[]
 for category in CATEGORY_QUERIES:
  c=await discover_category(category,excluded)
  if c:selected.append(c); excluded.add(c["asin"])
 if len(selected)<15:
  extras=[]
  for category,q in CATEGORY_QUERIES.items():
   try: extras += [c for c in (_candidate(x,category) for x in await _search(q[0],2)) if c and c["asin"] not in excluded]
   except Exception as e: logger.warning("Amazon fallback failed for %s: %s",category,e)
  for c in sorted(extras,key=lambda x:(-x["score"],x["asin"])):
   if c["asin"] in excluded:continue
   selected.append(c); excluded.add(c["asin"])
   if len(selected)==15:break
 if len(selected)!=15: raise RuntimeError(f"Amazon discovery produced {len(selected)}/15 unique US products")
 return selected
def composio_ready()->bool:
 return bool(os.getenv("COMPOSIO_API_KEY","").strip() and os.getenv("COMPOSIO_ENTITY_ID","").strip())
