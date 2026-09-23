"""Autonomous Pinterest Agent - Railway service.
The existing /submit URL workflow is unchanged in routing; its shared product workflow now targets four Pins, and Amazon automation remains additive.
"""
import os,uuid,re,logging,asyncio,hmac,json
from datetime import datetime,timezone
from typing import Optional,Dict,Any,List
from contextlib import asynccontextmanager
from fastapi import FastAPI,BackgroundTasks,HTTPException,Header,Depends,Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel,Field
from dotenv import load_dotenv
import jwt
from jwt import PyJWKClient
load_dotenv()
import agent as agent_module
import quality_patch
QUALITY_PATCH_VERSION=quality_patch.install_identity(agent_module)
from wire_board_org import apply_agent_wiring
from quota import quota
from mcp_bridge import router as mcp_router,MCP_PATH,register_custom_mcp_with_retry
apply_agent_wiring(agent_module)
import runtime_hardening
runtime_hardening.install(agent_module)
import provider_failover
provider_failover.install(__import__("wire_board_org"),__import__("ai_quality_gate"))
quality_patch.install_process_gate(agent_module)
from agent import process_pinterest_job
from models import JobStore,JobStatus,Job
from published_registry import registry,extract_asin
from amazon_client import amazon_credentials_present
from batch_submit import prepare_batch_items, discover_n_products, MAX_BATCH, validate_and_canonicalize
from pin_config import PINS_PER_PRODUCT
from amazon_discovery import is_dormant as amazon_discovery_dormant
from amazon_scheduler import amazon_scheduler,SCHEDULER_MODE
from amazon_boards import REQUIRED_PRIMARY_SLOTS
from daily_ledger import ledger as daily_ledger
import publication_guard
import image_diversity_guard
publication_guard.install(agent_module)
image_diversity_guard.install(agent_module)
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger=logging.getLogger("pinterest-agent");job_store=JobStore();_enqueue_lock=asyncio.Lock();API_SECRET=os.getenv("API_SECRET","").strip();AMAZON_BATCH_SECRET=os.getenv("AMAZON_BATCH_SECRET","").strip();CLOUDFLARE_WAKE_SECRET=os.getenv("CLOUDFLARE_WAKE_SECRET","").strip();GITHUB_REPO="Ndarwin-hub/pinterest-autonomous-agent";GITHUB_ISSUER="https://token.actions.githubusercontent.com";GITHUB_AUDIENCE=f"https://github.com/{GITHUB_REPO}";_jwks=PyJWKClient("https://token.actions.githubusercontent.com/.well-known/jwks",cache_keys=True)
def verify_secret(x_api_secret:Optional[str]=Header(None)):
 if API_SECRET and x_api_secret!=API_SECRET:raise HTTPException(status_code=401,detail="Invalid or missing API secret")
 return True
def verify_manual_oidc(authorization:Optional[str]=Header(None)):
 if not authorization or not authorization.startswith("Bearer "):raise HTTPException(status_code=401,detail="Missing scheduler authentication")
 token=authorization.split(" ",1)[1].strip()
 try:
  key=_jwks.get_signing_key_from_jwt(token).key;claims=jwt.decode(token,key,algorithms=["RS256"],issuer=GITHUB_ISSUER,audience=GITHUB_AUDIENCE,options={"require":["iss","sub","aud","exp","repository"]})
  if claims.get("repository")!=GITHUB_REPO or claims.get("ref")!="refs/heads/main" or claims.get("event_name") not in ("push","workflow_dispatch","schedule"):raise ValueError("OIDC claims not authorized")
  return True
 except Exception as e:logger.warning("GitHub OIDC manual-submit authentication failed: %s",type(e).__name__);raise HTTPException(status_code=401,detail="Invalid scheduler identity")

def verify_batch_secret(authorization:Optional[str]=Header(None),x_scheduler_secret:Optional[str]=Header(None,alias="X-Scheduler-Secret")):
 if x_scheduler_secret and ((AMAZON_BATCH_SECRET and hmac.compare_digest(x_scheduler_secret,AMAZON_BATCH_SECRET)) or (API_SECRET and hmac.compare_digest(x_scheduler_secret,API_SECRET)) or (CLOUDFLARE_WAKE_SECRET and hmac.compare_digest(x_scheduler_secret,CLOUDFLARE_WAKE_SECRET))):return True
 if not authorization or not authorization.startswith("Bearer "):raise HTTPException(status_code=401,detail="Missing scheduler authentication")
 token=authorization.split(" ",1)[1].strip()
 try:
  key=_jwks.get_signing_key_from_jwt(token).key;claims=jwt.decode(token,key,algorithms=["RS256"],issuer=GITHUB_ISSUER,audience=GITHUB_AUDIENCE,options={"require":["iss","sub","aud","exp","repository"]})
  if claims.get("repository")!=GITHUB_REPO or claims.get("ref")!="refs/heads/main" or claims.get("event_name") not in ("schedule","workflow_dispatch"):raise ValueError("OIDC claims not authorized")
  return True
 except Exception as e:logger.warning("GitHub OIDC scheduler authentication failed: %s",type(e).__name__);raise HTTPException(status_code=401,detail="Invalid scheduler identity")
def extract_url(text:str)->str:
 text=(text or "").strip();m=re.search(r"https?://\S+",text)
 if m:return m.group(0).rstrip(").,]',\"")
 if text.startswith("http"):return text
 raise ValueError("No valid http(s) URL found")
@asynccontextmanager
async def lifespan(app:FastAPI):
 logger.info("Pinterest Autonomous Agent v4.0.0 starting... quality_patch=%s",QUALITY_PATCH_VERSION);logger.info("Quota governor: %s",quota.snapshot());logger.info("Amazon layer source=composio amazon_api_credentials_present=%s mode=%s",amazon_credentials_present(),SCHEDULER_MODE)
 registration_task=None
 if MCP_PATH:registration_task=asyncio.create_task(register_custom_mcp_with_retry())
 startup_pin_count=int(os.getenv("PIN_COMMAND_ON_START","0") or "0")
 if startup_pin_count>0:
  async def _run_startup_pin_command():
   marker="/data/pin_command_"+str(startup_pin_count)+".done"
   try:
    if os.path.exists(marker):
     logger.info("Startup Pin command %s already consumed",startup_pin_count);return
    class _BG:
     def add_task(self,fn,*args):asyncio.create_task(fn(*args))
    result=await amazon_discover_submit(DiscoverSubmitRequest(count=startup_pin_count,exclude_asins=[]),_BG(),True)
    os.makedirs("/data",exist_ok=True)
    with open(marker,"w") as f:json.dump({"count":startup_pin_count,"result":result},f,default=str)
    logger.info("STARTUP PIN %s COMPLETED: %s",startup_pin_count,json.dumps(result,separators=(",",":"))[:5000])
   except Exception as exc:logger.exception("STARTUP PIN %s FAILED: %s",startup_pin_count,exc)
  asyncio.create_task(_run_startup_pin_command(),name=f"startup-pin-{startup_pin_count}")
 else:logger.warning("MCP bridge disabled: MCP_BRIDGE_TOKEN is not configured") if not MCP_PATH else logger.info("MCP bridge enabled at configured protected endpoint")
 async def _enqueue_for_amazon(url:str,target_board_id:str|None=None,target_board_name:str|None=None):
  class _BG:
   def add_task(self,fn,*args):asyncio.create_task(fn(*args))
  r=await enqueue_job(url,_BG(),target_board_id=target_board_id,target_board_name=target_board_name);return {"job_id":r.job_id,"status":r.status,"message":r.message}
 async def _list_boards_for_amazon():
  try:
   data=await agent_module.run_composio_tool("PINTEREST_LIST_BOARDS",{});return data.get("items") or data.get("boards") or []
  except Exception as e:logger.warning("Amazon board listing failed: %s",e);return []
 async def _wait_job(job_id:str):
  for _ in range(180):
   await asyncio.sleep(10);job=job_store.get(job_id)
   if not job:return {"status":"missing"}
   if job.status.value in ("completed","completed_partial","failed"):return {"status":job.status.value,"error":job.error,"result":job.result}
  return {"status":"timeout"}
 app.state.amazon_enqueue=_enqueue_for_amazon;app.state.amazon_list_boards=_list_boards_for_amazon;app.state.amazon_wait_job=_wait_job
 await amazon_scheduler.start(enqueue=_enqueue_for_amazon,list_boards=_list_boards_for_amazon,wait_job=_wait_job)
 yield
 await amazon_scheduler.stop_daily_session()
 await amazon_scheduler.stop()
 if registration_task:
  registration_task.cancel()
  try:await registration_task
  except asyncio.CancelledError:pass
 logger.info("Shutting down...")
app=FastAPI(title="Pinterest Autonomous Agent",description="Submit a product/affiliate URL. Agent researches, creates four unique Pins with multi-provider images, publishes and verifies.",version="4.0.0",lifespan=lifespan)
if MCP_PATH:app.include_router(mcp_router,prefix=MCP_PATH)
class SubmitRequest(BaseModel):url:str=Field(...,description="Product/affiliate URL. Exact URL preserved as destination for all pins.")
class SubmitResponse(BaseModel):job_id:str;status:str;message:str
class StatusResponse(BaseModel):job_id:str;status:str;progress:Optional[str]=None;result:Optional[Dict[str,Any]]=None;error:Optional[str]=None;created_at:str;updated_at:str
class BatchRequest(BaseModel):
 batch:int=Field(1,ge=1,le=10)
 scheduler_run_id:Optional[str]=None
 scheduled_local_time:Optional[str]=None
 github_delay_seconds:int=0
 github_queued_runs:int=0
 github_active_runs:int=0
 github_load_class:str="UNKNOWN"
 scheduler_run_id:Optional[str]=None
 scheduled_local_time:Optional[str]=None
 github_delay_seconds:int=0
 github_queued_runs:int=0
 github_active_runs:int=0
 github_load_class:str="UNKNOWN"
class BatchSubmitRequest(BaseModel):
 urls:List[str]=Field(...,min_length=1,max_length=50,description="List of already-resolved Amazon US product/affiliate URLs")
 wait:bool=Field(False,description="If true, wait briefly for job acceptance only; does not wait for full product Pin completion")
class DiscoverSubmitRequest(BaseModel):
 count:int=Field(...,ge=1,le=50,description="Number of distinct Amazon US products to discover and submit")
 exclude_asins:Optional[List[str]]=Field(default=None,description="Optional ASIN exclude list")
class RepairItem(BaseModel):
 url:str
 pin_ids:List[str]=Field(default_factory=list,max_length=4)
 retry_pin_indices:List[int]=Field(default_factory=list,max_length=4)
 target_board_id:Optional[str]=None
 target_board_name:Optional[str]=None
class RepairRequest(BaseModel):
 items:List[RepairItem]=Field(...,min_length=1,max_length=10)
async def enqueue_job(url_str:str,background_tasks:BackgroundTasks,target_board_id:Optional[str]=None,target_board_name:Optional[str]=None,force_new:bool=False)->SubmitResponse:
 async with _enqueue_lock:
  existing=job_store.find_by_url(url_str)
  if existing and not force_new:return SubmitResponse(job_id=existing.job_id,status=existing.status.value,message="Existing job reused; duplicate Pinterest workflow was not started.")
  if not quota.reserve_job():raise HTTPException(status_code=429,detail={"message":"Monthly safe Pinterest capacity reached; job not started.","quota":quota.snapshot()})
  job_id=str(uuid.uuid4());job=Job(job_id=job_id,url=url_str,status=JobStatus.QUEUED,progress=f"Job accepted — {PINS_PER_PRODUCT}-Pin workflow queued",target_board_id=target_board_id,target_board_name=target_board_name);job_store.save(job);background_tasks.add_task(run_job,job_id,url_str);return SubmitResponse(job_id=job_id,status=JobStatus.QUEUED.value,message=f"Job accepted. {PINS_PER_PRODUCT} Pins will be researched, imaged, published and verified. Poll /status/{{job_id}}")
@app.get("/health")
async def health():return {"status":"ok","service":"pinterest-autonomous-agent","version":"4.0.0","quality_patch_version":QUALITY_PATCH_VERSION,"mcp_bridge":bool(MCP_PATH),"amazon":{"credentials_present":not amazon_discovery_dormant(),"source":"composio_amazon","amazon_api_credentials_present":amazon_credentials_present(),"scheduler":amazon_scheduler.status,"scheduler_mode":SCHEDULER_MODE,"required_primary_boards":REQUIRED_PRIMARY_SLOTS,"published_registry_count":registry.count_success()},"time":datetime.now(timezone.utc).isoformat()}
@app.get("/quota")
async def quota_status(_:bool=Depends(verify_secret)):return quota.snapshot()
@app.post("/submit",response_model=SubmitResponse)
async def submit(body:SubmitRequest,background_tasks:BackgroundTasks,_:bool=Depends(verify_secret)):
 try:url_str=extract_url(body.url)
 except ValueError as e:raise HTTPException(status_code=400,detail=str(e))
 return await enqueue_job(url_str,background_tasks)
@app.post("/repair-pins")
async def repair_pins(body:RepairRequest,background_tasks:BackgroundTasks,_:bool=Depends(verify_manual_oidc)):
    """Explicit repair-only route: delete specified bad Pins, release their logical claims, then rerun the unchanged product workflow."""
    results=[]
    for item in body.items:
        url=extract_url(item.url)
        deleted=[]
        try:
            if item.retry_pin_indices:
                if any(int(i)<1 or int(i)>PINS_PER_PRODUCT for i in item.retry_pin_indices):
                    raise HTTPException(status_code=400,detail=f"retry_pin_indices must be within 1..{PINS_PER_PRODUCT}")
                publication_guard.guard.release_for_repair(url,item.retry_pin_indices)
            for pin_id in item.pin_ids:
                pid=str(pin_id).strip()
                if not re.fullmatch(r"\d+",pid):
                    raise HTTPException(status_code=400,detail=f"Invalid Pinterest Pin ID: {pid}")
                try:
                    await agent_module.run_composio_tool("PINTEREST_DELETE_PIN",{"pin_id":pid},retries=1)
                except Exception as delete_error:
                    msg=str(delete_error).lower()
                    if "404" not in msg and "not found" not in msg:
                        raise
                try:
                    await agent_module.run_composio_tool("PINTEREST_GET_PIN",{"pin_id":pid},retries=0)
                    raise RuntimeError(f"Pin {pid} still exists after delete")
                except Exception as verify_error:
                    if "404" not in str(verify_error) and "not found" not in str(verify_error).lower():
                        raise
                deleted.append(pid)
            if not item.retry_pin_indices:
                publication_guard.guard.release_for_repair(url)
            resp=await enqueue_job(url,background_tasks,target_board_id=item.target_board_id,target_board_name=item.target_board_name,force_new=True)
            results.append({"url":url,"deleted_pin_ids":deleted,"job_id":resp.job_id,"status":resp.status,"message":resp.message,"target_board_id":item.target_board_id})
        except Exception as e:
            results.append({"url":url,"deleted_pin_ids":deleted,"status":"failed","error":str(e)[:1000]})
    return {"requested":len(body.items),"accepted":sum(1 for r in results if r.get("job_id")),"results":results,"workflow":"existing_shared_submit_pipeline","pins_per_product":PINS_PER_PRODUCT}

@app.get("/status/{job_id}",response_model=StatusResponse)
async def status(job_id:str,_:bool=Depends(verify_secret)):
 job=job_store.get(job_id)
 if not job:raise HTTPException(status_code=404,detail="Job not found")
 return StatusResponse(job_id=job.job_id,status=job.status.value,progress=job.progress,result=job.result,error=job.error,created_at=job.created_at,updated_at=job.updated_at)
@app.get("/amazon/status")
async def amazon_status(_:bool=Depends(verify_secret)):
 return {"credentials_present":not amazon_discovery_dormant(),"source":"composio_amazon","amazon_api_credentials_present":amazon_credentials_present(),"scheduler":amazon_scheduler.status,"scheduler_mode":SCHEDULER_MODE,"daily":daily_ledger.get_day_status(),"scheduler_events":daily_ledger.latest_scheduler_events(),"published_count":registry.count_success(),"required_primary_boards":REQUIRED_PRIMARY_SLOTS}
@app.post("/amazon/manual-submit")
async def amazon_manual_submit(body:BatchSubmitRequest,background_tasks:BackgroundTasks,_:bool=Depends(verify_manual_oidc)):
 """Authenticated GitHub-OIDC bridge for explicit Amazon US affiliate URLs; feeds the unchanged /submit pipeline."""
 if not body.urls:raise HTTPException(status_code=400,detail="urls must be a non-empty list")
 if len(body.urls)>MAX_BATCH:raise HTTPException(status_code=400,detail=f"max {MAX_BATCH} urls per batch")
 prepared=await prepare_batch_items(body.urls)
 results=[]
 for item in prepared:
  entry={"input_url":item.get("input_url"),"asin":item.get("asin"),"affiliate_url":item.get("affiliate_url"),"product_url":item.get("product_url"),"status":item.get("status"),"job_id":None,"message":item.get("message"),"error":item.get("error")}
  if item.get("status")!="accepted" or not item.get("affiliate_url"):
   results.append(entry);continue
  try:
   resp=await enqueue_job(item["affiliate_url"],background_tasks)
   entry["job_id"]=resp.job_id;entry["status"]=resp.status;entry["message"]=resp.message
  except HTTPException as he:
   entry["status"]="failed";entry["error"]=str(he.detail);entry["message"]=str(he.detail)
  except Exception as e:
   entry["status"]="failed";entry["error"]=str(e)[:500];entry["message"]=entry["error"]
  results.append(entry)
 return {"requested":len(body.urls),"accepted":sum(1 for r in results if r.get("job_id")),"skipped":sum(1 for r in results if r.get("status")=="skipped"),"rejected":sum(1 for r in results if r.get("status") in ("rejected","failed") and not r.get("job_id")),"results":results,"pipeline":"existing_/submit_job_pipeline","affiliate_tag":"desiredplus-20"}

@app.post("/pin-a")
async def pin_a_trigger(
    body: Optional[Dict[str,Any]]=None,
    authorization: Optional[str]=Header(None),
    x_pin_a_secret: Optional[str]=Header(None,alias="X-Pin-A-Secret"),
    x_scheduler_secret: Optional[str]=Header(None,alias="X-Scheduler-Secret"),
    x_pin_a_source: Optional[str]=Header(None,alias="X-Pin-A-Source"),
    x_pin_a_request_id: Optional[str]=Header(None,alias="X-Pin-A-Request-ID"),
):
    """Universal Pin A wake endpoint. Existing schedulers and independent callers may invoke it.
    Authentication is source-agnostic: an authorized shared secret or GitHub OIDC is accepted.
    The existing Railway scheduler/ledger remains the single owner of batch execution.
    """
    authorized=False
    for candidate, expected in (
        (x_pin_a_secret, API_SECRET),
        (x_pin_a_secret, CLOUDFLARE_WAKE_SECRET),
        (x_pin_a_secret, AMAZON_BATCH_SECRET),
        (x_scheduler_secret, API_SECRET),
        (x_scheduler_secret, CLOUDFLARE_WAKE_SECRET),
        (x_scheduler_secret, AMAZON_BATCH_SECRET),
    ):
        if candidate and expected and hmac.compare_digest(candidate, expected):
            authorized=True
            break
    if not authorized:
        if authorization and authorization.startswith("Bearer "):
            token=authorization.split(" ",1)[1].strip()
            try:
                key=_jwks.get_signing_key_from_jwt(token).key
                claims=jwt.decode(token,key,algorithms=["RS256"],issuer=GITHUB_ISSUER,audience=GITHUB_AUDIENCE,options={"require":["iss","sub","aud","exp","repository"]})
                if claims.get("repository")==GITHUB_REPO and claims.get("ref")=="refs/heads/main" and claims.get("event_name") in ("schedule","workflow_dispatch"):
                    authorized=True
            except Exception as e:
                logger.warning("Pin A GitHub OIDC authentication failed: %s",type(e).__name__)
    if not authorized:
        raise HTTPException(status_code=401,detail="Invalid or missing Pin A authentication")
    source=(x_pin_a_source or ((body or {}).get("source") if isinstance(body,dict) else None) or "unknown").strip()[:200]
    request_id=(x_pin_a_request_id or ((body or {}).get("request_id") if isinstance(body,dict) else None) or str(uuid.uuid4())).strip()[:200]
    day=daily_ledger.today_str()
    daily_ledger.record_scheduler_event(day=day,batch_requested=1,scheduler_run_id=f"pin-a:{request_id}",scheduled_local_time="pin-a",github_delay_seconds=0,github_queued_runs=0,github_active_runs=0,github_load_class="PIN_A")
    logger.info("Pin A accepted source=%s request_id=%s",source,request_id)
    if SCHEDULER_MODE!="external":
        return {"status":"ignored","source":source,"request_id":request_id,"reason":"Amazon scheduler is not in external mode"}
    result=await amazon_scheduler.start_daily_session(app.state.amazon_enqueue,app.state.amazon_list_boards,app.state.amazon_wait_job,trigger_batch=1)
    return {**result,"pin_a":True,"source":source,"request_id":request_id,"scheduler":"railway_owned_daily_session","message":"Pin A wake accepted; Railway scheduler/ledger owns batch execution and duplicate prevention."}

@app.post("/amazon/run-batch")
async def amazon_run_batch(body:BatchRequest,x_scheduler_secret:Optional[str]=Header(None,alias="X-Scheduler-Secret"),_:bool=Depends(verify_batch_secret)):
 if SCHEDULER_MODE!="external":raise HTTPException(status_code=409,detail="Amazon scheduler is not in external mode")
 day=daily_ledger.today_str()
 daily_ledger.record_scheduler_event(day=day,batch_requested=body.batch,scheduler_run_id=body.scheduler_run_id,scheduled_local_time=body.scheduled_local_time,github_delay_seconds=body.github_delay_seconds,github_queued_runs=body.github_queued_runs,github_active_runs=body.github_active_runs,github_load_class=body.github_load_class)
 pin_a_source="cloudflare-compat" if (x_scheduler_secret and CLOUDFLARE_WAKE_SECRET and hmac.compare_digest(x_scheduler_secret,CLOUDFLARE_WAKE_SECRET)) else "legacy-scheduler"
 logger.info("Pin A compatibility activation via existing /amazon/run-batch source=%s requested_batch=%s github_run=%s",pin_a_source,body.batch,body.scheduler_run_id)
 logger.info("Amazon wake trigger requested_batch=%s github_run=%s delay=%ss queued=%s active=%s load=%s",body.batch,body.scheduler_run_id,body.github_delay_seconds,body.github_queued_runs,body.github_active_runs,body.github_load_class)
 result=await amazon_scheduler.start_daily_session(app.state.amazon_enqueue,app.state.amazon_list_boards,app.state.amazon_wait_job,trigger_batch=body.batch)
 return {**result,"pin_a":True,"pin_a_source":pin_a_source,"scheduler":"railway_owned_daily_session","github_trigger_batch":body.batch,"day":day,"message":"Existing scheduler trigger also activates the Pin A layer; Railway owns the remaining daily batches and duplicate prevention."}
@app.post("/amazon/notification-check")
async def amazon_notification_check(_:bool=Depends(verify_batch_secret)):
    """Final reconciliation check: sends exactly one daily STARTED, NOT STARTED, or FAILED status notification."""
    day=daily_ledger.today_str()
    from amazon_alerts import notify_daily_started,notify_daily_not_started,notify_daily_failed
    state=daily_ledger.daily_status_state(day)
    if state.get("started"):
        if daily_ledger.is_day_complete(day):
            return {"status":"complete","day":day,"notification":"none"}
        await notify_daily_failed(day)
        return {"status":"failed","day":day,"notification":"failed"}
    await notify_daily_not_started(day)
    return {"status":"not_started","day":day,"notification":"not_started"}

@app.post("/batch-submit")
async def batch_submit(body:BatchSubmitRequest,background_tasks:BackgroundTasks,_:bool=Depends(verify_secret)):
 """Accept a list of already-resolved Amazon US product URLs. Each accepted URL is fed into the existing single-product four-Pin job pipeline unchanged in routing."""
 if not body.urls:raise HTTPException(status_code=400,detail="urls must be a non-empty list")
 if len(body.urls)>MAX_BATCH:raise HTTPException(status_code=400,detail=f"max {MAX_BATCH} urls per batch")
 prepared=await prepare_batch_items(body.urls)
 results=[]
 for item in prepared:
  entry={"input_url":item.get("input_url"),"asin":item.get("asin"),"affiliate_url":item.get("affiliate_url"),"product_url":item.get("product_url"),"status":item.get("status"),"job_id":None,"message":item.get("message"),"error":item.get("error")}
  if item.get("status")!="accepted" or not item.get("affiliate_url"):
   results.append(entry);continue
  try:
   resp=await enqueue_job(item["affiliate_url"],background_tasks)
   entry["job_id"]=resp.job_id;entry["status"]=resp.status;entry["message"]=resp.message
  except HTTPException as he:
   entry["status"]="failed";entry["error"]=str(he.detail);entry["message"]=str(he.detail)
  except Exception as e:
   entry["status"]="failed";entry["error"]=str(e)[:500];entry["message"]=entry["error"]
  results.append(entry)
 accepted=sum(1 for r in results if r.get("job_id"))
 skipped=sum(1 for r in results if r.get("status")=="skipped")
 rejected=sum(1 for r in results if r.get("status") in ("rejected","failed") and not r.get("job_id"))
 return {"batch_size":len(body.urls),"accepted":accepted,"skipped":skipped,"rejected":rejected,"results":results,"pipeline":"existing_/submit_job_pipeline","note":"Each accepted URL enters the existing four-Pin research/publish/verify workflow. Poll /status/{job_id} for completion."}

@app.post("/amazon/discover-submit")
async def amazon_discover_submit(body:DiscoverSubmitRequest,background_tasks:BackgroundTasks,_:bool=Depends(verify_secret)):
 """Discover N distinct Amazon US products using live board-balance ordering, then submit each to the existing job pipeline."""
 n=int(body.count)
 if n<1 or n>MAX_BATCH:raise HTTPException(status_code=400,detail=f"count must be 1..{MAX_BATCH}")
 exclude=set(a.upper() for a in (body.exclude_asins or []) if a)
 live_boards=[]
 balance_meta={"available":False,"message":"Board balancing could not be verified because current Pinterest counts were unavailable."}
 try:
  from board_balance import extract_board_rows,balance_state,discovery_board_order
  data=await agent_module.run_composio_tool("PINTEREST_LIST_BOARDS",{})
  live_boards=data.get("items") or data.get("boards") or []
  balance_meta=balance_state(extract_board_rows(live_boards))
 except Exception as e:
  logger.warning("Live board fetch for balance failed: %s",e)
 try:
  discovered=await discover_n_products(n,exclude_asins=exclude,live_boards=live_boards)
 except Exception as e:
  raise HTTPException(status_code=502,detail=f"discovery_failed:{type(e).__name__}:{e}")
 if not discovered:
  return {"requested":n,"discovered":0,"accepted":0,"skipped":0,"rejected":0,"results":[],"board_balance":balance_meta,"message":"No distinct unpublished Amazon US products found"}
 urls=[d["affiliate_url"] for d in discovered if d.get("affiliate_url")]
 prepared=await prepare_batch_items(urls,exclude_asins=exclude)
 title_by_asin={ (d.get("asin") or "").upper():d.get("title") for d in discovered }
 target_by_asin={ (d.get("asin") or "").upper():{"board":d.get("target_board_name"),"mode":d.get("balance_mode")} for d in discovered }
 results=[]
 for item in prepared:
  entry={"input_url":item.get("input_url"),"asin":item.get("asin"),"title":title_by_asin.get((item.get("asin") or "").upper()),"affiliate_url":item.get("affiliate_url"),"product_url":item.get("product_url"),"status":item.get("status"),"job_id":None,"message":item.get("message"),"error":item.get("error"),"target_board":(target_by_asin.get((item.get("asin") or "").upper()) or {}).get("board"),"balance_mode":(target_by_asin.get((item.get("asin") or "").upper()) or {}).get("mode")}
  if item.get("status")!="accepted" or not item.get("affiliate_url"):
   results.append(entry);continue
  try:
   resp=await enqueue_job(item["affiliate_url"],background_tasks)
   entry["job_id"]=resp.job_id;entry["status"]=resp.status;entry["message"]=resp.message
  except HTTPException as he:
   entry["status"]="failed";entry["error"]=str(he.detail);entry["message"]=str(he.detail)
  except Exception as e:
   entry["status"]="failed";entry["error"]=str(e)[:500];entry["message"]=entry["error"]
  results.append(entry)
 accepted=sum(1 for r in results if r.get("job_id"))
 skipped=sum(1 for r in results if r.get("status")=="skipped")
 rejected=sum(1 for r in results if r.get("status") in ("rejected","failed") and not r.get("job_id"))
 return {"requested":n,"discovered":len(discovered),"accepted":accepted,"skipped":skipped,"rejected":rejected,"results":results,"board_balance":{"available":balance_meta.get("available"),"state":balance_meta.get("state"),"spread":balance_meta.get("spread"),"message":balance_meta.get("message"),"snapshot":balance_meta.get("snapshot")},"pipeline":"existing_/submit_job_pipeline","tag":"desiredplus-20","note":"Discovery used live board pin counts + COMPOSIO_SEARCH_AMAZON + tag injection. Each accepted product uses the existing four-Pin pipeline."}

@app.get("/amazon/pin-count")
async def amazon_pin_count_get(count:int=1,token:str="",request:Request=None):
 """Protected GET trigger for the Railway Pin N bridge; executes the same discovery-submit pipeline."""
 expected=os.getenv("MCP_BRIDGE_TOKEN","").strip()
 if not expected or not hmac.compare_digest(token,expected):
  raise HTTPException(status_code=401,detail="Invalid trigger token")
 if count<1 or count>MAX_BATCH:raise HTTPException(status_code=400,detail=f"count must be 1..{MAX_BATCH}")
 class _BG:
  def add_task(self,fn,*args):asyncio.create_task(fn(*args))
 body=DiscoverSubmitRequest(count=count,exclude_asins=[])
 return await amazon_discover_submit(body,_BG(),True)

@app.get("/")
async def root():return {"service":"Pinterest Autonomous Agent","version":"3.9.0","endpoints":{"health":"GET /health","submit":"POST /submit body: {\"url\": \"<product_url>\"}","status":"GET /status/{job_id}","quota":"GET /quota","amazon_status":"GET /amazon/status","amazon_batch":"POST /amazon/run-batch body: {\"batch\":1|2|3}","batch_submit":"POST /batch-submit body: {\"urls\":[\"<amazon_us_url\",...]}","amazon_discover_submit":"POST /amazon/discover-submit body: {\"count\":N}"},"usage":"Send one product/affiliate URL. System creates 4 unique Pins automatically."}
async def run_job(job_id:str,url:str):
 try:
  job_store.update(job_id,status=JobStatus.RUNNING,progress=f"Starting {PINS_PER_PRODUCT}-pin workflow")
  result=await process_pinterest_job(job_id,url,job_store)
  quota.record_job(bool(result.get("pins_published")))
  result["quota"]=quota.snapshot()
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
  try:
   pins=result.get("pins") or [];pin_ids=[str(p.get("pin_id")) for p in pins if p.get("pin_id")];verified=bool(pins) and len(pins)==PINS_PER_PRODUCT and all(bool(p.get("verified")) for p in pins);dest=next((p.get("destination_url") for p in pins if p.get("destination_url")),None) or url;registry.record_success(affiliate_url=dest,product_url=url,source="manual",job_id=job_id,board_id=str(result.get("board_id") or "") or None,asin=extract_asin(dest) or extract_asin(url),pinterest_verified=verified,pin_ids=pin_ids)
  except Exception as e:logger.warning("Published registry update skipped: %s",e)
  logger.info("Job %s completed with status=%s: %s",job_id,final_status.value,result.get("summary"))
 except Exception as e:quota.record_job(False);logger.exception("Job %s failed",job_id);job_store.update(job_id,status=JobStatus.FAILED,progress="Failed",error=str(e))
