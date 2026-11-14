"""手机远程富消息的本机持久缓存。

原始历史文件仍是权威来源。这里只保存已经规范化的 ``user`` / ``assistant`` 消息、
读取游标和 generation，让打开会话不必把整份 JSONL 再解析一遍。

失败一律视为未命中，不得阻断真实读取。磁盘写入遵循 ``CORRAL_CACHE``；
进程内缓存由 ``SessionHub`` 自行持有，不受该开关影响。

缓存签名是「读之前」看到的文件状态，不是写完之后的补拍：一条「签名相同」的记录
因此保证至少覆盖它落盘时文件里的全部内容，冷启动可以直接用，不必再补一轮
``poll()``。文件在那之后又长了，签名就对不上，调用方恢复游标后增量补读。
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.request import pathname2url

from corral.cache import cache_dir, enabled, file_signature, history_signature
from corral.remote.richmsg import RichMessage

PARSER_VERSION = "2026-11-14.1"  # antigravity rich transcript via SessKit open_reader
_SCHEMA_VERSION = 2  # 2: session_prompts projection beside the transcript row


def cache_path() -> Path:
    return cache_dir() / "remote-transcripts.sqlite3"


@dataclass(frozen=True)
class CachedTranscript:
    """A persisted transcript and how well it still matches the history file.

    ``exact`` means the file has not changed since ``messages``/``state`` were
    written, so a reader restored from ``state`` is already current. Otherwise
    the same inode is still there but the file moved on; restore the cursor and
    ``poll()`` to catch up instead of re-parsing the whole history.
    """

    messages: list[RichMessage]
    state: dict
    generation: int
    exact: bool


def _same_content(
    stored: tuple[int, int, int, int],
    current: tuple[int, int, int, int],
) -> bool:
    """Same file version? Ignores ``st_dev`` on purpose.

    macOS reports a different device number for the very same inode across
    boots and data-volume snapshots (measured on this host: 31 cache rows
    differed only in ``dev``, with identical inode, size and ``mtime_ns``).
    Comparing the device makes every cold open of those sessions a full
    re-parse. Inode + size + ``mtime_ns`` at the same path identify one file
    version; a different file reusing the inode *and* both values is not a
    realistic collision.
    """

    return stored[1:] == current[1:]


def _rollback(conn: sqlite3.Connection) -> None:
    """Best-effort rollback so a failed write keeps no txn or lock behind."""
    try:
        conn.rollback()
    except sqlite3.Error:
        pass


class TranscriptCache:
    """按历史文件签名保存规范化消息；签名变化即失效。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or cache_path()
        self._local = threading.local()
        # One writer at a time in this process: the per-session locks let two
        # sessions persist concurrently, and SQLite's 80 ms busy timeout would
        # otherwise drop one of the two big payload writes (a silent cache miss
        # on the next open). Encoding stays outside this lock.
        self._write_lock = threading.Lock()

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
                _rollback(conn)
            return
        opened_here = False
        try:
            if create:
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(self.path.parent, 0o700)
            elif not self.path.exists():
                # Fast path only: the mode=rw open below stays race-safe if
                # the file disappears after this check (open raises, no create).
                yield None
                return
            if conn is None:
                if create:
                    conn = sqlite3.connect(self.path, timeout=0.08)
                else:
                    # Reads must not create the main DB file; mode=rw opens
                    # an existing file and raises instead of creating a
                    # missing one. WAL sidecars on an existing DB remain
                    # SQLite's own native behavior, not cache creation.
                    conn = sqlite3.connect(
                        "file:" + pathname2url(str(self.path)) + "?mode=rw",
                        timeout=0.08,
                        uri=True,
                    )
                # Own the new handle from the instant connect returns, before
                # any PRAGMA/schema work that can fail: the handler below must
                # close it even though it is not cached in thread-local yet.
                opened_here = True
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
            if opened_here:
                _rollback(conn)
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
                if getattr(self._local, "connection", None) is conn:
                    self._local.connection = None
            yield None
            return
        try:
            yield conn
        except sqlite3.Error:
            _rollback(conn)

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
            CREATE TABLE IF NOT EXISTS session_prompts (
                runtime TEXT NOT NULL,
                session_key TEXT NOT NULL,
                path TEXT NOT NULL,
                ino INTEGER NOT NULL,
                size INTEGER NOT NULL,
                generation INTEGER NOT NULL,
                upto_seq INTEGER NOT NULL,
                complete INTEGER NOT NULL,
                payload TEXT NOT NULL,
                accessed_at REAL NOT NULL,
                PRIMARY KEY(runtime, session_key)
            );
            """
        )
        conn.commit()

    @staticmethod
    def _write_row(conn: sqlite3.Connection, statement: str, params: tuple) -> bool:
        """Single INSERT-or-replace with narrow schema repair.

        Only a confirmed missing table (``OperationalError: no such table``)
        runs schema creation and one retry. Lock/busy, readonly, I/O and
        corrupt-DB failures roll back and report ``False`` instead. Every
        failed path rolls back first, so the cached handle never retains an
        open write transaction or its lock. A ``False`` write is a silent
        cache miss per the module contract, never a caller-visible error.
        """
        try:
            conn.execute(statement, params)
            conn.commit()
            return True
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc):
                _rollback(conn)
                return False
        except sqlite3.Error:
            _rollback(conn)
            return False
        _rollback(conn)
        try:
            TranscriptCache._init_schema(conn)
        except (OSError, sqlite3.Error):
            _rollback(conn)
            return False
        try:
            conn.execute(statement, params)
            conn.commit()
            return True
        except sqlite3.Error:
            _rollback(conn)
            return False

    def get(
        self,
        runtime: str,
        session_key: str,
        path: str,
    ) -> CachedTranscript | None:
        signature = history_signature(path)
        if signature is None:
            return None
        # Read path: no mkdir/chmod/schema, and mode=rw cannot create the
        # main DB file. WAL sidecars on an existing DB are SQLite's own
        # native behavior, not cache creation.
        with self._connect(create=False) as conn:
            if conn is None:
                return None
            try:
                row = conn.execute(
                    "SELECT dev, ino, size, mtime_ns, parser_version, generation, "
                    "reader_state, payload FROM transcript "
                    "WHERE runtime=? AND session_key=?",
                    (runtime, session_key),
                ).fetchone()
            except sqlite3.Error:
                return None
            if row is None or row[4] != PARSER_VERSION:
                return None
            if int(row[1]) != signature[1]:
                # Another file took over this path (new session, same path).
                # Its reader cursor means nothing here: re-parse.
                return None
            stored = (int(row[0]), int(row[1]), int(row[2]), int(row[3]))
            exact = _same_content(stored, signature)
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
            return CachedTranscript(messages, state, generation, exact)

    def put(
        self,
        runtime: str,
        session_key: str,
        path: str,
        messages: list[RichMessage],
        reader_state: dict,
        generation: int,
        *,
        signature: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Persist one transcript.

        ``signature`` must be the history signature observed *before* the
        messages were read. A newer one would let a later cold open believe a
        tail it never saw is current. Callers that do not track it may omit it;
        the file is then stamped as of now (the pre-``signature`` behaviour).
        """
        if signature is None:
            signature = history_signature(path)
        if signature is None:
            return
        payload = json.dumps(
            [item.to_dict() for item in messages],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        state = json.dumps(reader_state, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock, self._connect() as conn:
            if conn is None:
                return
            statement = (
                "INSERT OR REPLACE INTO transcript("
                "runtime, session_key, path, dev, ino, size, mtime_ns, "
                "parser_version, generation, reader_state, payload, accessed_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
            )
            params = (
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
            )
            self._write_row(conn, statement, params)

    # -- Your prompts projection -----------------------------------------
    #
    # ``session.userPrompts`` lists every human prompt of a session, so the
    # host used to page the whole history back to its first message on every
    # cold open. The projection is a pure function of a complete transcript
    # (same generation, same seq space), so it is stored next to it and only
    # extended with prompts newer than ``upto_seq``.
    #
    # Like the transcript row, ``size`` is the pre-read file size: a projection
    # written here is complete for the file as it was then. Earlier prompts
    # never change while a history grows, so a larger file is still usable.

    def get_prompts(
        self,
        runtime: str,
        session_key: str,
        path: str,
        generation: int,
    ) -> tuple[list[dict], int] | None:
        """Cached complete prompt projection for this generation, if any.

        Returns ``(rows, upto_seq)``: every human prompt up to message
        ``upto_seq``, oldest first, in the exact ``session.userPrompts`` row
        shape. ``None`` means the caller must walk the history.
        """
        current = file_signature(path)
        if current is None:
            return None
        # Read path: same no-build-storage contract as get(); a missing table
        # is a miss the next put repairs.
        with self._connect(create=False) as conn:
            if conn is None:
                return None
            try:
                row = conn.execute(
                    "SELECT ino, size, generation, upto_seq, complete, payload "
                    "FROM session_prompts WHERE runtime=? AND session_key=?",
                    (runtime, session_key),
                ).fetchone()
            except sqlite3.Error:
                return None
            if row is None:
                return None
            ino, size, stored_generation, upto_seq, complete, raw = row
            if not int(complete or 0):
                return None
            if int(ino) != current[1]:
                return None
            if int(size) > current[2]:
                # File shrank: truncated or replaced, seq space may differ.
                return None
            if int(stored_generation) != int(generation or 0):
                return None
            try:
                rows = json.loads(raw or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                return None
            if not isinstance(rows, list):
                return None
            return [item for item in rows if isinstance(item, dict)], int(upto_seq or 0)

    def put_prompts(
        self,
        runtime: str,
        session_key: str,
        path: str,
        generation: int,
        rows: list[dict],
        *,
        upto_seq: int,
    ) -> None:
        """Store a projection that covers the session start.

        Partial walks are never cached: a later call would have to merge an
        unknown middle, so re-walking is cheaper than serving a wrong list.
        """
        current = file_signature(path)
        if current is None:
            return
        payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
        with self._write_lock, self._connect() as conn:
            if conn is None:
                return
            statement = (
                "INSERT OR REPLACE INTO session_prompts("
                "runtime, session_key, path, ino, size, generation, upto_seq, "
                "complete, payload, accessed_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?)"
            )
            params = (
                runtime,
                session_key,
                path,
                current[1],
                current[2],
                int(generation or 0),
                int(upto_seq or 0),
                1,
                payload,
                time.time(),
            )
            self._write_row(conn, statement, params)

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
