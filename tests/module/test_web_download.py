"""Run the real web selection through the queue, worker, TXT writer and history."""

import asyncio
import tempfile
import threading
import unittest
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

import media_downloader as downloader
import module.web as web
from module.app import Application, DownloadStatus
from module.web_state import WebState
from tests.module.test_web_flow import CHAT, DISCUSSION, TelegramFixture, message
from datetime import datetime
from tests.test_common import MockMessage, MockVideo


class WebDownloadTestCase(unittest.TestCase):
    def test_preview_selection_reaches_disk_and_history(self):
        self.run_download_case(media=False)

    def test_preview_media_selection_reaches_disk_and_caption_sidecar(self):
        self.run_download_case(media=True)

    def test_comment_text_shares_source_directory_and_filename_prefix(self):
        self.run_download_case(media=True, comments=True)

    def run_download_case(self, media, comments=False):
        with tempfile.TemporaryDirectory() as directory:
            state = WebState(str(Path(directory) / "state.sqlite3"))
            with patch("module.app.WebState", return_value=state):
                app = Application("unused.yaml", "unused-data.yaml")
            app.save_path = str(Path(directory) / "downloads")
            app.temp_save_path = str(Path(directory) / "temp")
            state.set_settings({"api_id": 123, "api_hash": "test"})
            telegram = TelegramFixture()
            if media:
                telegram.messages[5] = MockMessage(
                    id=5, chat_id=CHAT, chat_title="Test channel", date=telegram.messages[5].date,
                    media=True, caption="完整媒体说明\n#标签 <&>",
                    video=MockVideo(file_name="test.mp4", mime_type="video/mp4", file_size=5),
                )

                async def transfer(msg, file_name, **kwargs):
                    path = Path(file_name)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"media")
                    return str(path)
                telegram.download_media = transfer
            if comments:
                async def replies(*args, **kwargs):
                    reply = message(789, DISCUSSION, "跨月评论原文")
                    reply.chat.title = "Discussion group"
                    reply.date = datetime(2026, 12, 1)
                    telegram.messages[789] = reply
                    yield reply
                telegram.get_discussion_replies = replies
            loop = app.loop
            ready = threading.Event()
            thread = threading.Thread(target=lambda: self.run_loop(loop, ready), daemon=True)
            thread.start()
            self.assertTrue(ready.wait(5))
            queue = asyncio.Queue()
            try:
                with patch.object(downloader, "app", app), patch.object(downloader, "queue", queue), patch.dict(
                    web._web_runtime, clear=True
                ), patch("module.pyrogram_extension._download_cache", defaultdict(lambda: None)):
                    async def start():
                        return telegram, True

                    async def activate(client):
                        app.worker_tasks = []

                    async def stop():
                        for task in app.worker_tasks:
                            task.cancel()
                        await asyncio.gather(*app.worker_tasks, return_exceptions=True)

                    web.configure_web_runtime(app, loop, downloader.add_download_task, start, activate, stop)
                    client = web.get_flask_app().test_client()
                    self.assertEqual(200, client.post("/api/runtime/start").status_code)
                    response = client.post("/api/task/preview", json={"links": "https://t.me/channel/5", "include_comments": comments})
                    result = response.get_json()["results"][0]
                    self.assertNotIn("error", result, result)
                    response = client.post("/api/task/submit", json={"selected": [{
                        "preview_id": result["preview_id"], "indexes": [0, 1] if comments else [0],
                    }]})
                    self.assertEqual(200, response.status_code, response.get_json())
                    self.assertEqual(2 if comments else 1, response.get_json()["queued"])
                    before = client.get("/api/downloads").get_json()
                    self.assertTrue(all(row["status"] == "Queued" for row in before))
                    started, release = asyncio.Event(), asyncio.Event()
                    real_download = downloader.download_task

                    async def controlled_download(*args):
                        started.set()
                        await release.wait()
                        return await real_download(*args)

                    async def begin():
                        app.worker_tasks = [loop.create_task(downloader.worker(telegram))]
                        await started.wait()

                    with patch.object(downloader, "download_task", side_effect=controlled_download):
                        asyncio.run_coroutine_threadsafe(begin(), loop).result(5)
                        active = client.get("/api/downloads").get_json()
                        self.assertIn("Downloading", [row["status"] for row in active])
                        loop.call_soon_threadsafe(release.set)
                        asyncio.run_coroutine_threadsafe(queue.join(), loop).result(10)
                    finished = client.get("/api/downloads").get_json()
                    self.assertTrue(all(row["status"] == "SuccessDownload" for row in finished))
                    files = list(Path(app.save_path).rglob("*.txt"))
                    self.assertEqual(2 if comments else 1, len(files))
                    expected = telegram.messages[5].caption if media else telegram.messages[5].text
                    source_text = next(path for path in files if path.name == "5_202611.txt")
                    self.assertEqual(expected, source_text.read_text(encoding="utf-8"))
                    if comments:
                        comment_text = next(path for path in files if "_comment_" in path.name)
                        self.assertEqual("5_202611_comment_789_202612.txt", comment_text.name)
                        self.assertEqual(source_text.parent, comment_text.parent)
                        self.assertEqual("跨月评论原文", comment_text.read_text(encoding="utf-8"))
                    if media:
                        videos = list(Path(app.save_path).rglob("*.mp4"))
                        self.assertEqual(1, len(videos))
                        self.assertEqual(b"media", videos[0].read_bytes())
                    history = client.get("/api/history").get_json()
                    self.assertIn(str(CHAT), [row["chat_id"] for row in history])
                    self.assertEqual(DownloadStatus.SuccessDownload.name, history[0]["status"])
                    self.assertEqual(200, client.post("/api/runtime/stop").status_code)
            finally:
                async def cleanup():
                    tasks = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                asyncio.run_coroutine_threadsafe(cleanup(), loop).result(5)
                loop.call_soon_threadsafe(loop.stop)
                thread.join(5)
                loop.close()
                app.executor.shutdown(wait=True)
                asyncio.set_event_loop(None)

    @staticmethod
    def run_loop(loop, ready):
        asyncio.set_event_loop(loop)
        loop.call_soon(ready.set)
        loop.run_forever()
