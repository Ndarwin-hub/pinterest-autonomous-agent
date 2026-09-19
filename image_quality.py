"""Priority-ranked Pinterest image sourcing with usable fallback selection."""
from __future__ import annotations
import asyncio, logging, os, re
from typing import Any, Dict, List, Optional, Tuple
import io
from PIL import Image
import httpx
from PIL import ImageFile
logger=logging.getLogger("pinterest-agent.image-quality")
MIN_DIMENSION=100
PREFERRED_MIN_DIMENSION=1200
PREFERRED_PORTRAIT_MIN_HEIGHT=1200
MAX_ASPECT=2.0
MIN_SCORE=0
PERCEPTUAL_DUPLICATE_DISTANCE=10
MAX_CANDIDATES_PER_PIN=20
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
    if w<=0 or h<=0:return False,f"invalid_dimensions:{w}x{h}"
    if min(w,h)<MIN_DIMENSION:return False,f"too_small_to_use:{w}x{h}"
    ratio=max(w,h)/max(1,min(w,h))
    if ratio>MAX_ASPECT:return True,f"usable_extreme_aspect:{w}x{h}"
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
    short=min(w,h)
    if short>=PREFERRED_MIN_DIMENSION:s+=12
    elif short>=800:s+=7
    elif short>=400:s+=3
    elif short>=200:s-=3
    else:s-=8
    if .60<=ratio<=.80:s+=14
    elif .80<ratio<=1.05:s+=10
    elif 1.05<ratio<=1.35:s+=7
    elif 1.35<ratio<=1.80:s+=3
    elif ratio>MAX_ASPECT:s-=8
    if c.get("original"):s+=2
    return max(1,min(100,s))
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
        f"{base} official product photo high resolution",
        f"{base} {focus} product image high resolution",
        f"{base} front product photography 4k",
        f"{base} alternate product image high resolution",
        f"{base} manufacturer retailer product gallery image",
    ]
def _fingerprint_distance(a:Tuple[int,int],b:Tuple[int,int])->int:
    return (a[0]^b[0]).bit_count()+(a[1]^b[1]).bit_count()

async def image_fingerprint(url:str)->Optional[Tuple[int,int]]:
    try:
        async with httpx.AsyncClient(timeout=12,follow_redirects=True) as client:
            r=await client.get(url,headers={"User-Agent":"Mozilla/5.0 PinterestAgent/fingerprint"})
            if r.status_code>=400 or not r.content or len(r.content)>MAX_IMAGE_BYTES_TO_INSPECT:return None
        with Image.open(io.BytesIO(r.content)) as im:
            im=im.convert("L")
            a=im.resize((8,8),Image.Resampling.LANCZOS); ap=list(a.getdata()); avg=sum(ap)/len(ap)
            ah=sum((1<<i) for i,v in enumerate(ap) if v>=avg)
            d=im.resize((9,8),Image.Resampling.LANCZOS); dp=list(d.getdata()); dh=0; bit=0
            for y in range(8):
                for x in range(8):
                    if dp[y*9+x]>=dp[y*9+x+1]:dh|=1<<bit
                    bit+=1
            return ah,dh
    except Exception:
        return None

def _stored_fingerprints(used_urls:set)->List[Tuple[int,int]]:
    out=[]
    for value in used_urls:
        if isinstance(value,str) and value.startswith("__imgfp__:"):
            try:
                _,a,d=value.split(":",2);out.append((int(a,16),int(d,16)))
            except Exception:pass
    return out

def candidate_is_unique(candidate:Dict[str,Any],used_urls:set)->bool:
    url=str(candidate.get("url") or "")
    if not url or url in used_urls:return False
    fp=candidate.get("_fingerprint")
    if not fp:return True
    try:
        fp=(int(fp[0]),int(fp[1])) if not isinstance(fp,str) else (int(fp.split(":",1)[0],16),int(fp.split(":",1)[1],16))
    except Exception:return True
    return not any(_fingerprint_distance(fp,old)<=PERCEPTUAL_DUPLICATE_DISTANCE for old in _stored_fingerprints(used_urls))

def reserve_candidate(candidate:Dict[str,Any],used_urls:set)->None:
    url=candidate.get("url")
    if url:used_urls.add(url)
    fp=candidate.get("_fingerprint")
    if fp:
        try:
            if isinstance(fp,str):a,d=fp.split(":",1); token=f"__imgfp__:{a}:{d}"
            else:token=f"__imgfp__:{int(fp[0]):016x}:{int(fp[1]):016x}"
            used_urls.add(token)
        except Exception:pass

async def choose_candidates(product:Dict[str,Any],strategy:Dict[str,Any],pin_index:int,used_urls:set,agent_mod:Any)->List[Dict[str,Any]]:
    """Canonical image-selection path shared by every publishing entrypoint."""
    raw=[]
    for u in product.get("images") or []:
        if u and u not in used_urls:raw.append({"url":u,"provider":"product_page","source":"product page","license":"product_page","original":True})
    queries=_search_queries(product,strategy)
    for query in queries:
        try:raw.extend(await agent_mod.search_composio_images(query,num=10))
        except Exception:pass
    if not COMPOSIO_API_KEY:raw.extend(await search_pexels(queries[0],agent_mod))
    valid=await validate_many(raw)
    for item in valid:item["score"]=score(item,product,strategy.get("key",""))
    valid.sort(key=lambda x:(x.get("score",0),x.get("provider")=="product_page",x.get("original",False)),reverse=True)
    selected=[];seen_fps=[]
    for item in valid[:MAX_CANDIDATES_PER_PIN]:
        if not item.get("url") or item["url"] in used_urls:continue
        fp=await image_fingerprint(item["url"])
        if fp:
            if any(_fingerprint_distance(fp,old)<=PERCEPTUAL_DUPLICATE_DISTANCE for old in _stored_fingerprints(used_urls)+seen_fps):continue
            item["_fingerprint"]=fp;seen_fps.append(fp)
        selected.append(item)
    return selected

async def choose_best_image(product:Dict[str,Any],strategy:Dict[str,Any],pin_index:int,used_urls:set,agent_mod:Any)->Optional[Dict[str,Any]]:
    candidates=await choose_candidates(product,strategy,pin_index,used_urls,agent_mod)
    if not candidates:return None
    best=candidates[0];reserve_candidate(best,used_urls);return best
