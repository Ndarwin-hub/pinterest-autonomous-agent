from quality_patch import _amazon_page_title

def test_amazon_meta_title_attribute_order_is_supported():
    html='<meta content="Cool Coolers by Fit + Fresh 4 Pack Slim Reusable Ice Packs" property="og:title">'
    assert "Cool Coolers" in _amazon_page_title(html)

def test_amazon_product_title_markup_is_supported():
    html='<span class="a-size-large" id="productTitle">STANLEY Quencher H2.0 FlowState 40 oz Tumbler</span>'
    assert "STANLEY Quencher" in _amazon_page_title(html)
