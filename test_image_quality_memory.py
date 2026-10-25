"""Regression tests for bounded image-validation memory use."""
import asyncio

import image_quality as iq


def test_validated_byte_cache_evicts_oldest_to_stay_within_budget(monkeypatch):
    monkeypatch.setattr(iq, "VALIDATED_CACHE_MAX_BYTES", 10)
    iq._VALIDATED_URL_CACHE.clear()
    iq._VALIDATED_URL_CACHE_BYTES = 0
    iq._cache_validated_bytes("first", b"a" * 6)
    iq._cache_validated_bytes("second", b"b" * 6)
    assert iq._VALIDATED_URL_CACHE_BYTES == 6
    assert list(iq._VALIDATED_URL_CACHE) == ["second"]
    assert iq._get_cached_validated_bytes("second") == b"b" * 6
    iq._VALIDATED_URL_CACHE.clear()
    iq._VALIDATED_URL_CACHE_BYTES = 0


def test_oversized_image_dimensions_fail_hard_gate(monkeypatch):
    monkeypatch.setattr(iq, "MAX_IMAGE_PIXELS_TO_INSPECT", 100)
    ok, reason = iq.hard_gate(20, 20)
    assert not ok
    assert reason.startswith("pixel_limit:")


def test_validate_many_caps_concurrent_validations(monkeypatch):
    active = 0
    peak = 0

    async def fake_validate(candidate):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.005)
        active -= 1
        return dict(candidate)

    monkeypatch.setattr(iq, "validate", fake_validate)
    monkeypatch.setattr(iq, "VALIDATION_CONCURRENCY", 3)
    candidates = [{"url": f"https://example.test/{i}"} for i in range(12)]
    result = asyncio.run(iq.validate_many(candidates))
    assert len(result) == 12
    assert peak <= 3
