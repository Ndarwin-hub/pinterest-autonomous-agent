"""Minimal dependency-free MCP/JSON-RPC bridge for Composio Custom MCP."""
import asyncio,hashlib,os,json
from typing import Any,Dict,Optional
import httpx
from fastapi import APIRouter,Request
from fastapi.responses import JSONResponse,Response

COMPOSIO_API_KEY=os.getenv("COMPOSIO_API_KEY","").strip();COMPOSIO_ENTITY_ID=os.getenv("COMPOSIO_ENTITY_ID","").strip();API_SECRET=os.getenv("API_SECRET","").strip();BRIDGE_TOKEN=os.getenv("MCP_BRIDGE_TOKEN","").strip();PUBLIC_DOMAIN=os.getenv("RAILWAY_PUBLIC_DOMAIN","web-production-dae68.up.railway.app").strip();SUBMIT_URL=f"https://{PUBLIC_DOMAIN}/submit";DISCOVER_URL=f"https://{PUBLIC_DOMAIN}/amazon/discover-submit";MCP_TOOLKIT_SLUG="PINTEREST_RAILWAY_BRIDGE";CUSTOM_MCP_TOOLKIT_SLUG="CUSTOM_PINTEREST_RAILWAY_BRIDGE";COMPOSIO_SEARCH_TOOLKIT_SLUG="composio_search";COMPOSIO_BASE="https://backend.composio.dev/api/v3.1";MCP_PATH=f"/mcp/{BRIDGE_TOKEN}" if BRIDGE_TOKEN else "";SMOKE_TEST_URL=os.getenv("COMPOSIO_BRIDGE_SMOKE_TEST_URL","").strip();SMOKE_MARKER="/data/composio_bridge_smoke_test_v2.sha256"
router=APIRouter();_router_session_id:Optional[str]=None;_router_submit_tool_slug:Optional[str]=None;_router_amazon_tool_slug:Optional[str]=None;_router_session_mcp_url:Optional[str]=None
TOOL={"name":"PINTEREST_SUBMIT_URL","description":"Submit one exact product/affiliate URL to the autonomous Pinterest workflow. Pass the URL unchanged; do not shorten, rewrite, or replace it.","inputSchema":{"type":"object","properties":{"url":{"type":"string","description":"Exact http(s) product or affiliate URL."}},"required":["url"],"additionalProperties":False}}
COUNT_TOOL={"name":"PINTEREST_PIN_COUNT","description":"Search, verify and publish N distinct Amazon US products through the Railway Pinterest workflow. N is the number of products, not the number of Pins. Each product uses the existing independent Pin research/image/publish/verification pipeline; publish every usable verified Pin and do not block the batch merely because fewer than five usable Pins are available for a product. Preserve desiredplus-20, reject duplicate ASINs, and use the configured image-quality priority.","inputSchema":{"type":"object","properties":{"count":{"type":"integer","minimum":1,"maximum":50,"description":"Number of distinct Amazon US products to search, verify and publish."}},"required":["count"],"additionalProperties":False}}

def _result(request_id:Any,result:Dict[str,Any])->JSONResponse:return JSONResponse({"jsonrpc":"2.0","id":request_id,"result":result})
def _error(request_id:Any,code:int,message:str)->JSONResponse:return JSONResponse({"jsonrpc":"2.0","id":request_id,"error":{"code":code,"message":message}})
def _headers()->Dict[str,str]:return {"X-API-Secret":API_SECRET} if API_SECRET else {}
async def _post_json(url:str,payload:Dict[str,Any],timeout:float=60.0)->Dict[str,Any]:
 async with httpx.AsyncClient(timeout=timeout,follow_redirects=False) as client:
  response=await client.post(url,headers={**_headers(),"Content-Type":"application/json"},json=payload)
 if response.status_code>=400:raise RuntimeError(f"Railway endpoint returned HTTP {response.status_code}: {response.text[:1000]}")
 return response.json()
async def _submit_exact_url(url:str)->str:
 value=(url or "").strip()
 if not value.startswith(("http://","https://")):raise ValueError("url must be an http(s) URL")
 data=await _post_json(SUBMIT_URL,{"url":value},timeout=30)
 return f"Railway accepted the exact URL. job_id={data.get('job_id')}; status={data.get('status')}; message={data.get('message')}"
async def _pin_count(count:int)->Dict[str,Any]:
 n=int(count)
 if n<1 or n>50:raise ValueError("count must be between 1 and 50")
 launch=await _post_json(DISCOVER_URL,{"count":n},timeout=90)
 results=launch.get("results") or []
 job_ids=[str(x.get("job_id")) for x in results if x.get("job_id")]
 pending=set(job_ids);finals={}
 async with httpx.AsyncClient(timeout=30,follow_redirects=False) as client:
  for _ in range(180):
   if not pending:break
   for job_id in list(pending):
    try:
     r=await client.get(f"https://{PUBLIC_DOMAIN}/status/{job_id}",headers=_headers())
     if r.status_code>=400:continue
     data=r.json();status=str(data.get("status") or "")
     if status in ("completed","completed_partial","failed"):
      finals[job_id]=data;pending.discard(job_id)
    except Exception:continue
   if pending:await asyncio.sleep(10)
 summary={"requested":n,"discovered":int(launch.get("discovered") or 0),"accepted":int(launch.get("accepted") or 0),"skipped":int(launch.get("skipped") or 0),"rejected":int(launch.get("rejected") or 0),"jobs":len(job_ids),"completed":0,"completed_partial":0,"failed":0,"pins_published":0,"verified_pins":0,"pending_jobs":len(pending),"products":[]}
 for job_id,data in finals.items():
  st=str(data.get("status") or "failed");summary[st]=int(summary.get(st,0))+1
  result=data.get("result") or {};pins=result.get("pins") or []
  verified=sum(1 for p in pins if isinstance(p,dict) and p.get("verified") and p.get("pin_id"))
  summary["pins_published"]+=int(result.get("pins_published") or len(pins))
  summary["verified_pins"]+=verified
  summary["products"].append({"job_id":job_id,"status":st,"asin":result.get("asin") or result.get("product_asin"),"pins_published":int(result.get("pins_published") or len(pins)),"verified_pins":verified,"pin_ids":[str(p.get("pin_id")) for p in pins if isinstance(p,dict) and p.get("pin_id")]})
 return summary
async def _composio_request(method:str,path:str,body:Optional[Dict[str,Any]]=None)->Dict[str,Any]:
 if not COMPOSIO_API_KEY:raise RuntimeError("Composio API key is not configured")
 headers={"x-api-key":COMPOSIO_API_KEY,"Content-Type":"application/json"}
 async with httpx.AsyncClient(timeout=45.0) as client:
  response=await client.request(method,f"{COMPOSIO_BASE}{path}",headers=headers,json=body)
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
   search=await _composio_request("POST",f"/tool_router/session/{sid}/search",{"queries":[{"use_case":"execute Pinterest Railway tools PINTEREST_SUBMIT_URL or PINTEREST_PIN_COUNT"}],"search_strategy":"tool_search"})
   submit_slug=None
   for result in search.get("results") or []:
    for slug in (result.get("primary_tool_slugs") or [])+(result.get("related_tool_slugs") or []):
     if str(slug).upper().endswith("PINTEREST_SUBMIT_URL"):submit_slug=str(slug);break
    if submit_slug:break
   if not submit_slug:
    for slug,schema in (search.get("tool_schemas") or {}).items():
     if str(slug).upper().endswith("PINTEREST_SUBMIT_URL") or "exact product/affiliate URL" in str(schema.get("description","")):submit_slug=str(slug);break
   if not submit_slug:raise RuntimeError("Composio session search did not expose PINTEREST_SUBMIT_URL")
   _router_session_id=sid;_router_submit_tool_slug=submit_slug;_router_session_mcp_url=(session.get("mcp") or {}).get("url")
   print(f"Composio Railway Tool Router session ready; Pinterest bridge tool discovered as {_router_submit_tool_slug}")
   return {"ready":True,"session_id":sid,"tool_slug":submit_slug,"mcp_url":_router_session_mcp_url}
  except Exception as exc:
   last_error=str(exc);print(f"Composio Tool Router session attempt {attempt} failed: {last_error[:500]}");await asyncio.sleep(min(2**attempt,15))
 return {"ready":False,"reason":last_error[:1000] or "Tool Router session creation failed"}
async def composio_router_search_amazon(query:str,amazon_domain:str="amazon.com",page:int=1)->Dict[str,Any]:
 global _router_amazon_tool_slug
 session=await ensure_composio_router_session()
 if not session.get("ready"):raise RuntimeError(str(session.get("reason") or "Composio Tool Router session is not ready"))
 if not _router_amazon_tool_slug:
  search=await _composio_request("POST",f"/tool_router/session/{_router_session_id}/search",{"queries":[{"use_case":"search Amazon products and return buyable product detail results from the requested Amazon marketplace","known_fields":f"amazon_domain:{amazon_domain}"}],"search_strategy":"tool_search"})
  for result in search.get("results") or []:
   for slug in (result.get("primary_tool_slugs") or [])+(result.get("related_tool_slugs") or []):
    if str(slug).upper()=="COMPOSIO_SEARCH_AMAZON":_router_amazon_tool_slug=str(slug);break
   if _router_amazon_tool_slug:break
  if not _router_amazon_tool_slug:
   for slug in (search.get("tool_schemas") or {}):
    if str(slug).upper()=="COMPOSIO_SEARCH_AMAZON":_router_amazon_tool_slug=str(slug);break
  if not _router_amazon_tool_slug:raise RuntimeError("Composio Tool Router did not expose COMPOSIO_SEARCH_AMAZON")
 return await _composio_request("POST",f"/tool_router/session/{_router_session_id}/execute",{"tool_slug":_router_amazon_tool_slug,"arguments":{"query":query,"amazon_domain":amazon_domain,"page":page}})

async def composio_router_submit_exact_url(url:str)->Dict[str,Any]:
 session=await ensure_composio_router_session()
 if not session.get("ready"):raise RuntimeError(str(session.get("reason") or "Composio Tool Router session is not ready"))
 value=(url or "").strip()
 if not value.startswith(("http://","https://")):raise ValueError("url must be an http(s) URL")
 return await _composio_request("POST",f"/tool_router/session/{_router_session_id}/execute",{"tool_slug":_router_submit_tool_slug,"arguments":{"url":value}})
async def run_one_shot_smoke_test()->None:
 if not SMOKE_TEST_URL:return
 marker=hashlib.sha256(SMOKE_TEST_URL.encode()).hexdigest()
 try:
  if os.path.exists(SMOKE_MARKER) and open(SMOKE_MARKER).read().strip()==marker:
   print("Composio bridge smoke test already completed for configured URL");return
  result=await composio_router_submit_exact_url(SMOKE_TEST_URL)
  if result.get("error"):raise RuntimeError(str(result.get("error")))
  print(f"Composio bridge smoke test executed successfully: {result}")
  os.makedirs(os.path.dirname(SMOKE_MARKER),exist_ok=True)
  with open(SMOKE_MARKER,"w") as f:f.write(marker)
 except Exception as exc:print(f"Composio bridge smoke test failed: {type(exc).__name__}: {str(exc)[:500]}")
@router.post("/")
async def mcp_endpoint(request:Request):
 try:body=await request.json()
 except Exception:return JSONResponse({"error":"Invalid JSON"},status_code=400)
 request_id=body.get("id");method=body.get("method");params=body.get("params") or {}
 if request_id is None:
  if method in {"notifications/initialized","notifications/cancelled"}:return Response(status_code=202)
  if method=="ping":return Response(status_code=202)
 if method=="initialize":return _result(request_id,{"protocolVersion":params.get("protocolVersion") or "2025-06-18","capabilities":{"tools":{}},"serverInfo":{"name":"Pinterest Railway Bridge","version":"1.2.0"},"instructions":"Use PINTEREST_SUBMIT_URL for exact URLs or PINTEREST_PIN_COUNT for Pin N product batches."})
 if method=="ping":return _result(request_id,{})
 if method=="tools/list":return _result(request_id,{"tools":[TOOL,COUNT_TOOL]})
 if method=="tools/call":
  name=params.get("name");args=params.get("arguments") or {}
  try:
   if name==TOOL["name"]:text=await _submit_exact_url(args.get("url",""))
   elif name==COUNT_TOOL["name"]:text=json.dumps(await _pin_count(int(args.get("count",0))),separators=(",",":"))
   else:return _error(request_id,-32601,f"Unknown tool: {name}")
   return _result(request_id,{"content":[{"type":"text","text":text}],"isError":False})
  except Exception as exc:return _result(request_id,{"content":[{"type":"text","text":str(exc)}],"isError":True})
 return _error(request_id,-32601,f"Unsupported MCP method: {method}")
async def _register_once()->bool:
 if not(COMPOSIO_API_KEY and MCP_PATH):return False
 app_url=f"https://{PUBLIC_DOMAIN}{MCP_PATH}/";headers={"x-api-key":COMPOSIO_API_KEY,"Content-Type":"application/json"};payload={"slug":MCP_TOOLKIT_SLUG,"toolkit_config":{"name":"Pinterest Railway Bridge","app_url":app_url,"auth_schemes":[{"mode":"NO_AUTH"}]}}
 async with httpx.AsyncClient(timeout=30.0) as client:
  response=await client.post(f"{COMPOSIO_BASE}/custom/toolkits/upsert",headers=headers,json=payload)
  if response.status_code==409:
   old_slug=CUSTOM_MCP_TOOLKIT_SLUG
   delete_response=await client.delete(f"{COMPOSIO_BASE}/custom/toolkits/{old_slug}",headers=headers)
   if delete_response.status_code not in (200,404):delete_response.raise_for_status()
   response=await client.post(f"{COMPOSIO_BASE}/custom/toolkits/upsert",headers=headers,json=payload)
  response.raise_for_status()
  normalized=response.json().get("slug") or CUSTOM_MCP_TOOLKIT_SLUG
  sync=await client.post(f"{COMPOSIO_BASE}/custom/toolkits/sync",headers=headers,json={"slug":normalized})
  sync.raise_for_status()
  return True
async def register_custom_mcp_with_retry()->bool:
 if not(COMPOSIO_API_KEY and MCP_PATH):return False
 for attempt in range(1,6):
  try:
   if await _register_once():
    print("Composio Custom MCP bridge registered and synced");session=await ensure_composio_router_session()
    if session.get("ready"):print("Composio Railway Tool Router session ready with Pinterest bridge");await run_one_shot_smoke_test()
    else:print(f"Composio Railway Tool Router session dormant: {session.get('reason')}")
    return True
  except Exception as exc:print(f"Composio Custom MCP registration attempt {attempt} failed: {type(exc).__name__}")
  await asyncio.sleep(min(2**attempt,15))
 return False
