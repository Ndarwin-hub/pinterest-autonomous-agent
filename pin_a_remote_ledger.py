"""Remote adapter for the Pin A scheduler's SQLite ledger.

The authoritative SQLite database remains on the existing web service volume.
The dedicated Pin A worker executes the scheduler and uses this adapter only for
durable ledger operations, so Video A deployments cannot terminate Pin A.
"""
from __future__ import annotations
import os
from typing import Any, Dict
import httpx

class RemotePinALedger:
    def __init__(self, base_url: str | None = None, secret: str | None = None):
        self.base_url = (base_url or os.getenv("PIN_A_WEB_URL", "")).rstrip("/")
        self.secret = secret or os.getenv("PIN_A_WORKER_SECRET", "")
        if not self.base_url:
            raise RuntimeError("PIN_A_WEB_URL is required")
        if not self.secret:
            raise RuntimeError("PIN_A_WORKER_SECRET is required")
        self.timeout = float(os.getenv("PIN_A_LEDGER_RPC_TIMEOUT_SEC", "45"))
        self._client = httpx.Client(timeout=self.timeout)

    def _call(self, operation: str, **args: Any) -> Any:
        response = self._client.post(
            f"{self.base_url}/internal/pin-a/ledger",
            headers={"X-Pin-A-Worker-Secret": self.secret},
            json={"operation": operation, "args": args},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok"):
            raise RuntimeError(str(payload.get("error") or "Pin A ledger RPC failed"))
        return payload.get("result")

    def today_str(self): return self._call("today_str")
    def get_day_status(self, day=None): return self._call("get_day_status", day=day)
    def next_unfinished_batch(self, day=None, max_batch=10): return self._call("next_unfinished_batch", day=day, max_batch=max_batch)
    def is_day_complete(self, day=None): return self._call("is_day_complete", day=day)
    def reclaim_orphaned_batches(self, day=None): return self._call("reclaim_orphaned_batches", day=day)
    def reclaim_stale_processing(self, day=None): return self._call("reclaim_stale_processing", day=day)
    def ensure_day(self, day=None, slots_spec=None): return self._call("ensure_day", day=day, slots_spec=slots_spec)
    def next_recovery_slot(self, day=None): return self._call("next_recovery_slot", day=day)
    def claim_recovery_slot(self, day, slot): return self._call("claim_recovery_slot", day=day, slot=slot)
    def mark_slot(self, slot, *, status, day=None, selected_asin=None, selected_url=None, affiliate_url=None, job_id=None, pinterest_verified=False, error=None, inc_replacement=False):
        return self._call("mark_slot", slot=slot, status=status, day=day, selected_asin=selected_asin, selected_url=selected_url, affiliate_url=affiliate_url, job_id=job_id, pinterest_verified=pinterest_verified, error=error, inc_replacement=inc_replacement)
    def next_pending_slot(self, day=None, slot_min=1, slot_max=50): return self._call("next_pending_slot", day=day, slot_min=slot_min, slot_max=slot_max)
    def claim_slot(self, day, slot): return self._call("claim_slot", day=day, slot=slot)
    def try_begin_batch(self, day, batch_index, owner): return self._call("try_begin_batch", day=day, batch_index=batch_index, owner=owner)
    def complete_batch(self, day, batch_index, owner, status="completed", result_json=None, error=None):
        return self._call("complete_batch", day=day, batch_index=batch_index, owner=owner, status=status, result_json=result_json, error=error)
    def historical_selected_asins(self, exclude_day=None): return self._call("historical_selected_asins", exclude_day=exclude_day)
    def enqueue_video_job(self, day, batch, slot, asin, product_url, affiliate_url=None, source="pinterest", title=None):
        return self._call("enqueue_video_job", day=day, batch=batch, slot=slot, asin=asin, product_url=product_url, affiliate_url=affiliate_url, source=source, title=title)
    def close_day(self, day=None, reason="daily_session_finished"): return self._call("close_day", day=day, reason=reason)
    def record_scheduler_event(self, day=None, batch_requested=None, scheduler_run_id=None, scheduled_local_time=None, github_delay_seconds=0, github_queued_runs=0, github_active_runs=0, github_load_class="UNKNOWN"):
        return self._call("record_scheduler_event", day=day, batch_requested=batch_requested, scheduler_run_id=scheduler_run_id, scheduled_local_time=scheduled_local_time, github_delay_seconds=github_delay_seconds, github_queued_runs=github_queued_runs, github_active_runs=github_active_runs, github_load_class=github_load_class)
    def enqueue_pin_a_request(self, request_id, source, day=None): return self._call("enqueue_pin_a_request", request_id=request_id, source=source, day=day)
    def recover_pin_a_requests(self): return self._call("recover_pin_a_requests")
    def claim_next_pin_a_request(self): return self._call("claim_next_pin_a_request")
    def finish_pin_a_request(self, request_id, status="completed", error=None): return self._call("finish_pin_a_request", request_id=request_id, status=status, error=error)
