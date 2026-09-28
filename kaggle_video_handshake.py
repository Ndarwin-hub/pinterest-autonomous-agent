from __future__ import annotations
import json, os, sqlite3, time, uuid
from datetime import datetime, timezone
from pathlib import Path

DB=Path(os.getenv("JOB_DB_PATH", "/data/pinterest_agent_jobs.db")); STALE_SEC=int(os.getenv("KAGGLE_VIDEO_HEARTBEAT_STALE_SEC", "900"))
def _conn(): DB.parent.mkdir(parents=True,exist_ok=True); return sqlite3.connect(str(DB),check_same_thread=False)
def init():
    c=_conn(); c.execute("CREATE TABLE IF NOT EXISTS kaggle_video_runs(run_id TEXT PRIMARY KEY,day TEXT NOT NULL,pair_start INTEGER NOT NULL,status TEXT NOT NULL,started_at TEXT NOT NULL,heartbeat_at TEXT NOT NULL,completed_at TEXT,last_error TEXT,metadata_json TEXT)"); c.execute("CREATE TABLE IF NOT EXISTS kaggle_video_jobs(run_id TEXT NOT NULL,batch_index INTEGER NOT NULL,slot INTEGER NOT NULL,asin TEXT NOT NULL,status TEXT NOT NULL,render_status TEXT,quality_status TEXT,publishing_status TEXT,platform_results_json TEXT,last_error TEXT,updated_at TEXT NOT NULL,PRIMARY KEY(run_id,batch_index,slot))"); c.commit(); c.close()
def _row(c,run_id):
    r=c.execute("SELECT run_id,day,pair_start,status,started_at,heartbeat_at,completed_at,last_error,metadata_json FROM kaggle_video_runs WHERE run_id=?",(run_id,)).fetchone(); return dict(zip(["run_id","day","pair_start","status","started_at","heartbeat_at","completed_at","last_error","metadata_json"],r)) if r else None
def upsert_run(run_id,day,pair_start,status="RUNNING",error=None,metadata=None):
    init(); now=datetime.now(timezone.utc).isoformat(); c=_conn(); c.execute("INSERT INTO kaggle_video_runs(run_id,day,pair_start,status,started_at,heartbeat_at,last_error,metadata_json) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET status=excluded.status,heartbeat_at=excluded.heartbeat_at,last_error=excluded.last_error,metadata_json=excluded.metadata_json",(run_id,day,int(pair_start),status,now,now,error,json.dumps(metadata or {},separators=(",",":")))); c.commit(); r=_row(c,run_id); c.close(); return r
def heartbeat(run_id,status="RUNNING",error=None):
    init(); now=datetime.now(timezone.utc).isoformat(); c=_conn(); c.execute("UPDATE kaggle_video_runs SET status=?,heartbeat_at=?,last_error=? WHERE run_id=?",(status,now,error,run_id)); c.commit(); r=_row(c,run_id); c.close(); return r
def report_job(run_id,batch,slot,asin,status,render_status=None,quality_status=None,publishing_status=None,platform_results=None,error=None):
    init(); now=datetime.now(timezone.utc).isoformat(); c=_conn(); c.execute("INSERT INTO kaggle_video_jobs(run_id,batch_index,slot,asin,status,render_status,quality_status,publishing_status,platform_results_json,last_error,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,batch_index,slot) DO UPDATE SET status=excluded.status,render_status=excluded.render_status,quality_status=excluded.quality_status,publishing_status=excluded.publishing_status,platform_results_json=excluded.platform_results_json,last_error=excluded.last_error,updated_at=excluded.updated_at",(run_id,int(batch),int(slot),str(asin).upper(),status,render_status,quality_status,publishing_status,json.dumps(platform_results or {},separators=(",",":")),error,now)); c.execute("UPDATE kaggle_video_runs SET heartbeat_at=? WHERE run_id=?",(now,run_id)); c.commit(); c.close()
def complete_run(run_id,status="COMPLETED",error=None):
    init(); now=datetime.now(timezone.utc).isoformat(); c=_conn(); c.execute("UPDATE kaggle_video_runs SET status=?,heartbeat_at=?,completed_at=?,last_error=? WHERE run_id=?",(status,now,now,error,run_id)); c.commit(); r=_row(c,run_id); c.close(); return r
def get_run(run_id): init(); c=_conn(); r=_row(c,run_id); c.close(); return r

def _derived_pair_run(day,pair_start):
    c=_conn(); first=(int(pair_start)-1)*5+1; last=first+9; rows=c.execute("SELECT batch_index,slot,asin,status,claim_owner,claimed_at,updated_at,publication_json,last_error FROM video_jobs WHERE day=? AND slot BETWEEN ? AND ? AND claim_owner LIKE 'kaggle-%' ORDER BY updated_at DESC",(day,first,last)).fetchall(); c.close()
    if not rows:return None
    owners=[str(r[4]).split("kaggle-",1)[1] for r in rows if str(r[4]).startswith("kaggle-")]; run_id=owners[0] if owners else None
    relevant=[r for r in rows if str(r[4])==f"kaggle-{run_id}"]
    if not relevant:return None
    latest=max((r[6] for r in relevant),default=datetime.now(timezone.utc).isoformat()); terminal=len(relevant)>=10 and all(str(r[3]).lower() in ("completed","failed") for r in relevant); success=len(relevant)>=10 and all(str(r[3]).lower()=="completed" for r in relevant)
    return {"run_id":run_id,"day":day,"pair_start":int(pair_start),"status":"COMPLETED" if success else ("INCOMPLETE" if terminal else "RUNNING"),"started_at":min((r[5] or latest) for r in relevant),"heartbeat_at":latest,"completed_at":latest if terminal else None,"last_error":next((r[8] for r in relevant if r[8]),None),"metadata_json":"{}"}

def _auto_start_kaggle(day,pair_start):
    if not os.getenv("KAGGLE_VIDEO_KERNEL_ID") or not (os.getenv("KAGGLE_API_TOKEN") or os.getenv("KAGGLE_USERNAME")):
        return None
    run_id=f"kaggle-{day}-pair-{int(pair_start)}-{uuid.uuid4().hex[:10]}"
    upsert_run(run_id,day,pair_start,status="RUNNING",metadata={"source":"railway_kaggle_trigger"})
    try:
        from kaggle_video_launcher import launch_existing_kernel
        launch_existing_kernel(run_id,day,pair_start)
        return get_run(run_id)
    except Exception as exc:
        complete_run(run_id,"FAILED",str(exc)[:1000])
        return get_run(run_id)

def get_pair_run(day,pair_start):
    init(); c=_conn(); r=c.execute("SELECT run_id,day,pair_start,status,started_at,heartbeat_at,completed_at,last_error,metadata_json FROM kaggle_video_runs WHERE day=? AND pair_start=? ORDER BY started_at DESC LIMIT 1",(day,int(pair_start))).fetchone(); c.close()
    if r:return dict(zip(["run_id","day","pair_start","status","started_at","heartbeat_at","completed_at","last_error","metadata_json"],r))
    derived=_derived_pair_run(day,pair_start)
    if derived:return derived
    # A missing run is no longer treated as an immediate Railway failure.
    # If Kaggle execution is configured, start the primary first.
    return _auto_start_kaggle(day,pair_start)

def run_is_stale(run):
    if not run:return True
    try:return time.time()-datetime.fromisoformat(run["heartbeat_at"]).timestamp()>STALE_SEC
    except Exception:return True
def pair_product_statuses(day,pair_start,run_id=None):
    run=get_run(run_id) if run_id else get_pair_run(day,pair_start); out={"complete":False,"run_id":run.get("run_id") if run else None,"products":{}}
    if not run:return out
    init(); c=_conn(); rows=c.execute("SELECT batch_index,slot,asin,status,render_status,quality_status,publishing_status,platform_results_json,last_error FROM kaggle_video_jobs WHERE run_id=? ORDER BY batch_index,slot",(run["run_id"],)).fetchall()
    if not rows:
        first=(int(pair_start)-1)*5+1; last=first+9; rows=c.execute("SELECT batch_index,slot,asin,status,NULL,NULL,NULL,publication_json,last_error FROM video_jobs WHERE day=? AND slot BETWEEN ? AND ? AND claim_owner=? ORDER BY batch_index,slot",(day,first,last,f"kaggle-{run['run_id']}")).fetchall()
    c.close()
    for r in rows:
        try:platforms=json.loads(r[7] or "{}")
        except Exception:platforms={}
        out["products"][str(r[2]).upper()]={"batch":r[0],"slot":r[1],"status":r[3],"render_status":r[4],"quality_status":r[5],"publishing_status":r[6],"platforms":platforms,"error":r[8]}
    out["complete"]=run["status"]=="COMPLETED" and len(rows)>=10 and all(str(r[3]).upper() in ("COMPLETED","SUCCESS") for r in rows); return out
