"""Persistence tests for Web UI settings, history, and resource blacklist."""

import tempfile
import unittest
from pathlib import Path

from module.web_state import WebState


class WebStateTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state = WebState(str(Path(self.temp_dir.name) / "state.sqlite3"))

    def test_active_tasks_not_hidden_by_finished_history_limit(self):
        self.state.record(-1001, 1, "waiting", "Queued")
        self.state.record(-1001, 2, "active", "Downloading")
        self.state.record(-1001, 3, "finished", "SuccessDownload")
        self.state.record(-1001, 4, "finished", "SuccessDownload")
        self.assertEqual(3, len(self.state.downloads(limit=1)))
        self.state.interrupt_pending()
        statuses = {row["message_id"]: row["status"] for row in self.state.history()}
        self.assertEqual("Cancelled", statuses[1])
        self.assertEqual("Cancelled", statuses[2])
        self.assertEqual("SuccessDownload", statuses[3])

    def test_settings_round_trip(self):
        settings = {"api_id": 123, "api_hash": "secret", "chats": [{"chat_id": -1001}]}
        self.state.set_settings(settings)
        self.assertEqual(settings, self.state.get_settings())

    def test_history_upsert_and_blacklist_lifecycle(self):
        self.state.record(-1001, 12, "caption", "SuccessDownload", "clip.mp4")
        self.state.record(-1001, 12, "updated caption", "SkipDownload", "")
        self.assertEqual(1, len(self.state.history()))
        self.assertEqual("updated caption", self.state.history()[0]["title"])

        self.state.block(-1001, 12, "updated caption")
        self.assertTrue(self.state.is_blocked(-1001, 12))
        self.assertEqual({("-1001", 12)}, self.state.blocked_ids())
        self.state.unblock(-1001, 12)
        self.assertFalse(self.state.is_blocked(-1001, 12))
        self.assertEqual([], self.state.blacklist())


if __name__ == "__main__":
    unittest.main()
