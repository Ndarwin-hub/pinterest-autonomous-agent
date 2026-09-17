"""Pin-command result supervisor for manual and background jobs.

The supervisor reports the actual per-Pin outcome without rolling back Pins
that were successfully published. It does not publish or unpublish Pins and
never bypasses the existing idempotency, image-diversity, quality, board, or
verification gates.
"""
from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import urlsplit, urlunsplit

VERSION = "pin-supervisor-v2"


def normalize_for_identity(url: str) -> str:
    """Normalize only for job identity; never use this value as the destination."""
    raw = str(url or "").strip()
    p = urlsplit(raw)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))


def _pins(result: Any) -> List[Dict[str, Any]]:
    value = result.get("pins") if isinstance(result, dict) else None
    return value if isinstance(value, list) else []


def inspect_result(result: Dict[str, Any], requested_url: str) -> Dict[str, Any]:
    """Classify the completed publication attempt by the actual Pin results.

    5/5 is completed, 1-4/5 is completed_partial, and 0/5 is failed.
    Successfully published Pins are retained; there is no rollback/unpublish
    operation in this supervisor.
    """
    pins = _pins(result)
    requested = str(requested_url or "").strip()
    valid_verified: List[Dict[str, Any]] = []
    failures: List[str] = []

    for index, pin in enumerate(pins, 1):
        if not isinstance(pin, dict):
            failures.append(f"Pin {index}: malformed result")
            continue
        pin_id = str(pin.get("pin_id") or "").strip()
        destination = str(pin.get("destination_url") or "").strip()
        if pin.get("verified") and pin_id and destination == requested:
            valid_verified.append(pin)
        else:
            reasons = []
            if not pin.get("verified"):
                reasons.append("not individually verified")
            if not pin_id:
                reasons.append("missing Pinterest Pin ID")
            if destination != requested:
                reasons.append("destination URL mismatch")
            failures.append(f"Pin {index}: {', '.join(reasons)}")

    verified_count = len(valid_verified)
    if verified_count == 5:
        status = "completed"
    elif verified_count > 0:
        status = "completed_partial"
    else:
        status = "failed"

    out = dict(result)
    out["pin_supervisor"] = {
        "version": VERSION,
        "status": status,
        "success_contract": "5_pins_published_and_individually_verified_for_completed",
        "verified_count": verified_count,
        "planned_count": 5,
        "failures": failures,
        "rollback_unpublish": False,
    }
    out["pins_published"] = len(pins)
    out["verified_pins"] = verified_count
    out["pin_supervisor_status"] = status
    out["job_status"] = status
    return out


def capability_contract() -> Dict[str, Any]:
    """Describe supervisor ownership without claiming providers are connected."""
    return {
        "version": VERSION,
        "master": "application_supervisor",
        "delegates": "only actually executable configured providers",
        "publication_executor": "existing_pinterest_pipeline",
        "railway_role": "background_executor_and_scheduler",
        "completion_contract": "5_completed; 1_to_4_completed_partial; 0_failed",
        "successful_pins_are_kept": True,
        "unpublish_on_partial": False,
        "manual_submit_preserved": True,
        "idempotency_bypass": False,
        "image_diversity_bypass": False,
    }


def install_runtime(agent_module: Any) -> None:
    """Install a non-destructive result classifier around the existing job."""
    if getattr(agent_module, "_pin_supervisor_installed", False):
        return
    original = agent_module.process_pinterest_job

    async def supervised(job_id: str, url: str, job_store: Any):
        result = await original(job_id, url, job_store)
        return inspect_result(result, url)

    agent_module.process_pinterest_job = supervised
    agent_module._pin_supervisor_installed = True
