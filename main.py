"""Autonomous Pinterest Agent - Railway service.
The existing /submit URL->5-pin workflow is unchanged; Amazon automation is additive and dormant without credentials.
"""
import os,uuid,re,logging,asyncio
from datetime import datetime,timezone
from typing import Optional,Dict,Any
from contextlib import asynccontextmanager
from fastapi import FastAPI,BackgroundTasks,HTTPException,Header,Depends
from pydantic import BaseModel,Field
from dotenv import load_dotenv
load_dotenv()
import agent as agent_module
from wire_board_org import apply_agent_wiring
from quota import quota
from mcp_bridge import router as mcp_router,MCP_PATH,register_custom_mcp_with_retry
apply_agent_wiring(agent_module)
import provider_failover
provider_failover.install(__import__("wire_board_org"),__import__("ai_quality_gate"))
from agent import process_pinterest_job
from models import JobStore,JobStatus,Job
from published_registry import registry,extract_asin
from amazon_client import amazon_credentials_present
from amazon_scheduler import amazon_scheduler
from amazon_boards import REQUIRED_PRIMARY_SLOTS
from daily_ledger import ledger as daily_ledger
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger=logging.getLogger("pinterest-agent"); job_store=JobStore(); _enqueue_lock=asyncio.Lock(); API_SECRET=os.getenv("API_SECRET","").strip()
def verify_secret(x_api_secret:Optional[str]=Header(None)):
    if API_SECRET and x_api_secret!=API_SECRET: raise HTTPException(status_code=401,detail="Invalid or missing API secret")
    return True
def extract_url(text:str)->str:
    text=(text or "").strip(); m=re.search(r"https?://\S+",text)
    if m:return m.group(0).rstrip(").,]',\"")
    if text.startswith("http"):return text
    raise ValueError("No valid http(s) URL found")
@asynccontextmanager
async def lifespan(app:FastAPI):
    logger.info("Pinterest Autonomous Agent v3 starting..."); logger.info("Quota governor: %s",quota.snapshot()); logger.info("Amazon layer credentials_present=%s",amazon_credentials_present())
    registration_task=None
    if MCP_PATH: registration_task=asyncio.create_task(register_custom_mcp_with_retry())
    else: logger.warning("MCP bridge disabled: MCP_BRIDGE_TOKEN is not configured")
    async def _enqueue_for_amazon(url:str):
        class _BG:
            def add_task(self,fn,*args): asyncio.create_task(fn(*args))
        r=await enqueue_job(url,_BG()); return {"job_id":r.job_id,"status":r.status,"message":r.message}
    async def _list_boards_for_amazon():
        try:
            data=await agent_module.run_composio_tool("PINTEREST_LIST_BOARDS",{})
            return data.get("items") or data.get("boards") or []
        except Exception as e: logger.warning("Amazon board listing failed: %s",e); return []
    async def _wait_job(job_id:str):
        for _ in range(180):
            await asyncio.sleep(10); job=job_store.get(job_id)
            if not job:return {"status":"missing"}
            if job.status.value in ("completed","failed"):return {"status":job.status.value,"error":job.error,"result":job.result}
        return {"status":"timeout"}
    await amazon_scheduler.start(enqueue=_enqueue_for_amazon,list_boards=_list_boards_for_amazon,wait_job=_wait_job)
    yield
    await amazon_scheduler.stop()
    if registration_task:
        registration_task.cancel()
        try: await registration_task
        except asyncio.CancelledError: pass
    logger.info("Shutting down...")
app=FastAPI(title="Pinterest Autonomous Agent",description="Submit a product/affiliate URL. Agent researches, creates 5 unique Pins with multi-provider images, publishes and verifies.",version="3.5.0",lifespan=lifespan)
if MCP_PATH: app.include_router(mcp_router,prefix=MCP_PATH)
class SubmitRequest(BaseModel): url:str=Field(...,description="Product/affiliate URL. Exact URL preserved as destination for all pins.")
class SubmitResponse(BaseModel): job_id:str; status:str; message:str
class StatusResponse(BaseModel): job_id:str; status:str; progress:Optional[str]=None; result:Optional[Dict[str,Any]]=None; error:Optional[str]=None; created_at:str; updated_at:str
async def enqueue_job(url_str:str,background_tasks:BackgroundTasks)->SubmitResponse:
    async with _enqueue_lock:
        existing=job_store.find_by_url(url_str)
        if existing:return SubmitResponse(job_id=existing.job_id,status=existing.status.value,message="Existing job reused; duplicate Pinterest workflow was not started.")
        if not quota.reserve_job(): raise HTTPException(status_code=429,detail={"message":"Monthly safe Pinterest capacity reached; job not started.","quota":quota.snapshot()})
        job_id=str(uuid.uuid4()); job=Job(job_id=job_id,url=url_str,status=JobStatus.QUEUED,progress="Job accepted — 5-pin workflow queued"); job_store.save(job); background_tasks.add_task(run_job,job_id,url_str); return SubmitResponse(job_id=job_id,status=JobStatus.QUEUED.value,message="Job accepted. 5 Pins will be researched, imaged, published and verified. Poll /status/{job_id}")
@app.get("/health")
async def health():
    return {"status":"ok","service":"pinterest-autonomous-agent","version":"3.5.0","mcp_bridge":bool(MCP_PATH),"amazon":{"credentials_present":amazon_credentials_present(),"scheduler":amazon_scheduler.status,"required_primary_boards":REQUIRED_PRIMARY_SLOTS,"published_registry_count":registry.count_success()},"time":datetime.now(timezone.utc).isoformat()}
@app.get("/quota")
async def quota_status(_:bool=Depends(verify_secret)):return quota.snapshot()
@app.post("/submit",response_model=SubmitResponse)
async def submit(body:SubmitRequest,background_tasks:BackgroundTasks,_:bool=Depends(verify_secret)):
    try:url_str=extract_url(body.url)
    except ValueError as e:raise HTTPException(status_code=400,detail=str(e))
    return await enqueue_job(url_str,background_tasks)
@app.get("/status/{job_id}",response_model=StatusResponse)
async def status(job_id:str,_:bool=Depends(verify_secret)):
    job=job_store.get(job_id)
    if not job:raise HTTPException(status_code=404,detail="Job not found")
    return StatusResponse(job_id=job.job_id,status=job.status.value,progress=job.progress,result=job.result,error=job.error,created_at=job.created_at,updated_at=job.updated_at)
@app.get("/amazon/status")
async def amazon_status(_:bool=Depends(verify_secret)):
    return {"credentials_present":amazon_credentials_present(),"scheduler":amazon_scheduler.status,"daily":daily_ledger.get_day_status(),"published_count":registry.count_success(),"required_primary_boards":REQUIRED_PRIMARY_SLOTS}
@app.get("/")
async def root():
    return {"service":"Pinterest Autonomous Agent","version":"3.5.0","endpoints":{"health":"GET /health","submit":"POST /submit body: {\"url\": \"<product_url>\"}","status":"GET /status/{job_id}","quota":"GET /quota","amazon_status":"GET /amazon/status","mcp":"Tokenized Composio MCP endpoint is enabled when MCP_BRIDGE_TOKEN is configured."},"usage":"Send one product/affiliate URL. System creates 5 unique Pins automatically."}
async def run_job(job_id:str,url:str):
    try:
        job_store.update(job_id,status=JobStatus.RUNNING,progress="Starting 5-pin workflow")
        result=await process_pinterest_job(job_id,url,job_store); quota.record_job(True); result["quota"]=quota.snapshot(); job_store.update(job_id,status=JobStatus.COMPLETED,progress="Finished",result=result)
        try:
            pins=result.get("pins") or []; pin_ids=[str(p.get("pin_id")) for p in pins if p.get("pin_id")]; verified=all(bool(p.get("verified")) for p in pins) if pins else False; dest=next((p.get("destination_url") for p in pins if p.get("destination_url")),None) or url
            registry.record_success(affiliate_url=dest,product_url=url,source="manual",job_id=job_id,board_id=str(result.get("board_id") or "") or None,asin=extract_asin(dest) or extract_asin(url),pinterest_verified=verified,pin_ids=pin_ids)
        except Exception as e: logger.warning("Published registry update skipped: %s",e)
        logger.info("Job %s completed: %s",job_id,result.get("summary"))
    except Exception as e:
        quota.record_job(False); logger.exception("Job %s failed",job_id); job_store.update(job_id,status=JobStatus.FAILED,progress="Failed",error=str(e))
