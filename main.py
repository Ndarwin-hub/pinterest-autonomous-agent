"""
Autonomous Pinterest Agent - Railway service
Accepts product/affiliate URL, runs background job, publishes one Pin via Composio.
"""
import os
import uuid
import re
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, BackgroundTasks, HTTPException, Header, Depends
from pydantic import BaseModel, Field
from dotenv import load_dotenv

load_dotenv()

from agent import process_pinterest_job
from models import JobStore, JobStatus, Job

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("pinterest-agent")

job_store = JobStore()
API_SECRET = os.getenv("API_SECRET", "")


def verify_secret(x_api_secret: Optional[str] = Header(None)):
    if API_SECRET and x_api_secret != API_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing API secret")
    return True


def extract_url(text: str) -> str:
    text = (text or "").strip()
    m = re.search(r"https?://\S+", text)
    if m:
        return m.group(0).rstrip(").,]'")
    if text.startswith("http"):
        return text
    raise ValueError("No valid http(s) URL found")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Pinterest Autonomous Agent starting...")
    yield
    logger.info("Shutting down...")


app = FastAPI(
    title="Pinterest Autonomous Agent",
    description="Submit a product/affiliate URL. Agent researches, creates SEO content, publishes one Pin, verifies.",
    version="2.0.0",
    lifespan=lifespan,
)


class SubmitRequest(BaseModel):
    url: str = Field(
        ...,
        description="Product/affiliate URL. You may also send 'Pinterest https://...'. Exact URL is preserved as Pin destination.",
    )


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
        "version": "2.0.0",
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/submit", response_model=SubmitResponse)
async def submit(
    body: SubmitRequest,
    background_tasks: BackgroundTasks,
    _: bool = Depends(verify_secret),
):
    try:
        url_str = extract_url(body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

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
        "version": "2.0.0",
        "endpoints": {
            "health": "GET /health",
            "submit": "POST /submit  body: {\"url\": \"<product_or_affiliate_url>\"}",
            "status": "GET /status/{job_id}",
        },
        "usage": "Send only the product/affiliate URL. System handles research, SEO, image, publish, verify.",
    }


async def run_job(job_id: str, url: str):
    try:
        job_store.update(job_id, status=JobStatus.RUNNING, progress="Starting workflow")
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
