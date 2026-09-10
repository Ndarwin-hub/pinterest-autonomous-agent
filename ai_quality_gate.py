"""Zero-tolerance visual AI gate for Pinterest product Pins.

Gemini reviews the complete five-Pin batch first. Grok is the final approver when
an XAI_API_KEY is available. If Grok is unavailable, Gemini becomes final. If no
visual AI can actually review the images, publishing is blocked.
"""
from __future__ import annotations
import asyncio, json, logging, os, re
from typing import Any, Dict, List, Optional
import httpx

logger=logging.getLogger("pinterest-agent.ai-quality")
XAI_API_KEY=os.getenv("XAI_API_KEY","").strip()
GEMINI_API_KEY=(os.getenv("GEMINI_API_KEY","").strip() or os.getenv("GOOGLE_API_KEY","").strip())
XAI_MODEL=os.getenv("GROK_REVIEW_MODEL","grok-4.6")
GEMINI_MODEL=os.getenv("GEMINI_REVIEW_MODEL","gemini-2.5-flash")
SYSTEM="""You are a zero-tolerance Pinterest commerce image/copy quality controller. Reject anything blurry, low-resolution, stretched, banner-like, duplicated, generic, visually weak, misleading, or not confidently the exact product. Prefer authentic product photography from official/manufacturer/retailer sources. Pexels is allowed only when it genuinely supports the product/use case and does not misrepresent the actual product. AI-generated product imagery must be rejected if it invents product appearance, logo, ports, specs, packaging, or other identity details. Return JSON only."""

def _json(text:str)->Optional[Dict[str,Any]]:
    try:
        m=re.search(r"\{.*\}",text or "",re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:return None

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
            t=r.json().get("choices",[{}])[0].get("message",{}).get("content","")
            return _json(t)
    except Exception:return None

async def _gemini(items:List[Dict[str,Any]])->Optional[Dict[str,Any]]:
    if not GEMINI_API_KEY:return None
    parts=[{"text":SYSTEM+"\nReview all five candidates. Return JSON exactly as {approved_indexes:[1,2,...],scores:{\"1\":0,\"2\":0,...},reason:string}. Every Pin must score >=85 to be approved."}]
    for i,x in enumerate(items,1):
        parts.append({"text":f"CANDIDATE {i}: {json.dumps(x.get('metadata') or {},ensure_ascii=False)[:3500]}"})
        ref=x.get("image_ref") or ""
        if ref.startswith("data:image/"):
            try:
                header,b64=ref.split(',',1); mime=header.split(';')[0].split(':',1)[1]
                parts.append({"inline_data":{"mime_type":mime,"data":b64}})
            except Exception:pass
    body={"contents":[{"role":"user","parts":parts}],"generationConfig":{"temperature":0,"responseMimeType":"application/json"}}
    try:
        async with httpx.AsyncClient(timeout=90) as c:
            r=await c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",headers={"x-goog-api-key":GEMINI_API_KEY,"Content-Type":"application/json"},json=body)
            if r.status_code>=400:return None
            parts2=r.json().get("candidates",[{}])[0].get("content",{}).get("parts",[])
            text=''.join(str(x.get('text','')) for x in parts2 if isinstance(x,dict))
            return _json(text)
    except Exception:return None

async def review_batch(items:List[Dict[str,Any]])->Dict[str,Any]:
    gem=await _gemini(items)
    if XAI_API_KEY:
        grok=await asyncio.gather(*(_grok_one(x) for x in items))
        passed=all(isinstance(x,dict) and bool(x.get("approved")) and int(x.get("score",0))>=85 for x in grok)
        return {"approved":passed,"final_reviewer":"grok","reason":"All five Pins passed Grok final approval." if passed else "Grok final approval failed.","gemini":gem,"grok":grok}
    if isinstance(gem,dict):
        approved=gem.get("approved_indexes") or []
        scores=gem.get("scores") or {}
        passed=len(approved)==len(items) and all(int(scores.get(str(i),0))>=85 for i in range(1,len(items)+1))
        return {"approved":passed,"final_reviewer":"gemini","reason":"Grok unavailable; Gemini is final reviewer." if passed else "Gemini final review failed.","gemini":gem}
    return {"approved":False,"final_reviewer":"none","reason":"No visual AI reviewer is available; zero-tolerance policy blocks publication."}
