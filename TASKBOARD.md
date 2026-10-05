# TASKBOARD — 多 Agent 并行协作看板

> 规则见 agentsync 全局 docs/AGENT_TASKBOARD_GUIDE.md。只编辑自己的条目；完成后删除。

| 任务 | 状态 | 影响范围 | 开始 | 最近更新 | 备注 |
|---|---|---|---|---|---|
| 本机/局域网连接零掉线：开发机发送有序、多实例共存、建通道限流、中继通道固定车道 | 进行中 | src/corral/remote/transport/（channel.py、local.py、relay.py）、service.py（attach 取代规则）、ratelimit.py、tests/test_remote_connection_stability.py、tests/test_remote_service.py、docs/REMOTE_KNOWLEDGE_BASE.md | 12:55 | 2026-10-05 13:10 | |
| Claude 运行中被误判已停（大图片记录挤出尾部窗口；只有工具调用不算运行） | 进行中 | attention_signals.py（仅 _read_jsonl_tail 与 _inspect_claude）、tests/test_claude_attention_tail.py、docs/SESSION_SCANNING_KNOWLEDGE_BASE.md 第 202 行 Claude 行 | 13:15 | 2026-10-05 13:15 | 与 Codex 提问条目同文件不同函数 |
| Mac 通知事件与手机完成推送验收 | 进行中 | remote/sessions.py（仅通知检测/事件）、tests/test_remote_push.py、REMOTE_KNOWLEDGE_BASE（通知节） | 17:10 | 2026-10-05 17:10 | 不改 transport/service；与 Apple 客户端同任务 |
| 分屏组改名：水果名不再显示，按成员标题拼名 + 可重命名（TUI 与 Mac 共用规则） | 进行中 | split_layout.py（显示名/重命名）、ui/session_list.py（组卡渲染与筛选）、ui/main_screen.py（Ctrl+T 重命名、删除确认）、ui/modals.py（新增输入框）、i18n.py、remote/protocol.py + service.py 方法表各加一行、remote/sessions.py（仅 layout_snapshot / 新增 layout_rename_group）、tests、docs/TERMINAL_UI_KNOWLEDGE_BASE.md 会话组节、REMOTE_KNOWLEDGE_BASE 第 174 行 | 17:12 | 2026-10-05 17:12 | 与通知条目同在 sessions.py：只动 layout 段 |
| 客户端状态延迟：活跃会话轻量状态快检（与全量扫描解耦，不受内存压力降速）、文件写入定向刷新、新会话即时上列表 | 进行中 | remote/sessions.py（仅 _refresh_loop 与新增快检线程，不动通知检测与 layout 段）、store.py（新增轻量状态刷新）、history_watch.py、ui/main_screen.py（刷新节奏）、tests/test_remote_state_latency.py、docs/REMOTE_KNOWLEDGE_BASE.md 踩坑表首行、docs/design/MOBILE_REMOTE_DATA_PLANE_DESIGN.md | 17:20 | 2026-10-05 17:20 | |
| 侧栏标题与预览内容不符：测试把假对话写进本机真实派生缓存（缓存单例导入即锁定路径） | 进行中 | src/corral/cache.py（仅 PerformanceCache 路径解析）、tests/test_cache.py、docs 测试隔离/缓存条目 | 17:35 | 2026-10-05 17:35 | 不动其它条目文件 |
