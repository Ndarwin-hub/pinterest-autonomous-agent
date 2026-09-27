"""Single daily final email report for Pinterest + video automation."""
from __future__ import annotations
import json
import logging
from datetime import datetime, timezone
logger=logging.getLogger("pinterest-agent.amazon_alerts")
RECIPIENT="ndarwin1414@gmail.com"

async def send_failure_alert(*,batch:int,reason:str,details:str="") -> bool:
    logger.info("Suppressed batch-level failure alert for batch %s: %s",batch,reason)
    return True

def _video_summary(status):
    jobs=status.get("video_jobs") or []
    created=sum(1 for j in jobs if str(j.get("status") or "").lower()=="completed")
    unsuccessful=sum(1 for j in jobs if str(j.get("status") or "").lower()=="failed")
    railway=kaggle=0
    for j in jobs:
        try: data=json.loads(j.get("publication_json") or "{}")
        except Exception: data={}
        source=str(data.get("source") or "").lower()
        if str(j.get("status") or "").lower()=="completed":
            if "railway" in source: railway+=1
            elif "kaggle" in source: kaggle+=1
    return {"total":len(jobs),"created":created,"unsuccessful":unsuccessful,"railway":railway,"kaggle":kaggle}

async def notify_daily_final_status(day:str) -> bool:
    from daily_ledger import ledger
    if not ledger.claim_daily_notification(day,"final"):
        return True
    try:
        from agent import run_composio_tool
        status=ledger.daily_report(day)
        slots=status.get("slots") or []
        successful=sum(1 for s in slots if s.get("status")=="success" and s.get("pinterest_verified"))
        unsuccessful=sum(1 for s in slots if s.get("status") in ("failed_open","exhausted","deferred","partial"))
        recovery=sum(1 for s in slots if s.get("status") in ("deferred","partial","exhausted"))
        completed=sum(1 for s in slots if s.get("status")=="success")
        batches=status.get("batches") or []
        batches_completed=sum(1 for b in batches if b.get("status")=="completed")
        batches_unsuccessful=sum(1 for b in batches if b.get("status") in ("failed","partial_failure"))
        video=_video_summary(status)
        usage=status.get("composio_usage") or {}
        subject="Pinterest + Video Automation — Daily Final Report"
        body=(f"Pinterest + Video Automation — DAILY FINAL REPORT\n"
              f"Date: {day} (Asia/Kathmandu)\n"
              f"Final report generated (UTC): {datetime.now(timezone.utc).isoformat()}\n\n"
              f"PINTEREST\nSuccessful: {successful}\nUnsuccessful: {unsuccessful}\n"
              f"Sent to recovery: {recovery}\nCompleted: {completed}\n"
              f"Normal batches completed: {batches_completed}/10\n"
              f"Unsuccessful/partial batches: {batches_unsuccessful}\n"
              f"Total product slots: {len(slots)}/50\n\n"
              f"VIDEO GENERATION\nVideos created successfully: {video['created']}\n"
              f"Created by Kaggle: {video['kaggle']}\nCreated by Railway fallback: {video['railway']}\n"
              f"Video jobs unsuccessful: {video['unsuccessful']}\nVideo jobs tracked: {video['total']}\n\n"
              f"USAGE\nComposio tool calls tracked: {usage.get('calls',0)}\n"
              f"Composio successful calls: {usage.get('successes',0)}\nComposio failed calls: {usage.get('failures',0)}\n"
              f"Pinterest product slots attempted: {len(slots)}\n\n"
              "No intermediate Pinterest, video, recovery, or batch emails are sent. "
              "This is the single daily consolidated report.")
        await run_composio_tool("GMAIL_SEND_EMAIL",{"recipient_email":RECIPIENT,"subject":subject,"body":body,"is_html":False})
        ledger.finish_daily_notification(day,"final",True)
        logger.info("Single consolidated daily final report sent for %s",day)
        return True
    except Exception as exc:
        ledger.finish_daily_notification(day,"final",False)
        logger.error("Daily final report could not be sent: %s",exc)
        return False

async def notify_daily_started(day:str,trigger_batch:int,scheduled_local_time:str|None=None) -> bool:
    from daily_ledger import ledger
    ledger.mark_daily_started(day,trigger_batch,scheduled_local_time)
    return True

async def notify_daily_not_started(day:str) -> bool:
    return True

async def notify_daily_failed(day:str) -> bool:
    return await notify_daily_final_status(day)
