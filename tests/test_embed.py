"""corral.embed.py 的单元测试：tmux 命令拼装、SGR 画面解析、Cell→Style 映射、按键翻译。

tmux 子进程一律 mock，不需要真实 tmux，可在无终端环境跑。
"""

from __future__ import annotations

import base64
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock as mock

from corral import embed, liveness
from corral.models import LaunchPlan


def _run_completed_ok(*_args, **_kwargs):
    return subprocess.CompletedProcess(args=[], returncode=0)


class _FakeLinePipe:
    """供 ControlChannel 协议测试使用的可阻塞逐行 stdout。"""

    _EOF = object()

    def __init__(self, lines=()):
        self._lines = queue.Queue()
        self.closed = False
        for line in lines:
            self.feed(line)

    def feed(self, line: str) -> None:
        self._lines.put(line.encode())

    def __iter__(self):
        return self

    def __next__(self):
        item = self._lines.get()
        if item is self._EOF:
            raise StopIteration
        return item

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._lines.put(self._EOF)


class _FakeStdin:
    def __init__(self, on_write=None):
        self.on_write = on_write
        self.closed = False
        self.writes = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)
        if self.on_write is not None:
            self.on_write(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeControlProcess:
    """只实现 ControlChannel 使用到的 Popen 契约，并记录资源是否完整回收。"""

    def __init__(self, *, startup_ready=True, on_write=None):
        startup = ("%begin 1 1\n", "%end 1 1\n") if startup_ready else ()
        self.stdout = _FakeLinePipe(startup)
        self.stdin = _FakeStdin(on_write)
        self.returncode = None
        self.waited = False

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15
        self.stdout.close()

    def kill(self) -> None:
        self.returncode = -9
        self.stdout.close()

    def wait(self, timeout=None):
        self.waited = True
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


class AvailableTests(unittest.TestCase):
    def test_available_with_tmux(self):
        with mock.patch.object(embed.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.dict("os.environ", {}, clear=True):
            self.assertTrue(embed.available())

    def test_unavailable_without_tmux(self):
        with mock.patch.object(embed.shutil, "which", return_value=None), \
                mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(embed.available())

    def test_unavailable_when_explicitly_disabled(self):
        with mock.patch.object(embed.shutil, "which", return_value="/usr/bin/tmux"):
            self.assertFalse(embed.available(disabled_flag=True))
            with mock.patch.dict("os.environ", {"CORRAL_KEEPALIVE": "0"}, clear=True):
                self.assertFalse(embed.available())
            with mock.patch.dict("os.environ", {"SC_KEEPALIVE": "0"}, clear=True):
                self.assertFalse(embed.available())
            with mock.patch.dict("os.environ", {"PICKUP_KEEPALIVE": "0"}, clear=True):
                self.assertFalse(embed.available())

    def test_ignores_tmux_env_nesting(self):
        # 用户在自己的 tmux 里跑 corral 时 keepalive.enabled() 会关闭，但内嵌不 attach，
        # TMUX/STY 不影响可用性。
        with mock.patch.object(embed.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.dict("os.environ", {"TMUX": "/tmp/tmux-1000/default,1,0"}, clear=True):
            self.assertTrue(embed.available())


class HostSessionTests(unittest.TestCase):
    @staticmethod
    def _new_session_argv(run_mock) -> list:
        """host_session 随后还会 set-option；断言必须找 new-session 那一次，不能用最后一次 call。"""
        for call in run_mock.call_args_list:
            argv = call.args[0]
            if "new-session" in argv:
                return argv
        raise AssertionError(f"no new-session call in {run_mock.call_args_list!r}")

    def test_argv_detached_with_size_and_env(self):
        plan = LaunchPlan(argv=("claude", "--resume", "abc"), cwd="/tmp/work")
        embed._manual_window_size_sockets.clear()
        with mock.patch.object(embed.subprocess, "run", side_effect=_run_completed_ok) as run, \
                mock.patch.object(embed.keepalive, "_ensure_config_file", return_value="/tmp/k.conf"), \
                mock.patch.object(embed.keepalive, "reap_pressure", return_value=[]), \
                mock.patch.object(embed.shutil, "which", return_value="/usr/bin/tmux"):
            name = embed.host_session(plan, "claude", "0123456789abcdef", 120, 40)
        self.assertEqual(name, "corral-claude-01234567")
        argv = self._new_session_argv(run)
        self.assertEqual(argv[:3], ["tmux", "-L", "corral-keepalive"])
        self.assertIn("-f", argv)
        joined = " ".join(argv)
        self.assertIn("new-session -d -P -F #{pane_id} -s corral-claude-01234567 -x 120 -y 40", joined)
        self.assertIn("-c /tmp/work", joined)
        for env_pair in ("CORRAL_RUNTIME=claude", "CORRAL_SESSION_ID=0123456789abcdef",
                         "PICKUP_RUNTIME=claude", "PICKUP_SESSION_ID=0123456789abcdef",
                         "SC_RUNTIME=claude", "SC_SESSION_ID=0123456789abcdef"):
            self.assertIn(f"-e {env_pair}", joined)
        self.assertEqual(argv[-2:], ["--resume", "abc"])

    def test_pi_new_session_argv_includes_session_id(self):
        from corral import pi_identity

        plan = LaunchPlan(argv=("pi", "--approve"), cwd="/tmp/work")
        embed._manual_window_size_sockets.clear()
        with mock.patch.object(embed.subprocess, "run", side_effect=_run_completed_ok) as run, \
                mock.patch.object(embed.keepalive, "_ensure_config_file", return_value="/tmp/k.conf"), \
                mock.patch.object(embed.keepalive, "reap_pressure", return_value=[]), \
                mock.patch.object(embed.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.object(pi_identity, "ensure_extension_installed", return_value={"status": "ok"}):
            embed.host_session(plan, "pi", "abcd1234", 120, 40)
        argv = self._new_session_argv(run)
        tail = argv[argv.index("--") + 1:]
        # 新架构：不再注入 --session-dir；身份由扩展 claim 提供。
        self.assertEqual(tail, ["pi", "--approve", "--session-id", "abcd1234"])
        env_pairs = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]
        self.assertTrue(
            any(pair.startswith(pi_identity.INSTANCE_ENV + "=") for pair in env_pairs)
        )
        self.assertTrue(
            any(pair.startswith(pi_identity.CLAIM_PATH_ENV + "=") for pair in env_pairs)
        )

    def test_duplicate_session_falls_back_to_reuse(self):
        plan = LaunchPlan(argv=("claude",), cwd=None)

        def run_side_effect(argv, **_kwargs):
            if "new-session" in argv:
                raise subprocess.CalledProcessError(1, argv)
            return subprocess.CompletedProcess(args=argv, returncode=0)  # has-session 成功

        with mock.patch.object(embed.subprocess, "run", side_effect=run_side_effect), \
                mock.patch.object(embed.keepalive, "_ensure_config_file", return_value="/tmp/k.conf"), \
                mock.patch.object(embed.keepalive, "reap_pressure", return_value=[]), \
                mock.patch.object(embed.shutil, "which", return_value="/usr/bin/tmux"):
            self.assertEqual(embed.host_session(plan, "claude", "0123456789abcdef", 80, 24),
                             "corral-claude-01234567")

    def test_create_failure_raises_embed_error(self):
        plan = LaunchPlan(argv=("claude",), cwd=None)

        def run_side_effect(argv, **_kwargs):
            raise subprocess.CalledProcessError(1, argv)  # new-session 与 has-session 都失败

        with mock.patch.object(embed.subprocess, "run", side_effect=run_side_effect), \
                mock.patch.object(embed.keepalive, "_ensure_config_file", return_value="/tmp/k.conf"), \
                mock.patch.object(embed.keepalive, "reap_pressure", return_value=[]), \
                mock.patch.object(embed.shutil, "which", return_value="/usr/bin/tmux"):
            with self.assertRaises(embed.EmbedError):
                embed.host_session(plan, "claude", "0123456789abcdef", 80, 24)


class SessionIoTests(unittest.TestCase):
    def test_capture_returns_decoded_text(self):
        with mock.patch.object(embed.subprocess, "check_output", return_value=b"hi \x1b[31mred\x1b[0m"):
            self.assertEqual(embed.capture("sc-claude-1"), "hi \x1b[31mred\x1b[0m")

    def test_capture_none_on_failure(self):
        with mock.patch.object(embed.subprocess, "check_output",
                               side_effect=subprocess.CalledProcessError(1, [])):
            self.assertIsNone(embed.capture("sc-claude-1"))

    def test_is_alive(self):
        with mock.patch.object(liveness.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.object(liveness.subprocess, "run", side_effect=_run_completed_ok):
            self.assertTrue(embed.is_alive("sc-claude-1"))
        with mock.patch.object(liveness.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.object(liveness.subprocess, "run",
                                  side_effect=subprocess.CalledProcessError(1, [])):
            self.assertFalse(embed.is_alive("sc-claude-1"))

    def test_is_alive_timeout_is_not_death(self):
        """has-session 超时不得当成 pane 已死，否则切走再切回会把还在跑的会话画成预览。"""
        liveness.note_alive("corral-claude-timeout")
        try:
            with mock.patch.object(liveness.shutil, "which", return_value="/usr/bin/tmux"), \
                    mock.patch.object(
                        liveness.subprocess, "run",
                        side_effect=subprocess.TimeoutExpired(cmd="has-session", timeout=1),
                    ):
                self.assertTrue(liveness.is_alive("corral-claude-timeout"))
        finally:
            liveness.forget_alive("corral-claude-timeout")

    def test_alive_evidence_cache_skips_fork(self):
        """抓帧成功即存活证据：`max_age` 内的活跃判定不得再 fork has-session。

        界面层每次切换选中会话都要判活，一次 fork 约 5ms、分屏几格就乘几，
        全压在 Textual 主线程上（真机现象：切会话时界面明显一顿）。
        """
        embed._alive_marks.clear()
        with mock.patch.object(embed.subprocess, "check_output", return_value=b"frame"):
            embed.capture("corral-claude-cache")
        with mock.patch.object(liveness.subprocess, "run",
                               side_effect=AssertionError("命中缓存时不该再 fork")):
            self.assertTrue(embed.is_alive("corral-claude-cache", max_age=3.0))

    def test_stale_alive_evidence_falls_back_to_real_check(self):
        embed._alive_marks["corral-claude-old"] = time.monotonic() - 30
        with mock.patch.object(liveness.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.object(liveness.subprocess, "run", side_effect=_run_completed_ok):
            self.assertTrue(embed.is_alive("corral-claude-old", max_age=3.0))

    def test_death_check_never_uses_cache(self):
        """判定「会话是否已结束」必须真问一次，缓存不能给死会话续命。"""
        embed._alive_marks["corral-claude-dead"] = time.monotonic()
        with mock.patch.object(liveness.shutil, "which", return_value="/usr/bin/tmux"), \
                mock.patch.object(liveness.subprocess, "run",
                                  side_effect=subprocess.CalledProcessError(1, [])):
            self.assertFalse(embed.is_alive("corral-claude-dead"))
        self.assertNotIn("corral-claude-dead", embed._alive_marks)

    def test_send_literal_and_key(self):
        calls = []

        def run_side_effect(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(args=argv, returncode=0)

        with mock.patch.object(embed.subprocess, "run", side_effect=run_side_effect):
            embed.send_literal("sc-claude-1", "你好 world")
            embed.send_key("sc-claude-1", "Enter")
            embed.send_key("sc-claude-1", "C-c")
        self.assertEqual(calls[0][3:],
                         ["send-keys", "-l", "-t", "sc-claude-1", "--", "你好 world"])
        self.assertEqual(calls[1][-2:], ["--", "Enter"])
        self.assertEqual(calls[2][-2:], ["--", "C-c"])

    def test_paste_uses_buffer_with_bracketed_flag(self):
        calls = []

        def run_side_effect(argv, **_kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(args=argv, returncode=0)

        with mock.patch.object(embed.subprocess, "run", side_effect=run_side_effect):
            embed.paste("sc-claude-1", "line1\nline2")
        self.assertEqual(len(calls), 2)
        set_name = calls[0][5]
        self.assertTrue(set_name.startswith("corral-embed-"))
        self.assertNotEqual(set_name, "corral-embed")
        self.assertEqual(calls[0][3:5], ["set-buffer", "-b"])
        self.assertEqual(calls[0][-1], "line1\nline2")
        self.assertEqual(calls[1][3:6], ["paste-buffer", "-p", "-d"])
        self.assertIn(set_name, calls[1])
        self.assertEqual(calls[1][-1], "sc-claude-1")

    def test_paste_buffers_are_unique_per_call(self):
        """Concurrent sends must not share one buffer (cross-target overwrite)."""
        names = []

        def run_side_effect(argv, **_kwargs):
            if argv[3:5] == ["set-buffer", "-b"]:
                names.append(argv[5])
            return subprocess.CompletedProcess(args=argv, returncode=0)

        with mock.patch.object(embed.subprocess, "run", side_effect=run_side_effect):
            self.assertTrue(embed.paste("pane-a", "aaa"))
            self.assertTrue(embed.paste("pane-b", "bbb"))
        self.assertEqual(len(names), 2)
        self.assertNotEqual(names[0], names[1])

    def test_concurrent_pastes_never_share_a_buffer(self):
        """Two threads pasting at once land in different buffers, same targets."""
        import threading as _threading

        seen: dict[str, str] = {}
        lock = _threading.Lock()

        def run_side_effect(argv, **_kwargs):
            if argv[3:5] == ["set-buffer", "-b"]:
                with lock:
                    seen[argv[5]] = argv[-1]
            return subprocess.CompletedProcess(args=argv, returncode=0)

        barrier = _threading.Barrier(2)

        def _one(target: str, text: str) -> None:
            barrier.wait()
            with mock.patch.object(embed.subprocess, "run", side_effect=run_side_effect):
                self.assertTrue(embed.paste(target, text))

        first = _threading.Thread(target=_one, args=("pane-a", "text-for-A"))
        second = _threading.Thread(target=_one, args=("pane-b", "text-for-B"))
        first.start()
        second.start()
        first.join()
        second.join()
        self.assertEqual(len(seen), 2)
        self.assertEqual(set(seen.values()), {"text-for-A", "text-for-B"})

    def test_paste_detailed_reports_safe_causes(self):
        from corral.embed import InjectionResult

        def _rc1(argv, **_kwargs):
            if argv[3] == "set-buffer":
                return subprocess.CompletedProcess(args=argv, returncode=0)
            return subprocess.CompletedProcess(args=argv, returncode=1, stderr=b"boom")

        with mock.patch.object(embed.subprocess, "run", side_effect=_rc1):
            with mock.patch.object(embed, "pane_liveness", return_value="dead"):
                res = embed.paste_detailed("dead-pane", "hi")
                # paste-buffer rc!=0 without a missing-target note stays uncertain.
                self.assertEqual((res.ok, res.uncertain), (False, True))
            with mock.patch.object(embed, "pane_liveness", return_value="alive"):
                res = embed.paste_detailed("busy-pane", "hi")
                self.assertFalse(res.ok)
                self.assertTrue(res.uncertain)
        with mock.patch.object(
            embed.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 1)
        ):
            # set-buffer timeout: certain failure, no terminal effect.
            res = embed.paste_detailed("slow-pane", "hi")
            self.assertEqual((res.ok, res.cause, res.uncertain), (False, "tmux_busy", False))
        with mock.patch.object(
            embed.subprocess, "run", side_effect=FileNotFoundError()
        ):
            res = embed.paste_detailed("no-tmux", "hi")
            self.assertEqual((res.ok, res.cause, res.uncertain), (False, "tmux_unavailable", False))
        res = embed.paste_detailed("any", "")
        self.assertEqual((res.ok, res.cause, res.uncertain), (True, "", False))
        self.assertIsInstance(res, InjectionResult)
        ok, _cause = res  # tuple unpacking stays compatible
        self.assertTrue(ok)

    def test_paste_buffer_timeout_is_uncertain_never_retried(self):
        """paste-buffer may have delivered before the timeout → uncertain."""
        calls = []

        def _run(argv, **_kwargs):
            calls.append(argv)
            if "paste-buffer" in argv:
                raise subprocess.TimeoutExpired(argv, 1.5)
            return subprocess.CompletedProcess(args=argv, returncode=0)

        with mock.patch.object(embed.subprocess, "run", side_effect=_run):
            res = embed.paste_detailed("pane-a", "hello")
        self.assertEqual((res.ok, res.cause, res.uncertain), (False, "uncertain", True))
        # The per-call buffer is cleaned up even on failure (same unique name).
        set_buf = next(a for a in calls if a[3] == "set-buffer")
        deletes = [a for a in calls if a[3] == "delete-buffer"]
        self.assertEqual(len(deletes), 1)
        self.assertEqual(deletes[0][5], set_buf[5])

    def test_paste_missing_target_with_dead_probe_is_certain(self):
        """Authoritative target-not-found + dead probe → certain, eligible for recovery."""
        def _run(argv, **_kwargs):
            if "paste-buffer" in argv:
                return subprocess.CompletedProcess(
                    args=argv, returncode=1, stderr=b"can't find session: pane-dead"
                )
            return subprocess.CompletedProcess(args=argv, returncode=0)

        with mock.patch.object(embed.subprocess, "run", side_effect=_run):
            with mock.patch.object(embed, "pane_liveness", return_value="dead"):
                res = embed.paste_detailed("pane-dead", "hello")
                self.assertEqual((res.ok, res.cause, res.uncertain), (False, "pane_gone", False))

    def test_pane_liveness_tristate_from_real_subprocess_shapes(self):
        """Unknowns (timeouts, transport, unparseable) must never read as dead."""
        argv0 = ["tmux", "has-session", "-t", "x"]
        with mock.patch.object(
            embed.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=argv0, returncode=0),
        ):
            self.assertEqual(embed.pane_liveness("p"), "alive")
        with mock.patch.object(
            embed.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=argv0, returncode=1, stderr=b"can't find session: p"
            ),
        ):
            self.assertEqual(embed.pane_liveness("p"), "dead")
        with mock.patch.object(
            embed.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(
                args=argv0, returncode=1, stderr=b"no server running on /tmp/x"
            ),
        ):
            self.assertEqual(embed.pane_liveness("p"), "unknown")
        with mock.patch.object(
            embed.subprocess,
            "run",
            return_value=subprocess.CompletedProcess(args=argv0, returncode=1, stderr=b"weird"),
        ):
            self.assertEqual(embed.pane_liveness("p"), "unknown")
        # Actual timeouts and spawn failures are unknown, not dead.
        with mock.patch.object(
            embed.subprocess, "run", side_effect=subprocess.TimeoutExpired(argv0, 1.5)
        ):
            self.assertEqual(embed.pane_liveness("p"), "unknown")
        with mock.patch.object(embed.subprocess, "run", side_effect=OSError("busy")):
            self.assertEqual(embed.pane_liveness("p"), "unknown")
        with mock.patch.object(embed.subprocess, "run", side_effect=FileNotFoundError()):
            self.assertEqual(embed.pane_liveness("p"), "unknown")

    def test_send_key_timeout_is_uncertain(self):
        with mock.patch.object(
            embed, "_active_channel", return_value=None
        ), mock.patch.object(
            embed.subprocess, "run", side_effect=subprocess.TimeoutExpired([], 1.5)
        ):
            res = embed.send_key_detailed("pane-a", "Enter")
            self.assertEqual((res.ok, res.cause, res.uncertain), (False, "uncertain", True))

    def test_pane_state_parses_formats(self):
        # (光标 x, 光标 y, 光标可见, 程序申请鼠标, SGR 鼠标模式, 回滚行数, 宽, 高)
        with mock.patch.object(embed.subprocess, "check_output", return_value=b"12|7|1|1|1|234|80|24\n"):
            self.assertEqual(embed.pane_state("s"), (12, 7, True, True, True, 234, 80, 24))
        with mock.patch.object(embed.subprocess, "check_output", return_value=b"0|0|0|0|0|0|40|10\n"):
            self.assertEqual(embed.pane_state("s"), (0, 0, False, False, False, 0, 40, 10))
        # 旧版 6 段输出（解析失败兜底 None，不崩）
        with mock.patch.object(embed.subprocess, "check_output", return_value=b"12|7|1|1|1|234\n"):
            self.assertIsNone(embed.pane_state("s"))
        # 更旧的 5 段输出同样拒绝
        with mock.patch.object(embed.subprocess, "check_output", return_value=b"12|7|1|1|1\n"):
            self.assertIsNone(embed.pane_state("s"))

    def test_pane_state_none_on_failure(self):
        with mock.patch.object(embed.subprocess, "check_output",
                               side_effect=subprocess.CalledProcessError(1, [])):
            self.assertIsNone(embed.pane_state("s"))
        with mock.patch.object(embed.subprocess, "check_output", return_value=b"garbage"):
            self.assertIsNone(embed.pane_state("s"))

    def test_normalize_and_guard_host_size(self):
        self.assertEqual(embed.normalize_host_size(10, 3), (40, 10))
        self.assertEqual(embed.normalize_host_size(120, 40), (120, 40))
        self.assertTrue(embed.should_resize_host(40, 10))
        self.assertFalse(embed.should_resize_host(39, 20))
        self.assertFalse(embed.should_resize_host(80, 9))

    def test_resize_skips_below_minimum(self):
        with mock.patch.object(embed.subprocess, "run") as run:
            embed.resize("corral-claude-x", 20, 18)
        run.assert_not_called()

    def test_tmux_config_uses_manual_window_size(self):
        from corral import keepalive

        config = keepalive._tmux_config()
        # Startup ``set -g window-size manual`` crashes Ubuntu tmux 3.4; use a
        # post-create hook instead (plus embed._ensure_manual_window_size).
        self.assertNotRegex(config, r"(?m)^set -g window-size manual$")
        self.assertIn('set-hook -g after-new-session "set -g window-size manual"', config)
        self.assertNotIn("set -g window-size latest", config)
        self.assertRegex(config, r'set -g default-terminal "[^"]+"')

    def test_resolve_default_terminal_prefers_known_entry(self):
        from corral import keepalive

        term = keepalive._resolve_default_terminal()
        self.assertIn(term, {
            "tmux-256color", "screen-256color", "xterm-256color", "screen",
        })

    def test_default_terminal_probe_runs_once_per_process(self):
        """Every host_session/wrap_plan builds the config; it must not fork infocmp each time."""
        from corral import keepalive

        keepalive._resolve_default_terminal.cache_clear()
        self.addCleanup(keepalive._resolve_default_terminal.cache_clear)
        with mock.patch.object(keepalive, "_terminfo_exists", return_value=True) as probe:
            for _ in range(3):
                keepalive._tmux_config()
        self.assertEqual(probe.call_count, 1)

    def test_tmux_env_strips_ld_library_path(self):
        """setup-python on Linux sets LD_LIBRARY_PATH; tmux must not inherit it."""
        from corral import keepalive

        poisoned = {
            **os.environ,
            "LD_LIBRARY_PATH": "/opt/hostedtoolcache/Python/3.11.16/x64/lib",
            "RUNNER_TRACKING_ID": "github_abc",
            "CORRAL_TMUX_ENV_PROBE": "1",
            "LANG": "C",
        }
        env = keepalive.tmux_env(poisoned)
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertNotIn("RUNNER_TRACKING_ID", env)
        self.assertEqual(env.get("TMUX_TMPDIR"), "/tmp")
        self.assertIn("UTF-8", (env.get("LANG") or "").upper())
        self.assertEqual(env.get("CORRAL_TMUX_ENV_PROBE"), "1")

    def test_ensure_manual_window_size_retries_after_failure(self):
        embed._manual_window_size_sockets.clear()
        failed = mock.Mock(returncode=1)
        ok = mock.Mock(returncode=0)
        with mock.patch.object(embed.subprocess, "run", side_effect=[failed, ok]) as run:
            embed._ensure_manual_window_size("corral-claude-retry")
            self.assertEqual(run.call_count, 1)
            embed._ensure_manual_window_size("corral-claude-retry")
            self.assertEqual(run.call_count, 2, "失败不得记住 socket，否则永远不再改 window-size")
            embed._ensure_manual_window_size("corral-claude-retry")
            self.assertEqual(run.call_count, 2, "成功后才缓存")
        embed._manual_window_size_sockets.clear()


class HostViewRegistryTests(unittest.TestCase):
    """较窄观看方不得压窄共享画面：有效尺寸取仍存活观看方的最大宽。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("CORRAL_CACHE_DIR")
        os.environ["CORRAL_CACHE_DIR"] = self._tmp.name
        with embed._VIEWER_LOCK:
            embed._viewer_last_write.clear()

    def tearDown(self) -> None:
        with embed._VIEWER_LOCK:
            embed._viewer_last_write.clear()
        if self._old is None:
            os.environ.pop("CORRAL_CACHE_DIR", None)
        else:
            os.environ["CORRAL_CACHE_DIR"] = self._old
        self._tmp.cleanup()

    def test_desired_host_size_prefers_widest_live_viewer(self) -> None:
        pid = os.getpid()
        name = "corral-claude-wide"
        wide = embed.desired_host_size(name, f"{pid}:wide", 240, 50)
        self.assertEqual(wide, (240, 50))
        narrow = embed.desired_host_size(name, f"{pid}:narrow", 80, 24)
        self.assertEqual(narrow, (240, 50), "较窄方必须跟最宽观看方，不能把窗压成 80 列")

    def test_release_host_view_lets_remaining_viewer_shrink(self) -> None:
        pid = os.getpid()
        name = "corral-claude-release"
        embed.desired_host_size(name, f"{pid}:wide", 240, 50)
        embed.desired_host_size(name, f"{pid}:narrow", 80, 24)
        embed.release_host_view(name, f"{pid}:wide")
        remaining = embed.desired_host_size(name, f"{pid}:narrow", 80, 24)
        self.assertEqual(remaining, (80, 24))

    def test_dead_pid_claims_are_ignored(self) -> None:
        pid = os.getpid()
        name = "corral-claude-dead"
        conn = embed._connect_host_viewers()
        self.assertIsNotNone(conn)
        assert conn is not None
        conn.execute(
            "INSERT INTO host_viewers"
            " (session_name, viewer_id, width, height, pid, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (name, "999999:gone", 240, 50, 999999, time.monotonic()),
        )
        conn.commit()
        conn.close()
        mine = embed.desired_host_size(name, f"{pid}:mine", 80, 24)
        self.assertEqual(mine, (80, 24))

    def test_below_min_withdraws_prior_wide_vote(self) -> None:
        """Shrinking below MIN must not keep pinning the previous wide claim."""
        pid = os.getpid()
        name = "corral-claude-below-min"
        wide = embed.desired_host_size(name, f"{pid}:only", 60, 26)
        self.assertEqual(wide, (60, 26))
        narrow = embed.desired_host_size(
            name, f"{pid}:only", embed.MIN_HOST_WIDTH - 1, 26,
        )
        self.assertEqual(
            narrow,
            (embed.MIN_HOST_WIDTH - 1, 26),
            "below-min request must withdraw the prior vote, not return the old width",
        )


class ImagePasteTests(unittest.TestCase):
    """浏览器增强脚本裹哨兵的图片粘贴：识别、落盘、送路径进 pane。"""

    def test_extract_pasted_image_roundtrip(self):
        raw = b"\xff\xd8\xfffake-jpeg-bytes"
        b64 = base64.b64encode(raw).decode()
        wrapped = f"␞CORRAL_IMG_BEGIN␞{b64}␞CORRAL_IMG_END␞"
        self.assertEqual(embed.extract_pasted_image(wrapped), raw)

    def test_extract_pasted_image_accepts_pickup_sentinel(self):
        raw = b"\xff\xd8\xfffake-jpeg-bytes"
        b64 = base64.b64encode(raw).decode()
        wrapped = f"␞PICKUP_IMG_BEGIN␞{b64}␞PICKUP_IMG_END␞"
        self.assertEqual(embed.extract_pasted_image(wrapped), raw)

    def test_extract_pasted_image_none_for_plain_text(self):
        self.assertIsNone(embed.extract_pasted_image("普通粘贴文本"))
        self.assertIsNone(embed.extract_pasted_image(""))

    def test_extract_pasted_image_none_for_bad_base64(self):
        wrapped = "␞CORRAL_IMG_BEGIN␞***not-base64***␞CORRAL_IMG_END␞"
        self.assertIsNone(embed.extract_pasted_image(wrapped))

    def test_extract_pasted_image_rejects_non_image_payload(self):
        raw = b"not-an-image"
        b64 = base64.b64encode(raw).decode()
        wrapped = f"␞CORRAL_IMG_BEGIN␞{b64}␞CORRAL_IMG_END␞"
        self.assertIsNone(embed.extract_pasted_image(wrapped))

    def test_extract_pasted_image_rejects_oversized_payload(self):
        # 只撑大 base64 文本长度，避免真的分配 8MB+ 解码缓冲
        payload = "A" * (embed._MAX_IMAGE_B64_CHARS + 1)
        wrapped = f"␞CORRAL_IMG_BEGIN␞{payload}␞CORRAL_IMG_END␞"
        self.assertIsNone(embed.extract_pasted_image(wrapped))

    def test_pane_cwd_parses_display_message(self):
        with mock.patch.object(embed.subprocess, "check_output", return_value=b"/home/demo/proj\n"):
            self.assertEqual(embed._pane_cwd("s"), "/home/demo/proj")

    def test_pane_cwd_none_on_failure(self):
        with mock.patch.object(embed.subprocess, "check_output",
                               side_effect=subprocess.CalledProcessError(1, [])):
            self.assertIsNone(embed._pane_cwd("s"))

    def test_save_image_and_paste_path_uses_pane_cwd(self):
        tmp_root = tempfile.mkdtemp()
        try:
            calls = []

            def check_output_side_effect(argv, **_kwargs):
                return (tmp_root + "\n").encode()

            def run_side_effect(argv, **_kwargs):
                calls.append(argv)
                return subprocess.CompletedProcess(args=argv, returncode=0)

            with mock.patch.object(embed.subprocess, "check_output",
                                   side_effect=check_output_side_effect), \
                    mock.patch.object(embed.subprocess, "run", side_effect=run_side_effect):
                path = embed.save_image_and_paste_path("s", b"\xff\xd8\xff-fake")
            self.assertIsNotNone(path)
            self.assertTrue(path.startswith(tmp_root))
            self.assertTrue(path.endswith(".jpg"))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"\xff\xd8\xff-fake")
            # 落盘后应经 paste() 把路径送进 pane（set-buffer + paste-buffer）
            self.assertTrue(any("set-buffer" in c for c in calls))
        finally:
            shutil.rmtree(tmp_root, ignore_errors=True)

    def test_save_image_and_paste_path_falls_back_to_tempdir(self):
        with mock.patch.object(embed.subprocess, "check_output",
                               side_effect=subprocess.CalledProcessError(1, [])), \
                mock.patch.object(embed.subprocess, "run", side_effect=_run_completed_ok):
            path = embed.save_image_and_paste_path("s", b"\xff\xd8\xffdata")
        try:
            self.assertIsNotNone(path)
            self.assertTrue(path.startswith(tempfile.gettempdir()))
            self.assertTrue(path.endswith(".jpg"))
        finally:
            if path:
                os.remove(path)

    def test_save_image_and_paste_path_rejects_unknown_bytes(self):
        with mock.patch.object(embed.subprocess, "run", side_effect=_run_completed_ok):
            self.assertIsNone(embed.save_image_and_paste_path("s", b"data"))


class ChannelPoolTests(unittest.TestCase):
    """控制通道按会话名池化：多分屏可同时存活，关一格不影响另一格。"""

    def setUp(self):
        embed.close_channel()

    def tearDown(self):
        embed.close_channel()

    @staticmethod
    def _fake_channel(session_name: str):
        ch = mock.Mock()
        ch.dead = False
        ch.name = session_name
        return ch

    def test_open_keeps_independent_channels(self):
        with mock.patch.object(embed, "ControlChannel") as CC:
            ch_a = self._fake_channel("sess-a")
            ch_b = self._fake_channel("sess-b")
            CC.side_effect = [ch_a, ch_b]
            a = embed.open_channel("sess-a")
            b = embed.open_channel("sess-b")
            self.assertIs(a, ch_a)
            self.assertIs(b, ch_b)
            self.assertEqual(CC.call_count, 2)
            # 同名复用，不新建
            self.assertIs(embed.open_channel("sess-a"), ch_a)
            self.assertEqual(CC.call_count, 2)

    def test_pool_evicts_least_recently_used_over_cap(self):
        """通道池必须有上限：右栏改成就地改绑后，切走的格子不再卸载、也就不再
        顺手关掉自己的通道，没有上限的话在侧边栏一路翻下去会攒出几十个
        `tmux -C attach` 子进程。淘汰按最久未用，正在用的会被抓帧不断续期。"""
        names = [f"sess-{i}" for i in range(embed._MAX_CHANNELS + 2)]
        with mock.patch.object(embed, "ControlChannel") as CC:
            CC.side_effect = [self._fake_channel(n) for n in names]
            for name in names:
                embed.open_channel(name)
                # 每次开完都摸一下第一条，让它成为「最近用过」的那条
                embed._active_channel(names[0])
        self.assertLessEqual(len(embed._channels), embed._MAX_CHANNELS)
        self.assertIn(names[0], embed._channels, "一直在用的通道不该被淘汰")
        self.assertNotIn(names[1], embed._channels, "最久未用的通道应被关掉")
        self.assertIn(names[-1], embed._channels, "刚打开的通道必须留下")

    def test_close_one_leaves_other_alive(self):
        with mock.patch.object(embed, "ControlChannel") as CC:
            ch_a = self._fake_channel("sess-a")
            ch_b = self._fake_channel("sess-b")
            CC.side_effect = [ch_a, ch_b]
            embed.open_channel("sess-a")
            embed.open_channel("sess-b")
            embed.close_channel("sess-a")
            ch_a.close.assert_called_once()
            ch_b.close.assert_not_called()
            self.assertIs(embed.open_channel("sess-b"), ch_b)
            self.assertEqual(CC.call_count, 2)

    def test_close_all_clears_pool(self):
        with mock.patch.object(embed, "ControlChannel") as CC:
            ch_a = self._fake_channel("sess-a")
            ch_b = self._fake_channel("sess-b")
            ch_a2 = self._fake_channel("sess-a")
            CC.side_effect = [ch_a, ch_b, ch_a2]
            embed.open_channel("sess-a")
            embed.open_channel("sess-b")
            embed.close_channel()
            ch_a.close.assert_called_once()
            ch_b.close.assert_called_once()
            # 池已空，再 open 会新建
            embed.open_channel("sess-a")
            self.assertEqual(CC.call_count, 3)


class ControlModeTests(unittest.TestCase):
    """控制模式转义、SGR 鼠标序列、tmux 版本门控、copy-mode 原语（无通道时走 fork）。"""

    def setUp(self):
        embed._tmux_version.cache_clear()  # lru_cache 跨用例污染
        self.addCleanup(embed._tmux_version.cache_clear)

    def test_ctl_quote_clean_ascii_passthrough(self):
        self.assertEqual(embed._ctl_quote("send-keys"), "send-keys")
        self.assertEqual(embed._ctl_quote("%1"), "%1")
        self.assertEqual(embed._ctl_quote("-t"), "-t")

    def test_ctl_quote_escapes_specials(self):
        self.assertEqual(embed._ctl_quote('a "b"'), '"a \\"b\\""')
        self.assertEqual(embed._ctl_quote("$HOME"), '"\\$HOME"')
        self.assertEqual(embed._ctl_quote("a`b"), '"a\\`b"')
        self.assertEqual(embed._ctl_quote("a\\b"), '"a\\\\b"')
        self.assertEqual(embed._ctl_quote(" "), '" "')  # 空格不在安全集，需包裹

    def test_ctl_quote_rejects_newlines(self):
        with self.assertRaises(embed.ControlQuoteError):
            embed._ctl_quote("Enter\nrun-shell id")
        with self.assertRaises(embed.ControlQuoteError):
            embed._ctl_quote("a\rb")

    def test_sgr_mouse_sequence(self):
        self.assertEqual(embed.sgr_mouse_sequence(64, 5, 3), "\x1b[<64;5;3M")
        self.assertEqual(embed.sgr_mouse_sequence(65, 1, 1), "\x1b[<65;1;1M")

    def test_send_mouse_sequence_nonblocking_and_forked(self):
        """滚轮序列必须排队到后台线程发送：调用方（UI 主线程）零 fork、立即返回。"""
        delivered = threading.Event()
        calls = []

        def fake_send(name, seq, *, force_fork=False):
            calls.append((name, seq, force_fork))
            delivered.set()

        with mock.patch.object(embed, "send_literal", side_effect=fake_send):
            started = time.monotonic()
            embed.send_mouse_sequence("sc-claude-1", "\x1b[<65;10;6M")
            elapsed = time.monotonic() - started
            self.assertTrue(delivered.wait(2.0), "后台线程应投递滚轮序列")
        self.assertLess(elapsed, 0.05, "send_mouse_sequence 自身不得阻塞调用方")
        self.assertEqual(calls, [("sc-claude-1", "\x1b[<65;10;6M", True)])

    def test_send_mouse_sequence_queue_cap_drops_oldest(self):
        """积压超过 _WHEEL_QUEUE_MAX 时丢弃最旧事件：内层重绘不过来就跳中间帧，
        不能停手后还在追滚动。"""
        gate = threading.Event()
        drained = threading.Event()
        delivered = []

        def fake_send(name, seq, *, force_fork=False):
            if not delivered:
                gate.wait(2.0)  # 第一条卡住发送线程，模拟内层程序重绘慢
            delivered.append(seq)
            if seq == "seq29":
                drained.set()

        with mock.patch.object(embed, "send_literal", side_effect=fake_send), \
                mock.patch.object(embed, "_WHEEL_SEND_INTERVAL", 0):
            embed.send_mouse_sequence("sc-claude-1", "first")
            # 等发送线程取走 first（队列清空即说明已进入发送、卡在 gate 上）
            for _ in range(200):
                with embed._wheel_lock:
                    if not embed._wheel_queues.get("sc-claude-1"):
                        break
                time.sleep(0.01)
            else:
                self.fail("发送线程未取走第一条")
            time.sleep(0.05)  # 留出「取走 → 进入 fake_send 卡 gate」的窗口
            for i in range(30):
                embed.send_mouse_sequence("sc-claude-1", f"seq{i}")
            gate.set()
            self.assertTrue(drained.wait(5.0), "队列应在放行后全部投递")
        # first + 队列上限 12 条（seq18..seq29，最旧的 seq0..seq17 被丢弃）
        self.assertEqual(delivered, ["first"] + [f"seq{i}" for i in range(18, 30)])

    def test_supports_theme_report_version_gate(self):
        for ver, expected in ((b"tmux 3.5a\n", True), (b"tmux 3.4\n", False),
                              (b"tmux next-3.7\n", True), (b"tmux 2.9\n", False)):
            embed._tmux_version.cache_clear()
            with mock.patch.object(embed.subprocess, "check_output", return_value=ver):
                self.assertEqual(embed.supports_theme_report(), expected, ver)

    def test_supports_theme_report_false_when_tmux_missing(self):
        with mock.patch.object(embed.subprocess, "check_output",
                               side_effect=FileNotFoundError()):
            self.assertFalse(embed.supports_theme_report())

    def test_capture_scroll_offset_inserts_range_flags(self):
        captured = {}

        def check_output_side_effect(argv, **kwargs):
            captured["argv"] = argv
            return b"screen text"

        with mock.patch.object(embed.subprocess, "check_output",
                               side_effect=check_output_side_effect):
            # 应用层滚动：offset>0 时抓「live 窗口上移 offset 行」的历史窗口，
            # 公式 -S -offset -E (h-1-offset)（真 tmux 实测钉死）；
            # copy-mode 滚动对 capture 不可见（实测），不能用
            self.assertEqual(embed.capture("s", scroll_offset=10, pane_height=40),
                             "screen text")
        argv = captured["argv"]
        # -S/-E 现在跟在 -p -e 之后、-t name 之前（原实现插在 capture-pane 与
        # -p 之间）——tmux 对 capture-pane 的标志解析不依赖顺序，位置调整是路由
        # 到 ControlChannel.request() 时统一构造 args 列表的自然结果，不是回归。
        self.assertEqual(argv[6:10], ["-S", "-10", "-E", "29"])

    def test_capture_live_without_range_flags(self):
        captured = {}

        def check_output_side_effect(argv, **kwargs):
            captured["argv"] = argv
            return b"x"

        with mock.patch.object(embed.subprocess, "check_output",
                               side_effect=check_output_side_effect):
            embed.capture("s")
        self.assertNotIn("-S", captured["argv"])


class ControlChannelProtocolTests(unittest.TestCase):
    """用可控 fake 管道钉死控制模式握手、响应配对和关闭时序。"""

    @staticmethod
    def _build_channel(process, on_output=None):
        with mock.patch.object(embed.subprocess, "Popen", return_value=process), \
                mock.patch.object(embed.ControlChannel, "_query_pane_id", return_value="%1"):
            return embed.ControlChannel("fake-session", on_output=on_output)

    @staticmethod
    def _wait_until(predicate, timeout=1.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.005)
        raise AssertionError("等待控制通道状态收敛超时")

    def test_constructor_waits_until_attach_startup_guard_is_drained(self):
        process = _FakeControlProcess(startup_ready=False)
        result = []
        errors = []

        def build():
            try:
                result.append(self._build_channel(process))
            except Exception as exc:  # 测试线程的异常要带回主线程断言
                errors.append(exc)

        thread = threading.Thread(target=build)
        thread.start()
        time.sleep(0.05)
        self.assertTrue(thread.is_alive(), "attach 启动响应未结束前构造函数不得返回")
        process.stdout.feed("%begin 1 1\n")
        process.stdout.feed("%end 1 1\n")
        thread.join(1.0)
        self.assertFalse(thread.is_alive())
        self.assertFalse(errors)
        self.assertEqual(len(result), 1)
        result[0].close()

    def test_fast_response_cannot_arrive_before_waiter_registration(self):
        holder = {}
        process = _FakeControlProcess()

        def respond(_data):
            channel = holder["channel"]
            # write 回调发生在 flush 之前；此时 waiter 必须已经登记在 FIFO。
            self.assertEqual(len(channel._pending), 1)
            process.stdout.feed("%begin 2 1\n")
            process.stdout.feed("FAST\n")
            process.stdout.feed("%end 2 1\n")

        channel = self._build_channel(process)
        holder["channel"] = channel
        process.stdin.on_write = respond
        try:
            self.assertEqual(channel.request("display-message", "-p", "ok"), ["FAST"])
            self.assertFalse(channel._pending)
            self.assertIsNone(channel._active_waiter)
        finally:
            channel.close()

    def test_async_output_inside_response_is_not_returned_as_body(self):
        fired = threading.Event()
        process = _FakeControlProcess()

        def respond(_data):
            process.stdout.feed("%begin 3 1\n")
            process.stdout.feed("%output %1 pane-data\n")
            process.stdout.feed("BODY\n")
            process.stdout.feed("%end 3 1\n")

        process.stdin.on_write = respond
        channel = self._build_channel(process, on_output=fired.set)
        try:
            self.assertEqual(channel.request("capture-pane", "-p"), ["BODY"])
            self.assertTrue(fired.wait(1.0), "%output 应触发画面刷新回调")
        finally:
            channel.close()

    def test_request_timeout_closes_channel_and_clears_waiters(self):
        process = _FakeControlProcess()
        channel = self._build_channel(process)
        self.assertIsNone(channel.request("capture-pane", "-p", timeout=0.01))
        self.assertTrue(channel.dead)
        self.assertTrue(channel._closed)
        self.assertFalse(channel._pending)
        self.assertIsNone(channel._active_waiter)
        # close() 只同步标记死亡并唤醒请求方，子进程回收走后台线程：
        # 这里等回收线程收完再断言资源（生产路径不能同步等，见 close 文档）。
        self.assertTrue(channel.wait_reaped())
        self.assertTrue(process.waited)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)

    def test_close_wakes_active_and_queued_requests_and_is_idempotent(self):
        process = _FakeControlProcess()
        writes = 0

        def respond(_data):
            nonlocal writes
            writes += 1
            if writes == 1:
                # 第一条只给 begin、不结束，让 reader 持有 active waiter。
                process.stdout.feed("%begin 4 1\n")

        process.stdin.on_write = respond
        channel = self._build_channel(process)
        results = {}

        def request(key):
            results[key] = channel.request("capture-pane", key, timeout=5.0)

        first = threading.Thread(target=request, args=("first",))
        second = threading.Thread(target=request, args=("second",))
        first.start()
        self._wait_until(lambda: channel._active_waiter is not None)
        second.start()
        self._wait_until(lambda: len(channel._pending) == 1)

        channel.close()
        channel.close()
        first.join(1.0)
        second.join(1.0)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(results, {"first": None, "second": None})
        self.assertFalse(channel._pending)
        self.assertIsNone(channel._active_waiter)
        # 回收走后台线程：等收完再断言资源（见上条用例注释）。
        self.assertTrue(channel.wait_reaped())
        self.assertIsNotNone(process.poll())
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)


class TranslateTextualKeyTests(unittest.TestCase):
    def test_enter_tab_backspace_escape(self):
        self.assertEqual(embed.translate_textual_key("enter"), ("keys", "Enter"))
        self.assertEqual(embed.translate_textual_key("return"), ("keys", "Enter"))
        self.assertEqual(embed.translate_textual_key("tab"), ("keys", "Tab"))
        self.assertEqual(embed.translate_textual_key("backspace"), ("keys", "BSpace"))
        self.assertEqual(embed.translate_textual_key("escape"), ("keys", "Escape"))

    def test_shift_tab_maps_to_btab(self):
        # tmux 没有 S-Tab：Shift+Tab 在终端里是 backtab，tmux 的具名键是 BTab
        # （tmux(1) 手册）。Claude Code 用这个键循环 plan/权限模式，漏译此前
        # 会在内嵌面板里表现为「按了没反应」（真实缺口回归）。
        self.assertEqual(embed.translate_textual_key("shift+tab"), ("keys", "BTab"))

    def test_control_letters(self):
        self.assertEqual(embed.translate_textual_key("ctrl+c"), ("keys", "C-c"))
        self.assertEqual(embed.translate_textual_key("ctrl+z"), ("keys", "C-z"))
        self.assertEqual(embed.translate_textual_key("ctrl+a"), ("keys", "C-a"))
        self.assertEqual(embed.translate_textual_key("ctrl+t"), ("keys", "C-t"))

    def test_control_underscore_and_slash_aliases(self):
        # 多数终端 Ctrl+/ ≡ Ctrl+_（ASCII 0x1F）；tmux 不认 C-/，统一成 C-_。
        # Claude Code 撤销输入就绑在这上面——以前只放行 Ctrl+字母会静默丢键。
        self.assertEqual(embed.translate_textual_key("ctrl+underscore"), ("keys", "C-_"))
        self.assertEqual(embed.translate_textual_key("ctrl+slash"), ("keys", "C-_"))
        self.assertEqual(embed.translate_textual_key("ctrl+/"), ("keys", "C-_"))
        self.assertEqual(embed.translate_textual_key("ctrl+minus"), ("keys", "C-_"))

    def test_modifier_arrows_and_meta(self):
        # 「其余一律放行」：带修饰的方向键 / Alt 字母必须能译出，不能再返回 None。
        self.assertEqual(embed.translate_textual_key("shift+up"), ("keys", "S-Up"))
        self.assertEqual(embed.translate_textual_key("ctrl+up"), ("keys", "C-Up"))
        self.assertEqual(embed.translate_textual_key("ctrl+shift+up"), ("keys", "C-S-Up"))
        self.assertEqual(embed.translate_textual_key("alt+b"), ("keys", "M-b"))
        self.assertEqual(embed.translate_textual_key("ctrl+shift+minus"), ("keys", "C-S--"))

    def test_special_keys(self):
        self.assertEqual(embed.translate_textual_key("up"), ("keys", "Up"))
        self.assertEqual(embed.translate_textual_key("pageup"), ("keys", "PPage"))
        self.assertEqual(embed.translate_textual_key("f5"), ("keys", "F5"))
        self.assertEqual(embed.translate_textual_key("delete"), ("keys", "DC"))
        self.assertEqual(embed.translate_textual_key("insert"), ("keys", "IC"))

    def test_untranslatable(self):
        self.assertIsNone(embed.translate_textual_key("x"))
        self.assertIsNone(embed.translate_textual_key("shift+unknown_key"))


class ParseScreenTests(unittest.TestCase):
    def test_native_rows_match_python_parser(self):
        """原生热路径必须与逐格 Python 参考实现保持完全相同的可见结果。"""
        cases = (
            ("abc", 5, 2),
            ("a\x1b[1;31mb\x1b[0mc", 5, 1),
            ("\x1b[38;5;200mx\x1b[48;2;255;0;0my", 3, 1),
            ("a好🙂e\u0301x", 9, 1),
            ("a\x1b[2Kb\x1b(Bc", 5, 1),
            ("\x1b[31m\x1b]8;;https://example.com\x07Click Me\x1b]8;;\x07\x1b[0m!", 12, 1),
            ("\x1b]8;;file:///tmp/a\x1b\\a\x1b]8;;\x1b\\b", 5, 1),
            ("\x9d8;;file:///tmp/a\x9ca\x9d8;;\x9cb", 5, 1),
            ("\x9b31mred\x9b0m", 5, 1),
            ("ab\x1b]8;;https://example.com", 6, 1),
        )
        for screen, width, height in cases:
            with self.subTest(screen=screen, width=width, height=height):
                expected = [embed.row_text_and_spans(row) for row in embed.parse_screen(
                    screen, width, height,
                )]
                actual = embed.parse_screen_rows(screen, width, height)
                self.assertEqual(
                    [(row.text, list(row.spans)) for row in actual],
                    expected,
                )

    def test_plain_text_and_padding(self):
        grid = embed.parse_screen("abc", 5, 2)
        self.assertEqual("".join(c.ch for c in grid[0]), "abc  ")
        self.assertEqual("".join(c.ch for c in grid[1]), "     ")
        self.assertEqual(len(grid), 2)

    def test_basic_sgr_color_and_reset(self):
        grid = embed.parse_screen("a\x1b[1;31mb\x1b[0mc", 3, 1)
        row = grid[0]
        self.assertEqual((row[0].ch, row[0].fg, row[0].bold), ("a", -1, False))
        self.assertEqual((row[1].ch, row[1].fg, row[1].bold), ("b", 1, True))
        self.assertEqual((row[2].ch, row[2].fg, row[2].bold), ("c", -1, False))

    def test_256_and_truecolor(self):
        grid = embed.parse_screen("\x1b[38;5;200mx\x1b[48;2;255;0;0my", 2, 1)
        self.assertEqual(grid[0][0].fg, 200)
        self.assertEqual(grid[0][1].bg, (255, 0, 0))  # 真彩色原样保留，不再量化

    def test_bright_colors_and_reverse(self):
        grid = embed.parse_screen("\x1b[92;7mz", 1, 1)
        self.assertEqual(grid[0][0].fg, 10)
        self.assertTrue(grid[0][0].reverse)

    def test_wide_char_occupies_two_cells(self):
        grid = embed.parse_screen("a好b", 4, 1)
        row = grid[0]
        self.assertEqual(row[1].ch, "好")
        self.assertFalse(row[1].wide_cont)
        self.assertTrue(row[2].wide_cont)
        self.assertEqual(row[3].ch, "b")

    def test_wide_char_cut_at_right_edge_becomes_blank(self):
        grid = embed.parse_screen("ab好", 3, 1)
        row = grid[0]
        self.assertEqual((row[0].ch, row[1].ch, row[2].ch), ("a", "b", " "))

    def test_combining_char_merges_into_previous_cell(self):
        grid = embed.parse_screen("éx", 2, 1)
        self.assertEqual(grid[0][0].ch, "é")
        self.assertEqual(grid[0][1].ch, "x")

    def test_spacing_mark_width_matches_rich_renderer(self):
        # Devanagari 的 Mc 附标在 Rich/Textual 宽度表中是零宽；若用
        # unicodedata.east_asian_width 自行判定会误算成 1 列，导致后续
        # 文本在内嵌终端的解析位置与 Textual 真实绘制位置错开。
        grid = embed.parse_screen("काx", 2, 1)
        self.assertEqual(grid[0][0].ch, "का")
        self.assertEqual(grid[0][1].ch, "x")

    def test_non_sgr_sequences_are_skipped(self):
        grid = embed.parse_screen("a\x1b[2Kb\x1b(Bc", 3, 1)
        self.assertEqual("".join(c.ch for c in grid[0]), "abc")

    def test_osc8_hyperlink_payload_is_discarded(self):
        """OSC 8 超链接只保留可见文字：漏掉这一支时链接前后会各粘一串 `8;;`。

        BEL 与 ST 两种终止写法都要覆盖——agent CLI 两种都在用，tmux
        capture-pane -e 原样透传。
        """
        for terminator in ("\x07", "\x1b\\"):
            with self.subTest(terminator=terminator):
                line = (f"\x1b]8;;https://example.com{terminator}"
                        f"Click Me\x1b]8;;{terminator}!")
                grid = embed.parse_screen(line, 9, 1)
                self.assertEqual("".join(c.ch for c in grid[0]), "Click Me!")

    def test_osc8_hyperlink_keeps_surrounding_sgr(self):
        """超链接夹在 SGR 之间时，颜色状态不能被跳过逻辑吃掉或错位。"""
        grid = embed.parse_screen(
            "\x1b[31m\x1b]8;;file:///tmp/a.txt\x07a.txt\x1b]8;;\x07\x1b[0mx", 6, 1,
        )
        self.assertEqual("".join(c.ch for c in grid[0]), "a.txtx")
        self.assertEqual([c.fg for c in grid[0][:5]], [1] * 5)
        self.assertEqual(grid[0][5].fg, -1)

    def test_osc8_c1_form_is_discarded(self):
        """ECMA-48 的 8-bit OSC/ST 形式也不能把 `8;;` 与地址画进正文。"""
        line = "\x9d8;;file:///tmp/report.md\x9creport.md\x9d8;;\x9c done"
        grid = embed.parse_screen(line, 14, 1)
        self.assertEqual("".join(c.ch for c in grid[0]), "report.md done")

    def test_c1_csi_keeps_sgr_semantics(self):
        """8-bit CSI 与 ESC [ 等价，不能把样式参数画成正文。"""
        grid = embed.parse_screen("\x9b31mred\x9b0mx", 4, 1)
        self.assertEqual("".join(c.ch for c in grid[0]), "redx")
        self.assertEqual([c.fg for c in grid[0][:3]], [1] * 3)
        self.assertEqual(grid[0][3].fg, -1)

    def test_unterminated_string_sequence_drops_rest_of_line(self):
        """本行内没有终止符时整段丢弃，不能把载荷当正文画出来。"""
        grid = embed.parse_screen("ab\x1b]8;;https://example.com", 4, 1)
        self.assertEqual("".join(c.ch for c in grid[0]), "ab  ")

    def test_height_truncation(self):
        grid = embed.parse_screen("l1\nl2\nl3", 2, 2)
        self.assertEqual(len(grid), 2)
        self.assertEqual(grid[1][0].ch, "l")


class CellStyleTests(unittest.TestCase):
    def test_default_colors_are_none(self):
        style = embed.cell_style(embed.Cell("x"))
        self.assertIsNone(style.color)
        self.assertIsNone(style.bgcolor)

    def test_colors_map_to_ansi_256(self):
        style = embed.cell_style(embed.Cell("x", fg=200, bg=17))
        self.assertEqual(style.color.number, 200)
        self.assertEqual(style.bgcolor.number, 17)

    def test_attr_flags(self):
        style = embed.cell_style(embed.Cell("x", bold=True, dim=True, underline=True, reverse=True))
        self.assertTrue(style.bold)
        self.assertTrue(style.dim)
        self.assertTrue(style.underline)
        self.assertTrue(style.reverse)

    def test_same_combo_returns_cached_equal_style(self):
        a = embed.cell_style(embed.Cell("x", fg=1, bg=2))
        b = embed.cell_style(embed.Cell("y", fg=1, bg=2))  # ch 不同不影响样式缓存键
        self.assertEqual(a, b)


class GridToTextTests(unittest.TestCase):
    def test_merges_adjacent_cells_with_same_style_into_one_span(self):
        grid = embed.parse_screen("a\x1b[1;31mb\x1b[0mc", 3, 1)
        rows = embed.grid_to_text(grid)
        self.assertEqual(len(rows), 1)
        text, spans = rows[0]
        self.assertEqual(text, "abc")
        self.assertEqual([(s, e) for s, e, _ in spans], [(0, 1), (1, 2), (2, 3)])
        self.assertNotEqual(spans[0][2], spans[1][2])

    def test_wide_char_continuation_cell_excluded_from_text(self):
        grid = embed.parse_screen("a好b", 4, 1)
        text, spans = embed.grid_to_text(grid)[0]
        self.assertEqual(text, "a好b")
        self.assertEqual([(start, end) for start, end, _ in spans], [(0, 3)])

    def test_combining_character_keeps_following_text_and_python_span_offsets(self):
        # parse_screen 会把组合音标并入前一个 Cell.ch；span 必须按 Python 字符
        # 下标累计（e + 组合音标占 2），不能按终端 cell 数累计，否则后面的 x
        # 在 Strip 字符串切片时会被截掉。
        grid = embed.parse_screen("e\u0301\x1b[31mx", 2, 1)
        text, spans = embed.row_text_and_spans(grid[0])
        self.assertEqual(text, "e\u0301x")
        self.assertEqual([(start, end) for start, end, _ in spans], [(0, 2), (2, 3)])
        self.assertNotEqual(spans[0][2], spans[1][2])
        self.assertEqual(embed.grid_to_text(grid)[0], (text, spans))


@unittest.skipUnless(shutil.which("tmux"), "需要真实 tmux")
class ControlChannelIntegrationTests(unittest.TestCase):
    """真 tmux 上的控制通道端到端：命令下发、%output 事件、copy-mode、主题注入、死亡检测。

    用独立 socket（corral-test-ctl），与 corral-keepalive 上的真实会话完全隔离；
    通过 patch keepalive._BASE_ARGV 让 embed 的全部 tmux 调用指向测试 socket。
    """

    SOCKET = "corral-test-ctl"
    SESSION = "ctl-it"

    @classmethod
    def setUpClass(cls):
        subprocess.run(["tmux", "-L", cls.SOCKET, "kill-server"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def setUp(self):
        subprocess.run(["tmux", "-L", self.SOCKET, "kill-server"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "-L", self.SOCKET, "new-session", "-d",
                        "-s", self.SESSION, "-x", "100", "-y", "30"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        patcher = mock.patch.object(embed.keepalive, "_BASE_ARGV",
                                    ("tmux", "-L", self.SOCKET))
        patcher.start()
        self.addCleanup(patcher.stop)
        argv_patcher = mock.patch.object(
            embed.keepalive, "tmux_argv", lambda name=None: ("tmux", "-L", self.SOCKET),
        )
        argv_patcher.start()
        self.addCleanup(argv_patcher.stop)
        self.addCleanup(embed.close_channel)
        time.sleep(0.3)  # 等 shell 就绪，避免首批按键被 init 吃掉

    def tearDown(self):
        subprocess.run(["tmux", "-L", self.SOCKET, "kill-server"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _capture(self) -> str:
        return embed.capture(self.SESSION) or ""

    def _wait_text(self, needle: str, timeout: float = 4.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = self._capture()
            if needle in text:
                return text
            time.sleep(0.1)
        self.fail(f"{timeout}s 内画面未出现 {needle!r}：\n{self._capture()}")

    def test_channel_send_reaches_pane_without_fork(self):
        fired = threading.Event()
        ch = embed.open_channel(self.SESSION, on_output=fired.set)
        self.assertIsNotNone(ch)
        embed.send_literal(self.SESSION, "echo chan-$((40+2))")
        embed.send_key(self.SESSION, "Enter")
        self._wait_text("chan-42")
        self.assertFalse(ch.dead)

    def test_output_event_fires_on_pane_output(self):
        fired = threading.Event()
        embed.open_channel(self.SESSION, on_output=fired.set)
        embed.send_literal(self.SESSION, "echo hello-event")
        embed.send_key(self.SESSION, "Enter")
        self.assertTrue(fired.wait(4.0), "%output 事件应在 pane 产生输出后触发")

    def test_real_capture_hides_osc8_markers(self):
        """真实 tmux 抓帧经过界面热路径后，只留下链接的可见文件名。"""
        subprocess.run(
            ["tmux", "-L", self.SOCKET, "kill-session", "-t", self.SESSION],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        program = (
            "import os,time;"
            "os.write(1,b'\\x1b]8;;file:///tmp/report.md\\x1b\\\\"
            "report.md\\x1b]8;;\\x1b\\\\ done\\n');"
            "time.sleep(60)"
        )
        subprocess.run(
            [
                "tmux", "-L", self.SOCKET, "new-session", "-d",
                "-s", self.SESSION, "-x", "60", "-y", "20",
                sys.executable, "-c", program,
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        embed.open_channel(self.SESSION)
        raw = self._wait_text("report.md")
        visible = "\n".join(row.text for row in embed.parse_screen_rows(raw, 60, 20))
        self.assertIn("report.md done", visible)
        self.assertNotIn("8;;", visible)
        self.assertNotIn("file:///tmp/report.md", visible)

    def test_capture_scroll_offset_reads_history(self):
        """应用层滚动的真 tmux 验证：静态会话里 offset 抓到的历史窗口内容上移。

        copy-mode 滚动对 capture 不可见（scroll_position 变但 pane buffer 不变），
        内嵌滚动必须走 capture-pane -S/-E 历史窗口——本测试钉死这条路径。"""
        subprocess.run(["tmux", "-L", self.SOCKET, "kill-session", "-t", self.SESSION],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["tmux", "-L", self.SOCKET, "new-session", "-d", "-s", self.SESSION,
                        "-x", "60", "-y", "20", "seq 1 100; sleep 60"],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.2)
        live = embed.capture(self.SESSION)
        self.assertIsNotNone(live)
        self.assertIn("100", live)
        back = embed.capture(self.SESSION, scroll_offset=30, pane_height=20)
        self.assertIsNotNone(back)
        # 新公式窗口 = live(82..100) 上移 30 = 52..71
        self.assertIn("60", back, f"上滚 30 行应看到 52..71 区间内容：{back!r}")
        self.assertNotIn("100", back)
        self.assertNotIn("95", back, f"窗口上界不得超过 71：{back!r}")

    def test_resize_via_channel(self):
        embed.open_channel(self.SESSION)
        embed.resize(self.SESSION, 90, 25)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            out = subprocess.check_output(
                ["tmux", "-L", self.SOCKET, "display-message", "-p", "-t", self.SESSION,
                 "#{window_width}x#{window_height}"], timeout=2).decode().strip()
            if out == "90x25":
                return
            time.sleep(0.1)
        self.fail("resize-window 经控制通道未生效")

    def test_close_reaps_real_control_client_and_closes_pipes(self):
        ch = embed.open_channel(self.SESSION)
        self.assertIsNotNone(ch)
        process = ch._proc
        reader = ch._reader
        ch.close()
        ch.close()  # 幂等关闭不能再次操作已回收资源或抛异常

        # 回收走后台线程：等收完再断言资源（见协议用例注释）。
        self.assertTrue(ch.wait_reaped(), "真实 tmux 控制 client 必须已经 wait 回收")
        self.assertFalse(reader.is_alive())
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)

    @unittest.skipUnless(embed.supports_theme_report(), "refresh-client -r 需要 tmux 3.5a+")
    def test_report_theme_answers_pane_osc11_query(self):
        """注入 OSC 11 应答后，pane 内程序的背景色查询应拿到注入值而非超时。"""
        ch = embed.open_channel(self.SESSION)
        self.assertIsNotNone(ch.pane_id)
        # 必须用 _probe_osc_colours 的真实形态：OSC 10（前景）在前、OSC 11（背景）在后。
        # 只测单条 OSC 11 会漏掉真机 bug——tmux 只认第一条序列，整串灌进去等于只注入
        # 前景色，背景停在默认纯黑；浅色终端前景恰是黑色，agent 因此全判深色主题。
        report = b"\x1b]10;rgb:0000/0000/0000\x07\x1b]11;rgb:abcd/1234/5678\x07"
        self.assertTrue(embed.report_theme(ch, report))
        probe = ("python3 -c 'import os,sys,termios,tty,select;"
                 "fd=sys.stdin.fileno();old=termios.tcgetattr(fd);tty.setraw(fd);"
                 "os.write(1,b\"\\x1b]11;?\\x07\");"
                 "r,_,_=select.select([fd],[],[],2.5);"
                 "d=os.read(fd,64) if r else b\"TIMEOUT\";"
                 "termios.tcsetattr(fd,termios.TCSADRAIN,old);"
                 "print(\"RESP\",repr(d))'")
        embed.send_literal(self.SESSION, probe)
        embed.send_key(self.SESSION, "Enter")
        # 等 "RESP b'"（python 输出行的特征前缀）：直接等 "RESP" 会匹配到命令行
        # 回显里 print("RESP",...) 的字样，在命令尚未执行完时就提前返回
        text = self._wait_text("RESP b'", timeout=8.0)
        # tmux 把注入的 16-bit RGB 归一化成高 8 位重复格式（abcd→abab/1212/5656），
        # 断言归一化后的值；TIMEOUT 出现则说明 pane 内查询无人应答（机制失效）
        self.assertIn("abab/1212/5656", text, f"pane 内 OSC 11 应答应为注入值：{text!r}")

    def test_channel_death_falls_back_to_fork(self):
        ch = embed.open_channel(self.SESSION)
        ch.close()
        embed.close_channel()
        # 通道死亡后 send_literal 应自动回退外部 fork 路径，文本依然到达
        embed.send_literal(self.SESSION, "echo fork-$((1+1))")
        embed.send_key(self.SESSION, "Enter")
        self._wait_text("fork-2")

    @unittest.skipUnless(embed.supports_theme_report(), "refresh-client -r 需要 tmux 3.5a+")
    def test_host_session_with_osc_report_answers_program_own_first_query(self):
        """回归测试：真机排查过的竞态——host_session(osc_report=...) 必须让**托管
        程序自己在启动瞬间发起的第一次 OSC 11 查询**就拿到正确颜色，而不是像
        test_report_theme_answers_pane_osc11_query 那样"先注入、再手动在已有
        shell 里补发一次查询"（那条测试测的是机制本身能不能用，不测时序）。

        真机发现过两种更差的坏法，这里都要防止回归：
        1. 完全不早注入：托管程序自己启动时查，大概率拿到 tmux 的默认猜测值
           （通常是纯黑），不是真实终端色。
        2. 注入后立刻关闭控制通道：比什么都不做还差——`refresh-client -r` 依赖
           "当前有控制模式客户端连接着"这个前提，通道一关，注入的颜色跟着失效，
           托管程序此后一次都查不到（而不是查到默认猜测值）。
        """
        subprocess.run(["tmux", "-L", self.SOCKET, "kill-session", "-t", self.SESSION],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with mock.patch.object(embed.keepalive, "_BASE_ARGV", ("tmux", "-L", self.SOCKET)), \
             mock.patch.object(embed.keepalive, "tmux_argv", lambda name=None: ("tmux", "-L", self.SOCKET)), \
             mock.patch.object(embed.keepalive, "reap_pressure", return_value=[]):
            probe_script = (
                "import os,sys,termios,tty,select,time;"
                "fd=sys.stdin.fileno();old=termios.tcgetattr(fd);tty.setraw(fd);"
                # Give host_session a beat to open the control channel and inject
                # OSC before the program's first query (slow/loaded hosts otherwise
                # time out even when injection is correct).
                "time.sleep(0.15);"
                "os.write(1,b'\\x1b]11;?\\x07');"
                "r,_,_=select.select([fd],[],[],3.0);"
                "d=os.read(fd,64) if r else b'TIMEOUT';"
                "termios.tcsetattr(fd,termios.TCSADRAIN,old);"
                "print('RESP', repr(d));"
                # 探测完立刻退出会让这个 pane（乃至整个测试用 tmux server，因为
                # 它是唯一会话）跟着关掉，capture 就再也读不到内容——留一个死
                # 循环撑住进程，直到测试自己 kill-session 收尾。
                "time.sleep(60)"
            )
            plan = LaunchPlan(("python3", "-c", probe_script), None)
            # 同上：必须用真实形态（OSC 10 在前、OSC 11 在后），否则测不出 tmux
            # 只认第一条序列导致的背景色注入失效
            report = b"\x1b]10;rgb:0000/0000/0000\x07\x1b]11;rgb:abcd/1234/5678\x07"
            name = embed.host_session(plan, "themetest", "themetest-race", 80, 24,
                                      osc_report=report)
            self.addCleanup(lambda: subprocess.run(
                ["tmux", "-L", self.SOCKET, "kill-session", "-t", name],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            deadline = time.monotonic() + 6.0
            text = ""
            while time.monotonic() < deadline:
                text = embed.capture(name) or ""
                if "RESP" in text:
                    break
                time.sleep(0.1)
            self.assertIn("RESP", text, f"托管程序自己的首次查询应已完成：{text!r}")
            self.assertNotIn("TIMEOUT", text, f"首次查询不应超时无应答：{text!r}")
            self.assertIn("abab/1212/5656", text,
                          f"首次查询应拿到注入的真实颜色，而不是 tmux 的默认猜测值：{text!r}")


@unittest.skipUnless(shutil.which("tmux"), "需要真实 tmux")
class HostSessionKeepaliveSmokeTests(unittest.TestCase):
    """host_session against the real corral-keepalive socket (no argv patches)."""

    def test_host_session_creates_alive_keepalive_session(self):
        """Catches Linux CI gaps IntegrationTests on corral-test-ctl miss."""
        from corral import keepalive

        plan = LaunchPlan(
            ("bash", "-c", "printf 'HOST-SESSION-IT\\n'; sleep 30"), None,
        )
        name = None
        try:
            name = embed.host_session(plan, "claude", "hostsessionit01", 80, 24)
            self.assertTrue(name.startswith("corral-claude-"))
            self.assertTrue(embed.is_alive(name), name)
            deadline = time.monotonic() + 4.0
            text = ""
            while time.monotonic() < deadline:
                text = embed.capture(name) or ""
                if "HOST-SESSION-IT" in text:
                    break
                time.sleep(0.1)
            self.assertIn("HOST-SESSION-IT", text, repr(text))
        finally:
            if name:
                keepalive.kill(name)


@unittest.skipUnless(shutil.which("tmux"), "需要真实 tmux")
class StartupFailureCaptureTests(unittest.TestCase):
    """Real tmux: a failed launch leaves its reason, a clean exit leaves nothing.

    Isolated socket; liveness must stay what it was before (session gone).
    """

    SOCKET = "corral-test-exit"

    def setUp(self):
        self._tmux("kill-server")
        subprocess.run(
            ["tmux", "-L", self.SOCKET, "-f", "/dev/null", "new-session", "-d", "-s", "keep",
             "--", "sleep", "60"],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        patcher = mock.patch.object(
            embed.keepalive, "tmux_argv", lambda name=None: ("tmux", "-L", self.SOCKET),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmux, "kill-server")
        self.tmp = tempfile.mkdtemp(prefix="corral-test-exit-")

    def _tmux(self, *args):
        return subprocess.run(
            ["tmux", "-L", self.SOCKET, *args],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, check=False,
        )

    def _launch(self, name: str, body: str) -> None:
        script = os.path.join(self.tmp, f"{name}.sh")
        with open(script, "w", encoding="utf-8") as fh:
            fh.write("#!/bin/sh\n" + body + "\n")
        os.chmod(script, 0o755)
        embed._prepare_exit_capture(name)
        subprocess.run(
            ["tmux", "-L", self.SOCKET, "new-session", "-d", "-s", name, "-x", "100", "-y", "20",
             "--", script],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def test_failed_launch_reports_reason_and_session_is_gone(self):
        self._launch("corral-codex-bad", 'echo "Error: Missing optional dependency x" >&2; exit 1')
        report = embed.wait_for_startup_failure("corral-codex-bad", timeout=3.0)
        self.assertIsNotNone(report)
        self.assertEqual(report.status, 1)
        self.assertTrue(report.startup_failure)
        self.assertEqual(report.lines, ("Error: Missing optional dependency x",))
        self.assertIn("Missing optional dependency x", embed.exit_summary(report))
        self.assertEqual(embed.pane_liveness("corral-codex-bad"), "dead")
        self.assertIsNone(embed.take_exit_report("corral-codex-bad"), "report is consumed")

    def test_clean_exit_and_running_session_leave_no_report(self):
        self._launch("corral-codex-ok", "echo fine; exit 0")
        self._launch("corral-codex-live", "sleep 30")
        self.assertIsNone(embed.wait_for_startup_failure("corral-codex-ok", timeout=1.0))
        self.assertIsNone(embed.wait_for_startup_failure("corral-codex-live", timeout=0.3))
        self.assertEqual(embed.pane_liveness("corral-codex-live"), "alive")
        self.assertNotIn("corral-exit", self._tmux("list-buffers", "-F", "#{buffer_name}").stdout)

    def test_reused_name_drops_stale_report(self):
        self._launch("corral-codex-again", "echo first; exit 2")
        deadline = time.monotonic() + 3.0
        while embed.pane_liveness("corral-codex-again") != "dead" and time.monotonic() < deadline:
            time.sleep(0.05)
        self._launch("corral-codex-again", "sleep 30")
        self.assertIsNone(embed.take_exit_report("corral-codex-again"))


if __name__ == "__main__":
    unittest.main()
