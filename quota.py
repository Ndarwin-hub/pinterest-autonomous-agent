"""Quota governor for the existing Pinterest workflow."""
from __future__ import annotations
import os,json,sqlite3
from datetime import datetime,timezone
from pathlib import Path
from threading import Lock
from typing import Any,Dict
_DATA_DIR=Path(os.getenv("DATA_DIR","/data" if Path("/data").exists() else "/tmp")); DB_PATH=Path(os.getenv("QUOTA_DB_PATH",os.getenv("JOB_DB_PATH",str(_DATA_DIR/"pinterest_agent_jobs.db"))))
MONTHLY_LIMIT=int(os.getenv("COMPOSIO_MONTHLY_TOOL_BUDGET","100000")); SAFETY_RESERVE=int(os.getenv("COMPOSIO_SAFETY_RESERVE","10000")); JOB_BUDGET=int(os.getenv("COMPOSIO_JOB_BUDGET","22")); _lock=Lock()
def _month():return datetime.now(timezone.utc).strftime("%Y-%m")
class QuotaGovernor:
 def __init__(self,db_path=DB_PATH):self.db_path=Path(db_path);self._init()
 def _conn(self):self.db_path.parent.mkdir(parents=True,exist_ok=True);return sqlite3.connect(str(self.db_path),check_same_thread=False)
 def _init(self):
  with _lock:
   c=self._conn();c.execute("CREATE TABLE IF NOT EXISTS quota_ledger(month TEXT PRIMARY KEY,reserved_units INTEGER NOT NULL DEFAULT 0,completed_jobs INTEGER NOT NULL DEFAULT 0,failed_jobs INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL)");c.commit();c.close()
 def _row(self):
  m=_month();c=self._conn();r=c.execute("SELECT month,reserved_units,completed_jobs,failed_jobs,updated_at FROM quota_ledger WHERE month=?",(m,)).fetchone()
  if not r:
   now=datetime.now(timezone.utc).isoformat();c.execute("INSERT INTO quota_ledger VALUES(?,?,?,?,?)",(m,0,0,0,now));c.commit();r=(m,0,0,0,now)
  c.close();return r
 def snapshot(self)->Dict[str,Any]:
  r=self._row();safe=max(0,MONTHLY_LIMIT-SAFETY_RESERVE);remaining=max(0,safe-int(r[1]));return {"month":r[0],"monthly_limit":MONTHLY_LIMIT,"safety_reserve":SAFETY_RESERVE,"safe_capacity":safe,"reserved_units":int(r[1]),"remaining_safe_units":remaining,"job_budget_units":JOB_BUDGET,"estimated_complete_jobs_remaining":remaining//max(1,JOB_BUDGET),"completed_jobs":int(r[2]),"failed_jobs":int(r[3])}
 def can_start_job(self):return self.snapshot()["remaining_safe_units"]>=JOB_BUDGET
 def reserve_job(self):
  with _lock:
   r=self._row();safe=max(0,MONTHLY_LIMIT-SAFETY_RESERVE)
   if int(r[1])+JOB_BUDGET>safe:return False
   c=self._conn();c.execute("UPDATE quota_ledger SET reserved_units=?,updated_at=? WHERE month=?",(int(r[1])+JOB_BUDGET,datetime.now(timezone.utc).isoformat(),r[0]));c.commit();c.close();return True
 def record_job(self,success):
  with _lock:
   r=self._row();c=self._conn();c.execute("UPDATE quota_ledger SET completed_jobs=completed_jobs+?,failed_jobs=failed_jobs+?,updated_at=? WHERE month=?",(1 if success else 0,0 if success else 1,datetime.now(timezone.utc).isoformat(),r[0]));c.commit();c.close()
quota=QuotaGovernor()
