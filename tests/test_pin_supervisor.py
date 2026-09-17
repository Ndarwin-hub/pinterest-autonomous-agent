from pin_supervisor import inspect_result, normalize_for_identity


def _pin(url, i, verified=True):
    return {"pin_id": f"pin-{i}", "verified": verified, "destination_url": url}


def test_exactly_five_verified_pins_are_success():
    url = "https://www.amazon.com/dp/B000TEST01?tag=desiredplus-20"
    result = inspect_result({"pins": [_pin(url, i) for i in range(1, 6)]}, url)
    assert result["pin_supervisor_status"] == "SUCCESS"
    assert result["pin_supervisor"]["verified_count"] == 5


def test_partial_or_unverified_job_is_not_success():
    url = "https://www.amazon.com/dp/B000TEST01?tag=desiredplus-20"
    result = inspect_result({"pins": [_pin(url, i) for i in range(1, 5)]}, url)
    assert result["pin_supervisor_status"] == "UNCONFIRMED"
    assert result["pin_supervisor"]["failures"]


def test_destination_must_match_exactly():
    url = "https://www.amazon.com/dp/B000TEST01?tag=desiredplus-20"
    pins = [_pin(url, i) for i in range(1, 5)] + [_pin("https://www.amazon.com/dp/B000TEST01", 5)]
    result = inspect_result({"pins": pins}, url)
    assert result["pin_supervisor_status"] == "UNCONFIRMED"


def test_identity_normalization_does_not_change_destination_contract():
    url = "HTTPS://WWW.AMAZON.COM/dp/B000TEST01?tag=desiredplus-20#fragment"
    assert normalize_for_identity(url) == "https://www.amazon.com/dp/B000TEST01?tag=desiredplus-20"
