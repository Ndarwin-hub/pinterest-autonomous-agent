"""Amazon US discovery with Composio primary and direct-Amazon fallback."""
from __future__ import annotations
import logging, os, re, html
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit, quote_plus, unquote
import httpx
from bs4 import BeautifulSoup
from published_registry import registry

logger = logging.getLogger("pinterest-agent.amazon_composio_discovery")
AMAZON_DISCOVERY_DOMAINS = ["amazon.com"]
AMAZON_DOMAIN = AMAZON_DISCOVERY_DOMAINS[0]
AFFILIATE_TAG = "desiredplus-20"
INDEPENDENT_SEARCH_ENABLED = os.getenv("PIN_N_INDEPENDENT_SEARCH_ENABLED", "1").strip().lower() not in {"0","false","no"}
INDEPENDENT_SEARCH_ENDPOINT = os.getenv("PIN_N_INDEPENDENT_SEARCH_ENDPOINT", "https://html.duckduckgo.com/html/").strip()
ASIN_RE = re.compile(r"(?:/dp/|/gp/product/)([A-Z0-9]{10})(?:[/?]|$)", re.I)
CATEGORY_QUERIES = {
 "Electronics":["surge protector power strip"], "Clothing/Shoes":["running shoes"],
 "Beauty":["Mighty Patch acne patches"], "Home & Kitchen":["Stanley Quencher tumbler"],
 "Health & Household":["whey protein powder"], "Toys & Games":["LCD writing tablet kids"],
 "Sports & Outdoors":["reusable ice packs"], "Baby":["baby wipes"], "Pet Supplies":["cat litter"],
 "Appliances":["countertop ice maker"], "Watches & Clocks":["wristwatch","smartwatch","digital watch","alarm clock","wall clock"], "Cell Phones & Accessories":["phone screen protector"],
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
 independent = str(raw.get("source") or "").startswith("independent_web_search")
 if not asin or not link or len(title)<6 or (price in (None,"") and not independent) or registry.is_published(asin=asin,url=link): return None
 if independent:
  tl=title.lower().strip()
  restricted=("pharmacy","prescription","prescribed","medication","medicine","drug","metformin","sitagliptin","fluticasone","vilanterol","insulin","antibiotic")
  generic=(tl in {"amazon","amazon.com"} or tl == f"product {asin.lower()}" or len(re.findall(r"[a-zA-Z]{3,}",title)) < 2)
  if generic or any(term in tl for term in restricted): return None
  if len(title)<20: return None
  if price in (None,""): price=0
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
async def _independent_web_search(query:str,page:int=1)->List[Dict[str,Any]]:
    """Independent discovery fallback: search-engine result pages only.
    It never requests an Amazon URL; Amazon links are metadata only.
    """
    if not INDEPENDENT_SEARCH_ENABLED:
        return []
    params = {"q": f"site:amazon.com/dp/ OR site:amazon.com/gp/product/ {query}", "s": max(0,(int(page)-1)*30)}
    headers = {"User-Agent": os.getenv("PIN_N_SEARCH_USER_AGENT","Mozilla/5.0"), "Accept":"text/html,application/xhtml+xml", "Accept-Language":"en-US,en;q=0.9"}
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
            response = await client.get(INDEPENDENT_SEARCH_ENDPOINT, params=params)
            response.raise_for_status()
        soup=BeautifulSoup(response.text,"lxml"); out=[]
        for result in soup.select("div.result"):
            anchor=result.select_one("a.result__a[href]")
            if not anchor: continue
            href=anchor.get("href") or ""
            if "uddg=" in href:
                try: href=unquote(dict(parse_qsl(urlsplit(href).query)).get("uddg",""))
                except Exception: pass
            result_text=html.unescape(result.get_text(" ",strip=True))
            asin=_asin(href)
            if not asin:
                requested_asin=_asin(query)
                if requested_asin and requested_asin in result_text.upper():
                    asin=requested_asin
            if not asin: continue
            raw_title=anchor.get("title") or anchor.get_text(" ",strip=True)
            title=html.unescape(str(raw_title or "")).strip()
            if title.lower() in {"amazon","amazon.com","amazon.com:"} or len(title)<20:
                snippet=result.select_one(".result__snippet")
                snippet_text=html.unescape(snippet.get_text(" ",strip=True) if snippet else result_text)
                snippet_text=re.sub(r"\bAmazon(?:\.com)?\b[:\s-]*","",snippet_text,flags=re.I).strip(" -:|")
                if len(snippet_text)>=20:
                    title=snippet_text[:160]
            if title.lower() in {"amazon","amazon.com","amazon.com:"}:
                title=f"Product {asin}"
            destination=f"https://www.amazon.com/dp/{asin}?tag={AFFILIATE_TAG}"
            out.append({"asin":asin,"link":destination,"title":title,"extracted_price":0,"rating":0,"reviews":0,"bought_last_month":"","badges":[],"position":len(out)+1,"_amazon_domain":"amazon.com","source":"independent_web_search_duckduckgo"})
            if len(out)>=20: break
        logger.info("Independent Pin N search query=%s page=%s products=%s",query,page,len(out))
        return out
    except Exception as e:
        logger.warning("Independent Pin N search failed query=%s page=%s: %s",query,page,e)
        return []

async def _independent_bing_product_search(query:str,page:int=1)->List[Dict[str,Any]]:
    """Independent Bing web search fallback; never requests Amazon URLs."""
    try:
        endpoint="https://www.bing.com/search"
        headers={"User-Agent":os.getenv("PIN_N_SEARCH_USER_AGENT","Mozilla/5.0"),"Accept":"text/html,application/xhtml+xml","Accept-Language":"en-US,en;q=0.9"}
        async with httpx.AsyncClient(timeout=20.0,follow_redirects=True,headers=headers) as client:
            response=await client.get(endpoint,params={"q":f"{query} Amazon US product","first":max(1,(int(page)-1)*10+1)})
            response.raise_for_status()
        soup=BeautifulSoup(response.text,"lxml"); out=[]
        for result in soup.select("li.b_algo"):
            anchor=result.select_one("h2 a[href]")
            if not anchor: continue
            href=anchor.get("href") or ""
            text_blob=html.unescape(result.get_text(" ",strip=True))
            asin=_asin(href) or (_asin(text_blob) if "amazon" in text_blob.lower() else None)
            if not asin: continue
            title=html.unescape(anchor.get_text(" ",strip=True))
            if not title or len(title)<6: continue
            out.append({"asin":asin,"link":f"https://www.amazon.com/dp/{asin}?tag={AFFILIATE_TAG}","title":title,"extracted_price":0,"rating":0,"reviews":0,"bought_last_month":"","badges":[],"position":len(out)+1,"_amazon_domain":"amazon.com","source":"independent_web_search_bing"})
            if len(out)>=20: break
        logger.info("Independent Bing product search query=%s page=%s products=%s",query,page,len(out))
        return out
    except Exception as exc:
        logger.warning("Independent Bing product search failed query=%s page=%s: %s",query,page,str(exc)[:300])
        return []

async def _disabled_amazon_html_search(query:str,page:int=1)->List[Dict[str,Any]]:
    logger.warning("Direct Amazon HTML discovery is permanently disabled.")
    return []

async def _search(query:str,page:int=1)->List[Dict[str,Any]]:
 domain="amazon.com"; admin_blocked=False
 try:
  from agent import run_composio_tool
  data=await run_composio_tool("COMPOSIO_SEARCH_AMAZON",{"query":query,"amazon_domain":domain,"page":page},retries=0)
  if isinstance(data,dict) and isinstance(data.get("data"),dict): data=data["data"]
  products=list(data.get("products") or []) if isinstance(data,dict) else []
  if products:
   for p in products:
    if isinstance(p,dict): p.setdefault("_amazon_domain",domain)
   return products
  logger.warning("Direct Composio Amazon search returned no products; trying alternate discovery")
 except Exception as e:
  msg=str(e); admin_blocked=("403" in msg or "temporarily disabled by the administrator" in msg.lower() or "execution of toolkit" in msg.lower())
  logger.warning("Direct Composio Amazon search failed admin_blocked=%s: %s",admin_blocked,msg[:500])
 if not admin_blocked:
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
  except Exception as e: logger.warning("Composio Amazon Tool Router search failed: %s",str(e)[:500])
 return (await _independent_web_search(query,page)) + (await _independent_bing_product_search(query,page))

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
