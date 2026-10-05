# TASKBOARD — 多 Agent 并行协作看板

> 规则见 agentsync 全局 docs/AGENT_TASKBOARD_GUIDE.md。只编辑自己的条目；完成后删除。

| 任务 | 状态 | 影响范围 | 开始 | 最近更新 | 备注 |
|---|---|---|---|---|---|
| 本机/局域网连接零掉线：开发机发送有序、多实例共存、建通道限流、中继通道固定车道 | 进行中 | src/corral/remote/transport/（channel.py、local.py、relay.py）、service.py（attach 取代规则）、ratelimit.py、tests/test_remote_connection_stability.py、tests/test_remote_service.py、docs/REMOTE_KNOWLEDGE_BASE.md | 12:55 | 2026-10-05 13:10 | |
| Claude 运行中被误判已停（大图片记录挤出尾部窗口；只有工具调用不算运行） | 进行中 | attention_signals.py（仅 _read_jsonl_tail 与 _inspect_claude）、tests/test_claude_attention_tail.py、docs/SESSION_SCANNING_KNOWLEDGE_BASE.md 第 202 行 Claude 行 | 13:15 | 2026-10-05 13:15 | 与 Codex 提问条目同文件不同函数 |
| Fix false Working after quota interruption | 验证中 | activity_board.py、tests/test_activity_board.py、sessions.py（仅 _detect_marker_changes）、tests/test_remote_sessions.py、docs/REMOTE_KNOWLEDGE_BASE.md、docs/TERMINAL_UI_KNOWLEDGE_BASE.md | 14:38 | 2026-10-05 14:37 | Ready checkout .venv; host marker only, no overlap with attention parser or Apple client work |
