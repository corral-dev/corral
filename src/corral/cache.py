"""corral 本地派生缓存：会话元数据与对话正文。

缓存只保存可从原始历史重建的数据。任何错误都应退化为缓存未命中，不能阻断扫描。
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from corral.legacy_names import cache_dir as product_cache_dir
from corral.legacy_names import getenv
from corral.models import ConversationMessage

SCHEMA_VERSION = 1
DEFAULT_MAX_MB = 256
# Bump when list metadata extraction changes; otherwise performance-cache keeps
# old first_user_msg slices (2026-09-12: handoff digest must be extracted before
# the 300-char clip; 2026-09-29.3: Claude mid-turn queued_command prompts now
# count as user messages in conversation/Your prompts).
_PARSER_VERSION = "2026-09-29.3"


def enabled() -> bool:
    return (getenv("CACHE", "1") or "1").strip().lower() not in {"0", "false", "no", "off"}


def cache_dir() -> Path:
    return product_cache_dir()


def cache_path() -> Path:
    return cache_dir() / "performance-cache.sqlite3"


def max_bytes() -> int:
    try:
        value = int(getenv("CACHE_MAX_MB", str(DEFAULT_MAX_MB)) or DEFAULT_MAX_MB)
    except ValueError:
        value = DEFAULT_MAX_MB
    return max(16, value) * 1024 * 1024


def file_signature(path: str) -> tuple[int, int, int, int] | None:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def history_signature(path: str) -> tuple[int, int, int, int] | None:
    """历史文件缓存签名；SQLite 旁路 WAL 有变更时必须失效。

    Cursor / OpenCode 等 WAL 库的最新写入常只更新 ``path-wal``，主文件 size/mtime
    不动。对话预览若只签主文件，会一直命中缺尾消息的旧缓存。
    返回值仍是 4 元组，兼容现有 conversation 表列；identity 取自主文件，
    size/mtime_ns 与 WAL 混折后写入，任一端变化都会 miss。
    """
    main = file_signature(path)
    if main is None:
        return None
    wal = file_signature(path + "-wal")
    if wal is None:
        return main
    # wal[0]/wal[1]（dev/ino）不参与：WAL 重建后 ino 会变，但内容等价于「有旁路」。
    return (main[0], main[1], (main[2] + wal[2]) & 0x7FFFFFFFFFFFFFFF, main[3] ^ wal[3])


class PerformanceCache:
    """多进程安全的 SQLite 派生缓存；失败时始终按未命中处理。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or cache_path()
        self._local = threading.local()
        self._pending_lock = threading.Lock()
        self._pending_sessions: list[tuple] = []
        # 一轮扫描内的元数据快照：runtime -> {path: (dev, ino, size, mtime_ns,
        # parser_version, payload)}。None 表示当前不在扫描期间，走逐条查询老路。
        # 见 begin_scan()／_session_snapshot() 的说明。
        self._snapshot_lock = threading.Lock()
        self._snapshots: dict[str, dict[str, tuple]] | None = None

    @contextmanager
    def _connect(self, *, create: bool = True) -> Iterator[sqlite3.Connection | None]:
        if not enabled():
            yield None
            return
        conn = getattr(self._local, "connection", None)
        if conn is not None and create:
            # 连接已经建好，就说明目录当时已创建成功，热路径上不必再 mkdir + chmod
            # 一遍：一次 Codex 扫描原本要为此白做约 1900 次系统调用。create=False
            # 的冷路径（status/clear）保持原样，它还要靠 path.exists() 判断库在不在。
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
            # 缓存损坏、竞争或只读文件系统都只能造成未命中，不能影响原始会话读取。
            try:
                conn.rollback()
            except sqlite3.Error:
                pass

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS cache_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_meta (
                runtime TEXT NOT NULL,
                path TEXT NOT NULL,
                dev INTEGER NOT NULL,
                ino INTEGER NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                parser_version TEXT NOT NULL,
                payload TEXT NOT NULL,
                accessed_at REAL NOT NULL,
                PRIMARY KEY(runtime, path)
            );
            CREATE TABLE IF NOT EXISTS conversation (
                runtime TEXT NOT NULL,
                session_key TEXT NOT NULL,
                path TEXT NOT NULL,
                dev INTEGER NOT NULL,
                ino INTEGER NOT NULL,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                parser_version TEXT NOT NULL,
                payload TEXT NOT NULL,
                accessed_at REAL NOT NULL,
                PRIMARY KEY(runtime, session_key)
            );
            CREATE INDEX IF NOT EXISTS conversation_lru ON conversation(accessed_at);
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO cache_meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        conn.commit()

    def begin_scan(self) -> None:
        """标记一轮扫描开始：本轮内每个运行时的元数据只查一次库。

        扫描要在上千个候选文件里筛出最近的几十条（大量候选会被子代理线程、空
        会话、目录已删等规则过滤掉），逐条查库意味着一次 Codex 扫描要发起约
        950 次独立查询。快照必须严格限定在一轮扫描内，否则同一进程里后续扫描
        看不到本轮新写入的会话。
        """
        with self._snapshot_lock:
            self._snapshots = {}

    def end_scan(self) -> None:
        with self._snapshot_lock:
            self._snapshots = None

    def _session_snapshot(self, runtime: str) -> dict[str, tuple] | None:
        """取（必要时建）本轮扫描的元数据快照；不在扫描期间或取数失败时返回 None。"""
        with self._snapshot_lock:
            if self._snapshots is None:
                return None
            cached = self._snapshots.get(runtime)
            if cached is not None:
                return cached
        # 建快照时不持锁，避免阻塞其它运行时的扫描线程；_connect 会把 sqlite 异常
        # 吞成"未命中"，所以用 rows is None 判断有没有真正取到数，取不到就回退逐条查。
        rows = None
        with self._connect() as conn:
            if conn is not None:
                rows = conn.execute(
                    "SELECT path, dev, ino, size, mtime_ns, parser_version, payload "
                    "FROM session_meta WHERE runtime=?",
                    (runtime,),
                ).fetchall()
        if rows is None:
            return None
        loaded = {row[0]: tuple(row[1:]) for row in rows}
        with self._snapshot_lock:
            if self._snapshots is None:
                return loaded  # 扫描已经结束，这份仍可用但不入册
            return self._snapshots.setdefault(runtime, loaded)

    @staticmethod
    def _decode_session_row(row, signature: tuple, version: str) -> dict | None:
        """校验签名与解析器版本，通过了才解码 payload。

        解码必须保持惰性：快照里装着该运行时的全部条目，本轮只会用到其中一小
        部分，提前解码等于白做大量无用功，收益会被吃光。
        """
        if row is None or tuple(row[:4]) != signature or row[4] != version:
            return None
        try:
            payload = json.loads(row[5])
        except (TypeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def get_session(self, runtime: str, path: str, extra_version: str = "") -> dict | None:
        signature = file_signature(path)
        if signature is None:
            return None
        version = _PARSER_VERSION + extra_version
        snapshot = self._session_snapshot(runtime)
        if snapshot is not None:
            return self._decode_session_row(snapshot.get(path), signature, version)
        with self._connect() as conn:
            if conn is None:
                return None
            row = conn.execute(
                "SELECT dev, ino, size, mtime_ns, parser_version, payload "
                "FROM session_meta WHERE runtime=? AND path=?",
                (runtime, path),
            ).fetchone()
            return self._decode_session_row(row, signature, version)

    def put_session(self, runtime: str, path: str, payload: dict, extra_version: str = "") -> None:
        signature = file_signature(path)
        if signature is None:
            return
        try:
            encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return
        with self._pending_lock:
            self._pending_sessions.append(
                (runtime, path, *signature, _PARSER_VERSION + extra_version, encoded, time.time())
            )

    def flush_pending(self) -> None:
        """把扫描线程积累的元数据一次事务落盘，避免每条会话各做一次同步提交。"""
        with self._pending_lock:
            pending, self._pending_sessions = self._pending_sessions, []
        if not pending:
            return
        with self._connect() as conn:
            if conn is None:
                return
            conn.executemany(
                "INSERT OR REPLACE INTO session_meta "
                "(runtime,path,dev,ino,size,mtime_ns,parser_version,payload,accessed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                pending,
            )
            conn.commit()
        self.prune()

    def get_conversation(
        self, runtime: str, session_key: str, path: str,
    ) -> list[ConversationMessage] | None:
        signature = history_signature(path)
        if signature is None:
            return None
        row = None
        with self._connect() as conn:
            if conn is None:
                return None
            row = conn.execute(
                "SELECT dev, ino, size, mtime_ns, parser_version, payload "
                "FROM conversation WHERE runtime=? AND session_key=?",
                (runtime, session_key),
            ).fetchone()
            if row is None or tuple(row[:4]) != signature or row[4] != _PARSER_VERSION:
                return None
            try:
                raw = json.loads(row[5])
                messages = [
                    ConversationMessage(str(item[0]), str(item[1]), item[2])
                    for item in raw
                ]
            except (TypeError, ValueError, json.JSONDecodeError, IndexError):
                return None
            return messages

    def put_conversation(
        self, runtime: str, session_key: str, path: str, messages: list[ConversationMessage],
    ) -> None:
        signature = history_signature(path)
        if signature is None:
            return
        raw = [[item.role, item.text, item.timestamp] for item in messages]
        encoded = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        with self._connect() as conn:
            if conn is None:
                return
            conn.execute(
                "INSERT OR REPLACE INTO conversation "
                "(runtime,session_key,path,dev,ino,size,mtime_ns,parser_version,payload,accessed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    runtime, session_key, path, *signature, _PARSER_VERSION,
                    encoded, time.time(),
                ),
            )
            conn.commit()
        self.prune()

    def prune(self) -> None:
        def total_size() -> int:
            return sum(
                candidate.stat().st_size
                for candidate in (
                    self.path,
                    Path(str(self.path) + "-wal"),
                    Path(str(self.path) + "-shm"),
                )
                if candidate.exists()
            )

        try:
            if not self.path.exists() or total_size() <= max_bytes():
                return
        except OSError:
            return
        with self._connect() as conn:
            if conn is None:
                return
            while self.path.exists() and total_size() > max_bytes():
                deleted = conn.execute(
                    "DELETE FROM conversation WHERE rowid IN "
                    "(SELECT rowid FROM conversation ORDER BY accessed_at LIMIT 64)"
                ).rowcount
                if not deleted:
                    break
                conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def status(self) -> dict:
        from corral.native import available as native_available

        result = {
            "enabled": enabled(),
            "native_accelerator": native_available(),
            "path": str(self.path),
            "size_bytes": 0,
            "max_bytes": max_bytes(),
            "session_count": 0,
            "conversation_count": 0,
            "search_index_count": 0,
            "schema_version": SCHEMA_VERSION,
        }
        try:
            result["size_bytes"] = sum(
                path.stat().st_size
                for path in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm"))
                if path.exists()
            )
        except OSError:
            pass
        with self._connect(create=False) as conn:
            if conn is not None:
                result["session_count"] = conn.execute("SELECT count(*) FROM session_meta").fetchone()[0]
                result["conversation_count"] = conn.execute("SELECT count(*) FROM conversation").fetchone()[0]
        return result

    def clear(self, *, dry_run: bool = False) -> dict:
        status = self.status()
        remote_base = cache_dir() / "remote-transcripts.sqlite3"
        remote_files = (
            remote_base,
            Path(str(remote_base) + "-wal"),
            Path(str(remote_base) + "-shm"),
        )
        remote_exists = any(path.exists() for path in remote_files)
        existed = bool(status["session_count"] or status["conversation_count"] or remote_exists)
        if dry_run or not existed:
            return {"status": "would_clear" if dry_run and existed else "unchanged", **status}
        with self._connect(create=False) as conn:
            if conn is not None:
                conn.execute("DELETE FROM conversation")
                conn.execute("DELETE FROM session_meta")
                conn.commit()
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        for candidate in remote_files:
            try:
                candidate.unlink()
            except OSError:
                pass
        return {"status": "cleared", **self.status()}


class scan_period:
    """派生缓存的一轮扫描期：进出自洽的上下文管理器，全程吞掉异常。

    `begin_scan()` / `end_scan()` + `flush_pending()` 必须成对调用且 `end_scan`
    放进 `finally`，快照严禁跨扫描长期持有（同进程后续扫描会看不到本轮新写入
    的会话）。这条不变式此前在 `runtime/registry.py` 的 `scan_all` 和
    `agent_api.py` 的 `_scan_runtimes` 各复制了一份，写反了不报错、只表现成
    「列表少了会话」；这里收敛成唯一实现，两处都改用 `with scan_period():`。

    派生缓存永远不能影响原始会话扫描结果：缓存本身不可用（CORRAL_CACHE=0、
    数据库损坏）时整个扫描期直接跳过，与原先的 `except Exception: pass` 语义
    完全一致。
    """

    def __enter__(self) -> None:
        try:
            get_cache().begin_scan()
        except Exception:
            pass

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            get_cache().end_scan()
            get_cache().flush_pending()
        except Exception:
            pass


_CACHE = PerformanceCache()


def get_cache() -> PerformanceCache:
    return _CACHE
