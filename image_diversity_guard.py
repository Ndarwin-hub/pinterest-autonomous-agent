"""Final image-diversity gate for the five-Pin workflow.

The existing selector already tries to avoid reused URLs. This final boundary adds
an independent normalized-URL check so a provider/CDN variant cannot silently
reuse the same asset for multiple Pins. When a duplicate is returned, use the
existing strategy-specific Pillow creative rather than publishing the same asset.
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
        if image.get("mode") != "url":
            return image

        value = str(image.get("value") or "")
        canonical = normalize_media_url(value)
        prior = {normalize_media_url(str(u)) for u in used_urls if str(u).startswith("http")}
        if canonical in prior:
            # The core selector has already attempted diversity; fail closed here.
            # pillow_card is strategy-specific and therefore cannot be the same
            # visual asset as another selected URL.
            fallback = agent_module.pillow_card(product, strategy["key"])
            fallback["diversity_fallback"] = "normalized_duplicate_media_url"
            return fallback
        used_urls.add(value)
        return image

    agent_module.get_best_pin_image = guarded_get_best_pin_image
