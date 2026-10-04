"""Reset download history after explicit confirmation, preserving user settings."""

import os
import tempfile
from datetime import datetime
from io import StringIO
from pathlib import Path

from ruamel.yaml import YAML


def reset_history(root: Path):
    """Validate and back up both YAML files before resetting their history fields."""
    yaml = YAML()
    yaml.preserve_quotes = True
    updates = []
    cursor_count = 0
    retry_count = 0
    for name in ("config.yaml", "data.yaml"):
        path = root / name
        if name == "data.yaml" and not path.exists():
            continue
        original = path.read_bytes()
        document = yaml.load(original.decode("utf-8-sig").replace("\r\n", "\n"))
        if document is None and name == "data.yaml":
            continue
        if not isinstance(document, dict):
            raise ValueError(f"Invalid mapping in {name}")
        chats = document.get("chat") or []
        if not isinstance(chats, list) or any(
            not isinstance(chat, dict) for chat in chats
        ):
            raise ValueError(f"Invalid chat list in {name}")
        for entry in [document] + list(chats):
            if "last_read_message_id" in entry or (
                name == "config.yaml" and (entry is not document or "chat_id" in entry)
            ):
                entry["last_read_message_id"] = 0
                cursor_count += 1
            for key in ("ids_to_retry", "already_download_ids"):
                if key in entry:
                    values = entry[key]
                    if values is not None and not isinstance(values, list):
                        raise ValueError(f"Invalid history list in {name}")
                    retry_count += len(values or [])
                    entry[key] = []
        output = StringIO()
        yaml.dump(document, output)
        rendered = output.getvalue().encode("utf-8")
        if yaml.load(rendered.decode("utf-8")) != document:
            raise ValueError(f"Could not verify {name}")
        updates.append((path, original, rendered))

    backup_parent = root / "temp"
    backup_parent.mkdir(exist_ok=True)
    backup = Path(
        tempfile.mkdtemp(
            prefix=f"history-reset-backup-{datetime.now():%Y%m%d-%H%M%S}-",
            dir=backup_parent,
        )
    )
    for path, original, _ in updates:
        (backup / path.name).write_bytes(original)
    print(f"备份目录：{backup}")
    for path, _, rendered in updates:
        # Replace a complete file so interruption cannot leave truncated YAML.
        with tempfile.NamedTemporaryFile(dir=root, suffix=".tmp", delete=False) as temp:
            temporary_path = Path(temp.name)
            try:
                temp.write(rendered)
                temp.flush()
                os.fsync(temp.fileno())
            except BaseException:
                temp.close()
                temporary_path.unlink()
                raise
        try:
            os.replace(temporary_path, path)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()
    return cursor_count, retry_count


def main() -> int:
    """Only an exact lowercase y authorizes clearing the local download history."""
    print("请先关闭正在运行的下载器，避免旧进度被重新写回。")
    print("将清空下载进度和重试记录；保留配置、已下载媒体和登录状态。")
    try:
        answer = input("输入 y 并回车确认清除（其他输入取消）：")
    except (EOFError, KeyboardInterrupt):
        print("\n已取消，未修改任何文件。")
        return 0
    if answer != "y":
        print("已取消，未修改任何文件。")
        return 0
    try:
        cursors, retries = reset_history(Path(__file__).resolve().parent)
    except Exception as error:
        # YAML parser errors may include account details; do not print their text.
        print(f"清除失败（{type(error).__name__}）。如已生成备份，可从上方目录恢复。")
        return 1
    print(f"已重置 {cursors} 项下载进度，清空 {retries} 条历史/重试记录。")
    print("重新启动即可从头扫描；已存在的同名媒体仍会跳过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
