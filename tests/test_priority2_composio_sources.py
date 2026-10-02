from priority2_composio_sources import _query_variants, _candidate_from_record, _dedupe


def test_query_variants_prioritize_exact_asin():
    q = _query_variants({"asin": "B0ABC12345", "name": "Example Product 2", "brand": "Example"})
    assert "B0ABC12345" in q["amazon"]
    assert "B0ABC12345" in q["shopping"]
    assert "manufacturer" in q["web"]


def test_search_result_direct_image_keeps_product_evidence():
    rows = _candidate_from_record(
        {
            "title": "Example Product 2 B0ABC12345",
            "asin": "B0ABC12345",
            "image": "https://example.com/images/product.jpg",
            "link": "https://example.com/product/B0ABC12345",
        },
        "COMPOSIO_SEARCH_AMAZON",
        {"asin": "B0ABC12345", "name": "Example Product 2"},
    )
    assert any(r["url"].endswith("product.jpg") for r in rows)
    assert any("Example Product" in r["source"] for r in rows)


def test_dedupe_preserves_first_candidate():
    rows = [
        {"url": "https://example.com/a.jpg", "provider": "one"},
        {"url": "https://example.com/a.jpg", "provider": "two"},
        {"url": "https://example.com/b.jpg", "provider": "three"},
    ]
    out = _dedupe(rows)
    assert [x["provider"] for x in out] == ["one", "three"]
