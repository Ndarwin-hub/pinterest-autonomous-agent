"""Perceptual image fingerprinting and visual-diversity selection.

Purpose:
- reject exact/near-identical image variants within a product's four Pins;
- prefer genuinely different views/angles/compositions of the same product;
- never treat merely similar products as duplicate images.
"""
from __future__ import annotations

import io
from typing import Any, Dict, Iterable, List, Optional, Tuple

from PIL import Image, ImageOps, ImageStat


def _load(data: bytes) -> Optional[Image.Image]:
    try:
        im = Image.open(io.BytesIO(data)).convert("RGB")
        return ImageOps.exif_transpose(im)
    except Exception:
        return None


def _hashes(im: Image.Image) -> Tuple[int, int]:
    gray = ImageOps.grayscale(im)
    # Stable perceptual average hash.
    small = gray.resize((16, 16), Image.Resampling.LANCZOS)
    px = list(small.getdata())
    avg = sum(px) / len(px)
    ah = 0
    for v in px:
        ah = (ah << 1) | int(v >= avg)

    # dHash captures edge/angle/composition changes better than URL/hash checks.
    dh_im = gray.resize((17, 16), Image.Resampling.LANCZOS)
    dp = list(dh_im.getdata())
    dh = 0
    for y in range(16):
        row = dp[y * 17 : (y + 1) * 17]
        for x in range(16):
            dh = (dh << 1) | int(row[x + 1] >= row[x])
    return ah, dh


def _hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def fingerprint(data: bytes) -> Optional[Dict[str, Any]]:
    im = _load(data)
    if im is None:
        return None
    ah, dh = _hashes(im)
    # Low-resolution color signature helps distinguish different product angles
    # that happen to have similar silhouettes.
    thumb = im.resize((8, 8), Image.Resampling.BILINEAR)
    stat = ImageStat.Stat(thumb)
    color = tuple(round(float(x), 1) for x in stat.mean)
    return {"ahash": ah, "dhash": dh, "color": color}


def similarity(a: Dict[str, Any], b: Dict[str, Any]) -> float:
    if not a or not b:
        return 0.0
    ah = 1.0 - _hamming(int(a["ahash"]), int(b["ahash"])) / 256.0
    dh = 1.0 - _hamming(int(a["dhash"]), int(b["dhash"])) / 256.0
    ca = a.get("color") or (0, 0, 0)
    cb = b.get("color") or (0, 0, 0)
    cd = sum(abs(float(x) - float(y)) for x, y in zip(ca, cb)) / 765.0
    color_similarity = max(0.0, 1.0 - cd)
    return max(0.0, min(1.0, 0.45 * ah + 0.45 * dh + 0.10 * color_similarity))


def is_near_duplicate(candidate: Dict[str, Any], previous: Iterable[Dict[str, Any]], threshold: float = 0.93) -> bool:
    fp = candidate.get("_fingerprint")
    if not fp:
        return False
    return any(similarity(fp, old) >= threshold for old in previous if old)


def diversity_bonus(candidate: Dict[str, Any], previous: List[Dict[str, Any]]) -> float:
    """Reward a genuinely different angle/composition without punishing resolution."""
    fp = candidate.get("_fingerprint")
    if not fp or not previous:
        return 0.0
    sims = [similarity(fp, old) for old in previous if old]
    if not sims:
        return 0.0
    # Lower similarity to prior images means greater diversity.
    return max(0.0, 20.0 * (1.0 - max(sims)))


def attach_fingerprint(candidate: Dict[str, Any], data: bytes | None) -> Dict[str, Any]:
    out = dict(candidate)
    if data:
        fp = fingerprint(data)
        if fp:
            out["_fingerprint"] = fp
    return out


# Durable visual registry: content fingerprints, not URLs, are the identity key.
import os as _os, sqlite3 as _sqlite3, threading as _threading
from pathlib import Path as _Path
from datetime import datetime as _datetime, timezone as _timezone
_FP_DB = _Path(_os.getenv("DATA_DIR", "/data" if _Path("/data").exists() else "/tmp")) / "pinterest_visual_fingerprints.db"
_FP_LOCK = _threading.Lock()

def _fp_conn():
    _FP_DB.parent.mkdir(parents=True, exist_ok=True)
    c = _sqlite3.connect(str(_FP_DB), timeout=30, check_same_thread=False)
    c.execute("PRAGMA busy_timeout=30000")
    return c

def _fp_key(fp: Dict[str, Any]) -> str:
    return f"{int(fp.get('ahash',0)):064x}:{int(fp.get('dhash',0)):064x}:{','.join(str(x) for x in (fp.get('color') or ())) }"

def register_fingerprint(fp: Dict[str, Any], *, pin_id: str = "", job_id: str = "", asin: str = "") -> None:
    if not fp:
        return
    with _FP_LOCK:
        c = _fp_conn()
        c.execute("CREATE TABLE IF NOT EXISTS visual_fingerprints(id INTEGER PRIMARY KEY,fp_key TEXT UNIQUE NOT NULL,pin_id TEXT,job_id TEXT,asin TEXT,created_at TEXT NOT NULL)")
        c.execute("INSERT OR IGNORE INTO visual_fingerprints(fp_key,pin_id,job_id,asin,created_at) VALUES(?,?,?,?,?)",
                  (_fp_key(fp), str(pin_id or ""), str(job_id or ""), str(asin or ""), _datetime.now(_timezone.utc).isoformat()))
        c.commit(); c.close()

def known_fingerprints(limit: int = 5000) -> List[Dict[str, Any]]:
    with _FP_LOCK:
        c = _fp_conn()
        c.execute("CREATE TABLE IF NOT EXISTS visual_fingerprints(id INTEGER PRIMARY KEY,fp_key TEXT UNIQUE NOT NULL,pin_id TEXT,job_id TEXT,asin TEXT,created_at TEXT NOT NULL)")
        rows = c.execute("SELECT fp_key FROM visual_fingerprints ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()
        c.close()
    out=[]
    for (key,) in rows:
        try:
            ah,dh,color=key.split(":",2)
            out.append({"ahash":int(ah,16),"dhash":int(dh,16),"color":tuple(float(x) for x in color.split(",") if x)})
        except Exception:
            continue
    return out
