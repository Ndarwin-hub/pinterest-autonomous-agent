"""Minimal dependency-free MCP/JSON-RPC bridge for Composio Custom MCP.

The bridge exposes exactly one tool and forwards the exact URL to the existing
Railway /submit intake. It intentionally does not duplicate the Pinterest
workflow.
"""
import asyncio
import os
from typing import Any, Dict, Optional

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "").strip()
COMPOSIO_ENTITY_ID = os.getenv("COMPOSIO_ENTITY_ID", "").strip()
API_SECRET = os.getenv("API_SECRET", "").strip()
BRIDGE_TOKEN = os.getenv("MCP_BRIDGE_TOKEN", "").strip()
PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "web-production-dae68.up.railway.app").strip()
SUBMIT_URL = f"https://{PUBLIC_DOMAIN}/submit"
MCP_TOOLKIT_SLUG = "PINTEREST_RAILWAY_BRIDGE"
CUSTOM_MCP_TOOLKIT_SLUG = "CUSTOM_PINTEREST_RAILWAY_BRIDGE"
COMPOSIO_SEARCH_TOOLKIT_SLUG = "composio_search"
COMPOSIO_BASE = "https://backend.composio.dev/api/v3.1"
MCP_PATH = f"/mcp/{BRIDGE_TOKEN}" if BRIDGE_TOKEN else ""

router = APIRouter()
_router_session_id: Optional[str] = None
_router_submit_tool_slug: Optional[str] = None
_router_session_mcp_url: Optional[str] = None

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


async def _composio_request(method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not COMPOSIO_API_KEY:
        raise RuntimeError("Composio API key is not configured")
    headers = {"x-api-key": COMPOSIO_API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=45.0) as client:
        response = await client.request(method, f"{COMPOSIO_BASE}{path}", headers=headers, json=body)
    if response.status_code >= 400:
        detail = response.text[:1000].replace("\n", " ")
        raise RuntimeError(f"Composio API HTTP {response.status_code}: {detail}")
    return response.json()


async def ensure_composio_router_session() -> Dict[str, Any]:
    """Create a Railway-owned Tool Router session and explicitly enable both toolkits."""
    global _router_session_id, _router_submit_tool_slug, _router_session_mcp_url
    if not (COMPOSIO_API_KEY and COMPOSIO_ENTITY_ID):
        return {"ready": False, "reason": "Composio credentials are not configured"}
    if _router_session_id and _router_submit_tool_slug:
        return {
            "ready": True,
            "session_id": _router_session_id,
            "tool_slug": _router_submit_tool_slug,
            "mcp_url": _router_session_mcp_url,
        }

    last_error = ""
    for attempt in range(1, 6):
        try:
            # Start with the smallest valid session. Custom MCP sync can be
            # eventually consistent, so toolkit selection is patched after creation.
            session = await _composio_request(
                "POST",
                "/tool_router/session",
                {"user_id": COMPOSIO_ENTITY_ID},
            )
            sid = str(session.get("session_id") or "")
            if not sid:
                raise RuntimeError("Composio created a session without a session_id")
            patched = await _composio_request(
                "PATCH",
                f"/tool_router/session/{sid}",
                {"toolkits": {"enabled": [COMPOSIO_SEARCH_TOOLKIT_SLUG, CUSTOM_MCP_TOOLKIT_SLUG]}},
            )
            custom_toolkits = ((patched.get("experimental") or {}).get("custom_toolkits") or [])
            submit_slug = None
            for toolkit in custom_toolkits:
                for tool in toolkit.get("tools") or []:
                    if tool.get("original_slug") == TOOL["name"] or tool.get("name") == TOOL["name"]:
                        submit_slug = tool.get("slug")
                        break
                if submit_slug:
                    break
            if not submit_slug:
                raise RuntimeError("Composio session exists but PINTEREST_SUBMIT_URL is not exposed")
            _router_session_id = sid
            _router_submit_tool_slug = str(submit_slug)
            _router_session_mcp_url = ((patched.get("mcp") or {}).get("url"))
            return {
                "ready": True,
                "session_id": _router_session_id,
                "tool_slug": _router_submit_tool_slug,
                "mcp_url": _router_session_mcp_url,
                "enabled_toolkits": (patched.get("config") or {}).get("toolkits", {}).get("enabled", []),
            }
        except Exception as exc:
            last_error = str(exc)
            print(f"Composio Tool Router session attempt {attempt} failed: {last_error[:500]}")
            await asyncio.sleep(min(2 ** attempt, 15))
    return {"ready": False, "reason": last_error[:1000] or "Tool Router session creation failed"}


async def composio_router_submit_exact_url(url: str) -> Dict[str, Any]:
    session = await ensure_composio_router_session()
    if not session.get("ready"):
        raise RuntimeError(str(session.get("reason") or "Composio Tool Router session is not ready"))
    value = (url or "").strip()
    if not value.startswith(("http://", "https://")):
        raise ValueError("url must be an http(s) URL")
    return await _composio_request(
        "POST",
        f"/tool_router/session/{_router_session_id}/execute",
        {"tool_slug": _router_submit_tool_slug, "arguments": {"url": value}},
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
                "serverInfo": {"name": "Pinterest Railway Bridge", "version": "1.1.0"},
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
            return _result(request_id, {"content": [{"type": "text", "text": text}], "isError": False})
        except Exception as exc:
            return _result(request_id, {"content": [{"type": "text", "text": str(exc)}], "isError": True})

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
        response = await client.post(f"{COMPOSIO_BASE}/custom/toolkits/upsert", headers=headers, json=payload)
        response.raise_for_status()
        normalized = response.json().get("slug", CUSTOM_MCP_TOOLKIT_SLUG)
        sync = await client.post(f"{COMPOSIO_BASE}/custom/toolkits/sync", headers=headers, json={"slug": normalized})
        sync.raise_for_status()
        return True


async def register_custom_mcp_with_retry() -> bool:
    if not (COMPOSIO_API_KEY and MCP_PATH):
        return False
    for attempt in range(1, 6):
        try:
            if await _register_once():
                print("Composio Custom MCP bridge registered and synced")
                session = await ensure_composio_router_session()
                if session.get("ready"):
                    print("Composio Railway Tool Router session ready with Pinterest bridge")
                else:
                    print(f"Composio Railway Tool Router session dormant: {session.get('reason')}")
                return True
        except Exception as exc:
            print(f"Composio Custom MCP registration attempt {attempt} failed: {type(exc).__name__}")
        await asyncio.sleep(min(2 ** attempt, 15))
    return False
