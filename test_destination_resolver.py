from board_org import SECTION_IDS, PERMANENT_BOARD_IDS, resolve_pinterest_destination


def destination(name, description="", **extra):
    return resolve_pinterest_destination({"name": name, "description": description, **extra})


def assert_route(name, board, section):
    d = destination(name)
    assert d["board_id"] == PERMANENT_BOARD_IDS[board]
    assert d["section_id"] == (SECTION_IDS.get(section) if section else None)


def test_electronics_sections_and_root():
    assert_route("Apple iPhone 17", "Electronics & Gadgets", "electronics_smartphones")
    assert_route("Samsung Galaxy smartphone", "Electronics & Gadgets", "electronics_smartphones")
    assert_route("Apple iPad", "Electronics & Gadgets", "electronics_smartphones")
    assert_route("Dell laptop", "Electronics & Gadgets", "electronics_pc_home")
    assert_route("Gaming PC", "Electronics & Gadgets", "electronics_pc_home")
    assert_route("Samsung TV", "Electronics & Gadgets", "electronics_pc_home")
    for name in ["iPhone case", "iPhone screen protector", "AirPods", "headphones", "USB cable", "power bank", "laptop bag"]:
        assert_route(name, "Electronics & Gadgets", None)


def test_health_sections_and_root():
    assert_route("Baby diapers", "Health & Fitness", "health_baby_kids")
    assert_route("Baby bottle", "Health & Fitness", "health_baby_kids")
    assert_route("Face serum", "Health & Fitness", "health_beauty_personal")
    assert_route("Shampoo", "Health & Fitness", "health_beauty_personal")
    for name in ["Yoga mat", "Dumbbells", "Protein powder", "Blood-pressure monitor", "Resistance bands"]:
        assert_route(name, "Health & Fitness", None)


def test_description_does_not_promote_an_accessory_to_a_device_section():
    d = destination("Protective case", "Compatible with iPhone 17 Pro")
    assert d["board_id"] == PERMANENT_BOARD_IDS["Electronics & Gadgets"]
    assert d["section_id"] is None


def test_root_has_no_section():
    d = destination("USB-C charging cable")
    assert d["is_root"] is True
    assert d["section_id"] is None


def test_stable_ids_not_position_based():
    d = destination("iPhone")
    assert d["board_id"] == "987906936951145057"
    assert d["section_id"] == "3856307780748685888"


def test_manual_and_normalized_creators_style_input_match():
    manual = destination("Dell laptop")
    creators = destination("Dell laptop", product_type="Laptop", category="Electronics")
    assert (manual["board_id"], manual["section_id"]) == (creators["board_id"], creators["section_id"])
