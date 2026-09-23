from pathlib import Path
from daily_ledger import DailyLedger


def _slots():
    return [{"slot":i,"target_board_name":f"Board {i}","target_board_id":str(i),"slot_kind":"board"} for i in range(1,51)]


def test_new_day_has_independent_fifty_slots(tmp_path: Path):
    ledger=DailyLedger(tmp_path/"ledger.db")
    yesterday=ledger.ensure_day("2099-01-01",_slots())
    ledger.mark_slot(1,status="success",day=yesterday,selected_asin="B000000001")
    ledger.mark_slot(2,status="failed_open",day=yesterday,selected_asin="B000000002")
    today=ledger.ensure_day("2099-01-02",_slots())
    state=ledger.get_day_status(today)
    assert len(state["slots"]) == 50
    assert all(s["status"] == "pending" for s in state["slots"])
    assert ledger.next_unfinished_batch(today) == 1
    assert "B000000002" in ledger.historical_selected_asins(exclude_day=today)
