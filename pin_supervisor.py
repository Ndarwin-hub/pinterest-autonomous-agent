"""Autonomous Pin-command supervisor for both manual and background jobs.

This module is intentionally a thin policy layer over the existing Pinterest
pipeline. It does not publish Pins itself and never bypasses the existing
idempotency, image-diversity, quality, board, or verification gates.
"""
from __future__ import annotations

from typing import Any, Dict, List
from urllib.parse import urlsplit, urlunsplit

VERSION = "pin-supervisor-v1"


def normalize_for_identity(url: str) -> str:
    """Normalize only for job identity; never use this value as the destination."""
    raw = str(url or "").strip()
    p = urlsplit(raw)
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, p.query, ""))


def _pins(result: Any) -> List[Dict[str, Any]]:
    value = result.get("pins") if isinstance(result, dict) else None
    return value if isinstance(value, list) else []


def inspect_result(result: Dict[str, Any], requested_url: str) -> Dict[str, Any]:
    """Apply the Pin command's final success contract to an existing job result."""
    pins = _pins(result)
    failures: List[str] = []
    if len(pins) != 5:
        failures.append(f"expected exactly 5 Pins, found {len(pins)}")

    requested = str(requested_url or "").strip()
    for index, pin in enumerate(pins, 1):
        if not isinstance(pin, dict):
            failures.append(f"Pin {index}: malformed result")
            continue
        if not pin.get("verified"):
            failures.append(f"Pin {index}: not individually verified")
        destination = str(pin.get("destination_url") or "").strip()
        if destination and destination != requested:
            failures.append(f"Pin {index}: destination URL mismatch")
        if not destination:
            failures.append(f"Pin {index}: missing destination URL")
        if not pin.get("pin_id"):
            failures.append(f"Pin {index}: missing Pinterest Pin ID")

    success = not failures and len(pins) == 5
    out = dict(result)
    out["pin_supervisor"] = {
        "version": VERSION,
        "status": "SUCCESS" if success else "UNCONFIRMED",
        "success_contract": "5_pins_published_and_individually_verified",
        "verified_count": sum(1 for p in pins if isinstance(p, dict) and p.get("verified")),
        "failures": failures,
    }
    out["pins_published"] = len(pins)
    out["pin_supervisor_status"] = "SUCCESS" if success else "UNCONFIRMED"
    return out


def capability_contract() -> Dict[str, Any]:
    """Describe what this supervisor owns without claiming providers are connected."""
    return {
        "version": VERSION,
        "master": "application_supervisor",
        "delegates": "only actually executable configured providers",
        "publication_executor": "existing_pinterest_pipeline",
        "railway_role": "background_executor_and_scheduler",
        "success_contract": "5_pins_published_and_individually_verified",
        "manual_submit_preserved": True,
        "idempotency_bypass": False,
        "image_diversity_bypass": False,
    }


def install_runtime(agent_module: Any) -> None:
    """Install a final-result supervisor before main imports the job function."""
    if getattr(agent_module, "_pin_supervisor_installed", False):
        return
    original = agent_module.process_pinterest_job

    async def supervised(job_id: str, url: str, job_store: Any):
        result = await original(job_id, url, job_store)
        checked = inspect_result(result, url)
        if checked["pin_supervisor_status"] != "SUCCESS":
            # Preserve the underlying result for callers that can inspect the
            # exception, but fail closed: a partial/unverified job is not success.
            raise RuntimeError(
                "Pin supervisor rejected final job state: "
                + "; ".join(checked["pin_supervisor"]["failures"])
            )
        return checked

    agent_module.process_pinterest_job = supervised
    agent_module._pin_supervisor_installed = True
