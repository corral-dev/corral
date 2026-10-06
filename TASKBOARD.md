# TASKBOARD — 多 Agent 并行协作看板

> 规则见 agentsync 全局 docs/AGENT_TASKBOARD_GUIDE.md。只编辑自己的条目；完成后删除。

| 任务 | 状态 | 影响范围 | 开始 | 最近更新 | 备注 |
|---|---|---|---|---|---|
| Agent workflow optimization | 进行中 | scripts/agent_workflow.py, scripts/test_agent_workflow.py, docs/AGENT_WORKFLOW_GUIDE.md, AGENTS.md (local navigation), inherited product navigation | 11:41 | 2026-10-06 11:41 | Reuse existing environment, CI stamp and delivery; no product UI edits |
| 助手启动即退出时在格子里显示失败原因 | 进行中 | src/corral/embed.py、ui/embed_pane.py、ui/split_pane_area.py、i18n.py、tests（embed/ui）、docs/EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md、docs/TERMINAL_UI_KNOWLEDGE_BASE.md；可能读 remote 托管路径 | 11:42 | 2026-10-06 11:42 | |
| 共享终端高度取最高观看方，较矮方保底部 | 排队中 | src/corral/embed.py（仅 desired_host_size）、ui/embed_pane.py（仅画面纵向偏移/鼠标/光标换算）、tests、docs/EMBEDDED_TERMINAL_KNOWLEDGE_BASE.md | 11:55 | 2026-10-06 11:55 | 等「助手启动即退出」任务交出 embed.py / embed_pane.py 后再改 |
