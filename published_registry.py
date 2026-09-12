"""Durable success registry for manual and Amazon publications."""
from __future__ import annotations
import os,re,sqlite3,threading
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlsplit,urlunsplit
DATA=Path(os.getenv("DATA_DIR", "/data" if Path("/data").exists() else "/tmp")); DB_PATH=Path(os.getenv("PUBLISHED_DB_PATH",str(DATA/"published_products.db"))); _lock=threading.Lock()
ASIN_RE=re.compile(r"(?:/dp/|/gp/product/|/product/)([A-Z0-9]{10})",re.I); ASIN_QUERY_RE=re.compile(r"[?&]asin=([A-Z0-9]{10})",re.I)
def extract_asin(url:str):
    if not url:return None
    m=ASIN_RE.search(url) or ASIN_QUERY_RE.search(url); return m.group(1).upper() if m else None
def normalize_url_key(url:str)->str:
    p=urlsplit((url or "").strip()); path=p.path or "/"; path=path.rstrip("/") or "/"; return urlunsplit((p.scheme.lower(),p.netloc.lower(),path,p.query,""))
class PublishedRegistry:
    def __init__(self,db_path:Path=DB_PATH): self.db_path=db_path; self._init()
    def _conn(self): self.db_path.parent.mkdir(parents=True,exist_ok=True); return sqlite3.connect(str(self.db_path),check_same_thread=False)
    def _init(self):
        with _lock:
            c=self._conn(); c.execute("CREATE TABLE IF NOT EXISTS published_products(id INTEGER PRIMARY KEY,asin TEXT,product_url TEXT,affiliate_url TEXT NOT NULL,url_key TEXT NOT NULL,board_id TEXT,board_name TEXT,job_id TEXT,source TEXT NOT NULL,status TEXT NOT NULL,pinterest_verified INTEGER DEFAULT 0,pin_ids TEXT,completed_at TEXT NOT NULL,created_at TEXT NOT NULL,notes TEXT)"); c.execute("CREATE INDEX IF NOT EXISTS idx_pub_asin ON published_products(asin)"); c.execute("CREATE INDEX IF NOT EXISTS idx_pub_url ON published_products(url_key)"); c.commit(); c.close()
    def is_published(self,*,asin=None,url=None):
        with _lock:
            c=self._conn()
            try:
                if asin and c.execute("SELECT 1 FROM published_products WHERE asin=? AND status='success' LIMIT 1",(asin.upper(),)).fetchone(): return True
                if url:
                    key=normalize_url_key(url)
                    if c.execute("SELECT 1 FROM published_products WHERE url_key=? AND status='success' LIMIT 1",(key,)).fetchone(): return True
                    a=extract_asin(url)
                    if a and c.execute("SELECT 1 FROM published_products WHERE asin=? AND status='success' LIMIT 1",(a,)).fetchone(): return True
                return False
            finally:c.close()
    def record_success(self,*,affiliate_url,source,asin=None,product_url=None,board_id=None,board_name=None,job_id=None,pinterest_verified=False,pin_ids=None,notes=None):
        asin=(asin or extract_asin(affiliate_url) or "").upper() or None; key=normalize_url_key(affiliate_url); now=datetime.now(timezone.utc).isoformat()
        with _lock:
            c=self._conn()
            if c.execute("SELECT 1 FROM published_products WHERE (asin IS NOT NULL AND asin=? OR url_key=?) AND status='success' LIMIT 1",(asin,key)).fetchone(): c.close(); return False
            c.execute("INSERT INTO published_products(asin,product_url,affiliate_url,url_key,board_id,board_name,job_id,source,status,pinterest_verified,pin_ids,completed_at,created_at,notes) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(asin,product_url,affiliate_url,key,board_id,board_name,job_id,source,"success",1 if pinterest_verified else 0,",".join(pin_ids or []),now,now,notes)); c.commit(); c.close(); return True
    def all_published_asins(self):
        with _lock:
            c=self._conn(); rows=c.execute("SELECT asin FROM published_products WHERE status='success' AND asin IS NOT NULL").fetchall(); c.close(); return {r[0] for r in rows}
    def count_success(self):
        with _lock:
            c=self._conn(); n=c.execute("SELECT COUNT(*) FROM published_products WHERE status='success'").fetchone()[0]; c.close(); return int(n)
registry=PublishedRegistry()
