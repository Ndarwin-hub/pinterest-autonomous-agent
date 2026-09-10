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
    # Prefer the already-connected Composio Grok account when available. Each
    # candidate consumes exactly one managed Composio call, so the caller's
    # hard job budget remains authoritative.
    if composio_run is not None and not XAI_API_KEY:
        grok=await asyncio.gather(*(_composio_grok_one(x,composio_run) for x in items))
        passed=all(isinstance(x,dict) and bool(x.get("approved")) and int(x.get("score",0))>=85 for x in grok)
        if all(x is not None for x in grok):
            return {"approved":passed,"final_reviewer":"grok_composio","reason":"All Pins passed the connected Composio Grok final approval." if passed else "Composio Grok final approval failed.","grok":grok}
        # Do not silently publish if the connected reviewer cannot inspect an image.
        return {"approved":False,"final_reviewer":"grok_composio","reason":"Connected Composio Grok could not return a valid visual review; zero-tolerance policy blocks publication.","grok":grok}
    gem=await _gemini(items)
    if XAI_API_KEY:
        grok=await asyncio.gather(*(_grok_one(x) for x in items)); passed=all(isinstance(x,dict) and bool(x.get("approved")) and int(x.get("score",0))>=85 for x in grok)
        return {"approved":passed,"final_reviewer":"grok","reason":"All Pins passed Grok final approval." if passed else "Grok final approval failed.","gemini":gem,"grok":grok}
    if isinstance(gem,dict):
        approved=gem.get("approved_indexes") or []; scores=gem.get("scores") or {}; passed=len(approved)==len(items) and all(int(scores.get(str(i),0))>=85 for i in range(1,len(items)+1))
        return {"approved":passed,"final_reviewer":"gemini","reason":"Grok unavailable; Gemini is final reviewer." if passed else "Gemini final review failed.","gemini":gem}
    return {"approved":False,"final_reviewer":"none","reason":"No visual AI reviewer is available; zero-tolerance policy blocks publication."}

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
