from pin_supervisor import inspect_result, normalize_for_identity


def _pin(url, i, verified=True):
    return {"pin_id": f"pin-{i}", "verified": verified, "destination_url": url}


def test_five_verified_pins_are_completed():
    url = "https://www.amazon.com/dp/B000TEST01?tag=desiredplus-20"
    result = inspect_result({"pins": [_pin(url, i) for i in range(1, 6)]}, url)
    assert result["pin_supervisor_status"] == "completed"
    assert result["job_status"] == "completed"
    assert result["pin_supervisor"]["verified_count"] == 5
    assert result["pin_supervisor"]["rollback_unpublish"] is False


def test_four_verified_pins_are_completed_partial_and_kept():
    url = "https://www.amazon.com/dp/B000TEST01?tag=desiredplus-20"
    pins = [_pin(url, i) for i in range(1, 5)] + [_pin(url, 5, verified=False)]
    result = inspect_result({"pins": pins}, url)
    assert result["pin_supervisor_status"] == "completed_partial"
    assert result["verified_pins"] == 4
    assert result["pins_published"] == 5
    assert result["pin_supervisor"]["rollback_unpublish"] is False


def test_one_verified_pin_is_completed_partial():
    url = "https://www.amazon.com/dp/B000TEST01?tag=desiredplus-20"
    pins = [_pin(url, 1)]
    result = inspect_result({"pins": pins}, url)
    assert result["pin_supervisor_status"] == "completed_partial"
    assert result["verified_pins"] == 1


def test_zero_verified_pins_are_failed():
    url = "https://www.amazon.com/dp/B000TEST01?tag=desiredplus-20"
    result = inspect_result({"pins": []}, url)
    assert result["pin_supervisor_status"] == "failed"
    assert result["verified_pins"] == 0


def test_destination_must_match_exactly_for_verification():
    url = "https://www.amazon.com/dp/B000TEST01?tag=desiredplus-20"
    pins = [_pin(url, i) for i in range(1, 5)] + [_pin("https://www.amazon.com/dp/B000TEST01", 5)]
    result = inspect_result({"pins": pins}, url)
    assert result["pin_supervisor_status"] == "completed_partial"
    assert result["verified_pins"] == 4


def test_identity_normalization_does_not_change_destination_contract():
    url = "HTTPS://WWW.AMAZON.COM/dp/B000TEST01?tag=desiredplus-20#fragment"
    assert normalize_for_identity(url) == "https://www.amazon.com/dp/B000TEST01?tag=desiredplus-20"
