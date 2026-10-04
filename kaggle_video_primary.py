import os, json, time, hashlib, mimetypes, subprocess, shutil, requests
from pathlib import Path

RAILWAY = os.getenv("RAILWAY_VIDEO_BASE_URL","https://web-production-dae68.up.railway.app").rstrip("/")
VIDEO_SECRET = os.getenv("VIDEO_BRIDGE_SECRET","").strip()
COMPOSIO_KEY = os.getenv("COMPOSIO_API_KEY","").strip()
COMPOSIO_ENTITY = os.getenv("COMPOSIO_ENTITY_ID","").strip()
WOOP_PROJECT = os.getenv("WOOP_SOCIAL_PROJECT_ID","pr_2gj5wkt0wr9E")
PAIR_START = int(os.getenv("KAGGLE_VIDEO_PAIR_START","1"))
WORK = Path("/kaggle/working/video_primary")
WORK.mkdir(parents=True,exist_ok=True)

TRACKS = [
 ("Pianoflage - Roy Bargy","https://commons.wikimedia.org/wiki/Special:Redirect/file/Roy_Bargy_-_Pianoflage_(1922).ogg"),
 ("The Wish Bone Rag - Charlotte Blake","https://commons.wikimedia.org/wiki/Special:Redirect/file/Charlotte_Blake_-_The_Wish_Bone_Rag_(1909).ogg"),
 ("Kinklets Ragtime Two Step - Arthur A Marshall","https://commons.wikimedia.org/wiki/Special:Redirect/file/Arthur_A._Marshall_-_Kinklets_Ragtime_Two_Step_(1906).ogg"),
 ("That Hula Hula - Irving Berlin","https://commons.wikimedia.org/wiki/Special:Redirect/file/Irving_Berlin_-_That_Hula_Hula_(1915).ogg"),
 ("At the Jazz Band Ball - U.S. Coast Guard Band","https://commons.wikimedia.org/wiki/Special:Redirect/file/At_the_Jazz_Band_Ball_-_U.S._Coast_Guard_Band.ogg"),
]

def api(method,path,**kwargs):
    headers=kwargs.pop("headers",{})
    headers["X-Video-Secret"]=VIDEO_SECRET
    r=requests.request(method,RAILWAY+path,headers=headers,timeout=60,**kwargs)
    if r.status_code>=400: raise RuntimeError(f"RAILWAY_{r.status_code}:{r.text[:800]}")
    return r.json()

def composio(slug,args):
    r=requests.post(f"https://backend.composio.dev/api/v3.1/tools/execute/{slug}",
        headers={"x-api-key":COMPOSIO_KEY,"Content-Type":"application/json"},
        json={"user_id":COMPOSIO_ENTITY,"version":"latest","arguments":args},timeout=180)
    r.raise_for_status()
    j=r.json()
    if j.get("successful") is False: raise RuntimeError(j.get("error") or str(j))
    return j.get("data") or j

def download(url,path):
    r=requests.get(url,headers={"User-Agent":"Mozilla/5.0"},timeout=60)
    r.raise_for_status()
    if len(r.content)<10000: raise RuntimeError("ASSET_TOO_SMALL")
    path.write_bytes(r.content)

def normalize_images(row,folder):
    raw=json.loads(row.get("image_urls_json") or "[]")
    urls=[u for u in raw if isinstance(u,str) and u.startswith(("http://","https://"))]
    if len(urls)<4: raise RuntimeError("VIDEO_IMAGE_HANDOFF_MISSING:need_4_exact_product_images")
    files=[]
    from PIL import Image
    for i,u in enumerate(urls[:5]):
        p=folder/f"product_{i+1}.jpg"
        try:
            download(u,p)
            with Image.open(p) as im:
                im=im.convert("RGB")
                if min(im.size)<160: raise RuntimeError("IMAGE_TOO_SMALL")
                im.save(p,"JPEG",quality=94)
            files.append(p)
        except Exception as e:
            print("IMAGE_REJECTED",i,str(e)[:250])
    if len(files)<4: raise RuntimeError(f"VIDEO_IMAGE_VALIDATION_FAILED:{len(files)}/4")
    if len(files)==4:
        with Image.open(files[3]) as im:
            w,h=im.size
            side=min(w,h)
            crop=im.crop(((w-side)//2,(h-side)//2,(w+side)//2,(h+side)//2))
            crop.save(folder/"product_5.jpg","JPEG",quality=94)
        files.append(folder/"product_5.jpg")
    return files[:5]

def render(row,out):
    folder=out.parent
    imgs=normalize_images(row,folder)
    filters=[]
    for i in range(5):
        # Product-only 9:16 scene construction. Scene 1/2 zoom+opposite rotation,
        # scenes 3/4 lateral motion, scene 5 static.
        base=f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2:black"
        if i==0:
            f=base+",rotate='0.025*sin(2*PI*t/3)':fillcolor=black,zoompan=z='min(zoom+0.0018,1.12)':d=75:s=1080x1920:fps=25"
        elif i==1:
            f=base+",rotate='-0.025*sin(2*PI*t/3)':fillcolor=black,zoompan=z='min(zoom+0.0018,1.12)':d=75:s=1080x1920:fps=25"
        elif i==2:
            f=base+",zoompan=z='1.05+0.04*sin(PI*on/75)':x='iw/2-(iw/zoom/2)+35*sin(PI*on/75)':y='ih/2-(ih/zoom/2)':d=75:s=1080x1920:fps=25"
        elif i==3:
            f=base+",zoompan=z='1.06+0.04*sin(PI*on/75)':x='iw/2-(iw/zoom/2)-35*sin(PI*on/75)':y='ih/2-(ih/zoom/2)':d=75:s=1080x1920:fps=25"
        else:
            f=base+",fps=25"
        filters.append(f+"[v{i}]")
    filters.append("[v0][v1][v2][v3][v4]concat=n=5:v=1:a=0,setpts=N/25/TB[v]")
    title=str(row.get("title") or row.get("asin") or "Amazon Product")[:70].replace("\\","\\\\").replace(":","\\:").replace("'","\\'")
    filters.append(f"[v]drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:text='{title}':x=(w-text_w)/2:y=120:fontsize=48:fontcolor=white:borderw=3:bordercolor=black@0.8,drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:text='SHOP NOW':x=(w-text_w)/2:y=1770:fontsize=62:fontcolor=white:borderw=3:bordercolor=black@0.8[outv]")
    inputs=[]
    for p in imgs: inputs += ["-loop","1","-t","3","-i",str(p)]
    cmd=["ffmpeg","-y",*inputs,"-filter_complex",";".join(filters),"-map","[outv]","-an","-c:v","libx264","-preset","veryfast","-crf","23","-pix_fmt","yuv420p",str(out)]
    q=subprocess.run(cmd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=420)
    if q.returncode: raise RuntimeError("FFMPEG_VIDEO_FAILED:"+q.stderr[-1000:])
    if not out.exists() or out.stat().st_size<20000: raise RuntimeError("VIDEO_OUTPUT_INVALID")
    music_index=int(hashlib.sha256(str(row.get("asin")).encode()).hexdigest(),16)%len(TRACKS)
    track_name,track_url=TRACKS[music_index]
    music=folder/"music.ogg"; download(track_url,music)
    final=folder/"final.mp4"
    aq=["ffmpeg","-y","-i",str(out),"-stream_loop","-1","-i",str(music),"-t","15",
        "-filter:a","volume=0.22,afade=t=in:st=0:d=0.5,afade=t=out:st=14:d=1",
        "-map","0:v:0","-map","1:a:0","-c:v","copy","-c:a","aac","-b:a","128k","-shortest","-movflags","+faststart",str(final)]
    q=subprocess.run(aq,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=180)
    if q.returncode: raise RuntimeError("FFMPEG_AUDIO_FAILED:"+q.stderr[-1000:])
    probe=subprocess.run(["ffprobe","-v","error","-show_entries","format=duration:format_tags=encoder","-of","json",str(final)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=30)
    if probe.returncode: raise RuntimeError("VIDEO_PROBE_FAILED")
    info=json.loads(probe.stdout)
    dur=float(info.get("format",{}).get("duration") or 0)
    if dur<14: raise RuntimeError("VIDEO_DURATION_INVALID")
    print("MUSIC_TRACK",track_name,track_url)
    return final,track_name,track_url

def publish(video,row):
    if not COMPOSIO_KEY or not COMPOSIO_ENTITY: raise RuntimeError("KAGGLE_COMPOSIO_SECRET_MISSING")
    data=video.read_bytes(); md5=hashlib.md5(data).hexdigest(); mime="video/mp4"
    req=requests.post("https://backend.composio.dev/api/v3.1/files/upload/request",
        headers={"x-api-key":COMPOSIO_KEY,"Content-Type":"application/json"},
        json={"toolkit_slug":"woop_social","tool_slug":"WOOP_SOCIAL_UPLOAD_MEDIA","filename":video.name,"mimetype":mime,"md5":md5},timeout=60)
    req.raise_for_status(); j=req.json()
    url=j.get("new_presigned_url") or j.get("newPresignedUrl"); key=j.get("key")
    if not url or not key: raise RuntimeError("COMPOSIO_MEDIA_STAGE_FAILED")
    requests.put(url,data=data,headers={"Content-Type":mime},timeout=180).raise_for_status()
    media=composio("WOOP_SOCIAL_UPLOAD_MEDIA",{"project_id":WOOP_PROJECT,"file":{"name":video.name,"mimetype":mime,"s3key":key}})
    media_id=media.get("media_id") if isinstance(media,dict) else None
    if not media_id: raise RuntimeError("WOOP_MEDIA_ID_MISSING")
    accounts=composio("WOOP_SOCIAL_LIST_SOCIAL_ACCOUNTS",{})
    connected={str(x.get("platform") or "").upper():x for x in (accounts.get("social_accounts") or []) if str(x.get("status") or "").upper()=="CONNECTED"}
    targets=[]
    if connected.get("FACEBOOK"): targets.append({"social_account_id":connected["FACEBOOK"]["id"],"platform":"FACEBOOK","post_type":"VIDEO"})
    if connected.get("INSTAGRAM"): targets.append({"social_account_id":connected["INSTAGRAM"]["id"],"platform":"INSTAGRAM","post_type":"REEL"})
    if connected.get("X"): targets.append({"social_account_id":connected["X"]["id"],"platform":"X"})
    if connected.get("YOUTUBE"): targets.append({"social_account_id":connected["YOUTUBE"]["id"],"platform":"YOUTUBE","title":str(row.get("title") or row.get("asin") or "Amazon Product"),"privacy":"public","tags":["amazon","productfinds","shopping","deals"]})
    if connected.get("TIKTOK"): targets.append({"social_account_id":connected["TIKTOK"]["id"],"platform":"TIKTOK","post_mode":"DIRECT_POST","post_type":"VIDEO","allow_duet":False,"allow_stitch":False,"allow_comment":True,"is_your_brand":False,"is_branded_content":False,"auto_add_music":False,"privacy_level":"PUBLIC_TO_EVERYONE","is_ai_generated_content":False})
    if not targets: raise RuntimeError("NO_CONNECTED_TARGETS")
    asin=str(row.get("asin") or "").upper()
    caption=f"{str(row.get('title') or asin)[:150]}\\nShop now: https://www.amazon.com/dp/{asin}?tag=desiredplus-20\\n\\nAs an Amazon Associate I earn from qualifying purchases."
    result=composio("WOOP_SOCIAL_PUBLISH_POST_NOW",{"content":[{"text":caption,"media":[{"type":"MEDIA_LIBRARY","media_id":media_id}]}],"social_accounts":targets,"auto_delete_media_after_publish":False})
    statuses={}
    for d in (result.get("social_account_posts") if isinstance(result,dict) else []) or []:
        statuses[str(d.get("platform") or "").lower()]={"status":d.get("delivery_status"),"external_post_id":d.get("external_post_id"),"url":d.get("external_post_url"),"error":d.get("error_message")}
    accepted=any(str(v.get("status") or "").upper() in ("PUBLISHED","SUCCESS","SUBMITTED") for v in statuses.values())
    if not accepted: raise RuntimeError("NO_PLATFORM_ACCEPTED_VIDEO:"+json.dumps(statuses)[:1200])
    return {"media_id":media_id,"platforms":statuses}

def main():
    if not VIDEO_SECRET: raise RuntimeError("VIDEO_BRIDGE_SECRET_MISSING")
    jobs=[]
    for batch in (PAIR_START,PAIR_START+1):
        jobs.extend(api("POST","/video/batch",json={"batch":batch,"fallback":False}).get("jobs") or [])
    completed=0
    for row in jobs[:10]:
        batch=int(row.get("batch") or 0); slot=int(row.get("slot") or 0)
        if not batch or not slot or str(row.get("status") or "").lower() in ("completed","failed"): continue
        c=api("POST",f"/video/job/{batch}/{slot}/claim")
        if not c.get("claimed"): continue
        owner=c.get("owner") or f"kaggle-primary-{batch}-{slot}"
        root=WORK/f"b{batch}_s{slot}"; root.mkdir(parents=True,exist_ok=True)
        try:
            final,track,url=render(row,root/f"video_no_audio.mp4")
            pub=publish(final,row)
            api("POST",f"/video/job/{batch}/{slot}/status",params={"status":"completed","owner":owner,"publication_json":json.dumps({**pub,"music_track":track,"music_source":url,"route":"product-1-zoom-right;product-2-zoom-left;product-3-pan-right;product-4-pan-left;product-5-static","aspect":"9:16"})[:12000]})
            completed+=1
            print("KAGGLE_REAL_PRODUCT_VIDEO_SUCCESS",batch,slot,row.get("asin"))
        except Exception as exc:
            try: api("POST",f"/video/job/{batch}/{slot}/status",params={"status":"failed","owner":owner,"error":str(exc)[:1000]})
            except Exception as e: print("STATUS_UPDATE_FAILED",str(e)[:300])
            print("KAGGLE_REAL_PRODUCT_VIDEO_FAILED",batch,slot,row.get("asin"),str(exc)[:1000])
        finally:
            shutil.rmtree(root,ignore_errors=True)
    print(json.dumps({"pair_start":PAIR_START,"videos_completed":completed,"target":10}))

main()
