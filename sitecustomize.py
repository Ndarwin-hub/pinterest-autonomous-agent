"""Runtime hardening for Composio execution from Railway.

Routes authenticated Pinterest/Gmail calls through their explicit Composio
connected-account IDs instead of legacy user/entity routing. A read-only
Pinterest self-test can be enabled at startup to prove the transport works.
"""
from __future__ import annotations
import asyncio
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger("pinterest-agent.composio-transport")

def _account_for(slug: str) -> Optional[str]:
    s = slug.upper()
    if s.startswith("PINTEREST_"):
        return os.getenv("COMPOSIO_PINTEREST_ACCOUNT_ID", "").strip() or None
    if s.startswith("GMAIL_"):
        return os.getenv("COMPOSIO_GMAIL_ACCOUNT_ID", "").strip() or None
    key = "COMPOSIO_ACCOUNT_" + "".join(c if c.isalnum() else "_" for c in s)
    return os.getenv(key, "").strip() or None

async def _direct_execute(tool_slug: str, arguments: Dict[str, Any], retries: int = 2) -> Dict[str, Any]:
    import httpx
    key = os.getenv("COMPOSIO_API_KEY", "").strip()
    if not key:
        raise RuntimeError("COMPOSIO_API_KEY is not set in Railway variables.")
    payload: Dict[str, Any] = {
        "arguments": arguments or {},
        "version": "latest",
        "dangerously_skip_version_check": True,
    }
    account = _account_for(tool_slug)
    if account:
        payload["connected_account_id"] = account
    else:
        payload["user_id"] = os.getenv("COMPOSIO_ENTITY_ID", "default").strip() or "default"
    url = f"https://backend.composio.dev/api/v3.1/tools/execute/{tool_slug}"
    headers = {"x-api-key": key, "Content-Type": "application/json"}
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
    logger.info("Composio transport hardening installed; explicit connected-account routing enabled.")

    if os.getenv("COMPOSIO_TRANSPORT_SELFTEST", "").strip().lower() in {"1", "true", "yes"}:
        async def _selftest() -> None:
            try:
                data = await _direct_execute("PINTEREST_LIST_BOARDS", {"page_size": 1}, retries=1)
                items = data.get("items") or data.get("boards") or [] if isinstance(data, dict) else []
                logger.info("COMPOSIO_TRANSPORT_SELFTEST=PASS pinterest_connected_account board_read=%s", bool(items))
            except Exception as exc:
                logger.error("COMPOSIO_TRANSPORT_SELFTEST=FAIL %s", exc)

        asyncio.run(_selftest())
except Exception:
    logger.exception("Composio transport hardening could not be installed; original transport retained.")
