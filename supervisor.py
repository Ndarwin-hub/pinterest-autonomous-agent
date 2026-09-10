"""AI supervisor layer for the existing Pinterest agent.

This module wraps the existing workflow instead of replacing its publisher,
image pipeline, board logic, or Composio execution path.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Dict, List, Optional

import agent as core

logger = logging.getLogger("pinterest-agent.supervisor")
_SUPERVISOR_LOCK = asyncio.Lock()

MASTER_PIN_PROTOCOL = r"""
You are an autonomous Pinterest supervisor operating the authoritative Pin workflow.
Given one product URL, create exactly 5 genuinely different Pinterest Pins.
Preserve the submitted product URL byte-for-byte as the immutable Pinterest destination.
Never shorten, rewrite, canonicalize, append, remove, or replace URL parameters.
Use authentic product information; never invent product facts.
Use the existing board/image/publishing machinery supplied by the application.
Every Pin must pass product accuracy, URL integrity, Pinterest quality, SEO,
uniqueness, and safety checks before publication.
If a check cannot be performed reliably, fail closed rather than claiming success.
Never claim an AI/tool/source participated unless it actually executed.
The application owns state, retries, publication, and verification.
""".strip()

PROVIDERS = [
    "grok",
    "gemini",
    "deepseek",
    "claude",
]


def _text_from_result(data: Any) -> str:
    if isinstance(data, str):
        return data
    if not isinstance(data, dict):
        return str(data)
    for key in ("text", "output_text", "content", "message", "response"):
        val = data.get(key)
        if isinstance(val, str):
            return val
        if isinstance(val, list):
            chunks = []
            for item in val:
                if isinstance(item, dict):
                    t = item.get("text") or item.get("content")
                    if isinstance(t, str):
                        chunks.append(t)
            if chunks:
                return "\n".join(chunks)
    nested = data.get("data")
    if nested is not None and nested is not data:
        return _text_from_result(nested)
    return json.dumps(data, ensure_ascii=False)


def _extract_json(text: str) -> Optional[Any]:
    text = text.strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    m = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S | re.I)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    for opener, closer in (("[", "]"), ("{", "}")):
        a, b = text.find(opener), text.rfind(closer)
        if a >= 0 and b > a:
            try:
                return json.loads(text[a:b + 1])
            except Exception:
                continue
    return None


async def _call_provider(provider: str, prompt: str) -> str:
    if provider == "grok":
        data = await core.run_composio_tool(
            "GROK_CREATE_RESPONSE",
            {"model": "grok-4-1-fast-reasoning", "input": prompt},
            retries=0,
        )
        return _text_from_result(data)

    if provider == "gemini":
        data = await core.run_composio_tool(
            "GEMINI_GENERATE_CONTENT",
            {
                "model": "gemini-2.5-flash",
                "prompt": prompt,
                "max_output_tokens": 4000,
                "temperature": 0.2,
            },
            retries=0,
        )
        return _text_from_result(data)

    if provider == "deepseek":
        data = await core.run_composio_tool(
            "DEEPSEEK_CREATE_CHAT_COMPLETION",
            {
                "model": "deepseek-chat",
                "messages": [
                    {"role": "system", "content": MASTER_PIN_PROTOCOL},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.2,
            },
            retries=0,
        )
        return _text_from_result(data)

    if provider == "claude":
        data = await core.run_composio_tool(
            "ANTHROPIC_ADMINISTRATOR_CREATE_MESSAGE",
            {
                "model": "claude-3-5-haiku-latest",
                "max_tokens": 4000,
                "temperature": 0.2,
                "messages": [{"role": "user", "content": MASTER_PIN_PROTOCOL + "\n\n" + prompt}],
            },
            retries=0,
        )
        return _text_from_result(data)

    raise RuntimeError(f"Unknown supervisor provider: {provider}")


async def ask_with_failover(prompt: str, start_provider: Optional[str] = None):
    order = list(PROVIDERS)
    if start_provider in order:
        order = order[order.index(start_provider):]
    failures = []
    for provider in order:
        try:
            text = await _call_provider(provider, prompt)
            if text and text.strip():
                return provider, text, failures
        except Exception as exc:
            failures.append({"provider": provider, "error": str(exc)[:300]})
            logger.warning("Supervisor %s unavailable: %s", provider, exc)
    return None, None, failures


async def generate_five_pins(product: Dict[str, Any], fallback: List[Dict[str, str]]):
    prompt = f"""
{MASTER_PIN_PROTOCOL}

Create exactly five Pinterest Pin records for this product.
Return ONLY a JSON array of exactly five objects with keys:
title, description, keywords, alt_text, strategy.
The five strategies must be materially different: hero, problem/solution,
benefit, audience/use-case, discovery/inspiration.
Do not include URLs in any field. Do not invent specifications.

PRODUCT:
{json.dumps({k: product.get(k) for k in ('name','description','brand','category','site','source_url')}, ensure_ascii=False)}

BASELINE:
{json.dumps(fallback, ensure_ascii=False)}
""".strip()
    provider, text, failures = await ask_with_failover(prompt)
    if not text:
        return fallback, {"provider": None, "failures": failures, "mode": "local_fallback"}
    parsed = _extract_json(text)
    if not isinstance(parsed, list) or len(parsed) != 5:
        return fallback, {"provider": provider, "failures": failures, "mode": "local_fallback_invalid_ai_output"}
    out = []
    for i, item in enumerate(parsed):
        if not isinstance(item, dict):
            return fallback, {"provider": provider, "failures": failures, "mode": "local_fallback_invalid_record"}
        out.append({
            "title": str(item.get("title") or fallback[i]["title"])[:100],
            "description": str(item.get("description") or fallback[i]["description"])[:800],
            "keywords": str(item.get("keywords") or fallback[i].get("keywords") or "")[:500],
            "alt_text": str(item.get("alt_text") or fallback[i].get("alt_text") or "")[:500],
            "strategy": str(item.get("strategy") or fallback[i].get("strategy") or "")[:100],
            "strategy_key": fallback[i].get("strategy_key"),
        })
    return out, {"provider": provider, "failures": failures, "mode": "ai"}


async def review_pin(product: Dict[str, Any], seo: Dict[str, Any], image: Dict[str, Any], source_url: str, index: int):
    if source_url != product.get("source_url"):
        return False, "Immutable source URL mismatch", None
    if not seo.get("title") or not seo.get("description"):
        return False, "Missing Pin text", None
    if image.get("mode") not in ("url", "base64") or not image.get("value"):
        return False, "Missing image", None

    prompt = f"""
{MASTER_PIN_PROTOCOL}

Review ONE Pinterest Pin before publication. Return ONLY JSON:
{{"approve": true/false, "issues": ["..."], "reason": "..."}}

Check product relevance, factual safety, title/description quality, SEO,
Pinterest suitability, and uniqueness context. If visual pixels are not directly
available to you, explicitly say so and do not claim visual inspection occurred.

PIN #{index}
PRODUCT: {json.dumps(product, ensure_ascii=False)}
PIN TEXT: {json.dumps(seo, ensure_ascii=False)}
IMAGE METADATA: {json.dumps(image, ensure_ascii=False)}
DESTINATION URL: {source_url}
""".strip()
    provider, text, failures = await ask_with_failover(prompt)
    if not text:
        return False, "No executable AI reviewer available", {"provider": None, "failures": failures}
    parsed = _extract_json(text)
    if not isinstance(parsed, dict):
        return False, "AI reviewer returned invalid output", {"provider": provider, "failures": failures}
    approved = bool(parsed.get("approve"))
    issues = parsed.get("issues") or []
    reason = str(parsed.get("reason") or "")[:500]
    return approved, "; ".join(map(str, issues))[:800] or reason, {"provider": provider, "failures": failures, "reason": reason}


_original_build = core.build_five_seo
_original_publish = core.publish_and_verify


def install_wrappers(ai_meta: Dict[str, Any], generated: List[Dict[str, str]], product: Dict[str, Any]):
    def wrapped_build(_product):
        return generated

    async def wrapped_publish(*args, **kwargs):
        link = kwargs.get("link")
        title = kwargs.get("title")
        description = kwargs.get("description")
        alt_text = kwargs.get("alt_text")
        image_mode = kwargs.get("image_mode")
        image_value = kwargs.get("image_value")
        job_store = kwargs.get("job_store")
        job_id = kwargs.get("job_id")
        pin_index = kwargs.get("pin_index")

        if not link:
            raise RuntimeError("Publication blocked: missing immutable destination URL")
        job = job_store.get(job_id)
        source_url = job.url if job else link
        if link != source_url:
            raise RuntimeError("Publication blocked: destination URL differs from original submitted URL")

        image = {"mode": image_mode, "value": image_value, "provider": "unknown"}
        approved, reason, meta = await review_pin(
            product,
            {"title": title, "description": description, "alt_text": alt_text},
            image,
            source_url,
            pin_index,
        )
        ai_meta["reviews"].append({"pin": pin_index, "approved": approved, "reason": reason, "meta": meta})
        if not approved:
            raise RuntimeError(f"AI quality gate rejected Pin {pin_index}: {reason}")
        return await _original_publish(*args, **kwargs)

    core.build_five_seo = wrapped_build
    core.publish_and_verify = wrapped_publish


def restore_wrappers():
    core.build_five_seo = _original_build
    core.publish_and_verify = _original_publish


async def process_supervised(job_id: str, url: str, job_store) -> Dict[str, Any]:
    """Run the existing process with AI generation/review layered around it."""
    product = await core.research_product(url, job_store, job_id)
    baseline = _original_build(product)
    generated, generation_meta = await generate_five_pins(product, baseline)
    ai_meta: Dict[str, Any] = {"generation": generation_meta, "reviews": []}

    async with _SUPERVISOR_LOCK:
        install_wrappers(ai_meta, generated, product)
        try:
            result = await core.process_pinterest_job(job_id, url, job_store)
            result["supervisor"] = ai_meta
            result["supervisor_policy"] = "Grok -> Gemini -> DeepSeek -> Claude; fail closed if no executable reviewer."
            return result
        finally:
            restore_wrappers()
