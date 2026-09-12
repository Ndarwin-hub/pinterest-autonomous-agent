"""Job models and persistent store."""
import os,json,sqlite3
from enum import Enum
from datetime import datetime,timezone,timedelta
from typing import Optional,Dict,Any
from dataclasses import dataclass,field
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
_DATA_DIR=Path(os.getenv("DATA_DIR","/data" if Path("/data").exists() else "/tmp")); DB_PATH=Path(os.getenv("JOB_DB_PATH",str(_DATA_DIR/"pinterest_agent_jobs.db")))
class JobStatus(str,Enum): QUEUED="queued"; RUNNING="running"; COMPLETED="completed"; FAILED="failed"
@dataclass
class Job:
 job_id:str; url:str; status:JobStatus=JobStatus.QUEUED; progress:Optional[str]=None; result:Optional[Dict[str,Any]]=None; error:Optional[str]=None; created_at:str=field(default_factory=lambda:datetime.now(timezone.utc).isoformat()); updated_at:str=field(default_factory=lambda:datetime.now(timezone.utc).isoformat())
def normalize_url(url:str)->str:
 p=urlsplit((url or "").strip()); path=p.path or "/"; path=path.rstrip("/") or "/"; return urlunsplit((p.scheme.lower(),p.netloc.lower(),path,p.query,""))
class JobStore:
 def __init__(self,db_path:Path=DB_PATH): self.db_path=db_path; self._memory={}; self._init_db()
 def _connect(self): self.db_path.parent.mkdir(parents=True,exist_ok=True); return sqlite3.connect(str(self.db_path),check_same_thread=False)
 def _init_db(self):
  try:
   c=self._connect(); c.execute("CREATE TABLE IF NOT EXISTS jobs(job_id TEXT PRIMARY KEY,url TEXT NOT NULL,status TEXT NOT NULL,progress TEXT,result TEXT,error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"); c.execute("CREATE INDEX IF NOT EXISTS idx_jobs_url ON jobs(url)"); c.commit(); c.close()
  except Exception as e: print(f"JobStore SQLite init warning: {e}")
 def save(self,job:Job):
  self._memory[job.job_id]=job
  try:
   c=self._connect(); c.execute("INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?,?,?)",(job.job_id,job.url,job.status.value,job.progress,json.dumps(job.result) if job.result else None,job.error,job.created_at,job.updated_at)); c.commit(); c.close()
  except Exception as e: print(f"JobStore save warning: {e}")
 def get(self,job_id:str):
  if job_id in self._memory:return self._memory[job_id]
  try:
   c=self._connect(); r=c.execute("SELECT job_id,url,status,progress,result,error,created_at,updated_at FROM jobs WHERE job_id=?",(job_id,)).fetchone(); c.close()
   if not r:return None
   j=Job(r[0],r[1],JobStatus(r[2]),r[3],json.loads(r[4]) if r[4] else None,r[5],r[6],r[7]); self._memory[job_id]=j; return j
  except Exception as e: print(f"JobStore get warning: {e}"); return self._memory.get(job_id)
 def find_by_url(self,url:str,completed_within_hours:int=24):
  key=normalize_url(url)
  try:
   c=self._connect(); rows=c.execute("SELECT job_id,url,status,progress,result,error,created_at,updated_at FROM jobs ORDER BY updated_at DESC LIMIT 200").fetchall(); c.close(); cutoff=datetime.now(timezone.utc)-timedelta(hours=completed_within_hours)
   for r in rows:
    if normalize_url(r[1])!=key:continue
    st=JobStatus(r[2])
    if st in (JobStatus.QUEUED,JobStatus.RUNNING):return self.get(r[0])
    if st==JobStatus.COMPLETED:
     try:
      if datetime.fromisoformat(r[7])>=cutoff:return self.get(r[0])
     except Exception:pass
  except Exception as e: print(f"JobStore URL lookup warning: {e}")
  return None
 def update(self,job_id:str,**kwargs):
  j=self.get(job_id)
  if not j:return
  for k,v in kwargs.items():
   if hasattr(j,k):setattr(j,k,v)
  j.updated_at=datetime.now(timezone.utc).isoformat(); self.save(j)
