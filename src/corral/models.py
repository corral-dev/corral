"""跨运行时共享的数据模型。

会话列表项与对话消息的权威实现在 SessKit；本模块保留 Corral 产品类型
（接力 / 启动）并 re-export SessKit 会话类型以保持 ``from corral.models import …``。
"""

from __future__ import annotations

from dataclasses import dataclass

from sesskit.models import (  # noqa: F401 — re-export
    ConversationMessage,
    effective_session_time,
    format_message_time,
    make_session_info,
    parse_session_key,
    session_key,
)
from sesskit.models import (
    SessionInfo as _SessKitSessionInfo,
)

from corral.i18n import t

SHELL_RUNTIME_ID = "shell"


class SessionInfo(_SessKitSessionInfo, total=False):
    """SessKit session metadata plus Corral-owned hosting and attention state."""

    keepalive_name: str
    provisional: bool
    attention_kind: str
    attention_token: str | None
    attention_updated_at: float


def is_shell_session(session: SessionInfo | dict) -> bool:
    """内嵌自由 shell 分屏占位，不是 AI 助手会话。"""
    return str(session.get("source") or "") == SHELL_RUNTIME_ID


@dataclass(frozen=True)
class Handoff:
    """源运行时导出的统一接力信息。"""

    source_runtime_id: str
    source_runtime_name: str
    title: str
    history_path: str
    original_cwd: str
    history_reading_hint: str
    conversation_digest: str = ""

    def render_prompt(self) -> str:
        """生成目标运行时收到的首条用户提示词。"""
        cwd = self.original_cwd or t("handoff.cwd_unknown")
        sections = [
            t("handoff.task", title=self.title),
            t("handoff.intro", name=self.source_runtime_name),
            t(
                "handoff.history",
                path=self.history_path,
                cwd=cwd,
                hint=self.history_reading_hint,
            ),
        ]
        if self.conversation_digest:
            sections.append(t("handoff.digest_intro", digest=self.conversation_digest))
            sections.append(t("handoff.read_with_digest"))
        else:
            sections.append(t("handoff.read_without_digest"))
        sections.append(t("handoff.closing"))
        return "\n\n".join(sections)


@dataclass(frozen=True)
class LaunchRequest:
    """用户在界面中确认的启动选择。"""

    session: SessionInfo
    target_runtime_id: str
    title: str
    force_new: bool = False
    copy_session: bool = False


@dataclass(frozen=True)
class NewSessionRequest:
    """用户在界面中确认的“空白新会话”选择：不关联任何已有会话历史。"""

    target_runtime_id: str
    cwd: str


@dataclass(frozen=True)
class LaunchPlan:
    """可独立测试、最终交给操作系统执行的启动计划。"""

    argv: tuple[str, ...]
    cwd: str | None
