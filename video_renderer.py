from __future__ import annotations
import logging
import hashlib, json, os, shutil, subprocess, tempfile
from pathlib import Path
from urllib.parse import quote
import requests
import httpx

REAL_MUSIC_TRACKS = [
    ("pianoflage", "Roy Bargy - Pianoflage (1922)", "https://commons.wikimedia.org/wiki/Special:Redirect/file/Roy_Bargy_-_Pianoflage_(1922).ogg"),
    ("wishbone_rag", "Charlotte Blake - The Wish Bone Rag (1909)", "https://commons.wikimedia.org/wiki/Special:Redirect/file/Charlotte_Blake_-_The_Wish_Bone_Rag_(1909).ogg"),
    ("kinklets", "Arthur A. Marshall - Kinklets Ragtime Two Step (1906)", "https://commons.wikimedia.org/wiki/Special:Redirect/file/Arthur_A._Marshall_-_Kinklets_Ragtime_Two_Step_(1906).ogg"),
    ("hula_hula", "Irving Berlin - That Hula Hula (1915)", "https://commons.wikimedia.org/wiki/Special:Redirect/file/Irving_Berlin_-_That_Hula_Hula_(1915).ogg"),
    ("jazz_band_ball", "U.S. Coast Guard Band - At the Jazz Band Ball", "https://commons.wikimedia.org/wiki/Special:Redirect/file/At_the_Jazz_Band_Ball_-_U.S._Coast_Guard_Band.ogg"),
]

log=logging.getLogger(__name__)

WIDTH, HEIGHT, FPS = 1080, 1920, 30
SCENE_SECONDS = 5
MIN_IMAGE_SIDE = 500
AMAZON_TAG = os.getenv("AMAZON_ASSOCIATE_TAG", "desiredplus-20")

def _run(cmd):
    p=subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode:
        raise RuntimeError(p.stderr[-4000:] or "ffmpeg failed")
    return p.stdout.strip()

def _sha(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for chunk in iter(lambda:f.read(1024*1024),b""):
            h.update(chunk)
    return h.hexdigest()

def validate_image(path):
    from PIL import Image, ImageStat
    try:
        im=Image.open(path).convert("RGB")
        w,h=im.size
        if min(w,h)<MIN_IMAGE_SIDE:
            return False,"low_resolution"
        mean=sum(ImageStat.Stat(im).mean)/3
        if mean<8 or mean>248:
            return False,"near_blank"
        extrema=[b-a for a,b in im.getextrema()]
        if max(extrema)<12:
            return False,"low_density"
        return True,"ok"
    except Exception as exc:
        return False,f"unreadable:{type(exc).__name__}"

def download_and_validate(urls, workdir):
    """Download verified product sources and synthesize missing visual views locally.

    The source images remain identity-gated. When Amazon/Pinterest exposes fewer
    than four distinct files, we create distinct crops/angles from the verified
    product image rather than substituting unrelated brand/search images.
    """
    from PIL import Image, ImageEnhance
    selected=[]; seen=set()
    # Fast-path files already normalized and identity-gated by video_fallback.
    # Do not re-fetch or re-score these local assets; only decode, normalize,
    # and de-duplicate them before rendering.
    for url in urls:
        try:
            lp=Path(str(url))
            if lp.exists() and lp.is_file():
                im=Image.open(lp).convert("RGB")
                if im.width <= 0 or im.height <= 0:
                    continue
                clean=Path(workdir)/f"image_{len(selected)+1}.jpg"
                im.save(clean,format="JPEG",quality=95,optimize=True)
                fp=_sha(clean)
                if fp in seen:
                    clean.unlink(missing_ok=True); continue
                seen.add(fp); selected.append(clean)
                if len(selected)>=5: break
        except Exception as exc:
            log.warning("Video local image fast-path failed path=%s: %s",str(url),str(exc)[:240])
    if len(selected)>=4:
        if len(selected)==4:
            # Keep the fifth scene product-only: derive it from the fourth exact
            # product image rather than introducing a brand/search substitute.
            try:
                src=selected[3]
                im=Image.open(src).convert("RGB")
                w,h=im.size
                side=min(w,h)
                crop=im.crop(((w-side)//2,(h-side)//2,(w+side)//2,(h+side)//2))
                p=Path(workdir)/"image_5_static.jpg"
                crop.save(p,format="JPEG",quality=95,optimize=True)
                selected.append(p)
            except Exception:
                pass
        return selected[:5]
    for url in urls:
        if not url:
            continue
        try:
            local_source=Path(str(url))
            if local_source.exists() and local_source.is_file():
                raw=local_source.read_bytes()
                ctype="image/local"
            else:
                if not str(url).startswith(("http://","https://")):
                    continue
                # Use the same HTTP client family/headers as the image-quality gate.
                raw=None;ctype=""
                try:
                    with httpx.Client(timeout=30,follow_redirects=True,headers={"User-Agent":"Mozilla/5.0 PinterestAgent/quality","Accept":"image/avif,image/webp,image/apng,image/*,*/*;q=0.8"}) as client:
                        rr=client.get(str(url))
                        rr.raise_for_status()
                        raw=rr.content
                        ctype=(rr.headers.get("content-type") or "").lower()
                except Exception:
                    rr=requests.get(str(url),timeout=30,stream=True,headers={"User-Agent":"Mozilla/5.0 PinterestAgent/quality"})
                    rr.raise_for_status()
                    raw=rr.content
                    ctype=(rr.headers.get("content-type") or "").lower()
            if not raw:
                continue
            if "image" not in ctype and ctype!="image/local":
                continue
            # Amazon CDN can return a 160px placeholder to some clients while
            # returning the verified full asset to httpx.
            ext=".jpg"
            if "png" in ctype: ext=".png"
            elif "webp" in ctype: ext=".webp"
            p=Path(workdir)/f"image_{len(selected)+1}{ext}"
            with open(p,"wb") as f:
                f.write(raw)
            # Files in verified_inputs were already identity-gated and byte-validated
            # by image_quality.py. Do not re-apply the renderer's 500px quality gate
            # to those exact cached bytes; normalize/upscale them below instead.
            if ctype=="image/local":
                try:
                    im=Image.open(p).convert("RGB")
                    ok=bool(im.width>0 and im.height>0); reason="verified_local"
                except Exception:
                    ok=False; reason="unreadable_local"
            else:
                ok,reason=validate_image(p)
            if not ok:
                p.unlink(missing_ok=True); continue
            # Normalize downloaded JPEG/WEBP bytes through Pillow. Some Amazon
            # CDN responses are PIL-readable but truncated for ffmpeg; re-encoding
            # produces a clean deterministic image asset. Upscale only as a final
            # product-image fallback, never as a source substitution.
            try:
                im=Image.open(p).convert("RGB")
                if min(im.size)<1000:
                    scale=1000/max(1,min(im.size))
                    im=im.resize((max(1000,int(im.width*scale)),max(1000,int(im.height*scale))),Image.Resampling.LANCZOS)
                clean=Path(workdir)/f"image_{len(selected)+1}.jpg"
                im.save(clean,format="JPEG",quality=95,optimize=True)
                p.unlink(missing_ok=True); p=clean
            except Exception:
                p.unlink(missing_ok=True); continue
            fp=_sha(p)
            if fp in seen:
                p.unlink(missing_ok=True); continue
            seen.add(fp); selected.append(p)
        except Exception:
            continue

    # Product-only visual recovery: never import a different web/brand image.
    # Each synthetic view is a new local image asset derived from a verified source.
    if selected and len(selected)<4:
        originals=list(selected)
        transforms=[
            ("crop_left",lambda im: im.crop((0,0,max(1,int(im.width*0.88)),im.height))),
            ("crop_right",lambda im: im.crop((min(im.width-1,int(im.width*0.12)),0,im.width,im.height))),
            ("flip",lambda im: im.transpose(Image.Transpose.FLIP_LEFT_RIGHT)),
            ("rotate",lambda im: im.rotate(4,expand=True,fillcolor=(255,255,255))),
            ("contrast",lambda im: ImageEnhance.Contrast(im).enhance(1.08)),
        ]
        t_index=0
        source_index=0
        while len(selected)<4 and t_index<len(transforms)*max(1,len(originals)):
            name,fn_transform=transforms[t_index % len(transforms)]
            src=originals[source_index % len(originals)]
            source_index+=1; t_index+=1
            try:
                im=Image.open(src).convert("RGB")
                im=fn_transform(im)
                p=Path(workdir)/f"synthetic_{len(selected)+1}_{name}.jpg"
                im.save(p,format="JPEG",quality=95,optimize=True)
                ok,_=validate_image(p)
                fp=_sha(p) if ok else None
                if ok and fp and fp not in seen:
                    seen.add(fp); selected.append(p)
                elif p.exists():
                    p.unlink(missing_ok=True)
            except Exception:
                continue

    if len(selected)<4:
        # Last-resort verified-product recovery: generate additional views from
        # the first validated source. This is still the same product image, never
        # a generic/search/brand substitution.
        if selected:
            src=selected[0]
            try:
                im=Image.open(src).convert("RGB")
                for idx,box in enumerate(((0,0,int(im.width*.78),im.height),(int(im.width*.22),0,im.width,im.height),(int(im.width*.08),int(im.height*.04),int(im.width*.92),int(im.height*.96)))):
                    if len(selected)>=4: break
                    view=im.crop(box)
                    p=Path(workdir)/f"verified_view_{len(selected)+1}_{idx}.jpg"
                    view.save(p,format="JPEG",quality=95,optimize=True)
                    ok,_=validate_image(p); fp=_sha(p) if ok else None
                    if ok and fp and fp not in seen: seen.add(fp); selected.append(p)
                    else: p.unlink(missing_ok=True)
            except Exception:
                pass
    if len(selected)<4:
        raise RuntimeError(f"IMAGE_SELECTION_FAILED: {len(selected)} verified product views available; 4 required even after local product-view synthesis")
    return selected[:5]

def _scene(image, out, mode):
    # Stable per-frame zoompan path. The input is first fitted to a modest 1200x2133
    # canvas, then zoompan emits an explicit 150 frames at 1080x1920.
    render_w,render_h=1200,2133
    if mode=="static":
        vf=f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},setsar=1"
        frames=SCENE_SECONDS*FPS
    else:
        if mode=="right":
            x="(iw-iw/zoom)*0.70"
        elif mode=="left":
            x="(iw-iw/zoom)*0.30"
        else:
            x="(iw-iw/zoom)/2"
        rotation = 0.022 if mode=="right" else -0.022
        vf=(f"scale={render_w}:{render_h}:force_original_aspect_ratio=increase,"
            f"crop={render_w}:{render_h},"
            f"zoompan=z='min(zoom+0.0008,1.12)':x='{x}':y='(ih-ih/zoom)/2':"
            f"d={SCENE_SECONDS*FPS}:s={WIDTH}x{HEIGHT}:fps={FPS},"
            f"rotate='{rotation}*sin(2*PI*t/{SCENE_SECONDS})':fillcolor=black,setsar=1")
        frames=SCENE_SECONDS*FPS
    _run(["ffmpeg","-y","-threads","1","-loop","1","-i",str(image),
          "-vf",vf,"-frames:v",str(frames),"-c:v","libx264","-preset","ultrafast","-crf","28","-threads","1",
          "-pix_fmt","yuv420p","-an",str(out)])

def render_video(image_urls, output_path, title="", music_path=None):
    output=Path(output_path); output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="video-render-") as td:
        images=download_and_validate(image_urls,td)
        scenes=[]
        modes=["right","left","right","left","static"]
        for i,img in enumerate(images):
            scene=Path(td)/f"scene_{i}.mp4"
            _scene(img,scene,modes[i])
            scenes.append(scene)
        concat=Path(td)/"concat.txt"
        concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in scenes))
        silent=Path(td)/"silent.mp4"
        _run(["ffmpeg","-y","-threads","1","-f","concat","-safe","0","-i",str(concat),
              "-c:v","libx264","-preset","ultrafast","-crf","28","-threads","1","-pix_fmt","yuv420p",
              "-movflags","+faststart",str(silent)])
        if music_path and Path(music_path).exists():
            _run(["ffmpeg","-y","-i",str(silent),"-stream_loop","-1","-i",str(music_path),
                  "-map","0:v:0","-map","1:a:0","-c:v","copy","-c:a","aac","-b:a","128k",
                  "-shortest","-movflags","+faststart",str(output)])
        else:
            shutil.copy2(silent,output)
        _quality_gate(output)
    return str(output)

def _quality_gate(path):
    probe=_run(["ffprobe","-v","error","-show_entries","format=duration,size",
                "-show_entries","stream=width,height,codec_name","-of","json",str(path)])
    data=json.loads(probe)
    streams=data.get("streams") or []
    video=next((x for x in streams if x.get("width")),None)
    if not video or int(video.get("width",0))!=WIDTH or int(video.get("height",0))!=HEIGHT:
        raise RuntimeError("VIDEO_QUALITY_FAILED: invalid dimensions")
    if video.get("codec_name")!="h264":
        raise RuntimeError("VIDEO_QUALITY_FAILED: invalid codec")
    duration=float((data.get("format") or {}).get("duration") or 0)
    if duration < 15:
        raise RuntimeError("VIDEO_QUALITY_FAILED: duration too short")
    if Path(path).stat().st_size < 100_000:
        raise RuntimeError("VIDEO_QUALITY_FAILED: output too small")

def affiliate_url(asin, tag=AMAZON_TAG):
    return f"https://www.amazon.com/dp/{quote(str(asin).upper(), safe='')}?tag={quote(tag, safe='')}"

def make_music(track_id, directory):
    """Download a real instrumental recording and return a local audio path.
    Track choice rotates deterministically across the 5-track public-domain set.
    We never synthesize beeps/tones as the background soundtrack.
    """
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    try:
        idx=int(''.join(ch for ch in str(track_id) if ch.isdigit()) or 1) % len(REAL_MUSIC_TRACKS)
    except Exception:
        idx=0
    for offset in range(len(REAL_MUSIC_TRACKS)):
        key,title,url=REAL_MUSIC_TRACKS[(idx+offset)%len(REAL_MUSIC_TRACKS)]
        source=directory/f"{key}.ogg"
        if not source.exists() or source.stat().st_size<50000:
            try:
                r=requests.get(url,headers={"User-Agent":"Mozilla/5.0 PinterestAgent/VideoMusic"},timeout=90,allow_redirects=True)
                r.raise_for_status()
                if len(r.content)<50000: raise RuntimeError("music file too small")
                source.write_bytes(r.content)
            except Exception:
                source.unlink(missing_ok=True)
                continue
        # Validate that this is actual decodable audio and long enough for a
        # 15-25s product video. ffprobe also prevents HTML/error pages from
        # being accepted as a music asset.
        try:
            probe=json.loads(_run(["ffprobe","-v","error","-show_entries","format=duration",
                                   "-of","json",str(source)]))
            duration=float((probe.get("format") or {}).get("duration") or 0)
            if duration<20: raise RuntimeError("music duration too short")
            log.info("Using real instrumental track: %s",title)
            return str(source)
        except Exception:
            source.unlink(missing_ok=True)
    raise RuntimeError("REAL_MUSIC_DOWNLOAD_FAILED:no verified instrumental track available")