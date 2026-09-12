"""Durable 15-slot daily Amazon success ledger."""
from __future__ import annotations
import os,sqlite3,threading
from datetime import datetime,timezone,date
from pathlib import Path
DATA=Path(os.getenv("DATA_DIR", "/data" if Path("/data").exists() else "/tmp")); DB_PATH=Path(os.getenv("DAILY_LEDGER_DB_PATH",str(DATA/"amazon_daily_ledger.db"))); _lock=threading.Lock(); SLOT_COUNT=15
class DailyLedger:
    def __init__(self,db_path=DB_PATH): self.db_path=Path(db_path); self._init()
    def _conn(self): self.db_path.parent.mkdir(parents=True,exist_ok=True); return sqlite3.connect(str(self.db_path),check_same_thread=False)
    def _init(self):
        with _lock:
            c=self._conn(); c.execute("CREATE TABLE IF NOT EXISTS daily_days(day TEXT PRIMARY KEY,status TEXT NOT NULL,success_count INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL)"); c.execute("CREATE TABLE IF NOT EXISTS daily_slots(day TEXT NOT NULL,slot INTEGER NOT NULL,target_board_name TEXT,target_board_id TEXT,slot_kind TEXT NOT NULL,status TEXT NOT NULL,selected_asin TEXT,selected_url TEXT,affiliate_url TEXT,replacement_attempts INTEGER NOT NULL DEFAULT 0,job_id TEXT,pinterest_verified INTEGER DEFAULT 0,error TEXT,completed_at TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(day,slot))"); c.commit(); c.close()
    @staticmethod
    def today_str(): return date.today().isoformat()
    def ensure_day(self,day=None,slots_spec=None):
        day=day or self.today_str(); now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn(); c.execute("INSERT OR IGNORE INTO daily_days(day,status,success_count,updated_at) VALUES(?,?,0,?)",(day,"in_progress",now))
            for s in slots_spec or []: c.execute("INSERT OR IGNORE INTO daily_slots(day,slot,target_board_name,target_board_id,slot_kind,status,updated_at) VALUES(?,?,?,?,?,?,?)",(day,int(s["slot"]),s.get("target_board_name"),s.get("target_board_id"),s.get("slot_kind","board"),"pending",now))
            c.commit(); c.close(); return day
    def get_day_status(self,day=None):
        day=day or self.today_str()
        with _lock:
            c=self._conn(); d=c.execute("SELECT day,status,success_count,updated_at FROM daily_days WHERE day=?",(day,)).fetchone(); rows=c.execute("SELECT slot,target_board_name,target_board_id,slot_kind,status,selected_asin,selected_url,affiliate_url,replacement_attempts,job_id,pinterest_verified,error,completed_at FROM daily_slots WHERE day=? ORDER BY slot",(day,)).fetchall(); c.close()
        if not d:return {"day":day,"status":"not_started","success_count":0,"slots":[]}
        keys=["slot","target_board_name","target_board_id","slot_kind","status","selected_asin","selected_url","affiliate_url","replacement_attempts","job_id","pinterest_verified","error","completed_at"]
        return {"day":d[0],"status":d[1],"success_count":d[2],"updated_at":d[3],"slots":[dict(zip(keys,r))|{"pinterest_verified":bool(r[10])} for r in rows]}
    def next_pending_slot(self,day=None):
        st=self.get_day_status(day)
        if st.get("status")=="complete":return None
        return next((s for s in st["slots"] if s["status"] in ("pending","failed_open")),None)
    def mark_slot(self,slot,*,status,day=None,selected_asin=None,selected_url=None,affiliate_url=None,job_id=None,pinterest_verified=False,error=None,inc_replacement=False):
        day=day or self.today_str(); now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn(); row=c.execute("SELECT replacement_attempts FROM daily_slots WHERE day=? AND slot=?",(day,slot)).fetchone(); attempts=(int(row[0]) if row else 0)+(1 if inc_replacement else 0)
            c.execute("UPDATE daily_slots SET status=?,selected_asin=COALESCE(?,selected_asin),selected_url=COALESCE(?,selected_url),affiliate_url=COALESCE(?,affiliate_url),replacement_attempts=?,job_id=COALESCE(?,job_id),pinterest_verified=?,error=?,completed_at=CASE WHEN ? IN ('success','exhausted') THEN ? ELSE completed_at END,updated_at=? WHERE day=? AND slot=?",(status,selected_asin,selected_url,affiliate_url,attempts,job_id,1 if pinterest_verified else 0,error,status,now,now,day,slot))
            if status=="success":
                c.execute("UPDATE daily_days SET success_count=success_count+1,updated_at=? WHERE day=?",(now,day)); c.execute("UPDATE daily_days SET status='complete' WHERE day=? AND success_count>=?",(day,SLOT_COUNT))
            c.commit(); c.close()
    def is_day_complete(self,day=None):
        s=self.get_day_status(day); return s.get("status")=="complete" or int(s.get("success_count",0))>=SLOT_COUNT
ledger=DailyLedger()
