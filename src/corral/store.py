"""会话仓库：扫描结果、标题缓存轮询、对话预览缓存与托管标注。"""

from __future__ import annotations

import os
import threading
import time

from corral import liveness, titles
from corral.attention import AttentionEvidence, AttentionState, AttentionStore
from corral.attention_signals import inspect_session
from corral.cache import cache_dir, get_cache
from corral.cache import enabled as cache_enabled
from corral.display import (
    _filter_sessions_by_query,
)
from corral.models import ConversationMessage, is_shell_session, session_key
from corral.projects import normalize_cwd, project_entries
from corral.runtime import RuntimeRegistry, default_registry
from corral.scan.common import is_ephemeral_agent_cwd

# 新扫到的会话：mtime 在此窗口内才插到列表最前；更旧的（常为临时 cwd 复活）
# 追加到末尾，避免几天前的会话整批顶到侧边栏。
_FRESH_PREPEND_MAX_AGE = 2 * 86400
# 有待生成标题时防抖拉后台，避免重扫抖动狂起进程。
_TITLE_SPAWN_DEBOUNCE_SECONDS = 3.0
# generating 仍非空且缓存长时间无变化时再拉一次（上一轮 daemon 可能已死）。
_TITLE_STALE_SPAWN_SECONDS = 30.0


def _session_matches_keepalive_ident(session: dict, name: str) -> bool:
    """托管名末段 ident 是否对得上这条会话 id（占位 8 位或完整 id）。"""
    if session.get("source") == "codex":
        return False
    return liveness._name_matches_session(name, session)


class SessionStore:
    """持有所有已注册运行时的会话列表与标题缓存。

    标题生成已移交独立后台进程（corral --generate-titles），本类只负责读取缓存，
    并通过轮询缓存文件把后台进程逐批写入的新标题反映到界面，自身不写缓存、
    不调用 claude，避免与后台进程重复花额度或竞争缓存文件。
    """

    def __init__(
        self,
        limit: int,
        registry: RuntimeRegistry | None = None,
        attention_store: AttentionStore | None = None,
        title_state: titles.TitleState | None = None,
    ):
        self.limit = limit
        self.registry = registry or default_registry()
        self.lock = threading.Lock()
        self.sessions: dict[str, list[dict]] = {runtime_id: [] for runtime_id in self.registry.ids}
        self.attention_store = attention_store or AttentionStore()
        self.attention_states: dict[str, AttentionState] = {}
        # Cursor 正文库只在会话存活或轻量 stat 签名变化后探测。首轮仅登记签名，
        # 禁止为所有历史会话打开 store.db，避免状态圆点拖慢首屏。
        self._cursor_attention_signatures: dict[str, tuple] = {}
        # 会话扫描签名未变化时复用上轮结构化证据，避免每次后台刷新都重新解析
        # 全部 JSONL/SQLite。值为 (失效签名, evidence)，只保留当前仍存在的会话。
        self._attention_evidence_cache: dict[str, tuple[tuple, AttentionEvidence]] = {}
        self._attention_lock = threading.Lock()
        self.display_titles: dict[str, str] = {}  # 跨运行时会话键 -> 当前展示标题
        self.dirty = threading.Event()
        self.title_state = title_state or titles.TitleState(mtime_fn=self._cache_file_mtime)
        self.cache = self.title_state.cache
        self.generating: set[str] = set()  # 仍是临时兜底、等待后台进程产出的会话键（转圈圈）
        # 可注入的标题后台拉起函数；默认懒加载 cli._spawn_title_daemon，测试可替换。
        self._title_spawn_fn = None
        self._last_title_spawn_at = 0.0
        self._generating_since: float | None = None
        # 本进程内嵌托管的 会话键 -> tmux 会话名。_embed_open 在启动成功的瞬间就写入，
        # 比 annotate() 的 pid 祖先链匹配更快、更确定：运行时还没来得及注册 pid 文件
        # （或像某些 fake CLI 一样根本不注册）时，后台重扫替换会话字典后仍能立刻恢复
        # keepalive_name，避免 x 拒绝关闭、回车误开竞争进程。
        self.hosted: dict[str, str] = {}
        # 跨运行时接力 / 空白新建：目标助手尚未落盘历史时，扫描器看不到条目。
        # 这里暂存本进程插入的「运行中(托管)」占位卡，后台重扫时若磁盘仍无对应
        # 会话且 tmux 还活着，就重新灌回列表；真实会话一经 annotate 挂上同一
        # keepalive 名，占位卡即退役（见 _merge_scanned）。
        self._provisional: dict[str, dict] = {}
        # 占位卡转正：旧会话键 → 新会话键。分屏组记忆可能还挂着旧键，
        # 而右栏已经切走，不能只靠当前格子上的托管名去迁。
        self._session_key_migrations: dict[str, str] = {}
        # 用户刚用 q 结束的会话键：杀掉到进程真正退出之间，扫描仍可能报 live=True。
        # 在确认已死之前强制按已结束展示，避免「托管 → 运行中 → 已结束」闪烁。
        self._force_ended: set[str] = set()
        # 用户按 x 确认删除过的会话键（tombstone），阻止 _merge_scanned 把卡片
        # 灌回列表。只在删除失败时解除，成功后必须永久保留：后台重扫是「先读磁盘、
        # 后合并」两段式，删除动作很容易落在某轮已读完磁盘、还没合并的窗口里，
        # tombstone 一旦提前解除，那轮携带旧数据的合并就会把卡片重新灌回侧边栏。
        self._deleted: set[str] = set()
        # 值是 (读取时的历史文件 mtime, 消息列表)；文件 mtime 变化就重读，
        # 修掉"同一次 corral 内 / 关闭预览重开还是旧内容"的问题。
        self.conversations: dict[str, tuple[float | None, list[ConversationMessage]]] = {}
        self._cache_mtime: float = self._cache_file_mtime()
        self._projects: list[dict] | None = None  # 项目聚合缓存，仅在 load() 时失效
        # 稳定的展示顺序（跨运行时会话键）：列表展示出来后已有会话位置固定，
        # 后台重扫只把「新出现」的会话插到最前，不再按 mtime 整体重排——
        # 否则运行中的会话一有消息更新就跳到列表顶上，用户刚要看的位置全乱（用户实报）。
        self._order: list[str] = []
        # load() 是否已经跑完至少一次：main() 现在把 load() 挪到后台线程异步跑，
        # UI 侧（MainScreen）据此决定是直接渲染已有数据，还是先展示空骨架列表、
        # 挂一个 worker 等它完成。_load_event 供 UI 线程阻塞等待，避免和 main()
        # 里预先起的加载线程重复扫描一次。
        self.loaded = False
        # 启动时是否已从磁盘快照秒开了会话列表（真扫描仍会照常跑并收敛）。
        # 与 `loaded` 互不影响：快照态下列表先田旧数据渲染，「没有会话」空态、
        # 启动分屏恢复等仍等真扫描完成。
        self.hydrated = False
        self._load_event = threading.Event()
        # 有侧栏快照时：main() 推迟启动 load 线程，等首帧画完再扫，避免与铺表抢锁。
        self._load_deferred = False
        self.load_error: str | None = None
        # 最近一次完整 `_merge_scanned`（非整表缓存命中后的轻量合并）的单调时钟。
        # 助手持续写 WAL 时签名仍可能命中；周期性全量合并兜住新会话与结束态。
        self._last_full_merge_at: float | None = None
        # 最近一次真正本地磁盘扫描（非共享索引）的单调时钟；到点强制 prefer_shared=False。
        self._last_local_scan_at: float | None = None
        # Wall clock of the last successful load/refresh (for honest "updated Ns ago").
        self.last_refresh_at: float | None = None

    # ---- 侧边栏快照：启动秒开（stale-while-revalidate） ----

    @staticmethod
    def _snapshot_path():
        return cache_dir() / "sidebar-snapshot.json"

    _SNAPSHOT_VERSION = 1
    # Snapshot rewrites (~400 KB) are startup-hydration only: minute-level
    # staleness is fine, so skip the write when the key set is unchanged and
    # the last save is recent. Session arrival/departure always saves.
    _SNAPSHOT_MIN_INTERVAL_SECONDS = 60.0

    def _save_sidebar_snapshot(self) -> None:
        """把当前合并后的会话列表落盘，供下次启动秒开（后台线程内调用）。

        只存展示元数据与稳定顺序，不存 hosted/占位这类进程内运行时态。
        任何失败都静默：快照只是加速，不能影响扫描与合并本身。
        `CORRAL_CACHE=0` 时禁用（与派生缓存同一开关）。
        """
        import json

        if not cache_enabled():
            return
        try:
            with self.lock:
                keys = frozenset(
                    session_key(session)
                    for bucket in self.sessions.values()
                    for session in bucket
                )
                now = time.time()
                last_at = getattr(self, "_last_snapshot_at", 0.0)
                if (
                    keys == getattr(self, "_last_snapshot_keys", None)
                    and now - last_at < self._SNAPSHOT_MIN_INTERVAL_SECONDS
                ):
                    return
                payload = {
                    "version": self._SNAPSHOT_VERSION,
                    "order": list(self._order),
                    "sessions": {
                        rid: [dict(session) for session in bucket]
                        for rid, bucket in self.sessions.items()
                    },
                }
            path = self._snapshot_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)
            with self.lock:
                self._last_snapshot_at = now
                self._last_snapshot_keys = keys
        except Exception:  # noqa: BLE001 快照失败不影响扫描
            pass

    def hydrate_from_snapshot(self) -> bool:
        """启动时同步读快照立即填入会话列表（必须在 load 线程启动前调用）。

        填入的是上次退出前的列表：运行状态/标题可能滞后，后台真扫描完成时
        经现有原地更新/区段 splice 收敛。失败静默返回 False。调用方在
        `CORRAL_CACHE=0` 时不应调用。
        """
        import json

        if self.loaded or self.hydrated:
            return False
        try:
            path = self._snapshot_path()
            try:
                with open(path, encoding="utf-8") as f:
                    payload = json.load(f)
            except OSError:
                return False
            if not isinstance(payload, dict) or payload.get("version") != self._SNAPSHOT_VERSION:
                return False
            sessions = payload.get("sessions")
            order = payload.get("order")
            if not isinstance(sessions, dict) or not isinstance(order, list):
                return False
            with self.lock:
                known = set()
                for rid in self.sessions:
                    bucket = sessions.get(rid)
                    if not isinstance(bucket, list):
                        continue
                    clean = [dict(s) for s in bucket if isinstance(s, dict)]
                    self.sessions[rid] = clean
                    known.update(session_key(s) for s in clean)
                # 只保留快照顺序里仍存在的键；未知运行时的桶忽略
                self._order = [key for key in order if key in known]
                self._rebuild_order_and_titles()
                self.hydrated = True
            return True
        except Exception:  # noqa: BLE001 快照只是加速，坏了就当没有
            return False

    @staticmethod
    def _cache_file_mtime() -> float:
        try:
            return os.path.getmtime(titles.CACHE_FILE)
        except OSError:
            return 0.0

    def _remembered_scan_ids(self) -> dict[str, set[str]]:
        """侧边栏置顶/分组成员，扫描 limit 不得把它们挤出列表。"""
        from corral.split_layout import remembered_ids_by_runtime

        try:
            return remembered_ids_by_runtime()
        except Exception:
            return {}

    def load(self) -> None:
        from corral import observe

        try:
            t0 = time.perf_counter()
            keep_ids = self._remembered_scan_ids()
            scanned = self.registry.scan_all(
                self.limit, keep_ids_by_runtime=keep_ids,
            )
            duration_ms = int((time.perf_counter() - t0) * 1000)
            session_count = sum(len(items) for items in scanned.values())
            shared = bool(getattr(self.registry, "last_scan_shared", False))
            observe.event(
                "scan_all",
                duration_ms=duration_ms,
                session_count=session_count,
                reason="load",
                shared_index=shared,
            )
            self._merge_scanned(scanned)
            self._last_full_merge_at = time.monotonic()
            if not shared:
                self._last_local_scan_at = self._last_full_merge_at
            self._save_sidebar_snapshot()
            with self.lock:
                self.load_error = None
                self.last_refresh_at = time.time()
        except Exception as exc:
            # main() 在裸后台线程里调用 load()；异常不能让线程直接退出、让 UI
            # 永远等不到完成事件。保留中文错误给页头展示，后台 refresh 仍会继续
            # 尝试并在成功后自动清除。
            with self.lock:
                from corral.i18n import t

                self.load_error = t("store.load_failed", error=exc)
        finally:
            with self.lock:
                self.loaded = True
            self._load_event.set()

    def wait_loaded(self, timeout: float | None = None) -> bool:
        """阻塞等待 load() 完成一次；已完成时立即返回。

        供 UI 侧的加载 worker 使用：main() 可能已经在后台线程里抢先跑了 load()
        （与探测终端 OSC 颜色并行），worker 不需要再重复扫一遍磁盘，只要等那次
        跑完即可。返回值语义与 threading.Event.wait 一致（超时未完成返回 False）。
        """
        return self._load_event.wait(timeout)

    def get_load_error(self) -> str | None:
        """线程安全读取最近一次加载/刷新错误，供界面页头展示。"""
        with self.lock:
            return self.load_error

    # 全量合并最短间隔：其间若各运行时签名全命中，只刷新存活标注与关注圆点。
    _FULL_MERGE_INTERVAL = 12.0

    def refresh(self) -> bool:
        """后台周期性重扫磁盘，把新增/结束的会话并入当前列表。

        与 load() 共用合并逻辑，唯一区别是返回「会话集合是否真的变了」，
        供调用方只在有变化时才 dirty.set()，避免主循环无谓重定位光标。

        当上一轮完整合并仍在 ``_FULL_MERGE_INTERVAL`` 内且本轮扫描全部命中
        签名缓存时，走轻量合并：不整表替换会话字典，只更新托管标注与关注态。
        这样 Cursor 流式写 WAL 不会每 3 秒付一次完整列表重建代价；新会话 /
        进程启停仍由签名未命中或周期性全量合并兜住。

        跨进程共享索引（``scan_index``）在间隔内可代替本机磁盘扫描；到
        ``_FULL_MERGE_INTERVAL`` 仍强制本地扫一轮，避免新会话永远等不到。
        """
        from corral import observe

        try:
            t0 = time.perf_counter()
            now = time.monotonic()
            force_local = (
                self._last_local_scan_at is None
                or (now - self._last_local_scan_at) >= self._FULL_MERGE_INTERVAL
            )
            keep_ids = self._remembered_scan_ids()
            scanned = self.registry.scan_all(
                self.limit,
                keep_ids_by_runtime=keep_ids,
                prefer_shared=not force_local,
            )
            duration_ms = int((time.perf_counter() - t0) * 1000)
            session_count = sum(len(items) for items in scanned.values())
            cache_hit = bool(getattr(self.registry, "last_scan_cache_hit_all", False))
            shared = bool(getattr(self.registry, "last_scan_shared", False))
            if not shared:
                self._last_local_scan_at = now
            use_live = (
                cache_hit
                and self._last_full_merge_at is not None
                and (now - self._last_full_merge_at) < self._FULL_MERGE_INTERVAL
                # Optimistic delete empties memory then clears the tombstone so the
                # next refresh must re-merge from scan. Live merge ignores ``scanned``
                # and would leave the sidebar empty forever.
                and self._memory_keys_match_scan(scanned)
            )
            observe.event(
                "scan_all",
                duration_ms=duration_ms,
                session_count=session_count,
                reason="refresh_live" if use_live else "refresh",
                cache_hit=cache_hit,
                shared_index=shared,
            )
            before = self._sessions_signature()
            if use_live:
                self._merge_live_state()
            else:
                self._merge_scanned(scanned)
                self._last_full_merge_at = now
            changed = self._sessions_signature() != before
            if changed:
                self._save_sidebar_snapshot()
        except Exception as exc:
            with self.lock:
                from corral.i18n import t

                self.load_error = t("store.refresh_failed", error=exc)
            raise
        with self.lock:
            self.load_error = None
            self.last_refresh_at = time.time()
        return changed

    def refresh_age_seconds(self) -> float | None:
        """Seconds since the last successful load/refresh, or None if never."""
        with self.lock:
            at = self.last_refresh_at
        if at is None:
            return None
        return max(0.0, time.time() - at)

    def _memory_keys_match_scan(self, scanned: dict[str, list[dict]]) -> bool:
        """True when in-memory session keys match this scan result (ignoring order).

        Hosted placeholders are extra on purpose until the next full merge
        retires them. Counting them here made live-merge unreachable whenever
        any pane was still provisional (phone / other-window hosts), so every
        3s paid a whole-table replace. Optimistic delete still fails this
        check because memory is empty.
        """
        scan_keys = {
            session_key(session)
            for bucket in scanned.values()
            for session in bucket
        }
        with self.lock:
            memory_keys = {
                session_key(session)
                for bucket in self.sessions.values()
                for session in bucket
                if not session.get("provisional")
            }
        return memory_keys == scan_keys

    def _merge_live_state(self) -> None:
        """签名全命中时的轻量合并：就地刷新托管标注与关注圆点，不换会话集合。"""
        with self.lock:
            sessions = [session for bucket in self.sessions.values() for session in bucket]
            for provisional in self._provisional.values():
                sessions.append(provisional)
        if not sessions:
            with liveness.tmux_list_wave():
                self._adopt_foreign_hosted([])
            return
        with liveness.tmux_list_wave():
            liveness.annotate(sessions)
            self._adopt_foreign_hosted(sessions)
        with self.lock:
            attention_sessions = [
                session for bucket in self.sessions.values() for session in bucket
            ]
        states = self._reconcile_attention(attention_sessions)
        self._inject_attention_states(states)

    def _sessions_signature(self) -> tuple:
        """判定「会话集合是否真的变了」的签名，只应纳入值变化后必须触发列表
        重建的字段。

        `live`/`keepalive_name` 必须在内——否则「运行中→已结束」状态翻转和
        托管标注出现/消失时，会话键集合本身没变，`refresh()` 判定"没变化"、
        `dirty` 不会 set，`SessionCard` 手上还是上一次合并时的旧 dict 引用，
        状态列和运行中标注会一直冻结在首次展示时的取值，直到某个真正的新增/
        结束会话顺带带动一次 rebuild（真实 bug：长时间开着 corral 盯一个正在
        跑的会话，看到的"运行中"字样可能已经过期很久）。

        列表已经支持按会话键原地更新卡片，因此 mtime、标题来源和详情摘要也要
        纳入签名；否则扫描拿到了新内容，refresh() 却会误判“没有变化”，卡片的
        相对时间和右栏最近问答会一直停在旧值。
        """
        with self.lock:
            return tuple(
                (
                    runtime_id,
                    tuple(
                        (
                            session_key(session),
                            bool(session.get("live")),
                            session.get("keepalive_name"),
                            session.get("mtime"),
                            session.get("cwd"),
                            session.get("cwd_display"),
                            session.get("native_title"),
                            session.get("fallback_title"),
                            self.display_titles.get(session_key(session)),
                            session.get("first_user_msg"),
                            session.get("last_user_msg"),
                            session.get("last_agent_msg"),
                            session.get("attention_kind"),
                            session.get("attention_token"),
                            session.get("attention_updated_at"),
                        )
                        for session in bucket
                    ),
                )
                for runtime_id, bucket in sorted(self.sessions.items())
            )

    @staticmethod
    def _cursor_attention_signature(session: dict) -> tuple:
        """只用 stat 构造 Cursor 状态探测签名，不打开正文数据库。"""
        path = str(session.get("path") or "")
        if os.path.isdir(path):
            chat_dir = path
            store_path = os.path.join(path, "store.db")
        else:
            store_path = path
            chat_dir = os.path.dirname(path)
        candidates = (
            store_path,
            f"{store_path}-wal" if store_path else "",
            os.path.join(chat_dir, "prompt_history.json") if chat_dir else "",
        )
        signature = []
        for candidate in candidates:
            if not candidate:
                signature.append(("", None, None))
                continue
            try:
                info = os.stat(candidate)
                signature.append((os.path.basename(candidate), info.st_size, info.st_mtime_ns))
            except OSError:
                signature.append((os.path.basename(candidate), None, None))
        return tuple(signature)

    def _reconcile_attention(self, sessions: list[dict]) -> dict[str, AttentionState]:
        """在仓库锁外提取证据并持久化，再由调用方把结果注入展示字典。"""
        with self._attention_lock:
            prepared: list[tuple[dict, tuple]] = []
            current_cursor_signatures: dict[str, tuple] = {}
            pi_phases: dict[str, dict[str, str]] = {}
            if any(session.get("source") == "pi" for session in sessions):
                try:
                    from corral.pi_identity import live_agent_phases

                    pi_phases = live_agent_phases()
                except Exception:
                    pi_phases = {}
            for session in sessions:
                candidate = dict(session)
                key = session_key(candidate)
                base_signature = (
                    candidate.get("source"),
                    candidate.get("path"),
                    candidate.get("mtime"),
                    candidate.get("size_bytes"),
                    bool(candidate.get("live")),
                )
                if candidate.get("source") == "cursor":
                    cursor_signature = self._cursor_attention_signature(candidate)
                    first_seen = key not in self._cursor_attention_signatures
                    changed = (
                        not first_seen
                        and self._cursor_attention_signatures.get(key) != cursor_signature
                    )
                    current_cursor_signatures[key] = cursor_signature
                    if candidate.get("live") or changed:
                        candidate["signal_probe"] = True
                    else:
                        candidate.pop("signal_probe", None)
                    evidence_signature = base_signature + (cursor_signature,)
                elif candidate.get("source") == "pi":
                    phase_info = pi_phases.get(str(candidate.get("id") or ""))
                    if phase_info:
                        candidate["agent_phase"] = phase_info.get("phase")
                        candidate["agent_phase_event"] = phase_info.get("event")
                        candidate["agent_phase_at"] = phase_info.get("at")
                    else:
                        candidate.pop("agent_phase", None)
                        candidate.pop("agent_phase_event", None)
                        candidate.pop("agent_phase_at", None)
                    # Claim phase changes without jsonl mtime; must bust the cache.
                    evidence_signature = base_signature + (
                        candidate.get("agent_phase"),
                        candidate.get("agent_phase_at"),
                    )
                else:
                    evidence_signature = base_signature
                prepared.append((candidate, evidence_signature))
            self._cursor_attention_signatures = current_cursor_signatures

            evidence_by_key: dict[str, AttentionEvidence] = {}
            next_evidence_cache: dict[str, tuple[tuple, AttentionEvidence]] = {}
            for session, evidence_signature in prepared:
                key = session_key(session)
                cached = self._attention_evidence_cache.get(key)
                if cached is not None and cached[0] == evidence_signature:
                    evidence_by_key[key] = cached[1]
                    next_evidence_cache[key] = cached
                    continue
                try:
                    evidence = inspect_session(session)
                except Exception:
                    # 状态圆点是派生能力，任何运行时格式异常都不能阻断主扫描。
                    continue
                evidence_by_key[key] = evidence
                next_evidence_cache[key] = (evidence_signature, evidence)
            self._attention_evidence_cache = next_evidence_cache
            prepared_sessions = [session for session, _signature in prepared]
            try:
                return self.attention_store.reconcile(prepared_sessions, evidence_by_key)
            except Exception:
                return {
                    session_key(session): AttentionState()
                    for session in prepared_sessions
                }

    @staticmethod
    def _inject_attention(session: dict, state: AttentionState) -> None:
        if state.kind == "none":
            session.pop("attention_kind", None)
            session.pop("attention_token", None)
            session.pop("attention_updated_at", None)
            return
        session["attention_kind"] = state.kind
        session["attention_token"] = state.activity_token
        session["attention_updated_at"] = state.updated_at

    def _merge_scanned(self, scanned: dict[str, list[dict]]) -> None:
        """把一轮扫描结果并入内存状态：墓碑过滤 → 占位卡 reconcile →
        稳定排序与标题状态 → 关注态注入，四步严格按序执行。

        拆分只是把原本挤在一个方法里的六件事分给四个私有步骤，`with self.lock:`
        的进出位置逐字保持不变——`_reconcile_provisional_sessions()` 与
        `_rebuild_order_and_titles()` 默认调用方已持锁，不会再自行加锁
        （`threading.Lock` 不可重入，二次获取会直接死锁）。
        """
        # 每个适配器负责按时间倒序返回，无需在界面层二次排序
        scanned = self._drop_tombstoned_sessions(scanned)
        annotated = [session for bucket in scanned.values() for session in bucket]
        with liveness.tmux_list_wave():
            liveness.annotate(annotated)
            # Remote (and any other process) hosts panes without sharing this store.
            # Adopt unmatched managed panes as provisional interactive cards so the
            # desktop sidebar does not wait for history / lag through a preview phase.
            self._adopt_foreign_hosted(annotated)

        with self.lock:
            attention_migrations = self._reconcile_provisional_sessions(scanned)
            attention_sessions = self._rebuild_order_and_titles()
            has_pending_titles = bool(self.generating)
            if has_pending_titles:
                if self._generating_since is None:
                    self._generating_since = time.time()
            else:
                self._generating_since = None

        # SQLite 与运行时历史探测均放在 SessionStore 锁外，避免界面读取被磁盘 I/O
        # 卡住。迁移必须先于真实会话 reconcile，确保占位状态无缝接到正式标识。
        self._migrate_provisional_attention(attention_migrations)
        states = self._reconcile_attention(attention_sessions)
        self._inject_attention_states(states)
        # 运行中新出现的待生成会话也要拉后台；只靠启动时那一次 spawn 会永久漏生成。
        if has_pending_titles:
            self.request_title_generation()

    def _adopt_foreign_hosted(self, annotated: list[dict]) -> None:
        """Register provisional cards for managed panes this process did not host.

        ``corral remote`` hosts into the shared keepalive socket but only calls
        ``register_hosted_session`` in the daemon. Until formal history exists,
        a separate TUI store sees nothing; once history appears, annotate may
        still miss the pane for a cycle or two (Cursor uuid ≠ 8-char ident), so
        the card first shows as a static preview then flips interactive. Adopting
        unmatched live panes closes both gaps.
        """
        from corral.i18n import t

        claimed: set[str] = set()
        for session in annotated:
            name = session.get("keepalive_name")
            if name:
                claimed.add(str(name))
        with self.lock:
            claimed.update(str(name) for name in self.hosted.values() if name)
            for provisional in self._provisional.values():
                name = provisional.get("keepalive_name")
                if name:
                    claimed.add(str(name))
            for bucket in self.sessions.values():
                for session in bucket:
                    name = session.get("keepalive_name")
                    if name:
                        claimed.add(str(name))
            known_runtimes = set(self.registry.ids)

        try:
            hosts = liveness.list_managed_hosts()
        except Exception:
            return
        for host in hosts:
            name = str(host.get("name") or "")
            runtime_id = str(host.get("runtime_id") or "")
            ident = str(host.get("ident") or "")
            if not name or not runtime_id or not ident:
                continue
            if name in claimed or runtime_id not in known_runtimes:
                continue
            if is_ephemeral_agent_cwd(str(host.get("cwd") or "")):
                continue  # automation / experiment pane; its sessions never list either
            key = f"{runtime_id}:{ident}"
            with self.lock:
                if key in self._provisional or key in self.hosted:
                    continue
            try:
                display = self.registry.get(runtime_id).display_name
            except (AttributeError, KeyError):
                # Test doubles and minimal registries may omit ``get``; never crash merge.
                display = runtime_id
            self.register_hosted_session(
                runtime_id=runtime_id,
                keepalive_name=name,
                title=t("session.title.new", name=display),
                cwd=host.get("cwd"),
                ident=ident,
            )
            claimed.add(name)

    def _drop_tombstoned_sessions(
        self, scanned: dict[str, list[dict]],
    ) -> dict[str, list[dict]]:
        """墓碑过滤：按 `x` 确认删除过的会话键滤掉本轮扫描结果，
        阻止已删会话被重新灌回列表（见 `_deleted` 的说明）。"""
        with self.lock:
            deleted = set(self._deleted)
        if not deleted:
            return scanned
        return {
            runtime_id: [
                session
                for session in bucket
                if session_key(session) not in deleted
            ]
            for runtime_id, bucket in scanned.items()
        }

    def _reconcile_provisional_sessions(
        self, scanned: dict[str, list[dict]],
    ) -> list[tuple[str, str, str]]:
        """占位卡 reconcile：调用方必须已持有 `self.lock`。

        把本轮扫描结果并入 `self.sessions`，判定跨运行时接力/空白新建插入的
        占位卡是否已被真实会话取代（退役）或托管进程已死（清理），仍存活且
        未被取代的占位卡继续插回列表最前。返回需要迁移关注态的
        (运行时, 旧占位 id, 新真实 id) 三元组列表，供调用方在锁外执行迁移。
        """
        self.sessions.update(scanned)
        claimed_keepalive = {
            str(session.get("keepalive_name")): session
            for bucket in self.sessions.values()
            for session in bucket
            if session.get("keepalive_name") and not session.get("provisional")
        }
        attention_migrations: list[tuple[str, str, str]] = []
        for key, provisional in list(self._provisional.items()):
            name = self.hosted.get(key) or provisional.get("keepalive_name")
            if name and name in claimed_keepalive:
                # 真实会话已挂上同一托管名：占位卡退役，避免双卡。
                real_session = claimed_keepalive[str(name)]
                self._retire_provisional(key, provisional, real_session, name, attention_migrations)
                continue
            if not name:
                self._provisional.pop(key, None)
                self.hosted.pop(key, None)
                continue
            if key not in self.hosted and not liveness.is_alive(str(name)):
                # Only drop a placeholder when this window is no longer hosting
                # it *and* tmux confirmed the pane is gone. A has-session
                # timeout while the user is looking at another session must
                # not turn a just-opened live session into Ended preview.
                self._provisional.pop(key, None)
                self.hosted.pop(key, None)
                continue
            runtime_id = str(provisional.get("source") or "")
            bucket = self.sessions.setdefault(runtime_id, [])
            if any(session_key(session) == key for session in bucket):
                # 落盘 id 已与占位 ident 相同（Pi `--session-id`）：占位完成使命。
                self._provisional.pop(key, None)
                continue
            claimed = self._claim_unique_hosted_newcomer(key, provisional)
            if claimed is not None:
                self._retire_provisional(key, provisional, claimed, name, attention_migrations)
                continue
            provisional["keepalive_name"] = name
            provisional["live"] = True
            # 与 register_hosted_session 相同：不要原地改扫描器可能复用的 list。
            self.sessions[runtime_id] = [provisional, *[
                item for item in bucket if session_key(item) != key
            ]]
        return attention_migrations

    def _retire_provisional(
        self,
        key: str,
        provisional: dict,
        real_session: dict,
        name: str,
        attention_migrations: list[tuple[str, str, str]],
    ) -> None:
        """占位卡被真实会话取代：迁关注态、把托管记录改挂到新键。"""
        runtime_id = str(provisional.get("source") or "")
        real_runtime_id = str(real_session.get("source") or "")
        if runtime_id and runtime_id == real_runtime_id:
            attention_migrations.append(
                (
                    runtime_id,
                    str(provisional.get("id") or ""),
                    str(real_session.get("id") or ""),
                )
            )
        real_session["keepalive_name"] = name
        real_session["live"] = True
        new_key = session_key(real_session)
        self.hosted[new_key] = str(name)
        self._provisional.pop(key, None)
        if key != new_key:
            self._session_key_migrations[key] = new_key
            self.hosted.pop(key, None)

    def _claim_unique_hosted_newcomer(
        self, key: str, provisional: dict,
    ) -> dict | None:
        """真实 id 与占位 ident 不同、annotate 又没贴上 keepalive 时的兜底。

        优先按托管 ``corral-<ident>/`` 目录一对一认领（同 cwd 两个分屏不再放弃）。
        匹配不到时仅当「这个 cwd 里活着的占位卡恰好一张，且可认领的真实会话也
        恰好一条」才认领，避免无 path 的旧卡串台。

        可认领集合 = 本轮新出现的卡 ∪ 已在列表里、但 mtime 落在占位登记前后窗口
        内的未托管卡。后者盖住「正式历史比占位卡更早进列表」的竞态（Cursor 分屏
        新建尤甚）：旧逻辑只认 newcomers，正式卡已进 ``_order`` 后占位永远退不掉，
        侧栏会短暂甚至持续「组里一份 + 组外一份」。禁止用同目录任意旧卡兜底——
        mtime 必须够新，两条及以上仍放弃。
        """
        runtime_id = str(provisional.get("source") or "")
        if not runtime_id:
            return None
        if runtime_id == "codex":
            # Codex UUIDs are independent of pane id and cwd. Wait for an
            # exact claim rather than converting a nearby history card.
            return None
        cwd = normalize_cwd(provisional.get("cwd"))
        known = set(self._order)
        bucket = self.sessions.get(runtime_id) or []
        now = time.time()
        prov_mtime = float(provisional.get("mtime") or now)
        # 占位登记前几秒落盘的正式卡仍算「这次新建」；再早的同目录历史一律不碰。
        # 墙上时钟上限防止占位卡挂很久后误认领被 touch 的旧会话。
        fresh_floor = max(prov_mtime - 5.0, now - 600.0)

        def _is_claim_candidate(session: dict) -> bool:
            sk = session_key(session)
            if session.get("provisional") or sk == key:
                return False
            if sk in self.hosted or session.get("keepalive_name"):
                return False
            if normalize_cwd(session.get("cwd")) != cwd:
                return False
            if sk not in known:
                return True
            return float(session.get("mtime") or 0) >= fresh_floor

        candidates = [session for session in bucket if _is_claim_candidate(session)]
        if runtime_id == "pi":
            from corral.runtime.host_extension import hosted_session_dir
            from corral.scan.pi import normalize_session_dir, session_file_dir

            ident = str(provisional.get("id") or "")
            expected_dir = (
                normalize_session_dir(hosted_session_dir(cwd, ident))
                if ident and cwd else ""
            )
            if expected_dir:
                matches = [
                    session
                    for session in candidates
                    if session_file_dir(str(session.get("path") or "")) == expected_dir
                ]
                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    return max(
                        matches,
                        key=lambda item: float(
                            item.get("file_mtime") or item.get("mtime") or 0
                        ),
                    )
        sibling_provisionals = 0
        for other_key, other in self._provisional.items():
            if other_key == key:
                continue
            if str(other.get("source") or "") != runtime_id:
                continue
            if normalize_cwd(other.get("cwd")) != cwd:
                continue
            other_name = self.hosted.get(other_key) or other.get("keepalive_name")
            if other_name and liveness.is_alive(str(other_name)):
                sibling_provisionals += 1
        if len(candidates) == 1 and sibling_provisionals == 0:
            return candidates[0]
        return None

    def _rebuild_order_and_titles(self) -> list[dict]:
        """稳定排序与标题状态：调用方必须已持有 `self.lock`。

        重建跨运行时展示顺序（已展示的会话保持原位，新出现的按新鲜度分组
        插入）、清理已消失会话的生成中标记、解析每条会话的展示标题。返回
        本轮全部会话的快照副本，供调用方在锁外提取关注态证据。
        """
        by_key: dict[str, dict] = {}
        for bucket in self.sessions.values():
            for session in bucket:
                by_key[session_key(session)] = session
        # 稳定顺序：已展示的会话保持原位（只更新内容，不移动）。
        # 新出现的会话：最近活跃的插到最前；「目录复活」等重新扫到的旧会话
        # 追加到末尾——避免 /tmp 临时 cwd 重建时几天前的会话整批顶到侧边栏。
        known = set(self._order)
        fresh = [session for key, session in by_key.items() if key not in known]
        now = time.time()
        fresh_hot = [
            session for session in fresh
            if now - float(session.get("mtime") or 0) <= _FRESH_PREPEND_MAX_AGE
        ]
        fresh_cold = [
            session for session in fresh
            if now - float(session.get("mtime") or 0) > _FRESH_PREPEND_MAX_AGE
        ]
        fresh_hot.sort(key=lambda session: float(session.get("mtime") or 0), reverse=True)
        fresh_cold.sort(key=lambda session: float(session.get("mtime") or 0), reverse=True)
        self._order = (
            [session_key(session) for session in fresh_hot]
            + [key for key in self._order if key in by_key]
            + [session_key(session) for session in fresh_cold]
        )
        # 已从扫描结果消失的会话不能继续占着生成状态，否则标题生成队列会
        # 永久挂着不存在的会话键。
        self.generating.intersection_update(by_key)
        for session in by_key.values():
            key = session_key(session)
            # 用户刚结束的会话：进程可能还没退出，扫描仍报 live；强制已结束展示，
            # 直到某次扫描确认 live=False 再解除（见 mark_hosted 清除分支）。
            if key in self._force_ended:
                if session.get("live"):
                    session["live"] = False
                    session["pid"] = None
                    session.pop("keepalive_name", None)
                else:
                    self._force_ended.discard(key)
            # annotate 没匹配上时，用本进程的内嵌托管记录兜底（见 __init__ 注释）。
            # 本窗口仍登记 hosted 时一律回填，禁止拿单次 is_alive 假阴性把还在
            # 跑的会话降成已结束预览（切走再切回最容易踩：右栏 remount 后
            # has-session 超时会被当成死亡）。真正结束只走 mark_hosted(None)
            # 或占位卡在「本窗口已不托管且 pane 确认消失」时退役。
            hosted_name = self.hosted.get(key)
            if hosted_name:
                session["keepalive_name"] = hosted_name
            title, needs = titles.resolve_initial_title(session, self.cache)
            self.display_titles[key] = title
            # 生成状态必须以标题状态机返回的 needs 为唯一依据。低价值会话、
            # 已尝试失败的会话都可能没有模型标题，但它们不应继续转圈。
            if needs:
                self.generating.add(key)
            else:
                self.generating.discard(key)
        self._dedupe_keepalive_names(by_key)
        self._projects = None

        return [
            dict(session)
            for bucket in self.sessions.values()
            for session in bucket
        ]

    def _dedupe_keepalive_names(self, by_key: dict[str, dict]) -> None:
        """同一托管名只能挂一条会话，否则两个分屏会抓同一份 tmux 画面。

        扫描串台或 ``annotate`` 把父/子进程贴到两张卡时，两条会话会带上同一个
        ``keepalive_name``。布局层虽然拒绝按歧义名迁移分屏键，右栏仍会按这个
        名字各开一格内嵌终端。调用方必须已持有 ``self.lock``。
        """
        owners: dict[str, list[str]] = {}
        for key, session in by_key.items():
            name = session.get("keepalive_name")
            if not name:
                continue
            owners.setdefault(str(name), []).append(key)
        for name, keys in owners.items():
            if len(keys) < 2:
                continue
            winner = self._preferred_keepalive_owner(name, keys, by_key)
            for key in keys:
                if key == winner:
                    continue
                by_key[key].pop("keepalive_name", None)
                if self.hosted.get(key) == name:
                    self.hosted.pop(key, None)

    def _preferred_keepalive_owner(
        self, name: str, keys: list[str], by_key: dict[str, dict],
    ) -> str:
        hosted_hits = [key for key in keys if self.hosted.get(key) == name]
        if len(hosted_hits) == 1:
            return hosted_hits[0]
        ident_hits = [
            key for key in keys
            if _session_matches_keepalive_ident(by_key[key], name)
        ]
        if len(ident_hits) == 1:
            return ident_hits[0]
        real = [key for key in keys if not by_key[key].get("provisional")]
        if len(real) == 1:
            return real[0]
        return keys[0]

    def _migrate_provisional_attention(
        self, attention_migrations: list[tuple[str, str, str]],
    ) -> None:
        """把退役占位卡的关注态迁移到真实会话 id 上；必须在锁外调用
        （会触发 SQLite 写入），且必须先于 `_reconcile_attention`，确保占位
        状态无缝接到正式标识。"""
        for runtime_id, old_session_id, new_session_id in attention_migrations:
            if not old_session_id or not new_session_id:
                continue
            try:
                self.attention_store.migrate_session(
                    runtime_id, old_session_id, new_session_id,
                )
            except Exception:
                continue

    def _inject_attention_states(self, states: dict[str, AttentionState]) -> None:
        """关注态注入：把 `_reconcile_attention` 算出的结果写回当前会话快照。"""
        with self.lock:
            current_keys = {
                session_key(session)
                for bucket in self.sessions.values()
                for session in bucket
            }
            self.attention_states = {
                key: state for key, state in states.items() if key in current_keys
            }
            for bucket in self.sessions.values():
                for session in bucket:
                    self._inject_attention(
                        session,
                        self.attention_states.get(session_key(session), AttentionState()),
                    )

    def projects(self) -> list[dict]:
        """跨所有来源聚合的项目文件夹列表（新建会话 / 侧边栏用），惰性计算并缓存。

        合并会话 cwd 与本机 git 根扫描（见 corral.projects），字段形状仍是
        cwd_key / label / count / latest_mtime。
        """
        with self.lock:
            if self._projects is None:
                self._projects = project_entries(self.sessions)
            return self._projects

    def all_sessions(self) -> list[dict]:
        """返回稳定展示顺序的会话快照：已有位置固定；近 2 天的新会话在前，更旧的复活会话在后。"""
        with self.lock:
            by_key = {
                session_key(session): session
                for bucket in self.sessions.values()
                for session in bucket
            }
            ordered = [by_key[key] for key in self._order if key in by_key]
            if len(ordered) != len(by_key):
                # 兜底：_order 尚未覆盖的 key（如测试直接塞 sessions 未经合并），
                # 按 mtime 倒序排在最前，与「新会话置顶」语义一致。
                missing = [s for key, s in by_key.items() if key not in set(self._order)]
                missing.sort(key=lambda session: float(session.get("mtime") or 0), reverse=True)
                ordered = missing + ordered
            return ordered

    def session_key_migrations(self) -> dict[str, str]:
        """占位卡转正产生的旧键→新键，供分屏记忆在右栏已切走时仍能跟上。"""
        with self.lock:
            return dict(self._session_key_migrations)

    def find_session(self, key: str) -> dict | None:
        """按跨运行时会话键返回当前扫描快照中的会话对象。"""
        with self.lock:
            for bucket in self.sessions.values():
                for session in bucket:
                    if session_key(session) == key:
                        return session
        return None

    def canonical_session_key(self, key: str) -> str:
        """Follow placeholder→real identity hops recorded this process.

        Selection follow must use the live continuation, not the card the user
        originally opened, once that card has retired onto a new session id.
        """
        with self.lock:
            seen: set[str] = set()
            current = key
            while current in self._session_key_migrations and current not in seen:
                seen.add(current)
                current = self._session_key_migrations[current]
            return current

    def hosted_name_for(self, key: str) -> str | None:
        """Keepalive name this window is hosting for ``key``, after identity hops."""
        canonical = self.canonical_session_key(key)
        with self.lock:
            name = self.hosted.get(canonical) or self.hosted.get(key)
            if name:
                return str(name)
            for bucket in self.sessions.values():
                for session in bucket:
                    session_id = session_key(session)
                    if session_id in (canonical, key):
                        existing = session.get("keepalive_name")
                        if existing:
                            return str(existing)
        return None

    def attention_for(self, key: str) -> AttentionState:
        """读取当前内存快照中的关注状态，不在界面热路径访问磁盘。"""
        with self.lock:
            return self.attention_states.get(key, AttentionState())

    def mark_session_read(self, key: str) -> AttentionState:
        """把会话最新活动标为已读；执行中和等待回答阶段保持不变。"""
        runtime_id, separator, session_id = key.partition(":")
        if not separator or not runtime_id or not session_id:
            return AttentionState()
        try:
            state = self.attention_store.mark_read(runtime_id, session_id)
        except Exception:
            state = AttentionState()
        with self.lock:
            previous = self.attention_states.get(key)
            self.attention_states[key] = state
            for bucket in self.sessions.values():
                for session in bucket:
                    if session_key(session) == key:
                        self._inject_attention(session, state)
            if state != previous:
                self.dirty.set()
        return state

    def mark_deleted(self, key: str) -> None:
        """x 确认的瞬间摘除内存状态并打上 tombstone，卡片不等磁盘 delete 完成。

        tombstone 不随删除成功解除（见 `_deleted` 的注释）：会话键全局唯一、
        历史已抹，永远不该再出现；解除只发生在 `abort_delete()`。
        """
        with self.lock:
            self._deleted.add(key)
        self.remove_session(key)

    def abort_delete(self, key: str) -> None:
        """磁盘 delete_session 失败：解除 tombstone，交由 refresh() 从磁盘恢复。"""
        with self.lock:
            self._deleted.discard(key)

    def remove_session(self, key: str) -> None:
        """从当前内存状态里彻底摘除一条会话，供删除动作调用后立即消失。

        磁盘历史已被 `runtime.delete_session` 抹掉；这里只是让 UI 不必等下一轮
        `refresh()` 才发现它没了。清理范围覆盖 `_merge_scanned`/`mark_hosted`/
        `register_hosted_session` 会写入的每一处按 key 索引的结构，任何一处漏清都
        会让卡片残留或状态机不一致（如 `generating` 漏清会让转圈圈永远转下去）。
        """
        with self.lock:
            for bucket in self.sessions.values():
                bucket[:] = [session for session in bucket if session_key(session) != key]
            self._order = [k for k in self._order if k != key]
            self.display_titles.pop(key, None)
            self.generating.discard(key)
            self.conversations.pop(key, None)
            self.hosted.pop(key, None)
            self._provisional.pop(key, None)
            self._force_ended.discard(key)
            self.attention_states.pop(key, None)
            self._cursor_attention_signatures.pop(key, None)
            self._attention_evidence_cache.pop(key, None)
            self._projects = None
        runtime_id, separator, session_id = key.partition(":")
        if separator and runtime_id and session_id:
            try:
                self.attention_store.remove_session(runtime_id, session_id)
            except Exception:
                pass

    def register_hosted_session(
        self,
        *,
        runtime_id: str,
        keepalive_name: str,
        title: str,
        cwd: str | None,
        ident: str | None = None,
    ) -> dict:
        """跨运行时接力 / 空白新建：在扫描出真实历史前插入「运行中(托管)」占位卡。

        返回写入列表的会话 dict；调用方应用其会话键选中左栏并挂右栏画面。
        """
        from corral.i18n import t
        from corral.models import format_message_time
        from corral.scan.common import shorten_cwd

        session_id = ident or keepalive_name.rsplit("-", 1)[-1]
        now = time.time()
        cwd_text = str(cwd or "").strip()
        session = {
            "source": runtime_id,
            "id": session_id,
            "short_id": session_id.replace("-", "")[:12],
            "cwd": cwd_text,
            "cwd_display": shorten_cwd(cwd_text) if cwd_text else "",
            "mtime": now,
            "display_time": format_message_time(now),
            "time_source": "provisional",
            "event_time": now,
            "file_mtime": now,
            "size_bytes": 0,
            "size_kb": 0,
            "native_title": None,
            "fallback_title": title or t("session.title.new", name=runtime_id),
            "status_tag": titles.STATUS_PENDING,
            "live": True,
            "pid": None,
            "first_user_msg": "",
            "last_user_msg": "",
            "last_agent_msg": "",
            "path": "",
            "keepalive_name": keepalive_name,
            "provisional": True,
        }
        key = session_key(session)
        try:
            attention_state = self.attention_store.get(runtime_id, session_id)
        except Exception:
            attention_state = AttentionState()
        self._inject_attention(session, attention_state)
        with self.lock:
            self.hosted[key] = keepalive_name
            self._force_ended.discard(key)
            self._provisional[key] = session
            # 扫描器有时会把同一 list 对象再次 return；若直接在上面 insert，
            # 下一轮 scan 结果会被占位卡污染，same-id 分支误清占位却不退役。
            existing = [
                item
                for item in list(self.sessions.get(runtime_id) or [])
                if session_key(item) != key
            ]
            existing.insert(0, session)
            self.sessions[runtime_id] = existing
            self._order = [key] + [item for item in self._order if item != key]
            self.display_titles[key] = session["fallback_title"]
            self.generating.discard(key)
            self.attention_states[key] = attention_state
        return session

    def mark_hosted(self, key: str, name: str | None) -> dict | None:
        """原子登记/清除托管会话，并同步更新当前扫描快照中的展示字段。

        清除时（name=None）一并把 `live`/`pid` 置为已结束，并记入 `_force_ended`：
        用户按 q 杀掉托管后，若只清 `keepalive_name` 而留下上次扫描的 `live=True`，
        列表会先从「运行中(托管)」闪成「运行中」，再等后台重扫才变成「已结束」；
        进程尚未退出时下一轮扫描仍可能报 live，靠 `_force_ended` 压住直到确认已死。
        """
        with self.lock:
            if name:
                self.hosted[key] = name
                self._force_ended.discard(key)
            else:
                self.hosted.pop(key, None)
                self._provisional.pop(key, None)
                self._force_ended.add(key)
            for bucket in self.sessions.values():
                for session in bucket:
                    if session_key(session) != key:
                        continue
                    if name:
                        session["keepalive_name"] = name
                    else:
                        session.pop("keepalive_name", None)
                        session["live"] = False
                        session["pid"] = None
                    return session
        return None

    def request_title_generation(self) -> None:
        """有待生成标题时按防抖拉起后台进程。

        撞文件锁时后台进程立即退出，不会重复烧额度；这里只负责「发现待办就再喊一声」。
        """
        with self.lock:
            if not self.generating:
                return
            now = time.time()
            if now - self._last_title_spawn_at < _TITLE_SPAWN_DEBOUNCE_SECONDS:
                return
            self._last_title_spawn_at = now
            limit = self.limit
        self._invoke_title_spawn(limit)

    def _invoke_title_spawn(self, limit: int) -> None:
        """拉起后台标题进程；未注入 spawn 函数时跳过（单测默认不误起真实进程）。"""
        spawn = self._title_spawn_fn
        if spawn is None:
            return
        try:
            spawn(limit)
        except Exception:
            pass

    def poll_cache_updates(self) -> None:
        """缓存文件被后台生成进程更新时重读，把新标题刷到界面并停掉对应转圈圈。"""
        changed_keys = self.poll_title_updates()
        # The cache may contain an entry for a session that is not currently in
        # this store's window; poll_title_updates keeps the repository current.
        if not changed_keys:
            # 缓存没变：若仍有待生成且已空等过久，再拉一次后台（上一轮可能已退出）。
            with self.lock:
                stale = (
                    bool(self.generating)
                    and self._generating_since is not None
                    and (time.time() - self._generating_since) >= _TITLE_STALE_SPAWN_SECONDS
                )
            if stale:
                self.request_title_generation()
            return
        self.request_title_generation()

    def snapshot(self) -> dict[str, str]:
        """取「当前展示标题」快照供界面渲染；正在生成的会话只在标题落地后经
        poll_cache_updates 刷新，界面不再需要感知生成中状态。"""
        with self.lock:
            return dict(self.display_titles)

    @property
    def title_revision(self) -> int:
        return self.title_state.revision

    def poll_title_updates(self) -> set[str]:
        """Refresh title state without rescanning histories; returns changed keys."""
        with self.lock:
            sessions = [s for bucket in self.sessions.values() for s in bucket]
        # Keep the historical store-level probe as an injection point for
        # tests and explicit invalidation callers.
        if self._cache_file_mtime() != self._cache_mtime:
            self.title_state._mtime = object()
        changed = self.title_state.poll(sessions)
        self._cache_mtime = self.title_state._mtime
        self.cache = self.title_state.cache
        with self.lock:
            for bucket in self.sessions.values():
                for session in bucket:
                    key = session_key(session)
                    title, needs = titles.resolve_initial_title(session, self.cache)
                    old_title = self.display_titles.get(key)
                    was_generating = key in self.generating
                    self.display_titles[key] = title
                    if needs:
                        self.generating.add(key)
                    else:
                        self.generating.discard(key)
                    if old_title != title or was_generating != needs:
                        changed.add(key)
            if self.generating:
                if self._generating_since is None:
                    self._generating_since = time.time()
            else:
                self._generating_since = None
        if changed:
            self.dirty.set()
        return changed

    def get_title(self, session: dict) -> str:
        with self.lock:
            return self.display_titles.get(session_key(session), session["fallback_title"])

    @staticmethod
    def _conversation_version(session: dict) -> object | None:
        """内存对话缓存版本。

        - 单文件运行时（claude / codex / kimi / cursor）：主历史 + 可选 SQLite WAL，
          任一端变化即失效——文件变化就是本会话内容变化。
        - opencode：全部会话共用同一个 `opencode.db`，任何会话写入都会带动文件
          mtime，拿文件签名判失效会把别的会话的写入算到本会话头上，导致关闭预览
          频繁整体失效（表现为预览只出表头、正文一直空白）。这里改按会话自身的
          更新时间（扫描时从 db session 行带出的毫秒时间戳）判失效：本会话有新
          消息时时间戳推进、缓存自然失效；别的会话写入不影响本会话命中。
        """
        path = str(session.get("path") or "")
        if str(session.get("source") or "") == "opencode":
            mtime = session.get("mtime")
            if mtime is None:
                return None
            return ("opencode", int(round(mtime * 1000)))
        if not path:
            return None
        try:
            main = os.stat(path)
        except OSError:
            return None
        try:
            wal = os.stat(path + "-wal")
            return (main.st_mtime_ns, main.st_size, wal.st_mtime_ns, wal.st_size)
        except OSError:
            return (main.st_mtime_ns, main.st_size, None, None)

    def get_conversation(self, session: dict) -> list[ConversationMessage]:
        """按需读取并缓存选中会话的真实聊天记录；历史文件 mtime 变化（有新写入）时自动
        重读，供预览页关闭重开和停留期间的轮询刷新使用。"""
        if is_shell_session(session):
            # 终端 pane 没有助手历史，也不属于任何注册的运行时；右栏 HUD / 预览
            # 会把它们当普通会话轮询，这里直接给空对话，避免一路查到底层运行时。
            return []
        key = session_key(session)
        path = str(session.get("path") or "")
        version = self._conversation_version(session)
        with self.lock:
            cached = self.conversations.get(key)
            if cached is not None and cached[0] == version:
                return list(cached[1])
        runtime_id = str(session.get("source") or "")
        persistent = get_cache().get_conversation(runtime_id, key, path) if path else None
        if persistent is not None:
            with self.lock:
                self.conversations[key] = (version, list(persistent))
            return list(persistent)
        runtime = self.registry.get(runtime_id)
        messages = runtime.load_conversation(session)
        with self.lock:
            self.conversations[key] = (version, list(messages))
        # 还在写的会话不落盘：助手每写一次历史，签名就变一次、缓存必然失效，落盘
        # 只是白写。右栏小窗和"在别的窗口跑"的对话都会每隔几秒重读一次，真按 mtime
        # 落盘就变成几秒一次的整份 JSON 写库 + prune（缓存到上限后还要删行、
        # checkpoint）。内存缓存照常更新，会话结束后的第一次读取会补上落盘。
        if path and not session.get("live"):
            get_cache().put_conversation(runtime_id, key, path, list(messages))
        return messages

    def peek_conversation(
        self, session: dict, *, stale_ok: bool = False
    ) -> list[ConversationMessage] | None:
        """若缓存仍有效则返回对话副本，否则返回 None（不触发磁盘读取）。

        `stale_ok=True` 时，缓存有内容但版本已随历史写入失效的情况下返回旧副本——
        给详情预览这类「宁可短暂显示旧内容，也不要在会话活跃期反复闪「正在读取…」
        占位」的调用方用；关心新鲜度的调用方（如关注已读判定）保持严格模式。
        """
        key = session_key(session)
        version = self._conversation_version(session)
        with self.lock:
            cached = self.conversations.get(key)
            if cached is None:
                return None
            if cached[0] == version:
                return list(cached[1])
            if stale_ok:
                return list(cached[1])
        return None


def _new_session_cwd(store: SessionStore, nav, session: dict | None) -> str | None:
    """新建会话工作目录：搜索结果若恰好只剩一个项目则沿用，否则用所选会话目录。

    `nav` 需要有 `project_query` 属性（界面层的 `ui.nav.NavState`），这里不直接
    依赖 ui 包的具体类型，避免循环 import。
    """
    query = str(getattr(nav, "project_query", "") or "").strip()
    if query:
        titles_map = getattr(store, "display_titles", None) or {}
        visible = _filter_sessions_by_query(store.all_sessions(), query, titles=titles_map)
        keys = {normalize_cwd(s.get("cwd")) for s in visible}
        keys.discard("")
        if len(keys) == 1:
            return next(iter(keys))
    if session is not None:
        cwd_key = normalize_cwd(session.get("cwd"))
        return cwd_key or None
    return None
