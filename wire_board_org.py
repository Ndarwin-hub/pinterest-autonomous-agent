"""Wire board_org category/board selection into the existing agent module at runtime.

Does not rewrite agent.py. Monkeypatches research_product category assignment and
select_or_create_board so Product Pins is last-resort only.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from board_org import detect_product_category, preferred_board_name, find_matching_board

logger = logging.getLogger("pinterest-agent.wire")


def apply_agent_wiring(agent_mod: Any) -> None:
    """Patch agent module globals used by process_pinterest_job at call time."""
    orig_research = agent_mod.research_product
    run_composio_tool = agent_mod.run_composio_tool
    default_board = getattr(agent_mod, "DEFAULT_BOARD_NAME", "Product Pins")

    async def research_product(url: str, job_store: Any, job_id: str) -> Dict[str, Any]:
        product = await orig_research(url, job_store, job_id)
        product["url"] = url
        product["category"] = detect_product_category(product)
        return product

    async def select_or_create_board(product: Dict[str, Any], job_store: Any, job_id: str) -> str:
        """Prefer professional category boards; Product Pins is last-resort only."""
        job_store.update(job_id, progress="Selecting Pinterest board")
        data = await run_composio_tool("PINTEREST_LIST_BOARDS", {})
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
            created = await run_composio_tool(
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
        created = await run_composio_tool(
            "PINTEREST_CREATE_BOARD",
            {"name": default_board, "description": "Product pins", "privacy": "PUBLIC"},
        )
        board_id = created.get("id") or (created.get("data") or {}).get("id")
        if not board_id:
            raise RuntimeError(f"Could not create board: {created}")
        return str(board_id)

    agent_mod.research_product = research_product
    agent_mod.select_or_create_board = select_or_create_board
    logger.info("board_org wiring applied to agent module")
