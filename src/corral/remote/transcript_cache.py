"""手机远程富消息的本机持久缓存。

原始历史文件仍是权威来源。这里只保存已经规范化的 ``user`` / ``assistant`` 消息、
读取游标和 generation，让打开会话不必把整份 JSONL 再解析一遍。

失败一律视为未命中，不得阻断真实读取。磁盘写入遵循 ``CORRAL_CACHE``；
进程内缓存由 ``SessionHub`` 自行持有，不受该开关影响。
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from corral.cache import cache_dir, enabled, history_signature
from corral.remote.richmsg import RichMessage

PARSER_VERSION = "2026-10-01.1"  # async panels survive mid-turn commentary/tools; turn-scope native errors settle them
_SCHEMA_VERSION = 1


def cache_path() -> Path:
    return cache_dir() / "remote-transcripts.sqlite3"


class TranscriptCache:
    """按历史文件签名保存规范化消息；签名变化即失效。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or cache_path()
        self._local = threading.local()

    @contextmanager
    def _connect(self, *, create: bool = True):
        if not enabled():
            yield None
            return
        conn = getattr(self._local, "connection", None)
        if conn is not None and create:
            try:
                yield conn
            except sqlite3.Error:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            return
        try:
            if create:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(self.path.parent, 0o700)
            elif not self.path.exists():
                yield None
                return
            if conn is None:
                conn = sqlite3.connect(self.path, timeout=0.08)
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
                conn.execute("PRAGMA busy_timeout=80")
                self._local.connection = conn
                if create:
                    self._init_schema(conn)
                    try:
                        os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
                    except OSError:
                        pass
        except (OSError, sqlite3.Error):
            yield None
            return
        try:
            yield conn
        except sqlite3.Error:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS transcript (
                runtime TEXT NOT NULL,
                session_key TEXT NOT NULL,
                path TEXT NOT NULL,
                dev INTEGER NOT NULL,
                ino INTEGER NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                parser_version TEXT NOT NULL,
                generation INTEGER NOT NULL,
                reader_state TEXT NOT NULL,
                payload TEXT NOT NULL,
                accessed_at REAL NOT NULL,
                PRIMARY KEY(runtime, session_key)
            );
            CREATE INDEX IF NOT EXISTS transcript_lru ON transcript(accessed_at);
            """
        )
        conn.commit()

    def get(
        self,
        runtime: str,
        session_key: str,
        path: str,
    ) -> tuple[list[RichMessage], dict, int] | None:
        signature = history_signature(path)
        if signature is None:
            return None
        with self._connect() as conn:
            if conn is None:
                return None
            row = conn.execute(
                "SELECT dev, ino, size, mtime_ns, parser_version, generation, "
                "reader_state, payload FROM transcript "
                "WHERE runtime=? AND session_key=?",
                (runtime, session_key),
            ).fetchone()
            if (
                row is None
                or tuple(row[:4]) != signature
                or row[4] != PARSER_VERSION
            ):
                return None
            try:
                state = json.loads(row[6])
                raw = json.loads(row[7])
                messages = [
                    RichMessage.from_dict(item)
                    for item in raw
                    if isinstance(item, dict)
                ]
                generation = int(row[5] or 1)
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            if not isinstance(state, dict):
                return None
            return messages, state, generation

    def put(
        self,
        runtime: str,
        session_key: str,
        path: str,
        messages: list[RichMessage],
        reader_state: dict,
        generation: int,
    ) -> None:
        signature = history_signature(path)
        if signature is None:
            return
        payload = json.dumps(
            [item.to_dict() for item in messages],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        state = json.dumps(reader_state, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            if conn is None:
                return
            conn.execute(
                "INSERT OR REPLACE INTO transcript("
                "runtime, session_key, path, dev, ino, size, mtime_ns, "
                "parser_version, generation, reader_state, payload, accessed_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    runtime,
                    session_key,
                    path,
                    signature[0],
                    signature[1],
                    signature[2],
                    signature[3],
                    PARSER_VERSION,
                    int(generation or 1),
                    state,
                    payload,
                    time.time(),
                ),
            )
            conn.commit()

    def close(self) -> None:
        conn = getattr(self._local, "connection", None)
        if conn is None:
            return
        try:
            conn.close()
        except sqlite3.Error:
            pass
        self._local.connection = None


# 进程内默认实例由 SessionHub 自行持有，避免测试夹具之间串目录。