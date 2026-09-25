"""Five-layer Amazon US discovery failover.

Order: Composio Amazon -> Creators API -> direct Amazon catalog -> durable ASIN
reservoir -> Amazon Best Sellers/New Releases. Legacy search engines remain outside
this module and are only used by the caller if every Amazon-native layer is empty.
"""
from __future__ import annotations
import html, logging, os, re
from typing import Any, Dict, List
from urllib.parse import urlencode
import httpx
from bs4 import BeautifulSoup
from amazon_reservoir import get as reservoir_get, put_many as reservoir_put_many

log=logging.getLogger("pinterest-agent.amazon_five_layer")
TAG="desiredplus-20"
ASIN_RE=re.compile(r"(?:/dp/|/gp/product/)([A-Z0-9]{10})(?:[/?]|$)",re.I)
BEST={"Electronics":"/Best-Sellers-Electronics/zgbs/electronics","Clothing/Shoes":"/Best-Sellers-Clothing-Shoes-Jewelry/zgbs/fashion","Beauty":"/Best-Sellers-Beauty/zgbs/beauty","Home & Kitchen":"/Best-Sellers-Home-Kitchen/zgbs/home-garden","Health & Household":"/Best-Sellers-Health-Personal-Care/zgbs/hpc","Toys & Games":"/Best-Sellers-Toys-Games/zgbs/toys-and-games","Sports & Outdoors":"/Best-Sellers-Sports-Outdoors/zgbs/sporting-goods","Baby":"/Best-Sellers-Baby/zgbs/baby-products","Pet Supplies":"/Best-Sellers-Pet-Supplies/zgbs/pet-supplies","Appliances":"/Best-Sellers-Appliances/zgbs/appliances","Cell Phones & Accessories":"/Best-Sellers-Cell-Phones-Accessories/zgbs/wireless","Computers & Accessories":"/Best-Sellers-Computers-Accessories/zgbs/pc","Video Games":"/Best-Sellers-Video-Games/zgbs/videogames","Musical Instruments":"/Best-Sellers-Musical-Instruments/zgbs/musical-instruments"}

def _asin(v:Any):
 m=ASIN_RE.search(str(v or "")); return m.group(1).upper() if m else None

def _affiliate(asin:str,source:Any=None):
 u=str(source or ""); return f"https://www.amazon.com/dp/{asin}?{urlencode({'tag':TAG})}" if not u else (u if "amazon.com" in u and _asin(u) else f"https://www.amazon.com/dp/{asin}?{urlencode({'tag':TAG})}")

def _parse_search(body:str):
 soup=BeautifulSoup(body,"lxml"); out=[]
 for card in soup.select('div[data-component-type="s-search-result"][data-asin]'):
  asin=str(card.get("data-asin") or "").upper()
  if not re.fullmatch(r"[A-Z0-9]{10}",asin): continue
  a=card.select_one('h2 a[href]') or card.select_one('a[href*="/dp/"]'); t=card.select_one("h2 span")
  title=html.unescape(t.get_text(" ",strip=True) if t else "")
  if not title or not a: continue
  href=a.get("href") or ""; price=card.select_one(".a-price .a-offscreen")
  pm=re.search(r"([0-9][0-9,]*\\.?[0-9]*)",price.get_text(" ",strip=True) if price else "")
  out.append({"asin":asin,"link":href if href.startswith("http") else "https://www.amazon.com"+href,"title":title,"extracted_price":float(pm.group(1).replace(",","")) if pm else 0,"rating":0,"reviews":0,"badges":[],"bought_last_month":"","position":len(out)+1,"source":"amazon_direct_html"})
 return out

async def _direct_search(query:str,page:int):
 try:
  headers={"User-Agent":os.getenv("PIN_N_AMAZON_USER_AGENT","Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/140 Mobile Safari/537.36"),"Accept-Language":"en-US,en;q=0.9"}
  async with httpx.AsyncClient(timeout=25,follow_redirects=True,headers=headers) as c:
   r=await c.get("https://www.amazon.com/s",params={"k":query,"page":max(1,int(page))}); r.raise_for_status(); return _parse_search(r.text)
 except Exception as e:
  log.warning("layer3 direct Amazon failed: %s",str(e)[:300]); return []

async def _best_sellers(query:str,page:int):
 path="/Best-Sellers/zgbs"; q=str(query).lower()
 for k,p in BEST.items():
  if k.lower() in q or any(w in q for w in k.lower().split() if len(w)>4): path=p; break
 try:
  headers={"User-Agent":os.getenv("PIN_N_AMAZON_USER_AGENT","Mozilla/5.0"),"Accept-Language":"en-US,en;q=0.9"}
  async with httpx.AsyncClient(timeout=25,follow_redirects=True,headers=headers) as c:
   r=await c.get("https://www.amazon.com"+path,params={"pg":max(1,int(page))}); r.raise_for_status()
  soup=BeautifulSoup(r.text,"lxml"); out=[]; seen=set()
  for a in soup.select('a[href*="/dp/"],a[href*="/gp/product/"]'):
   asin=_asin(a.get("href"));
   if not asin or asin in seen: continue
   card=a.find_parent(["div","li"]) or a.parent; title=a.get_text(" ",strip=True)
   if len(title)<20 and card: title=card.get_text(" ",strip=True)
   title=re.sub(r"\\s+"," ",html.unescape(title)).strip()
   if len(title)<20: continue
   seen.add(asin); out.append({"asin":asin,"link":_affiliate(asin),"title":title[:300],"extracted_price":0,"rating":0,"reviews":0,"badges":[],"bought_last_month":"","position":len(out)+1,"source":"amazon_best_sellers"})
   if len(out)>=20: break
  return out
 except Exception as e:
  log.warning("layer5 Amazon Best Sellers failed: %s",str(e)[:300]); return []

async def search(query:str,page:int=1)->List[Dict[str,Any]]:
 # Layer 1: current Composio Amazon route. Admin 403 is non-fatal.
 try:
  from agent import run_composio_tool
  data=await run_composio_tool("COMPOSIO_SEARCH_AMAZON",{"query":query,"amazon_domain":"amazon.com","page":page},retries=0)
  if isinstance(data,dict) and isinstance(data.get("data"),dict): data=data["data"]
  products=list(data.get("products") or []) if isinstance(data,dict) else []
  if products: reservoir_put_many(products); log.info("layer1 Composio products=%s",len(products)); return products
 except Exception as e: log.warning("layer1 Composio unavailable: %s",str(e)[:300])
 # Layer 2: official Amazon Creators API, if credentials + eligibility exist.
 try:
  from amazon_client import AmazonCreatorsClient, amazon_credentials_present
  if amazon_credentials_present():
   items=await AmazonCreatorsClient().search_items(query,item_count=10); products=[]
   for item in items:
    if not isinstance(item,dict): continue
    asin=str(item.get("asin") or item.get("ASIN") or item.get("itemId") or "").upper()
    u=item.get("detailPageURL") or item.get("detailPageUrl") or item.get("url") or ""
    info=item.get("itemInfo") if isinstance(item.get("itemInfo"),dict) else {}
    ti=info.get("title") if isinstance(info,dict) else {}
    title=ti.get("displayValue") if isinstance(ti,dict) else (item.get("title") or "")
    if re.fullmatch(r"[A-Z0-9]{10}",asin) and u:
     products.append({"asin":asin,"link":u,"title":str(title),"extracted_price":0,"rating":0,"reviews":0,"badges":[],"bought_last_month":"","position":len(products)+1,"source":"amazon_creators_api"})
   if products: reservoir_put_many(products); log.info("layer2 Creators API products=%s",len(products)); return products
 except Exception as e: log.warning("layer2 Creators API unavailable: %s",str(e)[:300])
 # Layer 3: direct Amazon search page.
 products=await _direct_search(query,page)
 if products: reservoir_put_many(products); log.info("layer3 direct Amazon products=%s",len(products)); return products
 # Layer 4: durable ASIN reservoir.
 cached=reservoir_get(25)
 if cached:
  for x in cached: x.setdefault("link",x.get("affiliate_url") or x.get("product_url")); x.setdefault("source","amazon_reservoir")
  log.info("layer4 reservoir products=%s",len(cached)); return cached
 # Layer 5: Amazon Best Sellers/New Releases catalog.
 products=await _best_sellers(query,page)
 if products: reservoir_put_many(products); log.info("layer5 Best Sellers products=%s",len(products)); return products
 return []
