"""Final runtime hardening applied after the legacy wiring layer.

The repository accumulated two publish wrappers: board_org's legacy section-aware
wrapper and wire_board_org's strict wrapper. The legacy wrapper captured the old
Composio runner before the per-job budget was installed, which could bypass the
40-call governor. This module replaces the final publish function with one
canonical implementation that always uses the currently-installed runner.
"""
from __future__ import annotations

import json
from typing import Any


def install(agent_mod: Any) -> str:
    """Install one canonical section-aware, budget-aware publish/verify function."""
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

        job_store.update(job_id, progress=f"Publishing Pin {pin_index}/5")
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

        # Always resolve the runner dynamically so wire_board_org's per-job
        # budget/governor remains authoritative.
        data = await agent_mod.run_composio_tool("PINTEREST_CREATE_PIN", args, retries=2)
        pin_id = str(
            data.get("id")
            or data.get("pin_id")
            or (data.get("data") or {}).get("id")
            or ""
        )
        if not pin_id:
            raise RuntimeError(f"Pin created but no ID: {json.dumps(data)[:400]}")

        # One independent GET is sufficient: it verifies both publication and
        # actual board/section placement without the old wrapper's duplicate GET.
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

        return {
            "pin_id": pin_id,
            "pin_url": f"https://www.pinterest.com/pin/{pin_id}/",
            "verified": True,
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
