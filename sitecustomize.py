"""Composio Railway transport hardening.

Uses explicit connected accounts for Pinterest/Gmail so scheduled Railway jobs do
not depend on managed-agent job routing. Falls back to the existing transport for
other tools.
"""
from __future__ import annotations
import asyncio, logging, os
from typing import Any, Dict, Optional

logger = logging.getLogger("pinterest-agent.composio-transport")

def _account_for(slug: str) -> Optional[str]:
    s = slug.upper()
    if s.startswith("PINTEREST_"):
        return os.getenv("COMPOSIO_PINTEREST_ACCOUNT_ID", "").strip() or None
    if s.startswith("GMAIL_"):
        return os.getenv("COMPOSIO_GMAIL_ACCOUNT_ID", "").strip() or None
    return None

async def _direct_execute(tool_slug: str, arguments: Dict[str, Any], retries: int = 2) -> Dict[str, Any]:
    import httpx
    key = os.getenv("COMPOSIO_API_KEY", "").strip()
    if not key:
        raise RuntimeError("COMPOSIO_API_KEY is not set in Railway variables.")
    account = _account_for(tool_slug)
    if not account:
        raise RuntimeError(f"No explicit connected account configured for {tool_slug}.")
    url = f"https://backend.composio.dev/api/v3.1/tools/execute/{tool_slug}"
    headers = {"x-api-key": key, "Content-Type": "application/json"}
    payload = {
        "connected_account_id": account,
        "arguments": arguments or {},
        "version": "latest",
    }
    last: Optional[Exception] = None
    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(url, headers=headers, json=payload)
                try:
                    data = resp.json()
                except Exception:
                    data = {"raw": resp.text}
                if resp.status_code >= 400:
                    err = data.get("error") if isinstance(data, dict) else data
                    if isinstance(err, dict):
                        err = err.get("message") or err
                    raise RuntimeError(f"{tool_slug} HTTP {resp.status_code}: {err}")
                if isinstance(data, dict) and data.get("successful") is False:
                    err = data.get("error") or data.get("data", {}).get("message") or str(data)
                    raise RuntimeError(f"{tool_slug} unsuccessful: {err}")
                if isinstance(data, dict) and "data" in data:
                    return data["data"] if data["data"] is not None else {}
                return data if isinstance(data, dict) else {"result": data}
        except Exception as exc:
            last = exc
            if attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 8))
    raise RuntimeError(str(last) if last else f"{tool_slug} failed")

try:
    import agent as _agent
    _original = _agent.run_composio_tool

    async def _hardened_run(tool_slug: str, arguments: Dict[str, Any], retries: int = 2) -> Dict[str, Any]:
        account = _account_for(tool_slug)
        if account:
            return await _direct_execute(tool_slug, arguments, retries=retries)
        return await _original(tool_slug, arguments, retries=retries)

    _agent.run_composio_tool = _hardened_run
    logger.info("Composio explicit connected-account transport enabled.")
except Exception:
    logger.exception("Composio transport hardening could not be installed.")
