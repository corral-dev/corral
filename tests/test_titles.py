from __future__ import annotations

import unittest
from unittest import mock

from corral import i18n, titles
from corral.models import Handoff

i18n.set_lang("en")


def _session(**overrides) -> dict:
    base = {
        "source": "cursor",
        "id": "sample",
        "mtime": 1,
        "size_bytes": 100,
        "size_kb": 0.1,
        "native_title": None,
        "fallback_title": None,
        "first_user_msg": None,
        "last_user_msg": None,
        "last_agent_msg": None,
    }
    base.update(overrides)
    return base


class TemporaryTitleRankingTests(unittest.TestCase):
    def test_clipped_attachment_fallback_yields_to_real_request_for_every_runtime(self) -> None:
        request = "/tmp/captures/screenshot.png 修复会话标题"
        for runtime in ("claude", "codex", "opencode", "cursor", "pi"):
            with self.subTest(runtime=runtime):
                session = _session(
                    source=runtime,
                    fallback_title="/tmp/captures/screenshot…",
                    first_user_msg=request,
                )
                title, needs = titles.resolve_initial_title(session, {})
                self.assertEqual(title, "修复会话标题")
                self.assertTrue(needs)

    def test_removed_references_cannot_become_punctuation_titles(self) -> None:
        for source in (
            "/tmp/captures/screenshot…",
            "@~/captures/screenshot...",
            "/tmp/one.png /tmp/two.png ⋯",
        ):
            with self.subTest(source=source):
                self.assertIsNone(titles._compact_title(source))
        self.assertEqual(titles._compact_title("截图：https://example.com/screenshot.png"), "截图")

    def test_punctuation_cache_is_repaired_but_valid_success_is_preserved(self) -> None:
        session = _session(first_user_msg="修复会话标题", fallback_title="/tmp/screenshot…")
        for punctuation in ("...", "…", "⋯", ". . .", "。。。", "---", "🖼️"):
            with self.subTest(punctuation=punctuation):
                cache = {"cursor:sample": {"title": punctuation}}
                self.assertEqual(titles.resolve_initial_title(session, cache), ("修复会话标题", True))
                self.assertFalse(titles.has_usable_cached_title(session, cache))
        cache = {"cursor:sample": {"title": "Fix session titles"}}
        self.assertEqual(titles.resolve_initial_title(session, cache), ("Fix session titles", False))

    def test_insult_prefix_does_not_beat_the_real_task(self) -> None:
        session = _session(
            native_title="Assemble annotated UI change requests",
            first_user_msg="怎么把标注请求组装成给 Agent 的说明",
            last_user_msg="你他妈的，全面排查无效提示词，把全局提示词里过时的入口删掉",
            fallback_title="怎么把标注请求组装成给 Agent 的说明",
        )

        title, needs = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "怎么把标注请求组装成给 Agent 的说明")
        self.assertNotEqual(title, "你他妈的")
        self.assertTrue(needs)

    def test_compact_title_keeps_substance_after_an_insult(self) -> None:
        self.assertEqual(
            titles._compact_title("你他妈的，全面排查无效提示词"),
            "全面排查无效提示词",
        )

    def test_short_real_request_is_not_dropped_as_an_emotion_clause(self) -> None:
        self.assertEqual(
            titles._compact_title("修复登录失败，不要改动其他功能"),
            "修复登录失败",
        )
        self.assertEqual(
            titles._compact_title("修复闪退，保留现有界面布局"),
            "修复闪退",
        )

    def test_generic_new_session_fallback_does_not_beat_inner_handoff_task(self) -> None:
        session = _session(
            source="codex",
            fallback_title="Task: New Codex session",
            first_user_msg="Task: 键盘未顶起输入框\n\n[Recent conversation]\nAssistant: 已复现",
        )
        title, needs = titles.resolve_initial_title(session, {})
        self.assertEqual(title, "键盘未顶起输入框")
        self.assertTrue(needs)

    def test_pure_insult_is_not_a_title_on_fallback_model_or_cache(self) -> None:
        session = _session(
            id="insult",
            first_user_msg="修复登录失败",
            fallback_title="修复登录失败",
            last_user_msg="你他妈的",
        )
        self.assertIsNone(titles._compact_title("你他妈的"))
        title, needs = titles.resolve_initial_title(session, {})
        self.assertEqual(title, "修复登录失败")
        self.assertNotEqual(title, "你他妈的")
        self.assertTrue(needs)

        cache = {"cursor:insult": {"fp": "v4:1", "title": "你他妈的"}}
        cached_title, cached_needs = titles.resolve_initial_title(session, cache)
        self.assertEqual(cached_title, "修复登录失败")
        self.assertTrue(cached_needs)

        written: dict = {}
        raw = {"cursor:insult": "你他妈的"}
        with (
            mock.patch.object(titles, "generate_titles_batch", return_value=raw),
            mock.patch.object(titles, "save_cache"),
        ):
            result = titles.refresh_titles([session], written, generator=mock.Mock())
        self.assertEqual(result, {})
        self.assertNotEqual(written["cursor:insult"].get("title"), "你他妈的")
        self.assertEqual(written["cursor:insult"]["generation_state"], "failed")

    def test_closing_doc_update_does_not_replace_the_original_task(self) -> None:
        session = _session(
            first_user_msg="修复 JotBox 退出后菜单栏图标还在",
            last_user_msg="/doc-update",
            fallback_title="修复 JotBox 退出后菜单栏图标还在",
        )

        title, needs = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "修复 JotBox 退出后菜单栏图标还在")
        self.assertNotIn(title, titles._command_label_values())
        self.assertTrue(needs)

    def test_one_line_handoff_wrapper_is_not_kept_as_title(self) -> None:
        blob = (
            "Task: 实现 You are picking up a session from Cursor. "
            "Start a new session of your own and continue the work"
        )
        session = _session(
            source="claude",
            first_user_msg=blob,
            fallback_title=blob,
            native_title="Fix Corral CI failures",
        )

        title, needs = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "Fix Corral CI failures")
        self.assertNotIn("You are picking up", title)
        self.assertTrue(needs)

    def test_cached_handoff_wrapper_title_is_ignored(self) -> None:
        session = _session(
            id="wrap",
            first_user_msg="修复 Corral 测试失败",
            fallback_title="修复 Corral 测试失败",
        )
        cache = {
            "cursor:wrap": {
                "fp": "v4:1",
                "title": "Task: 实现 You are picking up a session from Cursor. Start a n…",
            }
        }

        title, needs = titles.resolve_initial_title(session, cache)

        self.assertEqual(title, "修复 Corral 测试失败")
        self.assertTrue(needs)

    def test_handoff_task_wrapper_does_not_collapse_to_task(self) -> None:
        session = _session(
            source="codex",
            first_user_msg="Task: 修复 Corral 测试失败",
            last_user_msg="Task: 修复 Corral 测试失败",
            fallback_title="Task: 修复 Corral 测试失败",
        )

        title, needs = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "修复 Corral 测试失败")
        self.assertNotEqual(title, "Task")
        self.assertTrue(needs)

    def test_context_free_reply_loses_to_native_task_title(self) -> None:
        session = _session(
            native_title="Fix Corral CI failures",
            first_user_msg="实现",
            last_user_msg="实现",
            fallback_title="实现",
        )

        title, needs = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "Fix Corral CI failures")
        self.assertTrue(needs)

    def test_scanner_fallback_beats_raw_first_user_when_they_differ(self) -> None:
        session = _session(
            first_user_msg="旧首问",
            last_user_msg="另一条问题",
            fallback_title="旧标题",
            native_title="旧标题",
        )

        title, needs = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "旧标题")
        self.assertTrue(needs)

    def test_later_execute_reply_does_not_steal_the_first_request(self) -> None:
        session = _session(
            first_user_msg="恢复已安装的命令行工具并核对版本",
            last_user_msg="执行",
            fallback_title="恢复已安装的命令行工具并核对版本",
        )

        title, needs = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "恢复已安装的命令行工具并核对版本")
        self.assertTrue(needs)

    def test_cached_placeholder_success_is_ignored_when_task_exists(self) -> None:
        session = _session(
            id="empty-cache",
            fallback_title="整理应用图标多尺寸变体",
            first_user_msg="整理应用图标多尺寸变体",
        )
        cache = {"cursor:empty-cache": {"fp": "v4:1", "title": "空白会话"}}

        title, needs = titles.resolve_initial_title(session, cache)

        self.assertEqual(title, "整理应用图标多尺寸变体")
        self.assertTrue(needs)

    def test_cached_session_recap_is_ignored_when_task_exists(self) -> None:
        session = _session(
            id="recap",
            first_user_msg="给 Harbor 加回家离家自动化",
            last_user_msg="/doc-update",
            fallback_title="给 Harbor 加回家离家自动化",
        )
        cache = {"cursor:recap": {"fp": "v4:1", "title": "Session recap"}}

        title, needs = titles.resolve_initial_title(session, cache)

        self.assertEqual(title, "给 Harbor 加回家离家自动化")
        self.assertTrue(needs)

    def test_shortest_candidate_is_no_longer_preferred(self) -> None:
        session = _session(
            first_user_msg="修复支付回调重复扣款",
            last_user_msg="好了吗",
            fallback_title="修复支付回调重复扣款",
            native_title="pay",
        )

        title, _needs = titles.resolve_initial_title(session, {})

        self.assertEqual(title, "修复支付回调重复扣款")


class TitlePromptTests(unittest.TestCase):
    def test_prompt_keeps_marker_and_asks_for_insufficient_token(self) -> None:
        session = _session(
            first_user_msg="修复 JotBox 退出无效",
            last_user_msg="/doc-update",
            fallback_title="修复 JotBox 退出无效",
            native_title="jotbox-quit",
        )

        prompt = titles._build_batch_prompt([session])

        self.assertTrue(prompt.startswith(titles.PROMPT_MARKER))
        self.assertIn(titles.INSUFFICIENT_TITLE_TOKEN, prompt)
        self.assertIn("user_request", prompt)
        self.assertNotIn("preferred_title 是扫描器选出的最佳用户意图", prompt)
        self.assertIn("修复 JotBox 退出无效", prompt)
        self.assertNotIn("/doc-update", prompt)

    def test_handoff_prompt_uses_digest_instead_of_wrapper_prefix(self) -> None:
        handoff = Handoff(
            source_runtime_id="cursor",
            source_runtime_name="Cursor",
            title="实现",
            history_path="/tmp/history.jsonl",
            original_cwd="/tmp/proj",
            history_reading_hint="Codex rollout JSONL",
            conversation_digest="User: 修复 Corral 测试失败\nAssistant: 开始改相关用例",
        )
        session = _session(
            source="codex",
            id="handoff",
            first_user_msg=handoff.render_prompt(),
            last_user_msg=handoff.render_prompt(),
            fallback_title=handoff.render_prompt(),
        )

        item = titles._prompt_item(session)
        prompt = titles._build_batch_prompt([session])

        self.assertEqual(item["inherited_task"], "实现")
        self.assertIn("修复 Corral 测试失败", item["user_request"])
        self.assertNotIn("You are picking up a session from", item["user_request"])
        self.assertTrue(prompt.startswith(titles.PROMPT_MARKER))
        self.assertIn("修复 Corral 测试失败", prompt)

    def test_handoff_prompt_survives_scanner_300_char_clip(self) -> None:
        from corral.models import make_session_info
        from corral.runtime.host_extension import peel_handoff_text

        wrapper = (
            "Task: 实现\n\n"
            "You are picking up a session from Cursor. Start a new session of "
            "your own and continue the work. " + ("padding " * 40) + "\n\n"
            "Original session history file: /tmp/history.jsonl\n"
            "Original working directory: /tmp/proj\n"
            "History format hint: Codex rollout JSONL\n\n"
            "Below is a conversation excerpt automatically extracted from the "
            "original session (truncated; for quickly locating the task):\n"
            "User: 修复 Corral 测试失败\n"
            "Assistant: 开始改相关用例"
        )
        self.assertGreater(len(wrapper), 300)
        self.assertNotIn("修复 Corral 测试失败", wrapper[:300])

        # Scanners apply the host transform before records are built.
        peeled = peel_handoff_text(wrapper)
        scanned = make_session_info(
            source="codex",
            id="clip",
            short_id="clip",
            cwd="/tmp/proj",
            mtime=1.0,
            time_source="file_mtime",
            event_time=1.0,
            file_mtime=1.0,
            size_bytes=4000,
            native_title=None,
            fallback_title="实现",
            status_tag="",
            path="/tmp/clip.jsonl",
            first_user_msg=peeled,
            last_user_msg=peeled,
        )
        item = titles._prompt_item(scanned)

        self.assertIn("修复 Corral 测试失败", scanned["first_user_msg"])
        self.assertNotIn("You are picking up", scanned["first_user_msg"])
        self.assertIn("修复 Corral 测试失败", item["user_request"])
        self.assertNotEqual(item["user_request"], "实现")
        title = titles._compact_title(scanned["first_user_msg"])
        self.assertIn("修复", title or "")
        self.assertNotEqual(title, "User")
        self.assertNotEqual(title, "Below is a conversation ex…")
        self.assertNotIn("Below is a conversation", title or "")

    def test_nested_handoff_prompt_uses_inner_task(self) -> None:
        from corral.models import make_session_info
        from corral.runtime.host_extension import peel_handoff_text

        nested = (
            "Task: 实现\n\n"
            "You are picking up a session from Cursor. Start a new session of "
            "your own and continue the work. " + ("padding " * 20) + "\n\n"
            "Below is a conversation excerpt automatically extracted from the "
            "original session (truncated; for quickly locating the task):\n"
            "[Original request]Task: 排查并根治 Corral CI 错误 You are picking up a "
            "session from Codex. Start a new session of your own and continue "
            "the work; Original session history file: /tmp/inner.jsonl\n"
            "[Recent conversation]\n"
            "Assistant: 开始改相关用例"
        )
        peeled = peel_handoff_text(nested)
        scanned = make_session_info(
            source="claude",
            id="nested",
            short_id="nested",
            cwd="/tmp/proj",
            mtime=1.0,
            time_source="file_mtime",
            event_time=1.0,
            file_mtime=1.0,
            size_bytes=4000,
            native_title=None,
            fallback_title="实现",
            status_tag="",
            path="/tmp/nested.jsonl",
            first_user_msg=peeled,
        )
        item = titles._prompt_item(scanned)
        self.assertEqual(item["inherited_task"], "排查并根治 Corral CI 错误")
        self.assertNotIn("You are picking up", scanned["first_user_msg"])
        self.assertIn("排查并根治 Corral CI 错误", scanned["first_user_msg"])
        title = titles._compact_title(scanned["first_user_msg"])
        self.assertIn("排查", title or "")
        self.assertNotEqual(title, "实现")


class TitleGenerationStateTests(unittest.TestCase):
    def test_punctuation_model_output_records_invalid_and_keeps_readable_fallback(self) -> None:
        session = _session(first_user_msg="修复会话标题", fallback_title="/tmp/screenshot…")
        for punctuation in ("...", "…", "⋯", ". . .", "。。。", "---"):
            with self.subTest(punctuation=punctuation):
                generator = mock.Mock()
                generator.id = "fixture"
                generator.generate.return_value = '{"cursor:sample": "' + punctuation + '"}'
                cache: dict = {}
                with mock.patch.object(titles, "save_cache"):
                    self.assertEqual(titles.refresh_titles([session], cache, generator=generator), {})
                self.assertEqual(cache["cursor:sample"]["failure_reason"], "invalid")
                self.assertEqual(titles.resolve_initial_title(session, cache), ("修复会话标题", False))

    def test_insufficient_result_waits_for_new_content(self) -> None:
        session = _session(
            id="thin",
            size_bytes=10,
            fallback_title="在吗",
            first_user_msg="继续",
        )
        cache: dict = {}
        raw = {"cursor:thin": titles.INSUFFICIENT_TITLE_TOKEN}

        with (
            mock.patch.object(titles, "generate_titles_batch", return_value=raw),
            mock.patch.object(titles, "save_cache"),
        ):
            result = titles.refresh_titles([session], cache, generator=mock.Mock())

        self.assertEqual(result, {})
        self.assertEqual(cache["cursor:thin"]["generation_state"], "insufficient")
        title, needs = titles.resolve_initial_title(session, cache)
        self.assertFalse(needs)
        self.assertNotEqual(title, titles.INSUFFICIENT_TITLE_TOKEN)

        grown = dict(session, size_bytes=500, first_user_msg="修复登录页空白", fallback_title="修复登录页空白")
        grown_title, grown_needs = titles.resolve_initial_title(grown, cache)
        self.assertEqual(grown_title, "修复登录页空白")
        self.assertTrue(grown_needs)

    def test_insufficient_retry_tracks_task_input_not_file_size(self) -> None:
        session = _session(
            id="thin",
            size_bytes=100,
            fallback_title="在吗",
            first_user_msg="继续",
            last_agent_msg="ok",
        )
        cache: dict = {}
        raw = {"cursor:thin": titles.INSUFFICIENT_TITLE_TOKEN}
        with (
            mock.patch.object(titles, "generate_titles_batch", return_value=raw),
            mock.patch.object(titles, "save_cache"),
        ):
            titles.refresh_titles([session], cache, generator=mock.Mock())
        self.assertEqual(cache["cursor:thin"]["generation_state"], "insufficient")

        same_prompt = dict(session, size_bytes=200, last_agent_msg="tool log " * 40)
        self.assertFalse(titles.resolve_initial_title(same_prompt, cache)[1])

        edited = dict(session, first_user_msg="修复登录页空白", fallback_title="修复登录页空白")
        self.assertTrue(titles.resolve_initial_title(edited, cache)[1])

    def test_invalid_model_titles_are_not_recorded_as_transport(self) -> None:
        session = _session(
            id="task",
            first_user_msg="修复登录失败",
            fallback_title="修复登录失败",
        )
        cache: dict = {}
        raw = titles._BatchRaw({"cursor:task": "Task"}, kind="ok")
        with (
            mock.patch.object(titles, "generate_titles_batch", return_value=raw),
            mock.patch.object(titles, "save_cache"),
        ):
            titles.refresh_titles([session], cache, generator=mock.Mock())
        self.assertEqual(cache["cursor:task"]["failure_reason"], "invalid")
        self.assertNotEqual(cache["cursor:task"]["failure_reason"], "transport")

    def test_malformed_json_is_invalid_not_transport(self) -> None:
        session = _session(
            id="badjson",
            first_user_msg="修复登录失败",
            fallback_title="修复登录失败",
        )
        cache: dict = {}
        generator = mock.Mock()
        generator.id = "gateway"
        generator.generate.return_value = "definitely not json {"
        with mock.patch.object(titles, "save_cache"):
            titles.refresh_titles([session], cache, generator=generator)
        self.assertEqual(cache["cursor:badjson"]["failure_reason"], "invalid")
        self.assertNotEqual(cache["cursor:badjson"]["failure_reason"], "transport")

    def test_missing_config_retries_after_key_appears(self) -> None:
        session = _session(
            source="claude",
            id="offline",
            fallback_title="整理离线安装流程",
        )
        cache: dict = {}

        with (
            mock.patch.object(titles.titlegen, "available_generators", return_value=()),
            mock.patch.object(titles.titlegen, "gateway_key", return_value=None),
            mock.patch.object(titles, "save_cache"),
        ):
            titles.refresh_titles([session], cache)

        self.assertEqual(cache["claude:offline"]["failure_reason"], "missing_config")
        with mock.patch.object(titles.titlegen, "gateway_key", return_value=None):
            self.assertFalse(titles.resolve_initial_title(session, cache)[1])
        with mock.patch.object(titles.titlegen, "gateway_key", return_value="vk-test"):
            self.assertTrue(titles.resolve_initial_title(session, cache)[1])

    def test_transport_failure_keeps_cooldown_even_if_key_exists(self) -> None:
        session = _session(id="net", fallback_title="排查网络超时")
        cache = {
            "cursor:net": titles._failed_cache_entry(session, reason="transport"),
        }

        with mock.patch.object(titles.titlegen, "gateway_key", return_value="vk-test"):
            title, needs = titles.resolve_initial_title(session, cache)

        self.assertEqual(title, "排查网络超时")
        self.assertFalse(needs)


if __name__ == "__main__":
    unittest.main()
