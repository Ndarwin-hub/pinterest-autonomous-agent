"""External-wake Amazon scheduler feeding the existing Pinterest pipeline."""
from __future__ import annotations
import asyncio,logging,os,uuid,json
from datetime import datetime,timezone
from typing import Any,Awaitable,Callable,Dict,List
from amazon_client import amazon_credentials_present
from amazon_boards import build_slot_specs,REQUIRED_PRIMARY_SLOTS,classify_live_boards,CATEGORY_SLOTS
from amazon_discovery import MAX_REPLACEMENTS_PER_SLOT,discover_for_board,discover_global,is_dormant
from amazon_composio_discovery import discover_category
from daily_ledger import ledger,SLOT_COUNT,BATCH_SIZE
from published_registry import registry
from amazon_alerts import send_failure_alert
logger=logging.getLogger("pinterest-agent.amazon_scheduler")
SLOT_INTERVAL_SEC=int(os.getenv("AMAZON_SLOT_INTERVAL_SEC",str(48*60)));SCHEDULER_ENABLED=os.getenv("AMAZON_SCHEDULER_ENABLED","true").lower() in ("1","true","yes");SCHEDULER_MODE=os.getenv("AMAZON_SCHEDULER_MODE","external").strip().lower()
EnqueueFn=Callable[[str],Awaitable[Dict[str,Any]]];ListBoardsFn=Callable[[],Awaitable[List[Dict[str,Any]]]];WaitJobFn=Callable[[str],Awaitable[Dict[str,Any]]]
def pinterest_any_verified(result:Dict[str,Any])->bool:
 pins=result.get("pins") if isinstance(result,dict) else None
 return isinstance(pins,list) and any(isinstance(p,dict) and bool(p.get("verified")) and bool(p.get("pin_id")) and bool(p.get("destination_url")) for p in pins)
def pinterest_five_verified(result:Dict[str,Any])->bool:
 pins=result.get("pins") if isinstance(result,dict) else None
 return isinstance(pins,list) and len(pins)==5 and all(isinstance(p,dict) and bool(p.get("verified")) for p in pins)
class AmazonScheduler:
 def __init__(self):self._task=None;self._stop=asyncio.Event();self.status={"running":False,"dormant_reason":None,"last_tick":None,"mode":SCHEDULER_MODE,"current_batch":None,"source":"composio"}
 def gate_status(self,live_boards=None):
  composio=not is_dormant();ready=composio and SCHEDULER_ENABLED;info=classify_live_boards(live_boards or []);reasons=[]
  if not SCHEDULER_ENABLED:reasons.append("AMAZON_SCHEDULER_ENABLED is false")
  if not composio:reasons.append("Composio Amazon connection/configuration is not available")
  if live_boards is not None and not info["scheduler_ready"]:reasons.append(f"Need {REQUIRED_PRIMARY_SLOTS} approved primary boards; have {info['primary_count']}")
  return {"credentials_present":composio,"scheduler_enabled_flag":SCHEDULER_ENABLED,"mode":SCHEDULER_MODE,"source":"composio_amazon","amazon_api_credentials_present":amazon_credentials_present(),"board_info":info,"ready":ready and (live_boards is None or info["scheduler_ready"]),"blocking_reasons":reasons,"slot_interval_sec":SLOT_INTERVAL_SEC,"daily_target":SLOT_COUNT}
 async def start(self,*,enqueue,list_boards,wait_job=None):
  if SCHEDULER_MODE!="continuous":self.status.update({"mode":SCHEDULER_MODE,"running":False,"dormant_reason":"external_mode"});return
  if self._task and not self._task.done():return
  self._stop.clear();self._task=asyncio.create_task(self._loop(enqueue,list_boards,wait_job),name="amazon-scheduler")
 async def stop(self):
  self._stop.set()
  if self._task:
   self._task.cancel()
   try:await self._task
   except asyncio.CancelledError:pass
   self._task=None
  self.status["running"]=False
 async def _loop(self,enqueue,list_boards,wait_job):
  self.status["running"]=True
  try:
   while not self._stop.is_set():
    self.status["last_tick"]=datetime.now(timezone.utc).isoformat()
    try:await self._tick(enqueue,list_boards,wait_job)
    except asyncio.CancelledError:raise
    except Exception as e:logger.exception("Amazon scheduler tick error: %s",e)
    try:await asyncio.wait_for(self._stop.wait(),timeout=SLOT_INTERVAL_SEC)
    except asyncio.TimeoutError:pass
  finally:self.status["running"]=False
 async def _tick(self,enqueue,list_boards,wait_job):
  if is_dormant():self.status["dormant_reason"]="no_composio";return
  live=await list_boards();gate=self.gate_status(live)
  if not gate["ready"]:self.status["dormant_reason"]="; ".join(gate["blocking_reasons"]);return
  self.status["dormant_reason"]=None;specs,_=build_slot_specs(live)
  if not specs:return
  day=ledger.ensure_day(slots_spec=specs);ledger.reclaim_stale_processing(day)
  if ledger.is_day_complete(day):return
  slot=ledger.next_pending_slot(day)
  if slot and ledger.claim_slot(day,int(slot["slot"])):await self._process_slot(slot,enqueue,wait_job,day)
 async def run_batch(self,batch_index:int,enqueue,list_boards,wait_job):
  if batch_index < 1 or batch_index > (SLOT_COUNT // BATCH_SIZE):raise ValueError(f"batch must be 1..{SLOT_COUNT // BATCH_SIZE}")
  if is_dormant():
   self.status["dormant_reason"]="no_composio";await send_failure_alert(batch=batch_index,reason="Composio Amazon discovery is unavailable");return {"status":"failed","batch":batch_index,"reason":"Composio Amazon discovery is unavailable"}
  live=await list_boards();gate=self.gate_status(live)
  if not gate["ready"]:
   self.status["dormant_reason"]="; ".join(gate["blocking_reasons"]);await send_failure_alert(batch=batch_index,reason="scheduler gate blocked",details="; ".join(gate["blocking_reasons"]));return {"status":"blocked","batch":batch_index,"reasons":gate["blocking_reasons"]}
  specs,_=build_slot_specs(live)
  if not specs:
   await send_failure_alert(batch=batch_index,reason="daily slot configuration unavailable");return {"status":"blocked","batch":batch_index,"reason":"daily slot configuration unavailable"}
  day=ledger.ensure_day(slots_spec=specs);ledger.reclaim_stale_processing(day);owner=f"batch-{batch_index}-{uuid.uuid4().hex}";claim=ledger.try_begin_batch(day,batch_index,owner);wait_cycles=0
  while not claim["acquired"] and claim.get("status")=="busy" and wait_cycles<160:
   await asyncio.sleep(15);wait_cycles+=1;claim=ledger.try_begin_batch(day,batch_index,owner)
  if not claim["acquired"]:
   if claim.get("status") not in ("completed","running"):await send_failure_alert(batch=batch_index,reason=str(claim.get("status")),details=str(claim))
   return {"status":claim["status"],"batch":batch_index,"result_json":claim.get("result_json"),"active_batch":claim.get("active_batch")}
  self.status.update({"current_batch":batch_index,"dormant_reason":None});first=(batch_index-1)*BATCH_SIZE+1;last=first+BATCH_SIZE-1;successes=0;attempted=0;errors=[]
  balance_meta={"available":False,"message":"Board balancing could not be verified because current Pinterest counts were unavailable."}
  slot_order=list(range(first,last+1))
  try:
   from board_balance import extract_board_rows,balance_state
   rows=extract_board_rows(live);balance_meta=balance_state(rows)
   if balance_meta.get("available"):
    name_to_count={r["name"]:r.get("pin_count") for r in (balance_meta.get("ranked_dedicated") or [])}
    def _slot_fill_key(slot_no):
     if 1<=slot_no<=len(CATEGORY_SLOTS):
      bname=CATEGORY_SLOTS[slot_no-1][1];c=name_to_count.get(bname);return (c is None,c if c is not None else 10**9,slot_no)
     return (True,10**9,slot_no)
    slot_order=sorted(slot_order,key=_slot_fill_key)
  except Exception as e:logger.warning("Board balance reorder skipped: %s",e)
  try:
   for slot_no in slot_order:
    slot=ledger.next_pending_slot(day,slot_no,slot_no)
    if not slot:continue
    if not ledger.claim_slot(day,slot_no):continue
    attempted+=1;ok=await self._process_slot(slot,enqueue,wait_job,day)
    if ok:successes+=1
    else:errors.append({"slot":slot_no,"status":"failed_or_exhausted"})
   status="completed" if attempted>0 and successes>=attempted and not errors else ("partial_failure" if successes>0 else "failed")
   result={"status":status,"day":day,"batch":batch_index,"attempted":attempted,"successes":successes,"errors":errors,"slots_required":attempted,"source":"composio_amazon","board_balance":{"available":balance_meta.get("available"),"state":balance_meta.get("state"),"spread":balance_meta.get("spread"),"message":balance_meta.get("message"),"slot_order":slot_order,"snapshot":balance_meta.get("snapshot")}}
   ledger.complete_batch(day,batch_index,owner,status=status,result_json=json.dumps(result,separators=(",",":")))
   if status!="completed":await send_failure_alert(batch=batch_index,reason=status,details=json.dumps(result))
   return result
  except Exception as e:
   logger.exception("Amazon batch %s failed",batch_index);ledger.complete_batch(day,batch_index,owner,status="failed",error=str(e)[:500]);await send_failure_alert(batch=batch_index,reason="unhandled batch exception",details=str(e));raise
  finally:self.status["current_batch"]=None
 async def _process_slot(self,slot,enqueue,wait_job,day):
  n=int(slot["slot"]);attempts=int(slot.get("replacement_attempts") or 0);exclude=set()
  while attempts<MAX_REPLACEMENTS_PER_SLOT:
   if 1<=n<=len(CATEGORY_SLOTS):candidate=await discover_category(CATEGORY_SLOTS[n-1][0],exclude_asins=exclude)
   else:candidate=await discover_global(exclude_asins=exclude)
   if not candidate:ledger.mark_slot(n,status="exhausted",day=day,error="no_candidates",inc_replacement=True);return False
   exclude.add(candidate["asin"]);url=candidate["affiliate_url"];ledger.mark_slot(n,status="processing",day=day,selected_asin=candidate["asin"],selected_url=url,affiliate_url=url,inc_replacement=True)
   try:result=await enqueue(url)
   except Exception as e:attempts+=1;ledger.mark_slot(n,status="failed_open",day=day,error=str(e)[:500]);continue
   job_id=result.get("job_id")
   if result.get("status")=="completed" and pinterest_any_verified(result):ledger.mark_slot(n,status="success",day=day,job_id=job_id,pinterest_verified=True,affiliate_url=url);return True
   if wait_job and job_id:
    final=await wait_job(job_id)
    final_result=final.get("result") or {}
    if final.get("status") in ("completed","completed_partial") and pinterest_any_verified(final_result):
     ledger.mark_slot(n,status="success",day=day,job_id=job_id,pinterest_verified=True,affiliate_url=url);return True
    attempts+=1;ledger.mark_slot(n,status="failed_open",day=day,job_id=job_id,error=str(final.get("error") or "no_verified_pin_published")[:500])
    try:registry.record_blocked(asin=candidate.get("asin"),product_url=url,affiliate_url=url,job_id=job_id,notes="no_verified_pin_published")
    except Exception:pass
    continue
   attempts+=1;ledger.mark_slot(n,status="failed_open",day=day,error="job_not_completed")
  ledger.mark_slot(n,status="exhausted",day=day,error="max_replacements");return False

amazon_scheduler=AmazonScheduler()
