from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]

class KaggleMigrationContractTests(unittest.TestCase):
    def read(self, path):
        return (ROOT / path).read_text(encoding="utf-8")

    def test_kaggle_runners_are_valid_python(self):
        for path in ("kaggle_pin_primary.py", "kaggle_video_primary.py"):
            compile(self.read(path), path, "exec")

    def test_pinterest_runner_reuses_production_agent(self):
        source = self.read("kaggle_pin_primary.py")
        self.assertIn("process_pinterest_job", source)
        self.assertIn("AmazonScheduler", source)
        self.assertIn("pinterest_target_verified", source)

    def test_video_queue_requires_verified_pinterest_slots(self):
        source = self.read("kaggle_video_primary.py")
        self.assertIn("v.source='pinterest'", source)
        self.assertIn("s.status='success'", source)
        self.assertIn("s.pinterest_verified=1", source)

    def test_no_railway_http_endpoints_remain_in_kaggle_workflows(self):
        paths = (
            ".github/workflows/amazon-watchdog.yml",
            ".github/workflows/kaggle-video-primary.yml",
            "kaggle_pin_primary.py",
            "kaggle_video_primary.py",
        )
        forbidden = (
            "web-production-dae68.up.railway.app",
            "pinterest-autonomous-agent-production.up.railway.app",
            "RAILWAY_PUBLIC_DOMAIN",
        )
        for path in paths:
            content = self.read(path)
            for marker in forbidden:
                self.assertNotIn(marker, content, f"{marker} remains in {path}")

    def test_both_workflows_share_durable_state_lock(self):
        for path in (
            ".github/workflows/amazon-watchdog.yml",
            ".github/workflows/kaggle-video-primary.yml",
        ):
            self.assertIn("group: desiredplus-kaggle-state", self.read(path))

if __name__ == "__main__":
    unittest.main()
