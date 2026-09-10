"""
Autonomous Pinterest Agent - Railway service
Accepts product/affiliate URL, publishes 5 unique Pins via Composio.

The existing /submit route remains intact. A separate protected bridge endpoint
allows an external ChatGPT action/app to submit the same URL into the same job
executor without creating a second Pinterest workflow.
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

import agent as agent_module
from wire_board_org import apply_agent_wiring
from quota import quota

apply_agent_wiring(agent_module)
from agent import process_pinterest_job
from models import JobStore, JobStatus, Job

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("pinterest-agent")

job_store = JobStore()
API_SECRET = os.getenv("API_SECRET", "").strip()
CHATGPT_BRIDGE_SECRET = os.getenv("CHATGPT_BRIDGE_SECRET", "").strip()


def verify_secret(x_api_secret: Optional[str] = Header(None)):
    if API_SECRET and x_api_secret != API_SECRET:
        raise HTTPException(status_code=401, detail="Invalid or missing API secret")
    return True


def verify_bridge_secret(
    x_bridge_secret: Optional[str] = Header(None),
    authorization: Optional[str] = Header(None),
):
    # A bridge must never be exposed without an explicit secret. Prefer a
    # dedicated secret; API_SECRET remains a compatibility fallback.
    expected = CHATGPT_BRIDGE_SECRET or API_SECRET
    if not expected:
        raise HTTPException(status_code=503, detail="ChatGPT bridge is not configured")
    bearer = ""
    if authorization and authorization.lower().startswith("bearer "):
        bearer = authorization[7:].strip()
    supplied = x_bridge_secret or bearer
    if supplied != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing bridge secret")
    return True


def extract_url(text: str) -> str:
    text = (text or "").strip()
    m = re.search(r"https?://\S+", text)
    if m:
        return m.group(0).rstrip(").,]',\"")
    if text.startswith("http"):
        return text
    raise ValueError("No valid http(s) URL found")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Pinterest Autonomous Agent v3 starting...")
    logger.info("Quota governor: %s", quota.snapshot())
    yield
    logger.info("Shutting down...")


app = FastAPI(
    title="Pinterest Autonomous Agent",
    description="Submit one product URL. Agent researches, creates 5 unique Pins with multi-provider images, publishes and verifies.",
    version="3.3.0",
    lifespan=lifespan,
)


class SubmitRequest(BaseModel):
    url: str = Field(..., description="Product/affiliate URL. Exact URL preserved as destination for all pins.")


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


async def enqueue_job(url_str: str, background_tasks: BackgroundTasks) -> SubmitResponse:
    if not quota.reserve_job():
        snapshot = quota.snapshot()
        raise HTTPException(
            status_code=429,
            detail={
                "message": "Monthly safe Pinterest capacity reached; job not started.",
                "quota": snapshot,
            },
        )

    job_id = str(uuid.uuid4())
    job = Job(
        job_id=job_id,
        url=url_str,
        status=JobStatus.QUEUED,
        progress="Job accepted — 5-pin workflow queued",
    )
    job_store.save(job)
    background_tasks.add_task(run_job, job_id, url_str)
    logger.info(f"Job {job_id} queued for URL: {url_str}")
    return SubmitResponse(
        job_id=job_id,
        status=JobStatus.QUEUED.value,
        message="Job accepted. 5 Pins will be researched, imaged, published and verified. Poll /status/{job_id}",
    )


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "service": "pinterest-autonomous-agent",
        "version": "3.3.0",
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/quota")
async def quota_status(_: bool = Depends(verify_secret)):
    """Non-secret monthly production capacity report."""
    return quota.snapshot()


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
    return await enqueue_job(url_str, background_tasks)


@app.post("/bridge/chatgpt/pin", response_model=SubmitResponse)
async def chatgpt_pin_bridge(
    body: SubmitRequest,
    background_tasks: BackgroundTasks,
    _: bool = Depends(verify_bridge_secret),
):
    """Protected intake for a future ChatGPT action/app.

    It intentionally calls the same enqueue path as /submit, so ChatGPT and
    Grok cannot drift into separate Pinterest implementations.
    """
    try:
        url_str = extract_url(body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await enqueue_job(url_str, background_tasks)


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
        "version": "3.3.0",
        "endpoints": {
            "health": "GET /health",
            "submit": "POST /submit body: {\"url\": \"<product_url>\"}",
            "chatgpt_bridge": "POST /bridge/chatgpt/pin body: {\"url\": \"<product_url>\"}",
            "status": "GET /status/{job_id}",
            "quota": "GET /quota",
        },
        "usage": "Send one product/affiliate URL. System creates 5 unique Pins automatically.",
    }


async def run_job(job_id: str, url: str):
    try:
        job_store.update(job_id, status=JobStatus.RUNNING, progress="Starting 5-pin workflow")
        result = await process_pinterest_job(job_id, url, job_store)
        quota.record_job(True)
        result["quota"] = quota.snapshot()
        job_store.update(job_id, status=JobStatus.COMPLETED, progress="Finished", result=result)
        logger.info(f"Job {job_id} completed: {result.get('summary')}")
    except Exception as e:
        quota.record_job(False)
        logger.exception(f"Job {job_id} failed")
        job_store.update(job_id, status=JobStatus.FAILED, progress="Failed", error=str(e))
