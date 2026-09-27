from __future__ import annotations
import asyncio, json, logging, os, shutil, uuid
from pathlib import Path
from daily_ledger import ledger as daily_ledger, BATCH_SIZE
from video_renderer import render_video, affiliate_url, make_music
from video_publisher import publish_video
from kaggle_video_handshake import get_pair_run, run_is_stale, pair_product_statuses
from composio_video_verify import verify_run

log=logging.getLogger(__name__)
PAIR_STARTS=(1,3,5,7,9)
PLATFORMS=("facebook","instagram","youtube","x","tiktok")
ACTIVE={}

def _pair_for(start): return (start,start+1)
def _next_pointer(): return int(daily_ledger.get_video_pair_state().get("next_pair_start") or 1)

def _kaggle_decision(day,start):
    run=get_pair_run(day,start)
    if not run: return "missing",None
    status=str(run.get("status") or "").upper()
    if status=="COMPLETED": return "completed",run
    if status=="RUNNING" and not run_is_stale(run): return "running",run
    if status in {"FAILED","INCOMPLETE"} or run_is_stale(run): return "fallback",run
    return "unknown",run

async def on_pinterest_batch_start(day,batch):
    start=_next_pointer()
    if start not in PAIR_STARTS or int(batch)!=start+1:
        return {"status":"ignored","reason":"not_current_video_pair","next_pair_start":start}
    key=f"{day}:{start}"
    if ACTIVE.get(key): return {"status":"already_active","pair":_pair_for(start)}
    decision,run=_kaggle_decision(day,start)
    if decision=="completed":
        internal=pair_product_statuses(day,start,run.get("run_id"))
        external=verify_run(run.get("run_id"))
        if external.get("available") and external.get("complete"):
            daily_ledger.advance_video_pair(start,day,reason="kaggle_completed_composio_verified")
            return {"status":"kaggle_completed_verified","pair":_pair_for(start),"verification":{"internal":internal,"composio":external}}
        if not external.get("available"):
            return {"status":"kaggle_completed_unverified","pair":_pair_for(start),"verification":{"internal":internal,"composio":external}}
        task=asyncio.create_task(_run_pair(day,start,run.get("run_id")))
        ACTIVE[key]=task
        return {"status":"railway_fallback_started","pair":_pair_for(start),"kaggle_run_id":run.get("run_id"),"verification":{"internal":internal,"composio":external}}
    if decision=="running":
        return {"status":"kaggle_running","pair":_pair_for(start),"run_id":run.get("run_id")}
    if decision=="unknown":
        return {"status":"kaggle_status_unknown","pair":_pair_for(start)}
    task=asyncio.create_task(_run_pair(day,start,run.get("run_id") if run else None))
    ACTIVE[key]=task
    return {"status":"railway_fallback_started","pair":_pair_for(start),"kaggle_run_id":run.get("run_id") if run else None}

async def _run_pair(day,start,kaggle_run_id=None):
    key=f"{day}:{start}"
    owner=f"railway-video-{start}-{uuid.uuid4().hex[:8]}"
    try:
        daily_ledger.start_video_pair(day,start,owner)
        external=verify_run(kaggle_run_id) if kaggle_run_id else {"available":False,"products":{}}
        for batch in _pair_for(start):
            await _run_batch(day,start,batch,owner,kaggle_run_id,external)
        daily_ledger.advance_video_pair(start,day,reason="railway_pair_processed")
    except Exception as exc:
        log.exception("Railway video pair %s failed: %s",start,exc)
        daily_ledger.finish_video_pair(day,start,owner,"failed",str(exc)[:1000])
    finally:
        ACTIVE.pop(key,None)

def _terminal(row): return str(row.get("status") or "").lower() in {"completed","failed"}

async def _run_batch(day,pair_start,batch,owner,kaggle_run_id=None,external=None):
    rows=daily_ledger.video_batch(day,batch)
    if not rows:
        log.info("Video pair %s: batch %s has no handoff jobs; leaving it untouched",pair_start,batch)
        return
    for row in rows[:BATCH_SIZE]:
        if _terminal(row): continue
        claim=daily_ledger.claim_video_job(day,batch,row["slot"],owner)
        if not claim or not claim.get("claimed"): continue
        try:
            await asyncio.to_thread(_process_product,day,pair_start,batch,row,owner,kaggle_run_id,external)
        except Exception as exc:
            daily_ledger.mark_video_job(day,batch,row["slot"],"failed",owner=owner,error=str(exc)[:1000])

def _process_product(day,pair_start,batch,row,owner,kaggle_run_id=None,external=None):
    urls=json.loads(row.get("image_urls_json") or "[]")
    if len(urls)<4: raise RuntimeError("VIDEO_IMAGE_HANDOFF_MISSING: fewer than 4 Pinterest-associated images")
    root=Path(os.getenv("VIDEO_WORK_DIR","/data/video-runs"))/day/f"batch_{batch}"/f"slot_{row['slot']}"
    root.mkdir(parents=True,exist_ok=True); output=root/f"{row['asin']}.mp4"
    try:
        prior=json.loads(row.get("publication_json") or "{}") if row.get("publication_json") else {}
        prior_statuses=prior.get("platforms") or {}
        kaggle_status=(pair_product_statuses(day,pair_start,kaggle_run_id).get("products") or {}).get(str(row["asin"]).upper(),{}) if kaggle_run_id else {}
        composio_status=((external or {}).get("products") or {}).get(str(row["asin"]).upper(),{}) if kaggle_run_id else {}
        combined_statuses=dict(kaggle_status.get("platforms") or {})
        combined_statuses.update(composio_status.get("platforms") or {})
        combined_statuses.update(prior_statuses)
        failed=[p for p in PLATFORMS if p in combined_statuses and str(combined_statuses[p].get("status","")).upper() not in ("PUBLISHED","SUCCESS","SUBMITTED")]
        targets=failed or [p for p in PLATFORMS if p not in combined_statuses]
        if not targets and combined_statuses:
            daily_ledger.mark_video_job(day,batch,row["slot"],"completed",owner=owner,publication_json=json.dumps({"platforms":combined_statuses,"source":"kaggle+composio"},separators=(",",":"))[:12000]); return
        track_state=daily_ledger.next_video_music([f"track{i:02d}" for i in range(1,17)])
        music=make_music(track_state["track_id"],root/"music")
        render_video(urls,output,row.get("title") or "",music)
        aff=affiliate_url(row["asin"])
        marker=f"[video-run:{kaggle_run_id or 'railway'}][asin:{str(row['asin']).upper()}][batch:{batch}]"
        caption=(f"{(row.get('title') or row['asin'])[:150]}\nShop now: {aff}\n{marker}\n\nAs an Amazon Associate I earn from qualifying purchases.")
        result=publish_video(str(output),row.get("title") or row["asin"],caption,targets)
        merged=dict(combined_statuses); merged.update(result.get("platforms") or {})
        ok=bool(merged) and all(str(v.get("status","")).upper() in ("PUBLISHED","SUCCESS","SUBMITTED") for v in merged.values())
        combined=dict(result); combined["platforms"]=merged; combined["run_id"]=kaggle_run_id or "railway"; combined["source"]="railway_fallback"
        daily_ledger.mark_video_job(day,batch,row["slot"],"completed" if ok else "failed",owner=owner,publication_json=json.dumps(combined,separators=(",",":"))[:12000])
    finally:
        shutil.rmtree(root,ignore_errors=True)

def pair_status(day=None):
    day=day or daily_ledger.today_str(); state=daily_ledger.get_video_pair_state(); start=int(state.get("next_pair_start") or 1)
    return {"day":day,"next_pair_start":start,"pair":_pair_for(start),"status":state.get("status")}
