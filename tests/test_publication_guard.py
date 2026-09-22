import json
import asyncio
from pathlib import Path

from publication_guard import PublicationGuard


def test_concurrent_claims_are_singleton(tmp_path: Path):
    guard = PublicationGuard(tmp_path / "guard.db")
    results = []

    async def claim(i):
        return guard.claim(
            "https://www.amazon.com/example/dp/B000000001?tag=desiredplus-20",
            1,
            "hero",
            f"job-{i}",
        )

    async def run():
        return await asyncio.gather(*(claim(i) for i in range(2)))

    results = asyncio.run(run())
    assert sum(r[0] == "new" for r in results) == 1
    assert sum(r[0] == "claimed" for r in results) == 1


def test_success_is_reused_without_second_claim(tmp_path: Path):
    guard = PublicationGuard(tmp_path / "guard.db")
    url = "https://www.amazon.com/example/dp/B000000001?tag=desiredplus-20"
    state, _, _ = guard.claim(url, 1, "hero", "job-1")
    assert state == "new"
    result = {"pin_id": "123", "verified": True}
    guard.succeed(url, 1, result)
    state, saved, owner = guard.claim(url, 1, "hero", "job-2")
    assert state == "success"
    assert saved == result
    assert owner == "job-1"


def test_hard_four_pin_ceiling(tmp_path: Path):
    guard = PublicationGuard(tmp_path / "guard.db")
    url = "https://www.amazon.com/example/dp/B000000001?tag=desiredplus-20"
    for i in range(1, 5):
        state, _, _ = guard.claim(url, i, f"strategy-{i}", f"job-{i}")
        assert state == "new"
    state, _, _ = guard.claim(url, 5, "extra", "job-extra")
    assert state == "limit"


def test_normalized_query_variants_are_same_media_key(tmp_path: Path):
    guard = PublicationGuard(tmp_path / "guard.db")
    base = "https://www.amazon.com/example/dp/B000000001?tag=desiredplus-20"
    state, _, _ = guard.claim(base, 1, "hero", "job-1")
    assert state == "new"
    guard.succeed(base, 1, {"pin_id": "123"})
    state, saved, _ = guard.claim(
        "https://WWW.AMAZON.COM/example/dp/B000000001?tag=desiredplus-20#fragment",
        1,
        "hero",
        "job-2",
    )
    assert state == "success"
    assert json.loads(json.dumps(saved))["pin_id"] == "123"
