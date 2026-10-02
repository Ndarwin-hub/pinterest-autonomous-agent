"""Dedicated Railway Pin A worker.

The worker owns the full Pin A execution path: discovery, product job execution,
image selection, Pinterest publication and verification. The web service remains
the durable SQLite ledger/API side only. Video A is not started from this worker.
"""
from __future__ import annotations
import asyncio
import logging
import os
import uuid
import httpx

import agent as agent_module
import quality_patch
QUALITY_PATCH_VERSION = quality_patch.install_identity(agent_module)
from wire_board_org import apply_agent_wiring
apply_agent_wiring(agent_module)
import runtime_hardening
runtime_hardening.install(agent_module)
import provider_failover
provider_failover.install(__import__("wire_board_org"), __import__("ai_quality_gate"))
quality_patch.install_process_gate(agent_module)
import publication_guard
import image_priority
import image_diversity_guard
publication_guard.install(agent_module)
# Railway canonical image selector: hard integrity/diversity gate wraps it below.
image_priority.install(agent_module)
image_diversity_guard.install(agent_module)

from agent import process_pinterest_job
from models import JobStore, JobStatus, Job
from pin_config import PINS_PER_PRODUCT
from published_registry import extract_asin
from pin_a_remote_ledger import RemotePinALedger
from amazon_scheduler import amazon_scheduler as scheduler, SCHEDULER_MODE

logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger=logging.getLogger("pinterest-agent.pin-a-worker")

WEB_URL=os.getenv("PIN_A_WEB_URL","").rstrip("/")
WORKER_SECRET=os.getenv("PIN_A_WORKER_SECRET","").strip()
if not WEB_URL or not WORKER_SECRET:
    raise RuntimeError("PIN_A_WEB_URL and PIN_A_WORKER_SECRET are required")

os.makedirs(os.getenv("DATA_DIR","/tmp/pin-a-worker"),exist_ok=True)
ledger=RemotePinALedger(WEB_URL,WORKER_SECRET)
import amazon_scheduler as scheduler_module
scheduler_module.ledger=ledger
job_store=JobStore()
_tasks:dict[str,asyncio.Task]={}
_http=httpx.AsyncClient(timeout=float(os.getenv("PIN_A_WEB_HTTP_TIMEOUT_SEC","60")))

async def _run_job(job_id:str,url:str):
    try:
        job_store.update(job_id,status=JobStatus.RUNNING,progress=f"Starting {PINS_PER_PRODUCT}-pin workflow")
        result=await process_pinterest_job(job_id,url,job_store)
        success=bool(result.get("pins_published"))
        ledger.quota_record_job(success)
        try:
            result["quota"]=ledger.quota_snapshot()
        except Exception:
            pass
        supervisor_status=str(result.get("pin_supervisor_status") or "")
        published_count=int(result.get("pins_published") or 0)
        verified_count=int(result.get("verified_pins") or sum(1 for p in (result.get("pins") or []) if isinstance(p,dict) and p.get("verified")))
        if supervisor_status=="completed" or verified_count>=PINS_PER_PRODUCT:
            final_status=JobStatus.COMPLETED
        elif supervisor_status=="completed_partial" or verified_count>0 or published_count>0:
            final_status=JobStatus.COMPLETED_PARTIAL
        else:
            final_status=JobStatus.FAILED
        job_store.update(job_id,status=final_status,progress="Finished",result=result)
        logger.info("Pin A worker job=%s status=%s verified=%s published=%s",job_id,final_status.value,verified_count,published_count)
    except Exception as exc:
        try: ledger.quota_record_job(False)
        except Exception: pass
        logger.exception("Pin A worker job=%s failed",job_id)
        job_store.update(job_id,status=JobStatus.FAILED,progress="Failed",error=str(exc))

async def enqueue(url:str,target_board_id:str|None=None,target_board_name:str|None=None,force_new:bool=False):
    existing=job_store.find_by_url(url)
    if existing and not force_new:
        return {"job_id":existing.job_id,"status":existing.status.value,"message":"Existing Pin A worker job reused"}
    if not ledger.quota_reserve_job():
        return {"status":"failed","error":"monthly_safe_pinterest_capacity_reached"}
    job_id=str(uuid.uuid4())
    job=Job(job_id=job_id,url=url,status=JobStatus.QUEUED,progress=f"Pin A worker queued — {PINS_PER_PRODUCT} Pins",target_board_id=target_board_id,target_board_name=target_board_name)
    job_store.save(job)
    _tasks[job_id]=asyncio.create_task(_run_job(job_id,url),name=f"pin-a-job-{job_id}")
    return {"job_id":job_id,"status":JobStatus.QUEUED.value,"message":"Pin A job executing on dedicated worker"}

async def wait_job(job_id:str):
    for _ in range(180):
        await asyncio.sleep(10)
        job=job_store.get(job_id)
        if not job:
            return {"status":"missing"}
        if job.status.value in ("completed","completed_partial","failed"):
            return {"status":job.status.value,"error":job.error,"result":job.result}
    return {"status":"timeout"}

async def list_boards():
    data=await agent_module.run_composio_tool("PINTEREST_LIST_BOARDS",{})
    return data.get("items") or data.get("boards") or []

async def request_loop():
    while True:
        req=None
        try:
            if not getattr(request_loop,"_recovered",False):
                recovered=ledger.recover_pin_a_requests()
                request_loop._recovered=True
                if recovered:
                    logger.warning("Recovered %s interrupted Pin A request(s)",recovered)
            req=ledger.claim_next_pin_a_request()
            if not req:
                await asyncio.sleep(5)
                continue
            request_id=req["request_id"]
            logger.info("Dedicated Pin A worker claimed request_id=%s day=%s source=%s",request_id,req["day"],req["source"])
            result=await scheduler.start_daily_session(enqueue=enqueue,list_boards=list_boards,wait_job=wait_job,trigger_batch=1)
            status=str(result.get("status") or "")
            if status in ("session_started","already_running","already_completed"):
                ledger.finish_pin_a_request(request_id,"completed")
            elif status=="ignored":
                ledger.finish_pin_a_request(request_id,"failed",str(result.get("reason") or "scheduler_ignored"))
            else:
                ledger.finish_pin_a_request(request_id,"pending",f"dispatch returned {status or 'unknown'}")
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Dedicated Pin A worker loop error request_id=%s: %s",(req or {}).get("request_id"),exc)
            if req:
                ledger.finish_pin_a_request(req["request_id"],"pending",str(exc)[:1000])
            await asyncio.sleep(5)

async def _health_handler(reader,writer):
    try:
        await reader.read(2048)
        body=b'{"status":"ok","service":"pin-a-worker"}'
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "+str(len(body)).encode()+b"\r\nConnection: close\r\n\r\n"+body)
        await writer.drain()
    finally:
        writer.close()
        try: await writer.wait_closed()
        except Exception: pass

async def main():
    logger.info("Dedicated Pin A worker ONLINE executor=%s quality_patch=%s",os.getenv("RAILWAY_SERVICE_NAME"),QUALITY_PATCH_VERSION)
    health_server=await asyncio.start_server(_health_handler,"0.0.0.0",int(os.getenv("PORT","8080")))
    task=asyncio.create_task(request_loop(),name="pin-a-request-loop")
    try:
        await task
    finally:
        task.cancel()
        try: await task
        except asyncio.CancelledError: pass
        health_server.close()
        await health_server.wait_closed()
        for t in list(_tasks.values()):
            t.cancel()
        await _http.aclose()

if __name__=="__main__":
    asyncio.run(main())
