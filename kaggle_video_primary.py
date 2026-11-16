import os, json, time, hashlib, mimetypes, subprocess, shutil, requests
import base64, gzip, io, sqlite3, tarfile
from pathlib import Path
from datetime import datetime, timezone

def secret(name, default=""):
    value=os.getenv(name,"").strip()
    if value:
        return value
    try:
        from kaggle_secrets import UserSecretsClient
        value=UserSecretsClient().get_secret(name)
        return (value or "").strip()
    except Exception:
        return default

COMPOSIO_KEY = secret("COMPOSIO_API_KEY")
COMPOSIO_ENTITY = secret("COMPOSIO_ENTITY_ID")
WOOP_PROJECT = secret("WOOP_SOCIAL_PROJECT_ID","pr_2gj5wkt0wr9E")
STATE_FILE_ID = os.getenv("DRIVE_STATE_FILE_ID","1Qf-Ccn5aiyRo-tdgJxj5x0UhmYCVrEQb")
PAIR_START = int(os.getenv("KAGGLE_VIDEO_PAIR_START","1"))
RUN_ID = os.getenv("KAGGLE_VIDEO_RUN_ID","").strip() or f"kaggle-unknown-{int(time.time())}"
DATA_DIR = Path("/kaggle/working/desiredplus-state")
DATA_DIR.mkdir(parents=True,exist_ok=True)
os.environ["DATA_DIR"] = str(DATA_DIR)
os.environ["DAILY_LEDGER_DB_PATH"] = str(DATA_DIR/"amazon_daily_ledger.db")
os.environ["PUBLISHED_DB_PATH"] = str(DATA_DIR/"published_products.db")
os.environ["JOB_DB_PATH"] = str(DATA_DIR/"pinterest_agent_jobs.db")
os.environ["RAILWAY_VIDEO_RENDERING_ENABLED"] = "false"
WORK = Path("/kaggle/working/video_primary")
WORK.mkdir(parents=True,exist_ok=True)
DB_NAMES=("amazon_daily_ledger.db","published_products.db","pinterest_agent_jobs.db")

TRACKS = [
 ("Pianoflage - Roy Bargy","https://commons.wikimedia.org/wiki/Special:Redirect/file/Roy_Bargy_-_Pianoflage_(1922).ogg"),
 ("The Wish Bone Rag - Charlotte Blake","https://commons.wikimedia.org/wiki/Special:Redirect/file/Charlotte_Blake_-_The_Wish_Bone_Rag_(1909).ogg"),
 ("Kinklets Ragtime Two Step - Arthur A Marshall","https://commons.wikimedia.org/wiki/Special:Redirect/file/Arthur_A._Marshall_-_Kinklets_Ragtime_Two_Step_(1906).ogg"),
 ("That Hula Hula - Irving Berlin","https://commons.wikimedia.org/wiki/Special:Redirect/file/Irving_Berlin_-_That_Hula_Hula_(1915).ogg"),
 ("At the Jazz Band Ball - U.S. Coast Guard Band","https://commons.wikimedia.org/wiki/Special:Redirect/file/At_the_Jazz_Band_Ball_-_U.S._Coast_Guard_Band.ogg"),
]

FRAME_W, FRAME_H = 1080, 1920
CONTENT_W, CONTENT_H = 918, 1632   # 85% of the 9:16 frame in both dimensions
FPS = 25
SCENE_SECONDS = 3
MIN_IMAGE_SIDE = 160

def composio(slug,args):
    r=requests.post(
        f"https://backend.composio.dev/api/v3.1/tools/execute/{slug}",
        headers={"x-api-key":COMPOSIO_KEY,"Content-Type":"application/json"},
        json={"user_id":COMPOSIO_ENTITY,"version":"latest","dangerously_skip_version_check":True,"arguments":args},
        timeout=180,
    )
    r.raise_for_status()
    j=r.json()
    if j.get("successful") is False: raise RuntimeError(j.get("error") or str(j))
    return j.get("data") or j

def load_drive_state():
    if not COMPOSIO_KEY or not COMPOSIO_ENTITY:
        raise RuntimeError("KAGGLE_COMPOSIO_SECRET_MISSING")
    result=composio("GOOGLEDRIVE_DOWNLOAD_FILE",{"fileId":STATE_FILE_ID})
    item=result.get("downloaded_file_content") or {}
    url=item.get("s3url")
    if not url: raise RuntimeError("DRIVE_STATE_DOWNLOAD_URL_MISSING")
    response=requests.get(url,timeout=120); response.raise_for_status()
    state=json.loads(response.content.decode("utf-8"))
    packed=(state.get("databases") or {}).get("sqlite_gzip_b64")
    if packed:
        raw=gzip.decompress(base64.b64decode(packed))
        with tarfile.open(fileobj=io.BytesIO(raw),mode="r:") as archive:
            for member in archive.getmembers():
                if member.name not in DB_NAMES or not member.isfile(): continue
                stream=archive.extractfile(member)
                if stream: (DATA_DIR/member.name).write_bytes(stream.read())
    return state

def save_drive_state(state):
    for name in DB_NAMES:
        path=DATA_DIR/name
        if path.exists():
            try:
                con=sqlite3.connect(str(path)); con.execute("PRAGMA wal_checkpoint(TRUNCATE)"); con.close()
            except Exception: pass
    packed=io.BytesIO()
    with tarfile.open(fileobj=packed,mode="w:") as archive:
        for name in DB_NAMES:
            path=DATA_DIR/name
            if path.exists(): archive.add(path,arcname=name)
    state["schema_version"]=2
    state["project"]="desiredplus-kaggle-automation"
    state["databases"]={"sqlite_gzip_b64":base64.b64encode(gzip.compress(packed.getvalue(),compresslevel=6)).decode("ascii")}
    state["video_last_run"]={"at":datetime.now(timezone.utc).isoformat(),"pair_start":PAIR_START,"run_id":RUN_ID}
    result=composio("GOOGLEDRIVE_EDIT_FILE",{"file_id":STATE_FILE_ID,"content":json.dumps(state,separators=(",",":"),ensure_ascii=False),"mime_type":"application/json"})
    if not result.get("id"): raise RuntimeError("DRIVE_STATE_SAVE_UNCONFIRMED")

def verified_video_rows(ledger,day):
    con=sqlite3.connect(str(DATA_DIR/"amazon_daily_ledger.db"))
    con.row_factory=sqlite3.Row
    try:
        rows=con.execute(
            """SELECT v.day,v.batch_index AS batch,v.slot,v.asin,v.product_url,v.affiliate_url,
                      v.title,v.source,v.status,v.attempts,v.last_error,v.created_at,v.updated_at,
                      v.completed_at,v.publication_json,v.image_urls_json,v.image_meta_json
               FROM video_jobs v JOIN daily_slots s
                 ON s.day=v.day AND s.slot=v.slot
               WHERE v.day=? AND v.batch_index IN (?,?)
                 AND v.source='pinterest' AND s.status='success' AND s.pinterest_verified=1
               ORDER BY v.batch_index,v.slot""",
            (day,PAIR_START,PAIR_START+1)
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        con.close()

def download(url,path):
    r=requests.get(url,headers={"User-Agent":"Mozilla/5.0"},timeout=60)
    r.raise_for_status()
    if len(r.content)<10000: raise RuntimeError("ASSET_TOO_SMALL")
    path.write_bytes(r.content)

def _save_clean(src,dst):
    from PIL import Image
    with Image.open(src) as im:
        im=im.convert("RGB")
        if min(im.size)<MIN_IMAGE_SIDE: raise RuntimeError("IMAGE_TOO_SMALL")
        im.save(dst,"JPEG",quality=95,optimize=True)

def _unique_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def normalize_images(row,folder):
    """Use only exact product assets. If Amazon exposes too few views, derive
    additional views locally from the verified exact product image. Never use
    unrelated search/brand images."""
    from PIL import Image, ImageEnhance

    raw=json.loads(row.get("image_urls_json") or "[]")
    urls=[u for u in raw if isinstance(u,str) and (u.startswith(("http://","https://")) or Path(u).exists())][:8]
    selected=[]; seen=set()

    for i,u in enumerate(urls):
        try:
            p=folder/f"source_{i+1}.bin"
            if Path(u).exists():
                shutil.copyfile(u,p)
            else:
                download(u,p)
            clean=folder/f"product_{len(selected)+1}.jpg"
            _save_clean(p,clean)
            fp=_unique_hash(clean)
            if fp in seen:
                clean.unlink(missing_ok=True)
                continue
            seen.add(fp)
            selected.append(clean)
            if len(selected)>=5: break
        except Exception as exc:
            print("IMAGE_REJECTED",i,str(exc)[:220])

    if not selected:
        raise RuntimeError("VIDEO_IMAGE_HANDOFF_MISSING:no_verified_exact_product_image")

    # Five scenes do not require five source images. Reuse verified exact-product
    # bytes when Amazon exposes fewer views; each scene has independent motion.
    # Never mirror/crop/rotate the source here because that can alter logos/text.
    while len(selected)<5:
        selected.append(selected[0])
    return selected[:5]

def _scene_filter(index):
    # Build the motion INSIDE the 85% content box. Only after motion is applied
    # is the content centered on a full black 1080x1920 canvas. This prevents
    # zoompan from magnifying the black border and producing the old ~70% look.
    frames=FPS*SCENE_SECONDS
    fit=f"scale={CONTENT_W}:{CONTENT_H}:force_original_aspect_ratio=decrease,pad={CONTENT_W}:{CONTENT_H}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"

    if index==0:
        motion=(f"{fit},zoompan=z='min(zoom+0.0017,1.10)':"
                f"x='(iw-iw/zoom)*0.58':y='(ih-ih/zoom)/2':"
                f"d={frames}:s={CONTENT_W}x{CONTENT_H}:fps={FPS},"
                "rotate='0.018*sin(2*PI*t/3)':fillcolor=black,setsar=1")
    elif index==1:
        motion=(f"{fit},zoompan=z='min(zoom+0.0017,1.10)':"
                f"x='(iw-iw/zoom)*0.42':y='(ih-ih/zoom)/2':"
                f"d={frames}:s={CONTENT_W}x{CONTENT_H}:fps={FPS},"
                "rotate='-0.018*sin(2*PI*t/3)':fillcolor=black,setsar=1")
    elif index==2:
        motion=(f"{fit},zoompan=z='1.035+0.025*sin(PI*on/{frames})':"
                f"x='(iw-iw/zoom)*0.25':y='(ih-ih/zoom)/2':"
                f"d={frames}:s={CONTENT_W}x{CONTENT_H}:fps={FPS},setsar=1")
    elif index==3:
        motion=(f"{fit},zoompan=z='1.035+0.025*sin(PI*on/{frames})':"
                f"x='(iw-iw/zoom)*0.75':y='(ih-ih/zoom)/2':"
                f"d={frames}:s={CONTENT_W}x{CONTENT_H}:fps={FPS},setsar=1")
    else:
        motion=f"{fit},fps={FPS},setsar=1"

    return motion + f",pad={FRAME_W}:{FRAME_H}:(ow-iw)/2:(oh-ih)/2:black"

def render(row,out):
    folder=out.parent
    imgs=normalize_images(row,folder)
    filters=[]
    for i in range(5):
        filters.append(f"[{i}:v]{_scene_filter(i)}[v{i}]")

    filters.append("[v0][v1][v2][v3][v4]concat=n=5:v=1:a=0,setpts=N/25/TB[v]")
    title=str(row.get("title") or row.get("asin") or "Amazon Product")[:70]
    title=title.replace("\\","\\\\").replace(":","\\:").replace("'","\\'")
    filters.append(
        f"[v]drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
        f"text='{title}':x=(w-text_w)/2:y=110:fontsize=46:fontcolor=white:"
        "borderw=3:bordercolor=black@0.85,"
        "drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
        "text='SHOP NOW':x=(w-text_w)/2:y=1775:fontsize=60:fontcolor=white:"
        "borderw=3:bordercolor=black@0.85[outv]"
    )

    inputs=[]
    for p in imgs:
        inputs += ["-loop","1","-t",str(SCENE_SECONDS),"-i",str(p)]

    cmd=[
        "ffmpeg","-y",*inputs,
        "-filter_complex",";".join(filters),
        "-map","[outv]","-an",
        "-c:v","libx264","-preset","veryfast","-crf","23",
        "-pix_fmt","yuv420p","-movflags","+faststart",
        str(out)
    ]
    q=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=420)
    if q.returncode:
        raise RuntimeError("FFMPEG_VIDEO_FAILED:"+q.stderr[-1400:])
    if not out.exists() or out.stat().st_size<20000:
        raise RuntimeError("VIDEO_OUTPUT_INVALID")

    music_index=int(hashlib.sha256(str(row.get("asin")).encode()).hexdigest(),16)%len(TRACKS)
    track_name,track_url=TRACKS[music_index]
    music=folder/"music.ogg"
    download(track_url,music)

    final=folder/"final.mp4"
    aq=[
        "ffmpeg","-y","-i",str(out),"-stream_loop","-1","-i",str(music),
        "-t","15",
        "-filter:a","volume=0.22,afade=t=in:st=0:d=0.5,afade=t=out:st=14:d=1",
        "-map","0:v:0","-map","1:a:0",
        "-c:v","copy","-c:a","aac","-b:a","128k",
        "-shortest","-movflags","+faststart",str(final)
    ]
    q=subprocess.run(aq,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=180)
    if q.returncode:
        raise RuntimeError("FFMPEG_AUDIO_FAILED:"+q.stderr[-1400:])

    probe=subprocess.run(
        ["ffprobe","-v","error","-show_entries",
         "format=duration:format_tags=encoder:stream=width,height,codec_name",
         "-of","json",str(final)],
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=30
    )
    if probe.returncode:
        raise RuntimeError("VIDEO_PROBE_FAILED")
    info=json.loads(probe.stdout)
    streams=info.get("streams") or []
    video=next((s for s in streams if s.get("width")),None)
    dur=float(info.get("format",{}).get("duration") or 0)
    if not video or int(video.get("width",0))!=FRAME_W or int(video.get("height",0))!=FRAME_H:
        raise RuntimeError("VIDEO_DIMENSIONS_INVALID")
    if video.get("codec_name")!="h264":
        raise RuntimeError("VIDEO_CODEC_INVALID")
    if dur<14.5:
        raise RuntimeError("VIDEO_DURATION_INVALID")
    print("VIDEO_LAYOUT","9:16","content_box","918x1632","coverage_rule","85pct_max_box")
    print("MUSIC_TRACK",track_name,track_url)
    return final,track_name,track_url

def publish(video,row,batch):
    if not COMPOSIO_KEY or not COMPOSIO_ENTITY:
        raise RuntimeError("KAGGLE_COMPOSIO_SECRET_MISSING")

    data=video.read_bytes()
    md5=hashlib.md5(data).hexdigest()
    mime="video/mp4"
    req=requests.post(
        "https://backend.composio.dev/api/v3.1/files/upload/request",
        headers={"x-api-key":COMPOSIO_KEY,"Content-Type":"application/json"},
        json={"toolkit_slug":"woop_social","tool_slug":"WOOP_SOCIAL_UPLOAD_MEDIA",
              "filename":video.name,"mimetype":mime,"md5":md5},
        timeout=60
    )
    req.raise_for_status()
    j=req.json()
    url=j.get("new_presigned_url") or j.get("newPresignedUrl")
    key=j.get("key")
    if not url or not key: raise RuntimeError("COMPOSIO_MEDIA_STAGE_FAILED")
    requests.put(url,data=data,headers={"Content-Type":mime},timeout=180).raise_for_status()

    media=composio("WOOP_SOCIAL_UPLOAD_MEDIA",
                   {"project_id":WOOP_PROJECT,
                    "file":{"name":video.name,"mimetype":mime,"s3key":key}})
    media_id=media.get("media_id") if isinstance(media,dict) else None
    if not media_id: raise RuntimeError("WOOP_MEDIA_ID_MISSING")

    accounts=composio("WOOP_SOCIAL_LIST_SOCIAL_ACCOUNTS",{})
    connected={
        str(x.get("platform") or "").upper():x
        for x in (accounts.get("social_accounts") or [])
        if str(x.get("status") or "").upper()=="CONNECTED"
    }
    targets=[]
    if connected.get("FACEBOOK"):
        targets.append({"social_account_id":connected["FACEBOOK"]["id"],"platform":"FACEBOOK","post_type":"VIDEO"})
    if connected.get("INSTAGRAM"):
        targets.append({"social_account_id":connected["INSTAGRAM"]["id"],"platform":"INSTAGRAM","post_type":"REEL"})
    if connected.get("X"):
        targets.append({"social_account_id":connected["X"]["id"],"platform":"X"})
    if connected.get("YOUTUBE"):
        targets.append({"social_account_id":connected["YOUTUBE"]["id"],"platform":"YOUTUBE",
                        "title":str(row.get("title") or row.get("asin") or "Amazon Product"),
                        "privacy":"public","tags":["amazon","productfinds","shopping","deals"]})
    if connected.get("TIKTOK"):
        targets.append({"social_account_id":connected["TIKTOK"]["id"],"platform":"TIKTOK",
                        "post_mode":"DIRECT_POST","post_type":"VIDEO",
                        "allow_duet":False,"allow_stitch":False,"allow_comment":True,
                        "is_your_brand":False,"is_branded_content":False,"auto_add_music":False,
                        "privacy_level":"PUBLIC_TO_EVERYONE","is_ai_generated_content":False})
    if not targets: raise RuntimeError("NO_CONNECTED_TARGETS")

    asin=str(row.get("asin") or "").upper()
    caption=(
        f"{str(row.get('title') or asin)[:150]}\\n"
        f"Shop now: https://www.amazon.com/dp/{asin}?tag=desiredplus-20\\n[video-run:{RUN_ID}][asin:{asin}][batch:{batch}]\\n\\n"
        "As an Amazon Associate I earn from qualifying purchases."
    )
    result=composio(
        "WOOP_SOCIAL_PUBLISH_POST_NOW",
        {"content":[{"text":caption,"media":[{"type":"MEDIA_LIBRARY","media_id":media_id}]}],
         "social_accounts":targets,"auto_delete_media_after_publish":False}
    )
    statuses={}
    for d in (result.get("social_account_posts") if isinstance(result,dict) else []) or []:
        statuses[str(d.get("platform") or "").lower()]={
            "status":d.get("delivery_status"),
            "external_post_id":d.get("external_post_id"),
            "url":d.get("external_post_url"),
            "error":d.get("error_message")
        }
    accepted=any(
        str(v.get("status") or "").upper() in ("PUBLISHED","SUCCESS","SUBMITTED")
        for v in statuses.values()
    )
    if not accepted:
        raise RuntimeError("NO_PLATFORM_ACCEPTED_VIDEO:"+json.dumps(statuses)[:1200])
    return {"media_id":media_id,"platforms":statuses}

def main():
    if PAIR_START not in (1,3,5,7,9):
        raise RuntimeError("VIDEO_PAIR_START_MUST_BE_1_3_5_7_9")
    if not COMPOSIO_KEY or not COMPOSIO_ENTITY:
        raise RuntimeError("KAGGLE_COMPOSIO_SECRET_MISSING")
    state=load_drive_state()
    from daily_ledger import ledger
    day=ledger.today_str()
    jobs=verified_video_rows(ledger,day)
    if os.getenv("KAGGLE_VIDEO_DRY_RUN","false").strip().lower() in ("1","true","yes"):
        accounts=composio("WOOP_SOCIAL_LIST_SOCIAL_ACCOUNTS",{})
        connected=[str(x.get("platform") or "").upper() for x in (accounts.get("social_accounts") or [])
                   if str(x.get("status") or "").upper()=="CONNECTED"]
        save_drive_state(state)
        print(json.dumps({"status":"preflight_passed","day":day,"pair_start":PAIR_START,
                          "verified_pinterest_products":len(jobs),"connected_video_platforms":connected,
                          "drive_state_saved":True}))
        return
    completed=0
    if not jobs:
        save_drive_state(state)
        print(json.dumps({"pair_start":PAIR_START,"day":day,"videos_completed":0,
                          "status":"no_verified_pinterest_products","drive_state_saved":True}))
        return
    for row in jobs:
        batch=int(row.get("batch") or 0)
        slot=int(row.get("slot") or 0)
        if not batch or not slot or str(row.get("status") or "").lower()=="completed":
            continue
        owner=f"kaggle-{day}-pair-{PAIR_START}-{RUN_ID}"
        claim=ledger.claim_video_job(day,batch,slot,owner)
        if not claim or not claim.get("claimed"):
            print("VIDEO_JOB_NOT_CLAIMED",batch,slot,claim)
            continue
        root=WORK/f"b{batch}_s{slot}"
        root.mkdir(parents=True,exist_ok=True)
        publication_started=False
        try:
            from video_fallback import _resolve_product_images
            verified_inputs=root/"verified_inputs"
            image_urls,image_source=_resolve_product_images(row,materialize_dir=verified_inputs)
            row["image_urls_json"]=json.dumps(image_urls)
            final,track,url=render(row,root/"video_no_audio.mp4")
            ledger.mark_video_job(day,batch,slot,"publishing",owner=owner)
            save_drive_state(state)
            publication_started=True
            pub=publish(final,row,batch)
            pub.update({"music_track":track,"music_source":url,"image_source":image_source,
                        "route":"product-1-zoom-right;product-2-zoom-left;product-3-pan-right;product-4-pan-left;product-5-static",
                        "aspect":"9:16","content_coverage":"85pct_max_box","black_border":"15pct_outer_frame",
                        "run_id":RUN_ID})
            ledger.mark_video_job(day,batch,slot,"completed",owner=owner,
                                  publication_json=json.dumps(pub,separators=(",",":"))[:12000])
            completed+=1
            print("KAGGLE_REAL_PRODUCT_VIDEO_SUCCESS",batch,slot,row.get("asin"))
            save_drive_state(state)
        except Exception as exc:
            status="needs_review" if publication_started else "failed"
            ledger.mark_video_job(day,batch,slot,status,owner=owner,error=str(exc)[:1000])
            print("KAGGLE_REAL_PRODUCT_VIDEO_FAILED",batch,slot,row.get("asin"),status,str(exc)[:1000])
            save_drive_state(state)
        finally:
            shutil.rmtree(root,ignore_errors=True)
    remaining=verified_video_rows(ledger,day)
    bad=[{"batch":r.get("batch"),"slot":r.get("slot"),"asin":r.get("asin"),"status":r.get("status")}
         for r in remaining if str(r.get("status") or "").lower() not in ("completed","needs_review")]
    save_drive_state(state)
    print(json.dumps({"pair_start":PAIR_START,"day":day,"videos_completed":completed,
                      "verified_pinterest_products":len(jobs),"remaining_retryable":len(bad),
                      "drive_state_saved":True}))
    if bad:
        raise RuntimeError("VIDEO_PAIR_HAS_RETRYABLE_JOBS:"+json.dumps(bad)[:1800])

main()
