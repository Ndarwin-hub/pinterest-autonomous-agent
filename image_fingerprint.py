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
