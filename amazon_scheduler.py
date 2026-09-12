"""Dormant-by-default 15-success/day Amazon scheduler feeding only the existing /submit pipeline."""
from __future__ import annotations
import asyncio,logging,os
from datetime import datetime,timezone
from typing import Any,Awaitable,Callable,Dict,List,Optional
from amazon_client import amazon_credentials_present
from amazon_boards import build_slot_specs,REQUIRED_PRIMARY_SLOTS,classify_live_boards
from amazon_discovery import MAX_REPLACEMENTS_PER_SLOT,discover_for_board,discover_global,is_dormant
from daily_ledger import ledger,SLOT_COUNT
logger=logging.getLogger("pinterest-agent.amazon_scheduler")
SLOT_INTERVAL_SEC=int(os.getenv("AMAZON_SLOT_INTERVAL_SEC",str(96*60))); SCHEDULER_ENABLED=os.getenv("AMAZON_SCHEDULER_ENABLED","true").lower() in ("1","true","yes")
EnqueueFn=Callable[[str],Awaitable[Dict[str,Any]]]; ListBoardsFn=Callable[[],Awaitable[List[Dict[str,Any]]]]; WaitJobFn=Callable[[str],Awaitable[Dict[str,Any]]]
class AmazonScheduler:
    def __init__(self): self._task=None; self._stop=asyncio.Event(); self.status={"running":False,"dormant_reason":None,"last_tick":None}
    def gate_status(self,live_boards=None):
        creds=amazon_credentials_present(); ready=creds and SCHEDULER_ENABLED; info=classify_live_boards(live_boards or []); reasons=[]
        if not SCHEDULER_ENABLED: reasons.append("AMAZON_SCHEDULER_ENABLED is false")
        if not creds: reasons.append("Amazon credentials not configured (dormant)")
        if live_boards is not None and not info["scheduler_ready"]: reasons.append(f"Need {REQUIRED_PRIMARY_SLOTS} approved primary boards; have {info['primary_count']}")
        return {"credentials_present":creds,"scheduler_enabled_flag":SCHEDULER_ENABLED,"board_info":info,"ready":ready and (live_boards is None or info["scheduler_ready"]),"blocking_reasons":reasons,"slot_interval_sec":SLOT_INTERVAL_SEC,"daily_target":SLOT_COUNT}
    async def start(self,*,enqueue,list_boards,wait_job=None):
        if self._task and not self._task.done():return
        self._stop.clear(); self._task=asyncio.create_task(self._loop(enqueue,list_boards,wait_job),name="amazon-scheduler")
    async def stop(self):
        self._stop.set()
        if self._task:
            self._task.cancel()
            try: await self._task
            except asyncio.CancelledError: pass
            self._task=None
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
        finally:self.status["running"]=False
    async def _tick(self,enqueue,list_boards,wait_job):
        if is_dormant(): self.status["dormant_reason"]="no_credentials"; return
        live=await list_boards(); gate=self.gate_status(live)
        if not gate["ready"]: self.status["dormant_reason"]="; ".join(gate["blocking_reasons"]); return
        self.status["dormant_reason"]=None; specs,_=build_slot_specs(live)
        if not specs:return
        day=ledger.ensure_day(slots_spec=specs)
        if ledger.is_day_complete(day):return
        slot=ledger.next_pending_slot(day)
        if slot: await self._process_slot(slot,enqueue,wait_job)
    async def _process_slot(self,slot,enqueue,wait_job):
        n=int(slot["slot"]); attempts=int(slot.get("replacement_attempts") or 0); exclude=set()
        while attempts<MAX_REPLACEMENTS_PER_SLOT:
            candidate=await (discover_global(exclude_asins=exclude) if slot.get("slot_kind")=="global" else discover_for_board(slot.get("target_board_name") or "Everything Else",exclude_asins=exclude))
            if not candidate: ledger.mark_slot(n,status="exhausted",error="no_candidates",inc_replacement=True); return
            exclude.add(candidate["asin"]); url=candidate["affiliate_url"]; ledger.mark_slot(n,status="processing",selected_asin=candidate["asin"],selected_url=url,affiliate_url=url,inc_replacement=True)
            try: result=await enqueue(url)
            except Exception as e: attempts+=1; ledger.mark_slot(n,status="failed_open",error=str(e)[:500]); continue
            job_id=result.get("job_id")
            if result.get("status")=="completed": ledger.mark_slot(n,status="success",job_id=job_id,pinterest_verified=True,affiliate_url=url); return
            if wait_job and job_id:
                final=await wait_job(job_id)
                if final.get("status")=="completed": ledger.mark_slot(n,status="success",job_id=job_id,pinterest_verified=True,affiliate_url=url); return
                attempts+=1; ledger.mark_slot(n,status="failed_open",job_id=job_id,error=str(final.get("error") or "job_failed")[:500]); continue
            return
        ledger.mark_slot(n,status="exhausted",error="max_replacements")
amazon_scheduler=AmazonScheduler()
