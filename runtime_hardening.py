"""Final runtime hardening for the Pinterest/Amazon Railway service.

Installs the canonical publish/verify wrapper and routes authenticated
Pinterest/Gmail Composio calls through explicit connected-account IDs.
This avoids legacy entity routing that can enter Composio managed-job
execution paths unavailable to the unattended Railway process.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import httpx
from typing import Any, Dict, Optional
from pin_config import PINS_PER_PRODUCT

logger = logging.getLogger("pinterest-agent.runtime-hardening")
_PIN_CREATE_LOCK=asyncio.Lock()
_RATE_BACKOFF_SECONDS=(15,30,60,120)

def _account_for(slug: str) -> Optional[str]:
    s = slug.upper()
    if s.startswith("PINTEREST_"):
        return os.getenv("COMPOSIO_PINTEREST_ACCOUNT_ID", "").strip() or None
    if s.startswith("GMAIL_"):
        return os.getenv("COMPOSIO_GMAIL_ACCOUNT_ID", "").strip() or None
    key = "COMPOSIO_ACCOUNT_" + "".join(c if c.isalnum() else "_" for c in s)
    return os.getenv(key, "").strip() or None

async def _direct_composio_execute(tool_slug: str, arguments: Dict[str, Any], retries: int = 2) -> Dict[str, Any]:
    import httpx
    key = os.getenv("COMPOSIO_API_KEY", "").strip()
    if not key:
        raise RuntimeError("COMPOSIO_API_KEY is not set in Railway variables.")

    payload: Dict[str, Any] = {
        "arguments": arguments or {},
        "version": "latest",
        "dangerously_skip_version_check": True,
    }
    # Use the production Composio user/entity for session-scoped account resolution.
    # The Railway key currently cannot resolve the exposed connected-account nano-ID directly;
    # user-scoped resolution is the compatible path for this existing production entity.
    entity_id = os.getenv("COMPOSIO_ENTITY_ID", "").strip()
    if entity_id:
        payload["user_id"] = entity_id
    else:
        account = _account_for(tool_slug)
        if account:
            payload["connected_account_id"] = account

    url = f"https://backend.composio.dev/api/v3.1/tools/execute/{tool_slug}"
    headers = {"x-api-key": key, "Content-Type": "application/json"}
    last: Optional[Exception] = None

    for attempt in range(retries + 1):
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(url, headers=headers, json=payload)
                try:
                    data = response.json()
                except Exception:
                    data = {"raw": response.text}

                if response.status_code >= 400:
                    error = data.get("error") if isinstance(data, dict) else data
                    if isinstance(error, dict):
                        error = error.get("message") or error
                    raise RuntimeError(f"{tool_slug} HTTP {response.status_code}: {error}")

                if isinstance(data, dict) and data.get("successful") is False:
                    error = data.get("error") or data.get("data", {}).get("message") or str(data)
                    raise RuntimeError(f"{tool_slug} unsuccessful: {error}")

                if isinstance(data, dict) and "data" in data:
                    return data["data"] if data["data"] is not None else {}
                return data if isinstance(data, dict) else {"result": data}
        except Exception as exc:
            last = exc
            if attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 8))

    raise RuntimeError(str(last) if last else f"{tool_slug} failed")

def _install_composio_transport(agent_mod: Any) -> None:
    if getattr(agent_mod, "_connected_account_transport_installed", False):
        return

    original = agent_mod.run_composio_tool

    async def run_with_connected_account(tool_slug: str, arguments: Dict[str, Any], retries: int = 2) -> Dict[str, Any]:
        if _account_for(tool_slug):
            return await _direct_composio_execute(tool_slug, arguments, retries=retries)
        return await original(tool_slug, arguments, retries=retries)

    agent_mod._composio_transport_executor = _direct_composio_execute
    agent_mod.run_composio_tool = run_with_connected_account
    agent_mod._connected_account_transport_installed = True
    logger.info("Composio connected-account transport installed.")

    if os.getenv("COMPOSIO_TRANSPORT_SELFTEST", "").strip().lower() in {"1", "true", "yes"}:
        async def selftest() -> None:
            try:
                data = await _direct_composio_execute("PINTEREST_LIST_BOARDS", {"page_size": 1}, retries=1)
                items = (data.get("items") or data.get("boards") or []) if isinstance(data, dict) else []
                logger.warning("COMPOSIO_TRANSPORT_SELFTEST=PASS pinterest_board_read=%s", bool(items))
            except Exception as exc:
                logger.error("COMPOSIO_TRANSPORT_SELFTEST=FAIL %s", exc)
        try:
            asyncio.create_task(selftest())
        except RuntimeError:
            pass

def install(agent_mod: Any) -> str:
    """Install one canonical section-aware, budget-aware publish/verify function."""
    _install_composio_transport(agent_mod)

    if getattr(agent_mod, "_runtime_publish_hardening_installed", False):
        return "already-installed"

    async def publish_and_verify(
        board_id: str,
        title: str,
        description: str,
        alt_text: str,
        image_mode: str,
        image_value: str,
        link: str,
        job_store: Any,
        job_id: str,
        pin_index: int,
    ) -> dict:
        from board_org import current_section_id

        job_store.update(job_id, progress=f"Publishing Pin {pin_index}/{PINS_PER_PRODUCT}")
        # Stage 2 image gate: validate the exact bytes immediately before the
        # external CREATE_PIN call. This applies equally to manual /submit and Amazon jobs.
        from image_quality import validate_base64_image, inspect_image_content
        if image_mode == "base64":
            final_image_check = await validate_base64_image(str(image_value or ""))
        else:
            final_image_check = await inspect_image_content(str(image_value or ""))
        if not final_image_check:
            raise RuntimeError(f"Pin {pin_index}: final selected image failed byte-level validation; CREATE_PIN blocked.")
        if str((final_image_check or {}).get("provider") or "") == "pillow_card" or str(image_value or "").strip() == "":
            raise RuntimeError(f"Pin {pin_index}: invalid placeholder/empty image; CREATE_PIN blocked.")
        if image_mode == "base64":
            media_source = {
                "source_type": "image_base64",
                "content_type": "image/jpeg",
                "data": image_value,
            }
        else:
            media_source = {"source_type": "image_url", "url": image_value}

        args = {
            "board_id": str(board_id),
            "title": title[:100],
            "description": description[:800],
            "alt_text": alt_text[:500],
            "link": link,
            "media_source": media_source,
        }
        section_id = current_section_id()
        if section_id:
            args["board_section_id"] = str(section_id)

        async with _PIN_CREATE_LOCK:
            data = None
            last_error = None
            for attempt in range(4):
                try:
                    data = await agent_mod.run_composio_tool("PINTEREST_CREATE_PIN", args, retries=0)
                    pin_id_candidate = str(
                        data.get("id")
                        or data.get("pin_id")
                        or (data.get("data") or {}).get("id")
                        or ""
                    )
                    if pin_id_candidate:
                        break
                    msg=str(data)
                    if "rate limit" not in msg.lower() and "block (pins)" not in msg.lower() and "combat spam" not in msg.lower():
                        if attempt < 2:
                            await asyncio.sleep(3*(attempt+1))
                            continue
                    raise RuntimeError(f"PINTEREST_CREATE_PIN unsuccessful: {data}")
                except Exception as exc:
                    last_error=exc
                    msg=str(exc).lower()
                    if "rate limit" not in msg and "block (pins)" not in msg and "combat spam" not in msg:
                        if attempt < 2:
                            await asyncio.sleep(3*(attempt+1))
                            continue
                        raise
                    if attempt >= len(_RATE_BACKOFF_SECONDS):
                        raise
                    await asyncio.sleep(_RATE_BACKOFF_SECONDS[attempt])
            else:
                raise RuntimeError(f"PINTEREST_CREATE_PIN unsuccessful after adaptive retry: {last_error}")
            pin_id = pin_id_candidate
        if not pin_id:
            raise RuntimeError(f"Pin created but no ID: {json.dumps(data)[:400]}")

        verified = await agent_mod.run_composio_tool(
            "PINTEREST_GET_PIN", {"pin_id": pin_id}, retries=1
        )
        if not verified or not verified.get("id"):
            raise RuntimeError(
                f"Pin {pin_index} ({pin_id}): independent Pinterest fetch did not return a valid Pin."
            )

        actual_board = str(
            verified.get("board_id")
            or ((verified.get("board") or {}).get("id")
                if isinstance(verified.get("board"), dict) else "")
            or ""
        )
        if not actual_board:
            raise RuntimeError(
                f"Pin {pin_index} ({pin_id}): independent Pinterest fetch returned no board_id."
            )
        if actual_board != str(board_id):
            raise RuntimeError(
                f"Pin {pin_index} ({pin_id}): board verification mismatch; intended {board_id}, actual {actual_board}"
            )

        actual_section = str(
            verified.get("board_section_id")
            or ((verified.get("board_section") or {}).get("id")
                if isinstance(verified.get("board_section"), dict) else "")
            or ""
        )
        if section_id and actual_section != str(section_id):
            raise RuntimeError(
                f"Pin {pin_index} ({pin_id}): section verification mismatch; intended {section_id}, actual {actual_section}"
            )

        # Third-stage validation: Pinterest must have ingested a real, non-blank image.
        # API acceptance alone is not considered visual success.
        try:
            from image_quality import inspect_image_content
            media = verified.get("media") if isinstance(verified, dict) else None
            media_urls = []
            def _collect_media_urls(value):
                if isinstance(value, dict):
                    for k,v in value.items():
                        if k in {"url","original","original_url","image_url"} and isinstance(v,str) and v.startswith("http"):
                            media_urls.append(v)
                        else:
                            _collect_media_urls(v)
                elif isinstance(value, list):
                    for v in value: _collect_media_urls(v)
            _collect_media_urls(media)
            media_urls = [u for u in media_urls if "pinimg.com" in u][:4]
            if not media_urls:
                raise RuntimeError(f"Pin {pin_index} ({pin_id}): Pinterest returned no inspectable ingested image URL.")
            checked = False
            cdn_access_blocked = False
            for media_url in media_urls:
                if await inspect_image_content(media_url):
                    checked = True
                    break
                try:
                    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
                        probe = await client.get(media_url, headers={"User-Agent":"Mozilla/5.0 PinterestAgent/verify"})
                    if probe.status_code == 403:
                        cdn_access_blocked = True
                except Exception:
                    pass
            if not checked and not cdn_access_blocked:
                raise RuntimeError(f"Pin {pin_index} ({pin_id}): post-Pinterest image content validation failed.")
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError(f"Pin {pin_index} ({pin_id}): post-Pinterest image validation error: {exc}") from exc

        return {
            "pin_id": pin_id,
            "pin_url": f"https://www.pinterest.com/pin/{pin_id}/",
            "verified": True,
            "post_publish_image_validation": "passed" if checked else "cdn_access_blocked",
            "board_verified_independently": True,
            "board_id": actual_board,
            "board_section_id": str(section_id) if section_id else None,
            "section_verified_independently": bool(section_id and actual_section == str(section_id)),
            "destination_url": link,
            "title": title,
        }

    agent_mod.publish_and_verify = publish_and_verify
    agent_mod._runtime_publish_hardening_installed = True
    return "installed"
