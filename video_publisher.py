import os,hashlib,requests,json,mimetypes
from pathlib import Path
BASE="https://backend.composio.dev/api/v3.1"
def _api():
    key=os.getenv("COMPOSIO_API_KEY","").strip(); user=os.getenv("COMPOSIO_ENTITY_ID","").strip(); project=os.getenv("WOOP_SOCIAL_PROJECT_ID","pr_2gj5wkt0wr9E").strip()
    if not key or not user: raise RuntimeError("COMPOSIO_API_KEY/COMPOSIO_ENTITY_ID missing")
    return key,user,project
def _stage_file(path,api_key):
    p=Path(path); data=p.read_bytes(); md5=hashlib.md5(data).hexdigest(); mime=mimetypes.guess_type(p.name)[0] or "video/mp4"
    r=requests.post(BASE+"/files/upload/request",headers={"x-api-key":api_key,"Content-Type":"application/json"},json={"toolkit_slug":"woop_social","tool_slug":"WOOP_SOCIAL_UPLOAD_MEDIA","filename":p.name,"mimetype":mime,"md5":md5},timeout=30); r.raise_for_status(); j=r.json()
    url=j.get("new_presigned_url") or j.get("newPresignedUrl"); key=j.get("key")
    if not url or not key: raise RuntimeError(f"Composio upload request returned no presigned URL: {j}")
    u=requests.put(url,data=data,headers={"Content-Type":mime},timeout=120); u.raise_for_status()
    return {"name":p.name,"mimetype":mime,"s3key":key}
def _execute(slug,user,api_key,args):
    r=requests.post(f"{BASE}/tools/execute/{slug}",headers={"x-api-key":api_key,"Content-Type":"application/json"},json={"user_id":user,"version":"latest","arguments":args},timeout=180); r.raise_for_status(); j=r.json()
    if j.get("successful") is False: raise RuntimeError(j.get("error") or str(j))
    return j.get("data") or j
def publish_video(path,title,text,platforms=None):
    api_key,user,project=_api(); staged=_stage_file(path,api_key)
    media=_execute("WOOP_SOCIAL_UPLOAD_MEDIA",user,api_key,{"project_id":project,"file":staged}); media_id=(media.get("media_id") if isinstance(media,dict) else None)
    if not media_id: raise RuntimeError(f"WoopSocial media upload returned no media_id: {media}")
    accounts=_execute("WOOP_SOCIAL_LIST_SOCIAL_ACCOUNTS",user,api_key,{})
    rows=(accounts.get("social_accounts") if isinstance(accounts,dict) else None) or []
    by={str(x.get("platform") or "").upper():x for x in rows if str(x.get("status") or "").upper()=="CONNECTED"}
    targets=[]
    allowed={str(x).lower() for x in (platforms or ["facebook","instagram","x","youtube","tiktok"])}
    if "facebook" in allowed and by.get("FACEBOOK"): targets.append({"social_account_id":by["FACEBOOK"]["id"],"platform":"FACEBOOK","post_type":"VIDEO"})
    if "instagram" in allowed and by.get("INSTAGRAM"): targets.append({"social_account_id":by["INSTAGRAM"]["id"],"platform":"INSTAGRAM","post_type":"REEL"})
    if "x" in allowed and by.get("X"): targets.append({"social_account_id":by["X"]["id"],"platform":"X"})
    if "youtube" in allowed and by.get("YOUTUBE"): targets.append({"social_account_id":by["YOUTUBE"]["id"],"platform":"YOUTUBE","title":title,"privacy":"public","tags":["amazon","productfinds","shopping","deals"]})
    if "tiktok" in allowed and by.get("TIKTOK"): targets.append({"social_account_id":by["TIKTOK"]["id"],"platform":"TIKTOK","post_mode":"MEDIA_UPLOAD","post_type":"VIDEO","allow_duet":False,"allow_stitch":False,"allow_comment":True,"is_your_brand":False,"is_branded_content":False,"auto_add_music":False,"privacy_level":"PUBLIC_TO_EVERYONE","is_ai_generated_content":False})
    if not targets: raise RuntimeError("No connected WoopSocial target accounts found")
    result=_execute("WOOP_SOCIAL_PUBLISH_POST_NOW",user,api_key,{"content":[{"text":text,"media":[{"type":"MEDIA_LIBRARY","media_id":media_id}]}],"social_accounts":targets,"auto_delete_media_after_publish":False})
    statuses={}
    for d in (result.get("social_account_posts") if isinstance(result,dict) else None) or []:
        statuses[str(d.get("platform") or "").lower()]={"status":d.get("delivery_status"),"external_post_id":d.get("external_post_id"),"url":d.get("external_post_url"),"error":d.get("error_message")}
    return {"status":"submitted","media_id":media_id,"platforms":statuses,"raw":result}
