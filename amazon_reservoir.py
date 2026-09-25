"""Durable Amazon candidate reservoir."""
from __future__ import annotations
import json, os, sqlite3, time
from typing import Any, Dict, List, Optional, Set
DB_PATH=os.getenv("AMAZON_RESERVOIR_DB_PATH",os.path.join(os.getenv("DATA_DIR","/data"),"amazon_reservoir.db"))
def _connect():
 os.makedirs(os.path.dirname(DB_PATH) or ".",exist_ok=True); c=sqlite3.connect(DB_PATH); c.execute("CREATE TABLE IF NOT EXISTS candidates(asin TEXT PRIMARY KEY,payload TEXT NOT NULL,first_seen REAL NOT NULL,last_seen REAL NOT NULL,use_count INTEGER NOT NULL DEFAULT 0)"); c.commit(); return c
def put(candidate:Dict[str,Any])->None:
 asin=str(candidate.get("asin") or "").upper().strip()
 if len(asin)!=10:return
 now=time.time(); payload=json.dumps(candidate,separators=(",",":"),ensure_ascii=False)
 with _connect() as c:c.execute("INSERT INTO candidates(asin,payload,first_seen,last_seen,use_count) VALUES(?,?,?,?,0) ON CONFLICT(asin) DO UPDATE SET payload=excluded.payload,last_seen=excluded.last_seen",(asin,payload,now,now)); c.commit()
def put_many(candidates:List[Dict[str,Any]])->None:
 for x in candidates:
  if isinstance(x,dict):put(x)
def get(limit:int=25,exclude_asins:Optional[Set[str]]=None)->List[Dict[str,Any]]:
 excluded={str(x).upper() for x in (exclude_asins or set())}; out=[]
 with _connect() as c:
  for asin,payload in c.execute("SELECT asin,payload FROM candidates ORDER BY last_seen DESC LIMIT ?",(max(1,min(limit,200)),)).fetchall():
   if asin in excluded:continue
   try:
    x=json.loads(payload)
    if isinstance(x,dict):out.append(x)
   except Exception:pass
 return out
def mark_used(asin:str)->None:
 with _connect() as c:c.execute("UPDATE candidates SET use_count=use_count+1,last_seen=? WHERE asin=?",(time.time(),str(asin).upper())); c.commit()
