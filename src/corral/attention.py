"""会话关注状态：未读、执行中与等待回答的本地持久化。

本模块只保存运行时、会话标识、事件令牌和时间，不保存对话正文。状态库是可降级的
本地派生数据：损坏、锁竞争或只读文件系统都不能阻断会话扫描和终端界面。
"""

from __future__ import annotations

import logging
import os
import sqlite3
import stat
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from corral.legacy_names import cache_dir as product_cache_dir
from corral.models import SessionInfo, session_key

AttentionPhase = Literal["working", "waiting", "idle", "unknown"]
AttentionSource = Literal["history", "observer"]
AttentionKind = Literal["none", "unread", "working", "waiting"]

_SCHEMA_VERSION = 1
_DEFAULT_MAX_AGE_SECONDS = 180 * 24 * 60 * 60
_SOURCE_RANK: dict[AttentionSource, int] = {"history": 0, "observer": 1}

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AttentionEvidence:
    """运行时历史或观察器提供的一次会话状态证据。"""

    phase: AttentionPhase = "unknown"
    activity_token: str | None = None
    question_token: str | None = None
    observed_at: float = 0.0
    source: AttentionSource = "history"


@dataclass(frozen=True)
class AttentionState:
    """界面直接消费的会话关注状态。"""

    kind: AttentionKind = "none"
    activity_token: str | None = None
    updated_at: float = 0.0


@dataclass(frozen=True)
class _StoredState:
    phase: AttentionPhase
    activity_token: str | None
    question_token: str | None
    seen_token: str | None
    observed_at: float
    source: AttentionSource
    updated_at: float


def attention_cache_dir() -> Path:
    """返回 corral 缓存目录，遵循统一的目录覆盖约定。"""
    return product_cache_dir()


def attention_cache_path() -> Path:
    return attention_cache_dir() / "session-attention.sqlite3"


def _clean_token(value: str | None) -> str | None:
    if value is None:
        return None
    token = str(value).strip()
    return token or None


def _state_kind(state: _StoredState) -> AttentionKind:
    # 黄色、绿色描述当前任务阶段，优先覆盖潜在的未读红点。
    if state.phase == "waiting" and state.question_token:
        return "waiting"
    if state.phase == "working":
        return "working"
    if state.activity_token and state.activity_token != state.seen_token:
        return "unread"
    return "none"


def _public_state(state: _StoredState | None) -> AttentionState:
    if state is None:
        return AttentionState()
    return AttentionState(_state_kind(state), state.activity_token, state.updated_at)


class AttentionStore:
    """多进程安全的 SQLite 会话关注状态库。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path).expanduser() if path is not None else attention_cache_path()
        self._warning_lock = threading.Lock()
        self._degraded_reported = False

    def _report_degraded(self, error: BaseException) -> None:
        with self._warning_lock:
            if self._degraded_reported:
                return
            self._degraded_reported = True
        logger.warning("会话状态缓存不可用，已降级为无状态显示：%s", error)

    def _open(self) -> sqlite3.Connection | None:
        conn: sqlite3.Connection | None = None
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)
            conn = sqlite3.connect(self.path, timeout=1.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=1000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._init_schema(conn)
            try:
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            return conn
        except (OSError, sqlite3.Error) as error:
            if conn is not None:
                conn.close()
            self._report_degraded(error)
            return None

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS attention_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS session_attention (
                runtime_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                phase TEXT NOT NULL,
                activity_token TEXT,
                question_token TEXT,
                seen_token TEXT,
                observed_at REAL NOT NULL,
                source TEXT NOT NULL,
                updated_at REAL NOT NULL,
                PRIMARY KEY(runtime_id, session_id)
            );
            CREATE INDEX IF NOT EXISTS session_attention_updated
            ON session_attention(updated_at);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO attention_meta(key, value) VALUES('schema_version', ?)",
            (str(_SCHEMA_VERSION),),
        )
        conn.execute(
            "INSERT OR IGNORE INTO attention_meta(key, value) VALUES('created_at', ?)",
            (repr(time.time()),),
        )
        conn.commit()

    @staticmethod
    def _row_state(row: sqlite3.Row | None) -> _StoredState | None:
        if row is None:
            return None
        try:
            phase = str(row["phase"])
            source = str(row["source"])
            if phase not in {"working", "waiting", "idle", "unknown"}:
                phase = "unknown"
            if source not in {"history", "observer"}:
                source = "history"
            return _StoredState(
                phase=phase,  # type: ignore[arg-type]
                activity_token=_clean_token(row["activity_token"]),
                question_token=_clean_token(row["question_token"]),
                seen_token=_clean_token(row["seen_token"]),
                observed_at=float(row["observed_at"]),
                source=source,  # type: ignore[arg-type]
                updated_at=float(row["updated_at"]),
            )
        except (KeyError, TypeError, ValueError, IndexError):
            return None

    @classmethod
    def _load_state(
        cls, conn: sqlite3.Connection, runtime_id: str, session_id: str,
    ) -> _StoredState | None:
        row = conn.execute(
            "SELECT phase,activity_token,question_token,seen_token,observed_at,source,updated_at "
            "FROM session_attention WHERE runtime_id=? AND session_id=?",
            (runtime_id, session_id),
        ).fetchone()
        return cls._row_state(row)

    @staticmethod
    def _save_state(
        conn: sqlite3.Connection, runtime_id: str, session_id: str, state: _StoredState,
    ) -> None:
        conn.execute(
            "INSERT INTO session_attention "
            "(runtime_id,session_id,phase,activity_token,question_token,seen_token,"
            "observed_at,source,updated_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(runtime_id,session_id) DO UPDATE SET "
            "phase=excluded.phase, activity_token=excluded.activity_token, "
            "question_token=excluded.question_token, seen_token=excluded.seen_token, "
            "observed_at=excluded.observed_at, source=excluded.source, "
            "updated_at=excluded.updated_at",
            (
                runtime_id,
                session_id,
                state.phase,
                state.activity_token,
                state.question_token,
                state.seen_token,
                state.observed_at,
                state.source,
                state.updated_at,
            ),
        )

    @staticmethod
    def _baseline_complete(conn: sqlite3.Connection) -> bool:
        row = conn.execute(
            "SELECT value FROM attention_meta WHERE key='baseline_complete'"
        ).fetchone()
        return row is not None and row[0] == "1"

    @staticmethod
    def _store_created_at(conn: sqlite3.Connection) -> float:
        row = conn.execute(
            "SELECT value FROM attention_meta WHERE key='created_at'"
        ).fetchone()
        try:
            return float(row[0]) if row is not None else 0.0
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _activity_predates_store(
        cls, conn: sqlite3.Connection, evidence: AttentionEvidence,
    ) -> bool:
        # 原生事件时间使用 Unix epoch；测试/某些 opaque 顺序号的小整数不能拿来
        # 和墙上时钟比较，否则会把真正的新活动永久当成升级前旧内容。
        return (
            evidence.observed_at >= 1_000_000_000
            and evidence.observed_at <= cls._store_created_at(conn)
        )

    @staticmethod
    def _observer_event(evidence: AttentionEvidence) -> str:
        """观察器活动令牌形如 ``<generation>:<event>``；对不上时返回空串。"""
        if evidence.source != "observer":
            return ""
        token = str(evidence.activity_token or "")
        if ":" not in token:
            return ""
        return token.rsplit(":", 1)[-1]

    @staticmethod
    def _current_fact_time(
        evidence: AttentionEvidence,
        current: _StoredState | None,
        now: float,
    ) -> float:
        """把仍成立的当前事实时间推进到已存状态之后，避免被更早的历史时间挡掉。"""
        return max(
            evidence.observed_at,
            (current.observed_at + 0.000001) if current else 0.0,
            now,
        )

    @staticmethod
    def _is_newer(evidence: AttentionEvidence, current: _StoredState) -> bool:
        if evidence.observed_at > current.observed_at:
            return True
        if evidence.observed_at < current.observed_at:
            return False
        incoming_rank = _SOURCE_RANK[evidence.source]
        current_rank = _SOURCE_RANK[current.source]
        if incoming_rank != current_rank:
            return incoming_rank > current_rank
        # 同一份历史证据会在每轮扫描中重复出现；内容也相同时不刷新 updated_at，
        # 否则陈旧记录永远无法被 prune。相同时间内的阶段或令牌变化仍需接纳。
        return any(
            (
                evidence.phase not in {"unknown", current.phase},
                _clean_token(evidence.activity_token) not in {None, current.activity_token},
                _clean_token(evidence.question_token) not in {None, current.question_token},
            )
        )

    @classmethod
    def _apply_evidence(
        cls,
        current: _StoredState | None,
        evidence: AttentionEvidence,
        *,
        baseline_new_activity: bool,
        now: float,
    ) -> _StoredState:
        activity_token = _clean_token(evidence.activity_token)
        question_token = _clean_token(evidence.question_token)
        observed_at = evidence.observed_at if evidence.observed_at > 0 else now

        if (
            current is not None
            and current.phase == "working"
            and current.source == "observer"
            and cls._observer_event(
                AttentionEvidence(
                    phase="working",
                    activity_token=current.activity_token,
                    source="observer",
                )
            )
            == "afterAgentResponse"
        ):
            # 旧版本把「本轮说完」记成执行中；进程仍活着时后续 unknown 会把绿点钉死。
            current = replace(current, phase="idle")

        if (
            current is not None
            and current.phase == "waiting"
            and current.question_token
            and evidence.source == "observer"
            and cls._observer_event(evidence) not in {"beforeSubmitPrompt", "sessionEnd"}
        ):
            # stop / afterAgentResponse 表示本轮生成结束，不等于用户已经作答。
            return current

        if (
            current is not None
            and current.phase == "working"
            and current.source == "observer"
            and cls._observer_event(
                AttentionEvidence(
                    phase="working",
                    activity_token=current.activity_token,
                    source="observer",
                )
            )
            == "beforeSubmitPrompt"
            and evidence.source == "history"
            and evidence.phase == "idle"
        ):
            # 本轮已由用户提交打开：中间助手正文不得提前熄灭绿点。真正收束仍靠
            # afterAgentResponse / stop / sessionEnd，或进程不活时的强制 idle。
            return current

        if current is not None and not cls._is_newer(
            replace(evidence, observed_at=observed_at), current,
        ):
            return current

        previous_phase: AttentionPhase = current.phase if current else "unknown"
        phase = evidence.phase
        if phase == "unknown" and current is not None:
            phase = current.phase
        if phase == "waiting" and not question_token:
            # 没有结构化问题标识时不能显示黄点。
            phase = current.phase if current is not None else "unknown"
            question_token = current.question_token if current is not None else None
        elif phase in {"working", "idle"}:
            question_token = None
        elif phase == "unknown" and current is not None:
            question_token = current.question_token

        if activity_token is None and current is not None:
            activity_token = current.activity_token
        if (
            current is not None
            and previous_phase in {"working", "waiting"}
            and phase == "idle"
            and activity_token == current.activity_token
        ):
            # 即使运行时没有另给完成消息标识，任务终止本身也是一次可见的新状态。
            activity_token = f"terminal:{observed_at:.6f}"

        if current is None:
            seen_token = activity_token if baseline_new_activity else None
        else:
            seen_token = current.seen_token

        return _StoredState(
            phase=phase,
            activity_token=activity_token,
            question_token=question_token,
            seen_token=seen_token,
            observed_at=observed_at,
            source=evidence.source,
            updated_at=now,
        )

    @staticmethod
    def _has_meaningful_evidence(evidence: AttentionEvidence) -> bool:
        return bool(
            evidence.phase != "unknown"
            or _clean_token(evidence.activity_token)
            or _clean_token(evidence.question_token)
        )

    def record_event(
        self, runtime_id: str, session_id: str, evidence: AttentionEvidence,
    ) -> AttentionState:
        """记录一份新证据并返回合并后的界面状态。"""
        runtime_id, session_id = str(runtime_id), str(session_id)
        if not runtime_id or not session_id:
            return AttentionState()
        conn = self._open()
        if conn is None:
            return AttentionState()
        try:
            conn.execute("BEGIN IMMEDIATE")
            current = self._load_state(conn, runtime_id, session_id)
            # 初次历史扫描属于旧内容基线；观察器事件一定是在安装后新发生的事件。
            baseline = (
                current is None
                and evidence.source == "history"
                and (
                    not self._baseline_complete(conn)
                    or self._activity_predates_store(conn, evidence)
                )
            )
            state = self._apply_evidence(
                current, evidence, baseline_new_activity=baseline, now=time.time(),
            )
            if current is None and not self._has_meaningful_evidence(evidence):
                state = None
            if state is not None and state != current:
                self._save_state(conn, runtime_id, session_id, state)
            conn.commit()
            return _public_state(state)
        except (OSError, sqlite3.Error) as error:
            conn.rollback()
            self._report_degraded(error)
            return AttentionState()
        finally:
            conn.close()

    def reconcile(
        self,
        sessions: Sequence[SessionInfo | dict],
        evidence_by_key: Mapping[str, AttentionEvidence],
    ) -> dict[str, AttentionState]:
        """把一轮会话扫描证据合并进状态库。

        首次完整调用把当时已有的活动标识设为已读。扫描确认进程已结束时，会清除旧的
        执行中/等待回答状态；这条当前活性事实优先于较早的外部观察器事件。
        """
        conn = self._open()
        keys = [session_key(session) for session in sessions]
        if conn is None:
            return {key: AttentionState() for key in keys}
        results: dict[str, AttentionState] = {}
        try:
            conn.execute("BEGIN IMMEDIATE")
            first_baseline = not self._baseline_complete(conn)
            store_created_at = self._store_created_at(conn)
            now = time.time()
            for session, key in zip(sessions, keys, strict=True):
                runtime_id = str(session.get("source") or "unknown")
                session_id = str(session.get("id") or "")
                if not session_id:
                    results[key] = AttentionState()
                    continue
                current = self._load_state(conn, runtime_id, session_id)
                evidence = evidence_by_key.get(key)
                live = bool(session.get("live"))

                if evidence is not None:
                    if not live and (
                        evidence.phase in {"working", "waiting"}
                        or (
                            current is not None
                            and current.phase in {"working", "waiting"}
                        )
                    ):
                        # 当前进程不活是本轮扫描得到的即时事实，优先级高于任何较旧
                        # observer/history 阶段。时间必须推进到 current 之后，确保合并
                        # 接纳这次强制 idle；下一轮 current 已 idle，不会重复造终止令牌。
                        evidence = replace(
                            evidence,
                            phase="idle",
                            question_token=None,
                            observed_at=self._current_fact_time(evidence, current, now),
                        )
                    elif (
                        live
                        and evidence.phase == "waiting"
                        and _clean_token(evidence.question_token)
                    ):
                        # 进程仍活且历史里有未配对结构化问题：这是当前事实，优先于
                        # 更早的「生成结束 / 误判不活」时间戳。
                        evidence = replace(
                            evidence,
                            observed_at=self._current_fact_time(evidence, current, now),
                        )
                    elif (
                        live
                        and current is not None
                        and current.phase == "waiting"
                        and current.question_token
                        and not _clean_token(evidence.question_token)
                        and evidence.phase != "idle"
                    ):
                        # 提问已从历史消失、进程还在：视为继续执行，避免黄点粘住。
                        # 若历史已经给出本轮结束，不能再因为进程还在而强行亮绿。
                        evidence = replace(
                            evidence,
                            phase="working",
                            question_token=None,
                            observed_at=self._current_fact_time(evidence, current, now),
                        )
                    state = self._apply_evidence(
                        current,
                        evidence,
                        baseline_new_activity=(
                            current is None
                            and evidence.source == "history"
                            and (
                                first_baseline
                                or (
                                    evidence.observed_at >= 1_000_000_000
                                    and evidence.observed_at <= store_created_at
                                )
                            )
                        ),
                        now=now,
                    )
                elif current is not None and not live and current.phase in {"working", "waiting"}:
                    state = self._apply_evidence(
                        current,
                        AttentionEvidence(
                            phase="idle",
                            observed_at=max(current.observed_at, now),
                            source="history",
                        ),
                        baseline_new_activity=False,
                        now=now,
                    )
                else:
                    state = current

                if (
                    current is None
                    and evidence is not None
                    and not self._has_meaningful_evidence(evidence)
                ):
                    state = None
                if state is not None and state != current:
                    self._save_state(conn, runtime_id, session_id, state)
                results[key] = _public_state(state)

            if first_baseline and sessions:
                conn.execute(
                    "INSERT OR REPLACE INTO attention_meta(key,value) "
                    "VALUES('baseline_complete','1')"
                )
            conn.commit()
            return results
        except (OSError, sqlite3.Error) as error:
            conn.rollback()
            self._report_degraded(error)
            return {key: AttentionState() for key in keys}
        finally:
            conn.close()

    def working_pairs(self) -> list[tuple[str, str]]:
        """返回当前 phase=working 的 (runtime_id, session_id)；库不可用时返回空列表。

        供保活压力回收判断「进行中」：长任务可能长时间无终端输出，不得只靠
        tmux session_activity 判断。
        """
        return self._pairs_in_phases(("working",)) or []

    def busy_pairs(self) -> list[tuple[str, str]] | None:
        """返回 phase 为 working 或 waiting 的 (runtime_id, session_id)。

        供静默回收判断「不可动」：执行中与等待用户回答的会话都不能停。与
        ``working_pairs`` 不同，库不可用时返回 ``None`` 而不是空列表——回收方必须把
        「不知道」当作阻断，不能把它读成「没有会话在忙」。
        """
        return self._pairs_in_phases(("working", "waiting"))

    def _pairs_in_phases(self, phases: tuple[str, ...]) -> list[tuple[str, str]] | None:
        conn = self._open()
        if conn is None:
            return None
        try:
            marks = ",".join("?" for _ in phases)
            rows = conn.execute(
                "SELECT runtime_id, session_id FROM session_attention "
                f"WHERE phase IN ({marks})",
                phases,
            ).fetchall()
            pairs: list[tuple[str, str]] = []
            for row in rows:
                runtime_id = str(row["runtime_id"] or "").strip()
                session_id = str(row["session_id"] or "").strip()
                if runtime_id and session_id:
                    pairs.append((runtime_id, session_id))
            return pairs
        except (OSError, sqlite3.Error) as error:
            self._report_degraded(error)
            return None
        finally:
            conn.close()

    def get(self, runtime_id: str, session_id: str) -> AttentionState:
        conn = self._open()
        if conn is None:
            return AttentionState()
        try:
            return _public_state(self._load_state(conn, str(runtime_id), str(session_id)))
        except (OSError, sqlite3.Error) as error:
            self._report_degraded(error)
            return AttentionState()
        finally:
            conn.close()

    def get_many(self, keys: Iterable[str]) -> dict[str, AttentionState]:
        requested = list(dict.fromkeys(str(key) for key in keys))
        results = {key: AttentionState() for key in requested}
        if not requested:
            return results
        conn = self._open()
        if conn is None:
            return results
        try:
            for key in requested:
                runtime_id, separator, session_id = key.partition(":")
                if not separator or not runtime_id or not session_id:
                    continue
                results[key] = _public_state(
                    self._load_state(conn, runtime_id, session_id)
                )
            return results
        except (OSError, sqlite3.Error) as error:
            self._report_degraded(error)
            return {key: AttentionState() for key in requested}
        finally:
            conn.close()

    def mark_read(self, runtime_id: str, session_id: str) -> AttentionState:
        """仅清除红色未读；黄点和绿点保持当前任务阶段。"""
        conn = self._open()
        if conn is None:
            return AttentionState()
        try:
            conn.execute("BEGIN IMMEDIATE")
            state = self._load_state(conn, str(runtime_id), str(session_id))
            if state is None:
                conn.commit()
                return AttentionState()
            state = replace(state, seen_token=state.activity_token, updated_at=time.time())
            self._save_state(conn, str(runtime_id), str(session_id), state)
            conn.commit()
            return _public_state(state)
        except (OSError, sqlite3.Error) as error:
            conn.rollback()
            self._report_degraded(error)
            return AttentionState()
        finally:
            conn.close()

    def migrate_session(self, runtime_id: str, old_session_id: str, new_session_id: str) -> None:
        """把占位会话状态迁移到同运行时的正式会话标识。"""
        if old_session_id == new_session_id:
            return
        conn = self._open()
        if conn is None:
            return
        try:
            conn.execute("BEGIN IMMEDIATE")
            old = self._load_state(conn, str(runtime_id), str(old_session_id))
            new = self._load_state(conn, str(runtime_id), str(new_session_id))
            if old is not None:
                chosen = old
                if new is not None:
                    new_is_newer = (
                        new.observed_at > old.observed_at
                        or (
                            new.observed_at == old.observed_at
                            and _SOURCE_RANK[new.source] >= _SOURCE_RANK[old.source]
                        )
                    )
                    chosen = new if new_is_newer else old
                self._save_state(conn, str(runtime_id), str(new_session_id), chosen)
                conn.execute(
                    "DELETE FROM session_attention WHERE runtime_id=? AND session_id=?",
                    (str(runtime_id), str(old_session_id)),
                )
            conn.commit()
        except (OSError, sqlite3.Error) as error:
            conn.rollback()
            self._report_degraded(error)
        finally:
            conn.close()

    def remove_session(self, runtime_id: str, session_id: str) -> None:
        conn = self._open()
        if conn is None:
            return
        try:
            conn.execute(
                "DELETE FROM session_attention WHERE runtime_id=? AND session_id=?",
                (str(runtime_id), str(session_id)),
            )
            conn.commit()
        except (OSError, sqlite3.Error) as error:
            conn.rollback()
            self._report_degraded(error)
        finally:
            conn.close()

    def prune(self, max_age_seconds: float = _DEFAULT_MAX_AGE_SECONDS) -> int:
        """删除长期未更新的派生状态并返回删除条数。"""
        conn = self._open()
        if conn is None:
            return 0
        try:
            cursor = conn.execute(
                "DELETE FROM session_attention WHERE updated_at < ?",
                (time.time() - max(0.0, float(max_age_seconds)),),
            )
            conn.commit()
            return max(0, cursor.rowcount)
        except (OSError, sqlite3.Error, ValueError) as error:
            conn.rollback()
            self._report_degraded(error)
            return 0
        finally:
            conn.close()
