# TASKBOARD — 多 Agent 并行协作看板

> 规则见 agentsync 全局 docs/AGENT_TASKBOARD_GUIDE.md。只编辑自己的条目；完成后删除。

| 任务 | 状态 | 影响范围 | 开始 | 最近更新 | 备注 |
|---|---|---|---|---|---|
| Stable adaptive quick filters | Implementing | TUI quick_filters; MacSidebarFilters and iOS SessionFilterRows; owning docs and acceptance | 2026-10-11 20:00 | 2026-10-11 20:00 | Owner: adaptive TUI packing, selection never reorders choices across all clients, no lingering underline |
| Split sidebar redesign | Docs then code | Mac sidebar split rows, split naming/rename removal (Mac, TUI card, layout.renameGroup), related docs | 2026-10-11 10:19 | 2026-10-11 10:19 | Owner request: no split names, rail + group-end separator; avoids remote delivery-error edits in progress |
| Quick filters + Active filter + New Session capsules | Verifying: needs Mac Debug app restart next (one GUI copy) | Mac/iPhone filter chips (MacSidebarFilters, SessionFilterRows), Active sessions board removal (Mac workspace/sidebar, TUI board), NewSessionSheet, TUI filter/new-session modal, related docs | 2026-10-11 10:30 | 2026-10-11 11:05 | Owner request; avoids split-row/rename code of Split sidebar redesign |
| Delivery feedback + send queue (host side) | Focused 255 tests + ruff pass; full gate red only from in-progress board removal (test_activity_board/reclaim_wiring/list.activity_board); release pending | remote/service.py input admission, remote/sessions.py confirm wording, embed.py exit wording, i18n remote.err.*/launch.exited, tests | 2026-10-11 10:12 | 2026-10-11 11:03 | Owner screenshot; no busy refusal; release CLI patch after gate |
