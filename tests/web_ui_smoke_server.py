"""Local test-only HTTP server backed by a fake Telegram client and temporary state."""

from werkzeug.serving import make_server

from module.web import get_flask_app
from unittest.mock import patch
from tests.module.test_web_flow import CHAT
from tests.module.test_web_flow import WebFlowTestCase


def main():
    fixture = WebFlowTestCase()
    fixture.setUp()
    try:
        fixture.save_chat()
        fixture.start()
        for index, status in enumerate(["Queued", "Downloading", "SuccessDownload", "FailedDownload"], 1):
            fixture.app.web_state.record(CHAT, index, f"测试任务 {index}", status)
        telemetry = {CHAT: {2: {"down_byte": 50, "total_size": 100,
                                "download_speed": 20, "file_name": "clip.mp4"}}}
        stats_patch = patch("module.web.get_download_result", return_value=telemetry)
        stats_patch.start()
        fixture.addCleanup(stats_patch.stop)
        server = make_server("127.0.0.1", 0, get_flask_app())
        print(f"TEST_UI_URL=http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
    finally:
        fixture.doCleanups()


if __name__ == "__main__":
    main()
