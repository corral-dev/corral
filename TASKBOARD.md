# TASKBOARD — 多 Agent 并行协作看板

> 规则见 agentsync 全局 docs/AGENT_TASKBOARD_GUIDE.md。只编辑自己的条目；完成后删除。

| 任务 | 状态 | 影响范围 | 开始 | 最近更新 | 备注 |
|---|---|---|---|---|---|
| Mac 通知事件与手机完成推送验收 | 验证中 | remote/sessions.py（通知检测/事件）、tests/test_remote_push.py、test_ui（Pi exact-claim fixture）、test_session_scanning（非阻断耗时报告）、通知文档 | 17:10 | 2026-10-05 17:50 | 35 项通知回归通过；完整全树检查进行中 |
| 客户端状态延迟：活跃会话轻量状态快检（与全量扫描解耦，不受内存压力降速）、文件写入定向刷新、新会话即时上列表 | 进行中 | remote/sessions.py（仅 _refresh_loop 与新增快检线程，不动通知检测与 layout 段）、store.py（新增轻量状态刷新）、history_watch.py、ui/main_screen.py（刷新节奏）、tests/test_remote_state_latency.py、docs/REMOTE_KNOWLEDGE_BASE.md 踩坑表首行、docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md | 17:20 | 2026-10-05 17:20 | |
