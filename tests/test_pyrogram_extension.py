"""Regression tests for the Telegram media extension helper."""

import unittest
from unittest import mock

from pyrogram.file_id import FileType

import module.pyrogram_extension as extension


class ExtensionTestCase(unittest.TestCase):
    """mimetypes.guess_extension() already returns a dot, so never add a second one."""

    def test_guessed_video_extension_has_a_single_dot(self):
        with mock.patch.object(extension, "_get_file_type", return_value=FileType.VIDEO):
            suffix = extension.get_extension("video-file-id", "video/mp4")
        self.assertEqual(".mp4", suffix)
        self.assertEqual("10075_202609_p1.mp4", f"10075_202609_p1{suffix}")

    def test_guessed_document_extension_has_a_single_dot(self):
        with mock.patch.object(extension, "_get_file_type", return_value=FileType.DOCUMENT):
            self.assertEqual(".pdf", extension.get_extension("document-file-id", "application/pdf"))

    def test_bare_extension_has_no_dot(self):
        with mock.patch.object(extension, "_get_file_type", return_value=FileType.AUDIO):
            self.assertEqual("m4a", extension.get_extension("audio-file-id", "audio/mp4", False))

    def test_fallbacks_are_unchanged(self):
        with mock.patch.object(extension, "_get_file_type", return_value=FileType.PHOTO):
            self.assertEqual(".jpg", extension.get_extension("photo-file-id", ""))
        with mock.patch.object(extension, "_get_file_type", return_value=FileType.VIDEO):
            self.assertEqual(".mp4", extension.get_extension("video-file-id", ""))
        self.assertEqual(".unknown", extension.get_extension("", "video/mp4"))


if __name__ == "__main__":
    unittest.main()
