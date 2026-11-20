"""Exercise successful web requests across a running asyncio client loop."""

import asyncio
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pyrogram import raw
from pyrogram.utils import get_channel_id

import module.web as web
from module.app import Application, TaskNode
from module.web_state import WebState
from tests.test_common import MockMessage


CHAT = -100123456
GROUP_CHANNEL = 3743604771
DISCUSSION = get_channel_id(GROUP_CHANNEL)


def message(message_id, chat_id=CHAT, text=None):
    return MockMessage(
        id=message_id, chat_id=chat_id, chat_title="Test channel",
        text=text or f"消息 {message_id}\n完整原文 <&>", date=datetime(2026, 11, 20),
        media=None,
    )


class TelegramFixture:
    """A deterministic Telegram transport, without bypassing web/loop code."""

    def __init__(self):
        self.messages = {i: message(i) for i in range(1, 8)}
        self.history_calls = []
        self.discussion_requests = []
        self.reply_requests = []
        # post id -> discussion copies as (root id, reply count or None)
        self.discussion_copies = {}
        self.fetch_peers = AsyncMock(return_value=False)
        self.resolve_peer = AsyncMock(side_effect=lambda chat_id: chat_id)
        self.invoke = AsyncMock(side_effect=self.handle_invoke)
        self.get_messages = AsyncMock(side_effect=self.fetch)
        self.get_chat = AsyncMock(return_value=SimpleNamespace(id=CHAT, title="Test channel"))

    async def handle_invoke(self, request):
        if isinstance(request, raw.functions.messages.GetDiscussionMessage):
            self.discussion_requests.append(request.msg_id)
            copies = self.discussion_copies.get(request.msg_id, [(99, 2)])
            group = SimpleNamespace(id=GROUP_CHANNEL, access_hash=7, megagroup=True)
            return SimpleNamespace(
                messages=[
                    SimpleNamespace(
                        id=root_id,
                        replies=None if count is None else SimpleNamespace(replies=count),
                    )
                    for root_id, count in copies
                ],
                chats=[group], users=[],
            )
        raise AssertionError(f"unexpected request {request!r}")

    async def fetch(self, chat_id, message_ids):
        return self.messages[message_ids]

    async def get_chat_history(self, chat_id, limit=0, offset_id=0, **kwargs):
        self.history_calls.append((chat_id, limit, offset_id))
        candidates = [m for i, m in sorted(self.messages.items(), reverse=True)
                      if not offset_id or i < offset_id]
        for item in candidates[:limit or len(candidates)]:
            yield item

    async def get_discussion_replies(self, chat_id, message_id, limit=0):
        self.reply_requests.append((chat_id, message_id))
        yield message(5, DISCUSSION, "评论完整原文")


class WebFlowTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        try:
            self.previous_loop = asyncio.get_event_loop()
        except RuntimeError:
            # Earlier test modules may have detached the loop from this thread.
            self.previous_loop = None
        state = WebState(str(Path(self.temp.name) / "state.sqlite3"))
        with patch("module.app.WebState", return_value=state):
            self.app = Application("unused.yaml", "unused-data.yaml")
        self.loop = self.app.loop
        self.telegram = TelegramFixture()
        self.queued = []
        self.intercept_queue = True
        self.start_calls = 0
        self.activate_calls = 0
        self.stop_calls = 0

        async def enqueue(msg, node):
            if hasattr(self, "intercept_queue") and self.intercept_queue:
                self.queued.append((msg, node))
                self.app.web_state.record(
                    node.chat_id, msg.id, msg.caption or msg.text or "", "Queued"
                )
                return True
            import media_downloader as downloader
            return await downloader.add_download_task(msg, node)

        async def start():
            self.start_calls += 1
            return self.telegram, True

        async def activate(client):
            self.activate_calls += 1

        async def stop():
            self.stop_calls += 1

        state.set_settings({"api_id": 123, "api_hash": "test-hash"})
        self.runtime_patch = patch.dict(web._web_runtime, clear=True)
        self.runtime_patch.start()
        self.addCleanup(self.runtime_patch.stop)
        web.configure_web_runtime(self.app, self.loop, enqueue, start, activate, stop)
        web._preview_cache.clear()
        ready = threading.Event()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.call_soon(ready.set)
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        self.assertTrue(ready.wait(5))
        self.addCleanup(self.close_loop)
        self.client = web.get_flask_app().test_client()

    def close_loop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()
        self.app.executor.shutdown(wait=True)
        if self.previous_loop and not self.previous_loop.is_closed():
            asyncio.set_event_loop(self.previous_loop)
        else:
            asyncio.set_event_loop(None)

    def start(self):
        response = self.client.post("/api/runtime/start")
        self.assertEqual(200, response.status_code, response.get_json())
        self.assertTrue(response.get_json()["running"])

    def preview(self, **payload):
        response = self.client.post("/api/task/preview", json={
            "links": "https://t.me/du_shi_li_ren/5", **payload,
        })
        self.assertEqual(200, response.status_code, response.get_json())
        result = response.get_json()["results"][0]
        self.assertNotIn("error", result, result)
        self.assertTrue(result["messages"], result)
        return result

    def submit(self, result, indexes=None):
        response = self.client.post("/api/task/submit", json={"selected": [{
            "preview_id": result["preview_id"],
            "indexes": indexes if indexes is not None else list(range(len(result["messages"]))),
        }]})
        self.assertEqual(200, response.status_code, response.get_json())
        return response.get_json()

    def save_chat(self, **fields):
        response = self.client.put("/api/task/chats", json={"chats": [{
            "chat_id": "du_shi_li_ren", "last_read_message_id": 2,
            "end_message_id": 6, "preview_limit": 100, **fields,
        }]})
        self.assertEqual(200, response.status_code, response.get_json())

    def test_start_link_preview_submit_and_stop(self):
        self.start()
        result = self.preview()
        self.assertEqual(CHAT, result["messages"][0]["chat_id"])
        self.assertIn("完整原文 <&>", result["messages"][0]["text"])
        self.assertEqual(1, self.submit(result)["queued"])
        msg, node = self.queued[0]
        self.assertEqual(CHAT, node.chat_id)
        self.assertTrue(node.save_text_messages)
        response = self.client.post("/api/runtime/stop")
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, self.stop_calls)
        self.assertEqual(503, self.client.post("/api/task/submit", json={"selected": []}).status_code)

    def test_chat_progress_round_trip_and_range_preview(self):
        self.save_chat()
        saved = self.client.get("/api/task/chats").get_json()[0]
        self.assertEqual(2, saved["last_read_message_id"])
        self.assertEqual(6, saved["end_message_id"])
        self.assertEqual(100, saved["preview_limit"])
        self.start()
        response = self.client.post("/api/task/preview-chats", json={"chat_ids": ["du_shi_li_ren"]})
        self.assertEqual(200, response.status_code, response.get_json())
        result = response.get_json()["results"][0]
        self.assertNotIn("error", result, result)
        self.assertEqual([3, 4, 5, 6], [item["id"] for item in result["messages"]])
        self.assertEqual(4, self.submit(result)["queued"])
        self.assertTrue(all(node.chat_id == CHAT for _, node in self.queued))

    def test_invalid_chat_progress_does_not_partially_save(self):
        self.save_chat()
        before = self.app.web_state.get_settings()
        response = self.client.put("/api/task/chats", json={"chats": [
            {"chat_id": "new_channel", "last_read_message_id": -1},
        ]})
        self.assertEqual(400, response.status_code)
        self.assertEqual(before, self.app.web_state.get_settings())

    def test_chat_filter_used_for_username_and_numeric_chat(self):
        self.save_chat(download_filter="id > 4")
        self.start()
        response = self.client.post("/api/task/preview-chats", json={"chat_ids": ["du_shi_li_ren"]})
        result = response.get_json()["results"][0]
        self.assertNotIn("error", result, result)
        selected = [i for i, item in enumerate(result["messages"]) if item["selected"]]
        self.assertEqual([5, 6], [result["messages"][i]["id"] for i in selected])
        self.assertEqual(2, self.submit(result)["queued"])

    def test_comments_use_discussion_chat_identity(self):
        self.start()
        result = self.preview(include_comments=True)
        self.assertEqual([CHAT, DISCUSSION], [item["chat_id"] for item in result["messages"]])
        self.assertEqual(2, self.submit(result)["queued"])
        self.assertEqual([CHAT, DISCUSSION], [node.chat_id for _, node in self.queued])

    def test_album_thread_root_skips_copy_without_replies(self):
        # Telegram returns one copy per album item, newest first, and only the
        # copy holding the thread answers messages.GetReplies.
        self.telegram.discussion_copies[5] = [(10193, None), (10192, 3)]
        self.start()
        result = self.preview(include_comments=True)
        self.assertEqual([CHAT, DISCUSSION], [item["chat_id"] for item in result["messages"]])
        self.assertEqual([(DISCUSSION, 10192)], self.telegram.reply_requests)

    def test_album_comment_filename_origin_uses_first_post_message(self):
        self.telegram.messages[5].media_group_id = "source-album"
        self.telegram.messages[2].media_group_id = "source-album"
        self.telegram.get_media_group = AsyncMock(return_value=[
            self.telegram.messages[5], self.telegram.messages[2],
        ])
        self.start()
        result = self.preview(include_comments=True)
        comment_index = next(i for i, row in enumerate(result["messages"]) if row["is_comment"])
        self.assertEqual(1, self.submit(result, [comment_index])["queued"])
        _, node = self.queued[0]
        self.assertEqual(DISCUSSION, node.chat_id)
        self.assertEqual(2, node.comment_source.id)
        self.assertEqual(CHAT, node.comment_source.chat.id)
        self.assertEqual([2], self.telegram.discussion_requests)

    def test_blacklist_rechecked_after_preview(self):
        self.start()
        result = self.preview()
        self.app.web_state.block(CHAT, 5, "blocked after preview")
        response = self.submit(result)
        self.assertEqual(0, response["queued"])
        self.assertEqual([], self.queued)

    def test_duplicate_selected_indexes_queue_once(self):
        self.start()
        result = self.preview()
        self.assertEqual(1, self.submit(result, [0, 0])["queued"])
        self.assertEqual(1, len(self.queued))

    def test_preview_category_selection_and_comment_error(self):
        self.start()
        result = self.preview(categories=[])
        self.assertFalse(result["messages"][0]["selected"])
        self.telegram.invoke.side_effect = RuntimeError("no comments access")
        result = self.preview(include_comments=True)
        self.assertEqual(1, len(result["messages"]))
        self.assertTrue(result.get("warnings") or result.get("warning"))

    def test_link_preview_applies_saved_username_filter(self):
        self.save_chat(download_filter="id > 5")
        self.start()
        result = self.preview()
        self.assertTrue(result["messages"][0]["filtered"])
        self.assertFalse(result["messages"][0]["selected"])
        self.assertEqual(0, self.submit(result)["queued"])

    def test_status_serializes_quotes_backslashes_and_empty_total(self):
        records = {CHAT: {5: {
            "total_size": 0, "down_byte": 0, "download_speed": 0,
            "file_name": 'C:\\downloads\\a"b.mp4',
        }}}
        with patch.object(web, "get_download_result", return_value=records):
            response = self.client.get("/get_download_list?already_down=false")
        self.assertEqual(200, response.status_code)
        self.assertIn('a"b.mp4', response.get_json()[0]["filename"])
        self.assertEqual("0", response.get_json()[0]["download_progress"])

    def test_global_pause_resume_and_active_task_removal(self):
        self.intercept_queue = False
        import media_downloader as downloader
        with patch.object(downloader, "app", self.app):
            self.app.web_state.record(CHAT, 12, "waiting", "Queued")
        pause = self.client.post("/api/downloads/pause", json={})
        self.assertEqual(200, pause.status_code, pause.get_json())
        self.assertTrue(pause.get_json()["paused"])
        with patch.object(web, "get_download_result", return_value={}):
            rows = self.client.get("/api/downloads").get_json()
        self.assertTrue(rows[0]["global_paused"])
        resume = self.client.post("/api/downloads/resume", json={})
        self.assertFalse(resume.get_json()["paused"])
        self.start()
        async def enqueue_one():
            import media_downloader as downloader
            return await downloader.add_download_task(message(12), TaskNode(chat_id=CHAT))
        from module.app import TaskNode
        with patch.object(downloader, "app", self.app):
            self.assertTrue(asyncio.run_coroutine_threadsafe(enqueue_one(), self.loop).result(5))
        removed = self.client.delete("/api/downloads/remove", json={"chat_id": str(CHAT), "message_id": 12})
        self.assertEqual(200, removed.status_code, removed.get_json())
        self.assertIsNone(self.app.web_state.task(CHAT, 12))

    def test_remove_active_task_cancels_on_client_loop(self):
        self.app.web_state.record(CHAT, 18, "active", "Downloading")
        started, cancelled = threading.Event(), threading.Event()

        async def active():
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def create_task():
            return asyncio.create_task(active())
        task = asyncio.run_coroutine_threadsafe(create_task(), self.loop).result(5)
        self.assertTrue(started.wait(5))
        with web.active_download_lock:
            web.active_download_tasks[(str(CHAT), 18)] = (TaskNode(CHAT), task)
        response = self.client.delete("/api/downloads/remove", json={"chat_id": str(CHAT), "message_id": 18})
        self.assertEqual(200, response.status_code, response.get_json())
        self.assertTrue(cancelled.wait(5))
        self.assertIsNone(self.app.web_state.task(CHAT, 18))
        with web.active_download_lock:
            web.active_download_tasks.pop((str(CHAT), 18), None)

    def test_retry_comment_resolves_from_source_post_thread(self):
        self.start()
        self.app.web_state.record(
            DISCUSSION, 5, "failed comment", "FailedDownload",
            origin_chat_id=CHAT, origin_message_id=5,
        )
        response = self.client.post(
            "/api/downloads/retry",
            json={"chat_id": str(DISCUSSION), "message_id": 5},
        )
        self.assertEqual(200, response.status_code, response.get_json())
        self.assertEqual([5], self.telegram.discussion_requests)
        msg, node = self.queued[-1]
        self.assertEqual(DISCUSSION, msg.chat.id)
        self.assertEqual(CHAT, node.comment_source.chat.id)
        self.assertEqual(5, node.comment_source.id)

    def test_retry_requeues_failed_message(self):
        self.start()
        self.app.web_state.record(CHAT, 5, "failed", "FailedDownload")
        response = self.client.post("/api/downloads/retry", json={"chat_id": str(CHAT), "message_id": 5})
        self.assertEqual(200, response.status_code, response.get_json())
        self.assertEqual("Queued", self.app.web_state.task(CHAT, 5)["status"])
        self.assertEqual(5, self.queued[0][0].id)

    def test_combined_download_list_contains_queue_progress_and_history(self):
        state = self.app.web_state
        state.record(CHAT, 1, "waiting", "Queued")
        state.record(CHAT, 2, "active", "Downloading")
        state.record(CHAT, 3, "finished", "SuccessDownload")
        records = {CHAT: {2: {"down_byte": 50, "total_size": 100,
                              "download_speed": 20, "file_name": "clip.mp4"}}}
        with patch.object(web, "get_download_result", return_value=records):
            response = self.client.get("/api/downloads")
        self.assertEqual(200, response.status_code)
        data = {row["message_id"]: row for row in response.get_json()}
        self.assertEqual({1, 2, 3}, set(data))
        self.assertEqual("Queued", data[1]["status"])
        self.assertEqual(50, data[2]["download_progress"])
        self.assertEqual(20, data[2]["download_speed"])
        self.assertEqual(100, data[3]["download_progress"])

    def test_pending_connection_not_recreated(self):
        async def pending():
            self.start_calls += 1
            return self.telegram, False
        web._web_runtime["start_client"] = pending
        first = self.client.post("/api/runtime/start").get_json()
        second = self.client.post("/api/runtime/start").get_json()
        self.assertTrue(first["needs_phone"])
        self.assertTrue(second["needs_phone"])
        self.assertEqual(1, self.start_calls)


if __name__ == "__main__":
    unittest.main()
