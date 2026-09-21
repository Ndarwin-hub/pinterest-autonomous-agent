"""Runtime wiring: zero-tolerance image sourcing, AI approval, recovery and 40-call cap."""
from __future__ import annotations
import contextvars, logging, re
from typing import Any, Dict
from board_org import detect_product_category, preferred_board_name, find_matching_board, DEFAULT_BOARD_NAME, LEGACY_BOARD_NAMES, LEGACY_BOARD_IDS
from image_quality import choose_candidates
from ai_quality_gate import review_batch, generate_image, GEMINI_API_KEY, XAI_API_KEY, OPENAI_API_KEY
logger=logging.getLogger("pinterest-agent.wire")
MAX_COMPOSIO_CALLS=40
MAX_RECOVERY_ROUNDS=1
# Five Pins x four targeted image-search queries = 20 calls. The old value of 18
# starved the last Pins of the initial candidate pool under concurrent load.
# Reserve 25 image-search calls so all five Pins receive the full initial search
# pass and one bounded recovery refresh can still fit inside the 40-call ceiling.
MAX_IMAGE_SEARCH_CALLS=25
_call_budget=contextvars.ContextVar("pinterest_call_budget",default=None)
class CallBudget:
    def __init__(self,maximum=MAX_COMPOSIO_CALLS):
        self.maximum=maximum; self.used=0; self.image_search_invocations=0; self.pexels_invocations=0
    def reserve(self,slug):
        if self.used>=self.maximum: raise RuntimeError(f"Composio hard job budget exhausted ({self.maximum} calls); stopping safely.")
        self.used+=1; logger.info("Composio budget %s/%s %s",self.used,self.maximum,slug)
async def _static_capabilities(agent_mod):
    return {"composio_search_image":{"connected":bool(getattr(agent_mod,"COMPOSIO_API_KEY","")),"executable":True,"production_tested":False,"kind":"image_search","reason":"Four targeted search angles per Pin, with one adaptive recovery search round available."},"pexels":{"connected":True,"executable":True,"production_tested":False,"kind":"image_search","reason":"Pexels via Composio only when the normal Composio image route is unavailable."},"openai_chatgpt_review":{"connected":bool(OPENAI_API_KEY),"executable":bool(OPENAI_API_KEY),"kind":"visual_quality","reason":"First-priority OpenAI/ChatGPT-compatible visual review when configured."},"gemini_review":{"connected":bool(GEMINI_API_KEY),"executable":bool(GEMINI_API_KEY),"kind":"visual_quality","reason":"Gemini first-pass visual review."},"ai_generation":{"connected":bool(XAI_API_KEY or OPENAI_API_KEY),"executable":bool(XAI_API_KEY or OPENAI_API_KEY),"kind":"image_generation","reason":"Second-stage fallback only."},"pinterest":{"connected":True,"executable":True,"production_tested":True,"kind":"publish","reason":"Existing pipeline."}}
def apply_agent_wiring(agent_mod:Any)->None:
    orig_research=agent_mod.research_product; orig_run=agent_mod.run_composio_tool; orig_publish=agent_mod.publish_and_verify
    async def budgeted_run(slug:str,args:Dict[str,Any],retries:int=2):
        b=_call_budget.get()
        if b is None: raise RuntimeError("Composio execution attempted outside managed job budget.")
        if slug=="PEXELS_SEARCH_PHOTOS":
            if b.pexels_invocations>=5:return {}
            b.pexels_invocations+=1
        if slug=="COMPOSIO_SEARCH_IMAGE":
            if b.image_search_invocations>=MAX_IMAGE_SEARCH_CALLS:
                raise RuntimeError(f"Adaptive image-search budget exhausted ({MAX_IMAGE_SEARCH_CALLS} calls).")
            b.image_search_invocations+=1
        b.reserve(slug)
        transport = getattr(agent_mod, "_composio_transport_executor", None)
        if transport is not None:
            return await transport(slug, args or {}, retries=0)
        return await orig_run(slug,args or {},retries=0)
    async def strict_publish(board_id,title,description,alt_text,image_mode,image_value,link,job_store,job_id,pin_index):
        # Dynamic lookup: runtime_hardening.install() replaces agent_mod.publish_and_verify
        # after this wiring. Never call the captured pre-hardening orig_publish (it bypassed
        # the budgeted runner). If hardening has not run yet, fall back to orig_publish.
        current = getattr(agent_mod, "publish_and_verify", None)
        if current is not None and current is not strict_publish and getattr(agent_mod, "_runtime_publish_hardening_installed", False):
            return await current(board_id,title,description,alt_text,image_mode,image_value,link,job_store,job_id,pin_index)
        return await orig_publish(board_id,title,description,alt_text,image_mode,image_value,link,job_store,job_id,pin_index)
    async def research(url,job_store,job_id):
        p=await orig_research(url,job_store,job_id)
        # Preserve resolved affiliate destination from quality_patch (short-URL canonicalization).
        # Only fall back to the inbound job URL when research did not set a better destination.
        resolved = (p.get("affiliate_url") or p.get("url") or "").strip()
        if resolved and resolved != url and resolved.startswith("http"):
            p["url"] = resolved
            p["affiliate_url"] = resolved
        else:
            p["url"] = url
        p["category"]=detect_product_category(p); return p
    async def board(product,job_store,job_id):
        job_store.update(job_id,progress="Selecting Pinterest board")
        data=await budgeted_run("PINTEREST_LIST_BOARDS",{}); items=data.get("items") or data.get("boards") or []
        from board_org import classify_with_confidence
        from board_balance import resolve_board_with_balance
        info=classify_with_confidence(product)
        category=(info.get("category") or product.get("category") or "general").lower()
        confidence=(info.get("confidence") or "LOW").upper()
        # Core rule: live counts → relevance-first assignment with balance metadata
        resolved=resolve_board_with_balance(product,items)
        product["category"]=resolved.get("category") or category
        product["board_confidence"]=resolved.get("confidence") or confidence
        product["board_balance"]=resolved.get("balance")
        product["board_assignment_reason"]=resolved.get("reason")
        mid=resolved.get("board_id")
        if mid:
            if str(mid) in LEGACY_BOARD_IDS:raise RuntimeError("Legacy board ID selected for a new Pin; refusing publication.")
            for b in items:
                bid=str(b.get("id") or b.get("board_id") or "")
                if bid==str(mid) and (b.get("name") or "").strip() in LEGACY_BOARD_NAMES:raise RuntimeError("Legacy board name selected for a new Pin; refusing publication.")
            # Confirm the live board still exists
            live_ids={str(b.get("id") or b.get("board_id") or "") for b in items}
            if str(mid) in live_ids:
                return str(mid)
        # Fallback to previous name-based match if balance resolver missed a live id
        preferred=preferred_board_name(category)
        mid=find_matching_board(items,preferred)
        if mid:
            if str(mid) in LEGACY_BOARD_IDS:raise RuntimeError("Legacy board ID selected for a new Pin; refusing publication.")
            for b in items:
                bid=str(b.get("id") or b.get("board_id") or "")
                if bid==str(mid) and (b.get("name") or "").strip() in LEGACY_BOARD_NAMES:raise RuntimeError("Legacy board name selected for a new Pin; refusing publication.")
            product["category"]=category
            product["board_confidence"]=confidence
            return mid
        if category not in ("general",) and confidence in ("HIGH","MEDIUM"):
            raise RuntimeError(
                f"Intended board {preferred!r} unavailable for category {category!r} "
                f"(confidence={confidence}); refusing Everything Else fallback."
            )
        fallback=find_matching_board(items,DEFAULT_BOARD_NAME)
        if fallback:
            if str(fallback) in LEGACY_BOARD_IDS:raise RuntimeError("Legacy board ID selected as fallback; refusing publication.")
            product["category"]=category
            product["board_confidence"]=confidence
            return fallback
        raise RuntimeError("No verified permanent Pinterest board is available for this product; automatic board creation is disabled.")
    def build_review_items(pins,product):
        return [{"image_ref":p["image_ref"],"metadata":{"pin_number":p["pin_number"],"strategy":p["strategy"]["name"],"title":p["seo"]["title"],"description":p["seo"]["description"],"product":product.get("name"),"brand":product.get("brand"),"image_score":p["image"].get("score"),"image_provider":p["image"].get("provider"),"dimensions":[p["image"].get("width"),p["image"].get("height")]}} for p in pins]
    def failed_indexes(review,pin_count):
        approved={int(x) for x in (review.get("approved_indexes") or []) if str(x).isdigit()}
        return {i for i in range(1,pin_count+1) if i not in approved}
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
                pins.append({"pin_number":i,"strategy":strategy,"seo":seo[i-1],"image":best,"image_ref":ref,"candidate_count":len(candidates),"candidate_pool":candidates[:12],"candidate_cursor":0})
            review=None; recovery_rounds=0
            while True:
                review_items=build_review_items(pins,product)
                job_store.update(job_id,progress="Internal image review" if not recovery_rounds else f"Internal re-review after automatic recovery round {recovery_rounds}")
                review=await review_batch(review_items,composio_run=budgeted_run if (getattr(agent_mod,"COMPOSIO_API_KEY","") and not XAI_API_KEY) else None)
                failed=failed_indexes(review,len(pins))
                # Partial approval is sufficient. A fifth rejected/missing Pin never blocks valid Pins.
                if not failed or review.get("final_reviewer")=="deterministic":
                    break
                if recovery_rounds>=MAX_RECOVERY_ROUNDS:
                    break
                replaced=0; refreshed=False
                for idx in sorted(failed):
                    p=pins[idx-1]; pool=p.get("candidate_pool") or []; cursor=int(p.get("candidate_cursor") or 0); next_candidate=None
                    while cursor+1<len(pool):
                        cursor+=1; c=pool[cursor]; u=c.get("url")
                        if not u or u not in used:next_candidate=c;break
                    p["candidate_cursor"]=cursor
                    if not next_candidate and not refreshed and b.image_search_invocations+3<=MAX_IMAGE_SEARCH_CALLS:
                        extra=await choose_candidates(product,p["strategy"],idx,used,agent_mod); refreshed=True
                        if extra:
                            p["candidate_pool"]=(pool or [])+extra; pool=p["candidate_pool"]; cursor=int(p.get("candidate_cursor") or 0)
                            while cursor+1<len(pool):
                                cursor+=1; c=pool[cursor]; u=c.get("url")
                                if not u or u not in used:next_candidate=c;break
                            p["candidate_cursor"]=cursor
                    if next_candidate:
                        old=p["image"]; old_url=old.get("url")
                        if old_url:used.discard(old_url)
                        p["image"]=next_candidate
                        p["image_ref"]=next_candidate.get("url") or ("data:image/jpeg;base64,"+str(next_candidate.get("value") or ""))
                        p["candidate_count"]=len(p.get("candidate_pool") or [])
                        if next_candidate.get("url"):used.add(next_candidate["url"])
                        resources.add(next_candidate.get("provider") or "unknown"); replaced+=1
                if replaced==0:break
                recovery_rounds+=1
            published=[]; errors=[]
            approved_indexes=set(int(x) for x in (review.get("approved_indexes") or []) if str(x).isdigit())
            for p in pins:
                if p["pin_number"] not in approved_indexes:
                    errors.append({"pin_number":p["pin_number"],"error":"Rejected by visual quality reviewer; not published"})
                    continue
                s=p["seo"]; im=p["image"]
                try:
                    dest_url=(product.get("url") or product.get("affiliate_url") or url); r=await agent_mod.publish_and_verify(board_id,s["title"],s["description"],s["alt_text"],im.get("mode","url"),im.get("value") or im.get("url"),dest_url,job_store,job_id,p["pin_number"])
                    published.append({"pin_number":p["pin_number"],"strategy":p["strategy"]["name"],"image_provider":im.get("provider"),"image_id":im.get("id"),"image_score":im.get("score"),"dimensions":[im.get("width"),im.get("height")],"candidate_count":p["candidate_count"],"title":s["title"],"keywords":s.get("keywords"),**r})
                except Exception as e:errors.append({"pin_number":p["pin_number"],"error":str(e)})
            if not published:raise RuntimeError("No Pins were successfully published and verified.")
            return {"product_name":product.get("name"),"source_url":url,"affiliate_url":product.get("url") or product.get("affiliate_url") or url,"category":product.get("category"),"capabilities":await _static_capabilities(agent_mod),"resources_used":sorted(resources),"pins_planned":5,"pins_published":len(published),"pins_failed":len(errors),"failed_pin_indexes":[e.get("pin_number") for e in errors],"board_id":board_id,"pins":published,"errors":errors,"ai_quality_review":review,"recovery_rounds":recovery_rounds,"composio_call_budget":{"used":b.used,"maximum":b.maximum,"remaining":b.maximum-b.used,"image_search_calls":b.image_search_invocations,"external_visual_review_calls":0},"summary":f"{len(published)}/5 Pins published and independently verified; successful Pins preserved and failed Pins reported."}
        finally:_call_budget.reset(token)
    agent_mod.run_composio_tool=budgeted_run; agent_mod.publish_and_verify=strict_publish; agent_mod.probe_capabilities=lambda:_static_capabilities(agent_mod); agent_mod.research_product=research; agent_mod.select_or_create_board=board; agent_mod.process_pinterest_job=process
    logger.info("Zero-tolerance image sourcing + multi-angle comparison + adaptive recovery + 40-call budget + strict board routing/verification wiring applied")
