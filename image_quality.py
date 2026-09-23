"""Zero-tolerance Pinterest image sourcing and quality selection."""
from __future__ import annotations
import asyncio, io, logging, os, re
from typing import Any, Dict, List, Optional, Tuple
import httpx
from PIL import Image, ImageFile
logger=logging.getLogger("pinterest-agent.image-quality")
MIN_DIMENSION=800
PREFERRED_MIN_DIMENSION=1200
PREFERRED_PORTRAIT_MIN_HEIGHT=1200
MAX_ASPECT=2.0
MIN_SCORE=85
MAX_CANDIDATES_PER_PIN=12
MAX_IMAGE_BYTES_TO_INSPECT=5*1024*1024
PEXELS_API_KEY=os.getenv("PEXELS_API_KEY","").strip()
COMPOSIO_API_KEY=os.getenv("COMPOSIO_API_KEY","").strip()
async def inspect_image_url(url:str)->Optional[Tuple[int,int]]:
    if not url or not str(url).startswith(("http://","https://")): return None
    parser=ImageFile.Parser(); total=0
    try:
        async with httpx.AsyncClient(timeout=18,follow_redirects=True) as client:
            async with client.stream("GET",url,headers={"User-Agent":"Mozilla/5.0 PinterestAgent/quality"}) as r:
                if r.status_code>=400:return None
                ct=(r.headers.get("content-type") or "").lower()
                if ct and "image" not in ct:return None
                async for chunk in r.aiter_bytes(16384):
                    total+=len(chunk)
                    if total>MAX_IMAGE_BYTES_TO_INSPECT:break
                    parser.feed(chunk)
                    if parser.image is not None:return int(parser.image.width),int(parser.image.height)
    except Exception:pass
    return None
def hard_gate(w:int,h:int)->Tuple[bool,str]:
    if min(w,h)<MIN_DIMENSION:return False,f"too_small:{w}x{h}"
    ratio=max(w,h)/max(1,min(w,h))
    if ratio>MAX_ASPECT:return False,f"bad_aspect:{w}x{h}"
    return True,"ok"
async def validate(c:Dict[str,Any])->Optional[Dict[str,Any]]:
    d=await inspect_image_url(c.get("url",""))
    if not d:return None
    w,h=d; ok,reason=hard_gate(w,h)
    if not ok:return None
    x=dict(c);x.update(width=w,height=h,quality_gate=reason);return x
async def validate_many(raw:List[Dict[str,Any]])->List[Dict[str,Any]]:
    seen=set();unique=[]
    for c in raw:
        u=c.get("url")
        if u and u not in seen:
            seen.add(u);unique.append(c)
    checked=await asyncio.gather(*(validate(c) for c in unique[:60]))
    return [c for c in checked if c]
def score(c:Dict[str,Any],product:Dict[str,Any],strategy:str)->int:
    p=(c.get("provider") or "").lower();src=(c.get("source") or "").lower();name=(product.get("name") or "").lower();brand=(product.get("brand") or "").lower();w,h=int(c.get("width") or 0),int(c.get("height") or 0);ratio=w/max(1,h);s=45
    if p=="product_page":s+=30
    elif p=="composio_search_image":
        s+=20
        if brand and brand in src:s+=12
        toks=[x for x in re.findall(r"[a-z0-9]+",name) if len(x)>3];s+=min(10,sum(1 for x in toks[:5] if x in src))
    elif p=="pexels":s+=8
    if min(w,h)>=PREFERRED_MIN_DIMENSION:s+=12
    if .60<=ratio<=.80:s+=14
    elif .80<ratio<=1.05:s+=10
    elif 1.05<ratio<=1.35:s+=7
    elif 1.35<ratio<=1.80:s+=3
    if c.get("original"):s+=2
    return min(100,s)


async def _visual_signature(url:str)->Optional[Tuple[Tuple[int,...],Tuple[int,...]]]:
    """Create a lightweight visual fingerprint for human-visible near-duplicate detection."""
    if not url or not str(url).startswith(("http://","https://")): return None
    try:
        async with httpx.AsyncClient(timeout=12,follow_redirects=True) as client:
            r=await client.get(url,headers={"User-Agent":"Mozilla/5.0 PinterestAgent/visual"})
            if r.status_code>=400 or len(r.content)>1024*1024:return None
        im=Image.open(io.BytesIO(r.content)).convert("RGB").resize((32,32))
        px=list(im.getdata())
        gray=[(299*r+587*g+114*b)//1000 for r,g,b in px]
        avg=sum(gray)/len(gray)
        ah=tuple(1 if v>=avg else 0 for v in gray)
        hist=[0]*64
        for r,g,b in px:
            hist=((r//64)*16)+((g//64)*4)+(b//64),
            hist_index=hist[0]
            # histogram bucket update kept explicit for portability
            if not hasattr(_visual_signature,"_dummy"): pass
        hist=[0]*64
        for r,g,b in px: hist[((r//64)*16)+((g//64)*4)+(b//64)]+=1
        total=float(len(px)); hist=tuple(round(v/total,5) for v in hist)
        return ah,hist
    except Exception:return None

def _visual_distance(a,b)->float:
    if not a or not b:return 1.0
    ah1,h1=a; ah2,h2=b
    hamming=sum(x!=y for x,y in zip(ah1,ah2))/max(1,len(ah1))
    color=sum(abs(x-y) for x,y in zip(h1,h2))/max(1,len(h1))
    return 0.75*hamming+0.25*min(1.0,color*4.0)

async def _remove_visual_duplicates(candidates:List[Dict[str,Any]],used_urls:set)->List[Dict[str,Any]]:
    if not candidates:return []
    used_sigs=[]
    for u in list(used_urls)[:4]:
        sig=await _visual_signature(u)
        if sig:used_sigs.append(sig)
    selected=[]; selected_sigs=[]
    for c in candidates:
        u=c.get("url")
        if not u or u in used_urls:continue
        sig=await _visual_signature(u)
        if sig is None:
            selected.append(c); continue
        distances=[_visual_distance(sig,s) for s in used_sigs+selected_sigs]
        if distances and min(distances)<0.12:continue
        c["visual_distance"]=round(min(distances),4) if distances else 1.0
        selected.append(c); selected_sigs.append(sig)
        if len(selected)>=MAX_CANDIDATES_PER_PIN:break
    return selected

async def search_pexels(query:str,agent_mod:Any)->List[Dict[str,Any]]:
    if PEXELS_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r=await client.get("https://api.pexels.com/v1/search",headers={"Authorization":PEXELS_API_KEY},params={"query":query[:90],"orientation":"portrait","per_page":12})
                if r.status_code<400:
                    out=[]
                    for p in (r.json().get("photos") or []):
                        src=p.get("src") or {};u=src.get("original") or src.get("large2x") or src.get("large")
                        if u:out.append({"url":u,"provider":"pexels","id":str(p.get("id") or ""),"source":"pexels","license":"Pexels License","original":True})
                    return out
        except Exception:pass
    try:
        data=await agent_mod.run_composio_tool("PEXELS_SEARCH_PHOTOS",{"query":query[:80],"orientation":"portrait","per_page":10},retries=0)
        out=[]
        for p in (data.get("photos") or []):
            src=p.get("src") or {};u=src.get("large2x") or src.get("large") or src.get("original") or src.get("portrait")
            if u:out.append({"url":u,"provider":"pexels","id":str(p.get("id") or ""),"source":"pexels","license":"Pexels License","original":True})
        return out
    except Exception:return []
def _search_queries(product:Dict[str,Any],strategy:Dict[str,Any])->List[str]:
    name=(product.get("name") or "product").strip()
    brand=(product.get("brand") or "").strip()
    focus=(strategy.get("focus") or "product photo").strip()
    base=f"{brand} {name}" if brand and brand.lower() not in name.lower() else name
    return [
        f"{base} official product photo",
        f"{base} {focus} product image",
        f"{base} front product photography",
        f"{base} clean high resolution product photo",
    ]
async def choose_candidates(product:Dict[str,Any],strategy:Dict[str,Any],pin_index:int,used_urls:set,agent_mod:Any)->List[Dict[str,Any]]:
    """Search multiple independent query angles, merge all candidates, hard-validate, then rank globally.

    The caller's Composio budget limits actual executions. Four targeted Composio image
    searches are attempted per Pin so the five Pins can compare a broad candidate pool.
    No candidate is accepted merely because it was found: dimensions/aspect and the
    minimum score gate still apply before ranking.
    """
    raw=[]
    for u in product.get("images") or []:
        if u and u not in used_urls:raw.append({"url":u,"provider":"product_page","source":"product page","license":"product_page","original":True})
    queries=_search_queries(product,strategy)
    for query in queries:
        try:raw.extend(await agent_mod.search_composio_images(query,num=10))
        except Exception:pass
    if not COMPOSIO_API_KEY:
        # Only use the direct/credentialed Pexels path when Composio is not the active
        # image-search route, avoiding duplicate quota use in the normal production path.
        raw.extend(await search_pexels(queries[0],agent_mod))
    valid=await validate_many(raw)
    for c in valid:c["score"]=score(c,product,strategy.get("key",""))
    valid=[c for c in valid if c.get("url") and c.get("url") not in used_urls and c.get("score",0)>=MIN_SCORE]
    valid.sort(key=lambda x:(x.get("score",0),x.get("provider") == "product_page",x.get("original",False)),reverse=True)
    diverse=await _remove_visual_duplicates(valid[:MAX_CANDIDATES_PER_PIN*2],used_urls)
    if diverse:return diverse[:MAX_CANDIDATES_PER_PIN]
    return valid[:1]
async def choose_best_image(product:Dict[str,Any],strategy:Dict[str,Any],pin_index:int,used_urls:set,agent_mod:Any)->Optional[Dict[str,Any]]:
    candidates=await choose_candidates(product,strategy,pin_index,used_urls,agent_mod)
    if not candidates:return None
    best=candidates[0];used_urls.add(best["url"]);return best
