"""运行时注册、接力编排与最终进程替换。"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor

from corral.cache import scan_period
from corral.i18n import t
from corral.models import LaunchPlan, LaunchRequest, NewSessionRequest, SessionInfo
from corral.runtime.base import BaseRuntime, LaunchError, normalize_fork_title
from corral.runtime.claude import ClaudeRuntime
from corral.runtime.codex import CodexRuntime
from corral.runtime.cursor import CursorRuntime
from corral.runtime.kimi import KimiRuntime
from corral.runtime.opencode import OpenCodeRuntime
from corral.runtime.pi import PiRuntime


class RuntimeRegistry:
    """按注册顺序管理所有运行时，界面和接力逻辑只依赖本注册表。"""

    def __init__(self, runtimes: Iterable[BaseRuntime]):
        self._runtimes: dict[str, BaseRuntime] = {}
        for runtime in runtimes:
            if runtime.id in self._runtimes:
                raise ValueError(f"运行时重复注册：{runtime.id}")
            self._runtimes[runtime.id] = runtime
        if not self._runtimes:
            raise ValueError("至少需要注册一个运行时")
        # 廉价预检缓存：runtime.id -> (limit, scan_signature() 返回值) 与对应的上一次
        # 扫描结果，只有实现了 scan_signature()（非 None）的运行时才会命中，见
        # scan_all() 和 BaseRuntime.scan_signature 的文档。只应由同一调用方顺序调用
        # scan_all()（如 SessionStore 的后台重扫循环），不是线程安全的并发写结构——
        # 调用方需要自己保证同一 registry 实例不会被多个线程同时 scan_all()。
        self._scan_cache: dict[str, tuple[int, object]] = {}
        self._scan_cache_result: dict[str, list[SessionInfo]] = {}
        # 上一轮 scan_all 是否全部命中签名缓存（无实现签名的运行时不计）。
        # SessionStore 用它在「助手还在写 WAL」时走轻量合并，避免每 3 秒整表替换。
        self.last_scan_cache_hit_all = False
        # 上一轮是否来自跨进程共享扫描索引（见 scan_index）；与签名命中互斥。
        self.last_scan_shared = False

    def __iter__(self):
        return iter(self._runtimes.values())

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._runtimes)

    def get(self, runtime_id: str) -> BaseRuntime:
        try:
            return self._runtimes[runtime_id]
        except KeyError as exc:
            raise LaunchError(t("launch.unregistered", id=runtime_id)) from exc

    @property
    def launch_tokens(self) -> tuple[str, ...]:
        """所有能触发直启子命令的第一个词：运行时 id + 可执行文件名 + 别名。

        入口层（`cli.main`）只拿它做一次成员判断，判定"这是不是直启命令"；判定通过
        后再由 `resolve_id()` 展开成真正的运行时 id。分成两步是为了让入口探测保持
        纯粹的集合包含语义，不引入"某个函数返回真值就算命中"的隐式分支。
        """
        tokens: list[str] = []
        for runtime in self._runtimes.values():
            tokens.append(runtime.id)
            if runtime.executable != runtime.id:
                tokens.append(runtime.executable)
            tokens.extend(alias for alias in runtime.executable_aliases if alias not in tokens)
        return tuple(tokens)

    def resolve_id(self, token: str) -> str | None:
        """把用户敲的第一个词解析成运行时 id，认不出返回 None。

        依次认：运行时 id（`cursor`）、可执行文件名（`agent`）、适配器登记的别名
        （`cursor-agent`）。别名只在这一层展开，`get()` 与其余逻辑仍只认 id，避免
        运行时标识出现第二套事实来源。
        """
        if token in self._runtimes:
            return token
        for runtime in self._runtimes.values():
            if token == runtime.executable or token in runtime.executable_aliases:
                return runtime.id
        return None

    def scan_all(
        self,
        limit: int,
        keep_ids_by_runtime: dict[str, set[str]] | None = None,
        *,
        prefer_shared: bool = True,
    ) -> dict[str, list[SessionInfo]]:
        """并发扫描各运行时。各适配器只读各自独立的历史目录，互不干扰，
        用线程池重叠磁盘 I/O 等待时间即可，不需要多进程。

        单个运行时的扫描异常（如某条真实会话记录格式异常触发未预料的解析
        bug）被隔离在这里：该运行时降级为空列表，不拖垮其余运行时的结果，
        也不让 corral 首屏因为一条脏数据直接崩溃退出。

        实现了 `scan_signature()` 的运行时会先做一次廉价签名比对：签名和上一次
        调用相同就直接复用上一次的扫描结果，跳过完整的 `scan_sessions()`；返回
        `None` 的运行时不受影响。Claude/Codex/Cursor/Kimi/Pi 走逐文件 stat + pid
        快照，OpenCode 走库文件/WAL + 进程快照；禁止用祖先目录 mtime。

        ``keep_ids_by_runtime`` 把侧边栏置顶/分组成员的会话 id 交给各扫描器，
        避免 mtime 配额把仍被钉住的历史挤出列表。

        ``prefer_shared``：为真时先尝试跨进程共享扫描索引（见 ``scan_index``）。
        命中则跳过本机磁盘扫描；调用方仍应按自己的节奏强制本地全量（
        ``prefer_shared=False``）以免新会话永远等不到。
        """
        from corral import scan_index

        runtimes = list(self)
        keep_ids_by_runtime = keep_ids_by_runtime or {}
        self.last_scan_shared = False

        if prefer_shared:
            shared = scan_index.try_consume(limit, keep_ids_by_runtime)
            if shared is not None:
                # Fill missing runtime keys so callers see a complete map.
                result = {runtime.id: list(shared.get(runtime.id) or []) for runtime in runtimes}
                self.last_scan_cache_hit_all = True
                self.last_scan_shared = True
                return result

        def _copy_sessions(sessions: list[SessionInfo]) -> list[SessionInfo]:
            """缓存与调用方之间隔离可变会话字典，避免界面注入字段反向污染缓存。"""
            return [dict(session) for session in sessions]

        def _scan_one(runtime: BaseRuntime) -> tuple[list[SessionInfo], bool | None]:
            """返回 (sessions, cache_hit)；``None`` 表示该运行时无签名、不参与命中统计。"""
            keep_ids = keep_ids_by_runtime.get(runtime.id)
            try:
                signature = runtime.scan_signature()
            except Exception:
                signature = None
            if signature is not None:
                cache_key = (limit, signature, tuple(sorted(keep_ids or ())))
                if self._scan_cache.get(runtime.id) == cache_key:
                    return _copy_sessions(self._scan_cache_result.get(runtime.id, [])), True
            try:
                result = runtime.scan_sessions(limit, keep_ids=keep_ids)
            except Exception:
                # 瞬时读取失败不能把一份空结果写进新签名、覆盖最后一次成功缓存；
                # 有旧数据时继续展示旧快照，首次扫描就失败才降级为空列表。
                cached = self._scan_cache_result.get(runtime.id)
                return (
                    _copy_sessions(cached[:limit]) if cached is not None else [],
                    False if signature is not None else None,
                )
            if signature is not None:
                self._scan_cache[runtime.id] = (limit, signature, tuple(sorted(keep_ids or ())))
                # 保存一份、返回另一份：SessionStore/keepalive 会就地给调用方拿到的
                # dict 注入 keepalive_name 等展示状态，不能让这些字段进入扫描缓存。
                self._scan_cache_result[runtime.id] = _copy_sessions(result)
                return _copy_sessions(self._scan_cache_result[runtime.id]), False
            return result, None

        # 本轮扫描内每个运行时的元数据只查一次库，而不是每个候选文件查一次；
        # 快照必须随本轮扫描一起结束，否则后续扫描看不到新写入的会话。
        # 扫描期协议收敛在 cache.scan_period（见其 docstring），异常由它吞掉，
        # 派生缓存永远不能影响原始会话扫描结果。
        with scan_period():
            with ThreadPoolExecutor(max_workers=max(1, len(runtimes))) as pool:
                scanned = list(pool.map(_scan_one, runtimes))
                result = {
                    runtime.id: sessions
                    for runtime, (sessions, _hit) in zip(runtimes, scanned, strict=True)
                }
        hits = [hit for _sessions, hit in scanned if hit is not None]
        self.last_scan_cache_hit_all = bool(hits) and all(hits)
        scan_index.publish(
            result, limit=limit, keep_ids_by_runtime=keep_ids_by_runtime,
        )
        return result

    def build_launch_plan(self, request: LaunchRequest) -> LaunchPlan:
        source_id = str(request.session.get("source") or "")
        source = self.get(source_id)
        target = self.get(request.target_runtime_id)
        # 分叉会话：同助手官方分叉（磁盘克隆路径在 prepare_copy_request 里已换成新会话 + 原生恢复）。
        if request.copy_session:
            if source.id != target.id:
                raise LaunchError(t("launch.copy_same_assistant"))
            fork_plan = source.build_fork_plan(request.session)
            if fork_plan is None:
                raise LaunchError(t("launch.copy_no_fork", id=source.id))
            return fork_plan
        # 同助手且未强制新建 → 原生恢复；跨助手或 force_new → 导出接力后新建。
        if source.id == target.id and not request.force_new:
            return source.build_resume_plan(request.session)
        handoff = source.export_handoff(request.session, request.title)
        return target.build_new_plan(handoff)

    def prepare_copy_request(self, session: SessionInfo, title: str) -> LaunchRequest:
        """高级操作「分叉会话」：优先官方分叉，否则磁盘克隆后再原生恢复。"""
        source_id = str(session.get("source") or "")
        source = self.get(source_id)
        if not source.is_available():
            raise LaunchError(t("launch.copy_not_installed", name=source.display_name))
        if source.build_fork_plan(session) is not None:
            return LaunchRequest(
                session, source.id, title, copy_session=True,
            )
        cloned = source.clone_session(session)
        copy_title = normalize_fork_title(title)
        return LaunchRequest(cloned, source.id, copy_title)

    def build_new_session_plan(self, request: NewSessionRequest) -> LaunchPlan:
        """构造不关联任何已有会话历史的空白新会话计划。"""
        return self.get(request.target_runtime_id).build_new_session_plan(request.cwd)

    def build_passthrough_plan(self, runtime_id: str, user_args: Iterable[str]) -> LaunchPlan:
        """构造直启透传计划：`sc <runtime> [参数…]`，参数原样交给运行时，只垫上默认全自动放行参数。

        用户已经在 user_args 里显式带了该运行时的放行参数时不重复添加，尊重用户的显式选择。
        """
        runtime = self.get(runtime_id)
        return LaunchPlan(argv=runtime.compose_passthrough_argv(tuple(user_args)), cwd=None)


ACTIVE_RUNTIME_IDS: frozenset[str] = frozenset({"claude", "codex", "opencode", "cursor", "pi"})
"""活跃运行时 allowlist：仅这五个在 Corral 产品界面中可见/可新建。已有 Kimi
适配保留于注册表处于 dormant，仅供直接程序化兼容，不在 UI 展示。"""

DORMANT_RUNTIME_IDS: frozenset[str] = frozenset({"kimi"})
"""Dormant 会话来源：展示/投影层（侧栏、搜索、项目候选）仅排除这些来源。
注意不用 ACTIVE  allowlist 做会话过滤——shell 等非运行时来源（如终端窗格）
同样要展示；运行时创建入口（新建/接力菜单、顶栏 chips）才按 ACTIVE 过滤。"""


def default_registry() -> RuntimeRegistry:
    """创建默认运行时注册表；新增运行时只需在这里注册一次。

    完整注册仍包含 dormant 的 KimiRuntime 以保留兼容；界面与列表展示层按
    ACTIVE_RUNTIME_IDS 过滤，不在 UI 暴露入口。新增活跃运行时需用户显式决策
    后同时更新 ACTIVE_RUNTIME_IDS。
    """
    return RuntimeRegistry(
        (ClaudeRuntime(), CodexRuntime(), OpenCodeRuntime(), KimiRuntime(), CursorRuntime(), PiRuntime())
    )


def active_runtimes(registry: RuntimeRegistry) -> list:
    """返回 registry 中属于 ACTIVE_RUNTIME_IDS 的运行时，保持注册顺序。"""
    return [runtime for runtime in registry if runtime.id in ACTIVE_RUNTIME_IDS]


def execute_launch(plan: LaunchPlan) -> None:
    """校验启动计划并让目标运行时接管当前终端。"""
    executable = plan.argv[0]
    if shutil.which(executable) is None:
        raise LaunchError(t("launch.executable_missing", executable=executable))
    if plan.cwd:
        os.chdir(plan.cwd)
    try:
        # wrap_plan / attach_plan start with tmux; keep Python's LD_LIBRARY_PATH
        # out of that exec so system tmux does not load a mismatched libtinfo.
        if os.path.basename(executable) == "tmux":
            from corral.keepalive import tmux_env

            os.execvpe(executable, list(plan.argv), tmux_env())
        os.execvp(executable, list(plan.argv))
    except OSError as exc:
        raise LaunchError(t("launch.cannot_start", executable=executable, error=exc)) from exc
