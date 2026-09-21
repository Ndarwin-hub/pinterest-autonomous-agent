"""Email alerts for the Railway-owned Amazon automation."""
from __future__ import annotations
import logging
from datetime import datetime, timezone
logger=logging.getLogger("pinterest-agent.amazon_alerts")
RECIPIENT="ndarwin1414@gmail.com"

async def send_failure_alert(*,batch:int,reason:str,details:str="") -> bool:
    """Compatibility shim: batch-level email alerts are disabled."""
    logger.info("Suppressed batch-level failure alert for batch %s: %s",batch,reason)
    return True

async def _send_daily_status(day:str,kind:str) -> bool:
    from daily_ledger import ledger
    if not ledger.claim_daily_notification(day,kind):
        return True
    try:
        from agent import run_composio_tool
        state=ledger.daily_status_state(day)
        if kind=="started":
            subject="Pinterest Amazon automation — STARTED"
            body=(f"Pinterest Amazon automation — STARTED\n\n"
                  f"Railway successfully woke and accepted the daily automation session.\n\n"
                  f"Started (UTC): {state.get('started_at')}\n"
                  f"Trigger batch: {state.get('started_trigger_batch')}\n"
                  f"Scheduled trigger: {state.get('started_scheduled_local_time')}\n\n"
                  "This is the only daily automation status notification. No batch, completion, or retry notifications will be sent.")
        else:
            subject="Pinterest Amazon automation — NOT STARTED"
            body=(f"Pinterest Amazon automation — NOT STARTED\n\n"
                  "No successful Railway daily-session start was detected within today's scheduled trigger window.\n\n"
                  f"Checked (UTC): {datetime.now(timezone.utc).isoformat()}\n"
                  "Scheduled trigger window: 06:08–14:00 Asia/Kathmandu\n\n"
                  "This is the only daily automation status notification.")
        await run_composio_tool("GMAIL_SEND_EMAIL",{"recipient_email":RECIPIENT,"subject":subject,"body":body,"is_html":False})
        ledger.finish_daily_notification(day,kind,True)
        logger.info("Daily automation %s notification sent for %s",kind,day)
        return True
    except Exception as exc:
        ledger.finish_daily_notification(day,kind,False)
        logger.error("Daily automation %s notification could not be sent: %s",kind,exc)
        return False

async def notify_daily_started(day:str,trigger_batch:int,scheduled_local_time:str|None=None) -> bool:
    from daily_ledger import ledger
    ledger.mark_daily_started(day,trigger_batch,scheduled_local_time)
    return await _send_daily_status(day,"started")

async def notify_daily_not_started(day:str) -> bool:
    from daily_ledger import ledger
    if ledger.daily_status_state(day).get("started"):
        return await _send_daily_status(day,"started")
    return await _send_daily_status(day,"not_started")


async def notify_daily_failed(day:str) -> bool:
    from daily_ledger import ledger
    if ledger.is_day_complete(day):
        return True
    if not ledger.claim_daily_notification(day,"failed"):
        return True
    try:
        from agent import run_composio_tool
        state=ledger.daily_status_state(day)
        status=ledger.get_day_status(day)
        subject="Pinterest Amazon automation — FAILED"
        body=(f"Pinterest Amazon automation — FAILED\n\n"
              "The daily scheduler reached its final scheduled check without completing the required daily run.\n\n"
              f"Checked (UTC): {datetime.now(timezone.utc).isoformat()}\n"
              f"Successful slots: {status.get('success_count',0)}/50\n"
              f"Started (UTC): {state.get('started_at')}\n\n"
              "This is the only daily failure notification. Batch retry/failure emails are disabled.")
        await run_composio_tool("GMAIL_SEND_EMAIL",{"recipient_email":RECIPIENT,"subject":subject,"body":body,"is_html":False})
        ledger.finish_daily_notification(day,"failed",True)
        return True
    except Exception as exc:
        ledger.finish_daily_notification(day,"failed",False)
        logger.error("Daily automation failed notification could not be sent: %s",exc)
        return False
