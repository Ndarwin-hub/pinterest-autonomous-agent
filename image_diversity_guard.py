"""Final image-diversity gate for the four-Pin workflow.

This boundary validates actual image bytes and compares content fingerprints;
URL equality is only an additional fast check. No placeholder fallback exists.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


def normalize_media_url(url: str) -> str:
    p = urlsplit((url or "").strip())
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, "", ""))


def install(agent_module):
    original = agent_module.get_best_pin_image

    async def guarded_get_best_pin_image(product, strategy, pin_index, job_store, job_id, used_urls, **kwargs):
        image = await original(
            product, strategy, pin_index, job_store, job_id, used_urls, **kwargs
        )
        value = str(image.get("value") or "")
        from image_quality import validate_base64_image, inspect_image_content
        from image_fingerprint import similarity
        checked = await validate_base64_image(value) if image.get("mode") == "base64" else await inspect_image_content(value)
        if not checked:
            raise RuntimeError("Image diversity gate blocked an invalid final image asset.")
        image.update(checked, content_gate="passed")
        canonical = normalize_media_url(value)
        prior = {normalize_media_url(str(u)) for u in used_urls if str(u).startswith("http")}
        if image.get("mode") == "url" and canonical in prior:
            raise RuntimeError("Image diversity gate blocked a repeated media URL.")
        fp=checked.get("_fingerprint")
        history=getattr(agent_module,"_pin_image_fingerprints",{}).get(job_id,[])
        if fp and any(similarity(fp,old)>=0.93 for old in history):
            raise RuntimeError("Image diversity gate blocked a visually duplicated asset.")
        if fp: history.append(fp)
        if image.get("mode") == "url": used_urls.add(value)
        return image

    agent_module.get_best_pin_image = guarded_get_best_pin_image
