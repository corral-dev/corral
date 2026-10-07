# TASKBOARD — 多 Agent 并行协作看板

> 规则见 agentsync 全局 docs/AGENT_TASKBOARD_GUIDE.md。只编辑自己的条目；完成后删除。

| 任务 | 状态 | 影响范围 | 开始 | 最近更新 | 备注 |
|---|---|---|---|---|---|
| Native full-text search latency | 验证中 | search.py, cache.py, remote/sessions.py, search/cache tests, PERFORMANCE_KNOWLEDGE_BASE.md; prepared cli .venv; Apple read-only acceptance | 13:19 | 2026-10-07 13:27 | Codex primary; 62 focused tests passed; persistent entries + host background refresh; full suite and native acceptance next |
| Conversation rebuilt sequence replacement | 进行中 | remote/richmsg.py rebuild markers, sessions.py conversation poll/publish only (no search), transcript_cache parser version, focused tests, REMOTE_KNOWLEDGE_BASE | 13:29 | 2026-10-07 13:29 | Primary; confirmed old assistant tail follows new user after reader rematerialization; no search changes |
| 开始/结束状态不再被扫描卡住（绿点与 Working 秒级跟随） | 进行中 | src/corral/store.py（关注缓存、合并交接、提交即在干活）、src/corral/remote/sessions.py（状态线程与扫描线程拆分）、src/corral/ui/main_screen.py（仅后台刷新 worker）、tests、docs/REMOTE_KNOWLEDGE_BASE.md、docs/TERMINAL_UI_KNOWLEDGE_BASE.md | 2026-10-07 14:00 | 2026-10-07 14:00 | |
