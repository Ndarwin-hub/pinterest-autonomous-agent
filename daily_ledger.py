"""Durable 50-slot daily Amazon success ledger with 10-batch idempotency."""
from __future__ import annotations
import os, sqlite3, threading, time, json
from datetime import datetime, timezone, date
from pathlib import Path
DATA=Path(os.getenv("DATA_DIR", "/data" if Path("/data").exists() else "/tmp")); DB_PATH=Path(os.getenv("DAILY_LEDGER_DB_PATH",str(DATA/"amazon_daily_ledger.db"))); _lock=threading.Lock(); SLOT_COUNT=50; BATCH_SIZE=5; BATCH_LEASE_SEC=int(os.getenv("AMAZON_BATCH_LEASE_SEC","2700")); SLOT_PROCESSING_LEASE_SEC=int(os.getenv("AMAZON_SLOT_PROCESSING_LEASE_SEC","2700"))
class DailyLedger:
    def __init__(self,db_path=DB_PATH): self.db_path=Path(db_path); self._init()
    def _conn(self):
        self.db_path.parent.mkdir(parents=True,exist_ok=True); c=sqlite3.connect(str(self.db_path),check_same_thread=False,timeout=30); c.execute("PRAGMA busy_timeout=30000"); return c
    def _init(self):
        with _lock:
            c=self._conn(); c.execute("CREATE TABLE IF NOT EXISTS daily_days(day TEXT PRIMARY KEY,status TEXT NOT NULL,success_count INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL)"); c.execute("CREATE TABLE IF NOT EXISTS daily_slots(day TEXT NOT NULL,slot INTEGER NOT NULL,target_board_name TEXT,target_board_id TEXT,slot_kind TEXT NOT NULL,status TEXT NOT NULL,selected_asin TEXT,selected_url TEXT,affiliate_url TEXT,replacement_attempts INTEGER NOT NULL DEFAULT 0,job_id TEXT,pinterest_verified INTEGER DEFAULT 0,error TEXT,completed_at TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(day,slot))"); c.execute("CREATE TABLE IF NOT EXISTS batch_runs(day TEXT NOT NULL,batch_index INTEGER NOT NULL,status TEXT NOT NULL,owner TEXT,started_at TEXT,completed_at TEXT,result_json TEXT,error TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(day,batch_index))")
            c.execute("CREATE TABLE IF NOT EXISTS scheduler_events(id INTEGER PRIMARY KEY AUTOINCREMENT,day TEXT NOT NULL,batch_requested INTEGER,scheduler_run_id TEXT,scheduled_local_time TEXT,github_delay_seconds INTEGER,github_queued_runs INTEGER,github_active_runs INTEGER,github_load_class TEXT,received_at TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS pin_a_requests(request_id TEXT PRIMARY KEY,day TEXT NOT NULL,source TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,claimed_at TEXT,completed_at TEXT,last_error TEXT,updated_at TEXT NOT NULL)")
            c.execute("CREATE TABLE IF NOT EXISTS daily_status_notifications(day TEXT PRIMARY KEY,started_at TEXT,started_trigger_batch INTEGER,started_scheduled_local_time TEXT,started_claimed_at TEXT,started_notified_at TEXT,not_started_claimed_at TEXT,not_started_notified_at TEXT,failed_claimed_at TEXT,failed_notified_at TEXT,updated_at TEXT NOT NULL)")
            for col in ("failed_claimed_at","failed_notified_at"):
                try:
                    c.execute(f"ALTER TABLE daily_status_notifications ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass
            c.commit(); c.close()
    @staticmethod
    def today_str(): return date.today().isoformat()
    def ensure_day(self,day=None,slots_spec=None):
        day=day or self.today_str(); now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn(); c.execute("INSERT OR IGNORE INTO daily_days(day,status,success_count,updated_at) VALUES(?,?,0,?)",(day,"in_progress",now))
            for spec in slots_spec or []:
                c.execute("INSERT OR IGNORE INTO daily_slots(day,slot,target_board_name,target_board_id,slot_kind,status,updated_at) VALUES(?,?,?,?,?,?,?)",(day,int(spec["slot"]),spec.get("target_board_name"),spec.get("target_board_id"),spec.get("slot_kind","board"),"pending",now))
                c.execute("UPDATE daily_slots SET target_board_name=?,target_board_id=?,slot_kind=?,updated_at=? WHERE day=? AND slot=? AND status IN ('pending','failed_open','exhausted','processing')",(spec.get("target_board_name"),spec.get("target_board_id"),spec.get("slot_kind","board"),now,day,int(spec["slot"])))
            c.commit(); c.close(); return day
    def reclaim_stale_processing(self,day=None):
        day=day or self.today_str(); cutoff=time.time()-SLOT_PROCESSING_LEASE_SEC
        with _lock:
            c=self._conn(); rows=c.execute("SELECT slot,updated_at,job_id FROM daily_slots WHERE day=? AND status='processing'",(day,)).fetchall(); reclaimed=0
            for slot,updated,job_id in rows:
                try: age=time.time()-datetime.fromisoformat(updated).timestamp()
                except Exception: age=SLOT_PROCESSING_LEASE_SEC+1
                # A processing slot with no external job ID cannot have reached the
                # publish phase. Reclaim it immediately after a scheduler restart;
                # this prevents a deployment/restart from marooning a slot for 45m.
                stale_without_job = not str(job_id or "").strip()
                if stale_without_job or age>=SLOT_PROCESSING_LEASE_SEC:
                    c.execute("UPDATE daily_slots SET status='failed_open',error='stale_processing_reclaimed',updated_at=? WHERE day=? AND slot=? AND status='processing'",(datetime.now(timezone.utc).isoformat(),day,slot)); reclaimed += 1 if c.execute("SELECT changes()").fetchone()[0] == 1 else 0
            c.commit(); c.close(); return reclaimed
    def get_day_status(self,day=None):
        day=day or self.today_str()
        with _lock:
            c=self._conn(); d=c.execute("SELECT day,status,success_count,updated_at FROM daily_days WHERE day=?",(day,)).fetchone(); rows=c.execute("SELECT slot,target_board_name,target_board_id,slot_kind,status,selected_asin,selected_url,affiliate_url,replacement_attempts,job_id,pinterest_verified,error,completed_at FROM daily_slots WHERE day=? ORDER BY slot",(day,)).fetchall(); batches=c.execute("SELECT batch_index,status,started_at,completed_at,error FROM batch_runs WHERE day=? ORDER BY batch_index",(day,)).fetchall(); c.close()
        if not d:return {"day":day,"status":"not_started","success_count":0,"slots":[],"batches":[]}
        keys=["slot","target_board_name","target_board_id","slot_kind","status","selected_asin","selected_url","affiliate_url","replacement_attempts","job_id","pinterest_verified","error","completed_at"]
        return {"day":d[0],"status":d[1],"success_count":d[2],"updated_at":d[3],"slots":[dict(zip(keys,r))|{"pinterest_verified":bool(r[10])} for r in rows],"batches":[{"batch":r[0],"status":r[1],"started_at":r[2],"completed_at":r[3],"error":r[4]} for r in batches]}
    def enqueue_pin_a_request(self,request_id,source,day=None):
        day=day or self.today_str(); now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn(); c.execute("INSERT OR IGNORE INTO pin_a_requests(request_id,day,source,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",(str(request_id),day,str(source or "unknown"),"pending",now,now))
            row=c.execute("SELECT request_id,day,source,status,created_at,claimed_at,completed_at,last_error FROM pin_a_requests WHERE request_id=?",(str(request_id),)).fetchone(); c.commit(); c.close()
        keys=["request_id","day","source","status","created_at","claimed_at","completed_at","last_error"]
        return dict(zip(keys,row))

    def recover_pin_a_requests(self):
        now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn(); c.execute("UPDATE pin_a_requests SET status='pending',claimed_at=NULL,updated_at=? WHERE status='running'",(now,)); n=c.execute("SELECT changes()").fetchone()[0]; c.commit(); c.close(); return int(n)

    def claim_next_pin_a_request(self):
        now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn(); c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT request_id,day,source,status,created_at,claimed_at,completed_at,last_error FROM pin_a_requests WHERE status='pending' ORDER BY created_at LIMIT 1").fetchone()
            if not row: c.commit(); c.close(); return None
            rid=row[0]; cur=c.execute("UPDATE pin_a_requests SET status='running',claimed_at=?,updated_at=? WHERE request_id=? AND status='pending'",(now,now,rid))
            if cur.rowcount!=1: c.commit(); c.close(); return None
            c.commit(); c.close()
        keys=["request_id","day","source","status","created_at","claimed_at","completed_at","last_error"]
        return dict(zip(keys,row)) | {"status":"running","claimed_at":now}

    def finish_pin_a_request(self,request_id,status="completed",error=None):
        now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn(); c.execute("UPDATE pin_a_requests SET status=?,completed_at=?,last_error=?,updated_at=? WHERE request_id=?",(status,now,error,now,str(request_id))); c.commit(); c.close()

    def record_scheduler_event(self,day=None,batch_requested=None,scheduler_run_id=None,scheduled_local_time=None,github_delay_seconds=0,github_queued_runs=0,github_active_runs=0,github_load_class="UNKNOWN"):
        day=day or self.today_str(); now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn()
            c.execute("INSERT INTO scheduler_events(day,batch_requested,scheduler_run_id,scheduled_local_time,github_delay_seconds,github_queued_runs,github_active_runs,github_load_class,received_at) VALUES(?,?,?,?,?,?,?,?,?)",(day,batch_requested,scheduler_run_id,scheduled_local_time,int(github_delay_seconds or 0),int(github_queued_runs or 0),int(github_active_runs or 0),github_load_class or "UNKNOWN",now))
            c.commit(); c.close()

    def mark_daily_started(self,day=None,trigger_batch=None,scheduled_local_time=None):
        day=day or self.today_str(); now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn(); c.execute("INSERT OR IGNORE INTO daily_status_notifications(day,updated_at) VALUES(?,?)",(day,now))
            c.execute("UPDATE daily_status_notifications SET started_at=COALESCE(started_at,?),started_trigger_batch=COALESCE(started_trigger_batch,?),started_scheduled_local_time=COALESCE(started_scheduled_local_time,?),updated_at=? WHERE day=?",(now,trigger_batch,scheduled_local_time,now,day)); c.commit(); c.close()
    def daily_status_state(self,day=None):
        day=day or self.today_str()
        with _lock:
            c=self._conn(); row=c.execute("SELECT day,started_at,started_trigger_batch,started_scheduled_local_time,started_claimed_at,started_notified_at,not_started_claimed_at,not_started_notified_at,failed_claimed_at,failed_notified_at,updated_at FROM daily_status_notifications WHERE day=?",(day,)).fetchone(); c.close()
        if not row:return {"day":day,"started":False,"started_at":None,"started_notified":False,"not_started_notified":False}
        return {"day":row[0],"started":bool(row[1]),"started_at":row[1],"started_trigger_batch":row[2],"started_scheduled_local_time":row[3],"started_notified":bool(row[5]),"not_started_notified":bool(row[7]),"failed_notified":bool(row[9]),"updated_at":row[10]}
    def claim_daily_notification(self,day=None,kind="started"):
        day=day or self.today_str(); now=datetime.now(timezone.utc).isoformat()
        if kind not in ("started","not_started","failed"): raise ValueError("kind must be started, not_started or failed")
        claim_col=f"{kind}_claimed_at"; notified_col=f"{kind}_notified_at"
        with _lock:
            c=self._conn(); c.execute("BEGIN IMMEDIATE"); c.execute("INSERT OR IGNORE INTO daily_status_notifications(day,updated_at) VALUES(?,?)",(day,now))
            row=c.execute(f"SELECT {notified_col},{claim_col} FROM daily_status_notifications WHERE day=?",(day,)).fetchone()
            if row and (row[0] or row[1]): c.commit(); c.close(); return False
            c.execute(f"UPDATE daily_status_notifications SET {claim_col}=?,updated_at=? WHERE day=? AND {notified_col} IS NULL AND {claim_col} IS NULL",(now,now,day)); ok = c.execute("SELECT changes()").fetchone()[0] == 1; c.commit(); c.close(); return ok
    def finish_daily_notification(self,day=None,kind="started",success=True):
        day=day or self.today_str(); now=datetime.now(timezone.utc).isoformat()
        if kind not in ("started","not_started","failed"): raise ValueError("kind must be started, not_started or failed")
        claim_col=f"{kind}_claimed_at"; notified_col=f"{kind}_notified_at"
        with _lock:
            c=self._conn()
            if success: c.execute(f"UPDATE daily_status_notifications SET {notified_col}=?,{claim_col}=NULL,updated_at=? WHERE day=?",(now,now,day))
            else: c.execute(f"UPDATE daily_status_notifications SET {claim_col}=NULL,updated_at=? WHERE day=?",(now,day))
            c.commit(); c.close()

    def latest_scheduler_events(self,day=None,limit=20):
        day=day or self.today_str()
        with _lock:
            c=self._conn(); rows=c.execute("SELECT batch_requested,scheduler_run_id,scheduled_local_time,github_delay_seconds,github_queued_runs,github_active_runs,github_load_class,received_at FROM scheduler_events WHERE day=? ORDER BY id DESC LIMIT ?",(day,int(limit))).fetchall(); c.close()
        keys=["batch_requested","scheduler_run_id","scheduled_local_time","github_delay_seconds","github_queued_runs","github_active_runs","github_load_class","received_at"]
        return [dict(zip(keys,r)) for r in rows]

    def next_unfinished_batch(self,day=None,max_batch=SLOT_COUNT//BATCH_SIZE):
        """Return the next normal-pass batch without reopening earlier batches.

        Normal daily progression is durable: once a batch has been attempted and
        reached a terminal batch_runs status (completed/partial_failure/failed),
        its unfinished slots are deferred to the single final recovery phase.
        A batch reclaimed from an orphaned executor is the one exception: it is
        returned so a restarted executor resumes that interrupted batch.

        This deliberately does not inspect pending/failed_open slot states in
        earlier terminal batches, because doing so would make Batch 1 block
        Batch 2 after a partial Batch 1 attempt.
        """
        day=day or self.today_str()
        with _lock:
            c=self._conn()
            rows=c.execute(
                "SELECT batch_index,status,error FROM batch_runs "
                "WHERE day=? AND batch_index BETWEEN 1 AND ? ORDER BY batch_index",
                (day,int(max_batch))
            ).fetchall()
            by_batch={int(r[0]):(str(r[1] or ""),str(r[2] or "")) for r in rows}

            # A scheduler restart can reclaim a genuinely running batch as
            # orphaned. Resume that exact batch rather than skipping forward.
            for batch in range(1,int(max_batch)+1):
                status,error=by_batch.get(batch,("", ""))
                if status=="failed" and error=="orphaned_executor_reclaimed":
                    c.close()
                    return batch

            # The first batch with no durable batch-run record is the next
            # untouched batch. Earlier terminal batches are recovery-only.
            for batch in range(1,int(max_batch)+1):
                if batch not in by_batch:
                    c.close()
                    return batch

            c.close()
            return None

    def try_begin_batch(self,day,batch_index,owner):
        now=time.time(); iso=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn(); c.execute("BEGIN IMMEDIATE"); row=c.execute("SELECT status,owner,started_at,completed_at,result_json,error FROM batch_runs WHERE day=? AND batch_index=?",(day,batch_index)).fetchone()
            if row:
                status,old_owner,started,completed,result,error=row
                if status=="completed": c.commit(); c.close(); return {"acquired":False,"status":"completed","result_json":result}
                if status=="running":
                    try: age=now-datetime.fromisoformat(started).timestamp() if started else 0
                    except Exception: age=BATCH_LEASE_SEC+1
                    if age<BATCH_LEASE_SEC: c.commit(); c.close(); return {"acquired":False,"status":"running","owner":old_owner}
            active=c.execute("SELECT batch_index,owner,started_at FROM batch_runs WHERE day=? AND status='running' AND batch_index<>? LIMIT 1",(day,batch_index)).fetchone()
            if active:
                try: age=now-datetime.fromisoformat(active[2]).timestamp() if active[2] else 0
                except Exception: age=BATCH_LEASE_SEC+1
                if age<BATCH_LEASE_SEC: c.commit(); c.close(); return {"acquired":False,"status":"busy","active_batch":active[0]}
            if row: c.execute("UPDATE batch_runs SET status='running',owner=?,started_at=?,completed_at=NULL,result_json=NULL,error=NULL,updated_at=? WHERE day=? AND batch_index=?",(owner,iso,iso,day,batch_index))
            else: c.execute("INSERT INTO batch_runs(day,batch_index,status,owner,started_at,updated_at) VALUES(?,?,?,?,?,?)",(day,batch_index,"running",owner,iso,iso))
            c.commit(); c.close(); return {"acquired":True,"status":"running"}
    def reclaim_orphaned_batches(self,day=None):
        """Reclaim any running batch when a new Railway executor is starting.
        A new process cannot own a batch from the previous process, so its lease
        is orphaned regardless of age."""
        day=day or self.today_str(); now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn()
            rows=c.execute("SELECT batch_index,owner FROM batch_runs WHERE day=? AND status='running'",(day,)).fetchall()
            reclaimed=[int(r[0]) for r in rows]
            if reclaimed:
                c.execute("UPDATE batch_runs SET status='failed',error='orphaned_executor_reclaimed',updated_at=? WHERE day=? AND status='running'",(now,day))
            c.commit(); c.close()
        return reclaimed

    def reclaim_stale_batches(self,day=None):
        """Release batch ownership left behind by a crashed/restarted Railway executor."""
        day=day or self.today_str(); cutoff=time.time()-BATCH_LEASE_SEC; now_iso=datetime.now(timezone.utc).isoformat(); reclaimed=[]
        with _lock:
            c=self._conn()
            rows=c.execute("SELECT batch_index,owner,started_at FROM batch_runs WHERE day=? AND status='running'",(day,)).fetchall()
            for batch,owner,started in rows:
                try: age=time.time()-datetime.fromisoformat(started).timestamp() if started else BATCH_LEASE_SEC+1
                except Exception: age=BATCH_LEASE_SEC+1
                if age>=BATCH_LEASE_SEC:
                    c.execute("UPDATE batch_runs SET status='failed',error='stale_batch_lease_reclaimed',updated_at=? WHERE day=? AND batch_index=? AND status='running'",(now_iso,day,int(batch)))
                    if c.execute("SELECT changes()").fetchone()[0]==1: reclaimed.append(int(batch))
            c.commit(); c.close()
        return reclaimed

    def close_day(self,day=None,reason="daily_session_finished"):
        """Close today's allocation after the normal pass and bounded final recovery.
        Closed means no new executor should restart the same day's session; historical
        unfinished/failed slots remain queryable and recoverable as history."""
        day=day or self.today_str(); now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn()
            c.execute("UPDATE daily_days SET status='closed',updated_at=? WHERE day=? AND status='in_progress'",(now,day))
            c.commit(); c.close()

    def complete_batch(self,day,batch_index,owner,status="completed",result_json=None,error=None):
        now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn(); c.execute("UPDATE batch_runs SET status=?,completed_at=?,result_json=?,error=?,updated_at=? WHERE day=? AND batch_index=? AND owner=? AND status='running'",(status,now,result_json,error,now,day,batch_index,owner)); c.commit(); c.close()
    def claim_slot(self,day,slot):
        now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn(); c.execute("BEGIN IMMEDIATE")
            cur=c.execute("UPDATE daily_slots SET status='processing',updated_at=? WHERE day=? AND slot=? AND status IN ('pending','failed_open','exhausted')",(now,day,slot))
            ok=cur.rowcount==1; c.commit(); c.close(); return ok
    def next_pending_slot(self,day=None,slot_min=1,slot_max=SLOT_COUNT):
        day=day or self.today_str()
        with _lock:
            c=self._conn(); row=c.execute("SELECT slot,target_board_name,target_board_id,slot_kind,status,selected_asin,selected_url,affiliate_url,replacement_attempts,job_id,pinterest_verified,error,completed_at FROM daily_slots WHERE day=? AND slot BETWEEN ? AND ? AND status IN ('pending','failed_open') ORDER BY slot LIMIT 1",(day,slot_min,slot_max)).fetchone(); c.close()
        if not row:return None
        keys=["slot","target_board_name","target_board_id","slot_kind","status","selected_asin","selected_url","affiliate_url","replacement_attempts","job_id","pinterest_verified","error","completed_at"]; d=dict(zip(keys,row)); return d
    def claim_recovery_slot(self,day,slot):
        now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn(); c.execute("BEGIN IMMEDIATE")
            cur=c.execute("UPDATE daily_slots SET status='processing',updated_at=? WHERE day=? AND slot=? AND status IN ('deferred','partial','exhausted')",(now,day,slot))
            ok=cur.rowcount==1; c.commit(); c.close(); return ok
    def next_recovery_slot(self,day=None):
        day=day or self.today_str()
        with _lock:
            c=self._conn(); row=c.execute("SELECT slot,target_board_name,target_board_id,slot_kind,status,selected_asin,selected_url,affiliate_url,replacement_attempts,job_id,pinterest_verified,error,completed_at FROM daily_slots WHERE day=? AND (status IN ('deferred','partial') OR (status='exhausted' AND COALESCE(error,'') NOT LIKE 'final_recovery_exhausted%')) ORDER BY slot LIMIT 1",(day,)).fetchone(); c.close()
        if not row:return None
        keys=["slot","target_board_name","target_board_id","slot_kind","status","selected_asin","selected_url","affiliate_url","replacement_attempts","job_id","pinterest_verified","error","completed_at"]
        return dict(zip(keys,row))
    def mark_slot(self,slot,*,status,day=None,selected_asin=None,selected_url=None,affiliate_url=None,job_id=None,pinterest_verified=False,error=None,inc_replacement=False):
        day=day or self.today_str(); now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn(); row=c.execute("SELECT replacement_attempts,status FROM daily_slots WHERE day=? AND slot=?",(day,slot)).fetchone(); attempts=(int(row[0]) if row else 0)+(1 if inc_replacement else 0); old_status=row[1] if row else None
            c.execute("UPDATE daily_slots SET status=?,selected_asin=COALESCE(?,selected_asin),selected_url=COALESCE(?,selected_url),affiliate_url=COALESCE(?,affiliate_url),replacement_attempts=?,job_id=COALESCE(?,job_id),pinterest_verified=?,error=?,completed_at=CASE WHEN ? IN ('success','exhausted') THEN ? ELSE completed_at END,updated_at=? WHERE day=? AND slot=?",(status,selected_asin,selected_url,affiliate_url,attempts,job_id,1 if pinterest_verified else 0,error,status,now,now,day,slot))
            if status=="success" and old_status!="success": c.execute("UPDATE daily_days SET success_count=success_count+1,updated_at=? WHERE day=?",(now,day))
            c.execute("UPDATE daily_days SET status='complete',updated_at=? WHERE day=? AND success_count>=?",(now,day,SLOT_COUNT)); c.commit(); c.close()


    def selected_video_source_batch(self,day=None,batch=1):
        day=day or self.today_str(); first=(int(batch)-1)*BATCH_SIZE+1; last=first+BATCH_SIZE-1
        with _lock:
            c=self._conn(); rows=c.execute("""SELECT slot,target_board_name,selected_asin,selected_url,affiliate_url FROM daily_slots WHERE day=? AND slot BETWEEN ? AND ? AND selected_asin IS NOT NULL ORDER BY slot""",(day,first,last)).fetchall(); c.close()
        return [{"slot":int(r[0]),"target_board_name":r[1],"asin":str(r[2]).upper(),"product_url":r[3],"affiliate_url":r[4]} for r in rows]

    def enqueue_video_job(self,day,batch,slot,asin,product_url,affiliate_url=None,source="pinterest",title=None):
        day=day or self.today_str(); now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn()
            c.execute("""CREATE TABLE IF NOT EXISTS video_jobs(day TEXT NOT NULL,batch_index INTEGER NOT NULL,slot INTEGER NOT NULL,asin TEXT NOT NULL,product_url TEXT NOT NULL,affiliate_url TEXT,title TEXT,source TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',attempts INTEGER NOT NULL DEFAULT 0,claim_owner TEXT,claimed_at TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT,publication_json TEXT,PRIMARY KEY(day,batch_index,slot))""")
            c.execute("""INSERT OR IGNORE INTO video_jobs(day,batch_index,slot,asin,product_url,affiliate_url,title,source,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",(day,int(batch),int(slot),str(asin).upper(),str(product_url),affiliate_url,title,source,"queued",now,now))
            if source=="pinterest":
                c.execute("""UPDATE video_jobs SET asin=?,product_url=?,affiliate_url=COALESCE(?,affiliate_url),title=COALESCE(?,title),source='pinterest',updated_at=? WHERE day=? AND batch_index=? AND slot=? AND status IN ('queued','failed')""",(str(asin).upper(),str(product_url),affiliate_url,title,now,day,int(batch),int(slot)))
            row=c.execute("""SELECT day,batch_index,slot,asin,product_url,affiliate_url,title,source,status,attempts,claim_owner,claimed_at,last_error,created_at,updated_at,completed_at,publication_json FROM video_jobs WHERE day=? AND batch_index=? AND slot=?""",(day,int(batch),int(slot))).fetchone(); c.commit(); c.close()
        keys=["day","batch","slot","asin","product_url","affiliate_url","title","source","status","attempts","claim_owner","claimed_at","last_error","created_at","updated_at","completed_at","publication_json"]; return dict(zip(keys,row))

    def video_batch(self,day=None,batch=1):
        day=day or self.today_str()
        with _lock:
            c=self._conn(); c.execute("""CREATE TABLE IF NOT EXISTS video_jobs(day TEXT NOT NULL,batch_index INTEGER NOT NULL,slot INTEGER NOT NULL,asin TEXT NOT NULL,product_url TEXT NOT NULL,affiliate_url TEXT,title TEXT,source TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',attempts INTEGER NOT NULL DEFAULT 0,claim_owner TEXT,claimed_at TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT,publication_json TEXT,PRIMARY KEY(day,batch_index,slot))"""); rows=c.execute("""SELECT day,batch_index,slot,asin,product_url,affiliate_url,title,source,status,attempts,claim_owner,claimed_at,last_error,created_at,updated_at,completed_at,publication_json FROM video_jobs WHERE day=? AND batch_index=? ORDER BY slot""",(day,int(batch))).fetchall(); c.close()
        keys=["day","batch","slot","asin","product_url","affiliate_url","title","source","status","attempts","claim_owner","claimed_at","last_error","created_at","updated_at","completed_at","publication_json"]; return [dict(zip(keys,r)) for r in rows]

    def claim_video_job(self,day,batch,slot,owner):
        now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn(); c.execute("BEGIN IMMEDIATE"); row=c.execute("SELECT status,attempts,claim_owner,claimed_at FROM video_jobs WHERE day=? AND batch_index=? AND slot=?",(day,int(batch),int(slot))).fetchone()
            if not row: c.commit(); c.close(); return None
            status,attempts,old_owner,claimed=row
            if status=="completed": c.commit(); c.close(); return {"claimed":False,"status":"completed"}
            if status=="processing" and claimed:
                try: age=time.time()-datetime.fromisoformat(claimed).timestamp()
                except Exception: age=999999
                if age < int(os.getenv("VIDEO_JOB_LEASE_SEC","7200")): c.commit(); c.close(); return {"claimed":False,"status":"processing","owner":old_owner}
            cur=c.execute("""UPDATE video_jobs SET status='processing',attempts=attempts+1,claim_owner=?,claimed_at=?,updated_at=? WHERE day=? AND batch_index=? AND slot=? AND status IN ('queued','failed','processing')""",(owner,now,now,day,int(batch),int(slot))); ok=cur.rowcount==1; c.commit(); c.close(); return {"claimed":ok,"status":"processing" if ok else "busy","owner":owner if ok else old_owner}

    def mark_video_job(self,day,batch,slot,status,owner=None,error=None,publication_json=None):
        now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn(); c.execute("""UPDATE video_jobs SET status=?,last_error=?,publication_json=COALESCE(?,publication_json),completed_at=CASE WHEN ? IN ('completed','failed') THEN ? ELSE completed_at END,updated_at=? WHERE day=? AND batch_index=? AND slot=? AND (claim_owner=? OR ? IS NULL)""",(status,error,publication_json,status,now,now,day,int(batch),int(slot),owner,owner)); c.commit(); c.close()

    def video_selected_asins(self,day=None):
        day=day or self.today_str()
        with _lock:
            c=self._conn(); c.execute("""CREATE TABLE IF NOT EXISTS video_jobs(day TEXT NOT NULL,batch_index INTEGER NOT NULL,slot INTEGER NOT NULL,asin TEXT NOT NULL,product_url TEXT NOT NULL,affiliate_url TEXT,title TEXT,source TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'queued',attempts INTEGER NOT NULL DEFAULT 0,claim_owner TEXT,claimed_at TEXT,last_error TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT,publication_json TEXT,PRIMARY KEY(day,batch_index,slot))"""); rows=c.execute("SELECT DISTINCT asin FROM video_jobs WHERE day=?",(day,)).fetchall(); c.close()
        return {str(r[0]).upper() for r in rows if r and r[0]}


    def next_video_music(self,track_ids):
        track_ids=[str(x) for x in track_ids if str(x)]
        if not track_ids: raise ValueError("track_ids required")
        with _lock:
            c=self._conn(); c.execute("""CREATE TABLE IF NOT EXISTS video_music_state(id INTEGER PRIMARY KEY CHECK(id=1),used_json TEXT NOT NULL,cycle INTEGER NOT NULL DEFAULT 0,last_track TEXT)""")
            row=c.execute("SELECT used_json,cycle,last_track FROM video_music_state WHERE id=1").fetchone()
            used=[]; cycle=0; last=None
            if row:
                try: used=json.loads(row[0] or "[]") if row[0] else []
                except Exception: used=[]
                cycle=int(row[1] or 0); last=row[2]
            used=[x for x in used if x in track_ids]
            unused=[x for x in track_ids if x not in used]
            if not unused:
                cycle+=1; used=[]; unused=list(track_ids)
            if len(unused)>1 and last in unused: unused.remove(last)
            chosen=unused[0]; used.append(chosen)
            c.execute("INSERT INTO video_music_state(id,used_json,cycle,last_track) VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET used_json=excluded.used_json,cycle=excluded.cycle,last_track=excluded.last_track",(json.dumps(used,separators=(",",":")),cycle,chosen)); c.commit(); c.close()
        return {"track_id":chosen,"cycle":cycle,"used_count":len(used),"pool_size":len(track_ids)}

    def historical_selected_asins(self,exclude_day=None):
        """ASINs selected by prior daily allocations; prevents a new day from resuming old work."""
        exclude_day=exclude_day or self.today_str()
        with _lock:
            c=self._conn()
            rows=c.execute("SELECT DISTINCT selected_asin FROM daily_slots WHERE day<>? AND selected_asin IS NOT NULL AND selected_asin<>''",(exclude_day,)).fetchall()
            c.close()
        return {str(r[0]).upper() for r in rows if r and r[0]}

    def is_day_complete(self,day=None):
        s=self.get_day_status(day); return s.get("status") in ("complete","closed") or int(s.get("success_count",0))>=SLOT_COUNT
ledger=DailyLedger()
