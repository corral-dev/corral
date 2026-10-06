# TASKBOARD — 多 Agent 并行协作看板

> 规则见 agentsync 全局 docs/AGENT_TASKBOARD_GUIDE.md。只编辑自己的条目；完成后删除。

| 任务 | 状态 | 影响范围 | 开始 | 最近更新 | 备注 |
|---|---|---|---|---|---|
| 共享终端高度取最高观看方，较矮方保底部 | 进行中 | src/corral/embed.py（仅 desired_host_size）、ui/embed_pane.py（仅画面纵向偏移/鼠标/光标换算）、tests、docs/EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md | 11:55 | 2026-10-06 11:55 | 等「助手启动即退出」任务交出 embed.py / embed_pane.py 后再改 |
| 结束状态秒级同步（完成通知/Working 根治） | 进行中 | src/corral/store.py（refresh_state 状态探针）、activity_board.py（recent 档）、runtime/*.py（refresh_session 透传）、runtime/sesskit_bridge.py、tests（store/activity/remote push）、pyproject/uv.lock sesskit pin、docs/REMOTE_KNOWLEDGE_BASE.md、docs/SESSION_SCANNING_KNOWLEDGE_BASE.md、docs/TERMINAL_UI_KNOWLEDGE_BASE.md | 12:05 | 2026-10-06 12:05 | 跨仓 SessKit 新增单会话重读；Apple 仅改 docs/UI_DESIGN_KNOWLEDGE_BASE.md |
