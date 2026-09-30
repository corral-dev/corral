"""命令拦截（corral shim）的安装、卸载、放行判定与脚本语法测试。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import shim
from corral.runtime import default_registry
from corral.runtime.claude import ClaudeRuntime


class ShimTargetTableTests(unittest.TestCase):
    def setUp(self):
        self.tmp_cursor = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp_cursor.cleanup()

    def test_every_registered_runtime_has_a_shim_target(self):
        """拦截表和默认注册表不得漂移：新增运行时必须同时登记可拦截命令。"""
        registry = default_registry()
        covered = {target.runtime_id for target in shim.TARGETS}
        self.assertEqual(set(registry.ids), covered)

    def test_target_commands_match_runtime_executables(self):
        registry = default_registry()
        for target in shim.TARGETS:
            runtime = registry.get(target.runtime_id)
            valid = {runtime.executable, *runtime.executable_aliases}
            self.assertIn(target.command, valid, f"{target.command} 不是该运行时的可执行名")

    def test_pi_is_default_shimmed_and_management_calls_pass_through(self):
        pi = next(target for target in shim.TARGETS if target.command == "pi")
        self.assertTrue(pi.default_on)
        self.assertEqual(pi.runtime_id, "pi")
        self.assertEqual(
            pi.passthrough_words,
            ("install", "remove", "uninstall", "update", "list", "config", "auth",
             "--export", "--list-models"),
        )

    def test_opencode_v2_headless_calls_pass_through_and_mini_is_hosted(self):
        """opencode v2（本机实装 2.0.16 `--help` 实测）：无头/管理类放行。

        v1 独有的 github 在 v2 已不存在，不得再出现在放行表里；mini 是交互式
        子命令，必须托管，不得放行。
        """
        opencode = next(target for target in shim.TARGETS if target.command == "opencode")
        self.assertTrue(opencode.default_on)
        self.assertEqual(opencode.runtime_id, "opencode")
        self.assertEqual(
            opencode.passthrough_words,
            ("run", "serve", "auth", "upgrade", "models",
             "api", "service", "reload", "pair", "session", "update"),
        )
        self.assertNotIn("github", opencode.passthrough_words)
        self.assertNotIn("mini", opencode.passthrough_words)

    def test_agent_stays_opt_in_when_the_binary_is_not_cursor(self):
        agent = next(t for t in shim.TARGETS if t.command == "agent")
        self.assertFalse(agent.default_on)
        with mock.patch.object(shim.shutil, "which", return_value=None):
            selected = {t.command for t in shim.selected_targets()}
        self.assertNotIn("agent", selected)
        with mock.patch.object(shim.shutil, "which", return_value="/usr/bin/agent"):
            self.assertIn("agent", {t.command for t in shim.selected_targets(["agent"])})

    def test_agent_is_auto_selected_when_binary_is_official_cursor_cli(self):
        fake = Path(self.tmp_cursor.name) / "agent"
        fake.write_text("#!/bin/sh\nexport CURSOR_INVOKED_AS=agent\n", encoding="utf-8")
        with mock.patch.object(shim.shutil, "which",
                               side_effect=lambda name: str(fake) if name == "agent" else None):
            self.assertTrue(shim.looks_like_cursor_cli("agent"))
            self.assertIn("agent", {t.command for t in shim.selected_targets()})

    def test_agent_is_auto_selected_when_binary_is_cursor_mode_model_wrapper(self):
        fake = Path(self.tmp_cursor.name) / "wrapper-agent"
        fake.write_text(
            "#!/bin/sh\n# 由 cursor-mode-model install 生成\n"
            'exec /opt/bin/cursor-mode-model exec --invoked-as "agent" -- "$@"\n',
            encoding="utf-8",
        )
        with mock.patch.object(shim.shutil, "which",
                               side_effect=lambda name: str(fake) if name == "agent" else None):
            self.assertIn("agent", {t.command for t in shim.selected_targets()})

    def test_generic_agent_binary_is_not_auto_selected(self):
        fake = Path(self.tmp_cursor.name) / "generic-agent"
        fake.write_text("#!/bin/sh\necho some other agent tool\n", encoding="utf-8")
        with mock.patch.object(
            shim.shutil, "which",
            side_effect=lambda name: str(fake) if name == "agent" else None,
        ):
            self.assertFalse(shim.looks_like_cursor_cli("agent"))
            self.assertNotIn("agent", {t.command for t in shim.selected_targets()})

    def test_agent_is_not_reported_as_shimmed_by_cursor_agent_suffix(self):
        """`agent` 是 `cursor-agent` 的后缀，状态判定必须整行匹配而不是子串。"""
        targets = tuple(t for t in shim.TARGETS if t.command == "cursor-agent")
        for shell in ("bash", "fish"):
            with self.subTest(shell=shell):
                script = shim.render_script(shell, targets)
                defined = shim._shimmed_commands(shell, script)
                self.assertIn("cursor-agent", defined)
                self.assertNotIn("agent", defined)

    def test_unknown_include_is_a_usage_error(self):
        with self.assertRaises(shim.ShimError) as ctx:
            shim.selected_targets(["definitely-not-a-runtime"])
        self.assertEqual(ctx.exception.exit_code, shim.EXIT_USAGE)


class ShimScriptRenderTests(unittest.TestCase):
    def setUp(self):
        self.targets = tuple(t for t in shim.TARGETS if t.command in {"claude", "codex"})

    def test_cursor_management_commands_are_listed_for_passthrough(self):
        for command in ("cursor-agent", "agent"):
            target = next(t for t in shim.TARGETS if t.command == command)
            self.assertEqual(target.passthrough_words, shim.CURSOR_PASSTHROUGH)
        for word in ("about", "models", "whoami", "help", "--list-models"):
            self.assertIn(word, shim.CURSOR_PASSTHROUGH)

    def test_posix_script_has_every_passthrough_guard(self):
        script = shim.render_script("bash", self.targets)
        for guard in ("CORRAL_SHIM_ACTIVE", "CORRAL_RUNTIME", "PICKUP_SHIM_ACTIVE",
                      "PICKUP_RUNTIME", "SC_RUNTIME",
                      "[ -t 0 ]", "command -v corral"):
            self.assertIn(guard, script, f"缺少放行判据：{guard}")
        self.assertNotIn("${TMUX:-}", script)
        self.assertNotIn("${STY:-}", script)

    def test_posix_script_routes_to_corral_with_recursion_guard(self):
        script = shim.render_script("bash", self.targets)
        self.assertIn('CORRAL_SHIM_ACTIVE=1 PICKUP_SHIM_ACTIVE=1 command corral claude "$@"', script)
        self.assertIn('command claude "$@"', script)

    def test_fish_script_uses_fish_syntax(self):
        script = shim.render_script("fish", self.targets)
        self.assertIn("status is-interactive", script)
        self.assertIn("function claude --wraps claude", script)
        self.assertIn("env CORRAL_SHIM_ACTIVE=1 PICKUP_SHIM_ACTIVE=1 corral claude $argv", script)
        self.assertNotIn('"$@"', script)

    def test_headless_flags_are_listed_for_passthrough(self):
        """corral 自己用 `claude -p` 生成标题，这条放行丢了会打断标题生成。"""
        script = shim.render_script("bash", self.targets)
        self.assertIn("-p", shim.COMMON_PASSTHROUGH)
        self.assertIn("--print", shim.COMMON_PASSTHROUGH)
        self.assertIn("|".join(shim.COMMON_PASSTHROUGH), script)
        codex = next(t for t in shim.TARGETS if t.command == "codex")
        self.assertIn("exec", codex.passthrough_words)

    def test_opencode_v2_passthrough_words_are_rendered_into_the_script(self):
        opencode = next(target for target in shim.TARGETS if target.command == "opencode")
        script = shim.render_script("bash", (opencode,))
        self.assertIn('command corral opencode "$@"', script)
        self.assertIn(
            '"run serve auth upgrade models api service reload pair session update"',
            script,
        )

    def test_pi_routes_interactive_commands_and_passes_management_calls_through(self):
        pi = next(target for target in shim.TARGETS if target.command == "pi")
        script = shim.render_script("bash", (pi,))
        self.assertIn('CORRAL_SHIM_ACTIVE=1 PICKUP_SHIM_ACTIVE=1 command corral pi "$@"', script)
        self.assertIn(
            '"install remove uninstall update list config auth --export --list-models"',
            script,
        )


@unittest.skipIf(shutil.which("bash") is None, "本机没有 bash")
class ShimScriptSyntaxTests(unittest.TestCase):
    """真机语法校验：生成的脚本必须能被目标 shell 解析。"""

    def _write(self, shell: str) -> Path:
        targets = shim.TARGETS
        path = Path(self.tmp.name) / ("corral-shim.fish" if shell == "fish" else "corral-shim.sh")
        path.write_text(shim.render_script(shell, targets), encoding="utf-8")
        return path

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_bash_parses_generated_script(self):
        proc = subprocess.run(["bash", "-n", str(self._write("bash"))],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    @unittest.skipIf(shutil.which("zsh") is None, "本机没有 zsh")
    def test_zsh_parses_generated_script(self):
        proc = subprocess.run(["zsh", "-n", str(self._write("zsh"))],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    @unittest.skipIf(shutil.which("fish") is None, "本机没有 fish")
    def test_fish_parses_generated_script(self):
        proc = subprocess.run(["fish", "--no-execute", str(self._write("fish"))],
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


@unittest.skipIf(shutil.which("bash") is None, "本机没有 bash")
class ShimBehaviourTests(unittest.TestCase):
    """在真实 bash 里跑生成的脚本，逐条验证放行/托管判定。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.bin = root / "bin"
        self.bin.mkdir()
        for name in ("claude", "pi", "agent", "corral"):
            fake = self.bin / name
            fake.write_text(f'#!/usr/bin/env bash\necho "{name} $*"\n', encoding="utf-8")
            fake.chmod(0o755)
        targets = tuple(t for t in shim.TARGETS if t.command in {"claude", "pi", "agent"})
        self.script = root / "corral-shim.sh"
        self.script.write_text(shim.render_script("bash", targets), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, command: str, *, tty: bool, env: dict | None = None) -> str:
        """在交互式 bash 里 source 脚本后执行一条命令，返回 stdout。"""
        full_env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "HOME": self.tmp.name,
        }
        full_env.update(env or {})
        # `-i` 让 $- 含 i（脚本自己会检查交互性）；用 script(1) 造真 TTY 太重，
        # 这里用重定向区分"有无 TTY"：非 TTY 场景直接把 stdout 接管道即可。
        redirect = "" if tty else " > /dev/null"
        wrapper = f'source "{self.script}"; {command}'
        if tty:
            proc = subprocess.run(
                ["bash", "-ic", wrapper], capture_output=True, text=True, env=full_env,
            )
        else:
            proc = subprocess.run(
                ["bash", "-ic", wrapper + redirect], capture_output=True, text=True,
                env=full_env, stdout=subprocess.PIPE,
            )
        return proc.stdout

    def _run_on_pty(self, command: str, env: dict | None = None) -> str:
        """在真实伪终端里跑一条命令——只有这样 `[ -t 1 ]` 才成立，能验证"该托管的确实

        被托管"。用管道跑的用例只能验证放行分支，正向拦截必须靠 pty。
        """
        import pty

        full_env = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "HOME": self.tmp.name,
            "TERM": "dumb",
        }
        full_env.update(env or {})
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            ["bash", "-ic", f'source "{self.script}"; {command}'],
            stdin=slave, stdout=slave, stderr=slave, env=full_env, close_fds=True,
        )
        os.close(slave)
        chunks = []
        try:
            while True:
                try:
                    data = os.read(master, 4096)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
        finally:
            os.close(master)
            proc.wait(timeout=15)
        return b"".join(chunks).decode("utf-8", "replace")

    def test_interactive_bare_command_is_routed_through_corral(self):
        out = self._run_on_pty("claude")
        self.assertIn("corral claude", out)

    def test_pi_interactive_command_is_routed_through_corral(self):
        out = self._run_on_pty("pi")
        self.assertIn("corral pi", out)

    def test_pi_management_command_passes_through(self):
        out = self._run_on_pty("pi --list-models")
        self.assertIn("pi --list-models", out)
        self.assertNotIn("corral pi", out)

    def test_pi_recursion_guard_passes_through(self):
        out = self._run_on_pty("pi", env={"CORRAL_SHIM_ACTIVE": "1"})
        self.assertIn("pi ", out)
        self.assertNotIn("corral pi", out)

    def test_interactive_headless_flag_still_reaches_the_real_command(self):
        out = self._run_on_pty("claude -p 标题")
        self.assertIn("claude -p 标题", out)
        self.assertNotIn("corral claude", out)

    def test_interactive_inside_managed_session_is_not_nested(self):
        out = self._run_on_pty("claude", env={"CORRAL_RUNTIME": "claude"})
        self.assertNotIn("corral claude", out)

    def test_non_tty_invocation_falls_back_to_the_real_command(self):
        # 管道场景：stdout 不是终端，必须直接跑真身而不是绕道 corral。
        out = self._run("claude 你好 | cat", tty=True)
        self.assertIn("claude 你好", out)
        self.assertNotIn("corral", out)

    def test_already_managed_session_passes_through(self):
        out = self._run("claude", tty=True, env={"CORRAL_RUNTIME": "claude"})
        self.assertIn("claude", out)
        self.assertNotIn("corral claude", out)

    def test_recursion_guard_passes_through(self):
        out = self._run("claude", tty=True, env={"CORRAL_SHIM_ACTIVE": "1"})
        self.assertNotIn("corral claude", out)

    def test_interactive_prompt_containing_a_subcommand_word_is_still_hosted(self):
        """`update` 只在第一个位置参数时才算管理子命令，提示词里出现不该放行。"""
        out = self._run_on_pty("claude please update the docs")
        self.assertIn("corral claude", out)

    def test_agent_interactive_command_is_routed_through_corral(self):
        out = self._run_on_pty("agent")
        self.assertIn("corral cursor", out)

    def test_agent_about_passes_through(self):
        out = self._run_on_pty("agent about")
        self.assertIn("agent about", out)
        self.assertNotIn("corral cursor", out)

    def test_agent_list_models_flag_passes_through(self):
        out = self._run_on_pty("agent --list-models")
        self.assertIn("agent --list-models", out)
        self.assertNotIn("corral cursor", out)

    def test_own_tmux_still_hosts_interactive_commands(self):
        """用户自己的 tmux 不是 corral 托管层，必须照常拦截。"""
        out = self._run_on_pty("claude", env={"TMUX": "/tmp/tmux-0/default,1,0"})
        self.assertIn("corral claude", out)

    def test_legacy_sc_runtime_guard_passes_through(self):
        out = self._run_on_pty("claude", env={"SC_RUNTIME": "claude"})
        self.assertNotIn("corral claude", out)

    def test_headless_flag_passes_through(self):
        out = self._run('claude -p "生成标题" | cat', tty=True)
        self.assertIn("claude -p", out)
        self.assertNotIn("corral", out)

    def test_management_subcommand_passes_through(self):
        out = self._run("claude update | cat", tty=True)
        self.assertIn("claude update", out)
        self.assertNotIn("corral", out)

    def test_missing_corral_falls_back_to_the_real_command(self):
        """corral 被卸载后，用户的 claude 必须照常可用。"""
        (self.bin / "corral").unlink()
        out = self._run("claude | cat", tty=True)
        self.assertIn("claude", out)


class ShimInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.env = mock.patch.dict(os.environ, {
            "HOME": str(self.home),
            "CORRAL_CACHE_DIR": str(Path(self.tmp.name) / "cache"),
            "SHELL": "/bin/bash",
        }, clear=False)
        self.env.start()
        self.env.pop("ZDOTDIR", None) if False else None
        os.environ.pop("ZDOTDIR", None)
        # 只让 claude 看起来已安装，测试不依赖真机装了哪些 Agent。
        self.which = mock.patch.object(
            shim.shutil, "which",
            side_effect=lambda name: "/usr/bin/claude" if name == "claude" else None,
        )
        self.which.start()

    def tearDown(self):
        self.which.stop()
        self.env.stop()
        self.tmp.cleanup()

    def test_install_writes_block_and_script(self):
        rc = self.home / ".bashrc"
        rc.write_text("export EDITOR=vim\n", encoding="utf-8")
        result = shim.install(home=self.home)
        self.assertEqual(result["status"], "installed")
        text = rc.read_text(encoding="utf-8")
        self.assertIn("export EDITOR=vim", text)  # 用户原有内容不动
        self.assertIn(shim.BLOCK_BEGIN, text)
        self.assertIn(shim.BLOCK_END, text)
        self.assertTrue(Path(result["script_path"]).exists())
        self.assertIn("claude", [c["command"] for c in result["commands"] if c["shimmed"]])

    def test_install_auto_includes_cursor_named_agent(self):
        fake = Path(self.tmp.name) / "agent"
        fake.write_text("#!/bin/sh\nexport CURSOR_INVOKED_AS=agent\n", encoding="utf-8")
        self.which.stop()
        self.which = mock.patch.object(
            shim.shutil, "which",
            side_effect=lambda name: (
                "/usr/bin/claude" if name == "claude"
                else str(fake) if name == "agent"
                else None
            ),
        )
        self.which.start()
        result = shim.install(home=self.home)
        shimmed = [c["command"] for c in result["commands"] if c["shimmed"]]
        self.assertIn("agent", shimmed)
        script = Path(result["script_path"]).read_text(encoding="utf-8")
        self.assertIn("agent() {", script)

    def test_install_is_idempotent(self):
        shim.install(home=self.home)
        rc_after_first = (self.home / ".bashrc").read_text(encoding="utf-8")
        second = shim.install(home=self.home)
        self.assertEqual(second["status"], "unchanged")
        self.assertFalse(second["changed"])
        self.assertEqual(rc_after_first, (self.home / ".bashrc").read_text(encoding="utf-8"))
        self.assertEqual(rc_after_first.count(shim.BLOCK_BEGIN), 1)


    def test_repeated_stale_blocks_are_collapsed(self):
        rc = self.home / ".bashrc"
        rc.write_text(
            f"a=1\n{shim.BLOCK_BEGIN}\n旧内容\n{shim.BLOCK_END}\n"
            f"{shim.BLOCK_BEGIN}\n更旧内容\n{shim.BLOCK_END}\nb=2\n",
            encoding="utf-8",
        )
        shim.install(home=self.home)
        text = rc.read_text(encoding="utf-8")
        self.assertEqual(text.count(shim.BLOCK_BEGIN), 1)
        self.assertIn("a=1", text)
        self.assertIn("b=2", text)
        self.assertNotIn("旧内容", text)

    def test_dry_run_writes_nothing(self):
        rc = self.home / ".bashrc"
        rc.write_text("keep=1\n", encoding="utf-8")
        result = shim.install(home=self.home, dry_run=True)
        self.assertEqual(result["status"], "would_install")
        self.assertEqual(rc.read_text(encoding="utf-8"), "keep=1\n")
        self.assertFalse(Path(result["script_path"]).exists())

    def test_uninstall_removes_block_and_script_but_keeps_user_content(self):
        rc = self.home / ".bashrc"
        rc.write_text("alias ll='ls -l'\n", encoding="utf-8")
        installed = shim.install(home=self.home)
        result = shim.uninstall(home=self.home)
        self.assertEqual(result["status"], "uninstalled")
        text = rc.read_text(encoding="utf-8")
        self.assertIn("alias ll='ls -l'", text)
        self.assertNotIn(shim.BLOCK_BEGIN, text)
        self.assertFalse(Path(installed["script_path"]).exists())

    def test_uninstall_backs_up_the_rc_file(self):
        rc = self.home / ".bashrc"
        rc.write_text("keep=1\n", encoding="utf-8")
        shim.install(home=self.home)
        result = shim.uninstall(home=self.home)
        self.assertIsNotNone(result["backup_path"])
        self.assertIn("keep=1", Path(result["backup_path"]).read_text(encoding="utf-8"))

    def test_status_reports_not_installed_then_installed(self):
        self.assertEqual(shim.status(home=self.home)["status"], "not_installed")
        shim.install(home=self.home)
        self.assertEqual(shim.status(home=self.home)["status"], "installed")

    def test_status_reports_outdated_when_script_drifts(self):
        result = shim.install(home=self.home)
        Path(result["script_path"]).write_text("# 手改过\n", encoding="utf-8")
        self.assertEqual(shim.status(home=self.home)["status"], "outdated")

    def test_install_without_any_runtime_is_a_clear_error(self):
        with mock.patch.object(shim.shutil, "which", return_value=None):
            with self.assertRaises(shim.ShimError) as ctx:
                shim.install(home=self.home)
        self.assertEqual(ctx.exception.exit_code, shim.EXIT_NOT_FOUND)

    def test_cli_status_json_envelope(self):
        import io
        from contextlib import redirect_stdout

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = shim.cli_main(["status", "--shell", "bash", "--json"])
        self.assertEqual(code, shim.EXIT_OK)
        import json as json_mod

        payload = json_mod.loads(buffer.getvalue())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["meta"]["version"], shim.SHIM_API_VERSION)


class DirectLaunchAliasTests(unittest.TestCase):
    def test_cursor_is_reachable_by_the_names_users_actually_type(self):
        registry = default_registry()
        self.assertEqual(registry.resolve_id("cursor"), "cursor")
        self.assertEqual(registry.resolve_id("agent"), "cursor")
        self.assertEqual(registry.resolve_id("cursor-agent"), "cursor")

    def test_unknown_token_resolves_to_none(self):
        self.assertIsNone(default_registry().resolve_id("--help"))
        self.assertIsNone(default_registry().resolve_id("vim"))


class ClaudeRootAutoApproveTests(unittest.TestCase):
    """默认必须放行；唯一例外是 root 下 Claude 自己拒绝该参数导致起不来。"""

    def test_normal_user_gets_auto_approve(self):
        with mock.patch.object(os, "geteuid", return_value=1000, create=True):
            self.assertEqual(ClaudeRuntime().auto_approve_args,
                             ("--dangerously-skip-permissions",))

    def test_root_drops_the_flag_that_claude_itself_rejects(self):
        env = {k: v for k, v in os.environ.items()
               if k not in {"IS_SANDBOX", "CLAUDE_CODE_BUBBLEWRAP"}}
        with mock.patch.object(os, "geteuid", return_value=0, create=True), \
             mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(ClaudeRuntime().auto_approve_args, ())

    def test_root_inside_sandbox_keeps_the_flag(self):
        with mock.patch.object(os, "geteuid", return_value=0, create=True), \
             mock.patch.dict(os.environ, {"IS_SANDBOX": "1"}, clear=False):
            self.assertEqual(ClaudeRuntime().auto_approve_args,
                             ("--dangerously-skip-permissions",))

    def test_root_resume_plan_omits_the_flag(self):
        env = {k: v for k, v in os.environ.items()
               if k not in {"IS_SANDBOX", "CLAUDE_CODE_BUBBLEWRAP"}}
        with mock.patch.object(os, "geteuid", return_value=0, create=True), \
             mock.patch.dict(os.environ, env, clear=True):
            plan = ClaudeRuntime().build_resume_plan({"id": "abc", "cwd": ""})
        self.assertEqual(
            plan.argv,
            ("claude", "--settings", ClaudeRuntime._QUESTION_HOOK_SETTINGS, "--resume", "abc"),
        )


if __name__ == "__main__":
    unittest.main()
