"""Web UI route tests using temporary persistent state."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from module.app import Application
from module.web import configure_web_runtime, get_flask_app
from module.web_state import WebState


class WebRoutesTestCase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        state = WebState(str(Path(self.temp_dir.name) / "state.sqlite3"))
        with patch("module.app.WebState", return_value=state):
            self.app = Application("unused.yaml", "unused-data.yaml")
        self.loop = self.app.loop
        self.addCleanup(self.loop.close)
        self.addCleanup(self.app.executor.shutdown, True)
        configure_web_runtime(
            self.app,
            self.loop,
            lambda *_: None,
            lambda: None,
            lambda *_: None,
            lambda: None,
        )
        self.client = get_flask_app().test_client()

    def test_home_renders_task_manager(self):
        response = self.client.get("/")
        self.assertEqual(200, response.status_code)
        self.assertIn("任务与预览".encode("utf-8"), response.data)

    def test_runtime_settings_and_chats_are_stored_without_yaml(self):
        response = self.client.put(
            "/api/runtime",
            json={
                "api_id": 123,
                "api_hash": "hash",
                "save_path": str(Path(self.temp_dir.name) / "downloads"),
                "media_types": ["photo", "video"],
                "file_formats": {"video": ["mp4"]},
                "max_download_task": 2,
                "proxy_url": "",
            },
        )
        self.assertEqual(200, response.status_code)
        response = self.client.put(
            "/api/task/chats",
            json={"chats": [{"chat_id": "-100123", "download_filter": "media_type == 'video'"}]},
        )
        self.assertEqual(200, response.status_code)
        self.assertFalse((Path(self.temp_dir.name) / "unused.yaml").exists())
        stored = self.app.web_state.get_settings()
        self.assertEqual(123, stored["api_id"])
        self.assertEqual(-100123, stored["chats"][0]["chat_id"])
        self.assertEqual("media_type == 'video'", stored["chats"][0]["download_filter"])

    def test_blacklist_api_updates_persistent_state(self):
        self.assertEqual(
            200,
            self.client.post(
                "/api/blacklist",
                json={"chat_id": -100123, "message_id": 77, "title": "clip"},
            ).status_code,
        )
        rows = self.client.get("/api/blacklist").get_json()
        self.assertEqual(77, rows[0]["message_id"])
        self.assertTrue(self.app.web_state.is_blocked(-100123, 77))
        self.assertEqual(
            200,
            self.client.delete(
                "/api/blacklist",
                json={"chat_id": -100123, "message_id": 77},
            ).status_code,
        )
        self.assertFalse(self.app.web_state.is_blocked(-100123, 77))

    def test_preview_and_submit_require_explicit_runtime_start(self):
        preview = self.client.post("/api/task/preview", json={"links": "https://t.me/example/1"})
        self.assertEqual(503, preview.status_code)
        submit = self.client.post("/api/task/submit", json={"selected": []})
        self.assertEqual(503, submit.status_code)


if __name__ == "__main__":
    unittest.main()
