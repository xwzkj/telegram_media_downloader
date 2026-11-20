"""Persistent state owned by the web interface, independent from legacy YAML."""

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone


class WebState:
    def __init__(self, path="web_state.sqlite3"):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.lock = threading.RLock()
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS history (
                    chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
                    title TEXT NOT NULL, status TEXT NOT NULL, file_path TEXT NOT NULL,
                    updated_at TEXT NOT NULL, origin_chat_id TEXT NOT NULL DEFAULT '',
                    origin_message_id INTEGER, origin_chat_ref TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY(chat_id, message_id)
                )"""
            )
            columns = {row[1] for row in db.execute("PRAGMA table_info(history)")}
            if "origin_chat_id" not in columns:
                db.execute("ALTER TABLE history ADD COLUMN origin_chat_id TEXT NOT NULL DEFAULT ''")
            if "origin_message_id" not in columns:
                db.execute("ALTER TABLE history ADD COLUMN origin_message_id INTEGER")
            if "origin_chat_ref" not in columns:
                db.execute("ALTER TABLE history ADD COLUMN origin_chat_ref TEXT NOT NULL DEFAULT ''")
            db.execute(
                """CREATE TABLE IF NOT EXISTS blacklist (
                    chat_id TEXT NOT NULL, message_id INTEGER NOT NULL,
                    title TEXT NOT NULL, created_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, message_id)
                )"""
            )

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def get_settings(self):
        with self.lock, self._connect() as db:
            row = db.execute("SELECT value FROM settings WHERE key='runtime'").fetchone()
        return json.loads(row[0]) if row else {}

    def set_settings(self, value):
        encoded = json.dumps(value, ensure_ascii=False)
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT INTO settings(key,value) VALUES('runtime',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (encoded,),
            )

    def is_blocked(self, chat_id, message_id):
        with self.lock, self._connect() as db:
            return db.execute(
                "SELECT 1 FROM blacklist WHERE chat_id=? AND message_id=?",
                (str(chat_id), int(message_id)),
            ).fetchone() is not None

    def block(self, chat_id, message_id, title=""):
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO blacklist VALUES(?,?,?,?)",
                (str(chat_id), int(message_id), title, datetime.now(timezone.utc).isoformat()),
            )

    def unblock(self, chat_id, message_id):
        with self.lock, self._connect() as db:
            db.execute(
                "DELETE FROM blacklist WHERE chat_id=? AND message_id=?",
                (str(chat_id), int(message_id)),
            )

    def blacklist(self):
        with self.lock, self._connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM blacklist ORDER BY created_at DESC")]

    def record(self, chat_id, message_id, title, status, file_path="", origin_chat_id="", origin_message_id=None, origin_chat_ref=""):
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT INTO history(chat_id,message_id,title,status,file_path,updated_at,origin_chat_id,origin_message_id,origin_chat_ref) "
                "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(chat_id,message_id) DO UPDATE SET "
                "title=excluded.title,status=excluded.status,file_path=excluded.file_path,updated_at=excluded.updated_at, "
                "origin_chat_id=CASE WHEN excluded.origin_chat_id!='' THEN excluded.origin_chat_id ELSE history.origin_chat_id END, "
                "origin_message_id=COALESCE(excluded.origin_message_id,history.origin_message_id), "
                "origin_chat_ref=CASE WHEN excluded.origin_chat_ref!='' THEN excluded.origin_chat_ref ELSE history.origin_chat_ref END",
                (str(chat_id), int(message_id), title, status, file_path or "",
                 datetime.now(timezone.utc).isoformat(), str(origin_chat_id or ""), origin_message_id, str(origin_chat_ref or "")),
            )

    def history(self, limit=500):
        with self.lock, self._connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM history ORDER BY updated_at DESC LIMIT ?", (int(limit),)
            )]

    def downloads(self, limit=500):
        """Keep all active tasks visible alongside a bounded completed history."""
        with self.lock, self._connect() as db:
            active = [dict(row) for row in db.execute(
                "SELECT * FROM history WHERE status IN ('Queued','Downloading') "
                "ORDER BY updated_at DESC"
            )]
            finished = [dict(row) for row in db.execute(
                "SELECT * FROM history WHERE status NOT IN ('Queued','Downloading') "
                "ORDER BY updated_at DESC LIMIT ?", (int(limit),)
            )]
        return active + finished

    def remove_record(self, chat_id, message_id):
        with self.lock, self._connect() as db:
            db.execute("DELETE FROM history WHERE chat_id=? AND message_id=?", (str(chat_id), int(message_id)))

    def task(self, chat_id, message_id):
        with self.lock, self._connect() as db:
            row = db.execute(
                "SELECT * FROM history WHERE chat_id=? AND message_id=?",
                (str(chat_id), int(message_id)),
            ).fetchone()
        return dict(row) if row else None

    def interrupt_pending(self):
        with self.lock, self._connect() as db:
            db.execute(
                "UPDATE history SET status='Cancelled', updated_at=? "
                "WHERE status IN ('Queued','Downloading')",
                (datetime.now(timezone.utc).isoformat(),),
            )

    def blocked_ids(self):
        with self.lock, self._connect() as db:
            return {
                (str(row[0]), int(row[1]))
                for row in db.execute("SELECT chat_id, message_id FROM blacklist")
            }

    def upsert_history(self, chat_id, message_id, title, status, file_path=""):
        with self.lock, self._connect() as db:
            db.execute(
                "INSERT INTO history(chat_id,message_id,title,status,file_path,updated_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(chat_id,message_id) DO UPDATE SET "
                "title=excluded.title,status=excluded.status,file_path=excluded.file_path,updated_at=excluded.updated_at",
                (str(chat_id), int(message_id), title, status, file_path or "", datetime.now(timezone.utc).isoformat()),
            )
