"""Minimal dependency-free MCP/JSON-RPC bridge for Composio Custom MCP.

The bridge exposes exactly one tool and forwards the exact URL to the existing
Railway /submit intake. It intentionally does not duplicate the Pinterest
workflow.
"""
import asyncio
import os
from typing import Any, Dict

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "").strip()
API_SECRET = os.getenv("API_SECRET", "").strip()
BRIDGE_TOKEN = os.getenv("MCP_BRIDGE_TOKEN", "").strip()
PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "web-production-dae68.up.railway.app").strip()
SUBMIT_URL = f"https://{PUBLIC_DOMAIN}/submit"
MCP_TOOLKIT_SLUG = "PINTEREST_RAILWAY_BRIDGE"
MCP_PATH = f"/mcp/{BRIDGE_TOKEN}" if BRIDGE_TOKEN else ""

router = APIRouter()

TOOL = {
    "name": "PINTEREST_SUBMIT_URL",
    "description": (
        "Submit one exact product/affiliate URL to the autonomous Pinterest workflow. "
        "Pass the URL unchanged; do not shorten, rewrite, or replace it."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "Exact http(s) product or affiliate URL."}},
        "required": ["url"],
        "additionalProperties": False,
    },
}


def _result(request_id: Any, result: Dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


async def _submit_exact_url(url: str) -> str:
    if not API_SECRET:
        raise RuntimeError("Railway API secret is not configured")
    value = (url or "").strip()
    if not value.startswith(("http://", "https://")):
        raise ValueError("url must be an http(s) URL")
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
        response = await client.post(
            SUBMIT_URL,
            headers={"X-API-Secret": API_SECRET},
            json={"url": value},
        )
    if response.status_code >= 400:
        raise RuntimeError(f"Railway /submit returned HTTP {response.status_code}: {response.text[:500]}")
    data = response.json()
    return (
        f"Railway accepted the exact URL. job_id={data.get('job_id')}; "
        f"status={data.get('status')}; message={data.get('message')}"
    )


@router.post("/")
async def mcp_endpoint(request: Request):
    """Handle the small MCP Streamable-HTTP JSON-RPC surface needed by Composio."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    request_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    # JSON-RPC notifications intentionally receive 202 with no body.
    if request_id is None:
        if method in {"notifications/initialized", "notifications/cancelled"}:
            return Response(status_code=202)
        if method == "ping":
            return Response(status_code=202)

    if method == "initialize":
        requested = params.get("protocolVersion") or "2025-06-18"
        return _result(
            request_id,
            {
                "protocolVersion": requested,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "Pinterest Railway Bridge", "version": "1.0.0"},
                "instructions": "Use PINTEREST_SUBMIT_URL for exact product/affiliate URLs.",
            },
        )

    if method == "ping":
        return _result(request_id, {})

    if method == "tools/list":
        return _result(request_id, {"tools": [TOOL]})

    if method == "tools/call":
        name = params.get("name")
        if name != TOOL["name"]:
            return _error(request_id, -32601, f"Unknown tool: {name}")
        arguments = params.get("arguments") or {}
        try:
            text = await _submit_exact_url(arguments.get("url", ""))
            return _result(
                request_id,
                {"content": [{"type": "text", "text": text}], "isError": False},
            )
        except Exception as exc:
            return _result(
                request_id,
                {"content": [{"type": "text", "text": str(exc)}], "isError": True},
            )

    return _error(request_id, -32601, f"Unsupported MCP method: {method}")


async def _register_once() -> bool:
    if not (COMPOSIO_API_KEY and MCP_PATH):
        return False
    app_url = f"https://{PUBLIC_DOMAIN}{MCP_PATH}/"
    headers = {"x-api-key": COMPOSIO_API_KEY, "Content-Type": "application/json"}
    payload = {
        "slug": MCP_TOOLKIT_SLUG,
        "toolkit_config": {
            "name": "Pinterest Railway Bridge",
            "app_url": app_url,
            "auth_schemes": [{"mode": "NO_AUTH"}],
        },
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://backend.composio.dev/api/v3.1/custom/toolkits/upsert",
            headers=headers,
            json=payload,
        )
        response.raise_for_status()
        normalized = response.json().get("slug", "CUSTOM_" + MCP_TOOLKIT_SLUG)
        sync = await client.post(
            "https://backend.composio.dev/api/v3.1/custom/toolkits/sync",
            headers=headers,
            json={"slug": normalized},
        )
        sync.raise_for_status()
        return True


async def register_custom_mcp_with_retry() -> bool:
    if not (COMPOSIO_API_KEY and MCP_PATH):
        return False
    for attempt in range(1, 6):
        try:
            if await _register_once():
                print("Composio Custom MCP bridge registered and synced")
                return True
        except Exception as exc:
            print(f"Composio Custom MCP registration attempt {attempt} failed: {exc}")
        await asyncio.sleep(min(2 ** attempt, 15))
    return False
