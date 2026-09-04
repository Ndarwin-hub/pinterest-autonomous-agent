"""
Autonomous Pinterest Agent - Railway service
Accepts product URL, runs background job via Composio tools, publishes Pins.
"""
import os
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException, Header, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, HttpUrl, Field
from dotenv import load_dotenv

load_dotenv()

from agent import process_pinterest_job
from models import JobStore, JobStatus, Job

# Logging
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("pinterest-agent")

# In-memory + simple persistent store (SQLite via models)
job_store = JobStore()

API_SECRET = os.getenv("API_SECRET", "")

def verify_secret(x_api_secret: Optional[str] = Header(None)):
    if API_SECRET and x_api_secret != API_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing API secret")
    return True

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Pinterest Autonomous Agent starting...")
    yield
    logger.info("Shutting down...")

app = FastAPI(
    title="Pinterest Autonomous Agent",
    description="Submit a product URL. The agent researches, generates 5 Pins, and publishes via Composio.",
    version="1.0.0",
    lifespan=lifespan,
)

class SubmitRequest(BaseModel):
    url: HttpUrl = Field(..., description="Exact product or multi-product page URL. Must be preserved exactly as destination link.")

class SubmitResponse(BaseModel):
    job_id: str
    status: str
    message: str

class StatusResponse(BaseModel):
    job_id: str
    status: str
    progress: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    created_at: str
    updated_at: str

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "pinterest-autonomous-agent",
        "time": datetime.now(timezone.utc).isoformat(),
    }

@app.post("/submit", response_model=SubmitResponse)
async def submit(
    body: SubmitRequest,
    background_tasks: BackgroundTasks,
    _: bool = Depends(verify_secret),
):
    url_str = str(body.url)
    job_id = str(uuid.uuid4())

    job = Job(
        job_id=job_id,
        url=url_str,
        status=JobStatus.QUEUED,
        progress="Job accepted, waiting for background worker",
    )
    job_store.save(job)

    background_tasks.add_task(run_job, job_id, url_str)

    logger.info(f"Job {job_id} queued for URL: {url_str}")
    return SubmitResponse(
        job_id=job_id,
        status=JobStatus.QUEUED.value,
        message="Job accepted. Processing continues even if client disconnects. Poll /status/{job_id}",
    )

@app.get("/status/{job_id}", response_model=StatusResponse)
async def status(job_id: str, _: bool = Depends(verify_secret)):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return StatusResponse(
        job_id=job.job_id,
        status=job.status.value,
        progress=job.progress,
        result=job.result,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )

@app.get("/")
async def root():
    return {
        "service": "Pinterest Autonomous Agent",
        "endpoints": {
            "health": "GET /health",
            "submit": "POST /submit  body: {\"url\": \"<product_url>\"}",
            "status": "GET /status/{job_id}",
        },
        "note": "Set API_SECRET env var and pass X-API-Secret header for protected endpoints.",
    }

async def run_job(job_id: str, url: str):
    """Background task entrypoint. Continues after client disconnect."""
    try:
        job_store.update(job_id, status=JobStatus.RUNNING, progress="Starting research and content generation")
        result = await process_pinterest_job(job_id, url, job_store)
        job_store.update(
            job_id,
            status=JobStatus.COMPLETED,
            progress="Finished",
            result=result,
        )
        logger.info(f"Job {job_id} completed successfully")
    except Exception as e:
        logger.exception(f"Job {job_id} failed")
        job_store.update(
            job_id,
            status=JobStatus.FAILED,
            progress="Failed",
            error=str(e),
        )
