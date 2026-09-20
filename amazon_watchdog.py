"""Autonomous watchdog for all externally scheduled Amazon batches.

Runs inside the Railway web process as a safety net. It never touches /submit
or the manual Pin workflow. The existing daily ledger/idempotency remains the
source of truth for batch ownership.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from daily_ledger import ledger

logger = logging.getLogger("pinterest-agent.amazon-watchdog")

BATCH_UTC_MINUTES = {
    1: 23,
    2: 71,
    3: 119,
    4: 167,
    5: 215,
    6: 263,
    7: 311,
    8: 359,
    9: 407,
    10: 455,
}

async def check_batch(batch: int) -> dict:
    day = ledger.today_str()
    status = ledger.get_day_status(day)
    rows = {int(x["batch"]): x for x in status.get("batches", [])}
    row = rows.get(batch)
    if not row:
        return {"status": "missing", "day": day, "batch": batch}
    state = row.get("status")
    return {"status": "ok" if state in ("running", "completed") else "attention",
            "day": day, "batch": batch, "batch_status": state}

def due_batch(now: datetime) -> int | None:
    minute = now.hour * 60 + now.minute
    due = [b for b, m in BATCH_UTC_MINUTES.items() if 0 <= minute - m <= 4]
    return max(due) if due else None

async def run(enqueue, list_boards, wait_job, stop_event: asyncio.Event) -> None:
    """Check every minute and recover any batch checkpoint currently due.

    The checker deliberately starts only the Railway-owned daily session.
    Existing ledger idempotency decides whether work is actually needed.
    """
    while not stop_event.is_set():
        try:
            now = datetime.now(timezone.utc)
            batch = due_batch(now)
            if batch is not None:
                day = ledger.today_str()
                status = ledger.get_day_status(day)
                batches = {int(x["batch"]): x for x in status.get("batches", [])}
                row = batches.get(batch)
                state = (row or {}).get("status")
                session_running = bool(
                    status.get("started")
                    or any(
                        str(x.get("status")) == "running"
                        for x in status.get("batches", [])
                    )
                )
                if state not in ("running", "completed") or not session_running:
                    logger.info(
                        "Amazon watchdog checkpoint due: batch=%s day=%s state=%s; "
                        "starting/recovering Railway daily session.",
                        batch, day, state,
                    )
                    from amazon_scheduler import amazon_scheduler
                    await amazon_scheduler.start_daily_session(
                        enqueue, list_boards, wait_job, trigger_batch=batch
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Amazon watchdog checkpoint failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=60)
        except asyncio.TimeoutError:
            pass
