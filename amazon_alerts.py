"""Failure alerts for scheduled Amazon automation via connected Composio Gmail."""
from __future__ import annotations
import logging
from datetime import datetime, timezone
logger=logging.getLogger("pinterest-agent.amazon_alerts")
RECIPIENT="ndarwin1414@gmail.com"
async def send_failure_alert(*,batch:int,reason:str,details:str="") -> bool:
    try:
        from agent import run_composio_tool
        subject=f"Pinterest Amazon automation alert — batch {batch}"
        body=(f"Amazon Pinterest automation reported a failure or blocked run.\n\n"
              f"Batch: {batch}\nTime (UTC): {datetime.now(timezone.utc).isoformat()}\n"
              f"Reason: {reason}\n\nDetails:\n{details[:4000]}\n\n"
              "Source: browserless Composio Amazon discovery / Railway scheduler.")
        await run_composio_tool("GMAIL_SEND_EMAIL",{"recipient_email":RECIPIENT,"subject":subject,"body":body,"is_html":False})
        logger.info("Amazon failure alert sent for batch %s",batch)
        return True
    except Exception as exc:
        logger.error("Amazon failure alert could not be sent: %s",exc)
        return False
