"""Durable, conservative publication idempotency guard.

This guard protects the external Pinterest create boundary. It fails closed:
once a logical Pin is claimed for external publication, a retry will not call
Pinterest again for that same URL/pin index. This avoids duplicate Pins when an
external create succeeds but the process dies before local success persistence.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

logger = logging.getLogger("pinterest-agent.publication-guard")
_LOCK = threading.Lock()
DATA = Path(os.getenv("DATA_DIR", "/data" if Path("/data").exists() else "/tmp"))
DB_PATH = Path(os.getenv("PUBLICATION_GUARD_DB", str(DATA / "pinterest_publication_guard.db")))


def normalize_url(url: str) -> str:
    p = urlsplit((url or "").strip())
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/") or "/", p.query, ""))


class PublicationGuard:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init()

    def _conn(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=30, check_same_thread=False)
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    def _init(self):
        with _LOCK:
            conn = self._conn()
            conn.execute(
                """CREATE TABLE IF NOT EXISTS pin_publications (
                    url_key TEXT NOT NULL,
                    pin_index INTEGER NOT NULL,
                    strategy_key TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result TEXT,
                    claimed_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(url_key, pin_index)
                )"""
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_pin_pub_job ON pin_publications(job_id)")
            conn.commit()
            conn.close()

    def claim(self, url: str, pin_index: int, strategy_key: str, job_id: str):
        key = normalize_url(url)
        now = datetime.now(timezone.utc).isoformat()
        with _LOCK:
            conn = self._conn()
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state,result,job_id FROM pin_publications WHERE url_key=? AND pin_index=?",
                (key, pin_index),
            ).fetchone()
            if row:
                conn.commit()
                conn.close()
                return row[0], (json.loads(row[1]) if row[1] else None), row[2]
            count = conn.execute(
                "SELECT COUNT(*) FROM pin_publications WHERE url_key=? AND state IN ('claimed','success')",
                (key,),
            ).fetchone()[0]
            if count >= 5:
                conn.commit()
                conn.close()
                return "limit", None, None
            conn.execute(
                "INSERT INTO pin_publications(url_key,pin_index,strategy_key,job_id,state,result,claimed_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (key, pin_index, strategy_key, job_id, "claimed", None, now, now),
            )
            conn.commit()
            conn.close()
            return "new", None, job_id

    def succeed(self, url: str, pin_index: int, result: dict):
        key = normalize_url(url)
        now = datetime.now(timezone.utc).isoformat()
        with _LOCK:
            conn = self._conn()
            conn.execute(
                "UPDATE pin_publications SET state='success',result=?,updated_at=? WHERE url_key=? AND pin_index=? AND state='claimed'",
                (json.dumps(result), now, key, pin_index),
            )
            conn.commit()
            conn.close()

    def count_success(self, url: str) -> int:
        with _LOCK:
            conn = self._conn()
            n = conn.execute(
                "SELECT COUNT(*) FROM pin_publications WHERE url_key=? AND state='success'",
                (normalize_url(url),),
            ).fetchone()[0]
            conn.close()
            return int(n)


guard = PublicationGuard()


def install(agent_module):
    """Wrap agent.publish_and_verify without changing /submit or Amazon APIs."""
    original = agent_module.publish_and_verify

    async def guarded_publish_and_verify(
        *, board_id, title, description, alt_text, image_mode, image_value,
        link, job_store, job_id, pin_index, **kwargs
    ):
        strategy_key = str(kwargs.get("strategy_key") or title[:100])
        state, saved, owner = guard.claim(link, pin_index, strategy_key, job_id)

        if state == "success" and saved:
            logger.info(
                "Reusing successful Pin for url=%s pin=%s pin_id=%s",
                normalize_url(link), pin_index, saved.get("pin_id"),
            )
            return saved

        if state == "limit":
            raise RuntimeError(
                f"Publication blocked: five Pins already claimed/published for {normalize_url(link)}"
            )

        if state == "claimed":
            raise RuntimeError(
                f"Publication already claimed for url={normalize_url(link)}; pin={pin_index}; refusing duplicate external create"
            )

        result = await original(
            board_id=board_id,
            title=title,
            description=description,
            alt_text=alt_text,
            image_mode=image_mode,
            image_value=image_value,
            link=link,
            job_store=job_store,
            job_id=job_id,
            pin_index=pin_index,
        )
        guard.succeed(link, pin_index, result)
        return result

    agent_module.publish_and_verify = guarded_publish_and_verify
