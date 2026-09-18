"""Unit tests for batch_submit validation / tagging (no network required for pure canonicalize paths)."""
from __future__ import annotations

from batch_submit import validate_and_canonicalize, _ensure_affiliate_tag, MAX_BATCH
from amazon_url import AFFILIATE_TAG, is_amazon_us_product_url, extract_asin_from_url


def test_ensure_affiliate_tag_injects():
    url = "https://www.amazon.com/dp/B08N5WRWNW"
    tagged = _ensure_affiliate_tag(url)
    assert f"tag={AFFILIATE_TAG}" in tagged
    assert "B08N5WRWNW" in tagged


def test_ensure_affiliate_tag_replaces_other_tag():
    url = "https://www.amazon.com/dp/B08N5WRWNW?tag=someone-else"
    tagged = _ensure_affiliate_tag(url)
    assert f"tag={AFFILIATE_TAG}" in tagged
    assert "someone-else" not in tagged


def test_validate_clear_us_product_url():
    url = "https://www.amazon.com/dp/B08N5WRWNW"
    # resolve may use network; is_amazon_us_product_url is offline
    assert is_amazon_us_product_url(url)
    assert extract_asin_from_url(url) == "B08N5WRWNW"


def test_reject_non_product():
    assert not is_amazon_us_product_url("https://www.amazon.com/s?k=headphones")
    assert not is_amazon_us_product_url("https://www.amazon.co.uk/dp/B08N5WRWNW")
    assert not is_amazon_us_product_url("https://example.com/product")


def test_max_batch_constant():
    assert MAX_BATCH == 50


if __name__ == "__main__":
    test_ensure_affiliate_tag_injects()
    test_ensure_affiliate_tag_replaces_other_tag()
    test_validate_clear_us_product_url()
    test_reject_non_product()
    test_max_batch_constant()
    print("test_batch_submit: all offline checks passed")
