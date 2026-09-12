"""Runtime wiring: zero-tolerance image sourcing, AI approval, recovery and 22-call cap."""
from __future__ import annotations
import contextvars, logging, re
from typing import Any, Dict
from board_org import detect_product_category, preferred_board_name, find_matching_board, DEFAULT_BOARD_NAME
from image_quality import choose_candidates
from ai_quality_gate import review_batch, generate_image, GEMINI_API_KEY, XAI_API_KEY, OPENAI_API_KEY
logger=logging.getLogger("pinterest-agent.wire")
MAX_COMPOSIO_CALLS=22
MAX_RECOVERY_ROUNDS=2
_call_budget=contextvars.ContextVar("pinterest_call_budget",default=None)
class CallBudget:
    def __init__(self,maximum=MAX_COMPOSIO_CALLS): self.maximum=maximum; self.used=0; self.image_search_invocations=0; self.pexels_invocations=0; self.grok_invocations=0
    def reserve(self,slug):
        if self.used>=self.maximum: raise RuntimeError(f"Composio hard job budget exhausted ({self.maximum} calls); stopping safely.")
        self.used+=1; logger.info("Composio budget %s/%s %s",self.used,self.maximum,slug)
async def _static_capabilities(agent_mod):
    return {"composio_search_image":{"connected":bool(getattr(agent_mod,"COMPOSIO_API_KEY","")),"executable":True,"production_tested":False,"kind":"image_search","reason":"One targeted search per Pin."},"pexels":{"connected":True,"executable":True,"production_tested":False,"kind":"image_search","reason":"Pexels via Composio; one call per Pin."},"gemini_review":{"connected":bool(GEMINI_API_KEY),"executable":bool(GEMINI_API_KEY),"kind":"visual_quality","reason":"Gemini first-pass visual review."},"grok_review":{"connected":bool(XAI_API_KEY or getattr(agent_mod,"COMPOSIO_API_KEY","")),"executable":bool(XAI_API_KEY or getattr(agent_mod,"COMPOSIO_API_KEY","")),"kind":"final_approval","reason":"Grok via direct xAI key or the existing Composio connection."},"ai_generation":{"connected":bool(XAI_API_KEY or OPENAI_API_KEY),"executable":bool(XAI_API_KEY or OPENAI_API_KEY),"kind":"image_generation","reason":"Second-stage fallback only."},"pinterest":{"connected":True,"executable":True,"production_tested":True,"kind":"publish","reason":"Existing pipeline."}}
def apply_agent_wiring(agent_mod:Any)->None:
    orig_research=agent_mod.research_product; orig_run=agent_mod.run_composio_tool
    async def budgeted_run(slug:str,args:Dict[str,Any],retries:int=2):
        b=_call_budget.get()
        if b is None: raise RuntimeError("Composio execution attempted outside managed job budget.")
        if slug=="PEXELS_SEARCH_PHOTOS":
            if b.pexels_invocations>=5:return {}
            b.pexels_invocations+=1
        if slug=="COMPOSIO_SEARCH_IMAGE":
            if b.image_search_invocations>=5:return {}
            b.image_search_invocations+=1
        if slug=="GROK_CREATE_RESPONSE":
            if b.grok_invocations>=5:return {}
            b.grok_invocations+=1
        b.reserve(slug); return await orig_run(slug,args or {},retries=0)
    async def research(url,job_store,job_id):
        p=await orig_research(url,job_store,job_id); p["url"]=url; p["category"]=detect_product_category(p); return p
    async def board(product,job_store,job_id):
        job_store.update(job_id,progress="Selecting Pinterest board")
        data=await budgeted_run("PINTEREST_LIST_BOARDS",{}); items=data.get("items") or data.get("boards") or []
        category=(product.get("category") or "general").lower(); preferred=preferred_board_name(category); mid=find_matching_board(items,preferred)
        if mid:return mid
        # All Pinterest boards are fixed/permanent. Never create a board at runtime.
        # If the category board is unavailable, use the verified Everything Else
        # fallback. If that fallback is also unavailable, stop safely rather than
        # creating or mutating board structure.
        fallback=find_matching_board(items,DEFAULT_BOARD_NAME)
        if fallback:return fallback
        raise RuntimeError("No verified permanent Pinterest board is available for this product; automatic board creation is disabled.")
    def build_review_items(pins,product):
        return [{"image_ref":p["image_ref"],"metadata":{"pin_number":p["pin_number"],"strategy":p["strategy"]["name"],"title":p["seo"]["title"],"description":p["seo"]["description"],"product":product.get("name"),"brand":product.get("brand"),"image_score":p["image"].get("score"),"dimensions":[p["image"].get("width"),p["image"].get("height")]}} for p in pins]
    def failed_indexes(review,pin_count):
        if review.get("final_reviewer") in ("grok","grok_composio"):
            results=review.get("grok") or []
            return {i+1 for i,x in enumerate(results[:pin_count]) if not (isinstance(x,dict) and bool(x.get("approved")) and int(x.get("score",0))>=85)}
        if review.get("final_reviewer")=="gemini":
            approved={int(x) for x in (review.get("gemini") or {}).get("approved_indexes",[]) if str(x).isdigit()}
            scores=(review.get("gemini") or {}).get("scores") or {}
            return {i for i in range(1,pin_count+1) if i not in approved or int(scores.get(str(i),0))<85}
        return set(range(1,pin_count+1))
    async def process(job_id,url,job_store):
        token=_call_budget.set(CallBudget()); b=_call_budget.get()
        try:
            url=url.strip(); m=re.search(r"https?://\S+",url)
            if m:url=m.group(0).rstrip(".,)]")
            if not url.startswith("http"):raise RuntimeError("A valid product/affiliate URL is required.")
            product=await research(url,job_store,job_id); seo=agent_mod.build_five_seo(product); board_id=await board(product,job_store,job_id)
            used=set(); pins=[]; resources=set()
            for i,strategy in enumerate(agent_mod.STRATEGIES,1):
                candidates=await choose_candidates(product,strategy,i,used,agent_mod)
                if not candidates:
                    generated=await generate_image(product,strategy)
                    if not generated:raise RuntimeError(f"Pin {i}: no genuine high-quality image survived and no AI-generation fallback is configured.")
                    candidates=[generated]
                best=candidates[0]
                if best.get("url"):used.add(best["url"])
                resources.add(best.get("provider") or "unknown")
                ref=best.get("url") or (f"data:image/jpeg;base64,{best['value']}" if best.get("value") else "")
                if not ref:raise RuntimeError(f"Pin {i}: selected image has no usable media.")
                pins.append({"pin_number":i,"strategy":strategy,"seo":seo[i-1],"image":best,"image_ref":ref,"candidate_count":len(candidates),"candidate_pool":candidates[:8],"candidate_cursor":0})
            review=None; recovery_rounds=0
            while True:
                review_items=build_review_items(pins,product)
                job_store.update(job_id,progress="Composio Grok visual review" if not recovery_rounds else f"AI re-review after automatic recovery round {recovery_rounds}")
                review=await review_batch(review_items, composio_run=budgeted_run if (getattr(agent_mod,"COMPOSIO_API_KEY","") and not XAI_API_KEY) else None)
                if review.get("approved"):break
                failed=failed_indexes(review,len(pins))
                if not failed:break
                if recovery_rounds>=MAX_RECOVERY_ROUNDS:
                    raise RuntimeError("Zero-tolerance AI quality gate blocked publication after automatic recovery attempts: "+str(review.get("reason")))
                replaced=0
                for idx in sorted(failed):
                    p=pins[idx-1]; pool=p.get("candidate_pool") or []
                    cursor=int(p.get("candidate_cursor") or 0)
                    next_candidate=None
                    while cursor+1<len(pool):
                        cursor+=1; c=pool[cursor]; u=c.get("url")
                        if not u or u not in used:
                            next_candidate=c;break
                    p["candidate_cursor"]=cursor
                    if next_candidate:
                        old=p["image"]; old_url=old.get("url")
                        if old_url: used.discard(old_url)
                        p["image"]=next_candidate; p["image_ref"]=next_candidate.get("url") or (f"data:image/jpeg;base64,{next_candidate['value']}" if next_candidate.get("value") else "")
                        p["candidate_count"]=len(pool)
                        if next_candidate.get("url"):used.add(next_candidate["url"])
                        resources.add(next_candidate.get("provider") or "unknown"); replaced+=1
                if replaced==0:
                    raise RuntimeError("Zero-tolerance AI quality gate blocked publication: failed Pin(s) had no unused prevalidated replacement candidates, and no additional Composio image calls are permitted.")
                recovery_rounds+=1
            published=[]; errors=[]
            for p in pins:
                s=p["seo"]; im=p["image"]
                try:
                    r=await agent_mod.publish_and_verify(board_id,s["title"],s["description"],s["alt_text"],im.get("mode","url"),im.get("value") or im.get("url"),url,job_store,job_id,p["pin_number"])
                    published.append({"pin_number":p["pin_number"],"strategy":p["strategy"]["name"],"image_provider":im.get("provider"),"image_id":im.get("id"),"image_score":im.get("score"),"dimensions":[im.get("width"),im.get("height")],"candidate_count":p["candidate_count"],"title":s["title"],"keywords":s.get("keywords"),**r})
                except Exception as e:errors.append({"pin_number":p["pin_number"],"error":str(e)})
            if len(published)!=5:raise RuntimeError(f"Zero-tolerance publish failed: {len(published)}/5 Pins published.")
            return {"product_name":product.get("name"),"source_url":url,"category":product.get("category"),"capabilities":await _static_capabilities(agent_mod),"resources_used":sorted(resources),"pins_planned":5,"pins_published":5,"board_id":board_id,"pins":published,"errors":errors,"ai_quality_review":review,"recovery_rounds":recovery_rounds,"composio_call_budget":{"used":b.used,"maximum":b.maximum,"remaining":b.maximum-b.used},"summary":"5/5 Pins published only after hard image gates, automatic failed-Pin recovery, and final AI approval."}
        finally:_call_budget.reset(token)
    agent_mod.run_composio_tool=budgeted_run; agent_mod.probe_capabilities=lambda:_static_capabilities(agent_mod); agent_mod.research_product=research; agent_mod.select_or_create_board=board; agent_mod.process_pinterest_job=process
    logger.info("Zero-tolerance image sourcing + AI approval + automatic recovery + 22-call budget wiring applied")
