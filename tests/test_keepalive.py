from __future__ import annotations

import subprocess
import unittest
from unittest import mock

from corral import keepalive
from corral.models import LaunchPlan


class EnabledTests(unittest.TestCase):
    def test_disabled_flag_wins(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("corral.keepalive.shutil.which", return_value="/usr/bin/tmux"):
            self.assertFalse(keepalive.enabled(disabled_flag=True))

    def test_env_var_disables(self) -> None:
        with mock.patch.dict("os.environ", {"CORRAL_KEEPALIVE": "0"}, clear=True), \
             mock.patch("corral.keepalive.shutil.which", return_value="/usr/bin/tmux"):
            self.assertFalse(keepalive.enabled())

    def test_legacy_env_var_still_disables(self) -> None:
        # 改名前的旧变量名 SC_KEEPALIVE 继续生效，不悄悄破坏已有的用户配置
        with mock.patch.dict("os.environ", {"SC_KEEPALIVE": "0"}, clear=True), \
             mock.patch("corral.keepalive.shutil.which", return_value="/usr/bin/tmux"):
            self.assertFalse(keepalive.enabled())

    def test_pickup_env_var_still_disables(self) -> None:
        with mock.patch.dict("os.environ", {"PICKUP_KEEPALIVE": "0"}, clear=True), \
             mock.patch("corral.keepalive.shutil.which", return_value="/usr/bin/tmux"):
            self.assertFalse(keepalive.enabled())

    def test_already_inside_tmux_skips(self) -> None:
        with mock.patch.dict("os.environ", {"TMUX": "/tmp/tmux-1000/default,1234,0"}, clear=True), \
             mock.patch("corral.keepalive.shutil.which", return_value="/usr/bin/tmux"):
            self.assertFalse(keepalive.enabled())

    def test_already_inside_screen_skips(self) -> None:
        with mock.patch.dict("os.environ", {"STY": "1234.pts-0.host"}, clear=True), \
             mock.patch("corral.keepalive.shutil.which", return_value="/usr/bin/tmux"):
            self.assertFalse(keepalive.enabled())

    def test_missing_tmux_binary_skips(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("corral.keepalive.shutil.which", return_value=None):
            self.assertFalse(keepalive.enabled())

    def test_default_enabled(self) -> None:
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("corral.keepalive.shutil.which", return_value="/usr/bin/tmux"):
            self.assertTrue(keepalive.enabled())


class WrapPlanTests(unittest.TestCase):
    def test_wraps_argv_with_dedicated_socket_and_config(self) -> None:
        plan = LaunchPlan(("claude", "--resume", "abcd1234efgh"), "/work/dir")

        wrapped = keepalive.wrap_plan(plan, "claude", "abcd1234efgh")

        self.assertEqual(wrapped.argv[0], "tmux")
        self.assertIn("-L", wrapped.argv)
        self.assertEqual(wrapped.argv[wrapped.argv.index("-L") + 1], keepalive.SOCKET_NAME)
        self.assertIn("new-session", wrapped.argv)
        self.assertIn("-A", wrapped.argv)
        self.assertIn("-s", wrapped.argv)
        session_name = wrapped.argv[wrapped.argv.index("-s") + 1]
        self.assertTrue(session_name.startswith("corral-claude-"))
        self.assertIn("-c", wrapped.argv)
        self.assertEqual(wrapped.argv[wrapped.argv.index("-c") + 1], "/work/dir")
        self.assertIn("--", wrapped.argv)
        # 原始 argv 完整保留在 -- 之后，不能被拆散或改写
        tail = wrapped.argv[wrapped.argv.index("--") + 1:]
        self.assertEqual(tail, plan.argv)
        # tmux 自己接管 -c，不需要 execute_launch 再 os.chdir 一次
        self.assertIsNone(wrapped.cwd)

    def test_injects_both_new_and_legacy_env_vars(self) -> None:
        plan = LaunchPlan(("claude",), None)

        wrapped = keepalive.wrap_plan(plan, "claude", "abcd1234")

        for name in ("CORRAL_RUNTIME", "CORRAL_SESSION_ID",
                     "PICKUP_RUNTIME", "PICKUP_SESSION_ID",
                     "SC_RUNTIME", "SC_SESSION_ID"):
            self.assertTrue(any(arg == f"{name}={'claude' if name.endswith('RUNTIME') else 'abcd1234'}"
                                for arg in wrapped.argv), name)

    def test_wrap_plan_without_cwd_skips_dash_c(self) -> None:
        plan = LaunchPlan(("codex",), None)

        wrapped = keepalive.wrap_plan(plan, "codex", "session-id")

        self.assertNotIn("-c", wrapped.argv)

    def test_wrap_plan_pins_pi_session_id_for_new_sessions(self) -> None:
        from corral import pi_identity

        plan = LaunchPlan(("pi", "--approve"), "/tmp/proj")

        with mock.patch.object(pi_identity, "ensure_extension_installed", return_value={"status": "ok"}):
            wrapped = keepalive.wrap_plan(plan, "pi", "abcd1234")

        tail = wrapped.argv[wrapped.argv.index("--") + 1:]
        # 新架构：会话写回 Pi 默认目录，不再注入 --session-dir 小房间；
        # 分屏身份改由 corral-session-identity 扩展的 claim 提供。
        self.assertEqual(tail, ("pi", "--approve", "--session-id", "abcd1234"))
        self.assertNotIn("-e", wrapped.argv[wrapped.argv.index("--") + 1:])
        env_pairs = [wrapped.argv[i + 1] for i, arg in enumerate(wrapped.argv) if arg == "-e"]
        self.assertTrue(
            any(pair.startswith(pi_identity.INSTANCE_ENV + "=") for pair in env_pairs)
        )
        self.assertTrue(
            any(pair.startswith(pi_identity.CLAIM_PATH_ENV + "=") for pair in env_pairs)
        )
        resume = LaunchPlan(("pi", "--approve", "--session", "/tmp/a.jsonl"), "/tmp/proj")
        with mock.patch.object(pi_identity, "ensure_extension_installed", return_value={"status": "ok"}):
            resume_wrapped = keepalive.wrap_plan(resume, "pi", "abcd1234")
        resume_tail = resume_wrapped.argv[resume_wrapped.argv.index("--") + 1:]
        self.assertEqual(resume_tail, ("pi", "--approve", "--session", "/tmp/a.jsonl"))
        self.assertNotIn("--session-id", resume_tail)
        self.assertNotIn("--session-dir", resume_tail)

    def test_session_name_truncates_long_ident(self) -> None:
        plan = LaunchPlan(("claude",), None)

        wrapped = keepalive.wrap_plan(plan, "claude", "0123456789abcdef")

        session_name = wrapped.argv[wrapped.argv.index("-s") + 1]
        self.assertEqual(session_name, "corral-claude-01234567")


class AttachPlanTests(unittest.TestCase):
    def test_returns_none_without_keepalive_name(self) -> None:
        self.assertIsNone(keepalive.attach_plan({"id": "x"}))

    def test_builds_attach_command(self) -> None:
        plan = keepalive.attach_plan({"keepalive_name": "sc-claude-abcd1234"})

        self.assertIsNotNone(plan)
        self.assertEqual(plan.argv[0], "tmux")
        self.assertIn("attach-session", plan.argv)
        self.assertIn("-t", plan.argv)
        self.assertEqual(plan.argv[plan.argv.index("-t") + 1], "sc-claude-abcd1234")
        self.assertEqual(plan.argv[plan.argv.index("-L") + 1], "pickup-keepalive")
        self.assertIsNone(plan.cwd)

    def test_pickup_prefix_uses_legacy_socket(self) -> None:
        plan = keepalive.attach_plan({"keepalive_name": "pickup-claude-abcd1234"})

        self.assertIsNotNone(plan)
        self.assertEqual(plan.argv[plan.argv.index("-L") + 1], "pickup-keepalive")
        self.assertEqual(plan.argv[plan.argv.index("-t") + 1], "pickup-claude-abcd1234")

    def test_new_prefix_uses_new_socket(self) -> None:
        plan = keepalive.attach_plan({"keepalive_name": "corral-claude-abcd1234"})

        self.assertIsNotNone(plan)
        self.assertEqual(plan.argv[plan.argv.index("-L") + 1], "corral-keepalive")


def _fake_check_output(list_sessions_output: str, ps_output: str):
    def _run(argv, **kwargs):
        if argv[:1] == ["ps"]:
            return ps_output.encode()
        if "list-sessions" in argv:
            return list_sessions_output.encode()
        raise AssertionError(f"unexpected subprocess call: {argv}")
    return _run


def _list_sessions_only(list_sessions_output: str):
    """无 pid 路径只该打 tmux list-sessions，不得为祖先链再 fork 一次 ps。"""
    def _run(argv, **kwargs):
        if argv[:1] == ["ps"]:
            raise AssertionError("annotate without pid must not call ps")
        if "list-sessions" in argv:
            return list_sessions_output.encode()
        raise AssertionError(f"unexpected subprocess call: {argv}")
    return _run


class AnnotateTests(unittest.TestCase):
    def test_matches_session_by_pid_ancestor_chain(self) -> None:
        # tmux pane 顶层 pid 是 100（tmux 直接 exec 的进程），但运行时自己注册的
        # "活跃 pid" 是 102——中间隔了一层 fork，必须靠祖先链才能追上，不能只比对 pid 相等。
        sessions = [{"id": "s1", "pid": 102}, {"id": "s2", "pid": 999}]
        list_sessions_out = "corral-claude-abcd1234|100\n"
        ps_out = "  PID  PPID\n  100     1\n  101   100\n  102   101\n  999     1\n"

        with mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("corral.liveness.subprocess.check_output",
                         side_effect=_fake_check_output(list_sessions_out, ps_out)):
            keepalive.annotate(sessions)

        self.assertEqual(sessions[0]["keepalive_name"], "corral-claude-abcd1234")
        self.assertNotIn("keepalive_name", sessions[1])

    def test_legacy_sc_prefix_sessions_still_matched(self) -> None:
        # 改名前创建的 sc-* 保活会话（可能仍在用户机器上跑）必须继续被识别/回收
        sessions = [{"id": "s1", "pid": 101}]
        list_sessions_out = "sc-claude-abcd1234|100\n"
        ps_out = "  PID  PPID\n  100     1\n  101   100\n"

        with mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("corral.liveness.subprocess.check_output",
                         side_effect=_fake_check_output(list_sessions_out, ps_out)):
            keepalive.annotate(sessions)

        self.assertEqual(sessions[0]["keepalive_name"], "sc-claude-abcd1234")

    def test_legacy_pickup_prefix_sessions_still_matched(self) -> None:
        sessions = [{"id": "s1", "pid": 101}]
        list_sessions_out = "pickup-claude-abcd1234|100\n"
        ps_out = "  PID  PPID\n  100     1\n  101   100\n"

        with mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("corral.liveness.subprocess.check_output",
                         side_effect=_fake_check_output(list_sessions_out, ps_out)):
            keepalive.annotate(sessions)

        self.assertEqual(sessions[0]["keepalive_name"], "pickup-claude-abcd1234")

    def test_exact_pane_pid_match_wins_over_earlier_ancestor_match(self) -> None:
        # 进程树嵌套：pid 102 既是外层 pane(顶层100)的后代，又恰好是内层 pane
        # (corral-pi-second) 的顶层进程。精确命中（它自己就是某 pane 顶层）必须
        # 赢过先遍历到的祖先命中，否则名字会被外层 pane 抢走。
        sessions = [{"id": "s1", "pid": 102}]
        list_sessions_out = "corral-pi-second|102\ncorral-pi-first|100\n"
        ps_out = "  PID  PPID\n  100     1\n  101   100\n  102   101\n"

        with mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("corral.liveness.subprocess.check_output",
                         side_effect=_fake_check_output(list_sessions_out, ps_out)):
            keepalive.annotate(sessions)

        self.assertEqual(sessions[0]["keepalive_name"], "corral-pi-second")

    def test_annotated_pid_is_not_renamed_by_later_pane(self) -> None:
        # 同一个 pid 是两个 pane 顶层进程的后代（伪进程树）时，先对上的 pane 名字
        # 不能被后遍历到的 pane 改写：一个进程只挂一个名字。
        sessions = [{"id": "s1", "pid": 305}]
        list_sessions_out = "corral-pi-a|300\ncorral-pi-b|301\n"
        ps_out = "  PID  PPID\n  300     1\n  301     1\n  305   301\n  301   300\n"

        with mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("corral.liveness.subprocess.check_output",
                         side_effect=_fake_check_output(list_sessions_out, ps_out)):
            keepalive.annotate(sessions)

        self.assertEqual(sessions[0]["keepalive_name"], "corral-pi-a")

    def test_no_tmux_server_running_is_a_noop(self) -> None:
        sessions = [{"id": "s1", "pid": 100}]

        with mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("corral.liveness.subprocess.check_output",
                         side_effect=subprocess.CalledProcessError(1, "tmux")):
            keepalive.annotate(sessions)

        self.assertNotIn("keepalive_name", sessions[0])

    def test_sessions_without_pid_match_unique_managed_name(self) -> None:
        """扫描没标 pid 时仍按 wrap_plan 同款名贴回还活着的 pane。

        Pi 的 jsonl 写完即关、claim 过期后经常拿不到 pid；旧逻辑直接跳过 tmux，
        侧栏就把还在跑的托管会话画成 Enter restart，回车却走 ``new-session -A``。
        """
        sessions = [
            {"id": "39f9df6b-aaaa-bbbb-cccc-ddddeeeeffff", "source": "pi", "pid": None},
            {"id": "s2", "source": "pi"},
        ]
        list_sessions_out = "corral-pi-39f9df6b|4242\n"

        with mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("corral.liveness.subprocess.check_output",
                         side_effect=_list_sessions_only(list_sessions_out)):
            keepalive.annotate(sessions)

        self.assertEqual(sessions[0]["keepalive_name"], "corral-pi-39f9df6b")
        self.assertNotIn("keepalive_name", sessions[1])

    def test_sessions_without_source_are_not_name_matched(self) -> None:
        """缺 source 的历史不得靠 8 位 ident 猜绑；pid 测试夹具也没有 source。"""
        sessions = [{"id": "39f9df6b"}]
        list_sessions_out = "corral-pi-39f9df6b|4242\n"

        with mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("corral.liveness.subprocess.check_output",
                         side_effect=_list_sessions_only(list_sessions_out)):
            keepalive.annotate(sessions)

        self.assertNotIn("keepalive_name", sessions[0])

    def test_ambiguous_name_match_assigns_neither(self) -> None:
        sessions = [
            {"id": "39f9df6b-aaaa-bbbb-cccc-111111111111", "source": "pi"},
            {"id": "39f9df6b-aaaa-bbbb-cccc-222222222222", "source": "pi"},
        ]
        list_sessions_out = "corral-pi-39f9df6b|4242\n"

        with mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("corral.liveness.subprocess.check_output",
                         side_effect=_list_sessions_only(list_sessions_out)):
            keepalive.annotate(sessions)

        self.assertNotIn("keepalive_name", sessions[0])
        self.assertNotIn("keepalive_name", sessions[1])

    def test_one_pane_is_not_assigned_to_two_sessions(self) -> None:
        """父进程和子进程被扫成两张卡时，祖先链不得把同一份 tmux 画面贴给两张卡。

        真机：分屏两格内容一模一样——两条会话都挂了同一个 keepalive_name。
        """
        sessions = [{"id": "s-parent", "pid": 100}, {"id": "s-child", "pid": 102}]
        list_sessions_out = "corral-pi-aaaa1111|100\n"
        ps_out = "  PID  PPID\n  100     1\n  101   100\n  102   101\n"

        with mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("corral.liveness.subprocess.check_output",
                         side_effect=_fake_check_output(list_sessions_out, ps_out)):
            keepalive.annotate(sessions)

        self.assertEqual(sessions[0]["keepalive_name"], "corral-pi-aaaa1111")
        self.assertNotIn("keepalive_name", sessions[1])


class AutomaticCleanupSafetyTests(unittest.TestCase):
    def test_default_never_kills_even_with_old_many_sessions(self):
        with mock.patch.dict("os.environ", {}, clear=True), \
             mock.patch("corral.liveness._list_tmux_sessions") as listing, \
             mock.patch("corral.keepalive.kill") as kill:
            self.assertEqual(keepalive.reap(now=10**12), [])
        listing.assert_not_called()
        kill.assert_not_called()

    def test_idle_cleanup_protects_working_and_detached_output(self):
        rows = [
            ["corral-pi-working", "1", "1"],
            ["corral-pi-output", "1", "99999"],
            ["corral-pi-idle", "1", "1"],
        ]
        with mock.patch.dict("os.environ", {"CORRAL_KEEPALIVE_IDLE_HOURS": "2"}, clear=True), \
             mock.patch("corral.liveness._list_tmux_sessions", return_value=rows), \
             mock.patch("corral.keepalive._load_working_pairs", return_value=[("pi", "working")]), \
             mock.patch("corral.keepalive._background_busy", return_value=set()), \
             mock.patch("corral.keepalive.kill", return_value=True) as kill:
            self.assertEqual(keepalive.reap_idle(now=100000), ["corral-pi-idle"])
        kill.assert_called_once_with("corral-pi-idle")

    def test_pressure_cleanup_protects_detached_output(self):
        rows = [["corral-pi-output", "1", "99999"], ["corral-pi-idle", "1", "1"]]
        with mock.patch.dict("os.environ", {"CORRAL_KEEPALIVE_MAX_SESSIONS": "1"}, clear=True), \
             mock.patch("corral.liveness._list_tmux_sessions", return_value=rows), \
             mock.patch("corral.keepalive._load_working_pairs", return_value=[]), \
             mock.patch("corral.keepalive._background_busy", return_value=set()), \
             mock.patch("corral.keepalive.kill", return_value=True) as kill:
            self.assertEqual(keepalive.reap_pressure(now=100000), ["corral-pi-idle"])
        kill.assert_called_once_with("corral-pi-idle")

    def test_reaping_skips_sessions_with_pending_background_work(self):
        rows = [
            ["corral-pi-busy", "1", "1"],
            ["corral-pi-idle", "1", "1"],
        ]
        with mock.patch.dict("os.environ", {"CORRAL_KEEPALIVE_IDLE_HOURS": "2"}, clear=True), \
             mock.patch("corral.liveness._list_tmux_sessions", return_value=rows), \
             mock.patch("corral.keepalive._load_working_pairs", return_value=[]), \
             mock.patch("corral.keepalive._background_busy", return_value={"corral-pi-busy"}), \
             mock.patch("corral.keepalive.kill", return_value=True) as kill:
            self.assertEqual(keepalive.reap_idle(now=100000), ["corral-pi-idle"])
        kill.assert_called_once_with("corral-pi-idle")

    def test_reaping_stops_when_the_background_probe_is_unreadable(self):
        rows = [["corral-pi-idle", "1", "1"]]
        with mock.patch.dict("os.environ", {"CORRAL_KEEPALIVE_IDLE_HOURS": "2"}, clear=True), \
             mock.patch("corral.liveness._list_tmux_sessions", return_value=rows), \
             mock.patch("corral.keepalive._load_working_pairs", return_value=[]), \
             mock.patch("corral.keepalive._background_busy", return_value=None), \
             mock.patch("corral.keepalive.kill", return_value=True) as kill:
            self.assertEqual(keepalive.reap_idle(now=100000), [])
        kill.assert_not_called()


class ReapIdleTests(unittest.TestCase):
    def test_kills_sessions_past_idle_threshold(self) -> None:
        # 新旧两种前缀的会话都要被回收（sc-* 是改名前留下的存量）
        rows = "corral-claude-old|1000\nsc-claude-legacy|1000\ncorral-claude-fresh|99999\n"
        now = 100000.0  # 前两个空闲 99000 秒 ≈ 27.5 小时，超过默认 2 小时阈值

        with mock.patch.dict("os.environ", {"CORRAL_KEEPALIVE_IDLE_HOURS": "2"}, clear=True), \
             mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("corral.liveness.subprocess.check_output", return_value=rows.encode()), \
             mock.patch("corral.keepalive._background_busy", return_value=set()), \
             mock.patch("corral.keepalive.kill", return_value=True) as mocked_kill:
            reaped = keepalive.reap_idle(now=now)

        self.assertEqual(reaped, ["corral-claude-old", "sc-claude-legacy"])
        self.assertEqual(mocked_kill.call_count, 2)

    def test_explicit_threshold_is_two_hours(self) -> None:
        now = 10000.0
        over = now - 2.5 * 3600
        under = now - 1.5 * 3600
        rows = f"corral-claude-over|{over:.0f}\ncorral-claude-under|{under:.0f}\n"

        with mock.patch.dict("os.environ", {"CORRAL_KEEPALIVE_IDLE_HOURS": "2"}, clear=True), \
             mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("corral.liveness.subprocess.check_output", return_value=rows.encode()), \
             mock.patch("corral.keepalive._background_busy", return_value=set()), \
             mock.patch("corral.keepalive.kill", return_value=True) as mocked_kill:
            reaped = keepalive.reap_idle(now=now)

        self.assertEqual(reaped, ["corral-claude-over"])
        mocked_kill.assert_called_once_with("corral-claude-over")

    def test_zero_threshold_disables_reaping(self) -> None:
        with mock.patch.dict("os.environ", {"CORRAL_KEEPALIVE_IDLE_HOURS": "0"}, clear=True), \
             mock.patch("corral.keepalive.subprocess.check_output") as mocked:
            reaped = keepalive.reap_idle(now=100000.0)

        self.assertEqual(reaped, [])
        mocked.assert_not_called()

    def test_legacy_env_threshold_still_honored(self) -> None:
        with mock.patch.dict("os.environ", {"SC_KEEPALIVE_IDLE_HOURS": "0"}, clear=True), \
             mock.patch("corral.keepalive.subprocess.check_output") as mocked:
            reaped = keepalive.reap_idle(now=100000.0)

        self.assertEqual(reaped, [])
        mocked.assert_not_called()

    def test_custom_threshold_from_env(self) -> None:
        rows = "corral-claude-borderline|1000\n"
        now = 1000.0 + 3600.0  # 恰好空闲 1 小时

        with mock.patch.dict("os.environ", {"CORRAL_KEEPALIVE_IDLE_HOURS": "0.5"}, clear=True), \
             mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("corral.liveness.subprocess.check_output", return_value=rows.encode()), \
             mock.patch("corral.keepalive._background_busy", return_value=set()), \
             mock.patch("corral.keepalive.kill", return_value=True) as mocked_kill:
            reaped = keepalive.reap_idle(now=now)

        self.assertEqual(reaped, ["corral-claude-borderline"])
        mocked_kill.assert_called_once()


class ReapPressureTests(unittest.TestCase):
    def _rows(self, items: list[tuple[str, float]]) -> str:
        return "".join(f"{name}|{activity:.0f}\n" for name, activity in items)

    def test_under_cap_does_nothing(self) -> None:
        now = 100_000.0
        # 上限及以下：即使都很闲也不压
        items = [(f"corral-claude-{i:08x}", now - 3600) for i in range(10)]
        with mock.patch.dict("os.environ", {"CORRAL_KEEPALIVE_MAX_SESSIONS": "10"}, clear=True), \
             mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch(
                 "corral.liveness.subprocess.check_output",
                 return_value=self._rows(items).encode(),
             ), \
             mock.patch("corral.keepalive.kill", return_value=True) as mocked_kill, \
             mock.patch("corral.keepalive._load_working_pairs", return_value=[]):
            reaped = keepalive.reap_pressure(now=now)

        self.assertEqual(reaped, [])
        mocked_kill.assert_not_called()

    def test_over_cap_reaps_oldest_idle_non_working(self) -> None:
        now = 100_000.0
        idle = now - 11 * 60  # >10 分钟
        fresh = now - 60
        items = [(f"corral-claude-{i:08x}", idle - i) for i in range(12)]
        items[0] = ("corral-claude-00000000", fresh)  # 刚活动过，不收
        # 00000001 标记为执行中，不收
        with mock.patch.dict("os.environ", {"CORRAL_KEEPALIVE_MAX_SESSIONS": "10"}, clear=True), \
             mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch(
                 "corral.liveness.subprocess.check_output",
                 return_value=self._rows(items).encode(),
             ), \
             mock.patch("corral.keepalive.kill", return_value=True) as mocked_kill, \
             mock.patch("corral.keepalive._background_busy", return_value=set()), \
             mock.patch(
                 "corral.keepalive._load_working_pairs",
                 return_value=[("claude", "00000001abcd")],
             ):
            reaped = keepalive.reap_pressure(now=now)

        # 12 个 → 需关到 10，即关 2 个；跳过 fresh 与 working，从最闲开始
        self.assertEqual(len(reaped), 2)
        self.assertEqual(mocked_kill.call_count, 2)
        # activity 最小的是 index 11（idle-11），然后 10
        self.assertEqual(reaped[0], "corral-claude-0000000b")
        self.assertEqual(reaped[1], "corral-claude-0000000a")
        self.assertNotIn("corral-claude-00000000", reaped)
        self.assertNotIn("corral-claude-00000001", reaped)

    def test_zero_max_disables_pressure(self) -> None:
        with mock.patch.dict("os.environ", {"CORRAL_KEEPALIVE_MAX_SESSIONS": "0"}, clear=True), \
             mock.patch("corral.keepalive.subprocess.check_output") as mocked:
            reaped = keepalive.reap_pressure(now=100000.0)

        self.assertEqual(reaped, [])
        mocked.assert_not_called()

    def test_custom_pressure_idle_minutes(self) -> None:
        now = 10_000.0
        # 默认 10 分钟不够，自定义 1 分钟后应收
        items = [(f"corral-claude-{i:08x}", now - 90) for i in range(11)]
        with mock.patch.dict(
            "os.environ",
            {"CORRAL_KEEPALIVE_PRESSURE_IDLE_MINUTES": "1", "CORRAL_KEEPALIVE_MAX_SESSIONS": "10"},
            clear=True,
        ), \
             mock.patch("corral.liveness.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch(
                 "corral.liveness.subprocess.check_output",
                 return_value=self._rows(items).encode(),
             ), \
             mock.patch("corral.keepalive.kill", return_value=True) as mocked_kill, \
             mock.patch("corral.keepalive._background_busy", return_value=set()), \
             mock.patch("corral.keepalive._load_working_pairs", return_value=[]):
            reaped = keepalive.reap_pressure(now=now)

        self.assertEqual(len(reaped), 1)
        mocked_kill.assert_called_once()

    def test_reap_runs_idle_then_pressure(self) -> None:
        with mock.patch("corral.keepalive.reap_idle", return_value=["a"]) as idle, \
             mock.patch("corral.keepalive.reap_pressure", return_value=["b"]) as pressure:
            result = keepalive.reap(now=1.0)

        idle.assert_called_once_with(now=1.0)
        pressure.assert_called_once_with(now=1.0)
        self.assertEqual(result, ["a", "b"])


class KillTests(unittest.TestCase):
    def test_kill_invokes_tmux_kill_session(self) -> None:
        with mock.patch("corral.keepalive.shutil.which", return_value="/usr/bin/tmux"), \
             mock.patch("corral.keepalive.subprocess.run") as mocked_run:
            result = keepalive.kill("sc-claude-abcd1234")

        self.assertTrue(result)
        argv = mocked_run.call_args[0][0]
        self.assertIn("kill-session", argv)
        self.assertIn("-t", argv)
        self.assertEqual(argv[argv.index("-t") + 1], "sc-claude-abcd1234")
        self.assertEqual(argv[argv.index("-L") + 1], "pickup-keepalive")

    def test_kill_returns_false_without_tmux(self) -> None:
        with mock.patch("corral.keepalive.shutil.which", return_value=None):
            self.assertFalse(keepalive.kill("sc-claude-abcd1234"))


if __name__ == "__main__":
    unittest.main()
