"""Minimal dependency-free MCP/JSON-RPC bridge for Composio Custom MCP."""
import asyncio
import os
from typing import Any, Dict, Optional
import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

COMPOSIO_API_KEY=os.getenv("COMPOSIO_API_KEY","").strip(); COMPOSIO_ENTITY_ID=os.getenv("COMPOSIO_ENTITY_ID","").strip(); API_SECRET=os.getenv("API_SECRET","").strip(); BRIDGE_TOKEN=os.getenv("MCP_BRIDGE_TOKEN","").strip(); PUBLIC_DOMAIN=os.getenv("RAILWAY_PUBLIC_DOMAIN","web-production-dae68.up.railway.app").strip(); SUBMIT_URL=f"https://{PUBLIC_DOMAIN}/submit"; MCP_TOOLKIT_SLUG="PINTEREST_RAILWAY_BRIDGE"; CUSTOM_MCP_TOOLKIT_SLUG="CUSTOM_PINTEREST_RAILWAY_BRIDGE"; COMPOSIO_SEARCH_TOOLKIT_SLUG="composio_search"; COMPOSIO_BASE="https://backend.composio.dev/api/v3.1"; MCP_PATH=f"/mcp/{BRIDGE_TOKEN}" if BRIDGE_TOKEN else ""
router=APIRouter(); _router_session_id:Optional[str]=None; _router_submit_tool_slug:Optional[str]=None; _router_session_mcp_url:Optional[str]=None
TOOL={"name":"PINTEREST_SUBMIT_URL","description":"Submit one exact product/affiliate URL to the autonomous Pinterest workflow. Pass the URL unchanged; do not shorten, rewrite, or replace it.","inputSchema":{"type":"object","properties":{"url":{"type":"string","description":"Exact http(s) product or affiliate URL."}},"required":["url"],"additionalProperties":False}}

def _result(request_id:Any,result:Dict[str,Any])->JSONResponse:return JSONResponse({"jsonrpc":"2.0","id":request_id,"result":result})
def _error(request_id:Any,code:int,message:str)->JSONResponse:return JSONResponse({"jsonrpc":"2.0","id":request_id,"error":{"code":code,"message":message}})

async def _submit_exact_url(url:str)->str:
 if not API_SECRET:raise RuntimeError("Railway API secret is not configured")
 value=(url or "").strip()
 if not value.startswith(("http://","https://")):raise ValueError("url must be an http(s) URL")
 async with httpx.AsyncClient(timeout=30.0,follow_redirects=False) as client:response=await client.post(SUBMIT_URL,headers={"X-API-Secret":API_SECRET},json={"url":value})
 if response.status_code>=400:raise RuntimeError(f"Railway /submit returned HTTP {response.status_code}: {response.text[:500]}")
 data=response.json();return f"Railway accepted the exact URL. job_id={data.get('job_id')}; status={data.get('status')}; message={data.get('message')}"

async def _composio_request(method:str,path:str,body:Optional[Dict[str,Any]]=None)->Dict[str,Any]:
 if not COMPOSIO_API_KEY:raise RuntimeError("Composio API key is not configured")
 headers={"x-api-key":COMPOSIO_API_KEY,"Content-Type":"application/json"}
 async with httpx.AsyncClient(timeout=45.0) as client:response=await client.request(method,f"{COMPOSIO_BASE}{path}",headers=headers,json=body)
 if response.status_code>=400:raise RuntimeError(f"Composio API HTTP {response.status_code}: {response.text[:1000].replace(chr(10),' ')}")
 return response.json()

async def ensure_composio_router_session()->Dict[str,Any]:
 global _router_session_id,_router_submit_tool_slug,_router_session_mcp_url
 if not(COMPOSIO_API_KEY and COMPOSIO_ENTITY_ID):return {"ready":False,"reason":"Composio credentials are not configured"}
 if _router_session_id and _router_submit_tool_slug:return {"ready":True,"session_id":_router_session_id,"tool_slug":_router_submit_tool_slug,"mcp_url":_router_session_mcp_url}
 last_error=""
 for attempt in range(1,6):
  try:
   session=await _composio_request("POST","/tool_router/session",{"user_id":COMPOSIO_ENTITY_ID,"toolkits":{"enable":[COMPOSIO_SEARCH_TOOLKIT_SLUG,CUSTOM_MCP_TOOLKIT_SLUG]}});sid=str(session.get("session_id") or "")
   if not sid:raise RuntimeError("Composio created a session without a session_id")
   # Search inside this exact Railway-owned session. This is authoritative for
   # what the session can actually execute and avoids guessing custom slugs.
   search=await _composio_request("POST",f"/tool_router/session/{sid}/search",{"queries":[{"use_case":"execute the Pinterest Railway bridge tool PINTEREST_SUBMIT_URL to submit one exact product affiliate URL"}],"search_strategy":"tool_search"})
   submit_slug=None
   for result in search.get("results") or []:
    for slug in (result.get("primary_tool_slugs") or [])+(result.get("related_tool_slugs") or []):
     if str(slug).upper().endswith("PINTEREST_SUBMIT_URL"):
      submit_slug=str(slug);break
    if submit_slug:break
   if not submit_slug:
    # Also inspect returned schemas, which may contain the custom tool even when
    # it is not selected as a primary recommendation.
    for slug,schema in (search.get("tool_schemas") or {}).items():
     if str(slug).upper().endswith("PINTEREST_SUBMIT_URL") or str(schema.get("description","")).find("exact product/affiliate URL")>=0:
      submit_slug=str(slug);break
   if not submit_slug:raise RuntimeError("Composio session search did not expose PINTEREST_SUBMIT_URL")
   _router_session_id=sid;_router_submit_tool_slug=submit_slug;_router_session_mcp_url=(session.get("mcp") or {}).get("url")
   print(f"Composio Railway Tool Router session ready; Pinterest bridge tool discovered as {_router_submit_tool_slug}")
   return {"ready":True,"session_id":sid,"tool_slug":submit_slug,"mcp_url":_router_session_mcp_url}
  except Exception as exc:
   last_error=str(exc);print(f"Composio Tool Router session attempt {attempt} failed: {last_error[:500]}");await asyncio.sleep(min(2**attempt,15))
 return {"ready":False,"reason":last_error[:1000] or "Tool Router session creation failed"}

async def composio_router_submit_exact_url(url:str)->Dict[str,Any]:
 session=await ensure_composio_router_session()
 if not session.get("ready"):raise RuntimeError(str(session.get("reason") or "Composio Tool Router session is not ready"))
 value=(url or "").strip()
 if not value.startswith(("http://","https://")):raise ValueError("url must be an http(s) URL")
 return await _composio_request("POST",f"/tool_router/session/{_router_session_id}/execute",{"tool_slug":_router_submit_tool_slug,"arguments":{"url":value}})

@router.post("/")
async def mcp_endpoint(request:Request):
 try:body=await request.json()
 except Exception:return JSONResponse({"error":"Invalid JSON"},status_code=400)
 request_id=body.get("id");method=body.get("method");params=body.get("params") or {}
 if request_id is None:
  if method in {"notifications/initialized","notifications/cancelled"}:return Response(status_code=202)
  if method=="ping":return Response(status_code=202)
 if method=="initialize":return _result(request_id,{"protocolVersion":params.get("protocolVersion") or "2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"Pinterest Railway Bridge","version":"1.1.0"},"instructions":"Use PINTEREST_SUBMIT_URL for exact product/affiliate URLs."})
 if method=="ping":return _result(request_id,{})
 if method=="tools/list":return _result(request_id,{"tools":[TOOL]})
 if method=="tools/call":
  if params.get("name")!=TOOL["name"]:return _error(request_id,-32601,f"Unknown tool: {params.get('name')}")
  try:return _result(request_id,{"content":[{"type":"text","text":await _submit_exact_url((params.get("arguments") or {}).get("url",""))}],"isError":False})
  except Exception as exc:return _result(request_id,{"content":[{"type":"text","text":str(exc)}],"isError":True})
 return _error(request_id,-32601,f"Unsupported MCP method: {method}")

async def _register_once()->bool:
 if not(COMPOSIO_API_KEY and MCP_PATH):return False
 app_url=f"https://{PUBLIC_DOMAIN}{MCP_PATH}/";headers={"x-api-key":COMPOSIO_API_KEY,"Content-Type":"application/json"};payload={"slug":MCP_TOOLKIT_SLUG,"toolkit_config":{"name":"Pinterest Railway Bridge","app_url":app_url,"auth_schemes":[{"mode":"NO_AUTH"}]}}
 async with httpx.AsyncClient(timeout=30.0) as client:
  response=await client.post(f"{COMPOSIO_BASE}/custom/toolkits/upsert",headers=headers,json=payload);response.raise_for_status();normalized=response.json().get("slug",CUSTOM_MCP_TOOLKIT_SLUG);sync=await client.post(f"{COMPOSIO_BASE}/custom/toolkits/sync",headers=headers,json={"slug":normalized});sync.raise_for_status();return True

async def register_custom_mcp_with_retry()->bool:
 if not(COMPOSIO_API_KEY and MCP_PATH):return False
 for attempt in range(1,6):
  try:
   if await _register_once():
    print("Composio Custom MCP bridge registered and synced");session=await ensure_composio_router_session();print("Composio Railway Tool Router session ready with Pinterest bridge" if session.get("ready") else f"Composio Railway Tool Router session dormant: {session.get('reason')}");return True
  except Exception as exc:print(f"Composio Custom MCP registration attempt {attempt} failed: {type(exc).__name__}")
  await asyncio.sleep(min(2**attempt,15))
 return False
