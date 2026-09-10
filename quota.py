"""Quota governor for the Pinterest production workflow.

The governor is deliberately conservative: it budgets workflow tool-call units,
keeps a monthly safety reserve, and persists its ledger in SQLite. It never
changes Pinterest content/image selection; it only prevents starting a job when
there is not enough safe budget left.
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict

DB_PATH = Path(os.getenv("JOB_DB_PATH", "/tmp/pinterest_agent_jobs.db"))
MONTHLY_LIMIT = int(os.getenv("COMPOSIO_MONTHLY_TOOL_BUDGET", "100000"))
SAFETY_RESERVE = int(os.getenv("COMPOSIO_SAFETY_RESERVE", "10000"))
# Conservative logical units per complete 5-pin job. This includes the current
# workflow's normal board/image/publish/verify activity plus retry headroom.
JOB_BUDGET = int(os.getenv("COMPOSIO_JOB_BUDGET", "45"))

_lock = Lock()


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


class QuotaGovernor:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._init_db()

    def _connect(self):
        return sqlite3.connect(str(self.db_path), check_same_thread=False)

    def _init_db(self):
        with _lock:
            conn = self._connect()
            conn.execute(
                """CREATE TABLE IF NOT EXISTS quota_ledger (
                    month TEXT PRIMARY KEY,
                    reserved_units INTEGER NOT NULL DEFAULT 0,
                    completed_jobs INTEGER NOT NULL DEFAULT 0,
                    failed_jobs INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )"""
            )
            conn.commit()
            conn.close()

    def _row(self):
        month = _month()
        conn = self._connect()
        row = conn.execute(
            "SELECT month,reserved_units,completed_jobs,failed_jobs,updated_at FROM quota_ledger WHERE month=?",
            (month,),
        ).fetchone()
        if row is None:
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "INSERT INTO quota_ledger(month,reserved_units,completed_jobs,failed_jobs,updated_at) VALUES(?,?,?,?,?)",
                (month, 0, 0, 0, now),
            )
            conn.commit()
            row = (month, 0, 0, 0, now)
        conn.close()
        return row

    def snapshot(self) -> Dict[str, Any]:
        row = self._row()
        reserved = int(row[1])
        safe_capacity = max(0, MONTHLY_LIMIT - SAFETY_RESERVE)
        remaining = max(0, safe_capacity - reserved)
        return {
            "month": row[0],
            "monthly_limit": MONTHLY_LIMIT,
            "safety_reserve": SAFETY_RESERVE,
            "safe_capacity": safe_capacity,
            "reserved_units": reserved,
            "remaining_safe_units": remaining,
            "job_budget_units": JOB_BUDGET,
            "estimated_complete_jobs_remaining": remaining // max(1, JOB_BUDGET),
            "completed_jobs": int(row[2]),
            "failed_jobs": int(row[3]),
        }

    def can_start_job(self) -> bool:
        return self.snapshot()["remaining_safe_units"] >= JOB_BUDGET

    def reserve_job(self) -> bool:
        with _lock:
            row = self._row()
            reserved = int(row[1])
            safe_capacity = max(0, MONTHLY_LIMIT - SAFETY_RESERVE)
            if reserved + JOB_BUDGET > safe_capacity:
                return False
            conn = self._connect()
            conn.execute(
                "UPDATE quota_ledger SET reserved_units=?,updated_at=? WHERE month=?",
                (reserved + JOB_BUDGET, datetime.now(timezone.utc).isoformat(), row[0]),
            )
            conn.commit()
            conn.close()
            return True

    def record_job(self, success: bool):
        with _lock:
            row = self._row()
            conn = self._connect()
            conn.execute(
                "UPDATE quota_ledger SET completed_jobs=completed_jobs+?, failed_jobs=failed_jobs+?, updated_at=? WHERE month=?",
                (1 if success else 0, 0 if success else 1, datetime.now(timezone.utc).isoformat(), row[0]),
            )
            conn.commit()
            conn.close()


quota = QuotaGovernor()
