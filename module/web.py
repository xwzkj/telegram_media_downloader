"""web ui for media download"""

import asyncio
import logging
import mimetypes
import os
import threading
from concurrent.futures import TimeoutError as FutureTimeoutError
from functools import wraps
import uuid
from datetime import datetime
from secrets import token_urlsafe
from urllib.parse import unquote, urlparse

from flask import Flask, jsonify, redirect, render_template, request
from flask_login import LoginManager, UserMixin, login_required, login_user

import utils
from module.app import Application, ChatDownloadConfig, TaskNode
from module.filter import Filter
from utils.meta_data import MetaData
from module.download_stat import (
    DownloadState,
    get_download_result,
    get_download_state,
    get_total_download_speed,
    set_download_state,
)
from utils.crypto import AesBase64
from utils.format import extract_info_from_link, format_byte

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

_flask_app = Flask(__name__)

_flask_app.secret_key = "tdl"
_login_manager = LoginManager()
_login_manager.login_view = "login"
_login_manager.init_app(_flask_app)
web_login_users: dict = {}
deAesCrypt = AesBase64("1234123412ABCDEF", "ABCDEF1234123412")
_web_runtime: dict = {
    "app": None, "client": None, "loop": None, "add_download_task": None,
    "start_client": None, "activate_client": None, "stop_client": None,
    "running": False, "authorized": False,
}
_preview_cache: dict = {}
_chat_aliases: dict = {}
_preview_lock = threading.Lock()
_settings_lock = threading.RLock()
_lifecycle_lock = threading.RLock()


def _serialize_lifecycle(function):
    @wraps(function)
    def serialized(*args, **kwargs):
        with _lifecycle_lock:
            return function(*args, **kwargs)
    return serialized


def _parse_proxy(settings):
    proxy_url = str(settings.get("proxy_url") or "").strip()
    if not proxy_url:
        proxy = settings.get("proxy") or {}
        if not isinstance(proxy, dict):
            raise ValueError("proxy must be an object")
        return proxy
    parsed = urlparse(proxy_url)
    if parsed.scheme not in ("socks4", "socks5", "http", "https") or not parsed.hostname or not parsed.port:
        raise ValueError("代理 URL 必须包含有效的协议、主机和端口")
    proxy = {"scheme": parsed.scheme, "hostname": parsed.hostname, "port": parsed.port}
    if parsed.username:
        proxy["username"] = unquote(parsed.username)
    if parsed.password:
        proxy["password"] = unquote(parsed.password)
    return proxy


def _chat_key(value):
    if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip():
        raise ValueError("chat_id must be a non-empty ID or username")
    value = str(value).strip()
    try:
        return int(value)
    except ValueError:
        username = value.lstrip("@").lower()
        if not username or not all(character.isascii() and (character.isalnum() or character == "_") for character in username):
            raise ValueError("chat_id must be a Telegram ID or username")
        return username


def _nonnegative_integer(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{name} must be a non-negative integer")
    try:
        number = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a non-negative integer") from error
    if number < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return number


def _normalize_chats(chats, current_entries=None, current_configs=None):
    entries, seen = [], set()
    previous = {_chat_key(item["chat_id"]): item for item in (current_entries or [])}
    for item in chats:
        if not isinstance(item, dict):
            raise ValueError("Each Chat must be an object")
        chat_id = _chat_key(item.get("chat_id"))
        if chat_id in seen:
            raise ValueError(f"Duplicate Chat: {chat_id}")
        seen.add(chat_id)
        old = previous.get(chat_id, {})
        current = (current_configs or {}).get(chat_id)
        last_read = _nonnegative_integer(item.get("last_read_message_id", getattr(current, "last_read_message_id", old.get("last_read_message_id", 0))), "last_read_message_id")
        end = _nonnegative_integer(item.get("end_message_id", old.get("end_message_id", 0)), "end_message_id")
        limit = _nonnegative_integer(item.get("preview_limit", old.get("preview_limit", 100)), "preview_limit")
        if end and end < last_read:
            raise ValueError("end_message_id must be at least last_read_message_id, or 0")
        if not 1 <= limit <= 500:
            raise ValueError("preview_limit must be between 1 and 500")
        expression = item.get("download_filter", "") or ""
        if not isinstance(expression, str):
            raise ValueError("download_filter must be a string")
        entries.append({"chat_id": chat_id, "download_filter": expression.strip(), "last_read_message_id": last_read, "end_message_id": end, "preview_limit": limit})
    return entries


class User(UserMixin):
    """Web Login User"""

    def __init__(self):
        self.sid = "root"

    @property
    def id(self):
        """ID"""
        return self.sid


@_login_manager.user_loader
def load_user(_):
    """
    Load a user object from the user ID.

    Returns:
        User: The user object.
    """
    return User()


def get_flask_app() -> Flask:
    """get flask app instance"""
    return _flask_app


def run_web_server(app: Application):
    """
    Runs a web server using the Flask framework.
    """

    get_flask_app().run(
        app.web_host, app.web_port, debug=app.debug_web, use_reloader=False
    )


# pylint: disable = W0603
def configure_web_runtime(app, loop, add_download_task, start_client, activate_client, stop_client):
    """Provide runtime hooks; the Telegram client is created on explicit start."""
    settings = app.web_state.get_settings()
    app.web_state.interrupt_pending()
    with _preview_lock:
        _preview_cache.clear()
        _chat_aliases.clear()
    app.web_login_secret = settings.get("web_login_secret", "")
    _flask_app.secret_key = settings.get("session_secret") or token_urlsafe(32)
    settings.setdefault("session_secret", _flask_app.secret_key)
    app.web_state.set_settings(settings)
    app.api_id = settings.get("api_id", "")
    app.api_hash = settings.get("api_hash", "")
    app.bot_token = settings.get("bot_token", "")
    app.save_path = settings.get("save_path", app.save_path)
    app.media_types = settings.get("media_types", ["audio", "photo", "video", "document", "voice", "video_note"])
    app.file_formats = settings.get("file_formats", {"audio": ["all"], "document": ["all"], "video": ["all"]})
    app.max_download_task = settings.get("max_download_task", 3)
    app.proxy = _parse_proxy(settings)
    app.max_concurrent_transmissions = app.max_download_task * 5
    chats = _normalize_chats(settings.get("chats", []))
    app.config["chat"] = chats
    app.chat_download_config.clear()
    for item in chats:
        chat_id = item.get("chat_id")
        if chat_id is None:
            continue
        config = ChatDownloadConfig()
        config.last_read_message_id = item.get("last_read_message_id", 0)
        config.download_filter = item.get("download_filter", "")
        config.end_message_id = item["end_message_id"]
        config.preview_limit = item["preview_limit"]
        app.chat_download_config[chat_id] = config
    _web_runtime.update(
        app=app, client=None, loop=loop, add_download_task=add_download_task,
        start_client=start_client, activate_client=activate_client,
        stop_client=stop_client, running=False, authorized=False,
    )
    if settings.get("web_login_secret"):
        global web_login_users
        web_login_users = {"root": settings["web_login_secret"]}
        _flask_app.config["LOGIN_DISABLED"] = False
    else:
        web_login_users = {}
        _flask_app.config["LOGIN_DISABLED"] = True


def _run_on_client_loop(coroutine):
    loop = _web_runtime.get("loop")
    if loop is None or not loop.is_running():
        coroutine.close()
        raise RuntimeError("Telegram client is not ready")
    future = asyncio.run_coroutine_threadsafe(coroutine, loop)
    try:
        return future.result(timeout=300)
    except FutureTimeoutError:
        future.cancel()
        raise RuntimeError("Telegram operation timed out; the pending operation was cancelled") from None


def _media_type(message):
    return next((kind for kind in MetaData.AVAILABLE_MEDIA if getattr(message, kind, None) is not None), None)


def _message_preview(message):
    media_type = _media_type(message)
    date = getattr(message, "date", None)
    chat = getattr(message, "chat", None)
    return {
        "id": message.id,
        "date": date.isoformat() if date else "",
        "text": getattr(message, "text", None) or getattr(message, "caption", None) or "",
        "media": media_type,
        "media_label": f"[{media_type.upper()}]" if media_type else "",
        "category": media_type or "text",
        "chat_id": chat.id if chat else "",
        "selected": True,
        "blocked": False,
        "filtered": False,
        "disabled": False,
        "is_comment": False,
    }


def _message_metadata(message):
    # Filter metadata must also work for text and media with no decodable file_id.
    sender = getattr(message, "from_user", None)
    media_type = _media_type(message)
    media = getattr(message, media_type, None) if media_type else None
    filename = getattr(media, "file_name", None) or ""
    extension = os.path.splitext(filename)[1].lstrip(".").lower()
    if media and not extension:
        extension = (mimetypes.guess_extension(getattr(media, "mime_type", None) or "") or "").lstrip(".")
    return MetaData(
        message_date=getattr(message, "date", None),
        message_id=message.id,
        message_caption=getattr(message, "caption", None) or getattr(message, "text", None) or "",
        sender_id=getattr(sender, "id", 0), sender_name=getattr(sender, "username", None) or "",
        reply_to_message_id=getattr(message, "reply_to_message_id", None) or 1,
        message_thread_id=getattr(message, "message_thread_id", None) or 1,
        media_type=media_type, media_file_name=filename, file_extension=extension,
        media_file_size=getattr(media, "file_size", None),
        media_width=getattr(media, "width", None), media_height=getattr(media, "height", None),
        media_duration=getattr(media, "duration", None),
    )


def _matching_chat_settings(app, message, chat_id, configured_chat_id=None):
    chats = app.web_state.get_settings().get("chats", [])
    aliases = {_chat_key(chat_id)}
    with _preview_lock:
        aliases.update(_chat_aliases.get(str(chat_id), set()))
    username = getattr(getattr(message, "chat", None), "username", None)
    if username:
        aliases.add(_chat_key(username))
    matches = [item for item in chats if _chat_key(item["chat_id"]) in aliases]
    if configured_chat_id is not None:
        configured_key = _chat_key(configured_chat_id)
        matches.extend(item for item in chats if _chat_key(item["chat_id"]) == configured_key and item not in matches)
    return matches


def _message_allowed(app, message, chat_id, configured_chat_id=None):
    media_type = _media_type(message)
    if not media_type and not getattr(message, "text", None):
        return False
    if media_type and media_type not in app.media_types:
        return False
    metadata = _message_metadata(message)
    if media_type in ("audio", "document", "video"):
        formats = app.file_formats.get(media_type, ["all"])
        if "all" not in formats and metadata.file_extension not in formats:
            return False
    for chat in _matching_chat_settings(app, message, chat_id, configured_chat_id):
        expression = chat.get("download_filter")
        if expression:
            evaluator = Filter()
            evaluator.set_meta_data(metadata)
            try:
                if not evaluator.exec(expression):
                    return False
            except Exception as error:
                raise ValueError(f"Chat {chat['chat_id']} 的筛选表达式错误: {error}") from error
    return True


def _store_preview(app, chat_id, messages, categories=None, configured_chat_id=None):
    previews, records, seen = [], [], set()
    blocked_ids = app.web_state.blocked_ids()
    for record in messages:
        message, is_comment, item_chat_id = record[:3]
        origin = record[3] if len(record) > 3 else None
        origin_ref = record[4] if len(record) > 4 else ""
        actual_chat_id = getattr(getattr(message, "chat", None), "id", None)
        if actual_chat_id is None:
            raise ValueError("Message has no resolved Telegram Chat ID")
        key = (str(actual_chat_id), message.id)
        if key in seen or getattr(message, "empty", False):
            continue
        seen.add(key)
        preview = _message_preview(message)
        preview.update(chat_id=actual_chat_id, is_comment=is_comment)
        preview["blocked"] = key in blocked_ids
        preview["filtered"] = not _message_allowed(app, message, actual_chat_id, configured_chat_id)
        preview["disabled"] = preview["blocked"] or preview["filtered"]
        preview["selected"] = not preview["disabled"] and (categories is None or preview["category"] in categories)
        previews.append(preview)
        records.append((message, is_comment, actual_chat_id, origin, origin_ref))
    preview_id = uuid.uuid4().hex
    with _preview_lock:
        cutoff = datetime.utcnow()
        for key, old in list(_preview_cache.items()):
            if (cutoff - old["created_at"]).total_seconds() > 3600:
                del _preview_cache[key]
        _preview_cache[preview_id] = {
            "chat_id": chat_id, "messages": [item[0] for item in records],
            "message_chats": [item[2] for item in records],
            "is_comments": [item[1] for item in records],
            "origins": [item[3] for item in records],
            "origin_refs": [item[4] for item in records],
            "configured_chat_id": configured_chat_id, "created_at": cutoff,
        }
    return {"preview_id": preview_id, "chat_id": chat_id, "messages": previews}


async def _append_comments(client, messages, warnings, limit=1000):
    remaining = limit
    processed = set()
    for record in list(messages):
        message, is_comment, chat_id = record[:3]
        if is_comment:
            continue
        if remaining <= 0:
            warnings.append(f"评论预览达到上限 {limit}，可能已截断")
            break
        group_id = getattr(message, "media_group_id", None)
        post_key = (chat_id, group_id or message.id)
        if post_key in processed:
            continue
        processed.add(post_key)
        source = min(
            (item[0] for item in messages
             if not item[1] and item[2] == chat_id
             and group_id and getattr(item[0], "media_group_id", None) == group_id),
            key=lambda item: item.id, default=message,
        )
        if group_id:
            try:
                album = await client.get_media_group(chat_id, message.id)
                source = min(album or [source], key=lambda item: item.id)
            except Exception as error:
                warnings.append(f"原帖相册 {message.id} 无法完整解析: {error}")
        try:
            root = await client.get_discussion_message(chat_id, message.id)
            if root is None:
                warnings.append(f"消息 {message.id} 没有可访问的评论区")
                continue
            async for reply in client.get_discussion_replies(root.chat.id, root.id, limit=remaining):
                messages.append((reply, True, reply.chat.id, source))
                remaining -= 1
                if remaining <= 0:
                    warnings.append(f"评论预览达到上限 {limit}，可能已截断")
                    break
        except Exception as error:
            warnings.append(f"消息 {message.id} 的评论不可用: {error}")


def _preview_link_messages(client, link, include_comments):
    from module.pyrogram_extension import parse_link

    async def fetch():
        chat_id, message_id, topic_id = await parse_link(client, link)
        configured_alias = chat_id
        if chat_id is None:
            raise ValueError("无法识别 Telegram 链接")
        if message_id is None:
            chat = await client.get_chat(chat_id)
            with _preview_lock:
                _chat_aliases.setdefault(str(chat.id), set()).add(_chat_key(configured_alias))
            messages = []
            async for message in client.get_chat_history(chat.id, limit=20):
                messages.append((message, False, chat.id))
            warnings = []
            if include_comments:
                await _append_comments(client, messages, warnings)
            messages.sort(key=lambda item: item[0].id)
            return chat.id, messages, warnings

        message = await client.get_messages(chat_id, message_id)
        if not message or getattr(message, "empty", False):
            raise ValueError("没有找到这条消息，或当前账号无权访问")
        chat_id = message.chat.id
        with _preview_lock:
            _chat_aliases.setdefault(str(chat_id), set()).add(_chat_key(configured_alias))
        base_messages = [message]
        if getattr(message, "media_group_id", None):
            base_messages = await client.get_media_group(chat_id, message.id)
        origin_ref = str(configured_alias)
        messages = [(item, False, item.chat.id, None, origin_ref) for item in base_messages]
        warnings = []
        if include_comments:
            # Fetch comments once per post, preserving the post when discussion lookup fails.
            comment_messages = [(message, False, chat_id, None, origin_ref)]
            await _append_comments(client, comment_messages, warnings)
            messages.extend(comment_messages[1:])
        return chat_id, messages, warnings

    return _run_on_client_loop(fetch())


def _persist_chat_settings(app, chats):
    with _settings_lock:
        settings = app.web_state.get_settings()
        entries = _normalize_chats(chats, settings.get("chats", []), app.chat_download_config)
        # Validate every row and commit durable state before changing live objects.
        settings["chats"] = entries
        app.web_state.set_settings(settings)
        updated_configs = {}
        for item in entries:
            chat_id = item["chat_id"]
            config = app.chat_download_config.get(chat_id) or ChatDownloadConfig()
            config.download_filter = item["download_filter"]
            config.last_read_message_id = item["last_read_message_id"]
            config.end_message_id = item["end_message_id"]
            config.preview_limit = item["preview_limit"]
            updated_configs[chat_id] = config
        app.config["chat"] = entries
        app.chat_download_config.clear()
        app.chat_download_config.update(updated_configs)


def init_web(app: Application):
    """
    Set the value of the users variable.

    Args:
        users: The list of users to set.

    Returns:
        None.
    """
    global web_login_users
    if app.web_login_secret:
        web_login_users = {"root": app.web_login_secret}
    else:
        _flask_app.config["LOGIN_DISABLED"] = True
    if app.debug_web:
        threading.Thread(target=run_web_server, args=(app,)).start()
    else:
        threading.Thread(
            target=get_flask_app().run, daemon=True, args=(app.web_host, app.web_port)
        ).start()


@_flask_app.route("/login", methods=["GET", "POST"])
def login():
    """
    Function to handle the login route.

    Parameters:
    - No parameters

    Returns:
    - If the request method is "POST" and the username and
      password match the ones in the web_login_users dictionary,
      it returns a JSON response with a code of "1".
    - Otherwise, it returns a JSON response with a code of "0".
    - If the request method is not "POST", it returns the rendered "login.html" template.
    """
    if request.method == "POST":
        username = "root"
        web_login_form = {}
        for key, value in request.form.items():
            if value:
                value = deAesCrypt.decrypt(value)
            web_login_form[key] = value

        if not web_login_form.get("password"):
            return jsonify({"code": "0"})

        password = web_login_form["password"]
        if username in web_login_users and web_login_users[username] == password:
            user = User()
            login_user(user)
            return jsonify({"code": "1"})

        return jsonify({"code": "0"})

    return render_template("login.html")


@_flask_app.route("/")
@login_required
def index():
    """Render the Web UI task manager as the primary application view."""
    return render_template("task_manager.html")


@_flask_app.route("/status")
@login_required
def download_status_page():
    """Keep bookmarks working while using the unified management view."""
    return redirect("/#downloads")


@_flask_app.route("/tasks")
@login_required
def task_manager():
    """Compatibility route for the manual download task manager."""
    return render_template("task_manager.html")


@_flask_app.route("/api/task/chats", methods=["GET", "PUT"])
@login_required
@_serialize_lifecycle
def task_chats():
    """Read or persist chat IDs and per-chat filters."""
    app = _web_runtime.get("app")
    if app is None:
        return jsonify({"error": "Downloader is not ready"}), 503
    if request.method == "GET":
        chats = app.web_state.get_settings().get("chats", []) or []
        return jsonify(
            [
                {
                    "chat_id": str(item.get("chat_id", "")),
                    "download_filter": item.get("download_filter", ""),
                    "last_read_message_id": getattr(app.chat_download_config.get(_chat_key(item["chat_id"])), "last_read_message_id", item.get("last_read_message_id", 0)),
                    "end_message_id": item.get("end_message_id", 0),
                    "preview_limit": item.get("preview_limit", 100),
                }
                for item in chats
            ]
        )
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    chats = payload.get("chats")
    if not isinstance(chats, list):
        return jsonify({"error": "chats must be a list"}), 400
    try:
        _persist_chat_settings(app, chats)
    except Exception as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"saved": True})


@_flask_app.route("/api/runtime", methods=["GET", "PUT"])
@login_required
@_serialize_lifecycle
def runtime_settings():
    app = _web_runtime.get("app")
    if app is None:
        return jsonify({"error": "Runtime is unavailable"}), 503
    keys = ("api_id", "api_hash", "bot_token", "save_path", "media_types", "file_formats", "max_download_task", "proxy", "web_login_secret")
    if request.method == "GET":
        settings = app.web_state.get_settings()
        public = {key: settings.get(key) for key in keys if key not in ("api_hash", "bot_token", "web_login_secret", "proxy")}
        public["has_api_hash"] = bool(settings.get("api_hash"))
        public["has_bot_token"] = bool(settings.get("bot_token"))
        public["has_web_login_secret"] = bool(settings.get("web_login_secret"))
        public["running"] = bool(_web_runtime["running"])
        public["connected"] = _web_runtime.get("client") is not None
        public["needs_phone"] = bool(_web_runtime.get("client") and not _web_runtime.get("authorized"))
        public["worker_count"] = settings.get("max_download_task", 3)
        public["proxy_url"] = settings.get("proxy_url", "")
        return jsonify(public)
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    try:
        api_id = int(payload.get("api_id"))
        settings = app.web_state.get_settings()
        api_hash = str(payload.get("api_hash", "")).strip() or settings.get("api_hash", "")
        save_path = os.path.abspath(os.path.expanduser(str(payload.get("save_path", "downloads")).strip()))
        media_types = payload.get("media_types", [])
        if not api_hash or not isinstance(media_types, list):
            raise ValueError("API Hash 和媒体类型必填")
        formats = payload.get("file_formats", {})
        if not isinstance(formats, dict):
            raise ValueError("媒体格式设置无效")
        settings["api_id"] = api_id
        settings["api_hash"] = api_hash
        settings["bot_token"] = str(payload.get("bot_token", "")).strip() or settings.get("bot_token", "")
        settings["save_path"] = save_path
        settings["media_types"] = media_types
        settings["file_formats"] = formats
        settings["max_download_task"] = max(1, min(16, int(payload.get("max_download_task", 3))))
        settings["proxy_url"] = str(payload.get("proxy_url", "")).strip()
        settings["proxy"] = payload.get("proxy") or {}
        parsed_proxy = _parse_proxy(settings)
        if payload.get("clear_web_login_secret"):
            settings["web_login_secret"] = ""
        else:
            settings["web_login_secret"] = str(payload.get("web_login_secret", "")).strip() or settings.get("web_login_secret", "")
        settings["chats"] = settings.get("chats", [])
        app.web_state.set_settings(settings)
        app.web_login_secret = settings["web_login_secret"]
        if app.web_login_secret:
            web_login_users.clear()
            web_login_users["root"] = app.web_login_secret
            _flask_app.config["LOGIN_DISABLED"] = False
        else:
            web_login_users.clear()
            _flask_app.config["LOGIN_DISABLED"] = True
        app.api_id = api_id; app.api_hash = api_hash; app.bot_token = settings["bot_token"]
        app.save_path = save_path; app.media_types = media_types; app.file_formats = formats
        app.max_download_task = settings["max_download_task"]
        app.max_concurrent_transmissions = app.max_download_task * 5
        app.proxy = parsed_proxy
        return jsonify({"saved": True})
    except Exception as error:
        return jsonify({"error": str(error)}), 400


@_flask_app.route("/api/runtime/start", methods=["POST"])
@login_required
@_serialize_lifecycle
def start_runtime():
    app = _web_runtime.get("app")
    if app is None or not app.api_id or not app.api_hash:
        return jsonify({"error": "请先填写并保存 API ID 和 API Hash"}), 400
    if _web_runtime["running"]:
        return jsonify({"running": True, "authorized": True})
    if _web_runtime.get("client") is not None and not _web_runtime.get("authorized"):
        return jsonify({"running": False, "needs_phone": True})
    try:
        client = _web_runtime.get("client")
        if client is None:
            client, authorized = _run_on_client_loop(_web_runtime["start_client"]())
            _web_runtime["client"] = client
            _web_runtime["authorized"] = bool(authorized)
        else:
            authorized = _web_runtime.get("authorized", False)
        if authorized:
            _run_on_client_loop(_web_runtime["activate_client"](client))
            _web_runtime["authorized"] = True
            _web_runtime["running"] = True
            return jsonify({"running": True, "authorized": True})
        _web_runtime["authorized"] = False
        return jsonify({"running": False, "needs_phone": True})
    except Exception as error:
        return jsonify({"error": str(error)}), 400


@_flask_app.route("/api/runtime/send-code", methods=["POST"])
@login_required
@_serialize_lifecycle
def send_telegram_code():
    client = _web_runtime.get("client")
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    phone = str(payload.get("phone", "")).strip()
    if client is None or not phone:
        return jsonify({"error": "请填写手机号并先启动连接"}), 400
    try:
        result = _run_on_client_loop(client.send_code(phone))
        _web_runtime["phone"] = phone
        _web_runtime["phone_code_hash"] = result.phone_code_hash
        return jsonify({"sent": True})
    except Exception as error:
        return jsonify({"error": str(error)}), 400


@_flask_app.route("/api/runtime/sign-in", methods=["POST"])
@login_required
@_serialize_lifecycle
def sign_in_telegram():
    client = _web_runtime.get("client")
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    code = str(payload.get("code", "")).strip()
    password = str(payload.get("password", "")).strip()
    if client is None:
        return jsonify({"error": "请先启动 Telegram 连接"}), 400
    try:
        from pyrogram.errors import SessionPasswordNeeded
        if password:
            user = _run_on_client_loop(client.check_password(password))
        else:
            user = _run_on_client_loop(client.sign_in(
                _web_runtime["phone"], _web_runtime["phone_code_hash"], code
            ))
        client.me = user
        _run_on_client_loop(_web_runtime["activate_client"](client))
        _web_runtime["authorized"] = True
        _web_runtime["running"] = True
        return jsonify({"running": True, "username": user.username or user.first_name})
    except SessionPasswordNeeded:
        return jsonify({"needs_password": True})
    except Exception as error:
        if error.__class__.__name__ == "SessionPasswordNeeded":
            return jsonify({"needs_password": True})
        return jsonify({"error": str(error)}), 400


@_flask_app.route("/api/runtime/stop", methods=["POST"])
@login_required
@_serialize_lifecycle
def stop_runtime():
    if _web_runtime.get("client") is None:
        return jsonify({"running": False})
    try:
        _run_on_client_loop(_web_runtime["stop_client"]())
        _web_runtime["client"] = None
        _web_runtime["running"] = False
        _web_runtime["authorized"] = False
        _web_runtime.pop("phone", None)
        _web_runtime.pop("phone_code_hash", None)
        with _preview_lock:
            _preview_cache.clear()
            _chat_aliases.clear()
        return jsonify({"running": False})
    except Exception as error:
        return jsonify({"error": str(error)}), 400


@_flask_app.route("/api/downloads")
@login_required
def unified_downloads():
    app = _web_runtime.get("app")
    if app is None:
        return jsonify([])
    records = app.web_state.downloads()
    progress = get_download_result()
    paused = get_download_state() is DownloadState.StopDownload
    for record in records:
        record.update(down_byte=0, total_size=0, download_speed=0, download_progress=0, global_paused=paused)
        stats = progress.get(int(record["chat_id"]) if record["chat_id"].lstrip("-").isdigit()
                             else record["chat_id"], {}).get(record["message_id"])
        if stats and record["status"] != "Queued":
            record.update(
                down_byte=stats["down_byte"], total_size=stats["total_size"],
                download_speed=stats["download_speed"] if record["status"] == "Downloading" else 0,
                download_progress=round(stats["down_byte"] / stats["total_size"] * 100, 1)
                if stats["total_size"] else 0,
            )
            record["file_path"] = record["file_path"] or stats["file_name"]
        elif record["status"] == "SuccessDownload":
            record["download_progress"] = 100
            try:
                size = os.path.getsize(record["file_path"])
                record.update(down_byte=size, total_size=size)
            except OSError:
                pass
    return jsonify(records)


@_flask_app.route("/api/downloads/<action>", methods=["POST", "DELETE"])
@login_required
def manage_download(action):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    app = _web_runtime.get("app")
    client = _web_runtime.get("client")
    if app is None:
        return jsonify({"error": "Runtime is unavailable"}), 503
    try:
        action = request.view_args["action"]
        if action == "pause":
            set_download_state(DownloadState.StopDownload)
            return jsonify({"saved": True, "paused": True, "scope": "global"})
        if action == "resume":
            set_download_state(DownloadState.Downloading)
            return jsonify({"saved": True, "paused": False, "scope": "global"})
        chat_id = _chat_key(payload.get("chat_id"))
        message_id = _nonnegative_integer(payload.get("message_id"), "message_id")
        row = app.web_state.task(chat_id, message_id)
        if not row:
            return jsonify({"error": "下载任务不存在"}), 404
        if action == "retry":
            if row["status"] not in ("FailedDownload", "Cancelled", "SkipDownload"):
                return jsonify({"error": "只有失败、取消或跳过的任务可以重试"}), 409
            if app.web_state.is_blocked(chat_id, message_id):
                return jsonify({"error": "该消息在黑名单中，请先解除屏蔽"}), 409
            if client is None or not _web_runtime.get("running"):
                return jsonify({"error": "请先启动 Telegram"}), 503
            origin_ref = row.get("origin_chat_ref") or ""
            download_chat_id = origin_ref or row.get("origin_chat_id") or chat_id
            origin_message_id = row.get("origin_message_id") or message_id

            async def retry_download():
                origin = None
                try:
                    resolved_chat = await client.get_chat(
                        origin_ref if row.get("origin_message_id") and origin_ref else chat_id
                    )
                    resolved_chat_id = resolved_chat.id
                except Exception as error:
                    if row.get("origin_message_id") and not origin_ref:
                        raise ValueError(
                            "此旧评论任务未保存原帖链接，Telegram 也无法解析讨论组 ID。请重新预览原帖评论后再提交下载。"
                        ) from error
                    raise ValueError("Telegram 无法解析此 Chat，请确认账号仍有访问权限") from error
                if row.get("origin_message_id"):
                    source_chat = resolved_chat
                    origin = await client.get_messages(source_chat.id, origin_message_id)
                    if not origin or getattr(origin, "empty", False):
                        raise ValueError("无法重新获取原帖，不能恢复评论文件关联")
                    discussion = await client.get_discussion_message(source_chat.id, origin_message_id)
                    if not discussion:
                        raise ValueError("无法打开原帖评论区，请确认账号仍有访问权限")
                    fetched = None
                    async for reply in client.get_discussion_replies(
                        discussion.chat.id, discussion.id, limit=1000
                    ):
                        if reply.id == message_id and str(reply.chat.id) == str(chat_id):
                            fetched = reply
                            break
                    if not fetched:
                        raise ValueError("评论区中已找不到这条消息，或账号无权访问")
                else:
                    fetched = await client.get_messages(resolved_chat_id, message_id)
                if not fetched or getattr(fetched, "empty", False):
                    raise ValueError("无法重新获取消息")
                media_group = [fetched]
                if getattr(fetched, "media_group_id", None):
                    media_group = await client.get_media_group(chat_id, fetched.id)
                node = TaskNode(chat_id=chat_id)
                node.comment_source = origin
                node.save_text_messages = _media_type(fetched) is None and bool(getattr(fetched, "text", None))
                node.selected_media_group = media_group
                if not await _web_runtime["add_download_task"](fetched, node):
                    raise ValueError("任务未能加入队列")
                return True
            try:
                _run_on_client_loop(retry_download())
            except Exception as error:
                return jsonify({"error": str(error)}), 400
            return jsonify({"queued": True})
        if action == "remove":
            if row["status"] in ("Queued", "Downloading"):
                cancelled = False
                with active_download_lock:
                    task = active_download_tasks.get((str(chat_id), message_id))
                    if task is not None:
                        node, download_future = task
                        node.web_remove_requested = True
                        loop = _web_runtime.get("loop")
                        if loop and loop.is_running():
                            future = asyncio.run_coroutine_threadsafe(
                                _cancel_active_download(download_future), loop
                            )
                            future.result(timeout=10)
                            cancelled = True
                    else:
                        loop = _web_runtime.get("loop")
                        if loop and loop.is_running():
                            future = asyncio.run_coroutine_threadsafe(_remove_queued(chat_id, message_id), loop)
                            cancelled = future.result(timeout=5)
                if not cancelled:
                    return jsonify({"error": "当前任务状态正在变化，请刷新后重试"}), 409
            app.web_state.remove_record(chat_id, message_id)
            return jsonify({"removed": True})
        return jsonify({"error": "不支持的任务操作"}), 404
    except Exception as error:
        return jsonify({"error": str(error)}), 400


async def _cancel_active_download(download_future):
    download_future.cancel()
    try:
        await download_future
    except asyncio.CancelledError:
        pass
    return True


async def _remove_queued(chat_id, message_id):
    from media_downloader import queue

    retained = []
    removed = False
    while True:
        try:
            item = queue.get_nowait()
        except asyncio.QueueEmpty:
            break
        message, node = item
        queue.task_done()
        if str(node.chat_id) == str(chat_id) and message.id == message_id:
            removed = True
        else:
            retained.append(item)
    for item in retained:
        queue.put_nowait(item)
    return removed


active_download_tasks = {}
active_download_lock = threading.RLock()


@_flask_app.route("/api/history")
@login_required
def task_history():
    app = _web_runtime.get("app")
    return jsonify(app.web_state.history() if app else [])


@_flask_app.route("/api/blacklist", methods=["GET", "POST", "DELETE"])
@login_required
def resource_blacklist():
    app = _web_runtime.get("app")
    if app is None:
        return jsonify({"error": "Runtime is unavailable"}), 503
    if request.method == "GET":
        return jsonify(app.web_state.blacklist())
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    chat_id, message_id = payload.get("chat_id"), payload.get("message_id")
    if chat_id is None or message_id is None:
        return jsonify({"error": "chat_id and message_id are required"}), 400
    if request.method == "POST":
        app.web_state.block(chat_id, message_id, str(payload.get("title", "")))
    else:
        app.web_state.unblock(chat_id, message_id)
    return jsonify({"saved": True})


@_flask_app.route("/api/task/preview", methods=["POST"])
@login_required
def preview_task_links():
    """Resolve input links into selectable message previews."""
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    links = [line.strip() for line in str(payload.get("links", "")).splitlines() if line.strip()]
    if not links:
        return jsonify({"error": "请先输入至少一条 Telegram 链接"}), 400
    if len(links) > 50:
        return jsonify({"error": "单次最多预览 50 条链接"}), 400
    client = _web_runtime.get("client")
    if client is None or not _web_runtime.get("authorized"):
        return jsonify({"error": "请先在设置中完成 Telegram 登录"}), 503

    include_comments = bool(payload.get("include_comments"))
    selected_categories = payload.get("categories")
    if selected_categories is not None and not isinstance(selected_categories, list):
        return jsonify({"error": "categories must be a list"}), 400
    app = _web_runtime.get("app")
    if app is None:
        return jsonify({"error": "Downloader is not ready"}), 503
    results = []
    for link in links:
        try:
            chat_id, messages, warnings = _preview_link_messages(client, link, include_comments)
            result = _store_preview(app, chat_id, messages, selected_categories)
            result.update(link=link, warnings=warnings)
            results.append(result)
        except Exception as error:
            results.append({"link": link, "error": str(error), "messages": []})
    return jsonify({"results": results})


@_flask_app.route("/api/task/preview-chats", methods=["POST"])
@login_required
def preview_task_chats():
    """Preview a bounded newest history window using saved Chat settings."""
    app, client = _web_runtime.get("app"), _web_runtime.get("client")
    if app is None or client is None or not _web_runtime.get("authorized"):
        return jsonify({"error": "请先在设置中完成 Telegram 登录"}), 503
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    chat_ids = payload.get("chat_ids")
    categories = payload.get("categories")
    if not isinstance(chat_ids, list) or not chat_ids or len(chat_ids) > 50:
        return jsonify({"error": "chat_ids must contain 1 to 50 configured Chat IDs"}), 400
    if categories is not None and not isinstance(categories, list):
        return jsonify({"error": "categories must be a list"}), 400
    try:
        keys = [_chat_key(value) for value in chat_ids]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate Chat IDs")
        settings = {_chat_key(item["chat_id"]): item for item in app.web_state.get_settings().get("chats", [])}
        if any(key not in settings for key in keys):
            raise ValueError("All chat_ids must refer to saved Chats")
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    include_comments = bool(payload.get("include_comments"))
    results = []
    for key in keys:
        try:
            saved = settings[key]
            config = app.chat_download_config.get(key)
            start = getattr(config, "last_read_message_id", saved.get("last_read_message_id", 0))
            end, limit = saved.get("end_message_id", 0), saved.get("preview_limit", 100)

            async def fetch_history():
                chat = await client.get_chat(key)
                with _preview_lock:
                    _chat_aliases.setdefault(str(chat.id), set()).add(key)
                messages, warnings = [], []
                examined, reached_start = 0, False
                async for message in client.get_chat_history(chat.id, limit=limit, offset_id=end + 1 if end else 0):
                    examined += 1
                    if message.id <= start:
                        reached_start = True
                        break
                    if (not end or message.id <= end) and not getattr(message, "empty", False):
                        messages.append((message, False, message.chat.id, None, str(key)))
                    if examined >= limit:
                        break
                truncated = examined >= limit and not reached_start
                if truncated:
                    warnings.append(f"仅预览最新 {limit} 条候选消息，较早消息可能已截断")
                messages.sort(key=lambda item: item[0].id)
                candidate_count = len(messages)
                if include_comments:
                    await _append_comments(client, messages, warnings, limit=min(limit, 100))
                return chat.id, messages, warnings, truncated, candidate_count

            actual_chat_id, messages, warnings, truncated, candidate_count = _run_on_client_loop(fetch_history())
            result = _store_preview(app, actual_chat_id, messages, categories, key)
            result.update(configured_chat_id=str(key), last_read_message_id=start, end_message_id=end,
                          limit=limit, preview_limit=limit, truncated=truncated,
                          candidate_count=candidate_count, warnings=warnings)
            results.append(result)
        except Exception as error:
            results.append({"configured_chat_id": str(key), "error": str(error), "messages": []})
    return jsonify({"results": results})


@_flask_app.route("/api/task/submit", methods=["POST"])
@login_required
def submit_preview_task():
    """Queue only messages selected from a server-side preview."""
    if not _web_runtime.get("running"):
        return jsonify({"error": "请先启动 Telegram 再开始下载"}), 503
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    selected = payload.get("selected", [])
    if not isinstance(selected, list) or not selected:
        return jsonify({"error": "请至少选择一条消息"}), 400

    app = _web_runtime.get("app")
    add_download_task = _web_runtime.get("add_download_task")
    if app is None or add_download_task is None:
        return jsonify({"error": "Downloader is not ready"}), 503
    candidates, seen, duplicates = [], set(), 0
    try:
        with _preview_lock:
            for selection in selected:
                if not isinstance(selection, dict):
                    raise ValueError("Each selection must be an object")
                preview_id, indexes = selection.get("preview_id"), selection.get("indexes")
                if not isinstance(preview_id, str) or not isinstance(indexes, list) or not indexes:
                    raise ValueError("Each selection requires preview_id and non-empty indexes")
                preview = _preview_cache.get(preview_id)
                if not preview or (datetime.utcnow() - preview["created_at"]).total_seconds() > 3600:
                    raise ValueError("预览已失效，请重新预览后再提交")
                for index in indexes:
                    if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(preview["messages"]):
                        raise ValueError("Invalid preview message index")
                    message = preview["messages"][index]
                    chat_id = preview["message_chats"][index]
                    identity = (str(chat_id), message.id)
                    if identity in seen:
                        duplicates += 1
                        continue
                    seen.add(identity)
                    configured_key = preview.get("configured_chat_id")
                    progress_key = configured_key if str(chat_id) == str(preview["chat_id"]) else None
                    origin = preview.get("origins", [None] * len(preview["messages"]))[index]
                    origin_ref = preview.get("origin_refs", [""] * len(preview["messages"]))[index]
                    candidates.append((message, chat_id, configured_key, progress_key, origin, origin_ref))
        # Evaluate the entire selection before queueing anything.
        jobs, filtered, blocked = [], 0, 0
        for message, chat_id, configured_key, progress_key, origin, origin_ref in candidates:
            if app.web_state.is_blocked(chat_id, message.id):
                blocked += 1
                continue
            if not _message_allowed(app, message, chat_id, configured_key):
                filtered += 1
                continue
            node = TaskNode(chat_id=chat_id)
            node.web_chat_key = progress_key
            node.web_manual_selection = True
            node.comment_source = origin
            node.comment_source_ref = origin_ref
            node.save_text_messages = bool(getattr(message, "text", None)) and _media_type(message) is None
            jobs.append((message, node))
        # Keep album sidecars bounded to the user-selected, accepted messages.
        groups = {}
        for message, node in jobs:
            group_id = getattr(message, "media_group_id", None)
            if group_id:
                groups.setdefault((node.chat_id, group_id), []).append(message)
        for message, node in jobs:
            node.selected_media_group = groups.get((node.chat_id, getattr(message, "media_group_id", None)), [message])
    except Exception as error:
        return jsonify({"error": str(error)}), 400

    async def enqueue():
        queued, rejected, newly_blocked = 0, 0, 0
        for message, node in jobs:
            if app.web_state.is_blocked(node.chat_id, message.id):
                newly_blocked += 1
                continue
            if await add_download_task(message, node):
                queued += 1
            else:
                rejected += 1
        return queued, rejected, newly_blocked

    try:
        count, rejected, newly_blocked = _run_on_client_loop(enqueue())
    except Exception as error:
        return jsonify({"error": str(error)}), 503
    return jsonify({"queued": count, "filtered": filtered + rejected, "blocked": blocked + newly_blocked, "duplicates": duplicates})


@_flask_app.route("/get_download_status")
@login_required
def get_download_speed():
    """Get download speed"""
    return (
        '{ "download_speed" : "'
        + format_byte(get_total_download_speed())
        + '/s" , "upload_speed" : "0.00 B/s" } '
    )


@_flask_app.route("/set_download_state", methods=["POST"])
@login_required
def web_set_download_state():
    """Set download state"""
    state = request.args.get("state")

    if state == "continue" and get_download_state() is DownloadState.StopDownload:
        set_download_state(DownloadState.Downloading)
        return "pause"

    if state == "pause" and get_download_state() is DownloadState.Downloading:
        set_download_state(DownloadState.StopDownload)
        return "continue"

    return state


@_flask_app.route("/get_app_version")
def get_app_version():
    """Get telegram_media_downloader version"""
    return utils.__version__


@_flask_app.route("/get_download_list")
@login_required
def get_download_list():
    """Return status records with safely encoded filenames and paths."""
    if request.args.get("already_down") is None:
        return jsonify([])
    already_down = request.args.get("already_down") == "true"
    result = []
    for chat_id, messages in get_download_result().items():
        for message_id, value in messages.items():
            total = value["total_size"]
            downloaded = value["down_byte"]
            if already_down and downloaded != total:
                continue
            progress = round(downloaded / total * 100, 1) if total else 0
            result.append({
                "chat": str(chat_id), "id": str(message_id),
                "filename": os.path.basename(value["file_name"]),
                "total_size": format_byte(total), "download_progress": str(progress),
                "download_speed": format_byte(value["download_speed"]) + "/s",
                "save_path": value["file_name"].replace("\\", "/"),
            })
    return jsonify(result)
