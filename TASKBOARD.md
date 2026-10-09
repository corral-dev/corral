# TASKBOARD — 多 Agent 并行协作看板

> 规则见 agentsync 全局 docs/AGENT_TASKBOARD_GUIDE.md。只编辑自己的条目；完成后删除。

| 任务 | 状态 | 影响范围 | 开始 | 最近更新 | 备注 |
|---|---|---|---|---|---|
| Corral GitHub organization and open source | 进行中 | PUBLIC_PRESENTATION_GUIDE; titlegen private-endpoint removal and tests; publication/export tooling; GitHub remotes, install/release references and public metadata | 2026-10-09 | 2026-10-09 | Primary Codex; user authorized publication; history and content privacy audit before public push; no changes to scanner-migration owned spans |
| kh agent_sessions phase2 doc migration | 进行中 | docs/SESSION_SCANNING_KNOWLEDGE_BASE.md §2.2+§6 spans; docs/NEW_RUNTIME_ONBOARDING_KNOWLEDGE_BASE.md §6+§7-step8 spans; docs/MAINTAINER_GUIDE.md null/titles/is_alive/busycheck rationale spans only; no code/tests | 11:37 | 2026-10-09 12:45 | coordinator-ordered migration round 3; foreign rows untouched |
| kh agent_sessions finish correction | 进行中 | docs/MAINTAINER_GUIDE.md titles.save_cache/null/is_alive/busycheck pointer-clause English conversion only; no code/tests/version/UI; foreign CLI-help/project-discovery/titlegen spans untouched | 2026-10-09 18:03 | 2026-10-09 18:03 | one final bounded source correction; foreign rows preserved; primary owns integration/release |
| New-session privacy prompts | 验证中 | src/corral/projects.py; tests/test_projects.py; MAINTAINER_GUIDE project-discovery paragraph; version/release; cli.py two-line syntax integration repair | 17:31 | 2026-10-09 17:54 | v0.24.277 whole-workspace commit/final gate/install/release owned here; help + gateway changes included; no further source edits planned; TUI screenshot/real projects RPC passed; locked Mac and offline phone limits recorded |
| Complete CLI help | 验证中 | bootstrap/cli help parser; package/scan cache binding; i18n help; remote public help; agent runtime help; tests; MAINTAINER/PERFORMANCE help sections; release | 17:43 | 2026-10-09 17:51 | Primary Codex; help in cfc75cd/v277; 68 bilingual help routes + 5 assistant passthroughs + 6 regressions passed; 228 Python files compile clean; real scans for all 5 passed; current final gate/install integrated with privacy release; no more help source edits |
