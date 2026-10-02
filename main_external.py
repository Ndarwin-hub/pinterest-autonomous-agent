"""Web-service adapter for the dedicated Pin A worker.

This keeps the existing public API and volume-backed ledger on the web service,
but disables all Pin A execution there. The separate pin-a worker owns execution.
"""
from __future__ import annotations
import hmac
import os
import uuid
from typing import Any, Dict, Optional
from fastapi import Depends, Header, HTTPException

os.environ["PIN_A_EXECUTOR"]="external"

import amazon_scheduler as scheduler_module

async def _external_scheduler_start(*args, **kwargs):
    return {"status":"external_worker","message":"Pin A execution is delegated to the dedicated Railway worker."}

async def _external_scheduler_stop(*args, **kwargs):
    return None

async def _external_scheduler_stop_daily(*args, **kwargs):
    return None

async def _external_scheduler_start_daily_session(*args, **kwargs):
    import main
    day=main.daily_ledger.today_str()
    request_id=f"compat:{uuid.uuid4()}"
    req=main.daily_ledger.enqueue_pin_a_request(request_id,"amazon-run-batch",day)
    return {"status":"session_started","request_id":request_id,"day":day,"request_status":req["status"],"scheduler":"dedicated_pin_a_worker"}

scheduler_module.amazon_scheduler.start=_external_scheduler_start
scheduler_module.amazon_scheduler.stop=_external_scheduler_stop
scheduler_module.amazon_scheduler.stop_daily_session=_external_scheduler_stop_daily
scheduler_module.amazon_scheduler.start_daily_session=_external_scheduler_start_daily_session

import main

async def _disabled_pin_a_request_worker():
    while True:
        import asyncio
        await asyncio.sleep(3600)

main._pin_a_request_worker=_disabled_pin_a_request_worker

def _verify_worker(x_pin_a_worker_secret:Optional[str]=Header(None,alias="X-Pin-A-Worker-Secret")):
    expected=os.getenv("PIN_A_WORKER_SECRET","").strip()
    if not expected or not x_pin_a_worker_secret or not hmac.compare_digest(x_pin_a_worker_secret,expected):
        raise HTTPException(status_code=401,detail="Invalid or missing Pin A worker secret")
    return True

@main.app.post("/internal/pin-a/ledger")
async def pin_a_ledger_rpc(body:Dict[str,Any],_:bool=Depends(_verify_worker)):
    operation=str(body.get("operation") or "").strip()
    args=body.get("args") or {}
    allowed={
        "today_str","get_day_status","next_unfinished_batch","is_day_complete",
        "reclaim_orphaned_batches","reclaim_stale_processing","ensure_day",
        "next_recovery_slot","claim_recovery_slot","mark_slot","next_pending_slot",
        "claim_slot","try_begin_batch","complete_batch","historical_selected_asins",
        "enqueue_video_job","close_day","record_scheduler_event",
        "enqueue_pin_a_request","recover_pin_a_requests","claim_next_pin_a_request",
        "finish_pin_a_request"
    }
    try:
        if operation=="quota_reserve_job":
            result=main.quota.reserve_job()
        elif operation=="quota_record_job":
            result=main.quota.record_job(bool(args.get("success")))
        elif operation=="quota_snapshot":
            result=main.quota.snapshot()
        elif operation in allowed:
            result=getattr(main.daily_ledger,operation)(**args)
        else:
            raise HTTPException(status_code=400,detail="Unsupported Pin A ledger operation")
        return {"ok":True,"operation":operation,"result":result}
    except HTTPException:
        raise
    except Exception as exc:
        main.logger.exception("Pin A ledger RPC failed operation=%s: %s",operation,exc)
        raise HTTPException(status_code=500,detail=str(exc)[:1000])

app=main.app

if __name__=="__main__":
    import uvicorn
    uvicorn.run(app,host="0.0.0.0",port=int(os.getenv("PORT","8080")))
