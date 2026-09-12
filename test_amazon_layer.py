import os,tempfile,unittest
from pathlib import Path
D=tempfile.mkdtemp(prefix="amazon_layer_"); os.environ["DATA_DIR"]=D; os.environ["PUBLISHED_DB_PATH"]=str(Path(D)/"published.db"); os.environ["DAILY_LEDGER_DB_PATH"]=str(Path(D)/"ledger.db")
for k in ("AMAZON_CLIENT_ID","AMAZON_CLIENT_SECRET","AMAZON_PARTNER_TAG"): os.environ.pop(k,None)
from published_registry import PublishedRegistry,extract_asin,normalize_url_key
from daily_ledger import DailyLedger
from amazon_client import amazon_credentials_present,extract_detail_page_url
from amazon_discovery import is_dormant,assert_url_unmodified
from amazon_boards import classify_live_boards,build_slot_specs,REQUIRED_PRIMARY_SLOTS
class Tests(unittest.TestCase):
 def test_dormant(self): self.assertFalse(amazon_credentials_present()); self.assertTrue(is_dormant())
 def test_asin_and_exact_url(self):
  u="https://www.amazon.com/dp/B0ABCDEFGH?tag=desiredplus-20&x=1"; self.assertEqual(extract_asin(u),"B0ABCDEFGH"); self.assertEqual(extract_detail_page_url({"asin":"B0ABCDEFGH","detailPageURL":u}),u); self.assertTrue(assert_url_unmodified(u,u))
 def test_registry_duplicate(self):
  r=PublishedRegistry(Path(D)/"r.db"); u="https://www.amazon.com/dp/B0ABCDEFGH?tag=desiredplus-20"; self.assertTrue(r.record_success(affiliate_url=u,source="manual",asin="B0ABCDEFGH")); self.assertTrue(r.is_published(asin="B0ABCDEFGH")); self.assertFalse(r.record_success(affiliate_url=u,source="amazon",asin="B0ABCDEFGH"))
 def test_legacy_and_unknown_do_not_count(self):
  live=[{"id":"987906936951142945","name":"General Pins"},{"id":"x","name":"Random Board"}]; i=classify_live_boards(live); self.assertEqual(i["primary_count"],0); self.assertEqual(len(i["legacy_boards"]),1); self.assertEqual(len(i["unknown_non_legacy"]),1); self.assertIsNone(build_slot_specs(live)[0])
 def test_fourteen_slots(self):
  from board_org import PERMANENT_BOARD_IDS
  live=[{"id":v,"name":k} for k,v in PERMANENT_BOARD_IDS.items() if k!="Everything Else"]
  i=classify_live_boards(live); self.assertLess(i["primary_count"],REQUIRED_PRIMARY_SLOTS)
  l=DailyLedger(Path(D)/"l.db"); specs=[{"slot":n,"slot_kind":"board","target_board_name":str(n),"target_board_id":str(n)} for n in range(1,15)]+[{"slot":15,"slot_kind":"global"}]; day=l.ensure_day("2099-01-01",specs); self.assertEqual(len(l.get_day_status(day)["slots"]),15)
if __name__=="__main__": unittest.main(verbosity=2)
