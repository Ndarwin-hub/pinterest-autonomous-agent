"""External-wake Amazon scheduler feeding only the existing 5-Pin pipeline."""
from __future__ import annotations
import asyncio, logging, os, uuid, json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List
from amazon_client import amazon_credentials_present
from amazon_boards import build_slot_specs, REQUIRED_PRIMARY_SLOTS, classify_live_boards
from amazon_discovery import MAX_REPLACEMENTS_PER_SLOT, discover_for_board, discover_global, is_dormant
from daily_ledger import ledger, SLOT_COUNT, BATCH_SIZE
logger=logging.getLogger("pinterest-agent.amazon_scheduler")
SLOT_INTERVAL_SEC=int(os.getenv("AMAZON_SLOT_INTERVAL_SEC",str(96*60))); SCHEDULER_ENABLED=os.getenv("AMAZON_SCHEDULER_ENABLED","true").lower() in ("1","true","yes"); SCHEDULER_MODE=os.getenv("AMAZON_SCHEDULER_MODE","external").strip().lower()
EnqueueFn=Callable[[str],Awaitable[Dict[str,Any]]]; ListBoardsFn=Callable[[],Awaitable[List[Dict[str,Any]]]]; WaitJobFn=Callable[[str],Awaitable[Dict[str,Any]]]
class AmazonScheduler:
    def __init__(self): self._task=None; self._stop=asyncio.Event(); self.status={"running":False,"dormant_reason":None,"last_tick":None,"mode":SCHEDULER_MODE,"current_batch":None}
    def gate_status(self,live_boards=None):
        creds=amazon_credentials_present(); ready=creds and SCHEDULER_ENABLED; info=classify_live_boards(live_boards or []); reasons=[]
        if not SCHEDULER_ENABLED: reasons.append("AMAZON_SCHEDULER_ENABLED is false")
        if not creds: reasons.append("Amazon credentials not configured (dormant)")
        if live_boards is not None and not info["scheduler_ready"]: reasons.append(f"Need {REQUIRED_PRIMARY_SLOTS} approved primary boards; have {info['primary_count']}")
        return {"credentials_present":creds,"scheduler_enabled_flag":SCHEDULER_ENABLED,"mode":SCHEDULER_MODE,"board_info":info,"ready":ready and (live_boards is None or info["scheduler_ready"]),"blocking_reasons":reasons,"slot_interval_sec":SLOT_INTERVAL_SEC,"daily_target":SLOT_COUNT}
    async def start(self,*,enqueue,list_boards,wait_job=None):
        if SCHEDULER_MODE!="continuous": self.status.update({"mode":SCHEDULER_MODE,"running":False,"dormant_reason":"external_mode"}); return
        if self._task and not self._task.done(): return
        self._stop.clear(); self._task=asyncio.create_task(self._loop(enqueue,list_boards,wait_job),name="amazon-scheduler")
    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
            self._task=None
        self.status["running"]=False
    async def _loop(self,enqueue,list_boards,wait_job):
        self.status["running"]=True
        try:
            while not self._stop.is_set():
                self.status["last_tick"]=datetime.now(timezone.utc).isoformat()
                try: await self._tick(enqueue,list_boards,wait_job)
                except asyncio.CancelledError: raise
                except Exception as e: logger.exception("Amazon scheduler tick error: %s",e)
                try: await asyncio.wait_for(self._stop.wait(),timeout=SLOT_INTERVAL_SEC)
                except asyncio.TimeoutError: pass
        finally: self.status["running"]=False
    async def _tick(self,enqueue,list_boards,wait_job):
        if is_dormant(): self.status["dormant_reason"]="no_credentials"; return
        live=await list_boards(); gate=self.gate_status(live)
        if not gate["ready"]: self.status["dormant_reason"]="; ".join(gate["blocking_reasons"]); return
        self.status["dormant_reason"]=None; specs,_=build_slot_specs(live)
        if not specs:return
        day=ledger.ensure_day(slots_spec=specs); ledger.reclaim_stale_processing(day)
        if ledger.is_day_complete(day):return
        slot=ledger.next_pending_slot(day)
        if slot and ledger.claim_slot(day,int(slot["slot"])): await self._process_slot(slot,enqueue,wait_job,day)
    async def run_batch(self,batch_index:int,enqueue,list_boards,wait_job):
        if batch_index not in (1,2,3): raise ValueError("batch must be 1, 2, or 3")
        if is_dormant(): self.status["dormant_reason"]="no_credentials"; return {"status":"dormant","batch":batch_index,"reason":"Amazon credentials not configured"}
        live=await list_boards(); gate=self.gate_status(live)
        if not gate["ready"]: self.status["dormant_reason"]="; ".join(gate["blocking_reasons"]); return {"status":"blocked","batch":batch_index,"reasons":gate["blocking_reasons"]}
        specs,_=build_slot_specs(live)
        if len(specs)<SLOT_COUNT: return {"status":"blocked","batch":batch_index,"reason":"15 daily slots unavailable"}
        day=ledger.ensure_day(slots_spec=specs); ledger.reclaim_stale_processing(day); owner=f"batch-{batch_index}-{uuid.uuid4().hex}"; claim=ledger.try_begin_batch(day,batch_index,owner); wait_cycles=0
        while not claim["acquired"] and claim.get("status")=="busy" and wait_cycles<160:
            await asyncio.sleep(15); wait_cycles+=1; claim=ledger.try_begin_batch(day,batch_index,owner)
        if not claim["acquired"]: return {"status":claim["status"],"batch":batch_index,"result_json":claim.get("result_json"),"active_batch":claim.get("active_batch")}
        self.status.update({"current_batch":batch_index,"dormant_reason":None}); first=(batch_index-1)*BATCH_SIZE+1; last=first+BATCH_SIZE-1; successes=0; attempted=0; errors=[]
        try:
            for slot_no in range(first,last+1):
                slot=ledger.next_pending_slot(day,slot_no,slot_no)
                if not slot: continue
                if not ledger.claim_slot(day,slot_no): continue
                attempted+=1; ok=await self._process_slot(slot,enqueue,wait_job,day)
                if ok: successes+=1
                else: errors.append({"slot":slot_no,"status":"failed_or_exhausted"})
            result={"status":"completed","day":day,"batch":batch_index,"attempted":attempted,"successes":successes,"errors":errors}; ledger.complete_batch(day,batch_index,owner,result_json=json.dumps(result,separators=(",",":"))); return result
        except Exception as e:
            logger.exception("Amazon batch %s failed",batch_index); ledger.complete_batch(day,batch_index,owner,status="failed",error=str(e)[:500]); raise
        finally: self.status["current_batch"]=None
    async def _process_slot(self,slot,enqueue,wait_job,day):
        n=int(slot["slot"]); attempts=int(slot.get("replacement_attempts") or 0); exclude=set()
        while attempts<MAX_REPLACEMENTS_PER_SLOT:
            candidate=await (discover_global(exclude_asins=exclude) if slot.get("slot_kind")=="global" else discover_for_board(slot.get("target_board_name") or "Everything Else",exclude_asins=exclude))
            if not candidate: ledger.mark_slot(n,status="exhausted",day=day,error="no_candidates",inc_replacement=True); return False
            exclude.add(candidate["asin"]); url=candidate["affiliate_url"]; ledger.mark_slot(n,status="processing",day=day,selected_asin=candidate["asin"],selected_url=url,affiliate_url=url,inc_replacement=True)
            try: result=await enqueue(url)
            except Exception as e: attempts+=1; ledger.mark_slot(n,status="failed_open",day=day,error=str(e)[:500]); continue
            job_id=result.get("job_id")
            if result.get("status")=="completed": ledger.mark_slot(n,status="success",day=day,job_id=job_id,pinterest_verified=True,affiliate_url=url); return True
            if wait_job and job_id:
                final=await wait_job(job_id)
                if final.get("status")=="completed": ledger.mark_slot(n,status="success",day=day,job_id=job_id,pinterest_verified=True,affiliate_url=url); return True
                attempts+=1; ledger.mark_slot(n,status="failed_open",day=day,job_id=job_id,error=str(final.get("error") or "job_failed")[:500]); continue
            attempts+=1; ledger.mark_slot(n,status="failed_open",day=day,error="job_not_completed"); continue
        ledger.mark_slot(n,status="exhausted",day=day,error="max_replacements"); return False
amazon_scheduler=AmazonScheduler()
