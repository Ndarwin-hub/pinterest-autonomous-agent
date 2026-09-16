"""Architecture regression tests."""
from __future__ import annotations
import unittest
from unittest.mock import patch, MagicMock

class TestBoardTaxonomy(unittest.TestCase):
    def test_primary_slots_10(self):
        from amazon_boards import REQUIRED_PRIMARY_SLOTS
        self.assertEqual(REQUIRED_PRIMARY_SLOTS, 10)
    def test_travel_board(self):
        from board_org import resolve_pinterest_destination
        d = resolve_pinterest_destination({"name": "Intex Dura Beam air mattress", "description": "camping"})
        self.assertEqual(d["board_id"], "987906936951147708")
    def test_sports_name(self):
        from board_org import PERMANENT_BOARD_IDS
        self.assertIn("Sports, Games & Toys", PERMANENT_BOARD_IDS)

class TestBuyability(unittest.TestCase):
    def test_no_offer(self):
        from amazon_client import is_buyable_offer
        self.assertFalse(is_buyable_offer({"asin": "B0869FKM78"}))
    def test_in_stock(self):
        from amazon_client import is_buyable_offer
        self.assertTrue(is_buyable_offer({"offersV2": {"listings": [{"availability": {"message": "In Stock"}, "price": {"amount": 1}}]}}))

class TestRuntime(unittest.TestCase):
    def test_install(self):
        import types, runtime_hardening
        m = types.SimpleNamespace(); m.run_composio_tool = MagicMock()
        self.assertEqual(runtime_hardening.install(m), "installed")
        self.assertEqual(runtime_hardening.install(m), "already-installed")

class TestDormant(unittest.TestCase):
    def test_dormant(self):
        from amazon_discovery import is_dormant
        self.assertTrue(is_dormant())

if __name__ == "__main__":
    unittest.main()
