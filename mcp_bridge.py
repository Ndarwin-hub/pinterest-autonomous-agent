"""Remote MCP bridge for the Pinterest Railway intake.

This is deliberately a very small adapter: AI clients discover one tool,
PINTEREST_SUBMIT_URL, through a Composio Custom MCP toolkit. The tool sends
only the exact URL to the existing /submit intake, so the established
Pinterest workflow remains the execution engine.
"""
import asyncio
import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

COMPOSIO_API_KEY = os.getenv("COMPOSIO_API_KEY", "").strip()
API_SECRET = os.getenv("API_SECRET", "").strip()
BRIDGE_TOKEN = os.getenv("MCP_BRIDGE_TOKEN", "").strip()
PUBLIC_DOMAIN = os.getenv("RAILWAY_PUBLIC_DOMAIN", "web-production-dae68.up.railway.app").strip()
SUBMIT_URL = f"https://{PUBLIC_DOMAIN}/submit"
MCP_TOOLKIT_SLUG = "PINTEREST_RAILWAY_BRIDGE"
MCP_PATH = f"/mcp/{BRIDGE_TOKEN}" if BRIDGE_TOKEN else ""

# The current Railway service has two public domains. Include both so either
# remains valid if Railway selects the other hostname in a proxy request.
ALLOWED_HOSTS = {
    PUBLIC_DOMAIN,
    "web-production-dae68.up.railway.app",
    "web-production-24057.up.railway.app",
}
ALLOWED_HOSTS_WITH_PORTS = sorted(ALLOWED_HOSTS | {f"{h}:*" for h in ALLOWED_HOSTS})

mcp = FastMCP(
    "Pinterest Railway Bridge",
    instructions=(
        "Use PINTEREST_SUBMIT_URL when the user provides a product or affiliate URL. "
        "Pass the exact URL unchanged. Do not modify, shorten, or replace it."
    ),
)


@mcp.tool(name="PINTEREST_SUBMIT_URL")
async def pinterest_submit_url(url: str) -> str:
    """Submit one exact product/affiliate URL to the autonomous Pinterest workflow.

    The tool accepts only the URL. It does not publish Pins itself; Railway's
    existing queue, board routing, image system, reviewer gate, retries,
    idempotency, and final verification remain responsible for execution.
    """
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


def build_mcp_app():
    if not MCP_PATH:
        return None
    security = TransportSecuritySettings(
        allowed_hosts=ALLOWED_HOSTS_WITH_PORTS,
        enable_dns_rebinding_protection=True,
    )
    return mcp.streamable_http_app(
        streamable_http_path="/",
        json_response=True,
        stateless_http=True,
        transport_security=security,
        host="0.0.0.0",
    )


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
    """Register/sync the bridge without delaying Railway application startup."""
    if not (COMPOSIO_API_KEY and MCP_PATH):
        return False
    for attempt in range(1, 6):
        try:
            ok = await _register_once()
            if ok:
                return True
        except Exception as exc:
            print(f"Composio Custom MCP registration attempt {attempt} failed: {exc}")
        await asyncio.sleep(min(2 ** attempt, 15))
    return False
