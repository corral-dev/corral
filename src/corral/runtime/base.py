"""运行时适配器抽象。"""

from __future__ import annotations

import os
import shutil
from abc import ABC, abstractmethod

from corral.i18n import t
from corral.models import ConversationMessage, Handoff, LaunchPlan, SessionInfo


class LaunchError(RuntimeError):
    """启动计划无法安全执行。"""


# Fork title suffixes: the adopted Corral fork suffix plus legacy copy suffixes
# that must still be recognized. SessKit disk clones still stamp their own
# legacy suffix (currently spaceless `(copy)`); Corral normalizes new fork
# titles to the adopted suffix instead of preserving legacy wording verbatim.
_FORK_TITLE_SUFFIXES = ("（分叉）", " (fork)", "(fork)")
_LEGACY_COPY_TITLE_SUFFIXES = ("（副本）", " (copy)", "(copy)")


def normalize_fork_title(title: str | None) -> str:
    """Normalize a title to the adopted fork title.

    Strips any existing fork/copy suffixes, then appends the current suffix.
    Empty titles stay empty (matching the SessKit clone `if title` guard).
    Already-suffixed titles are unchanged; suffixes never stack.
    """
    base = str(title or "")
    if not base:
        return base
    stripped = base
    while True:
        for suffix in _FORK_TITLE_SUFFIXES + _LEGACY_COPY_TITLE_SUFFIXES:
            if stripped.endswith(suffix):
                stripped = stripped[: -len(suffix)]
                break
        else:
            break
    return f"{stripped}{t('session.title.copy_suffix')}"


def usable_cwd(cwd: str | None) -> str | None:
    """只返回当前机器真实存在的工作目录。"""
    return cwd if cwd and os.path.isdir(cwd) else None


_DIGEST_FIRST_LEN = 300  # 摘录里【原始需求】的截断长度
_DIGEST_MSG_LEN = 200  # 摘录里每条最近消息的截断长度
_DIGEST_RECENT_COUNT = 8  # 摘录保留的最近消息条数


def _clip(text: str | None, limit: int) -> str:
    """抽出接力任务后再压平换行并截断；摘录逐行列消息，多行原文会破坏行结构。"""
    from corral.runtime.host_extension import clip_for_digest

    peeled = clip_for_digest(text, limit=limit)
    flat = " ".join(peeled.split())
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "…"


class BaseRuntime(ABC):
    """每个命令行 Agent 运行时需要实现的最小能力集合。"""

    id: str
    display_name: str
    executable: str
    history_reading_hint: str
    auto_approve_args: tuple[str, ...] = ()  # 全自动放行参数（跳过权限审批），供直启子命令复用
    # 用户实际会敲、但不等于 id 也不等于 executable 的命令名。直启子命令按这张表把
    # `corral cursor-agent …` 解析到本运行时——用户记得住的是自己天天敲的命令名，
    # 不是 corral 内部的运行时 id。
    executable_aliases: tuple[str, ...] = ()

    def is_available(self) -> bool:
        return any(
            shutil.which(name) is not None
            for name in (self.executable, *self.executable_aliases)
        )

    def scan_signature(self) -> object | None:
        """返回一个廉价、可哈希的"本地历史是否可能有变化"签名，供 `RuntimeRegistry.scan_all`
        判断能否跳过一次完整的 `scan_sessions()`（后台重扫最重的开销）。

        只允许做目录/文件级别的元数据探测（`os.stat`/`os.listdir`），不能读文件内容；
        两次调用签名相等即视为"本地历史没有变化"，跳过扫描、复用上一次结果。返回
        `None` 表示该运行时没有可靠的廉价预检，调用方每次都必须完整扫描——这是安全
        默认值。禁止用祖先目录 mtime 当签名：Claude/Codex/Cursor 等多层历史下，既有
        文件追加不会冒泡到父目录。要跳过完整扫描，必须逐文件 ``stat``（路径/大小/
        mtime）并叠上进程 pid 快照，见各 ``scan.*.scan_signature``；OpenCode 的单文件
        SQLite 仍走库文件 + WAL + ``(pid, cwd)`` 快照。"""
        return None

    @abstractmethod
    def scan_sessions(self, limit: int, keep_ids: set[str] | None = None) -> list[SessionInfo]:
        """扫描并返回该运行时的本地会话。

        ``keep_ids`` 是侧边栏记忆里置顶/分组成员的会话 id，即使超过 ``limit``
        也必须保留在结果里，否则 pinned 区会凭空少卡。
        """

    def refresh_session(self, session: SessionInfo) -> SessionInfo | None:
        """Re-derive one listed session from its native history; None when unavailable.

        Same record the next ``scan_sessions`` would list for that history
        (status, completion identity, excerpts) without rescanning the runtime.
        Callers keep liveness and hosting annotations; see the SessKit contract.
        """
        from corral.runtime.sesskit_bridge import refresh_runtime_session

        return refresh_runtime_session({**session, "source": self.id})

    @abstractmethod
    def load_conversation(self, session: SessionInfo) -> list[ConversationMessage]:
        """按时间顺序读取用户消息和每轮最终答复。"""

    @abstractmethod
    def build_resume_plan(self, session: SessionInfo) -> LaunchPlan:
        """构造原运行时原生恢复计划。"""

    def build_continue_plan(self, session: SessionInfo, instruction: str) -> LaunchPlan:
        """构造携带新指令的非交互式原生续接计划，但不执行。

        保留默认实现，避免既有第三方适配器因新增可选能力无法实例化；调用方会把
        此异常转为结构化的“不支持续接计划”结果。
        """
        raise LaunchError(t("launch.no_continue", id=self.id))

    @abstractmethod
    def build_new_plan(self, handoff: Handoff) -> LaunchPlan:
        """构造读取其他运行时历史的新会话计划。"""

    @abstractmethod
    def build_new_session_plan(self, cwd: str | None) -> LaunchPlan:
        """构造不关联任何已有会话历史的空白新会话计划。"""

    def build_fork_plan(self, session: SessionInfo) -> LaunchPlan | None:
        """构造同助手「完整克隆历史」的原生分叉启动计划。

        返回 None 表示本运行时没有可用的官方分叉参数，调用方应改走
        `clone_session`（磁盘复制历史并换新身份）后再 `build_resume_plan`。
        默认不支持；有官方分叉的适配器覆写本方法。
        """
        return None

    def clone_session(self, session: SessionInfo) -> SessionInfo:
        """把会话历史复制为独立新会话（不改写原文件），返回新 SessionInfo。

        仅在没有官方分叉计划时作为回退。默认不支持。
        """
        raise LaunchError(t("launch.no_copy", id=self.id))

    def compose_passthrough_argv(self, user_args: tuple[str, ...]) -> tuple[str, ...]:
        """直启透传（`corral <运行时> [参数…]`）的完整 argv。

        默认把放行参数垫在最前；用户已显式带过就不重复。放行参数在某些运行时里
        只属于特定子命令、或对位置敏感（见 `runtime/opencode.py`），这类运行时
        覆写本方法，不要把这种私有规则写进注册表。
        """
        extra = tuple(arg for arg in self.auto_approve_args if arg not in user_args)
        return (self.executable, *extra, *user_args)

    def delete_session(self, session: SessionInfo) -> None:
        """彻底删除该会话在本地磁盘上的历史，不可恢复。

        保留默认实现（而非 abstractmethod），原因同 `build_continue_plan`：避免
        既有第三方适配器因新增可选能力无法实例化。默认直接报错，调用方（TUI）
        据此提示"该运行时尚未支持删除"。
        """
        raise LaunchError(t("launch.no_delete", id=self.id))

    def export_handoff(self, session: SessionInfo, title: str) -> Handoff:
        """把运行时私有会话导出为统一接力信息。"""
        raw_history_path = str(session.get("path") or "")
        if not raw_history_path:
            raise LaunchError(t("launch.no_history_path"))
        history_path = os.path.abspath(raw_history_path)
        if not os.path.isfile(history_path):
            raise LaunchError(t("launch.history_missing", path=history_path))
        return Handoff(
            source_runtime_id=self.id,
            source_runtime_name=self.display_name,
            title=title,
            history_path=history_path,
            original_cwd=str(session.get("cwd") or ""),
            history_reading_hint=self.history_reading_hint,
            conversation_digest=self._conversation_digest(session),
        )

    def _conversation_digest(self, session: SessionInfo) -> str:
        """构建接力提示词里的对话摘录；任何失败都降级为空串，不阻断接力。

        标题最长十几个字，作为任务说明极度有损；原始 JSONL 尾部又常是工具结果、
        系统注入事件等噪音，冷启动的目标 agent 首次解析容易定位错重点。这里用
        预览同款 load_conversation（已过滤系统事件、None 兜底）提取一段干净摘录
        做锚点。角色标"用户"而不是"你"——摘录是给接手的大模型看的，"你"会被
        误解为指它自己。
        """
        try:
            messages = self.load_conversation(session)
        except Exception:
            messages = []

        lines: list[str] = []
        if messages:
            recent = messages[-_DIGEST_RECENT_COUNT:]
            first_user = next((m for m in messages if m.role == "user"), None)
            if first_user is not None and first_user not in recent:
                lines.append(t("handoff.digest.first_need") + _clip(first_user.text, _DIGEST_FIRST_LEN))
            lines.append(t("handoff.digest.recent"))
            for message in recent:
                role = t("handoff.role.user") if message.role == "user" else t("handoff.role.assistant")
                lines.append(f"{role}: {_clip(message.text, _DIGEST_MSG_LEN)}")
            return "\n".join(lines)

        # 对话提取失败/为空时，回退扫描层已截好的首尾消息，尽量给出锚点。
        first = _clip(session.get("first_user_msg"), _DIGEST_FIRST_LEN)
        last_user = _clip(session.get("last_user_msg"), _DIGEST_MSG_LEN)
        last_agent = _clip(session.get("last_agent_msg"), _DIGEST_MSG_LEN)
        if first:
            lines.append(t("handoff.digest.first_need") + first)
        recent_lines = []
        if last_user and last_user != first:
            recent_lines.append(f"{t('handoff.role.user')}: {last_user}")
        if last_agent:
            recent_lines.append(f"{t('handoff.role.assistant')}: {last_agent}")
        if recent_lines:
            lines.append(t("handoff.digest.recent"))
            lines.extend(recent_lines)
        return "\n".join(lines)
