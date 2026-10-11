"""界面导航状态：项目搜索查询与默认运行时，跨 MainScreen/模态弹窗共享。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class NavState:
    source: str
    # 侧边栏顶部搜索框内容；空字符串 = 不过滤（全部项目/会话）
    project_query: str = ""
    # 侧栏「活跃」快捷筛选：只列画圆点的会话（与 project_query 取交集）；不持久化
    active_only: bool = False
    runtime_filters: set[str] = field(default_factory=set)
    project_filters: set[str] = field(default_factory=set)

    @property
    def narrowing(self) -> bool:
        return bool(self.project_query.strip() or self.active_only or self.runtime_filters or self.project_filters)
