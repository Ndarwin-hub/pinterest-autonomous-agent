"""Image priority supervisor for Pinterest.

Priority contract:
1. Composio Image Search native 8K+ imagery.
2. Composio Image Search native 4K+ imagery.
3. Other executable genuine image providers (for example Pexels or configured
   Pixabay/Unsplash), ranked by verified resolution.
4. Existing executable AI image generation/editing path, when available.
5. Verified 4K local upscale/derivative of the best genuine external image.
6. Native product-page imagery, only when priorities 1-5 cannot satisfy.
7. No placeholder fallback; fail closed.
"""
from __future__ import annotations

import base64
import io
import os
from typing import Any, Dict, List, Set

from image_fingerprint import attach_fingerprint, diversity_bonus, is_near_duplicate, known_fingerprints, similarity

import httpx

TARGET_W, TARGET_H = 2160, 3840
NATIVE_8K_MIN = 6000
NATIVE_4K_MIN = 3500
MAX_DOWNLOAD = 18 * 1024 * 1024
COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "").strip()
COMPOSIO_ENTITY_ID = os.getenv("COMPOSIO_ENTITY_ID", "default").strip() or "default"

def _pixels(c): return int(c.get("width") or 0) * int(c.get("height") or 0)
def _native_tier(c):
    m=max(int(c.get("width") or 0),int(c.get("height") or 0))
    if m>=NATIVE_8K_MIN:return 4
    if m>=NATIVE_4K_MIN:return 3
    if m>=2000:return 2
    if m>=1000:return 1
    return 0
def _provider_rank(provider):
    return {"composio_search_image":5,"pexels":4,"pixabay":3,"unsplash":3,"product_page":1}.get(provider or "",0)

async def _probe_dimensions(url):
    try:
        async with httpx.AsyncClient(timeout=25.0,follow_redirects=True) as client:
            r=await client.get(url,headers={"User-Agent":"Mozilla/5.0"})
            if r.status_code>=400 or not r.content or len(r.content)>MAX_DOWNLOAD:return 0,0,None
            from PIL import Image
            data=r.content
            with Image.open(io.BytesIO(data)) as im:return int(im.width),int(im.height),data
    except Exception:return 0,0,None

async def _composio_image_search(query,num=20):
    if not COMPOSIO_API_KEY:return []
    endpoint="https://backend.composio.dev/api/v3.1/tools/execute/COMPOSIO_SEARCH_IMAGE"
    payload={"user_id":COMPOSIO_ENTITY_ID,"arguments":{"query":query,"num":min(max(int(num),1),100)},"version":"latest","dangerously_skip_version_check":True}
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r=await client.post(endpoint,headers={"x-api-key":COMPOSIO_API_KEY,"Content-Type":"application/json"},json=payload)
            if r.status_code>=400:return []
            data=r.json() or {}
            if isinstance(data,dict) and isinstance(data.get("data"),dict):data=data["data"]
            rows=data.get("images_results") if isinstance(data,dict) else None
            if not isinstance(rows,list):return []
            out=[]
            for x in rows:
                if not isinstance(x,dict):continue
                url=x.get("original") or x.get("original_url") or x.get("link")
                if url and str(url).startswith("http"):
                    out.append({"url":str(url),"provider":"composio_search_image","id":str(x.get("position") or x.get("thumbnail") or ""), "source":x.get("source") or x.get("title"),"license":x.get("license") or "verify_before_commercial_use"})
            return out
    except Exception:return []

async def _configured_stock(query):
    out=[]
    async with httpx.AsyncClient(timeout=20) as client:
        key=os.getenv("PIXABAY_API_KEY","").strip()
        if key:
            try:
                r=await client.get("https://pixabay.com/api/",params={"key":key,"q":query,"image_type":"photo","orientation":"vertical","per_page":12})
                for x in (r.json() or {}).get("hits",[]) if r.status_code<400 else []:
                    u=x.get("largeImageURL") or x.get("webformatURL")
                    if u:out.append({"url":u,"provider":"pixabay","id":str(x.get("id") or ""),"license":"Pixabay License"})
            except Exception:pass
        key=os.getenv("UNSPLASH_ACCESS_KEY","").strip()
        if key:
            try:
                r=await client.get("https://api.unsplash.com/search/photos",params={"client_id":key,"query":query,"orientation":"portrait","per_page":12})
                for x in (r.json() or {}).get("results",[]) if r.status_code<400 else []:
                    u=(x.get("urls") or {}).get("full") or (x.get("urls") or {}).get("raw")
                    if u:out.append({"url":u,"provider":"unsplash","id":str(x.get("id") or ""),"license":"Unsplash License"})
            except Exception:pass
    return out

async def _verify_candidates(candidates,used_urls):
    verified=[];seen=set(used_urls)
    for c in candidates:
        url=c.get("url")
        if not url or url in seen:continue
        w,h,data=await _probe_dimensions(url)
        if not w or not h:continue
        item=dict(c);item["width"],item["height"],item["_data"]=w,h,data
        item=attach_fingerprint(item,data);verified.append(item);seen.add(url)
    return verified

def _best(items,minimum_tier=0,previous=None):
    eligible=[x for x in items if _native_tier(x)>=minimum_tier]
    previous=previous or []
    fresh=[x for x in eligible if not is_near_duplicate(x,previous)]
    if not fresh:return None
    return max(fresh,key=lambda x:(diversity_bonus(x,previous),_native_tier(x),_pixels(x),_provider_rank(x.get("provider",""))))

def _to_4k(data):
    try:
        from PIL import Image
        im=Image.open(io.BytesIO(data)).convert("RGB")
        scale=min(TARGET_W/im.width,TARGET_H/im.height);nw=max(1,int(im.width*scale));nh=max(1,int(im.height*scale))
        im=im.resize((nw,nh),Image.Resampling.LANCZOS);canvas=Image.new("RGB",(TARGET_W,TARGET_H),(255,255,255));canvas.paste(im,((TARGET_W-nw)//2,(TARGET_H-nh)//2))
        out=io.BytesIO();canvas.save(out,format="JPEG",quality=96,subsampling=0,optimize=True);return base64.b64encode(out.getvalue()).decode("ascii")
    except Exception:return None

async def _try_existing_ai(agent,product,strategy):
    for name in ("generate_pin_image","generate_ai_image","openai_generate_image"):
        fn=getattr(agent,name,None)
        if callable(fn):
            try:
                result=await fn(product,strategy)
                if isinstance(result,dict) and result.get("value"):return result
            except Exception:pass
    return None

async def get_best_pin_image(product,strategy,pin_index,job_store,job_id,used_urls,agent):
    history=getattr(agent,"_pin_image_fingerprints",None)
    if history is None:history={};setattr(agent,"_pin_image_fingerprints",history)
    previous=history.setdefault(job_id,[]);previous_all=list(previous)+known_fingerprints()
    job_store.update(job_id,progress=f"Pin {pin_index}/4: Composio 8K/4K image priority ({strategy['name']})")
    name=product.get("name") or "product";asin=str(product.get("asin") or "").strip()
    query=f"{name} {strategy['focus']}"[:160]
    composio=[]
    # ASIN-aware discovery is a recovery path for Amazon anti-bot/weak-title cases.
    search_queries=[]
    if asin:
        search_queries += [f"{asin} Amazon product image",f"{asin} product photo",f"{asin} Amazon"]
    search_queries += [name,f"{name} product",query,f"{name} front view",f"{name} side view",f"{name} lifestyle",f"{name} detail"]
    for q in search_queries:
        composio.extend(await _composio_image_search(q,num=30))
        verified_now=await _verify_candidates(composio,used_urls)
        if any(_native_tier(x)>=4 for x in verified_now):break
    composio_verified=await _verify_candidates(composio,used_urls)
    best_8k=_best(composio_verified,4,previous_all)
    if best_8k:
        used_urls.add(best_8k["url"])
        if best_8k.get("_fingerprint"):previous.append(best_8k["_fingerprint"])
        return {"mode":"url","value":best_8k["url"],"provider":"composio_search_image","id":best_8k.get("id"),"score":100,"license":best_8k.get("license"),"resolution_tier":"8K+"}
    best_4k=_best(composio_verified,3,previous_all)
    if best_4k:
        used_urls.add(best_4k["url"])
        if best_4k.get("_fingerprint"):previous.append(best_4k["_fingerprint"])
        return {"mode":"url","value":best_4k["url"],"provider":"composio_search_image","id":best_4k.get("id"),"score":98,"license":best_4k.get("license"),"resolution_tier":"4K+"}
    other=[]
    try:
        for f in await agent.search_pexels(query):
            if f.get("url"):other.append(f)
    except Exception:pass
    other.extend(await _configured_stock(query));other_verified=await _verify_candidates(other,used_urls)
    best_other=_best(other_verified,3,previous_all) or _best(other_verified,2,previous_all)
    if best_other:
        used_urls.add(best_other["url"])
        if best_other.get("_fingerprint"):previous.append(best_other["_fingerprint"])
        return {"mode":"url","value":best_other["url"],"provider":best_other.get("provider"),"id":best_other.get("id"),"score":94 if _native_tier(best_other)>=3 else 88,"license":best_other.get("license"),"resolution_tier":"4K+" if _native_tier(best_other)>=3 else "high-res"}
    ai=await _try_existing_ai(agent,product,strategy)
    if ai:
        try:
            from image_quality import validate_base64_image,inspect_image_content
            checked=await validate_base64_image(str(ai.get("value") or "")) if ai.get("mode")=="base64" else await inspect_image_content(str(ai.get("value") or ""))
            if checked:
                ai=dict(ai);ai.update(checked,content_gate="passed",image_bytes_validated=True)
                if ai.get("_fingerprint") and any(similarity(ai["_fingerprint"],old)>=0.93 for old in previous_all):ai=None
            else:ai=None
        except Exception:ai=None
    if ai:return ai
    external_any=composio_verified+other_verified
    if external_any:
        source=max(external_any,key=lambda x:(_pixels(x),_provider_rank(x.get("provider",""))));data=source.get("_data")
        if data:
            b64=_to_4k(data)
            if b64:
                used_urls.add(source["url"])
                if source.get("_fingerprint"):previous.append(source["_fingerprint"])
                return {"mode":"base64","value":b64,"provider":f"{source.get('provider')}_4k_upscale","id":source.get("id"),"score":86,"license":source.get("license"),"resolution_tier":"4K_upscaled"}
    native=[]
    for url in product.get("images") or []:
        if url in used_urls:continue
        w,h,data=await _probe_dimensions(url)
        if w and h:native.append({"url":url,"provider":"product_page","width":w,"height":h,"_data":data,"license":"product_page"})
    native_best=_best(native,0,previous_all)
    if native_best:
        used_urls.add(native_best["url"])
        if native_best.get("_fingerprint"):previous.append(native_best["_fingerprint"])
        tier=_native_tier(native_best)
        return {"mode":"url","value":native_best["url"],"provider":"product_page","score":80,"license":"product_page","resolution_tier":"native" if tier<3 else ("8K+" if tier==4 else "4K+")}
    raise RuntimeError(f"No trustworthy image source survived for Pin {pin_index}.")

def install(agent):
    if getattr(agent,"_image_priority_installed",False):return
    original=agent.get_best_pin_image
    async def wrapped(product,strategy,pin_index,job_store,job_id,used_urls):
        try:return await get_best_pin_image(product,strategy,pin_index,job_store,job_id,used_urls,agent)
        except Exception as exc:
            job_store.update(job_id,progress=f"Pin {pin_index}/4: diversity selector error: {type(exc).__name__}");raise
    agent.get_best_pin_image=wrapped;agent._image_priority_installed=True
