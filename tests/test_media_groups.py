"""Regression coverage for album names and unmodified text sidecars."""

import asyncio
import os
import tempfile
import unittest
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import media_downloader as downloader
from module.app import DownloadStatus, TaskNode

from .test_common import MockDocument, MockMessage, MockPhoto, MockVideo, get_extension


CHAT_ID = -100987654
GROUP_ID = "album-123"
FIRST_DATE = datetime(2026, 8, 31, 23, 59, 59)
LATER_DATE = datetime(2026, 9, 1, 0, 0, 1)


def album_message(message_id, **kwargs):
    """Build messages without contacting Telegram or loading user settings."""
    fields = {
        "id": message_id,
        "media": True,
        "media_group_id": GROUP_ID,
        "chat_id": CHAT_ID,
        "chat_title": "Album channel",
        "date": FIRST_DATE if message_id == 100 else LATER_DATE,
        "video": MockVideo(file_name="original.mp4", mime_type="video/mp4"),
    }
    fields.update(kwargs)
    return MockMessage(**fields)


class RawText(str):
    """Expose markup renderers to ensure saving uses the raw API string."""

    @property
    def html(self):
        raise AssertionError("Text must not be converted to HTML")

    @property
    def markdown(self):
        raise AssertionError("Text must not be converted to Markdown")


class MediaGroupTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        try:
            cls.previous_loop = asyncio.get_event_loop()
        except RuntimeError:
            cls.previous_loop = None

    @classmethod
    def tearDownClass(cls):
        # Legacy synchronous tests still retrieve the application's current loop.
        previous_loop = cls.previous_loop
        if previous_loop is None or previous_loop.is_closed():
            previous_loop = downloader.app.loop
        if not previous_loop.is_closed():
            asyncio.set_event_loop(previous_loop)
        super().tearDownClass()

    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.settings = patch.multiple(
            downloader.app,
            save_path=str(self.root / "downloads"),
            temp_save_path=str(self.root / "temp"),
            file_path_prefix=["chat_title", "media_datetime", "media_type"],
            file_name_prefix=["caption", "file_name"],
            file_name_prefix_split=" - ",
            date_format="%Y_%m",
            hide_file_name=False,
            enable_download_txt=False,
            caption_name_dict={},
            caption_entities_dict={},
        )
        self.settings.start()
        self.addCleanup(self.settings.stop)
        extensions = patch.object(downloader, "get_extension", get_extension)
        extensions.start()
        self.addCleanup(extensions.stop)
        download_cache = patch(
            "module.pyrogram_extension._download_cache", defaultdict(lambda: None)
        )
        download_cache.start()
        self.addCleanup(download_cache.stop)
        self.node = TaskNode(chat_id=CHAT_ID)
        self.client = SimpleNamespace(download_media=AsyncMock())

    def saved_path(self, name, media_type="msg", dirname="Album channel"):
        return self.root / "downloads" / dirname / "2026_08" / media_type / name

    async def test_single_message_does_not_fetch_an_album(self):
        message = album_message(100, media_group_id=None)
        with patch.object(
            downloader, "get_media_group_with_retry", new=AsyncMock()
        ) as get:
            result = await downloader._get_download_media_group(
                self.client, message, self.node
            )
        self.assertIsNone(result)
        get.assert_not_awaited()

    async def test_middle_member_resolves_album_with_first_members_month_and_positions(
        self,
    ):
        messages = [
            album_message(100),
            album_message(
                104,
                video=None,
                photo=MockPhoto(date=LATER_DATE, file_unique_id="photo-104"),
            ),
            album_message(
                110,
                video=None,
                document=MockDocument(
                    file_name="original.png", mime_type="application/octet-stream"
                ),
            ),
            album_message(205, video=MockVideo(mime_type="video/mp4")),
            album_message(
                240,
                video=None,
                photo=MockPhoto(date=LATER_DATE, file_unique_id="photo-240"),
            ),
        ]
        shuffled = [
            messages[3],
            album_message(98, empty=True),
            messages[2],
            album_message(99, media_group_id="different-album"),
            messages[0],
            messages[4],
            messages[1],
            messages[2],
        ]
        with patch.object(
            downloader,
            "get_media_group_with_retry",
            new=AsyncMock(return_value=shuffled),
        ) as get:
            group = await downloader._get_download_media_group(
                self.client, messages[2], self.node
            )
        get.assert_awaited_once_with(self.client, CHAT_ID, 110)
        self.assertEqual([100, 104, 110, 205, 240], [item.id for item in group])

        for position, (message, media_type, suffix) in enumerate(
            zip(
                messages,
                ["video", "photo", "document", "video", "photo"],
                [".mp4", ".jpg", ".png", ".mp4", ".jpg"],
            ),
            start=1,
        ):
            with self.subTest(message_id=message.id):
                file_name, temp_name, _ = await downloader._get_media_meta(
                    CHAT_ID,
                    message,
                    getattr(message, media_type),
                    media_type,
                    media_group=group,
                )
                expected_name = f"100_202608_p{position}{suffix}"
                self.assertEqual(
                    self.saved_path(expected_name, media_type), Path(file_name)
                )
                self.assertEqual(expected_name, os.path.basename(temp_name))

    async def test_concurrent_members_share_one_lookup_without_forwarding_cache(self):
        messages = [
            album_message(message_id) for message_id in [100, 104, 110, 205, 240]
        ]
        self.node.media_group_ids[GROUP_ID] = "forwarding-state"

        async def lookup(*_):
            await asyncio.sleep(0)
            return list(reversed(messages))

        with patch.object(
            downloader, "get_media_group_with_retry", new=AsyncMock(side_effect=lookup)
        ) as get:
            groups = await asyncio.gather(
                *(
                    downloader._get_download_media_group(
                        self.client, message, self.node
                    )
                    for message in reversed(messages)
                )
            )
        get.assert_awaited_once()
        self.assertTrue(
            all(
                [item.id for item in group] == [100, 104, 110, 205, 240]
                for group in groups
            )
        )
        self.assertEqual({GROUP_ID: "forwarding-state"}, self.node.media_group_ids)

    async def test_album_cache_is_independent_for_each_task(self):
        message = album_message(100)
        other_node = TaskNode(chat_id=CHAT_ID)
        with patch.object(
            downloader,
            "get_media_group_with_retry",
            new=AsyncMock(return_value=[message]),
        ) as get:
            await downloader._get_download_media_group(self.client, message, self.node)
            await downloader._get_download_media_group(self.client, message, other_node)
        self.assertEqual(2, get.await_count)
        self.assertIsNot(
            self.node.download_media_groups, other_node.download_media_groups
        )

    async def test_lookup_exception_is_not_cached(self):
        messages = [album_message(100), album_message(110)]
        with patch.object(
            downloader,
            "get_media_group_with_retry",
            new=AsyncMock(side_effect=[RuntimeError("temporary failure"), messages]),
        ) as get:
            with self.assertRaisesRegex(RuntimeError, "temporary failure"):
                await downloader._get_download_media_group(
                    self.client, messages[1], self.node
                )
            result = await downloader._get_download_media_group(
                self.client, messages[1], self.node
            )
        self.assertEqual([100, 110], [item.id for item in result])
        self.assertEqual(2, get.await_count)

    async def test_response_missing_current_message_is_rejected_and_retried(self):
        messages = [album_message(100), album_message(110)]
        with patch.object(
            downloader,
            "get_media_group_with_retry",
            new=AsyncMock(side_effect=[[messages[0]], messages]),
        ) as get:
            with self.assertRaises(ValueError):
                await downloader._get_download_media_group(
                    self.client, messages[1], self.node
                )
            result = await downloader._get_download_media_group(
                self.client, messages[1], self.node
            )
        self.assertEqual([100, 110], [item.id for item in result])
        self.assertEqual(2, get.await_count)

    async def test_all_text_fields_keep_raw_bytes_whitespace_and_repetitions(self):
        caption = RawText("  #标签 <tag> & literal **text**\r\n代码：\nprint('<&>')\t  ")
        text = RawText("普通文字\r\n黄色代码块\r\n    a < b && c > d\n")
        long_caption = RawText("长段落 <> & #标签\n" * 500)
        messages = [
            album_message(100, caption=caption, text=text),
            album_message(104, caption=caption),
            album_message(110, caption=long_caption, text=" \t\r\n"),
            album_message(205, caption="", text=None),
            album_message(240, text="最后一行\r"),
        ]
        downloader._save_media_group_text(CHAT_ID, messages)
        expected = "\n".join(
            [caption, text, caption, long_caption, " \t\r\n", "最后一行\r"]
        )
        file_path = self.saved_path("100_202608.txt")
        self.assertEqual(expected.encode("utf-8"), file_path.read_bytes())
        self.assertEqual([file_path], list((self.root / "downloads").rglob("*.txt")))

    async def test_album_without_text_still_has_empty_sidecar(self):
        downloader._save_media_group_text(
            CHAT_ID, [album_message(100, caption=None, text=""), album_message(110)]
        )
        self.assertEqual(b"", self.saved_path("100_202608.txt").read_bytes())

    async def test_text_path_uses_chat_id_when_title_is_missing(self):
        downloader._save_media_group_text(
            CHAT_ID, [album_message(100, chat_title=None, caption="#tag")]
        )
        self.assertEqual(
            b"#tag",
            self.saved_path("100_202608.txt", dirname=str(CHAT_ID)).read_bytes(),
        )

    async def test_existing_media_repairs_sidecar_with_text_download_disabled(self):
        messages = [album_message(100, caption="#tag\n原始文字"), album_message(110)]
        media_path = self.saved_path("100_202608_p2.mp4", "video")
        media_path.parent.mkdir(parents=True)
        media_path.write_bytes(b"already downloaded")
        with patch.object(
            downloader, "fetch_message", new=AsyncMock(return_value=messages[1])
        ), patch.object(
            downloader,
            "get_media_group_with_retry",
            new=AsyncMock(return_value=messages),
        ):
            status, file_name = await downloader.download_media(
                self.client, messages[1], ["video"], {"video": ["all"]}, self.node
            )
        self.assertEqual(DownloadStatus.SkipDownload, status)
        self.assertIsNone(file_name)
        self.assertEqual(
            "#tag\n原始文字".encode("utf-8"),
            self.saved_path("100_202608.txt").read_bytes(),
        )
        self.client.download_media.assert_not_awaited()

    async def test_concurrent_downloads_save_media_and_one_complete_text_file(self):
        messages = [
            album_message(100, caption="#tag\n第一段"),
            album_message(
                104,
                video=None,
                photo=MockPhoto(date=LATER_DATE, file_unique_id="photo-104"),
            ),
            album_message(
                110,
                video=None,
                document=MockDocument(file_name="image.png", mime_type="image/png"),
                caption="代码：\r\n    a < b && c > d",
            ),
            album_message(205),
            album_message(240, caption="最后一段"),
        ]

        async def fetch(_, message):
            return message

        async def transfer(message, file_name, **_):
            path = Path(file_name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(bytes([message.id]) * 1024)
            return str(path)

        self.client.download_media.side_effect = transfer
        with patch.object(downloader.app, "file_path_prefix", []), patch.object(
            downloader, "fetch_message", new=AsyncMock(side_effect=fetch)
        ), patch.object(
            downloader,
            "get_media_group_with_retry",
            new=AsyncMock(return_value=list(reversed(messages))),
        ) as get:
            results = await asyncio.gather(
                *(
                    downloader.download_media(
                        self.client,
                        message,
                        ["video", "photo", "document"],
                        {"video": ["all"], "document": ["all"]},
                        self.node,
                    )
                    for message in reversed(messages)
                )
            )

        get.assert_awaited_once()
        for (status, file_name), message in zip(results, reversed(messages)):
            self.assertEqual(DownloadStatus.SuccessDownload, status)
            self.assertEqual(bytes([message.id]) * 1024, Path(file_name).read_bytes())
        self.assertEqual(
            {
                "100_202608_p1.mp4",
                "100_202608_p2.jpg",
                "100_202608_p3.png",
                "100_202608_p4.mp4",
                "100_202608_p5.mp4",
                "100_202608.txt",
            },
            {path.name for path in (self.root / "downloads").iterdir()},
        )
        self.assertEqual(
            "#tag\n第一段\n代码：\r\n    a < b && c > d\n最后一段".encode("utf-8"),
            (self.root / "downloads" / "100_202608.txt").read_bytes(),
        )

    async def test_album_lookup_failure_does_not_download_with_individual_name(self):
        message = album_message(110)
        with patch.object(
            downloader, "fetch_message", new=AsyncMock(return_value=message)
        ), patch.object(
            downloader,
            "get_media_group_with_retry",
            new=AsyncMock(side_effect=RuntimeError("offline")),
        ):
            status, file_name = await downloader.download_media(
                self.client, message, ["video"], {"video": ["all"]}, self.node
            )
        self.assertEqual(DownloadStatus.FailedDownload, status)
        self.assertIsNone(file_name)
        self.client.download_media.assert_not_awaited()
        self.assertEqual([], list(self.root.rglob("*.txt")))

    async def test_rejected_media_format_does_not_create_sidecar(self):
        message = album_message(100, caption="#tag")
        with patch.object(
            downloader, "fetch_message", new=AsyncMock(return_value=message)
        ), patch.object(
            downloader,
            "get_media_group_with_retry",
            new=AsyncMock(return_value=[message]),
        ):
            status, _ = await downloader.download_media(
                self.client, message, ["video"], {"video": ["mov"]}, self.node
            )
        self.assertEqual(DownloadStatus.SkipDownload, status)
        self.assertEqual([], list(self.root.rglob("*.txt")))
        self.client.download_media.assert_not_awaited()

    async def test_single_media_uses_own_id_month_and_p1(self):
        message = album_message(100, media_group_id=None, caption="#tag")
        file_name, temp_name, file_format = await downloader._get_media_meta(
            CHAT_ID, message, message.video, "video"
        )
        self.assertEqual(self.saved_path("100_202608_p1.mp4", "video"), Path(file_name))
        self.assertEqual("100_202608_p1.mp4", os.path.basename(temp_name))
        self.assertEqual("mp4", file_format)
        self.assertEqual([], list(self.root.rglob("*.txt")))

    async def test_missing_date_uses_stable_placeholder_for_media_and_text(self):
        messages = [album_message(100, date=None, caption="#tag"), album_message(110)]
        with patch.object(downloader.app, "file_path_prefix", []):
            file_name, temp_name, _ = await downloader._get_media_meta(
                CHAT_ID, messages[1], messages[1].video, "video", media_group=messages
            )
            text_name = downloader._save_media_group_text(CHAT_ID, messages)
        self.assertEqual(self.root / "downloads" / "100_000000_p2.mp4", Path(file_name))
        self.assertEqual("100_000000_p2.mp4", os.path.basename(temp_name))
        self.assertEqual(self.root / "downloads" / "100_000000.txt", Path(text_name))
        self.assertEqual(b"#tag", Path(text_name).read_bytes())

    async def test_flat_single_media_name_does_not_depend_on_directory_date_format(
        self,
    ):
        message = album_message(104, media_group_id=None)
        with patch.object(downloader.app, "file_path_prefix", []), patch.object(
            downloader.app, "date_format", "%d-%m-%Y"
        ):
            file_name, temp_name, _ = await downloader._get_media_meta(
                CHAT_ID, message, message.video, "video"
            )
        self.assertEqual(self.root / "downloads" / "104_202609_p1.mp4", Path(file_name))
        self.assertEqual("104_202609_p1.mp4", os.path.basename(temp_name))


if __name__ == "__main__":
    unittest.main()
