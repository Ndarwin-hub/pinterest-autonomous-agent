"""Regression tests for Amazon short-URL resolution and canonicalization."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from amazon_url import (
    AFFILIATE_TAG,
    canonicalize_amazon_product_url,
    extract_asin_from_url,
    is_amazon_short_url,
    is_amazon_us_product_url,
    resolve_amazon_product_url,
)


class TestExtractAsin(unittest.TestCase):
    def test_dp(self):
        self.assertEqual(
            extract_asin_from_url("https://www.amazon.com/dp/B0869FKM78"),
            "B0869FKM78",
        )

    def test_slug_dp(self):
        self.assertEqual(
            extract_asin_from_url(
                "https://www.amazon.com/Intex-Dura-Beam/dp/B0869FKM78?tag=x"
            ),
            "B0869FKM78",
        )

    def test_gp_product(self):
        self.assertEqual(
            extract_asin_from_url("https://www.amazon.com/gp/product/B0869FKM78"),
            "B0869FKM78",
        )

    def test_none(self):
        self.assertIsNone(extract_asin_from_url("https://amzn.to/476wDnu"))
        self.assertIsNone(extract_asin_from_url("https://example.com"))


class TestIsProductUrl(unittest.TestCase):
    def test_valid(self):
        self.assertTrue(
            is_amazon_us_product_url("https://www.amazon.com/dp/B0869FKM78")
        )
        self.assertTrue(
            is_amazon_us_product_url(
                "https://www.amazon.com/Foo/dp/B0869FKM78?tag=desiredplus-20"
            )
        )

    def test_search_not_product(self):
        self.assertFalse(
            is_amazon_us_product_url("https://www.amazon.com/s?k=air+mattress")
        )

    def test_non_us(self):
        self.assertFalse(
            is_amazon_us_product_url("https://www.amazon.co.uk/dp/B0869FKM78")
        )

    def test_short_not_product(self):
        self.assertFalse(is_amazon_us_product_url("https://amzn.to/476wDnu"))


class TestCanonicalize(unittest.TestCase):
    def test_strips_noise_keeps_tag_compact(self):
        """Normal product URLs canonicalize to compact /dp/ASIN form."""
        messy = (
            "https://www.amazon.com/Intex-64135ED-Dura-Beam-Deluxe-Pillow/dp/B0869FKM78"
            "?crid=2B1YN7LNO83HH&keywords=camping&tag=desiredplus-20&ref_=as_li"
        )
        out = canonicalize_amazon_product_url(messy)
        self.assertEqual(out, "https://www.amazon.com/dp/B0869FKM78?tag=desiredplus-20")
        self.assertIn(f"tag={AFFILIATE_TAG}", out)
        self.assertNotIn("Intex", out)

    def test_injects_tag(self):
        out = canonicalize_amazon_product_url("https://www.amazon.com/dp/B0869FKM78")
        self.assertEqual(out, "https://www.amazon.com/dp/B0869FKM78?tag=desiredplus-20")

    def test_preserve_slug_keeps_verified_product_path(self):
        """Short-link resolution preserves slug for identity fallback."""
        src = (
            "https://www.amazon.com/Intex-64135ED-Dura-Beam-Deluxe-Pillow/dp/B0869FKM78"
            "?tag=other&ref_=x"
        )
        out = canonicalize_amazon_product_url(src, preserve_slug=True)
        self.assertEqual(
            out,
            "https://www.amazon.com/Intex-64135ED-Dura-Beam-Deluxe-Pillow/dp/B0869FKM78"
            f"?tag={AFFILIATE_TAG}",
        )
        self.assertEqual(extract_asin_from_url(out), "B0869FKM78")
        self.assertTrue(is_amazon_us_product_url(out))

    def test_preserve_slug_false_stays_compact(self):
        src = "https://www.amazon.com/Intex-64135ED-Dura-Beam-Deluxe-Pillow/dp/B0869FKM78"
        out = canonicalize_amazon_product_url(src, preserve_slug=False)
        self.assertEqual(out, "https://www.amazon.com/dp/B0869FKM78?tag=desiredplus-20")


class TestIsShort(unittest.TestCase):
    def test_amzn_to(self):
        self.assertTrue(is_amazon_short_url("https://amzn.to/476wDnu"))

    def test_a_co(self):
        self.assertTrue(is_amazon_short_url("https://a.co/d/xyz"))

    def test_normal(self):
        self.assertFalse(is_amazon_short_url("https://www.amazon.com/dp/B0869FKM78"))


class TestResolve(unittest.TestCase):
    def test_already_product_no_network_compact(self):
        """Existing /dp/ASIN inputs stay compact (no slug preservation)."""
        url = "https://www.amazon.com/Intex/dp/B0869FKM78?foo=1"
        out = resolve_amazon_product_url(url)
        self.assertEqual(out, "https://www.amazon.com/dp/B0869FKM78?tag=desiredplus-20")
        self.assertTrue(is_amazon_us_product_url(out))

    @patch("amazon_url._safe_follow")
    def test_short_url_resolves_preserves_slug(self, mock_follow):
        """Short URL -> verified slug path + ASIN + affiliate tag."""
        mock_follow.return_value = (
            "https://www.amazon.com/Intex-64135ED-Dura-Beam-Deluxe-Pillow/dp/B0869FKM78"
            "?tag=desiredplus-20&ref_=as_li",
            200,
        )
        out = resolve_amazon_product_url("https://amzn.to/476wDnu")
        self.assertEqual(
            out,
            "https://www.amazon.com/Intex-64135ED-Dura-Beam-Deluxe-Pillow/dp/B0869FKM78"
            f"?tag={AFFILIATE_TAG}",
        )
        self.assertEqual(extract_asin_from_url(out), "B0869FKM78")
        self.assertIn(f"tag={AFFILIATE_TAG}", out)
        self.assertTrue(is_amazon_us_product_url(out))
        mock_follow.assert_called_once()

    @patch("amazon_url._safe_follow")
    def test_short_url_slug_feeds_identity_fallback(self, mock_follow):
        """When page title is Amazon.com, _amazon_slug recovers human identity."""
        mock_follow.return_value = (
            "https://www.amazon.com/Intex-64135ED-Dura-Beam-Deluxe-Pillow/dp/B0869FKM78",
            200,
        )
        out = resolve_amazon_product_url("https://amzn.to/476wDnu")
        import re
        from urllib.parse import unquote, urlparse

        def amazon_slug(url: str) -> str:
            path = unquote(urlparse(url).path)
            m = re.search(r"/([^/]+)/dp/[A-Z0-9]{8,14}(?:[/?]|$)", path, flags=re.I)
            if not m:
                return ""
            slug = m.group(1).replace("-", " ").replace("_", " ")
            slug = re.sub(r"\s+", " ", slug).strip()
            return slug[:200]

        slug = amazon_slug(out)
        self.assertIn("Intex", slug)
        self.assertIn("Dura", slug)
        self.assertNotEqual(slug.lower(), "amazon.com")
        self.assertGreaterEqual(len(slug), 6)
        self.assertEqual(extract_asin_from_url(out), "B0869FKM78")

    @patch("amazon_url._safe_follow")
    def test_resolve_failure(self, mock_follow):
        mock_follow.side_effect = Exception("network down")
        with self.assertRaises(RuntimeError) as cm:
            resolve_amazon_product_url("https://amzn.to/deadlink")
        self.assertIn("Failed to resolve", str(cm.exception))

    @patch("amazon_url._safe_follow")
    def test_resolves_to_non_product(self, mock_follow):
        mock_follow.return_value = ("https://www.amazon.com/s?k=mattress", 200)
        with self.assertRaises(RuntimeError) as cm:
            resolve_amazon_product_url("https://amzn.to/searchpage")
        self.assertIn("no ASIN", str(cm.exception))

    @patch("amazon_url._safe_follow")
    def test_resolves_to_non_us(self, mock_follow):
        mock_follow.return_value = (
            "https://www.amazon.co.uk/dp/B0869FKM78",
            200,
        )
        with self.assertRaises(RuntimeError) as cm:
            resolve_amazon_product_url("https://amzn.to/uklink")
        self.assertIn("not Amazon US", str(cm.exception))

    def test_invalid_url(self):
        with self.assertRaises(RuntimeError):
            resolve_amazon_product_url("not-a-url")


if __name__ == "__main__":
    unittest.main()
