"""Advisory visual-quality review for Pinterest product Pins; deterministic technical validity remains authoritative."""
from __future__ import annotations
import asyncio, base64, json, logging, os, re
from typing import Any, Dict, List, Optional, Callable, Awaitable
import httpx
logger=logging.getLogger("pinterest-agent.ai-quality")
XAI_API_KEY=os.getenv("XAI_API_KEY","").strip()
OPENAI_API_KEY=os.getenv("OPENAI_API_KEY","").strip()
GEMINI_API_KEY=(os.getenv("GEMINI_API_KEY","").strip() or os.getenv("GOOGLE_API_KEY","").strip())
XAI_MODEL=os.getenv("GROK_REVIEW_MODEL","grok-4.6")
GEMINI_MODEL=os.getenv("GEMINI_REVIEW_MODEL","gemini-2.5-flash")
SYSTEM="""You are a zero-tolerance Pinterest commerce image/copy quality controller. Reject anything blurry, low-resolution, stretched, banner-like, duplicated, generic, visually weak, misleading, or not confidently the exact product. Prefer authentic product photography from official/manufacturer/retailer sources. Pexels is allowed only when it genuinely supports the product/use case and does not misrepresent the actual product. AI-generated product imagery must be rejected if it invents product appearance, logo, ports, specs, packaging, or other identity details. Return JSON only."""

def _json(text:str)->Optional[Dict[str,Any]]:
    try:
        m=re.search(r"\{.*\}",text or "",re.S); return json.loads(m.group(0)) if m else None
    except Exception:return None

def _extract_grok_text(data:Any)->str:
    if isinstance(data,str): return data
    if not isinstance(data,dict): return ""
    for key in ("output_text","text","content"):
        value=data.get(key)
        if isinstance(value,str): return value
    output=data.get("output")
    if isinstance(output,list):
        chunks=[]
        for item in output:
            if isinstance(item,dict):
                if isinstance(item.get("text"),str): chunks.append(item["text"])
                content=item.get("content")
                if isinstance(content,str): chunks.append(content)
                elif isinstance(content,list):
                    for part in content:
                        if isinstance(part,dict) and isinstance(part.get("text"),str): chunks.append(part["text"])
        return "".join(chunks)
    return ""

async def generate_image(product:Dict[str,Any],strategy:Dict[str,Any])->Optional[Dict[str,Any]]:
    prompt=(f"Create a premium Pinterest product-commerce image for the exact product: {product.get('name','Product')}. "
            f"Brand: {product.get('brand') or 'unknown'}. Strategy: {strategy.get('name')}. "
            "Do not invent logos, specifications, ports, packaging or physical details. If exact product appearance cannot be represented faithfully, create no misleading product depiction. "
            "Use a clean 2:3 Pinterest composition, sharp, high-end commercial photography, no banners, no tiny text, no watermark.")
    if XAI_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=180) as c:
                r=await c.post("https://api.x.ai/v1/images/generations",headers={"Authorization":f"Bearer {XAI_API_KEY}","Content-Type":"application/json"},json={"model":"grok-imagine-image-2.0","prompt":prompt,"aspect_ratio":"2:3"})
                if r.status_code<400:
                    d=(r.json().get("data") or [{}])[0]; b=d.get("b64_json")
                    if b:return {"mode":"base64","value":b,"provider":"grok_image_generation","id":"generated","score":0,"license":"ai_generated"}
                    u=d.get("url")
                    if u:return {"mode":"url","value":u,"provider":"grok_image_generation","id":"generated","score":0,"license":"ai_generated"}
        except Exception:pass
    if OPENAI_API_KEY:
        try:
            async with httpx.AsyncClient(timeout=180) as c:
                r=await c.post("https://api.openai.com/v1/images/generations",headers={"Authorization":f"Bearer {OPENAI_API_KEY}","Content-Type":"application/json"},json={"model":"gpt-image-1","prompt":prompt,"size":"1024x1536"})
                if r.status_code<400:
                    d=(r.json().get("data") or [{}])[0]; b=d.get("b64_json")
                    if b:return {"mode":"base64","value":b,"provider":"openai_image_generation","id":"generated","score":0,"license":"ai_generated"}
        except Exception:pass
    return None


async def _openai_one(item:Dict[str,Any])->Optional[Dict[str,Any]]:
    """First-priority OpenAI/ChatGPT-compatible visual reviewer when an API key is configured."""
    if not OPENAI_API_KEY:return None
    ref=item.get("image_ref") or ""
    if not ref:return None
    prompt=SYSTEM+"\\nReview this single Pinterest candidate image. Return JSON exactly as {approved:boolean,score:0-100,reason:string}. Score the candidate 0-100. Do not treat the score as a publication gate. The candidate must confidently depict the exact product and be commercially strong for Pinterest. Product metadata follows:\\n"+json.dumps(item.get("metadata") or {},ensure_ascii=False)[:5000]
    body={"model":os.getenv("OPENAI_REVIEW_MODEL","gpt-5.4"),"input":[{"role":"user","content":[{"type":"input_text","text":prompt},{"type":"input_image","image_url":ref}]}],"temperature":0}
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r=await c.post("https://api.openai.com/v1/responses",headers={"Authorization":f"Bearer {OPENAI_API_KEY}","Content-Type":"application/json"},json=body)
            if r.status_code>=400:return None
            return _json(r.json().get("output_text",""))
    except Exception as e:
        logger.warning("OpenAI/ChatGPT visual review failed: %s",e)
        return None

async def deterministic_review(items:List[Dict[str,Any]])->Dict[str,Any]:
    """Fast local safety gate used immediately when no AI reviewer is executable."""
    approved=[]; scores={}; reasons=[]; seen=set()
    for i,item in enumerate(items,1):
        meta=item.get("metadata") or {}; ref=item.get("image_ref") or ""
        dims=meta.get("dimensions") or []
        w=int(dims[0] or 0) if len(dims)>0 and dims[0] else 0
        h=int(dims[1] or 0) if len(dims)>1 and dims[1] else 0
        score=int(meta.get("image_score") or 0)
        # Technical validity only: image_quality.py owns image selection and quality scoring.
        # This reviewer must never reject a usable image merely because a candidate's
        # metadata omitted dimensions; URLs/base64 are valid media references by themselves.
        # When dimensions are known, enforce only the hard structural bounds.
        dimensions_known = w > 0 and h > 0
        aspect_ok = (max(w,h) / max(1,min(w,h)) <= 4.0) if dimensions_known else True
        size_ok = (w >= 100 and h >= 100) if dimensions_known else True
        ok=bool(ref) and ref not in seen and size_ok and aspect_ok
        if ref: seen.add(ref)
        scores[str(i)]=score
        if ok: approved.append(i)
        else: reasons.append(f"Pin {i} failed deterministic image safety checks")
    return {"approved_indexes":approved,"scores":scores,"reason":"; ".join(reasons) if reasons else "All candidates passed deterministic technical validation.","status":"DETERMINISTIC_VALIDATION_PASSED" if len(approved)==len(items) else "DETERMINISTIC_VALIDATION_PARTIAL","final_reviewer":"deterministic","tool_failure":False,"advisory":True}

async def _grok_one(item:Dict[str,Any])->Optional[Dict[str,Any]]:
    if not XAI_API_KEY:return None
    ref=item.get("image_ref"); content=[{"type":"text","text":SYSTEM+"\nReview this candidate. Return {approved:boolean,score:0-100,reason:string}. Score the candidate 0-100. Do not treat the score as a publication gate."}]
    if ref: content.append({"type":"image_url","image_url":{"url":ref,"detail":"high"}})
    content.append({"type":"text","text":json.dumps(item.get("metadata") or {},ensure_ascii=False)[:5000]})
    body={"model":XAI_MODEL,"messages":[{"role":"user","content":content}],"temperature":0,"max_tokens":180}
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r=await c.post("https://api.x.ai/v1/chat/completions",headers={"Authorization":f"Bearer {XAI_API_KEY}","Content-Type":"application/json"},json=body)
            if r.status_code>=400:return None
            return _json(r.json().get("choices",[{}])[0].get("message",{}).get("content",""))
    except Exception:return None

async def _composio_grok_one(item:Dict[str,Any],run_tool:Callable[[str,Dict[str,Any],int],Awaitable[Dict[str,Any]]])->Optional[Dict[str,Any]]:
    """Use the user's already-connected Composio Grok account; no xAI key is exposed to Railway."""
    ref=item.get("image_ref") or ""
    prompt=SYSTEM+"\nReview this single Pinterest candidate image. Return JSON exactly as {approved:boolean,score:0-100,reason:string}. Score the candidate 0-100. Do not treat the score as a publication gate. The candidate must confidently depict the exact product and be commercially strong for Pinterest. Product metadata follows:\n"+json.dumps(item.get("metadata") or {},ensure_ascii=False)[:5000]
    args={"model":XAI_MODEL,"input":[{"role":"user","content":[{"type":"input_text","text":prompt},{"type":"input_image","image_url":ref}]}],"store":False}
    try:
        data=await run_tool("GROK_CREATE_RESPONSE",args,0)
        text=_extract_grok_text(data)
        result=_json(text)
        if result is not None:return result
    except Exception as e:
        logger.warning("Composio Grok visual review failed: %s",e)
    return None


async def _composio_grok_batch(items:List[Dict[str,Any]],run_tool:Callable[[str,Dict[str,Any],int],Awaitable[Dict[str,Any]]])->Optional[Dict[str,Any]]:
    """Review the complete five-candidate set in exactly one Composio Grok call."""
    if not items:return None
    content=[{"type":"input_text","text":SYSTEM+"\nReview all candidate images together. Return JSON exactly as {approved_indexes:[1,2,...],scores:{\"1\":0,\"2\":0,...},reason:string}. Score each candidate 0-100. Do not treat score as a publication gate; technically usable candidates remain publishable."}]
    for i,item in enumerate(items,1):
        content.append({"type":"input_text","text":f"CANDIDATE {i}: "+json.dumps(item.get("metadata") or {},ensure_ascii=False)[:3500]})
        ref=item.get("image_ref") or ""
        if ref: content.append({"type":"input_image","image_url":ref})
    args={"model":XAI_MODEL,"input":[{"role":"user","content":content}],"store":False}
    try:
        data=await run_tool("GROK_CREATE_RESPONSE",args,0)
        return _json(_extract_grok_text(data))
    except Exception as e:
        logger.warning("Composio Grok batch visual review failed: %s",e)
        return None

async def review_batch(items:List[Dict[str,Any]], composio_run:Optional[Callable[[str,Dict[str,Any],int],Awaitable[Dict[str,Any]]]]=None)->Dict[str,Any]:
    """Internal-only review path.

    External visual reviewers are intentionally disabled. Existing local image
    quality selection, deterministic technical validation, duplicate detection,
    and publication verification remain authoritative.
    """
    return await deterministic_review(items)

async def _gemini(items:List[Dict[str,Any]])->Optional[Dict[str,Any]]:
    if not GEMINI_API_KEY:return None
    parts=[{"text":SYSTEM+"\nReview all five candidates. Return JSON exactly as {approved_indexes:[1,2,...],scores:{\"1\":0,\"2\":0,...},reason:string}. Use scores to identify stronger candidates; do not reject a technically usable candidate solely because of its score."}]
    for i,x in enumerate(items,1):
        parts.append({"text":f"CANDIDATE {i}: {json.dumps(x.get('metadata') or {},ensure_ascii=False)[:3500]}"})
        ref=x.get("image_ref") or ""
        if ref.startswith("data:image/"):
            try:
                header,b64=ref.split(',',1); mime=header.split(';')[0].split(':',1)[1]; parts.append({"inline_data":{"mime_type":mime,"data":b64}})
            except Exception:pass
    body={"contents":[{"role":"user","parts":parts}],"generationConfig":{"temperature":0,"responseMimeType":"application/json"}}
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r=await c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",headers={"x-goog-api-key":GEMINI_API_KEY,"Content-Type":"application/json"},json=body)
            if r.status_code>=400:return None
            p=r.json().get("candidates",[{}])[0].get("content",{}).get("parts",[]); text=''.join(str(x.get('text','')) for x in p if isinstance(x,dict)); return _json(text)
    except Exception:return None
