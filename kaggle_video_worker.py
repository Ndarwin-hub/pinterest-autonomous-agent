import os, json, time, hashlib, mimetypes, subprocess, shutil, requests
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NPT = ZoneInfo("Asia/Kathmandu")
WINDOW_START = 6*60
WINDOW_END = 14*60
BATCHES = range(1, 11)
MAX_VIDEOS = 50
POLL_SECONDS = 45
RAILWAY = os.getenv("RAILWAY_VIDEO_BASE_URL", "https://web-production-dae68.up.railway.app").rstrip("/")
WORK = Path("/kaggle/working/video_worker")
WORK.mkdir(parents=True, exist_ok=True)

def secret(name):
    v = os.getenv(name, "").strip()
    if v: return v
    try:
        from kaggle_secrets import UserSecretsClient
        v = UserSecretsClient().get_secret(name)
        if v: return v.strip()
    except Exception: pass
    return ""

VIDEO_SECRET = secret("VIDEO_BRIDGE_SECRET")
COMPOSIO_KEY = secret("COMPOSIO_API_KEY")
COMPOSIO_ENTITY = secret("COMPOSIO_ENTITY_ID")
WOOP_PROJECT = secret("WOOP_SOCIAL_PROJECT_ID") or "pr_2gj5wkt0wr9E"

def npt_minutes():
    now = datetime.now(timezone.utc).astimezone(NPT)
    return now.hour*60 + now.minute, now

def in_window():
    m,_ = npt_minutes()
    return WINDOW_START <= m < WINDOW_END

def wait_until_window():
    while True:
        m, now = npt_minutes()
        if m >= WINDOW_END: return False
        if m >= WINDOW_START: return True
        time.sleep(min(60, max(5, (WINDOW_START-m)*60)))

def api(method, path, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["X-Video-Secret"] = VIDEO_SECRET
    r = requests.request(method, RAILWAY + path, headers=headers, timeout=60, **kwargs)
    if r.status_code >= 400: raise RuntimeError(f"RAILWAY_{r.status_code}:{r.text[:1000]}")
    return r.json()

def list_batch(batch):
    return api("POST", "/video/batch", json={"batch": batch, "fallback": False}).get("jobs") or []

def claim(batch, slot):
    return api("POST", f"/video/job/{batch}/{slot}/claim")

def set_status(batch, slot, status, owner, error=None, publication=None):
    params = {"status": status, "owner": owner}
    if error: params["error"] = str(error)[:1000]
    if publication is not None: params["publication_json"] = json.dumps(publication, separators=(",",":"))[:12000]
    return api("POST", f"/video/job/{batch}/{slot}/status", params=params)

def download_image(url, path):
    r = requests.get(url, timeout=45, headers={"User-Agent":"Mozilla/5.0"})
    r.raise_for_status()
    if len(r.content) < 1024: raise RuntimeError("IMAGE_TOO_SMALL")
    path.write_bytes(r.content)

def render_video(row, out):
    urls = [u for u in json.loads(row.get("image_urls_json") or "[]") if isinstance(u,str) and u.startswith(("http://","https://"))][:4]
    if len(urls) < 4: raise RuntimeError("VIDEO_IMAGE_HANDOFF_MISSING")
    folder = out.parent / "frames"; folder.mkdir(parents=True, exist_ok=True)
    files=[]
    for i,u in enumerate(urls):
        p=folder/f"img{i}.jpg"; download_image(u,p); files.append(p)
    title=(row.get("title") or row.get("asin") or "Amazon Product")[:80]
    safe=title.replace("\\","\\\\").replace(":","\\:").replace("'","\\'")
    inputs=[]
    for p in files: inputs += ["-loop","1","-t","3","-i",str(p)]
    filters=[f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1[v{i}]" for i in range(4)]
    filters.append("[v0][v1][v2][v3]concat=n=4:v=1:a=0[v]")
    filters.append(f"[v]drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='{safe}':x=(w-text_w)/2:y=120:fontsize=54:fontcolor=white:borderw=3:bordercolor=black@0.8,drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='SHOP NOW':x=(w-text_w)/2:y=1770:fontsize=64:fontcolor=white:borderw=3:bordercolor=black@0.8[outv]")
    cmd=["ffmpeg","-y",*inputs,"-filter_complex",";".join(filters),"-map","[outv]","-an","-c:v","libx264","-preset","veryfast","-crf","23","-pix_fmt","yuv420p","-movflags","+faststart",str(out)]
    p=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=300)
    if p.returncode: raise RuntimeError("FFMPEG_FAILED:"+p.stderr[-1200:])
    if not out.exists() or out.stat().st_size < 10000: raise RuntimeError("VIDEO_OUTPUT_INVALID")
    q=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(out)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=30)
    if q.returncode or float(q.stdout.strip() or 0) < 10: raise RuntimeError("VIDEO_VALIDATION_FAILED")

def composio_execute(slug, arguments):
    r=requests.post(f"https://backend.composio.dev/api/v3.1/tools/execute/{slug}",headers={"x-api-key":COMPOSIO_KEY,"Content-Type":"application/json"},json={"user_id":COMPOSIO_ENTITY,"version":"latest","arguments":arguments},timeout=180)
    r.raise_for_status(); j=r.json()
    if j.get("successful") is False: raise RuntimeError(j.get("error") or str(j))
    return j.get("data") or j

def publish_direct(video_path, row):
    if not COMPOSIO_KEY or not COMPOSIO_ENTITY: raise RuntimeError("KAGGLE_SECRET_MISSING:COMPOSIO_API_KEY_OR_COMPOSIO_ENTITY_ID")
    data=video_path.read_bytes(); md5=hashlib.md5(data).hexdigest(); mime=mimetypes.guess_type(video_path.name)[0] or "video/mp4"
    r=requests.post("https://backend.composio.dev/api/v3.1/files/upload/request",headers={"x-api-key":COMPOSIO_KEY,"Content-Type":"application/json"},json={"toolkit_slug":"woop_social","tool_slug":"WOOP_SOCIAL_UPLOAD_MEDIA","filename":video_path.name,"mimetype":mime,"md5":md5},timeout=60)
    r.raise_for_status(); j=r.json(); url=j.get("new_presigned_url") or j.get("newPresignedUrl"); key=j.get("key")
    if not url or not key: raise RuntimeError("COMPOSIO_MEDIA_STAGE_FAILED")
    requests.put(url,data=data,headers={"Content-Type":mime},timeout=180).raise_for_status()
    media=composio_execute("WOOP_SOCIAL_UPLOAD_MEDIA",{"project_id":WOOP_PROJECT,"file":{"name":video_path.name,"mimetype":mime,"s3key":key}})
    media_id=media.get("media_id") if isinstance(media,dict) else None
    if not media_id: raise RuntimeError("WOOP_MEDIA_ID_MISSING")
    accounts=composio_execute("WOOP_SOCIAL_LIST_SOCIAL_ACCOUNTS",{})
    rows=(accounts.get("social_accounts") if isinstance(accounts,dict) else None) or []
    connected={str(x.get("platform") or "").upper():x for x in rows if str(x.get("status") or "").upper()=="CONNECTED"}
    targets=[]
    if connected.get("FACEBOOK"): targets.append({"social_account_id":connected["FACEBOOK"]["id"],"platform":"FACEBOOK","post_type":"VIDEO"})
    if connected.get("INSTAGRAM"): targets.append({"social_account_id":connected["INSTAGRAM"]["id"],"platform":"INSTAGRAM","post_type":"REEL"})
    if connected.get("X"): targets.append({"social_account_id":connected["X"]["id"],"platform":"X"})
    if connected.get("YOUTUBE"): targets.append({"social_account_id":connected["YOUTUBE"]["id"],"platform":"YOUTUBE","title":str(row.get("title") or row.get("asin") or "Amazon Product"),"privacy":"public","tags":["amazon","productfinds","shopping","deals"]})
    if connected.get("TIKTOK"): targets.append({"social_account_id":connected["TIKTOK"]["id"],"platform":"TIKTOK","post_mode":"DIRECT_POST","post_type":"VIDEO","allow_duet":False,"allow_stitch":False,"allow_comment":True,"is_your_brand":False,"is_branded_content":False,"auto_add_music":False,"privacy_level":"PUBLIC_TO_EVERYONE","is_ai_generated_content":False})
    if not targets: raise RuntimeError("NO_CONNECTED_WOOP_SOCIAL_TARGETS")
    asin=str(row.get("asin") or "").upper(); aff=f"https://www.amazon.com/dp/{asin}?tag=desiredplus-20"
    caption=f"{str(row.get('title') or asin)[:150]}\nShop now: {aff}\n\nAs an Amazon Associate I earn from qualifying purchases."
    result=composio_execute("WOOP_SOCIAL_PUBLISH_POST_NOW",{"content":[{"text":caption,"media":[{"type":"MEDIA_LIBRARY","media_id":media_id}]}],"social_accounts":targets,"auto_delete_media_after_publish":False})
    statuses={}
    for d in (result.get("social_account_posts") if isinstance(result,dict) else None) or []:
        statuses[str(d.get("platform") or "").lower()]={"status":d.get("delivery_status"),"external_post_id":d.get("external_post_id"),"url":d.get("external_post_url"),"error":d.get("error_message")}
    if not any(str(v.get("status") or "").upper() in ("PUBLISHED","SUCCESS","SUBMITTED") for v in statuses.values()): raise RuntimeError("NO_PLATFORM_ACCEPTED_VIDEO:"+json.dumps(statuses)[:1000])
    return {"status":"published","media_id":media_id,"platforms":statuses,"source":"kaggle_direct"}

def process_row(batch,row):
    slot=int(row.get("slot") or 0)
    if not slot: return False
    c=claim(batch,slot)
    if not c.get("claimed"): return False
    owner=c.get("owner") or f"kaggle-direct-{batch}-{slot}"
    out=WORK/f"batch_{batch}"/f"slot_{slot}"/f"{row.get('asin','video')}.mp4"; out.parent.mkdir(parents=True,exist_ok=True)
    try:
        render_video(row,out); publication=publish_direct(out,row); set_status(batch,slot,"completed",owner,publication=publication); print("KAGGLE_PUBLISHED",batch,slot,row.get("asin")); return True
    except Exception as exc:
        msg=str(exc)[:1000]
        try: set_status(batch,slot,"failed",owner,error=msg)
        except Exception as status_exc: print("KAGGLE_STATUS_UPDATE_FAILED",batch,slot,str(status_exc)[:500])
        print("KAGGLE_FAILED",batch,slot,row.get("asin"),msg); return False
    finally: shutil.rmtree(out.parent,ignore_errors=True)

def main():
    if not wait_until_window(): print("KAGGLE_VIDEO_WINDOW_ALREADY_CLOSED"); return
    if not VIDEO_SECRET: print("WAITING_FOR_KAGGLE_SECRET:VIDEO_BRIDGE_SECRET"); return
    processed=0; seen=set()
    print("KAGGLE_VIDEO_PRIMARY_STARTED",datetime.now(timezone.utc).isoformat())
    while in_window() and processed < MAX_VIDEOS:
        made_progress=False
        for batch in BATCHES:
            if not in_window() or processed >= MAX_VIDEOS: break
            try: jobs=list_batch(batch)
            except Exception as exc: print("BATCH_READ_FAILED",batch,str(exc)[:500]); continue
            for row in jobs[:5]:
                key=(int(batch),int(row.get("slot") or 0))
                if key in seen or str(row.get("status") or "").lower() in ("completed","failed"): continue
                seen.add(key); made_progress=True
                if process_row(batch,row):
                    processed += 1
                    if processed >= MAX_VIDEOS: break
        if processed >= MAX_VIDEOS or not in_window(): break
        if not made_progress: time.sleep(POLL_SECONDS)
    print("KAGGLE_VIDEO_PRIMARY_FINISHED",json.dumps({"videos_published":processed,"target":MAX_VIDEOS,"window":"06:00-14:00 Asia/Kathmandu"}))

main()
