"""Runtime wiring for board selection plus production call-budget enforcement.

This module keeps the existing agent workflow intact while enforcing a hard
per-job Composio ceiling. Capability probes that previously spent live tool
calls are reported without executing paid/limited tools. Image-search retries
are collapsed to one real search per Pin; Pexels probing is skipped so the
mandatory board + 5 publish + 5 verify path remains comfortably below 22.
"""
from __future__ import annotations

import contextvars
import logging
from typing import Any, Dict

from board_org import detect_product_category, preferred_board_name, find_matching_board

logger = logging.getLogger("pinterest-agent.wire")

MAX_COMPOSIO_CALLS = 22
_call_budget: contextvars.ContextVar["CallBudget | None"] = contextvars.ContextVar("pinterest_call_budget", default=None)


class CallBudget:
    def __init__(self, maximum: int = MAX_COMPOSIO_CALLS):
        self.maximum = maximum
        self.used = 0
        self.image_search_invocations = 0

    def reserve(self, tool_slug: str) -> None:
        if self.used >= self.maximum:
            raise RuntimeError(
                f"Composio hard job budget exhausted ({self.maximum} calls); stopping safely before another tool call."
            )
        self.used += 1
        logger.info("Composio budget: %s/%s (%s)", self.used, self.maximum, tool_slug)


async def _static_capabilities(agent_mod: Any) -> Dict[str, Any]:
    """Return capability metadata without spending Composio calls on probes."""
    return {
        "pexels": {
            "connected": False,
            "executable": False,
            "production_tested": False,
            "kind": "image_search",
            "reason": "Runtime probe skipped to protect the 22-call job budget.",
        },
        "deepseek": {
            "connected": False,
            "executable": False,
            "production_tested": False,
            "kind": "text",
            "reason": "Runtime probe skipped to protect the 22-call job budget.",
        },
        "perplexity": {
            "connected": False,
            "executable": False,
            "production_tested": False,
            "kind": "text",
            "reason": "Runtime probe skipped to protect the 22-call job budget.",
        },
        "composio_search_image": {
            "connected": bool(getattr(agent_mod, "COMPOSIO_API_KEY", "")),
            "executable": bool(getattr(agent_mod, "COMPOSIO_API_KEY", "")),
            "production_tested": False,
            "kind": "image_search",
            "reason": "Used directly during each Pin when needed; no separate probe call.",
        },
        "pinterest": {
            "connected": True,
            "executable": True,
            "production_tested": True,
            "kind": "publish",
            "reason": "Existing verified pipeline.",
        },
        "openai_images": {
            "connected": bool(getattr(agent_mod, "OPENAI_API_KEY", "")),
            "executable": bool(getattr(agent_mod, "OPENAI_API_KEY", "")),
            "production_tested": False,
            "kind": "image_generation",
            "reason": "Environment key presence only; no probe call.",
        },
        "pixabay": {
            "connected": bool(getattr(agent_mod, "PIXABAY_API_KEY", "")),
            "executable": bool(getattr(agent_mod, "PIXABAY_API_KEY", "")),
            "kind": "image_search",
            "reason": "Environment key presence only; no probe call.",
        },
        "unsplash": {
            "connected": bool(getattr(agent_mod, "UNSPLASH_ACCESS_KEY", "")),
            "executable": bool(getattr(agent_mod, "UNSPLASH_ACCESS_KEY", "")),
            "kind": "image_search",
            "reason": "Environment key presence only; no probe call.",
        },
        "gemini_image": {
            "connected": True,
            "executable": False,
            "kind": "image_generation",
            "reason": "Restricted in this environment.",
        },
        "pillow": {
            "connected": True,
            "executable": True,
            "kind": "emergency_fallback",
            "reason": "Last resort only.",
        },
    }


def apply_agent_wiring(agent_mod: Any) -> None:
    """Patch agent module globals used by process_pinterest_job at call time."""
    orig_research = agent_mod.research_product
    orig_run_composio_tool = agent_mod.run_composio_tool
    orig_process = agent_mod.process_pinterest_job
    default_board = getattr(agent_mod, "DEFAULT_BOARD_NAME", "Product Pins")

    async def budgeted_run_composio_tool(
        tool_slug: str, arguments: Dict[str, Any], retries: int = 2
    ) -> Dict[str, Any]:
        budget = _call_budget.get()
        if budget is None:
            # Any unexpected direct execution outside a managed job is denied;
            # this prevents a hidden bypass around the job ceiling.
            raise RuntimeError("Composio execution attempted outside a managed job budget.")

        # Pexels is optional. Do not spend a Composio call probing/fetching it;
        # the existing product-page/search/Pillow image path remains available.
        if tool_slug == "PEXELS_SEARCH_PHOTOS":
            return {}

        # The existing image function can try up to three search queries per
        # Pin. Permit only the first real search in each three-call group; the
        # skipped calls return empty results without touching Composio.
        if tool_slug == "COMPOSIO_SEARCH_IMAGE":
            invocation = budget.image_search_invocations
            budget.image_search_invocations += 1
            if invocation % 3 != 0:
                return {}

        # Force one HTTP execution per logical tool invocation. The original
        # function's retry loop would otherwise make the call ceiling opaque.
        budget.reserve(tool_slug)
        return await orig_run_composio_tool(tool_slug, arguments or {}, retries=0)

    async def process_pinterest_job(job_id: str, url: str, job_store: Any) -> Dict[str, Any]:
        token = _call_budget.set(CallBudget(MAX_COMPOSIO_CALLS))
        try:
            result = await orig_process(job_id, url, job_store)
            budget = _call_budget.get()
            if budget:
                result["composio_call_budget"] = {
                    "used": budget.used,
                    "maximum": budget.maximum,
                    "remaining": max(0, budget.maximum - budget.used),
                }
            return result
        finally:
            _call_budget.reset(token)

    async def research_product(url: str, job_store: Any, job_id: str) -> Dict[str, Any]:
        product = await orig_research(url, job_store, job_id)
        product["url"] = url
        product["category"] = detect_product_category(product)
        return product

    async def select_or_create_board(product: Dict[str, Any], job_store: Any, job_id: str) -> str:
        """Prefer professional category boards; Product Pins is last-resort only."""
        job_store.update(job_id, progress="Selecting Pinterest board")
        data = await budgeted_run_composio_tool("PINTEREST_LIST_BOARDS", {})
        items = data.get("items") or data.get("boards") or []
        if isinstance(data, list):
            items = data
        category_key = (product.get("category") or "general").lower()
        preferred = preferred_board_name(category_key)
        job_store.update(
            job_id,
            progress=f"Board selection: category={category_key}, preferred='{preferred}'",
        )
        matched_id = find_matching_board(items, preferred)
        if matched_id:
            logger.info("Reusing board '%s': %s", preferred, matched_id)
            return matched_id
        if preferred != default_board:
            logger.info("Creating category board: %s", preferred)
            created = await budgeted_run_composio_tool(
                "PINTEREST_CREATE_BOARD",
                {
                    "name": preferred,
                    "description": f"{preferred} product discovery",
                    "privacy": "PUBLIC",
                },
            )
            board_id = created.get("id") or (created.get("data") or {}).get("id")
            if board_id:
                return str(board_id)
            logger.warning("Category board create failed, falling back: %s", created)
        fallback_id = find_matching_board(items, default_board)
        if fallback_id:
            logger.info("Using fallback '%s': %s", default_board, fallback_id)
            return fallback_id
        created = await budgeted_run_composio_tool(
            "PINTEREST_CREATE_BOARD",
            {"name": default_board, "description": "Product pins", "privacy": "PUBLIC"},
        )
        board_id = created.get("id") or (created.get("data") or {}).get("id")
        if not board_id:
            raise RuntimeError(f"Could not create board: {created}")
        return str(board_id)

    agent_mod.run_composio_tool = budgeted_run_composio_tool
    agent_mod.probe_capabilities = lambda: _static_capabilities(agent_mod)
    agent_mod.research_product = research_product
    agent_mod.select_or_create_board = select_or_create_board
    agent_mod.process_pinterest_job = process_pinterest_job
    logger.info("board_org wiring + 22-call production budget applied")
