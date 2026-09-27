import asyncio, json, tempfile
from pathlib import Path

def test_video_modules_import():
    import video_renderer
    import video_fallback
    assert video_renderer.affiliate_url("B0FVSBT5YR") == "https://www.amazon.com/dp/B0FVSBT5YR?tag=desiredplus-20"
    assert video_fallback._pair_for(1) == (1, 2)
    assert video_fallback._pair_for(9) == (9, 10)

def test_video_pair_rotation_pointers():
    from daily_ledger import DailyLedger
    with tempfile.TemporaryDirectory() as td:
        ledger=DailyLedger(Path(td)/"ledger.db")
        assert ledger.get_video_pair_state()["next_pair_start"] == 1
        ledger.advance_video_pair(1,"2026-09-27","test")
        assert ledger.get_video_pair_state()["next_pair_start"] == 3
        ledger.advance_video_pair(3,"2026-09-27","test")
        assert ledger.get_video_pair_state()["next_pair_start"] == 5
        ledger.advance_video_pair(9,"2026-09-27","test")
        assert ledger.get_video_pair_state()["next_pair_start"] == 5
