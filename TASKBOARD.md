# TASKBOARD — 多 Agent 并行协作看板

> 规则见 agentsync 全局 docs/AGENT_TASKBOARD_GUIDE.md。只编辑自己的条目；完成后删除。

| 任务 | 状态 | 影响范围 | 开始 | 最近更新 | 备注 |
|---|---|---|---|---|---|
| 开始/结束状态不再被扫描卡住（绿点与 Working 秒级跟随） | 进行中 | src/corral/store.py（关注缓存、合并交接、提交即在干活）、src/corral/remote/sessions.py（状态线程与扫描线程拆分）、src/corral/ui/main_screen.py（仅后台刷新 worker）、tests、docs/REMOTE_KNOWLEDGE_BASE.md、docs/TERMINAL_UI_KNOWLEDGE_BASE.md | 2026-10-07 14:00 | 2026-10-07 14:00 | |
| Historical sessions promoted by settings records | 进行中 | SessKit dependency pin; docs/SESSION_SCANNING_KNOWLEDGE_BASE.md; immutable release handoff; no store/remote/UI source edits | 17:27 | 2026-10-07 17:27 | Codex primary; true activity clock; prepared venvs; three-client read-only acceptance |
