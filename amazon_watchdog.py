"""Watchdog for externally scheduled Amazon batches."""
from __future__ import annotations
from datetime import datetime,timezone
from daily_ledger import ledger
from amazon_alerts import send_failure_alert
async def check_batch(batch:int)->dict:
 day=ledger.today_str(); status=ledger.get_day_status(day); rows={int(x["batch"]):x for x in status.get("batches",[])}; row=rows.get(batch)
 if not row:
  await send_failure_alert(batch=batch,reason="scheduled batch was not recorded",details=f"No batch run record exists for {day} at watchdog time {datetime.now(timezone.utc).isoformat()}.")
  return {"status":"missing","day":day,"batch":batch}
 state=row.get("status")
 if state not in ("running","completed"):
  await send_failure_alert(batch=batch,reason=f"scheduled batch status is {state}",details=str(row))
  return {"status":"alerted","day":day,"batch":batch,"batch_status":state}
 return {"status":"ok","day":day,"batch":batch,"batch_status":state}
