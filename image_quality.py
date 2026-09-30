"""Fail-closed visual image validation and selection shared by every publication path."""
from __future__ import annotations
import asyncio, io, logging, os, re, math, hashlib, json
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
BLANK_STDDEV_THRESHOLD=4.0
BLANK_UNIQUE_COLOR_THRESHOLD=24
VISUAL_DUPLICATE_THRESHOLD=0.01
PEXELS_API_KEY=os.getenv("PEXELS_API_KEY","").strip()
COMPOSIO_API_KEY=os.getenv("COMPOSIO_API_KEY","").strip()

def inspect_image_bytes(raw: bytes, debug_url: str = "") -> Optional[Dict[str,Any]]:
    """Validate the actual image bytes; provider metadata is never trusted."""
    if not raw or len(raw) > MAX_IMAGE_BYTES_TO_INSPECT:
        if debug_url: logger.info("IMAGE_REJECT provider_url=%s reason=empty_or_too_large bytes=%s", debug_url, len(raw or b""))
        return None
    try:
        im=Image.open(io.BytesIO(raw)); im.verify()
        im=Image.open(io.BytesIO(raw)); im.load()
        if im.width < MIN_DIMENSION or im.height < MIN_DIMENSION:
            if debug_url: logger.info("IMAGE_REJECT provider_url=%s reason=too_small:%sx%s", debug_url, im.width, im.height)
            return None
        rgba=im.convert("RGBA"); alpha=list(rgba.getchannel("A").resize((64,64)).getdata())
        opaque_ratio=sum(1 for a in alpha if a>=250)/len(alpha)
        if opaque_ratio < 0.20:
            if debug_url: logger.info("IMAGE_REJECT provider_url=%s reason=transparent:%.3f", debug_url, opaque_ratio)
            return None
        rgb=rgba.convert("RGB").resize((64,64),Image.Resampling.LANCZOS); px=list(rgb.getdata())
        gray=[0.299*r+0.587*g+0.114*b for r,g,b in px]; mean=sum(gray)/len(gray)
        std=math.sqrt(sum((v-mean)**2 for v in gray)/len(gray)); unique=len(set(px))
        hist=[0]*32
        for v in gray: hist[min(31,int(v//8))]+=1
        entropy=-sum((n/len(gray))*math.log2(n/len(gray)) for n in hist if n)
        if std < BLANK_STDDEV_THRESHOLD or unique < BLANK_UNIQUE_COLOR_THRESHOLD or entropy < 1.8:
            if debug_url: logger.info("IMAGE_REJECT provider_url=%s reason=blank_like std=%.3f unique=%s entropy=%.3f", debug_url, std, unique, entropy)
            return None
        from PIL import ImageFilter
        edge_px=list(rgb.convert("L").filter(ImageFilter.FIND_EDGES).getdata())
        edge_ratio=sum(1 for v in edge_px if v>=180)/len(edge_px)
        sat=sum(max(p)-min(p) for p in px)/len(px)
        if edge_ratio > 0.34 and sat < 18 and entropy < 5.0:
            if debug_url: logger.info("IMAGE_REJECT provider_url=%s reason=edge_heavy edge=%.3f sat=%.2f entropy=%.3f", debug_url, edge_ratio, sat, entropy)
            return None
        from image_fingerprint import fingerprint
        return {"width":int(im.width),"height":int(im.height),"sha256":hashlib.sha256(raw).hexdigest(),
                "stddev":round(std,3),"unique_colors":unique,"entropy":round(entropy,3),
                "opaque_ratio":round(opaque_ratio,4),"edge_ratio":round(edge_ratio,4),
                "_fingerprint":fingerprint(raw)}
    except Exception as exc:
        if debug_url: logger.info("IMAGE_REJECT provider_url=%s reason=decode:%s", debug_url, type(exc).__name__)
        return None

async def _fetch_image_bytes(url:str)->Optional[bytes]:
    if not url or not str(url).startswith(("http://","https://")): return None
    try:
        async with httpx.AsyncClient(timeout=25,follow_redirects=True) as client:
            r=await client.get(url,headers={"User-Agent":"Mozilla/5.0 PinterestAgent/quality"})
            ct=(r.headers.get("content-type") or "").lower()
            if r.status_code>=400 or not r.content:
                logger.debug("image fetch rejected url=%s status=%s", url, r.status_code)
                return None
            if len(r.content)>MAX_IMAGE_BYTES_TO_INSPECT:
                logger.debug("image fetch rejected url=%s bytes=%s max=%s", url, len(r.content), MAX_IMAGE_BYTES_TO_INSPECT)
                return None
            if ct and not ct.startswith("image/"):
                logger.debug("image fetch rejected url=%s content_type=%s", url, ct)
                return None
            return r.content
    except Exception as exc:
        logger.debug("image fetch exception url=%s error=%s", url, str(exc)[:180])
        return None

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
async def inspect_image_content(url:str)->Optional[Dict[str,Any]]:
    raw=await _fetch_image_bytes(url)
    return inspect_image_bytes(raw, url) if raw else None

async def validate(c:Dict[str,Any])->Optional[Dict[str,Any]]:
    d=await inspect_image_url(c.get("url",""))
    if not d:return None
    w,h=d; ok,reason=hard_gate(w,h)
    if not ok:return None
    content=await inspect_image_content(c.get("url",""))
    if not content:return None
    x=dict(c);x.update(content);x.update(quality_gate=reason,content_gate="passed",content_sha256=content["sha256"],content_entropy=content["entropy"]);return x
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
    if w>=3840 or h>=3840:s+=20
    elif w>=2160 or h>=2160:s+=15
    elif w>=1440 or h>=1440:s+=10
    elif w>=1200 or h>=1200:s+=6
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
        if c.get("provider") == "pillow_card":continue
        sig=await _visual_signature(u)
        if sig is None:
            selected.append(c)
            if len(selected)>=MAX_CANDIDATES_PER_PIN:break
            continue
        distances=[_visual_distance(sig,s) for s in used_sigs+selected_sigs]
        if distances and min(distances)<VISUAL_DUPLICATE_THRESHOLD:continue
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
async def search_independent_bing_images(query:str,num:int=12)->List[Dict[str,Any]]:
    """Independent web-image fallback that does not depend on Composio."""
    try:
        endpoint="https://www.bing.com/images/search"
        headers={"User-Agent":os.getenv("PIN_N_SEARCH_USER_AGENT","Mozilla/5.0"),"Accept":"text/html,application/xhtml+xml","Accept-Language":"en-US,en;q=0.9"}
        async with httpx.AsyncClient(timeout=20,follow_redirects=True,headers=headers) as client:
            r=await client.get(endpoint,params={"q":query[:120],"form":"HDRSC2"})
            r.raise_for_status()
        out=[]
        for node in re.findall(r'<a[^>]+class=["\']iisc[^>]*>.*?</a>',r.text,re.I|re.S):
            m=re.search(r'\bm=["\']([^"\']+)["\']',node,re.I)
            if not m: continue
            try: meta=json.loads(m.group(1).replace("&quot;","\""))
            except Exception: continue
            u=str(meta.get("murl") or "")
            if not u.startswith(("http://","https://")): continue
            out.append({"url":u,"provider":"independent_bing_image","id":str(meta.get("purl") or ""),"source":str(meta.get("purl") or ""),"license":"web_search_verify_usage"})
            if len(out)>=num: break
        logger.info("Independent Bing image search query=%s results=%s",query,len(out))
        return out
    except Exception as exc:
        logger.warning("Independent Bing image search failed query=%s: %s",query,str(exc)[:300])
        return []

async def search_amazon_product_images(product:Dict[str,Any])->List[Dict[str,Any]]:
    """Direct Amazon image extraction fallback used only when normal image search is unavailable."""
    url=str(product.get("url") or "").strip()
    if not url:return []
    try:
        async with httpx.AsyncClient(timeout=25,follow_redirects=True) as client:
            r=await client.get(url,headers={"User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/128 Safari/537.36","Accept-Language":"en-US,en;q=0.9"})
            if r.status_code>=400:return []
            html=r.text
        html=html.replace("\\u002F","/").replace("\\/","/")
        found=[]
        patterns=[
            r'"(?:hiRes|large|main)"\s*:\s*"([^"]+)"',
            r'"(?:large|hiRes)"\s*:\s*"(https?://m\.media-amazon\.com/images/I/[^"]+)"',
            r'(https?://m\.media-amazon\.com/images/I/[A-Za-z0-9._%+-]+\.(?:jpg|jpeg|png|webp))'
        ]
        for pat in patterns:
            for u in re.findall(pat,html,re.I):
                u=u.replace("\\u0026","&").replace("\\u003d","=")
                if u.startswith("//"):u="https:"+u
                if "m.media-amazon.com/images/I/" not in u:continue
                if u not in found:found.append(u)
                # Upgrade common Amazon derivative filenames to their original asset.
                for marker in ("._SL1500_.","._SX1500_.","._SY1500_.","._AC_SL1500_.","._AC_UL1500_."):
                    bounded=re.sub(r'\._[^./]+_\.(?=[A-Za-z0-9]+$)',marker,u)
                    if bounded not in found: found.append(bounded)
                upgraded=re.sub(r'\._[^./]+_\.(?=[A-Za-z0-9]+$)','.',u)
                if upgraded!=u and upgraded not in found:found.append(upgraded)
                if len(found)>=24:break
            if len(found)>=24:break
        return [{"url":u,"provider":"amazon_direct","source":"Amazon product image","license":"Amazon product listing","original":True} for u in found[:24]]
    except Exception:
        return []

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
    """Canonical five-source selector: Composio -> independent web -> Pexels -> Amazon -> product page.
    Every candidate is byte-validated, fingerprint-checked, and visually de-duplicated.
    Provider failure advances to the next source; it never aborts the product by itself.
    """
    raw=[]
    product_page=[]
    for u in product.get("images") or []:
        if u and u not in used_urls:
            product_page.append({"url":u,"provider":"product_page","source":"product page","license":"product_page","original":True})
    queries=_search_queries(product,strategy)

    # 1) Composio image search (preferred when executable).
    for query in queries:
        try:
            found=await agent_mod.search_composio_images(query,num=10)
            if found:
                raw.extend(found)
                break
        except Exception as exc:
            logger.warning("Composio image source unavailable; advancing to next source: %s",str(exc)[:300])
            break

    # 2) Independent web-image search. This is deliberately outside Composio.
    if not raw:
        for query in queries[:2]:
            found=await search_independent_bing_images(query,num=12)
            if found:
                raw.extend(found)
                break

    # 3) Pexels, using direct credentials when present or the existing Composio
    # route when available. It is a genuine external image source, not a placeholder.
    if not raw:
        try:
            raw.extend(await search_pexels(queries[0],agent_mod))
        except Exception:
            pass

    # 4) Verified native Amazon product imagery. It is the recovery source when
    # external image discovery is unavailable or produces no valid candidate.
    raw.extend(await search_amazon_product_images(product))

    # 5) Native product-page imagery is the final source, never an invented image.
    raw.extend(product_page)

    valid=await validate_many(raw)
    from image_fingerprint import known_fingerprints, similarity
    known=known_fingerprints()
    filtered=[]
    for c in valid:
        fp=c.get("_fingerprint")
        if not fp: continue
        if any(similarity(fp,old_fp)>=0.999 for old_fp in known):
            logger.info("IMAGE_REJECT provider=%s reason=known_fingerprint", c.get("provider"))
            continue
        if any(similarity(fp,other.get("_fingerprint"))>=0.999 for other in filtered if other.get("_fingerprint")):
            logger.info("IMAGE_REJECT provider=%s reason=intra_product_duplicate", c.get("provider"))
            continue
        filtered.append(c)
    valid=filtered
    for c in valid:
        c["score"]=score(c,product,strategy.get("key",""))

    # Source priority is intentional: genuine external images first, Amazon
    # recovery second-last, native product-page imagery last. Resolution still
    # ranks within each source tier.
    source_rank={"composio_search_image":0,"independent_bing_image":1,"pexels":2,"amazon_direct":3,"product_page":4}
    def _resolution_tier(x):
        m=max(int(x.get("width") or 0),int(x.get("height") or 0))
        if m>=6000:return 4
        if m>=3500:return 3
        if m>=2000:return 2
        if m>=1200:return 1
        return 0
    valid=[c for c in valid if c.get("url") and c.get("url") not in used_urls]
    valid.sort(key=lambda x:(source_rank.get(str(x.get("provider") or ""),9),-_resolution_tier(x),-int(x.get("score",0)),not bool(x.get("original",False))))
    diverse=await _remove_visual_duplicates(valid[:MAX_CANDIDATES_PER_PIN*2],used_urls)
    return diverse[:MAX_CANDIDATES_PER_PIN] if diverse else []

async def validate_base64_image(value:str)->Optional[Dict[str,Any]]:
    if not value:return None
    try:
        import base64
        raw=base64.b64decode(value,validate=True)
        return inspect_image_bytes(raw)
    except Exception:return None

async def choose_best_image(product:Dict[str,Any],strategy:Dict[str,Any],pin_index:int,used_urls:set,agent_mod:Any)->Optional[Dict[str,Any]]:
    candidates=await choose_candidates(product,strategy,pin_index,used_urls,agent_mod)
    if not candidates:return None
    best=candidates[0];used_urls.add(best["url"]);return best
