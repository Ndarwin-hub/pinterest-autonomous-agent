from __future__ import annotations
import asyncio, json, logging, os, tempfile
from pathlib import Path
from daily_ledger import ledger as daily_ledger, BATCH_SIZE
from video_renderer import render_video, affiliate_url, make_music
from video_publisher import publish_video

log=logging.getLogger(__name__)
PAIR_STARTS=(1,3,5,7,9)
PLATFORMS=("facebook","instagram","youtube","x","tiktok")
ACTIVE={}

def _pair_for(start):
    return (start,start+1)

def _is_kaggle_active(rows):
    return any(r.get("status")=="processing" and str(r.get("claim_owner") or "").startswith("kaggle-") for r in rows)

def _kaggle_pair_complete(rows):
    # Only a durable completed state counts. Notebook stopped/returned without jobs is not success.
    return len(rows)==BATCH_SIZE*2 and all(r.get("status")=="completed" and str(r.get("claim_owner") or "").startswith("kaggle-") for r in rows)

def _pair_rows(day,start):
    rows=[]
    for batch in _pair_for(start):
        rows.extend(daily_ledger.video_batch(day,batch))
    return rows

def _next_pointer():
    return int(daily_ledger.get_video_pair_state().get("next_pair_start") or 1)

async def on_pinterest_batch_start(day,batch):
    start=_next_pointer()
    if start not in PAIR_STARTS or batch != start+1:
        return {"status":"ignored","reason":"not_current_video_pair","next_pair_start":start}
    key=f"{day}:{start}"
    if ACTIVE.get(key):
        return {"status":"already_active","pair":_pair_for(start)}
    rows=_pair_rows(day,start)
    if _kaggle_pair_complete(rows):
        daily_ledger.advance_video_pair(start,day,reason="kaggle_completed")
        return {"status":"kaggle_completed","pair":_pair_for(start)}
    if _is_kaggle_active(rows):
        return {"status":"kaggle_running","pair":_pair_for(start)}
    # Kaggle has not completed this pair and is not actively rendering this pair.
    # Railway now owns its own scheduled pair. No random selection and no catch-up.
    task=asyncio.create_task(_run_pair(day,start))
    ACTIVE[key]=task
    return {"status":"railway_fallback_started","pair":_pair_for(start)}

async def _run_pair(day,start):
    key=f"{day}:{start}"
    owner=f"railway-video-{start}-{os.urandom(4).hex()}"
    try:
        daily_ledger.start_video_pair(day,start,owner)
        for batch in _pair_for(start):
            deadline=asyncio.get_running_loop().time()+int(os.getenv("VIDEO_BATCH_READY_WAIT_SEC","1800"))
            while not daily_ledger.video_batch(day,batch) and asyncio.get_running_loop().time()<deadline:
                await asyncio.sleep(int(os.getenv("VIDEO_BATCH_READY_CHECK_SEC","30")))
            await _run_batch(day,start,batch,owner)
        daily_ledger.advance_video_pair(start,day,reason="railway_pair_processed")
    except Exception as exc:
        log.exception("Railway video pair %s failed: %s",start,exc)
        daily_ledger.finish_video_pair(day,start,owner,"failed",str(exc)[:1000])
    finally:
        ACTIVE.pop(key,None)

async def _run_batch(day,pair_start,batch,owner):
    rows=daily_ledger.video_batch(day,batch)
    # If this batch is not populated yet, the normal Pinterest batch-start hook will
    # call us again at its own start. We do not poll or wake independently.
    if not rows:
        log.info("Video pair %s: batch %s not yet populated; waiting for its normal Pinterest trigger",pair_start,batch)
        return
    for row in rows[:BATCH_SIZE]:
        if row.get("status")=="completed":
            continue
        claim=daily_ledger.claim_video_job(day,batch,row["slot"],owner)
        if not claim or not claim.get("claimed"):
            continue
        try:
            await asyncio.to_thread(_process_product,day,pair_start,batch,row,owner)
        except Exception as exc:
            daily_ledger.mark_video_job(day,batch,row["slot"],"failed",owner=owner,error=str(exc)[:1000])

def _process_product(day,pair_start,batch,row,owner):
    urls=json.loads(row.get("image_urls_json") or "[]")
    if len(urls)<4:
        raise RuntimeError("VIDEO_IMAGE_HANDOFF_MISSING: fewer than 4 Pinterest-associated images")
    root=Path(os.getenv("VIDEO_WORK_DIR","/data/video-runs"))/day/f"batch_{batch}"/f"slot_{row['slot']}"
    root.mkdir(parents=True,exist_ok=True)
    output=root/f"{row['asin']}.mp4"
    prior=json.loads(row.get("publication_json") or "{}") if row.get("publication_json") else {}
    prior_statuses=prior.get("platforms") or {}
    failed=[p for p in PLATFORMS if p in prior_statuses and str(prior_statuses[p].get("status","")).upper() not in ("PUBLISHED","SUCCESS","SUBMITTED")]
    targets=failed or [p for p in PLATFORMS if p not in prior_statuses]
    if not targets and prior_statuses:
        daily_ledger.mark_video_job(day,batch,row["slot"],"completed",owner=owner,publication_json=json.dumps(prior,separators=(",",":"))[:12000])
        return
    track_state=daily_ledger.next_video_music([f"track{i:02d}" for i in range(1,17)])
    music=make_music(track_state["track_id"],root/"music")
    try:
        render_video(urls,output,row.get("title") or "",music)
        aff=affiliate_url(row["asin"])
        caption=(f"{(row.get('title') or row['asin'])[:150]}\n"
                 f"Shop now: {aff}\n\n"
                 "As an Amazon Associate I earn from qualifying purchases.")
        result=publish_video(str(output),row.get("title") or row["asin"],caption,targets)
        merged=dict(prior_statuses)
        merged.update(result.get("platforms") or {})
        combined=dict(result); combined["platforms"]=merged
        ok=bool(merged) and all(str(v.get("status","")).upper() in ("PUBLISHED","SUCCESS","SUBMITTED") for v in merged.values())
        daily_ledger.mark_video_job(day,batch,row["slot"],"completed" if ok else "failed",owner=owner,publication_json=json.dumps(combined,separators=(",",":"))[:12000])
        if output.exists(): output.unlink()
        if root.exists():
            for p in sorted(root.rglob("*"),reverse=True):
                if p.is_file(): p.unlink(missing_ok=True)
                elif p.is_dir():
                    try:p.rmdir()
                    except OSError:pass
    finally:
        # Keep durable DB state; delete only disposable media.
        pass

def pair_status(day=None):
    day=day or daily_ledger.today_str()
    state=daily_ledger.get_video_pair_state()
    return {"day":day,"next_pair_start":state.get("next_pair_start"),"pair":_pair_for(int(state.get("next_pair_start") or 1))}
