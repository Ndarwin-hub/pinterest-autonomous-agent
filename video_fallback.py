from __future__ import annotations
import asyncio, json, logging, os, shutil, uuid
from pathlib import Path
from daily_ledger import ledger as daily_ledger, BATCH_SIZE
from video_renderer import render_video, affiliate_url, make_music
from video_publisher import publish_video
from kaggle_video_handshake import get_pair_run, run_is_stale, pair_product_statuses
from composio_video_verify import verify_run

log=logging.getLogger(__name__)
PAIR_STARTS=(1,3,5,7,9);PLATFORMS=("facebook","instagram","youtube","x","tiktok");ACTIVE={};SINGLE_ACTIVE=set()
_ORIGINAL_CLAIM=daily_ledger.claim_video_job;_ORIGINAL_MARK=daily_ledger.mark_video_job

def _claim_video_job_with_run(day,batch,slot,owner):
    normalized=owner
    if str(owner).startswith("kaggle-"): normalized=f"kaggle-{day}-pair-{((int(batch)-1)//2)*2+1}"
    return _ORIGINAL_CLAIM(day,batch,slot,normalized)

def _mark_video_job_with_run(day,batch,slot,status,owner=None,error=None,publication_json=None):
    normalized=owner
    if owner and str(owner).startswith("kaggle-"): normalized=f"kaggle-{day}-pair-{((int(batch)-1)//2)*2+1}"
    return _ORIGINAL_MARK(day,batch,slot,status,normalized,error,publication_json)

daily_ledger.claim_video_job=_claim_video_job_with_run;daily_ledger.mark_video_job=_mark_video_job_with_run

def _pair_for(start):return (start,start+1)
def _next_pointer():return int(daily_ledger.get_video_pair_state().get("next_pair_start") or 1)
def _kaggle_decision(day,start):
    run=get_pair_run(day,start)
    if not run:return "missing",None
    status=str(run.get("status") or "").upper()
    if status=="COMPLETED":return "completed",run
    if status=="RUNNING" and not run_is_stale(run):return "running",run
    if status in {"FAILED","INCOMPLETE"} or run_is_stale(run):return "fallback",run
    return "unknown",run

async def on_pinterest_batch_start(day,batch):
    start=_next_pointer()
    if start not in PAIR_STARTS or int(batch)!=start+1:return {"status":"ignored","reason":"not_current_video_pair","next_pair_start":start}
    key=f"{day}:{start}"
    if ACTIVE.get(key):return {"status":"already_active","pair":_pair_for(start)}
    decision,run=_kaggle_decision(day,start)
    if decision=="completed":
        internal=pair_product_statuses(day,start,run.get("run_id"));external=verify_run(run.get("run_id"))
        if internal.get("complete") and external.get("available") and external.get("complete"):
            daily_ledger.advance_video_pair(start,day,reason="kaggle_completed_composio_verified");return {"status":"kaggle_completed_verified","pair":_pair_for(start),"verification":{"internal":internal,"composio":external}}
        if not external.get("available"):return {"status":"kaggle_completed_unverified","pair":_pair_for(start),"verification":{"internal":internal,"composio":external}}
        task=asyncio.create_task(_run_pair(day,start,run.get("run_id")));ACTIVE[key]=task;return {"status":"railway_fallback_started","pair":_pair_for(start),"kaggle_run_id":run.get("run_id"),"verification":{"internal":internal,"composio":external}}
    if decision=="running":return {"status":"kaggle_running","pair":_pair_for(start),"run_id":run.get("run_id")}
    if decision=="unknown":return {"status":"kaggle_status_unknown","pair":_pair_for(start)}
    task=asyncio.create_task(_run_pair(day,start,run.get("run_id") if run else None));ACTIVE[key]=task;return {"status":"railway_fallback_started","pair":_pair_for(start),"kaggle_run_id":run.get("run_id") if run else None}

async def on_kaggle_job_failure(day,batch,slot,error=None):
    """Take over exactly one failed Kaggle video job; never re-run successful Kaggle jobs."""
    key=f"{day}:{int(batch)}:{int(slot)}"
    if key in SINGLE_ACTIVE:return {"status":"already_active","batch":int(batch),"slot":int(slot)}
    rows=daily_ledger.video_batch(day,int(batch))
    row=next((r for r in rows if int(r.get("slot") or 0)==int(slot)),None)
    if not row:return {"status":"ignored","reason":"video_job_not_found","batch":int(batch),"slot":int(slot)}
    if _terminal(row) and str(row.get("status")).lower()=="completed":return {"status":"already_completed","batch":int(batch),"slot":int(slot)}
    owner=f"railway-video-fallback-{int(batch)}-{int(slot)}-{uuid.uuid4().hex[:8]}"
    claim=daily_ledger.claim_video_job(day,int(batch),int(slot),owner)
    if not claim or not claim.get("claimed"):return {"status":"claim_lost","batch":int(batch),"slot":int(slot)}
    SINGLE_ACTIVE.add(key)
    try:
        await asyncio.to_thread(_process_product,day,((int(batch)-1)//2)*2+1,int(batch),row,owner,None,None)
        return {"status":"railway_fallback_completed","batch":int(batch),"slot":int(slot)}
    except Exception as exc:
        daily_ledger.mark_video_job(day,int(batch),int(slot),"failed",owner=owner,error=str(exc)[:1000])
        return {"status":"railway_fallback_failed","batch":int(batch),"slot":int(slot),"error":str(exc)[:1000]}
    finally:
        SINGLE_ACTIVE.discard(key)

async def _run_pair(day,start,kaggle_run_id=None):
    key=f"{day}:{start}";owner=f"railway-video-{start}-{uuid.uuid4().hex[:8]}"
    try:
        daily_ledger.start_video_pair(day,start,owner);external=verify_run(kaggle_run_id) if kaggle_run_id else {"available":False,"products":{}};results=[await _run_batch(day,start,batch,owner,kaggle_run_id,external) for batch in _pair_for(start)]
        if all(results):daily_ledger.advance_video_pair(start,day,reason="railway_pair_processed")
        else:daily_ledger.finish_video_pair(day,start,owner,"incomplete","One or more pair batches had no durable video handoff jobs")
    except Exception as exc:log.exception("Railway video pair %s failed: %s",start,exc);daily_ledger.finish_video_pair(day,start,owner,"failed",str(exc)[:1000])
    finally:ACTIVE.pop(key,None)

def _terminal(row):return str(row.get("status") or "").lower() in {"completed","failed"}
async def _run_batch(day,pair_start,batch,owner,kaggle_run_id=None,external=None):
    rows=daily_ledger.video_batch(day,batch)
    if not rows:log.warning("Video pair %s: batch %s has no durable Pinterest video handoff; pair will not advance",pair_start,batch);return False
    processed=0; failures=0
    for row in rows[:BATCH_SIZE]:
        if _terminal(row):
            if str(row.get("status") or "").lower()=="completed": processed+=1
            else: failures+=1
            continue
        claim=daily_ledger.claim_video_job(day,batch,row["slot"],owner)
        if not claim or not claim.get("claimed"):
            failures+=1
            continue
        try:
            await asyncio.to_thread(_process_product,day,pair_start,batch,row,owner,kaggle_run_id,external)
            processed+=1
        except Exception as exc:
            failures+=1
            log.exception("Video product failed day=%s batch=%s slot=%s asin=%s: %s",day,batch,row.get("slot"),row.get("asin"),exc)
            daily_ledger.mark_video_job(day,batch,row["slot"],"failed",owner=owner,error=str(exc)[:1000])
    return failures==0 and processed==len(rows[:BATCH_SIZE])

def _resolve_product_images(row):
    """Recover exact-product images when the Pinterest->video handoff is absent.
    Never fall back to generic brand/web images: every recovery source is tied to
    the exact Amazon product URL/ASIN.
    """
    urls=[]
    try:
        raw=json.loads(row.get("image_urls_json") or "[]")
        urls=[str(u) for u in raw if isinstance(u,str) and u.startswith(("http://","https://"))]
    except Exception:
        urls=[]
    if len(urls)>=4:
        return list(dict.fromkeys(urls))[:5], "pinterest_handoff"
    product={"asin":str(row.get("asin") or "").upper(),"name":str(row.get("title") or ""), "url":str(row.get("product_url") or "")}
    recovered=[]
    # Recovery 1: exact Amazon product-gallery extraction.
    try:
        from image_quality import search_amazon_product_images
        recovered.extend([x.get("url") for x in asyncio.run(search_amazon_product_images(product)) if x.get("url")])
    except Exception as exc:
        log.warning("Video image recovery Amazon gallery failed asin=%s: %s",product["asin"],str(exc)[:300])
    # Recovery 2: exact ASIN CDN gallery.
    if len(dict.fromkeys(urls+recovered))<4:
        try:
            from image_quality import search_amazon_asin_cdn_images
            recovered.extend([x.get("url") for x in asyncio.run(search_amazon_asin_cdn_images(product)) if x.get("url")])
        except Exception as exc:
            log.warning("Video image recovery ASIN CDN failed asin=%s: %s",product["asin"],str(exc)[:300])
    # Recovery 3: verified retailer/manufacturer product pages with exact identity evidence.
    if len(dict.fromkeys(urls+recovered))<4:
        try:
            from image_quality import search_verified_web_product_page_images
            recovered.extend([x.get("url") for x in asyncio.run(search_verified_web_product_page_images(product, num=16)) if x.get("url")])
        except Exception as exc:
            log.warning("Video image recovery verified product pages failed asin=%s: %s",product["asin"],str(exc)[:300])
    merged=list(dict.fromkeys(urls+recovered))
    if len(merged)<4:
        raise RuntimeError(f"VIDEO_IMAGE_RECOVERY_FAILED: only {len(merged)} exact-product candidates available for {product['asin']}")
    # Persist the recovered handoff so a retry uses the verified set instead of
    # repeating discovery.
    try:
        daily_ledger.attach_video_images(day if False else row.get("day"), row["asin"], [{"url":u,"provider":"video_exact_product_recovery"} for u in merged[:5]])
    except Exception:
        pass
    return merged[:5], "exact_product_recovery"

def _process_product(day,pair_start,batch,row,owner,kaggle_run_id=None,external=None):
    urls,image_source=_resolve_product_images(row)
    if len(urls)<4:raise RuntimeError("VIDEO_IMAGE_HANDOFF_MISSING: fewer than 4 verified product images")
    root=Path(os.getenv("VIDEO_WORK_DIR","/data/video-runs"))/day/f"batch_{batch}"/f"slot_{row['slot']}";root.mkdir(parents=True,exist_ok=True);output=root/f"{row['asin']}.mp4"
    try:
        prior=json.loads(row.get("publication_json") or "{}") if row.get("publication_json") else {};prior_statuses=prior.get("platforms") or {};kaggle_status=(pair_product_statuses(day,pair_start,kaggle_run_id).get("products") or {}).get(str(row["asin"]).upper(),{}) if kaggle_run_id else {};composio_status=((external or {}).get("products") or {}).get(str(row["asin"]).upper(),{}) if kaggle_run_id else {}
        combined_statuses=dict(kaggle_status.get("platforms") or {});combined_statuses.update(composio_status.get("platforms") or {});combined_statuses.update(prior_statuses);failed=[p for p in PLATFORMS if p in combined_statuses and str(combined_statuses[p].get("status","")).upper() not in ("PUBLISHED","SUCCESS","SUBMITTED")];targets=failed or [p for p in PLATFORMS if p not in combined_statuses]
        if not targets and combined_statuses:daily_ledger.mark_video_job(day,batch,row["slot"],"completed",owner=owner,publication_json=json.dumps({"platforms":combined_statuses,"source":"kaggle+composio"},separators=(",",":"))[:12000]);return
        track_state=daily_ledger.next_video_music([f"track{i:02d}" for i in range(1,17)]);music=make_music(track_state["track_id"],root/"music");log.info("Video render asin=%s batch=%s slot=%s source=%s images=%s",row["asin"],batch,row["slot"],image_source,len(urls));render_video(urls,output,row.get("title") or "",music);aff=affiliate_url(row["asin"]);marker=f"[video-run:{kaggle_run_id or 'railway'}][asin:{str(row['asin']).upper()}][batch:{batch}]";caption=f"{(row.get('title') or row['asin'])[:150]}\nShop now: {aff}\n{marker}\n\nAs an Amazon Associate I earn from qualifying purchases.";result=publish_video(str(output),row.get("title") or row["asin"],caption,targets);merged=dict(combined_statuses);merged.update(result.get("platforms") or {});ok=bool(merged) and all(str(v.get("status","")).upper() in ("PUBLISHED","SUCCESS","SUBMITTED") for v in merged.values());combined=dict(result);combined["platforms"]=merged;combined["run_id"]=kaggle_run_id or "railway";combined["source"]="railway_fallback";daily_ledger.mark_video_job(day,batch,row["slot"],"completed" if ok else "failed",owner=owner,publication_json=json.dumps(combined,separators=(",",":"))[:12000])
    finally:shutil.rmtree(root,ignore_errors=True)

def pair_status(day=None):
    day=day or daily_ledger.today_str();state=daily_ledger.get_video_pair_state();start=int(state.get("next_pair_start") or 1);return {"day":day,"next_pair_start":start,"pair":_pair_for(start),"status":state.get("status")}
