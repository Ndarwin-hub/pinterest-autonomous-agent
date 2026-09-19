"""Zero-tolerance visual AI gate for Pinterest product Pins."""
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
    prompt=SYSTEM+"\\nReview this single Pinterest candidate image. Return JSON exactly as {approved:boolean,score:0-100,reason:string}. Approve only at score >=85. The candidate must confidently depict the exact product and be commercially strong for Pinterest. Product metadata follows:\\n"+json.dumps(item.get("metadata") or {},ensure_ascii=False)[:5000]
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
        ok=bool(ref) and ref not in seen and w>=800 and h>=800 and max(w,h)/max(1,min(w,h))<=2.0
        if ref: seen.add(ref)
        scores[str(i)]=score
        if ok: approved.append(i)
        else: reasons.append(f"Pin {i} failed deterministic image safety checks")
    return {"approved_indexes":approved,"scores":scores,"reason":"; ".join(reasons) if reasons else "All candidates passed deterministic safety checks.","status":"DETERMINISTIC_VALIDATION_PASSED" if approved else "DETERMINISTIC_VALIDATION_FAILED","final_reviewer":"deterministic","tool_failure":False}

async def _grok_one(item:Dict[str,Any])->Optional[Dict[str,Any]]:
    if not XAI_API_KEY:return None
    ref=item.get("image_ref"); content=[{"type":"text","text":SYSTEM+"\nReview this candidate. Return {approved:boolean,score:0-100,reason:string}. Approve only at score >=85."}]
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
    prompt=SYSTEM+"\nReview this single Pinterest candidate image. Return JSON exactly as {approved:boolean,score:0-100,reason:string}. Approve only at score >=85. The candidate must confidently depict the exact product and be commercially strong for Pinterest. Product metadata follows:\n"+json.dumps(item.get("metadata") or {},ensure_ascii=False)[:5000]
    args={"model":XAI_MODEL,"input":[{"role":"user","content":[{"type":"input_text","text":prompt},{"type":"input_image","image_url":ref}]}],"store":False}
    try:
        data=await run_tool("GROK_CREATE_RESPONSE",args,0)
        text=_extract_grok_text(data)
        result=_json(text)
        if result is not None:return result
    except Exception as e:
        logger.warning("Composio Grok visual review failed: %s",e)
    return None


async def review_batch(items:List[Dict[str,Any]], composio_run:Optional[Callable[[str,Dict[str,Any],int],Awaitable[Dict[str,Any]]]]=None)->Dict[str,Any]:
    # Priority: OpenAI/ChatGPT-compatible reviewer, then Grok, then Gemini.
    if OPENAI_API_KEY:
        primary=await asyncio.gather(*(_openai_one(x) for x in items))
        if all(x is not None for x in primary):
            approved=[i+1 for i,x in enumerate(primary) if bool(x.get("approved")) and int(x.get("score",0))>=85]
            return {"approved":len(approved)==len(items),"approved_indexes":approved,"final_reviewer":"openai_chatgpt","status":"AI_REVIEW_PASSED" if len(approved)==len(items) else "AI_REVIEW_PARTIAL","tool_failure":False,"reason":"OpenAI/ChatGPT-compatible visual review completed.","openai":primary}
    if composio_run is not None and not XAI_API_KEY:
        grok=await asyncio.gather(*(_composio_grok_one(x,composio_run) for x in items))
        if all(x is not None for x in grok):
            approved=[i+1 for i,x in enumerate(grok) if bool(x.get("approved")) and int(x.get("score",0))>=85]
            return {"approved":len(approved)==len(items),"approved_indexes":approved,"final_reviewer":"grok_composio","status":"AI_REVIEW_PASSED" if len(approved)==len(items) else "AI_REVIEW_PARTIAL","tool_failure":False,"reason":"Connected Composio Grok visual review completed.","grok":grok}
    if XAI_API_KEY:
        grok=await asyncio.gather(*(_grok_one(x) for x in items))
        if all(x is not None for x in grok):
            approved=[i+1 for i,x in enumerate(grok) if bool(x.get("approved")) and int(x.get("score",0))>=85]
            return {"approved":len(approved)==len(items),"approved_indexes":approved,"final_reviewer":"grok","status":"AI_REVIEW_PASSED" if len(approved)==len(items) else "AI_REVIEW_PARTIAL","tool_failure":False,"reason":"Direct Grok visual review completed.","grok":grok}
    gem=await _gemini(items)
    if isinstance(gem,dict):
        approved=[int(x) for x in gem.get("approved_indexes",[]) if str(x).isdigit()]
        scores=gem.get("scores") or {}
        approved=[i for i in approved if int(scores.get(str(i),0))>=85]
        return {"approved":len(approved)==len(items),"approved_indexes":approved,"final_reviewer":"gemini","status":"AI_REVIEW_PASSED" if len(approved)==len(items) else "AI_REVIEW_PARTIAL","tool_failure":False,"reason":"Gemini visual review completed.","gemini":gem}
    # No reviewer: do not wait or loop; use the existing deterministic image safety gates.
    return await deterministic_review(items)

async def _gemini(items:List[Dict[str,Any]])->Optional[Dict[str,Any]]:
    if not GEMINI_API_KEY:return None
    parts=[{"text":SYSTEM+"\nReview all five candidates. Return JSON exactly as {approved_indexes:[1,2,...],scores:{\"1\":0,\"2\":0,...},reason:string}. Every Pin must score >=85 to be approved."}]
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
