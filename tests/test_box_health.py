"""check_box_health: which readings count as a problem."""
import unittest
from unittest import mock

import check_box_health as cbh


def _mem(avail_mb, swap_used_mb, swap_total_mb=2048):
    return {"MemAvailable": avail_mb * 1024, "SwapTotal": swap_total_mb * 1024,
            "SwapFree": (swap_total_mb - swap_used_mb) * 1024}


class ReadingTest(unittest.TestCase):
    def run_reading(self, mem, lat=0.05, backup_age=0.2):
        with mock.patch.object(cbh, "meminfo", return_value=mem), \
             mock.patch.object(cbh, "backup_age_hours", return_value=backup_age), \
             mock.patch.object(cbh, "api_latency", return_value=lat), \
             mock.patch.object(cbh, "api_memory_mb", return_value=(300, 600)):
            return cbh.reading()[0]

    def test_full_swap_with_free_ram_is_fine(self):
        self.assertEqual(self.run_reading(_mem(434, 1959)), [])

    def test_full_swap_and_short_ram(self):
        self.assertEqual(len(self.run_reading(_mem(250, 1959))), 1)

    def test_low_ram_alone(self):
        self.assertIn("only 100 MB", self.run_reading(_mem(100, 100))[0])

    def test_api_slow_or_down(self):
        self.assertIn("took", self.run_reading(_mem(800, 0), lat=9.0)[0])
        self.assertIn("did not answer", self.run_reading(_mem(800, 0), lat=None)[0])

    def test_stale_or_unreadable_backup(self):
        self.assertIn("no data backup for 300 hours", self.run_reading(_mem(800, 0), backup_age=300)[0])
        self.assertIn("can't read", self.run_reading(_mem(800, 0), backup_age=None)[0])
        self.assertEqual(self.run_reading(_mem(800, 0), backup_age=11), [])


if __name__ == "__main__":
    unittest.main()


class AlertCooldownTest(unittest.TestCase):
    """A refused Discord post must not start the cooldown (2026-11-17: the
    bot lost access to the alert channel and every alert counted as sent)."""

    def run_main(self, delivered, tmp_state):
        with mock.patch.object(cbh, "reading", return_value=(["swap 99% used"], "s")), \
             mock.patch.object(cbh, "top_processes", return_value=[]), \
             mock.patch.object(cbh, "STATE", tmp_state), \
             mock.patch.object(cbh.transport, "configured", return_value=True), \
             mock.patch.object(cbh.transport, "send_now", return_value=delivered), \
             mock.patch("sys.argv", ["check_box_health.py"]):
            cbh.main()
        import json
        return json.loads(tmp_state.read_text())

    def test_refused_post_keeps_retrying(self):
        import tempfile, json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            state = Path(d) / "s.json"
            state.write_text(json.dumps({"strikes": 1, "alerting": False, "last_alert": 0}))
            out = self.run_main(False, state)
            self.assertEqual(out["last_alert"], 0)
            self.assertFalse(out["alerting"])
            out = self.run_main(True, state)
            self.assertGreater(out["last_alert"], 0)
            self.assertTrue(out["alerting"])
