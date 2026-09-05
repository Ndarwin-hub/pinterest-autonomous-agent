"""Job models and persistent store (SQLite in /tmp for Railway)."""
import os
import json
import sqlite3
from enum import Enum
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from dataclasses import dataclass, field, asdict
from pathlib import Path

DB_PATH = Path(os.getenv("JOB_DB_PATH", "/tmp/pinterest_agent_jobs.db"))

class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

@dataclass
class Job:
    job_id: str
    url: str
    status: JobStatus = JobStatus.QUEUED
    progress: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

class JobStore:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._memory: Dict[str, Job] = {}
        self._init_db()

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        return conn

    def _init_db(self):
        try:
            conn = self._connect()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    progress TEXT,
                    result TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"JobStore SQLite init warning: {e}")

    def save(self, job: Job):
        self._memory[job.job_id] = job
        try:
            conn = self._connect()
            conn.execute(
                """INSERT OR REPLACE INTO jobs
                   (job_id, url, status, progress, result, error, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job.job_id,
                    job.url,
                    job.status.value,
                    job.progress,
                    json.dumps(job.result) if job.result else None,
                    job.error,
                    job.created_at,
                    job.updated_at,
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"JobStore save warning: {e}")

    def get(self, job_id: str) -> Optional[Job]:
        if job_id in self._memory:
            return self._memory[job_id]
        try:
            conn = self._connect()
            row = conn.execute(
                "SELECT job_id, url, status, progress, result, error, created_at, updated_at FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            conn.close()
            if not row:
                return None
            job = Job(
                job_id=row[0],
                url=row[1],
                status=JobStatus(row[2]),
                progress=row[3],
                result=json.loads(row[4]) if row[4] else None,
                error=row[5],
                created_at=row[6],
                updated_at=row[7],
            )
            self._memory[job_id] = job
            return job
        except Exception as e:
            print(f"JobStore get warning: {e}")
            return self._memory.get(job_id)

    def update(self, job_id: str, **kwargs):
        job = self.get(job_id)
        if not job:
            return
        for k, v in kwargs.items():
            if hasattr(job, k):
                setattr(job, k, v)
        job.updated_at = datetime.now(timezone.utc).isoformat()
        self.save(job)
