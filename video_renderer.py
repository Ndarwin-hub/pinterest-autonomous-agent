from __future__ import annotations
import hashlib, json, os, shutil, subprocess, tempfile
from pathlib import Path
from urllib.parse import quote
import requests

WIDTH, HEIGHT, FPS = 1080, 1920, 30
SCENE_SECONDS = 5
MIN_IMAGE_SIDE = 600
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
    selected=[]; seen=set()
    for url in urls:
        if not url or not str(url).startswith(("http://","https://")):
            continue
        try:
            r=requests.get(url,timeout=30,stream=True,headers={"User-Agent":"Mozilla/5.0"})
            r.raise_for_status()
            ctype=(r.headers.get("content-type") or "").lower()
            if "image" not in ctype:
                continue
            ext=".jpg"
            if "png" in ctype: ext=".png"
            elif "webp" in ctype: ext=".webp"
            p=Path(workdir)/f"image_{len(selected)+1}{ext}"
            with open(p,"wb") as f:
                for chunk in r.iter_content(1024*1024):
                    if chunk: f.write(chunk)
            ok,reason=validate_image(p)
            if not ok:
                p.unlink(missing_ok=True); continue
            fp=_sha(p)
            if fp in seen:
                p.unlink(missing_ok=True); continue
            seen.add(fp); selected.append(p)
        except Exception:
            continue
    if len(selected)<4:
        raise RuntimeError(f"IMAGE_SELECTION_FAILED: {len(selected)} valid unique Pinterest-associated images; 4 required")
    return selected[:5]

def _scene(image, out, mode):
    # zoompan is the same FFmpeg motion family used by the recovered Kaggle renderer.
    if mode=="static":
        vf=f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},setsar=1"
    else:
        x="(iw-iw/zoom)/2"
        if mode=="right": x="(iw-iw/zoom)*0.70"
        elif mode=="left": x="(iw-iw/zoom)*0.30"
        vf=(f"scale={WIDTH*2}:{HEIGHT*2}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH*2}:{HEIGHT*2},zoompan=z='min(zoom+0.0015,1.12)':"
            f"x='{x}':y='(ih-ih/zoom)/2':d={SCENE_SECONDS*FPS}:"
            f"s={WIDTH}x{HEIGHT}:fps={FPS},setsar=1")
    _run(["ffmpeg","-y","-loop","1","-i",str(image),"-t",str(SCENE_SECONDS),
          "-vf",vf,"-r",str(FPS),"-pix_fmt","yuv420p","-an",str(out)])

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
        _run(["ffmpeg","-y","-f","concat","-safe","0","-i",str(concat),
              "-c:v","libx264","-preset","veryfast","-crf","20","-pix_fmt","yuv420p",
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
    directory=Path(directory); directory.mkdir(parents=True,exist_ok=True)
    out=directory/f"{track_id}.wav"
    if out.exists() and out.stat().st_size>10000:
        return str(out)
    idx=int(''.join(ch for ch in str(track_id) if ch.isdigit()) or 1)
    base=160+idx*17
    expr=f"0.12*sin(2*PI*{base}*t)+0.08*sin(2*PI*{base*1.25:.2f}*t)+0.06*sin(2*PI*{base*1.5:.2f}*t)"
    _run(["ffmpeg","-y","-f","lavfi","-i",f"aevalsrc={expr}:s=44100:d=30",
          "-af","lowpass=f=6500,afade=t=in:st=0:d=1,afade=t=out:st=27:d=3",
          "-c:a","pcm_s16le",str(out)])
    return str(out)
