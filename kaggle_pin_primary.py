"""Railway-independent Kaggle executor for the existing Pinterest Pin A pipeline.

The production discovery, image-quality, board-routing, publication and verification
logic is reused from the repository. Only the worker/ledger transport is replaced.
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import io
import json
import os
import sqlite3
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

STATE_FILE_ID = os.getenv("DRIVE_STATE_FILE_ID", "1Qf-Ccn5aiyRo-tdgJxj5x0UhmYCVrEQb")
STATE_FILE_NAME = "desiredplus-kaggle-automation-state.json"
DB_NAMES = ("amazon_daily_ledger.db", "published_products.db", "pinterest_agent_jobs.db")
DATA_DIR = Path("/kaggle/working/desiredplus-state")
DATA_DIR.mkdir(parents=True, exist_ok=True)
os.environ["DATA_DIR"] = str(DATA_DIR)
os.environ["DAILY_LEDGER_DB_PATH"] = str(DATA_DIR / DB_NAMES[0])
os.environ["PUBLISHED_DB_PATH"] = str(DATA_DIR / DB_NAMES[1])
os.environ["JOB_DB_PATH"] = str(DATA_DIR / DB_NAMES[2])
os.environ["AMAZON_SCHEDULER_ENABLED"] = "true"
os.environ["AMAZON_SCHEDULER_MODE"] = "external"
os.environ["AMAZON_DISCOVERY_DISABLED"] = "0"
os.environ["RAILWAY_VIDEO_RENDERING_ENABLED"] = "false"
os.environ.setdefault("PIN_N_INDEPENDENT_SEARCH_ENABLED", "1")

def get_secret(name: str, default: str = "") -> str:
    value = os.getenv(name, "").strip()
    if value:
        return value
    try:
        from kaggle_secrets import UserSecretsClient
        return (UserSecretsClient().get_secret(name) or "").strip()
    except Exception:
        return default

COMPOSIO_API_KEY = get_secret("COMPOSIO_API_KEY")
COMPOSIO_ENTITY_ID = get_secret("COMPOSIO_ENTITY_ID")
PINTEREST_ACCOUNT_ID = get_secret(
    "COMPOSIO_PINTEREST_ACCOUNT_ID", "pinterest_naker-shoya"
)
if not COMPOSIO_API_KEY or not COMPOSIO_ENTITY_ID:
    raise RuntimeError("KAGGLE_SECRET_MISSING:COMPOSIO_API_KEY_OR_COMPOSIO_ENTITY_ID")
os.environ["COMPOSIO_API_KEY"] = COMPOSIO_API_KEY
os.environ["COMPOSIO_ENTITY_ID"] = COMPOSIO_ENTITY_ID
os.environ["COMPOSIO_PINTEREST_ACCOUNT_ID"] = PINTEREST_ACCOUNT_ID

def composio(slug: str, arguments: dict) -> dict:
    payload = {
        "arguments": arguments or {},
        "version": "latest",
        "dangerously_skip_version_check": True,
        "user_id": COMPOSIO_ENTITY_ID,
    }
    if slug.upper().startswith("PINTEREST_"):
        payload.pop("user_id", None)
        payload["connected_account_id"] = PINTEREST_ACCOUNT_ID
    response = requests.post(
        f"https://backend.composio.dev/api/v3.1/tools/execute/{slug}",
        headers={"x-api-key": COMPOSIO_API_KEY, "Content-Type": "application/json"},
        json=payload,
        timeout=180,
    )
    response.raise_for_status()
    result = response.json()
    if result.get("successful") is False:
        raise RuntimeError(f"{slug}_FAILED:{str(result.get('error') or result)[:1000]}")
    return result.get("data") or result

def _drive_download_state() -> dict:
    result = composio("GOOGLEDRIVE_DOWNLOAD_FILE", {"fileId": STATE_FILE_ID})
    item = result.get("downloaded_file_content") or {}
    url = item.get("s3url")
    if not url:
        raise RuntimeError("DRIVE_STATE_DOWNLOAD_URL_MISSING")
    response = requests.get(url, timeout=120)
    response.raise_for_status()
    state = json.loads(response.content.decode("utf-8"))
    archive_b64 = state.get("databases", {}).get("sqlite_gzip_b64")
    if archive_b64:
        archive = gzip.decompress(base64.b64decode(archive_b64))
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
            for member in tar.getmembers():
                if member.name not in DB_NAMES or not member.isfile():
                    continue
                target = DATA_DIR / member.name
                extracted = tar.extractfile(member)
                if extracted:
                    target.write_bytes(extracted.read())
    return state

def _database_archive_b64() -> str:
    for name in DB_NAMES:
        path = DATA_DIR / name
        if path.exists():
            try:
                con = sqlite3.connect(str(path))
                con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                con.close()
            except Exception:
                pass
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:") as tar:
        for name in DB_NAMES:
            path = DATA_DIR / name
            if path.exists():
                tar.add(path, arcname=name)
    return base64.b64encode(gzip.compress(raw.getvalue(), compresslevel=6)).decode("ascii")

def _read_manifests() -> tuple[list, list]:
    products, video_queue = [], []
    pub_db = DATA_DIR / "published_products.db"
    if pub_db.exists():
        con = sqlite3.connect(str(pub_db))
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                """SELECT asin,product_url,affiliate_url,board_id,board_name,pin_ids,
                          completed_at,source,status,pinterest_verified
                   FROM published_products WHERE status='success' ORDER BY completed_at"""
            ).fetchall()
            products = [dict(row) for row in rows]
        except sqlite3.Error:
            products = []
        finally:
            con.close()
    ledger_db = DATA_DIR / "amazon_daily_ledger.db"
    if ledger_db.exists():
        con = sqlite3.connect(str(ledger_db))
        con.row_factory = sqlite3.Row
        try:
            rows = con.execute(
                """SELECT day,batch_index,slot,asin,product_url,affiliate_url,title,
                          source,status,attempts,last_error,created_at,updated_at,
                          completed_at,publication_json,image_urls_json,image_meta_json
                   FROM video_jobs WHERE source='pinterest' ORDER BY day,batch_index,slot"""
            ).fetchall()
            video_queue = [dict(row) for row in rows]
        except sqlite3.Error:
            video_queue = []
        finally:
            con.close()
    return products, video_queue

def _save_drive_state(state: dict, run_result: dict) -> None:
    products, video_queue = _read_manifests()
    state["schema_version"] = 2
    state["project"] = "desiredplus-kaggle-automation"
    state["databases"] = {"sqlite_gzip_b64": _database_archive_b64()}
    state["products_published"] = products
    state["video_queue"] = video_queue
    state["last_run"] = {
        "at": datetime.now(timezone.utc).isoformat(),
        "runner": "kaggle-pinterest-primary",
        "batch": run_result.get("batch"),
        "status": run_result.get("status"),
        "attempted": run_result.get("attempted"),
        "successes": run_result.get("successes"),
        "errors": run_result.get("errors"),
    }
    payload = json.dumps(state, separators=(",", ":"), ensure_ascii=False)
    result = composio(
        "GOOGLEDRIVE_EDIT_FILE",
        {"file_id": STATE_FILE_ID, "content": payload, "mime_type": "application/json"},
    )
    if not result.get("id"):
        raise RuntimeError("DRIVE_STATE_SAVE_UNCONFIRMED")

async def _tool_outside_product_job(agent_module, slug: str, arguments: dict) -> dict:
    # Board inventory/preflight calls happen outside a product's 40-call budget.
    transport = getattr(agent_module, "_composio_transport_executor", None)
    if transport is None:
        raise RuntimeError("COMPOSIO_CONNECTED_ACCOUNT_TRANSPORT_NOT_INSTALLED")
    return await transport(slug, arguments or {}, retries=0)

def _install_existing_pin_pipeline():
    import agent as agent_module
    import quality_patch
    quality_patch.install_identity(agent_module)
    from wire_board_org import apply_agent_wiring
    apply_agent_wiring(agent_module)
    import runtime_hardening
    runtime_hardening.install(agent_module)
    import provider_failover
    provider_failover.install(__import__("wire_board_org"), __import__("ai_quality_gate"))
    import publication_guard
    import image_priority
    import image_diversity_guard
    publication_guard.install(agent_module)
    image_priority.install(agent_module)
    image_diversity_guard.install(agent_module)
    quality_patch.install_process_gate(agent_module)
    return agent_module

async def _run(batch: int, state: dict) -> dict:
    from models import JobStore, Job, JobStatus
    from pin_config import PINS_PER_PRODUCT
    from agent import process_pinterest_job
    from amazon_scheduler import AmazonScheduler, pinterest_target_verified
    import amazon_scheduler as scheduler_module

    agent_module = _install_existing_pin_pipeline()
    job_store = JobStore()
    async def list_boards():
        data = await _tool_outside_product_job(agent_module, "PINTEREST_LIST_BOARDS", {})
        return data.get("items") or data.get("boards") or []
    async def enqueue(url, target_board_id=None, target_board_name=None, force_new=False):
        existing = job_store.find_by_url(url)
        if existing and not force_new:
            if existing.result and pinterest_target_verified(existing.result):
                return {"job_id": existing.job_id, "status": existing.status.value}
        job_id = str(uuid.uuid4())
        job = Job(job_id=job_id, url=url, status=JobStatus.RUNNING,
                  progress=f"Starting {PINS_PER_PRODUCT}-Pin workflow",
                  target_board_id=target_board_id, target_board_name=target_board_name)
        job_store.save(job)
        try:
            result = await process_pinterest_job(job_id, url, job_store)
            verified = int(result.get("verified_pins") or sum(
                1 for pin in (result.get("pins") or [])
                if isinstance(pin, dict) and pin.get("verified")
            ))
            published = int(result.get("pins_published") or 0)
            if pinterest_target_verified(result):
                status = JobStatus.COMPLETED
            elif verified or published:
                status = JobStatus.COMPLETED_PARTIAL
            else:
                status = JobStatus.FAILED
            job_store.update(job_id, status=status, progress="Finished", result=result)
            return {"job_id": job_id, "status": status.value, "result": result}
        except Exception as exc:
            job_store.update(job_id, status=JobStatus.FAILED, error=str(exc), progress="Failed")
            return {"job_id": job_id, "status": "failed", "error": str(exc)}
    async def wait_job(job_id):
        job = job_store.get(job_id)
        if not job:
            return {"status": "missing"}
        return {"status": job.status.value, "error": job.error, "result": job.result}
    async def no_video_sidecar(day, batch_index):
        return {"status": "disabled_in_kaggle_pin_runner"}
    scheduler_module.on_pinterest_batch_start = no_video_sidecar
    scheduler = AmazonScheduler()
    return await scheduler.run_batch(batch, enqueue, list_boards, wait_job)

async def _preflight():
    agent_module = _install_existing_pin_pipeline()
    boards_data = await _tool_outside_product_job(agent_module, "PINTEREST_LIST_BOARDS", {})
    boards = boards_data.get("items") or boards_data.get("boards") or []
    if not boards:
        raise RuntimeError("PINTEREST_PREFLIGHT_NO_BOARDS")
    from amazon_discovery import discover_for_board
    candidate = await discover_for_board("Electronics & Gadgets", exclude_asins=set())
    return {"status": "preflight_passed", "boards": len(boards),
            "sample_candidate": (candidate or {}).get("asin"),
            "candidate_found": bool(candidate)}

def main():
    batch = int(os.getenv("KAGGLE_PIN_BATCH", "1"))
    if batch < 1 or batch > 10:
        raise RuntimeError("KAGGLE_PIN_BATCH_MUST_BE_1_TO_10")
    state = _drive_download_state()
    started = datetime.now(timezone.utc).isoformat()
    if os.getenv("KAGGLE_PIN_DRY_RUN", "false").strip().lower() in ("1", "true", "yes"):
        result = asyncio.run(_preflight())
        _save_drive_state(state, {"batch": batch, **result})
        print(json.dumps({"event": "KAGGLE_PIN_PREFLIGHT_FINISHED",
                          "started_at": started, "batch": batch, **result,
                          "drive_state_saved": True}, ensure_ascii=False))
        return
    result = asyncio.run(_run(batch, state))
    _save_drive_state(state, result)
    print(json.dumps({
        "event": "KAGGLE_PIN_BATCH_FINISHED",
        "started_at": started,
        "batch": batch,
        "status": result.get("status"),
        "attempted": result.get("attempted"),
        "successes": result.get("successes"),
        "errors": result.get("errors"),
        "drive_state_saved": True,
    }, ensure_ascii=False))
    if result.get("status") not in ("completed", "already_completed"):
        raise RuntimeError("PINTEREST_BATCH_NOT_COMPLETED:" + json.dumps(result)[:1800])

main()
