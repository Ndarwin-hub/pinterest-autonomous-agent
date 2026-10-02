"""Dedicated Railway Pin A worker.

Pin A execution lives in this service. The existing web service keeps the
authoritative SQLite ledger and runs the normal /submit job pipeline; this
worker only orchestrates the daily Pin A scheduler and calls the web service
over Railway private networking for durable ledger operations and job polling.
"""
from __future__ import annotations
import asyncio
import logging
import os
from typing import Any, Dict
import httpx

import agent as agent_module
import amazon_scheduler as scheduler_module
from pin_a_remote_ledger import RemotePinALedger

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger("pinterest-agent.pin-a-worker")

WEB_URL = os.getenv("PIN_A_WEB_URL", "").rstrip("/")
WORKER_SECRET = os.getenv("PIN_A_WORKER_SECRET", "").strip()
API_SECRET = os.getenv("API_SECRET", "").strip()
if not WEB_URL or not WORKER_SECRET:
    raise RuntimeError("PIN_A_WEB_URL and PIN_A_WORKER_SECRET are required")

ledger = RemotePinALedger(WEB_URL, WORKER_SECRET)
scheduler_module.ledger = ledger
amazon_scheduler = scheduler_module.amazon_scheduler

_client = httpx.AsyncClient(timeout=float(os.getenv("PIN_A_WEB_HTTP_TIMEOUT_SEC", "60")))

async def _json_request(method: str, path: str, **kwargs: Any) -> Dict[str, Any]:
    response = await _client.request(method, f"{WEB_URL}{path}", **kwargs)
    response.raise_for_status()
    return response.json()

async def list_boards():
    data = await agent_module.run_composio_tool("PINTEREST_LIST_BOARDS", {})
    return data.get("items") or data.get("boards") or []

async def enqueue(url: str, target_board_id: str | None = None, target_board_name: str | None = None, force_new: bool = False):
    payload = {
        "url": url,
        "target_board_id": target_board_id,
        "target_board_name": target_board_name,
        "force_new": bool(force_new),
    }
    return await _json_request(
        "POST",
        "/internal/pin-a/submit",
        headers={"X-Pin-A-Worker-Secret": WORKER_SECRET},
        json=payload,
    )

async def wait_job(job_id: str):
    for _ in range(180):
        await asyncio.sleep(10)
        try:
            job = await _json_request(
                "GET",
                f"/status/{job_id}",
                headers={"X-API-Secret": API_SECRET},
            )
        except Exception as exc:
            logger.warning("Pin A worker status poll failed job_id=%s: %s", job_id, exc)
            continue
        if str(job.get("status") or "") in ("completed", "completed_partial", "failed"):
            return {"status": job.get("status"), "error": job.get("error"), "result": job.get("result")}
    return {"status": "timeout"}

async def _request_loop():
    ledger.recover_pin_a_requests()
    while True:
        req = None
        try:
            req = ledger.claim_next_pin_a_request()
            if not req:
                await asyncio.sleep(5)
                continue
            request_id = req["request_id"]
            day = req["day"]
            logger.info("Dedicated Pin A worker claimed request_id=%s source=%s day=%s", request_id, req["source"], day)
            result = await amazon_scheduler.start_daily_session(
                enqueue,
                list_boards,
                wait_job,
                trigger_batch=1,
            )
            status = str(result.get("status") or "")
            if status in ("session_started", "already_running", "already_completed"):
                ledger.finish_pin_a_request(request_id, "completed")
            elif status == "ignored":
                ledger.finish_pin_a_request(request_id, "failed", str(result.get("reason") or "scheduler_ignored"))
            else:
                ledger.finish_pin_a_request(request_id, "pending", f"dispatch returned {status or 'unknown'}")
            await asyncio.sleep(0.1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Dedicated Pin A worker loop error request_id=%s: %s", (req or {}).get("request_id"), exc)
            if req:
                ledger.finish_pin_a_request(req["request_id"], "pending", str(exc)[:1000])
            await asyncio.sleep(5)

async def main():
    logger.info("Dedicated Pin A worker starting; executor isolated from web/video lifecycle")
    task = asyncio.create_task(_request_loop(), name="dedicated-pin-a-request-loop")
    try:
        await task
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await _client.aclose()

if __name__ == "__main__":
    asyncio.run(main())
