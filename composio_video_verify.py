from __future__ import annotations
import json, os, sqlite3, requests
from pathlib import Path
BASE="https://backend.composio.dev/api/v3.1"
def _cfg(): return (os.getenv("COMPOSIO_API_KEY","").strip(),os.getenv("COMPOSIO_ENTITY_ID","").strip(),os.getenv("WOOP_SOCIAL_PROJECT_ID","").strip())
def _execute(slug,args):
    key,user,_=_cfg()
    if not key or not user:return {"available":False,"reason":"composio_credentials_missing"}
    r=requests.post(f"{BASE}/tools/execute/{slug}",headers={"x-api-key":key,"Content-Type":"application/json"},json={"user_id":user,"version":"latest","arguments":args},timeout=60); r.raise_for_status(); j=r.json()
    if j.get("successful") is False:return {"available":False,"reason":j.get("error") or "composio_tool_failed"}
    return {"available":True,"data":j.get("data") or j}
def _text(post):return "\n".join(str(x.get("text") or "") for x in (post.get("content") or []) if isinstance(x,dict))
def _expected(run_id):
    db=Path(os.getenv("JOB_DB_PATH", "/data/pinterest_agent_jobs.db"))
    if not db.exists():return {}
    c=sqlite3.connect(str(db)); rows=c.execute("SELECT asin,platform_results_json FROM kaggle_video_jobs WHERE run_id=?",(run_id,)).fetchall()
    if not rows:
        rows=c.execute("SELECT asin,publication_json FROM video_jobs WHERE claim_owner=?",(f"kaggle-{run_id}",)).fetchall()
    c.close(); out={}
    for asin,raw in rows:
        try:out[str(asin).upper()]=json.loads(raw or "{}")
        except Exception:out[str(asin).upper()]={}
    return out

def verify_run(run_id,project=None):
    project=project or _cfg()[2]
    if not project:return {"available":False,"complete":False,"reason":"woop_social_project_missing","products":{}}
    expected=_expected(run_id); found=[]; cursor=None
    for _ in range(10):
        args={"limit":100,"project_ids":[project],"delivery_statuses":["NOT_STARTED","SENDING","PUBLISHED","FAILED"]}
        if cursor:args["next_cursor"]=cursor
        res=_execute("WOOP_SOCIAL_LIST_SOCIAL_ACCOUNT_POSTS",args)
        if not res.get("available"):return {**res,"complete":False,"products":{}}
        data=res.get("data") or {}; page=data.get("data") if isinstance(data,dict) else None
        if isinstance(page,dict):items=page.get("items") or []; cursor=page.get("next_cursor")
        else:items=data.get("items") or []; cursor=data.get("next_cursor") if isinstance(data,dict) else None
        for p in items:
            text=_text(p); ext=str(p.get("external_post_id") or ""); matched_marker=f"[video-run:{run_id}]" in text; matched_external=False
            if ext:
                for raw in expected.values():
                    vals=raw.get("platforms") if isinstance(raw,dict) else raw
                    if isinstance(vals,dict) and any(str(v.get("external_post_id") or "") == ext for v in vals.values() if isinstance(v,dict)):matched_external=True; break
            if matched_marker or matched_external:found.append(p)
        if not cursor:break
    products={}
    for p in found:
        text=_text(p); asin=""
        if "[asin:" in text:asin=text.split("[asin:",1)[1].split("]",1)[0].strip().upper()
        if not asin and p.get("external_post_id"):
            ext=str(p.get("external_post_id"))
            for candidate,raw in expected.items():
                vals=raw.get("platforms") if isinstance(raw,dict) else raw
                if isinstance(vals,dict) and any(str(v.get("external_post_id") or "") == ext for v in vals.values() if isinstance(v,dict)):asin=candidate; break
        if not asin:continue
        products.setdefault(asin,{"platforms":{}})["platforms"][str(p.get("platform") or "").lower()]={"status":p.get("delivery_status"),"external_post_id":p.get("external_post_id"),"url":p.get("external_post_url"),"error":p.get("error_message")}
    complete=bool(expected) and all(asin in products and products[asin].get("platforms") and all(str(v.get("status") or "").upper()=="PUBLISHED" for v in products[asin]["platforms"].values()) for asin in expected)
    return {"available":True,"complete":complete,"products":products,"matched_deliveries":len(found)}
