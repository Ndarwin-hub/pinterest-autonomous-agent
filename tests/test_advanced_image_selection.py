from advanced_image_intelligence import annotate_candidate, filter_obvious_non_product, is_obvious_non_product

PRODUCT={"name":"NEEWER KM19 Mini Wireless Lavalier Microphone","brand":"NEEWER"}

def test_obvious_logo_is_rejected():
    bad={"provider":"independent_bing_image","source":"https://example.com/NEEWER-brand-logo.png","title":"NEEWER logo"}
    ok,reason=is_obvious_non_product(bad,PRODUCT)
    assert ok
    assert "logo" in reason

def test_real_product_evidence_is_not_rejected():
    good={"provider":"amazon_asin_cdn","source":"Amazon ASIN CDN gallery","title":"NEEWER KM19 Mini Wireless Lavalier Microphone"}
    ok,_=is_obvious_non_product(good,PRODUCT)
    assert not ok
    annotated=annotate_candidate(good,PRODUCT)
    assert annotated["advanced_source_trust"]=="amazon"
    assert annotated["advanced_non_product"] is False

def test_filter_preserves_valid_candidates():
    good={"provider":"amazon_asin_cdn","source":"Amazon ASIN CDN gallery","title":"NEEWER KM19 Mini Wireless Lavalier Microphone"}
    logo={"provider":"independent_bing_image","source":"https://example.com/logo.png","title":"NEEWER logo"}
    out=filter_obvious_non_product([good,logo],PRODUCT)
    assert len(out)==1
    assert out[0]["provider"]=="amazon_asin_cdn"
