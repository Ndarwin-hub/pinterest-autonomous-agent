"""Fail-closed visual image validation and selection shared by every publication path."""
from __future__ import annotations
import asyncio, io, logging, os, re, math, hashlib, json, base64
from typing import Any, Dict, List, Optional, Tuple
import httpx
from PIL import Image, ImageFile
from advanced_image_intelligence import filter_obvious_non_product, advanced_tiebreak_key
logger=logging.getLogger("pinterest-agent.image-quality")
MIN_DIMENSION=500
PREFERRED_MIN_DIMENSION=1200
PREFERRED_PORTRAIT_MIN_HEIGHT=1200
MAX_ASPECT=2.0
MIN_SCORE=85
MAX_CANDIDATES_PER_PIN=12
MAX_IMAGE_BYTES_TO_INSPECT=12*1024*1024
MIN_IMAGE_BYTES_TO_INSPECT=512
BLANK_STDDEV_THRESHOLD=4.0
BLANK_UNIQUE_COLOR_THRESHOLD=24
VISUAL_DUPLICATE_THRESHOLD=0.01
PEXELS_API_KEY=os.getenv("PEXELS_API_KEY","").strip()
COMPOSIO_API_KEY=os.getenv("COMPOSIO_API_KEY","").strip()

def inspect_image_bytes(raw: bytes, debug_url: str = "") -> Optional[Dict[str,Any]]:
    """Hard technical image-integrity validation; aesthetic quality remains separate."""
    if not raw or len(raw) < MIN_IMAGE_BYTES_TO_INSPECT or len(raw) > MAX_IMAGE_BYTES_TO_INSPECT:
        if debug_url: logger.info("IMAGE_REJECT provider_url=%s reason=empty_too_small_or_too_large bytes=%s", debug_url, len(raw or b""))
        return None
    try:
        im=Image.open(io.BytesIO(raw)); im.verify()
        im=Image.open(io.BytesIO(raw)); im.load()
        if im.width < MIN_DIMENSION or im.height < MIN_DIMENSION:
            if debug_url: logger.info("IMAGE_REJECT provider_url=%s reason=too_small:%sx%s", debug_url, im.width, im.height)
            return None
        ratio=max(im.width,im.height)/max(1,min(im.width,im.height))
        if ratio > MAX_ASPECT:
            if debug_url: logger.info("IMAGE_REJECT provider_url=%s reason=bad_aspect:%sx%s", debug_url, im.width, im.height)
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
                "quality_advisory":{"too_small":False,"bad_aspect":False,"blank_like":False,"transparent":False,"edge_heavy":False},
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
    url=str(c.get("url") or "")
    content=await inspect_image_content(url)
    if not content:
        logger.info("IMAGE_REJECT provider=%s reason=content_validation_failed url=%s",c.get("provider"),url[:220])
        return None
    x=dict(c); x.update(content)
    x.update(quality_gate="hard_integrity_passed",content_gate="passed",content_sha256=content["sha256"],content_entropy=content["entropy"])
    return x


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
    """Prefer visually distinct images, but never block a product when only URL-distinct usable images remain."""
    if not candidates: return []
    used_sigs=[]
    for u in list(used_urls)[:4]:
        sig=await _visual_signature(u)
        if sig: used_sigs.append(sig)
    selected=[]; selected_sigs=[]; deferred=[]
    for c in candidates:
        u=c.get("url")
        if not u or u in used_urls: continue
        if c.get("provider")=="pillow_card": continue
        sig=await _visual_signature(u)
        if sig is None:
            selected.append(c)
            if len(selected)>=MAX_CANDIDATES_PER_PIN: break
            continue
        distances=[_visual_distance(sig,x) for x in used_sigs+selected_sigs]
        c["visual_distance"]=round(min(distances),4) if distances else 1.0
        if distances and min(distances)<VISUAL_DUPLICATE_THRESHOLD:
            deferred.append(c)
            continue
        selected.append(c); selected_sigs.append(sig)
        if len(selected)>=MAX_CANDIDATES_PER_PIN: break
    if len(selected)<MAX_CANDIDATES_PER_PIN:
        for c in deferred:
            if c not in selected:
                c["duplicate_advisory"]=True
                selected.append(c)
                if len(selected)>=MAX_CANDIDATES_PER_PIN: break
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
        # Bing has changed its image-result markup over time. Parse the embedded
        # murl metadata directly instead of relying on the old iisc anchor class.
        for m in re.finditer(r'\bm=["\']([^"\']+)["\']',r.text,re.I):
            try:
                raw=m.group(1).replace("&quot;","\"").replace("&#34;","\"").replace("\\\"","\"")
                meta=json.loads(raw)
            except Exception:
                continue
            u=str(meta.get("murl") or "")
            if not u.startswith(("http://","https://")): continue
            out.append({"url":u,"provider":"independent_bing_image","id":str(meta.get("purl") or ""),"source":str(meta.get("purl") or ""),"license":"web_search_verify_usage"})
            if len(out)>=num: break
        logger.info("Independent Bing image search query=%s results=%s",query,len(out))
        return out
    except Exception as exc:
        logger.warning("Independent Bing image search failed query=%s: %s",query,str(exc)[:300])
        return []

async def search_amazon_asin_cdn_images(product:Dict[str,Any])->List[Dict[str,Any]]:
    """Construct exact Amazon CDN gallery candidates from the verified ASIN."""
    url=str(product.get("url") or "")
    asin=str(product.get("asin") or "").upper().strip()
    if not asin:
        m=re.search(r"(?:/dp/|/gp/product/)([A-Z0-9]{10})",url,re.I)
        asin=m.group(1).upper() if m else ""
    if not asin: return []
    out=[]
    for shot in range(1,10):
        n=f"{shot:02d}"
        for host in ("https://images-na.ssl-images-amazon.com/images/P","https://m.media-amazon.com/images/P"):
            for suffix in (".LZZZZZZZ.jpg",".jpg","._SL1500_.jpg"):
                u=f"{host}/{asin}.{n}{suffix}"
                if u not in [x["url"] for x in out]:
                    out.append({"url":u,"provider":"amazon_asin_cdn","source":"Amazon ASIN CDN gallery","license":"Amazon product listing","original":True})
    logger.info("Amazon ASIN CDN gallery asin=%s candidates=%s",asin,len(out))
    return out

async def search_amazon_api_images(product:Dict[str,Any])->List[Dict[str,Any]]:
    """Use Amazon Creators API primary + variant large images when credentials are available."""
    try:
        from amazon_client import AmazonCreatorsClient, amazon_credentials_present, extract_asin_from_item
        if not amazon_credentials_present():
            return []
        url=str(product.get("url") or "")
        asin=str(product.get("asin") or "").upper().strip()
        if not asin:
            m=re.search(r"(?:/dp/|/gp/product/)([A-Z0-9]{10})",url,re.I)
            asin=m.group(1).upper() if m else ""
        if not asin:
            return []
        client=AmazonCreatorsClient()
        items=await client.get_items([asin])
        out=[]
        def walk(node):
            if isinstance(node,dict):
                for k,v in node.items():
                    if k.lower()=="url" and isinstance(v,str) and "m.media-amazon.com/images/I/" in v.lower():
                        low=v.lower()
                        if not any(x in low for x in ("amazon_logo","amazon-logo","social_share","prime_logo","prime-logo")):
                            if v not in [x["url"] for x in out]:
                                out.append({"url":v,"provider":"amazon_creators_api","source":"Amazon Creators API images","license":"Amazon product listing","original":True})
                    else: walk(v)
            elif isinstance(node,list):
                for v in node: walk(v)
        for item in items:
            item_asin=extract_asin_from_item(item) if isinstance(item,dict) else None
            if item_asin and item_asin!=asin: continue
            walk(item.get("images") if isinstance(item,dict) else {})
        logger.info("Amazon Creators API gallery asin=%s candidates=%s",asin,len(out))
        return out[:24]
    except Exception as exc:
        logger.info("Amazon Creators API image tier unavailable: %s",str(exc)[:220])
        return []

async def search_amazon_product_images(product:Dict[str,Any])->List[Dict[str,Any]]:
    """Extract Amazon's exact product-gallery assets; no web-search substitution at this tier."""
    url=str(product.get("url") or "").strip()
    if not url: return []
    try:
        async with httpx.AsyncClient(timeout=25,follow_redirects=True) as client:
            r=await client.get(url,headers={"User-Agent":"Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/140 Mobile Safari/537.36","Accept-Language":"en-US,en;q=0.9"})
            if r.status_code>=400: return []
            html=r.text
        html=(html.replace("\\u002F","/").replace("\\/","/").replace("\\u003A",":")
              .replace("\\u003D","=").replace("&quot;",'"').replace("&amp;","&"))
        found=[]
        def add(u):
            u=str(u or "").strip().replace("\\u0026","&").replace("\\u003d","=")
            if u.startswith("//"): u="https:"+u
            if not u.startswith(("http://","https://")): return
            low=u.lower()
            if "m.media-amazon.com/images/i/" not in low: return
            if any(x in low for x in ("amazon_logo","amazon-logo","social_share","prime_logo","prime-logo","auiclients")): return
            if re.search(r"\.(?:js|css)(?:[?&]|$)", low): return
            if not re.search(r"\.(?:jpg|jpeg|png|webp|gif)(?:[?&._-]|$)", low) and "images/i/" in low: return
            u=re.split(r'["\'<>\\s]',u,1)[0]
            if u and u not in found: found.append(u)
        # Exact Amazon gallery JSON/HTML patterns. Keep this layer narrow so CSS/JS assets cannot enter.
        for u in re.findall(r'["\'](?:large|hiRes|mainUrl|data-old-hires)["\']\s*:\s*["\']([^"\']+)', html, re.I):
            add(u)
        for u in re.findall(r'"(https://m\.media-amazon\.com/images/I/[^"<>\\s]+?\.(?:jpg|jpeg|png|webp|gif))"', html, re.I):
            add(u)
        for u in re.findall(r'https://m\.media-amazon\.com/images/I/[^"<>\\s]+?\.(?:jpg|jpeg|png|webp|gif)', html, re.I):
            add(u)
        for u in re.findall(r'//m\.media-amazon\.com/images/I/[^"<>\\s]+?\.(?:jpg|jpeg|png|webp|gif)', html, re.I):
            add(u)
        upgraded=[]
        for u in found:
            v=re.sub(r'\._(?:SL|SX|SY|AC_SL|AC_UL)\d+_\.',".",u)
            if v not in upgraded: upgraded.append(v)
        logger.info("Amazon gallery extraction asin=%s candidates=%s",str(product.get("asin") or (re.search(r"(?:/dp/|/gp/product/)([A-Z0-9]{10})",url,re.I).group(1) if re.search(r"(?:/dp/|/gp/product/)([A-Z0-9]{10})",url,re.I) else "") or "")[:32],len(upgraded))
        return [{"url":u,"provider":"amazon_direct","source":"Amazon product gallery","license":"Amazon product listing","original":True} for u in upgraded[:24]]
    except Exception as exc:
        logger.warning("Amazon gallery extraction failed: %s",str(exc)[:180])
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
async def _alternative_identity_ok(c:Dict[str,Any],product:Dict[str,Any])->bool:
    """Require exact-product evidence before accepting a non-Amazon fallback image."""
    src=str(c.get("source") or "")
    candidate_url=str(c.get("url") or "")
    supplied=str(c.get("identity_evidence") or "")
    evidence=(supplied+" "+src+" "+candidate_url).lower()
    asin=str(product.get("asin") or "").lower()
    name=str(product.get("name") or "").lower()
    brand=str(product.get("brand") or "").lower().strip()
    stop={"with","from","this","that","product","official","amazon","new","pack","size","color","the","for","and"}
    tokens=list(dict.fromkeys(t for t in re.findall(r"[a-z0-9][a-z0-9\-]{3,}",name) if t not in stop))
    if asin and asin in evidence: return True
    if brand and brand in evidence and any(t in evidence for t in tokens[:8]): return True
    if sum(1 for t in tokens[:10] if t in evidence)>=2: return True
    # Official-brand image URLs are strong identity evidence when the URL itself
    # contains multiple exact product tokens. This does not bypass legacy quality gates.
    try:
        host=urlparse(candidate_url).netloc.lower()
        brand_token=re.sub(r"[^a-z0-9]","",brand)
        host_token=re.sub(r"[^a-z0-9]","",host)
        if brand_token and brand_token in host_token and sum(1 for t in tokens[:12] if t in evidence)>=2:
            return True
    except Exception: pass
    if src.startswith(("http://","https://")) and not supplied:
        try:
            async with httpx.AsyncClient(timeout=10,follow_redirects=True) as client:
                rr=await client.get(src,headers={"User-Agent":"Mozilla/5.0 PinterestAgent/identity"})
                if rr.status_code<400 and rr.text:
                    page=re.sub(r"<[^>]+>"," ",rr.text).lower()
                    if asin and asin in page: return True
                    if brand and brand in page and any(t in page for t in tokens[:8]): return True
                    if sum(1 for t in tokens[:10] if t in page)>=2: return True
        except Exception: pass
    return False


async def search_verified_web_product_page_images(product:Dict[str,Any],num:int=12)->List[Dict[str,Any]]:
    """Find exact-product retailer/manufacturer pages and extract product images."""
    name=str(product.get("name") or "").strip()
    asin=str(product.get("asin") or "").strip()
    brand=str(product.get("brand") or "").strip()
    if not name and not asin: return []
    queries=[]
    if name: queries.append(" ".join(x for x in [f'"{name}"',brand,"official product"] if x))
    if asin: queries.append(" ".join(x for x in [f'"{asin}"',f'"{name}"'] if x))
    try:
        async with httpx.AsyncClient(timeout=15,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0 (Linux; Android 11) AppleWebKit/537.36 Chrome/140 Mobile Safari/537.36"}) as client:
            links=[]
            for q in queries:
                r=await client.get("https://www.bing.com/search",params={"q":q,"form":"QBLH"})
                if r.status_code>=400: continue
                html=r.text
                for u in re.findall(r"<h2[^>]*>\\s*<a[^>]+href=[\"']([^\"']+)",html,re.I):
                    u=unquote(u)
                    if "bing.com/ck/a" in u.lower():
                        mm=re.search(r"[?&]u=a1([^&]+)",u,re.I)
                        if mm:
                            try:
                                decoded=base64.urlsafe_b64decode(mm.group(1)+"===" ).decode("utf-8","ignore")
                                if decoded.startswith("http"): u=decoded
                            except Exception: pass
                    if u.startswith("http") and "bing.com" not in u.lower() and u not in links:
                        links.append(u)
            if not links:
                for q in queries:
                    try:
                        dr=await client.get("https://html.duckduckgo.com/html/",params={"q":q},headers={"Referer":"https://duckduckgo.com/"})
                        if dr.status_code>=400: continue
                        for u in re.findall(r"<a[^>]+class=[\"'][^\"']*result__a[^\"']*[\"'][^>]+href=[\"']([^\"']+)",dr.text,re.I):
                            u=unquote(u)
                            if u.startswith("http") and "duckduckgo.com" not in u.lower() and u not in links: links.append(u)
                        for u in re.findall(r"nuddg=([^&\"']+)",dr.text,re.I):
                            u=unquote(u)
                            if u.startswith("http") and u not in links: links.append(u)
                    except Exception: pass
                    if links: break
            out=[]
            tokens=[t for t in re.findall(r"[a-z0-9][a-z0-9\\-]{3,}",name.lower()) if t not in {"with","from","this","that","product","official","amazon","new","pack","size","color","the","for","and"}]
            for src in links[:12]:
                if any(x in src.lower() for x in ("pinterest.com","shutterstock.com","istockphoto.com","gettyimages.com")): continue
                try:
                    page=await client.get(src)
                    if page.status_code>=400 or not page.text: continue
                    page_html=page.text.replace("\\\\/","/").replace("\\\\u002F","/")
                    evidence=re.sub(r"<[^>]+>"," ",page_html).lower()+" "+src.lower()
                    identity=(bool(asin) and asin.lower() in evidence) or (bool(brand) and brand.lower() in evidence and sum(1 for t in tokens[:10] if t in evidence)>=1) or sum(1 for t in tokens[:10] if t in evidence)>=2
                    if not identity: continue
                    imgs=[]
                    imgs += re.findall(r"<meta[^>]+(?:property|name)=[\"'](?:og:image|twitter:image)[\"'][^>]+content=[\"']([^\"']+)",page_html,re.I)
                    imgs += re.findall(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"'](?:og:image|twitter:image)[\"']",page_html,re.I)
                    imgs += re.findall(r"\"image\"\\s*:\\s*\"(https?://[^\"\\]+)",page_html,re.I)
                    for u in imgs:
                        u=unquote(u).replace("\\\\/","/")
                        if not u.startswith("http") or any(x.get("url")==u for x in out): continue
                        out.append({"url":u,"provider":"verified_web_product_page","source":src,"identity_evidence":re.sub(r"<[^>]+>"," ",page_html)[:200000],"license":"verified product page","original":True})
                        if len(out)>=num: return out
                except Exception: continue
            logger.info("Verified web product-page image search asin=%s pages=%s images=%s",asin[:20],len(links[:12]),len(out))
            return out
    except Exception as exc:
        logger.warning("Verified web product-page image search unavailable: %s",str(exc)[:220])
        return []

async def choose_candidates(product:Dict[str,Any],strategy:Dict[str,Any],pin_index:int,used_urls:set,agent_mod:Any)->List[Dict[str,Any]]:
    """Amazon-first exact-product selector; fall back only when Amazon cannot fill the tier."""
    target=4
    amazon_api=await search_amazon_api_images(product)
    amazon_cdn=await search_amazon_asin_cdn_images(product)
    amazon_page=await search_amazon_product_images(product)
    amazon=amazon_api+amazon_cdn+amazon_page
    amazon_valid=await validate_many([c for c in amazon if c.get("url") and c.get("url") not in used_urls])
    amazon_valid=[c for c in amazon_valid if c.get("provider") in {"amazon_creators_api","amazon_asin_cdn","amazon_direct","product_page"}]
    amazon_valid=filter_obvious_non_product(amazon_valid,product)
    logger.info("AMAZON_IMAGE_TIER asin=%s candidates=%s providers=%s",str(product.get("asin") or "")[:20],len(amazon_valid),sorted(set(str(c.get("provider") or "") for c in amazon_valid)))
    if len(amazon_valid)>=target:
        amazon_valid.sort(key=lambda x:(-score(x,product,str(strategy.get("key",""))),not bool(x.get("original",False))))
        chosen=await _remove_visual_duplicates(amazon_valid[:MAX_CANDIDATES_PER_PIN*2],used_urls)
        if len(chosen)>=target:
            logger.info("IMAGE_SOURCE_TIER product=%s tier=amazon_gallery valid=%s selected=%s",str(product.get("asin") or product.get("name",""))[:80],len(amazon_valid),target)
            return chosen[:target]
    raw=[]
    for u in product.get("images") or []:
        if u and u not in used_urls:
            raw.append({"url":u,"provider":"product_page","source":"exact product page","license":"product_page","original":True})
    if len(amazon_valid)<target:
        queries=_search_queries(product,strategy)
        for query in queries[:2]:
            try:
                found=await agent_mod.search_composio_images(query,num=10)
                if found: raw.extend(found); break
            except Exception as exc:
                logger.warning("Composio image source unavailable; advancing: %s",str(exc)[:300])
        if not any(c.get("url") for c in raw if c.get("provider")=="composio_search_image"):
            for query in queries[:2]:
                found=await search_independent_bing_images(query,num=12)
                if found: raw.extend(found); break
        if not any(c.get("url") for c in raw if c.get("provider")=="independent_bing_image"):
            try: raw.extend(await search_pexels(queries[0],agent_mod))
            except Exception: pass
        # Add exact-product web-page evidence as the final trusted fallback.
        try: raw.extend(await search_verified_web_product_page_images(product,num=12))
        except Exception as exc: logger.warning("Verified web product-page fallback unavailable: %s",str(exc)[:220])
    validated=await validate_many([c for c in raw if c.get("url") and c.get("url") not in used_urls])
    validated=filter_obvious_non_product(validated,product)
    trusted=[]
    for c in validated:
        if c.get("provider")=="product_page" or await _alternative_identity_ok(c,product):
            if c.get("provider")!="product_page": c["identity_gate"]="passed"
            trusted.append(c)
        else:
            logger.info("IMAGE_REJECT provider=%s reason=product_identity_unverified url=%s",c.get("provider"),str(c.get("url"))[:220])
    trusted.extend(amazon_valid)
    if not trusted:
        logger.info("IMAGE_SOURCE_FAIL product=%s reason=no_exact_product_image_survived",str(product.get("asin") or product.get("name",""))[:80])
        return []
    for c in trusted: c["score"]=score(c,product,str(strategy.get("key","")))
    source_rank={"amazon_creators_api":0,"amazon_asin_cdn":1,"amazon_direct":2,"product_page":3,"composio_search_image":4,"verified_web_product_page":5,"independent_bing_image":6,"pexels":7}
    def _resolution_tier(x):
        m=max(int(x.get("width") or 0),int(x.get("height") or 0))
        return 4 if m>=6000 else 3 if m>=3500 else 2 if m>=2000 else 1 if m>=1200 else 0
    trusted.sort(key=lambda x:(source_rank.get(str(x.get("provider") or ""),9),-_resolution_tier(x),-int(x.get("score",0)),not bool(x.get("original",False)),-advanced_tiebreak_key(x)[0],-advanced_tiebreak_key(x)[1],-advanced_tiebreak_key(x)[2]))
    diverse=await _remove_visual_duplicates(trusted[:MAX_CANDIDATES_PER_PIN*3],used_urls)
    return diverse[:target] if len(diverse)>=target else diverse

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
