from image_quality import search_amazon_product_images

def test_amazon_dynamic_image_regex_layer_present():
    source = open("image_quality.py", encoding="utf-8").read()
    assert "data-a-dynamic-image" in source or "large|hiRes|mainUrl" in source
