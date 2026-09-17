"""Amazon discovery facade.

The scheduled Amazon path uses the connected Composio Amazon search tool. The
legacy Creators API client remains available as a separate module but is not a
runtime dependency of scheduled discovery.
"""
from __future__ import annotations
from typing import Optional, Set
from amazon_composio_discovery import CATEGORY_QUERIES, discover_category, discover_fifteen, composio_ready
MAX_REPLACEMENTS_PER_SLOT = 5

def is_dormant() -> bool:
    return not composio_ready()

_BOARD_CATEGORY = {
    "Electronics & Gadgets": "Electronics",
    "Smartphones & Tablets": "Cell Phones & Accessories",
    "PCs, Laptops & Home Electronics": "Computers & Accessories",
    "Health & Fitness": "Health & Household",
    "Beauty & Personal Care": "Beauty",
    "Home, Kitchen & Dining": "Home & Kitchen",
    "Sports, Games & Toys": "Toys & Games",
    "Fashion & Lifestyle": "Clothing/Shoes",
    "Pet Supplies": "Pet Supplies",
    "Baby & Kids": "Baby",
    "Automotive & Tools": "Electronics",
    "Office & Productivity": "Computers & Accessories",
    "Travel & Camping": "Clothing/Shoes",
    "Books & Learning": "Home & Kitchen",
}

async def discover_for_board(board_name: str, *, exclude_asins: Optional[Set[str]] = None, client=None):
    category = _BOARD_CATEGORY.get(board_name, "Electronics")
    return await discover_category(category, exclude_asins=exclude_asins)

async def discover_global(*, exclude_asins: Optional[Set[str]] = None, client=None):
    selected = await discover_fifteen(exclude_asins=exclude_asins)
    return selected[0] if selected else None

def assert_url_unmodified(original, candidate):
    return (original or "") == (candidate or "")
