"""Autonomous Pinterest Agent - Railway service.
The existing /submit URL->5-pin workflow is unchanged; Amazon automation is additive and now uses browserless Composio discovery.
"""
import os,uuid,re,logging,asyncio,hmac,json
from datetime import datetime,timezone
from typing import Optional,Dict,Any,List
from contextlib import asynccontextmanager
from fastapi import FastAPI,BackgroundTasks,HTTPException,Header,Depends
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
from amazon_discovery import is_dormant as amazon_discovery_dormant
from amazon_scheduler import amazon_scheduler,SCHEDULER_MODE
from amazon_boards import REQUIRED_PRIMARY_SLOTS
from daily_ledger import ledger as daily_ledger
import publication_guard
import image_diversity_guard
publication_guard.install(agent_module)
image_diversity_guard.install(agent_module)
logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger=logging.getLogger("pinterest-agent");job_store=JobStore();_enqueue_lock=asyncio.Lock();API_SECRET=os.getenv("API_SECRET","").strip();AMAZON_BATCH_SECRET=os.getenv("AMAZON_BATCH_SECRET","").strip();GITHUB_REPO="Ndarwin-hub/pinterest-autonomous-agent";GITHUB_ISSUER="https://token.actions.githubusercontent.com";GITHUB_AUDIENCE=f"https://github.com/{GITHUB_REPO}";_jwks=PyJWKClient("https://token.actions.githubusercontent.com/.well-known/jwks",cache_keys=True)
def verify_secret(x_api_secret:Optional[str]=Header(None)):
 if API_SECRET and x_api_secret!=API_SECRET:raise HTTPException(status_code=401,detail="Invalid or missing API secret")
 return True
def verify_batch_secret(authorization:Optional[str]=Header(None),x_scheduler_secret:Optional[str]=Header(None,alias="X-Scheduler-Secret")):
 if AMAZON_BATCH_SECRET and x_scheduler_secret and hmac.compare_digest(x_scheduler_secret,AMAZON_BATCH_SECRET):return True
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
 logger.info("Pinterest Autonomous Agent v3.9.0 starting... quality_patch=%s",QUALITY_PATCH_VERSION);logger.info("Quota governor: %s",quota.snapshot());logger.info("Amazon layer source=composio amazon_api_credentials_present=%s mode=%s",amazon_credentials_present(),SCHEDULER_MODE)
 registration_task=None
 if MCP_PATH:registration_task=asyncio.create_task(register_custom_mcp_with_retry())
 else:logger.warning("MCP bridge disabled: MCP_BRIDGE_TOKEN is not configured")
 async def _enqueue_for_amazon(url:str):
  class _BG:
   def add_task(self,fn,*args):asyncio.create_task(fn(*args))
  r=await enqueue_job(url,_BG());return {"job_id":r.job_id,"status":r.status,"message":r.message}
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
 await amazon_scheduler.stop()
 if registration_task:
  registration_task.cancel()
  try:await registration_task
  except asyncio.CancelledError:pass
 logger.info("Shutting down...")
app=FastAPI(title="Pinterest Autonomous Agent",description="Submit a product/affiliate URL. Agent researches, creates 5 unique Pins with multi-provider images, publishes and verifies.",version="3.9.0",lifespan=lifespan)
if MCP_PATH:app.include_router(mcp_router,prefix=MCP_PATH)
class SubmitRequest(BaseModel):url:str=Field(...,description="Product/affiliate URL. Exact URL preserved as destination for all pins.")
class SubmitResponse(BaseModel):job_id:str;status:str;message:str
class StatusResponse(BaseModel):job_id:str;status:str;progress:Optional[str]=None;result:Optional[Dict[str,Any]]=None;error:Optional[str]=None;created_at:str;updated_at:str
class BatchRequest(BaseModel):batch:int=Field(...,ge=1,le=3)
class BatchSubmitRequest(BaseModel):
 urls:List[str]=Field(...,min_length=1,max_length=50,description="List of already-resolved Amazon US product/affiliate URLs")
 wait:bool=Field(False,description="If true, wait briefly for job acceptance only; does not wait for full 5-Pin completion")
class DiscoverSubmitRequest(BaseModel):
 count:int=Field(...,ge=1,le=50,description="Number of distinct Amazon US products to discover and submit")
 exclude_asins:Optional[List[str]]=Field(default=None,description="Optional ASIN exclude list")
async def enqueue_job(url_str:str,background_tasks:BackgroundTasks)->SubmitResponse:
 async with _enqueue_lock:
  existing=job_store.find_by_url(url_str)
  if existing:return SubmitResponse(job_id=existing.job_id,status=existing.status.value,message="Existing job reused; duplicate Pinterest workflow was not started.")
  if not quota.reserve_job():raise HTTPException(status_code=429,detail={"message":"Monthly safe Pinterest capacity reached; job not started.","quota":quota.snapshot()})
  job_id=str(uuid.uuid4());job=Job(job_id=job_id,url=url_str,status=JobStatus.QUEUED,progress="Job accepted — 5-pin workflow queued");job_store.save(job);background_tasks.add_task(run_job,job_id,url_str);return SubmitResponse(job_id=job_id,status=JobStatus.QUEUED.value,message="Job accepted. 5 Pins will be researched, imaged, published and verified. Poll /status/{job_id}")
@app.get("/health")
async def health():return {"status":"ok","service":"pinterest-autonomous-agent","version":"3.9.0","quality_patch_version":QUALITY_PATCH_VERSION,"mcp_bridge":bool(MCP_PATH),"amazon":{"credentials_present":not amazon_discovery_dormant(),"source":"composio_amazon","amazon_api_credentials_present":amazon_credentials_present(),"scheduler":amazon_scheduler.status,"scheduler_mode":SCHEDULER_MODE,"required_primary_boards":REQUIRED_PRIMARY_SLOTS,"published_registry_count":registry.count_success()},"time":datetime.now(timezone.utc).isoformat()}
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
async def amazon_status(_:bool=Depends(verify_secret)):return {"credentials_present":not amazon_discovery_dormant(),"source":"composio_amazon","amazon_api_credentials_present":amazon_credentials_present(),"scheduler":amazon_scheduler.status,"scheduler_mode":SCHEDULER_MODE,"daily":daily_ledger.get_day_status(),"published_count":registry.count_success(),"required_primary_boards":REQUIRED_PRIMARY_SLOTS}
@app.post("/amazon/run-batch")
async def amazon_run_batch(body:BatchRequest,_:bool=Depends(verify_batch_secret)):
 if SCHEDULER_MODE!="external":raise HTTPException(status_code=409,detail="Amazon scheduler is not in external mode")
 task=asyncio.create_task(amazon_scheduler.run_batch(body.batch,app.state.amazon_enqueue,app.state.amazon_list_boards,app.state.amazon_wait_job),name=f"amazon-batch-{body.batch}")
 async def stream():
  while not task.done():
   yield json.dumps({"status":"running","batch":body.batch,"time":datetime.now(timezone.utc).isoformat()})+"\n"
   try:await asyncio.wait_for(asyncio.shield(task),timeout=25)
   except asyncio.TimeoutError:continue
  try:yield json.dumps(task.result(),separators=(",",":"))+"\n"
  except Exception as e:yield json.dumps({"status":"failed","batch":body.batch,"error":str(e)[:500]})+"\n"
 return StreamingResponse(stream(),media_type="application/x-ndjson")
@app.post("/batch-submit")
async def batch_submit(body:BatchSubmitRequest,background_tasks:BackgroundTasks,_:bool=Depends(verify_secret)):
 """Accept a list of already-resolved Amazon US product URLs. Each accepted URL is fed into the existing single-product job pipeline unchanged in behavior."""
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
 return {"batch_size":len(body.urls),"accepted":accepted,"skipped":skipped,"rejected":rejected,"results":results,"pipeline":"existing_/submit_job_pipeline","note":"Each accepted URL enters the existing 5-Pin research/publish/verify workflow. Poll /status/{job_id} for completion."}

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
 return {"requested":n,"discovered":len(discovered),"accepted":accepted,"skipped":skipped,"rejected":rejected,"results":results,"board_balance":{"available":balance_meta.get("available"),"state":balance_meta.get("state"),"spread":balance_meta.get("spread"),"message":balance_meta.get("message"),"snapshot":balance_meta.get("snapshot")},"pipeline":"existing_/submit_job_pipeline","tag":"desiredplus-20","note":"Discovery used live board pin counts + COMPOSIO_SEARCH_AMAZON + tag injection. Each accepted product uses the existing 5-Pin pipeline."}

@app.get("/")
async def root():return {"service":"Pinterest Autonomous Agent","version":"3.9.0","endpoints":{"health":"GET /health","submit":"POST /submit body: {\"url\": \"<product_url>\"}","status":"GET /status/{job_id}","quota":"GET /quota","amazon_status":"GET /amazon/status","amazon_batch":"POST /amazon/run-batch body: {\"batch\":1|2|3}","batch_submit":"POST /batch-submit body: {\"urls\":[\"<amazon_us_url\",...]}","amazon_discover_submit":"POST /amazon/discover-submit body: {\"count\":N}"},"usage":"Send one product/affiliate URL. System creates 5 unique Pins automatically."}
async def run_job(job_id:str,url:str):
 try:
  job_store.update(job_id,status=JobStatus.RUNNING,progress="Starting 5-pin workflow")
  result=await process_pinterest_job(job_id,url,job_store)
  quota.record_job(bool(result.get("pins_published")))
  result["quota"]=quota.snapshot()
  supervisor_status=str(result.get("pin_supervisor_status") or "")
  published_count=int(result.get("pins_published") or 0)
  verified_count=int(result.get("verified_pins") or sum(1 for p in (result.get("pins") or []) if isinstance(p,dict) and p.get("verified")))
  if supervisor_status=="completed" or verified_count>=5:
   final_status=JobStatus.COMPLETED
  elif supervisor_status=="completed_partial" or verified_count>0 or published_count>0:
   final_status=JobStatus.COMPLETED_PARTIAL
  else:
   final_status=JobStatus.FAILED
  job_store.update(job_id,status=final_status,progress="Finished",result=result)
  try:
   pins=result.get("pins") or [];pin_ids=[str(p.get("pin_id")) for p in pins if p.get("pin_id")];verified=bool(pins) and len(pins)==5 and all(bool(p.get("verified")) for p in pins);dest=next((p.get("destination_url") for p in pins if p.get("destination_url")),None) or url;registry.record_success(affiliate_url=dest,product_url=url,source="manual",job_id=job_id,board_id=str(result.get("board_id") or "") or None,asin=extract_asin(dest) or extract_asin(url),pinterest_verified=verified,pin_ids=pin_ids)
  except Exception as e:logger.warning("Published registry update skipped: %s",e)
  logger.info("Job %s completed with status=%s: %s",job_id,final_status.value,result.get("summary"))
 except Exception as e:quota.record_job(False);logger.exception("Job %s failed",job_id);job_store.update(job_id,status=JobStatus.FAILED,progress="Failed",error=str(e))
