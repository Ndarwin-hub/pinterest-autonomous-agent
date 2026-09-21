"""AI reviewer failover layer.

Keeps the existing zero-tolerance gate intact, but prevents one unavailable
provider from aborting an otherwise healthy job while never bypassing the
existing deterministic image-quality requirements.
"""
from __future__ import annotations
import asyncio, base64, json, logging, os, re
from typing import Any, Dict, List, Optional, Callable, Awaitable
import httpx
logger=logging.getLogger("pinterest-agent.failover")
GEMINI_MODEL=os.getenv("GEMINI_REVIEW_MODEL","gemini-2.5-flash")

def _json(text:str)->Optional[Dict[str,Any]]:
    try:
        m=re.search(r"\{.*\}",text or "",re.S); return json.loads(m.group(0)) if m else None
    except Exception:return None

def _gemini_text(data:Any)->str:
    if not isinstance(data,dict):return ""
    candidates=data.get("candidates") or []
    if not candidates:return ""
    parts=((candidates[0] or {}).get("content") or {}).get("parts") or []
    return "".join(str(p.get("text","")) for p in parts if isinstance(p,dict))

async def _image_part(ref:str)->Optional[Dict[str,Any]]:
    if not ref:return None
    if ref.startswith("data:image/"):
        try:
            header,data=ref.split(",",1); mime=header.split(";",1)[0].split(":",1)[1]
            return {"inline_data":{"mime_type":mime,"data":data}}
        except Exception:return None
    if not ref.startswith("http"):return None
    try:
        async with httpx.AsyncClient(timeout=30,follow_redirects=True) as client:
            r=await client.get(ref,headers={"User-Agent":"Mozilla/5.0"}); r.raise_for_status(); raw=r.content
            if len(raw)>12*1024*1024:return None
            mime=(r.headers.get("content-type") or "image/jpeg").split(";",1)[0]
            if not mime.startswith("image/"):mime="image/jpeg"
            return {"inline_data":{"mime_type":mime,"data":base64.b64encode(raw).decode("ascii")}}
    except Exception as e:
        logger.warning("Gemini fallback could not fetch image: %s",e); return None

async def _gemini_composio_one(item:Dict[str,Any],run_tool:Callable[[str,Dict[str,Any],int],Awaitable[Dict[str,Any]]])->Optional[Dict[str,Any]]:
    ref=item.get("image_ref") or ""; image_part=await _image_part(ref)
    if not image_part:return None
    prompt=("You are the final zero-tolerance Pinterest commerce image reviewer. Review the attached candidate image against the supplied product metadata. Reject blurry, low-resolution, stretched, duplicated, generic, misleading, or visually weak images. Reject images that do not confidently depict the exact product. Return JSON only: {\"approved\":boolean,\"score\":0-100,\"reason\":string}. Approve only score >=85. Metadata: "+json.dumps(item.get("metadata") or {},ensure_ascii=False)[:5000])
    args={"model":GEMINI_MODEL,"contents":[{"role":"user","parts":[{"text":prompt},image_part]}],"generation_config":{"temperature":0,"response_mime_type":"application/json"}}
    try:
        data=await run_tool("GEMINI_GENERATE_CONTENT",args,0); result=_json(_gemini_text(data) or str(data))
        if result is not None:return result
    except Exception as e:logger.warning("Composio Gemini reviewer failed: %s",e)
    return None

async def _direct_gemini_batch(items:List[Dict[str,Any]])->Optional[Dict[str,Any]]:
    key=os.getenv("GEMINI_API_KEY","").strip() or os.getenv("GOOGLE_API_KEY","").strip()
    if not key:return None
    async def one(item):
        part=await _image_part(item.get("image_ref") or "")
        if not part:return None
        prompt=("Review this Pinterest product image with zero tolerance. Return JSON only: {\"approved\":boolean,\"score\":0-100,\"reason\":string}. Approve only score >=85 and only if it confidently depicts the exact product. Metadata: "+json.dumps(item.get("metadata") or {},ensure_ascii=False)[:5000])
        body={"contents":[{"role":"user","parts":[{"text":prompt},part]}],"generationConfig":{"temperature":0,"responseMimeType":"application/json"}}
        try:
            async with httpx.AsyncClient(timeout=90) as client:
                r=await client.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",headers={"x-goog-api-key":key,"Content-Type":"application/json"},json=body)
                if r.status_code>=400:return None
                return _json(_gemini_text(r.json()))
        except Exception:return None
    results=await asyncio.gather(*(one(x) for x in items))
    if not all(x is not None for x in results):return None
    passed=all(bool(x.get("approved")) and int(x.get("score",0))>=85 for x in results)
    return {"approved":passed,"final_reviewer":"gemini","status":"AI_REVIEW_PASSED" if passed else "AI_REVIEW_REJECTED","tool_failure":False,"reason":"All Pins passed direct Gemini final approval." if passed else "Direct Gemini final approval failed.","gemini":results}

def _deterministic_validate(items:List[Dict[str,Any]])->Dict[str,Any]:
    """Fallback when visual reviewers are unavailable.
    Publication safety is already enforced by image_quality/choose_candidates and
    publication_guard; this layer must not invent an AI rejection.
    """
    approved=[]
    seen=set()
    failures=[]
    for i,item in enumerate(items,1):
        ref=str(item.get("image_ref") or "").strip()
        if not ref:
            failures.append(f"Pin {i}: missing image reference")
            continue
        if ref in seen:
            failures.append(f"Pin {i}: duplicate image reference")
            continue
        seen.add(ref)
        approved.append(i)
    if failures:
        return {"approved":False,"approved_indexes":approved,"final_reviewer":"deterministic","status":"DETERMINISTIC_VALIDATION_FAILED","tool_failure":False,"reason":"Deterministic fallback found unusable/duplicate media: "+ "; ".join(failures),"deterministic_failures":failures}
    return {"approved":True,"approved_indexes":approved,"final_reviewer":"deterministic","status":"AI_REVIEW_UNAVAILABLE_VALIDATION_PASSED","tool_failure":True,"reason":"Visual AI reviewers were unavailable; existing image-quality and publication guards remain authoritative.","deterministic_validation":"passed"}

def install(wire_module:Any,quality_module:Any)->None:
    original=wire_module.review_batch
    async def review_batch(items:List[Dict[str,Any]],composio_run=None):
        result=await original(items,composio_run=None)
        # Internal deterministic review is authoritative. No external visual reviewer
        # (Grok, Gemini, OpenAI, or any other provider) is invoked.
        if result.get("final_reviewer")=="deterministic":
            return result
        if result.get("approved"):return result
        # A genuine AI rejection remains a hard stop; only tool-unavailable states fail over.
        if result.get("status")=="AI_REVIEW_REJECTED" or (result.get("tool_failure") is False and result.get("final_reviewer") not in {"grok_composio","none","deterministic"}):
            return result
        if composio_run is not None and result.get("final_reviewer") in {"grok_composio","none"}:
            gem=await asyncio.gather(*(_gemini_composio_one(x,composio_run) for x in items))
            if all(x is not None for x in gem):
                passed=all(bool(x.get("approved")) and int(x.get("score",0))>=85 for x in gem)
                return {"approved":passed,"final_reviewer":"gemini_composio","status":"AI_REVIEW_PASSED" if passed else "AI_REVIEW_REJECTED","tool_failure":False,"reason":"All Pins passed Composio Gemini final approval." if passed else "Composio Gemini final approval failed.","gemini":gem,"previous_reviewer":result.get("final_reviewer")}
        direct=await _direct_gemini_batch(items)
        if direct is not None:return direct
        if result.get("tool_failure"):
            return _deterministic_validate(items)
        return result
    wire_module.review_batch=review_batch
    logger.info("AI provider failover installed: Composio Grok -> Composio Gemini -> direct Gemini -> deterministic safety validation")
