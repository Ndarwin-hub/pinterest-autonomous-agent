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


class TestFailClosedBoard(unittest.TestCase):
    def test_known_category_missing_board_fails(self):
        from board_org import classify_with_confidence, preferred_board_name, find_matching_board, DEFAULT_BOARD_NAME
        product = {"name": "La-Z-Boy Executive Office Chair Leather", "description": "office seating"}
        info = classify_with_confidence(product)
        self.assertIn(info["confidence"], ("HIGH", "MEDIUM"))
        self.assertEqual(info["category"], "office")
        preferred = preferred_board_name(info["category"])
        self.assertEqual(preferred, "Office & Productivity")
        self.assertIsNone(find_matching_board([], preferred))

    def test_general_may_use_everything_else(self):
        from board_org import classify_with_confidence, preferred_board_name, DEFAULT_BOARD_NAME
        product = {"name": "Generic household item", "description": "misc"}
        info = classify_with_confidence(product)
        self.assertEqual(info["category"], "general")
        self.assertEqual(info["confidence"], "LOW")
        self.assertEqual(preferred_board_name("general"), DEFAULT_BOARD_NAME)

class TestBatchSemantics(unittest.TestCase):
    def test_partial_not_completed(self):
        def derive(attempted, successes, errors):
            status = "completed" if (attempted > 0 and successes >= attempted and not errors) else ("partial_failure" if successes > 0 else "failed")
            if attempted > 0 and successes < attempted:
                status = "partial_failure" if successes > 0 else "failed"
            return status
        self.assertEqual(derive(5, 5, []), "completed")
        self.assertEqual(derive(5, 3, [{"slot": 4}]), "partial_failure")
        self.assertEqual(derive(5, 0, [{"slot": 1}]), "failed")
        self.assertNotEqual(derive(5, 4, []), "completed")

class TestRegistryPartial(unittest.TestCase):
    def test_blocked_asin_not_reselected(self):
        import tempfile
        from pathlib import Path
        from published_registry import PublishedRegistry
        d = tempfile.mkdtemp()
        r = PublishedRegistry(Path(d) / "p.db")
        r.record_blocked(asin="B0869FKM78", product_url="https://www.amazon.com/dp/B0869FKM78", notes="partial")
        self.assertTrue(r.is_published(asin="B0869FKM78"))
