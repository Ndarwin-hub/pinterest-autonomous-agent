"""External-wake Amazon scheduler feeding the existing Pinterest pipeline."""
from __future__ import annotations
import asyncio,logging,os,uuid,json
from datetime import datetime,timezone
from typing import Any,Awaitable,Callable,Dict,List
from amazon_client import amazon_credentials_present
from amazon_boards import build_slot_specs,REQUIRED_PRIMARY_SLOTS,classify_live_boards,BOARD_SCOPES
from amazon_discovery import MAX_REPLACEMENTS_PER_SLOT,discover_for_board,discover_global,is_dormant
from amazon_composio_discovery import discover_category
from daily_ledger import ledger,SLOT_COUNT,BATCH_SIZE
from published_registry import registry
from amazon_alerts import send_failure_alert,notify_daily_started
from pin_config import PINS_PER_PRODUCT
logger=logging.getLogger("pinterest-agent.amazon_scheduler")
SLOT_INTERVAL_SEC=int(os.getenv("AMAZON_SLOT_INTERVAL_SEC",str(48*60)));PIN_BLOCK_COOLDOWN_MIN=int(os.getenv("PINTEREST_BLOCK_COOLDOWN_MIN","30") or "30");PIN_BLOCK_MAX_COOLDOWN_MIN=int(os.getenv("PINTEREST_BLOCK_MAX_COOLDOWN_MIN","360") or "360");SCHEDULER_ENABLED=os.getenv("AMAZON_SCHEDULER_ENABLED","true").lower() in ("1","true","yes");SCHEDULER_MODE=os.getenv("AMAZON_SCHEDULER_MODE","external").strip().lower()
EnqueueFn=Callable[[str],Awaitable[Dict[str,Any]]];ListBoardsFn=Callable[[],Awaitable[List[Dict[str,Any]]]];WaitJobFn=Callable[[str],Awaitable[Dict[str,Any]]]
def pinterest_any_verified(result:Dict[str,Any])->bool:
 pins=result.get("pins") if isinstance(result,dict) else None
 return isinstance(pins,list) and any(isinstance(p,dict) and bool(p.get("verified")) and bool(p.get("pin_id")) and bool(p.get("destination_url")) for p in pins)
def pinterest_target_verified(result:Dict[str,Any])->bool:
 pins=result.get("pins") if isinstance(result,dict) else None
 return isinstance(pins,list) and len(pins)==PINS_PER_PRODUCT and all(isinstance(p,dict) and bool(p.get("verified")) for p in pins)
class AmazonScheduler:
 def __init__(self):
  self._task=None;self._stop=asyncio.Event();self._daily_task=None;self._daily_stop=asyncio.Event();self._daily_day=None
  self.status={"running":False,"dormant_reason":None,"last_tick":None,"mode":SCHEDULER_MODE,"current_batch":None,"source":"composio","pinterest_circuit_breaker":{"active":False,"until":None,"cooldown_minutes":0},"daily_session":{"running":False,"day":None,"started_at":None,"completed_at":None,"next_batch":None}}
 async def start_daily_session(self,enqueue,list_boards,wait_job=None,trigger_batch=None):
  if SCHEDULER_MODE!="external": return {"status":"ignored","reason":"not_external_mode"}
  day=ledger.today_str()
  if self._daily_task and not self._daily_task.done():
   if self._daily_day==day:
    return {"status":"already_running","day":day,"next_batch":ledger.next_unfinished_batch(day)}
   # Midnight/day rollover: never let yesterday's executor continue into today's allocation.
   old_day=self._daily_day
   self._daily_stop.set(); self._daily_task.cancel()
   try: await self._daily_task
   except asyncio.CancelledError: pass
   self._daily_task=None
   logger.info("Closed prior daily session day=%s before starting fresh day=%s",old_day,day)
  if ledger.is_day_complete(day):
   return {"status":"already_completed","day":day}
  # Materialize today's 50 fresh slot records now. Existing rows from other days are never reused.
  try:
   live=await list_boards()
   specs,_=build_slot_specs(live)
   if specs: ledger.ensure_day(day,slots_spec=specs)
  except Exception as e:
   logger.warning("Fresh-day slot initialization deferred to batch execution: %s",e)
  self._daily_day=day;self._daily_stop.clear()
  self.status["daily_session"]={"running":True,"day":day,"started_at":datetime.now(timezone.utc).isoformat(),"completed_at":None,"next_batch":ledger.next_unfinished_batch(day)}
  await notify_daily_started(day,trigger_batch or 0,None)
  self._daily_task=asyncio.create_task(self._daily_loop(enqueue,list_boards,wait_job,day,trigger_batch),name=f"amazon-daily-session-{day}")
  return {"status":"session_started","day":day,"next_batch":self.status["daily_session"]["next_batch"]}
 async def _daily_loop(self,enqueue,list_boards,wait_job,day,trigger_batch=None):
  try:
   recovery_mode=False
   while not self._daily_stop.is_set():
    if ledger.is_day_complete(day): break
    if not recovery_mode:
     batch=ledger.next_unfinished_batch(day,max_batch=SLOT_COUNT//BATCH_SIZE)
     if batch is None:
      recovery_mode=True
      logger.info("Normal daily pass reached Batch 10/terminal slots; entering final recovery pass.")
      continue
     wait_seconds=self.pinterest_circuit_wait_seconds()
     if wait_seconds>0:
      logger.warning("Pinterest circuit breaker active; pausing daily session for %ss before the next batch.",wait_seconds)
      try: await asyncio.wait_for(self._daily_stop.wait(),timeout=wait_seconds)
      except asyncio.TimeoutError: pass
      continue
     result=await self.run_batch(batch,enqueue,list_boards,wait_job)
     status=str(result.get("status") or "")
     if status in ("blocked","failed","partial_failure"):
      logger.warning("Daily session batch %s returned %s; continuing with today's fresh ledger allocation only.",batch,status)
    else:
     slot=ledger.next_recovery_slot(day)
     if not slot:
      break
     if not ledger.claim_recovery_slot(day,int(slot["slot"])):
      continue
     logger.info("Final recovery attempting deferred/exhausted slot %s.",slot["slot"])
     ok=await self._process_slot(slot,enqueue,wait_job,day,recovery=True)
     if not ok:
      current=ledger.next_recovery_slot(day)
      if not current or int(current.get("slot") or -1)!=int(slot["slot"]) or current.get("status")!="deferred":
       ledger.mark_slot(int(slot["slot"]),status="exhausted",day=day,error="final_recovery_exhausted")
    if ledger.is_day_complete(day): break
    try:
     await asyncio.wait_for(self._daily_stop.wait(),timeout=SLOT_INTERVAL_SEC)
    except asyncio.TimeoutError:
     try:
      import httpx
      public_domain=os.getenv("RAILWAY_PUBLIC_DOMAIN","pinterest-autonomous-agent-production.up.railway.app").strip()
      async with httpx.AsyncClient(timeout=15.0) as client:
       await client.get(f"https://{public_domain}/health?daily_heartbeat=1")
     except Exception as heartbeat_error:
      logger.warning("Daily session heartbeat failed: %s",heartbeat_error)
  except asyncio.CancelledError: raise
  except Exception as e:
   logger.exception("Daily Amazon session failed: %s",e)
  finally:
   complete=ledger.is_day_complete(day)
   self.status["daily_session"]={"running":False,"day":day,"started_at":self.status.get("daily_session",{}).get("started_at"),"completed_at":datetime.now(timezone.utc).isoformat() if complete else None,"next_batch":ledger.next_unfinished_batch(day)}
   self._daily_task=None
   self.status["current_batch"]=None
   if complete: logger.info("DAILY AMAZON SESSION COMPLETE for %s; service is now idle and may sleep.",day)
 async def stop_daily_session(self):
  self._daily_stop.set()
  if self._daily_task:
   self._daily_task.cancel()
   try: await self._daily_task
   except asyncio.CancelledError: pass
   self._daily_task=None
 def gate_status(self,live_boards=None):
  amazon_source_ready=not is_dormant();ready=amazon_source_ready and SCHEDULER_ENABLED;info=classify_live_boards(live_boards or []);reasons=[]
  if not SCHEDULER_ENABLED:reasons.append("AMAZON_SCHEDULER_ENABLED is false")
  if not amazon_source_ready:reasons.append("Composio Amazon connection/configuration is not available")
  if live_boards is not None and not info["scheduler_ready"]:reasons.append(f"Need {REQUIRED_PRIMARY_SLOTS} approved primary boards; have {info['primary_count']}")
  return {"credentials_present":amazon_source_ready,"scheduler_enabled_flag":SCHEDULER_ENABLED,"mode":SCHEDULER_MODE,"source":"amazon_api_primary_composio_fallback" if amazon_credentials_present() else "composio_amazon","amazon_api_credentials_present":amazon_credentials_present(),"board_info":info,"ready":ready and (live_boards is None or info["scheduler_ready"]),"blocking_reasons":reasons,"slot_interval_sec":SLOT_INTERVAL_SEC,"daily_target":SLOT_COUNT}
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
 def pinterest_circuit_wait_seconds(self):
  state=self.status.get("pinterest_circuit_breaker") or {}
  until=state.get("until")
  if not until:return 0
  try: remaining=max(0,int(float(until)-datetime.now(timezone.utc).timestamp()))
  except Exception:return 0
  if remaining<=0:
   self.status["pinterest_circuit_breaker"]={"active":False,"until":None,"cooldown_minutes":0}
   return 0
  return remaining

 def activate_pinterest_circuit(self,reason:str):
  previous=int((self.status.get("pinterest_circuit_breaker") or {}).get("cooldown_minutes") or 0)
  cooldown=PIN_BLOCK_COOLDOWN_MIN if previous<=0 else min(PIN_BLOCK_MAX_COOLDOWN_MIN,max(PIN_BLOCK_COOLDOWN_MIN,previous*2))
  until=datetime.now(timezone.utc).timestamp()+cooldown*60
  self.status["pinterest_circuit_breaker"]={"active":True,"until":until,"cooldown_minutes":cooldown,"reason":reason[:500]}
  logger.warning("Pinterest circuit breaker ACTIVE for %s minutes: %s",cooldown,reason[:300])

 @staticmethod
 def is_pinterest_block_error(value:Any)->bool:
  text=str(value or "").lower()
  return any(m in text for m in ("pinterest rate limit exceeded","you've hit a block (pins)","you have hit a block (pins)","combat spam","rate limit block","too many requests"))

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
  day=ledger.ensure_day(slots_spec=specs);ledger.reclaim_stale_processing(day);logger.info("Amazon batch %s slot states=%s",batch_index,[(s.get("slot"),s.get("status")) for s in ledger.get_day_status(day).get("slots",[]) if int(s.get("slot") or 0) >= (batch_index-1)*BATCH_SIZE+1 and int(s.get("slot") or 0) <= batch_index*BATCH_SIZE]);owner=f"batch-{batch_index}-{uuid.uuid4().hex}";claim=ledger.try_begin_batch(day,batch_index,owner);wait_cycles=0
  while not claim["acquired"] and claim.get("status")=="busy" and wait_cycles<160:
   await asyncio.sleep(15);wait_cycles+=1;claim=ledger.try_begin_batch(day,batch_index,owner)
  if not claim["acquired"]:
   if claim.get("status") not in ("completed","running"):await send_failure_alert(batch=batch_index,reason=str(claim.get("status")),details=str(claim))
   return {"status":claim["status"],"batch":batch_index,"result_json":claim.get("result_json"),"active_batch":claim.get("active_batch")}
  self.status.update({"current_batch":batch_index,"dormant_reason":None});first=(batch_index-1)*BATCH_SIZE+1;last=first+BATCH_SIZE-1;successes=0;attempted=0;errors=[]
  slot_order=list(range(first,last+1))
  balance_meta={"available":False,"message":"Serial Pinterest board order is authoritative; board-count balancing is disabled."}
  try:
   for slot_no in slot_order:
    slot=ledger.next_pending_slot(day,slot_no,slot_no)
    if not slot:continue
    if not ledger.claim_slot(day,slot_no):continue
    attempted+=1;ok=await self._process_slot(slot,enqueue,wait_job,day)
    if ok:successes+=1
    else:errors.append({"slot":slot_no,"status":"failed_or_deferred"})
    if self.pinterest_circuit_wait_seconds()>0:
     # Do not spend more Pinterest CREATE_PIN attempts in this batch after a block.
     for remaining in slot_order[slot_order.index(slot_no)+1:]:
      pending=ledger.next_pending_slot(day,remaining,remaining)
      if pending:
       ledger.claim_slot(day,remaining)
       ledger.mark_slot(remaining,status="deferred",day=day,error="pinterest_circuit_breaker_active")
     break
   status="completed" if attempted>0 and successes>=attempted and not errors else ("partial_failure" if successes>0 else "failed")
   result={"status":status,"day":day,"batch":batch_index,"attempted":attempted,"successes":successes,"errors":errors,"slots_required":attempted,"source":"composio_amazon","board_balance":{"available":balance_meta.get("available"),"state":balance_meta.get("state"),"spread":balance_meta.get("spread"),"message":balance_meta.get("message"),"slot_order":slot_order,"snapshot":balance_meta.get("snapshot")}}
   ledger.complete_batch(day,batch_index,owner,status=status,result_json=json.dumps(result,separators=(",",":")))
   if status!="completed":await send_failure_alert(batch=batch_index,reason=status,details=json.dumps(result))
   return result
  except Exception as e:
   logger.exception("Amazon batch %s failed",batch_index);ledger.complete_batch(day,batch_index,owner,status="failed",error=str(e)[:500]);await send_failure_alert(batch=batch_index,reason="unhandled batch exception",details=str(e));raise
  finally:self.status["current_batch"]=None
 async def _process_slot(self,slot,enqueue,wait_job,day,recovery=False):
  n=int(slot["slot"]);attempts=int(slot.get("replacement_attempts") or 0)
  # A new day never resumes an ASIN selected by a previous day's unfinished slot.
  exclude=set(ledger.historical_selected_asins(exclude_day=day))
  while attempts<MAX_REPLACEMENTS_PER_SLOT:
   target_board_name=str(slot.get("target_board_name") or "Everything Else")
   target_board_id=str(slot.get("target_board_id") or "")
   logger.info("Amazon slot %s discovery attempt=%s board_serial=%s board=%s",n,attempts+1,slot.get("board_serial"),target_board_name)
   candidate=await discover_for_board(target_board_name,exclude_asins=exclude)
   if not candidate:
    logger.warning("Amazon slot %s produced no fresh candidate",n);ledger.mark_slot(n,status="exhausted",day=day,error="no_candidates",inc_replacement=True);return False
   exclude.add(candidate.get("asin") or "")
   url=candidate["affiliate_url"];logger.info("Amazon slot %s selected asin=%s board=%s recovery=%s",n,candidate.get("asin"),target_board_name,recovery)
   if not recovery:
    ledger.mark_slot(n,status="processing",day=day,selected_asin=candidate.get("asin"),selected_url=url,affiliate_url=url,inc_replacement=True)
   try:
    result=await enqueue(url,target_board_id,target_board_name);logger.info("Amazon slot %s enqueue accepted board=%s job_id=%s status=%s",n,target_board_name,result.get("job_id"),result.get("status"))
   except Exception as e:attempts+=1;logger.exception("Amazon slot %s enqueue failed",n);ledger.mark_slot(n,status="failed_open",day=day,error=str(e)[:500]);continue
   job_id=result.get("job_id")
   if self.is_pinterest_block_error(json.dumps(result,separators=(",",":"))):
    self.activate_pinterest_circuit(json.dumps(result,separators=(",",":")))
    ledger.mark_slot(n,status="deferred",day=day,job_id=job_id,error="pinterest_rate_or_spam_block")
    return False
   if pinterest_target_verified(result):logger.info("Amazon slot %s completed inline with all target Pins verified",n);ledger.mark_slot(n,status="success",day=day,job_id=job_id,pinterest_verified=True,affiliate_url=url);return True
   if result.get("status")=="completed_partial" and pinterest_any_verified(result):
    ledger.mark_slot(n,status="partial",day=day,job_id=job_id,pinterest_verified=True,affiliate_url=url,error="partial_pin_set_recovery_pending")
    return False
   if wait_job and job_id:
    logger.info("Amazon slot %s waiting for job_id=%s",n,job_id)
    final=await wait_job(job_id)
    logger.info("Amazon slot %s job_id=%s finished status=%s",n,job_id,final.get("status"))
    final_result=final.get("result") or {}
    combined_error=json.dumps(final_result,separators=(",",":"))+" "+str(final.get("error") or "")
    if self.is_pinterest_block_error(combined_error):
     self.activate_pinterest_circuit(combined_error)
     ledger.mark_slot(n,status="deferred",day=day,job_id=job_id,error="pinterest_rate_or_spam_block")
     return False
    if pinterest_target_verified(final_result):
     ledger.mark_slot(n,status="success",day=day,job_id=job_id,pinterest_verified=True,affiliate_url=url);return True
    if final.get("status") == "completed_partial" and pinterest_any_verified(final_result):
     ledger.mark_slot(n,status="partial",day=day,job_id=job_id,pinterest_verified=True,affiliate_url=url,error="partial_pin_set_recovery_pending");return False
    attempts+=1;ledger.mark_slot(n,status="failed_open",day=day,job_id=job_id,error=str(final.get("error") or "no_verified_pin_published")[:500])
    try:registry.record_blocked(asin=candidate.get("asin"),product_url=url,affiliate_url=url,job_id=job_id,notes="no_verified_pin_published")
    except Exception:pass
    continue
   attempts+=1;ledger.mark_slot(n,status="failed_open",day=day,error="job_not_completed")
  ledger.mark_slot(n,status="exhausted",day=day,error="max_replacements");return False

amazon_scheduler=AmazonScheduler()
