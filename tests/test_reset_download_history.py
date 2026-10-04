"""Check confirmation and history reset without touching the user's files."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ruamel.yaml import YAML

import reset_download_history as reset


class ResetDownloadHistoryTestCase(unittest.TestCase):
    def test_only_exact_y_clears_records(self):
        for answer in ("", "n", "Y", "yes", " y", "y "):
            with self.subTest(answer=answer), patch(
                "builtins.input", return_value=answer
            ), patch("builtins.print"), patch.object(reset, "reset_history") as clear:
                self.assertEqual(reset.main(), 0)
                clear.assert_not_called()
        with patch("builtins.input", return_value="y"), patch(
            "builtins.print"
        ), patch.object(reset, "reset_history", return_value=(1, 2)) as clear:
            self.assertEqual(reset.main(), 0)
            clear.assert_called_once_with(Path(reset.__file__).resolve().parent)

    def test_closed_input_cancels(self):
        with patch("builtins.input", side_effect=EOFError), patch(
            "builtins.print"
        ), patch.object(reset, "reset_history") as clear:
            self.assertEqual(reset.main(), 0)
            clear.assert_not_called()

    def test_reset_preserves_settings_and_backs_up_original_bytes(self):
        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"):
            root = Path(directory)
            config = (
                '# user settings\r\napi_hash: "test-value"\r\n'
                "file_path_prefix: [chat_title]\r\nsave_path: downloads\r\n"
                "chat:\r\n- chat_id: 123\r\n  last_read_message_id: 456\r\n"
                "  download_filter: id > 10\r\n- chat_id: 789\r\n"
            ).encode("utf-8")
            data = (
                "ids_to_retry: [1, 2]\nchat:\n- chat_id: 123\n"
                "  ids_to_retry: [3, 4]\nalready_download_ids: [5]\n"
            ).encode("utf-8")
            (root / "config.yaml").write_bytes(config)
            (root / "data.yaml").write_bytes(data)
            media = root / "123_202609_p1.mp4"
            media.write_bytes(b"downloaded media")
            session = root / "account.session"
            session.write_bytes(b"login session")

            self.assertEqual(reset.reset_history(root), (2, 5))

            yaml = YAML()
            result = yaml.load((root / "config.yaml").read_text(encoding="utf-8"))
            self.assertEqual(result["file_path_prefix"], ["chat_title"])
            self.assertEqual(result["api_hash"], "test-value")
            self.assertEqual(result["save_path"], "downloads")
            self.assertEqual(result["chat"][0]["download_filter"], "id > 10")
            self.assertEqual(
                [item["last_read_message_id"] for item in result["chat"]], [0, 0]
            )
            result = yaml.load((root / "data.yaml").read_text(encoding="utf-8"))
            self.assertEqual(result["ids_to_retry"], [])
            self.assertEqual(result["chat"][0]["ids_to_retry"], [])
            self.assertEqual(result["already_download_ids"], [])
            backup = next((root / "temp").iterdir())
            self.assertEqual((backup / "config.yaml").read_bytes(), config)
            self.assertEqual((backup / "data.yaml").read_bytes(), data)
            self.assertEqual(media.read_bytes(), b"downloaded media")
            self.assertEqual(session.read_bytes(), b"login session")
            self.assertEqual(list(root.glob("*.tmp")), [])

    def test_missing_data_file_is_supported(self):
        with tempfile.TemporaryDirectory() as directory, patch("builtins.print"):
            root = Path(directory)
            (root / "config.yaml").write_text("chat_id: 123\n", encoding="utf-8")
            self.assertEqual(reset.reset_history(root), (1, 0))
            self.assertFalse((root / "data.yaml").exists())

    def test_invalid_data_cannot_partially_clear_config(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = b"chat_id: 123\nlast_read_message_id: 456\n"
            (root / "config.yaml").write_bytes(original)
            (root / "data.yaml").write_bytes(b"chat: invalid\n")
            with self.assertRaises(ValueError):
                reset.reset_history(root)
            self.assertEqual((root / "config.yaml").read_bytes(), original)
            self.assertFalse((root / "temp").exists())
