"""Offline tests for dynamic board balancing core rule."""
from __future__ import annotations

from board_balance import (
    extract_board_rows,
    balance_state,
    discovery_board_order,
    resolve_board_with_balance,
    apply_virtual_increment,
    product_matches_board,
    DEFAULT_BOARD_NAME,
    UNDERFILL_DELTA,
    BALANCE_SPREAD,
)


LIVE_FIXTURE = [
    {"id": "987906936951148022", "name": "Automotive & Tools", "pin_count": 0},
    {"id": "987906936951145056", "name": "Books & Learning", "pin_count": 4},
    {"id": "987906936951145057", "name": "Electronics & Gadgets", "pin_count": 31},
    {"id": "987906936951147704", "name": "Everything Else", "pin_count": 0},
    {"id": "987906936951147683", "name": "Fashion & Lifestyle", "pin_count": 5},
    {"id": "987906936951147682", "name": "Health & Fitness", "pin_count": 25},
    {"id": "987906936951145744", "name": "Home, Kitchen & Dining", "pin_count": 5},
    {"id": "987906936951148020", "name": "Office & Productivity", "pin_count": 5},
    {"id": "987906936951148021", "name": "Pet Supplies", "pin_count": 0},
    {"id": "987906936951147684", "name": "Sports, Games & Toys", "pin_count": 5},
    {"id": "987906936951147708", "name": "Travel & Camping", "pin_count": 5},
]


def test_extract_and_rank():
    rows = extract_board_rows(LIVE_FIXTURE)
    assert any(r["is_everything_else"] for r in rows)
    bal = balance_state(rows)
    assert bal["available"] is True
    ranked = bal["ranked_dedicated"]
    assert ranked[0]["pin_count"] <= ranked[-1]["pin_count"]
    assert ranked[0]["name"] in {"Automotive & Tools", "Pet Supplies"}


def test_underfilled_state_with_real_spread():
    rows = extract_board_rows(LIVE_FIXTURE)
    bal = balance_state(rows)
    # Live spread is 31-0=31 → underfilled
    assert bal["spread"] == 31
    assert bal["state"] == "underfilled"
    names = [r["name"] for r in bal["underfilled"]]
    assert "Automotive & Tools" in names or "Pet Supplies" in names
    assert "Electronics & Gadgets" not in names or bal["underfilled"][0]["pin_count"] < 10


def test_balanced_state():
    flat = [
        {"id": "1", "name": "Electronics & Gadgets", "pin_count": 10},
        {"id": "2", "name": "Health & Fitness", "pin_count": 12},
        {"id": "3", "name": "Home, Kitchen & Dining", "pin_count": 11},
        {"id": "4", "name": "Fashion & Lifestyle", "pin_count": 10},
        {"id": "5", "name": "Everything Else", "pin_count": 3},
        {"id": "6", "name": "Pet Supplies", "pin_count": 9},
        {"id": "7", "name": "Sports, Games & Toys", "pin_count": 11},
        {"id": "8", "name": "Office & Productivity", "pin_count": 10},
        {"id": "9", "name": "Automotive & Tools", "pin_count": 10},
        {"id": "10", "name": "Books & Learning", "pin_count": 10},
        {"id": "11", "name": "Travel & Camping", "pin_count": 12},
    ]
    bal = balance_state(extract_board_rows(flat))
    assert bal["available"] is True
    assert bal["state"] == "balanced"
    order = discovery_board_order(bal)
    assert order[0]["mode"] == "everything_else"
    assert order[0]["board_name"] == DEFAULT_BOARD_NAME


def test_relevance_over_balance():
    product = {"title": "Anker USB-C Charger 65W", "name": "Anker USB-C Charger 65W"}
    resolved = resolve_board_with_balance(product, LIVE_FIXTURE)
    assert resolved["board_name"] == "Electronics & Gadgets"
    assert resolved["reason"] == "product_relevance_dedicated"
    assert resolved["balance"]["available"] is True


def test_general_product_to_everything_else():
    # Avoid terms that match dedicated family rules (e.g. "novel" inside "novelty")
    product = {"title": "Assorted multipurpose household widget set", "name": "Assorted multipurpose household widget set"}
    resolved = resolve_board_with_balance(product, LIVE_FIXTURE)
    assert resolved["board_name"] == DEFAULT_BOARD_NAME


def test_virtual_increment_reorders():
    rows = extract_board_rows(LIVE_FIXTURE)
    bal = balance_state(rows)
    before = [r["name"] for r in bal["ranked_dedicated"][:3]]
    bal2 = apply_virtual_increment(bal, "Automotive & Tools", pins_per_product=4)
    after_counts = {r["name"]: r["pin_count"] for r in bal2["ranked_dedicated"]}
    assert after_counts.get("Automotive & Tools") == 4


def test_missing_counts_message():
    sparse = [{"id": "1", "name": "Electronics & Gadgets"}]  # no pin_count
    bal = balance_state(extract_board_rows(sparse))
    assert bal["available"] is False
    assert "unavailable" in (bal.get("message") or "").lower()


if __name__ == "__main__":
    test_extract_and_rank()
    test_underfilled_state_with_real_spread()
    test_balanced_state()
    test_relevance_over_balance()
    test_general_product_to_everything_else()
    test_virtual_increment_reorders()
    test_missing_counts_message()
    print("test_board_balance: all checks passed")
    # Print live ranking for human verification
    bal = balance_state(extract_board_rows(LIVE_FIXTURE))
    print("LIVE RANKING (lowest first):")
    for r in bal["ranked_dedicated"]:
        print(f"  {r['pin_count']:>3}  {r['name']}")
    print("state:", bal["state"], "spread:", bal["spread"])
    print("message:", bal["message"])
