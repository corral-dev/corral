"""界面多语言：检测、覆盖与中英文案切换。"""

from __future__ import annotations

import unittest

from corral import i18n
from corral.i18n import t


class I18nDetectTests(unittest.TestCase):
    def tearDown(self) -> None:
        i18n.set_lang("en")

    def test_default_is_english(self) -> None:
        self.assertEqual(i18n.detect_lang({}), "en")
        self.assertEqual(i18n.detect_lang({"LANG": "C"}), "en")
        self.assertEqual(i18n.detect_lang({"LANG": "en_US.UTF-8"}), "en")

    def test_chinese_locale_variants(self) -> None:
        for env in (
            {"LANG": "zh_CN.UTF-8"},
            {"LC_ALL": "zh_TW.UTF-8"},
            {"LC_MESSAGES": "zh-Hans"},
            {"LANGUAGE": "zh_CN:en_US:en"},
            {"LANG": "zh"},
        ):
            with self.subTest(env=env):
                self.assertEqual(i18n.detect_lang(env), "zh")

    def test_corral_lang_overrides_system(self) -> None:
        self.assertEqual(
            i18n.detect_lang({"CORRAL_LANG": "en", "LANG": "zh_CN.UTF-8"}),
            "en",
        )
        self.assertEqual(
            i18n.detect_lang({"CORRAL_LANG": "zh", "LANG": "en_US.UTF-8"}),
            "zh",
        )

    def test_pickup_lang_still_overrides(self) -> None:
        self.assertEqual(
            i18n.detect_lang({"PICKUP_LANG": "zh", "LANG": "en_US.UTF-8"}),
            "zh",
        )


class I18nCatalogTests(unittest.TestCase):
    def tearDown(self) -> None:
        i18n.set_lang("en")

    def test_english_and_chinese_strings(self) -> None:
        i18n.set_lang("en")
        self.assertEqual(t("action.advanced"), "Advanced")
        self.assertEqual(t("list.new_session"), "+ New session")
        self.assertEqual(t("list.active_filter"), "Only active")
        self.assertEqual(t("list.sep_pinned"), "Pinned")
        self.assertEqual(t("list.sep_today"), "Today")
        self.assertEqual(t("list.sep_yesterday"), "Yesterday")
        self.assertEqual(t("list.sep_monday"), "Monday")
        self.assertEqual(t("list.sep_sunday"), "Sunday")
        self.assertEqual(t("time.minutes_ago", n=2), "2m ago")

        i18n.set_lang("zh")
        self.assertEqual(t("action.advanced"), "高级操作")
        self.assertEqual(t("list.new_session"), "＋ 新建会话")
        self.assertEqual(t("list.active_filter"), "仅活跃会话")
        self.assertEqual(t("list.sep_pinned"), "置顶")
        self.assertEqual(t("list.sep_today"), "今天")
        self.assertEqual(t("list.sep_yesterday"), "昨天")
        self.assertEqual(t("list.sep_monday"), "周一")
        self.assertEqual(t("list.sep_sunday"), "周日")
        self.assertEqual(t("time.minutes_ago", n=2), "2分钟前")

    def test_join_names_uses_locale_separator(self) -> None:
        i18n.set_lang("en")
        self.assertEqual(i18n.join_names(["Claude", "Codex"]), "Claude, Codex")
        i18n.set_lang("zh")
        self.assertEqual(i18n.join_names(["Claude", "Codex"]), "Claude、Codex")

    def test_all_keys_have_both_languages(self) -> None:
        for key, catalog in i18n._MESSAGES.items():
            with self.subTest(key=key):
                self.assertIn("en", catalog)
                self.assertIn("zh", catalog)
                self.assertTrue(catalog["en"].strip())
                self.assertTrue(catalog["zh"].strip())

    def test_corrected_catalog_entries(self) -> None:
        i18n.set_lang("en")
        self.assertEqual(t("modal.column_runtime"), "Assistant")
        self.assertEqual(t("modal.handoff_title"), "Advanced: choose handoff assistant")
        self.assertEqual(t("detail.new_session_hint"), "New session: pick a project and assistant")
        self.assertEqual(t("detail.preview_end"), "──── END ────")
        self.assertEqual(t("pane.restart_hint"), "Enter restart")
        i18n.set_lang("zh")
        self.assertEqual(t("modal.column_runtime"), "助手")
        self.assertEqual(t("modal.handoff_title"), "高级操作：选择接力助手")
        self.assertEqual(t("detail.new_session_hint"), "新建会话：选择项目与助手")
        self.assertEqual(t("status.running_hosted"), "运行中（托管）")
        self.assertEqual(t("status.running_external"), "运行中（其他窗口）")
        self.assertEqual(t("detail.preview_end"), "──── 结束 ────")
        self.assertEqual(t("action.focus_list"), "返回列表")
        self.assertEqual(t("pane.restart_hint"), "Enter 重启")

    def test_new_catalog_entries(self) -> None:
        i18n.set_lang("en")
        self.assertEqual(t("session.title.new", name="Claude"), "New Claude session")
        self.assertEqual(t("session.title.pending"), "(pending title)")
        self.assertEqual(t("session.title.cmd.doc_init"), "Init docs")
        self.assertEqual(t("modal.export_session"), "Export session")
        self.assertEqual(t("error.launch_failed", error="boom"), "Launch failed: boom")
        self.assertEqual(t("remote.err.session_gone"), "This session is no longer in the list")
        self.assertEqual(t("shim.status.installed"), "Installed")
        i18n.set_lang("zh")
        self.assertEqual(t("session.title.new", name="Claude"), "新 Claude 会话")
        self.assertEqual(t("session.title.pending"), "(待生成标题)")
        self.assertEqual(t("session.title.cmd.doc_init"), "文档初始化")
        self.assertEqual(t("modal.export_session"), "导出会话")
        self.assertEqual(t("error.launch_failed", error="boom"), "启动失败：boom")
        self.assertEqual(t("remote.err.session_gone"), "这条会话已经不在列表里了")
        self.assertEqual(t("shim.status.installed"), "已安装")


if __name__ == "__main__":
    unittest.main()
