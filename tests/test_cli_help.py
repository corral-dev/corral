"""Help must advertise public routes and never execute their side effects."""

import os
import subprocess
import sys
import unittest
from unittest import mock

from corral import agent_api, bootstrap, i18n
from corral.cli_help import build_main_parser
from corral.remote import cli as remote_cli
from corral.runtime import default_registry
from corral.runtime.registry import active_runtimes


class CliHelpTests(unittest.TestCase):
    def tearDown(self):
        i18n.set_lang("en")

    def test_public_command_and_runtime_coverage_in_both_languages(self):
        commands = set(agent_api.COMMAND_ROOT_NAMES) | {
            "remote", "login", "logout", "whoami", "shim", "observer", "cache", "update",
        }
        self.assertEqual(set(agent_api.COMMAND_ROOT_NAMES), bootstrap._AGENT_ROOTS)
        registry = default_registry()
        for language in ("en", "zh"):
            with self.subTest(language=language), mock.patch.dict(os.environ, CORRAL_LANG=language):
                i18n.set_lang(language)
                help_text = build_main_parser().format_help()
                visible = {r.id for r in active_runtimes(registry)} | {"agent", "cursor-agent"}
                for name in commands | visible:
                    self.assertIn(name, help_text)
                for hidden in ("kimi", "Kimi", "_cursor-hook", "--generate-titles", "_serve"):
                    self.assertNotIn(hidden, help_text)

    def test_fresh_process_help_is_lightweight_and_side_effect_free(self):
        script = '''
import sys
from corral import bootstrap
bootstrap._migrate_pi_history = lambda: (_ for _ in ()).throw(AssertionError("migration"))
sys.stdin.isatty = sys.stdout.isatty = lambda: True
sys.argv = ["corral", *sys.argv[1:]]
try:
    bootstrap.main()
except SystemExit as exc:
    code = exc.code
else:
    code = 0
for name in ("corral.cli", "corral.updater", "corral.runtime", "corral.pi_migration",
             "corral.store", "textual", "sesskit"):
    assert name not in sys.modules, name
raise SystemExit(code)
'''
        cases = [
            (["--help"], 0), (["-h"], 0), (["--limit", "5", "--help"], 0),
            (["--no-keepalive", "--help"], 0), (["update", "--help"], 0),
            (["update", "-h"], 0), (["update", "--unknown"], 2),
            (["update", "unexpected"], 2),
        ]
        for argv, code in cases:
            with self.subTest(argv=argv):
                result = subprocess.run(
                    [sys.executable, "-c", script, *argv],
                    capture_output=True, text=True, timeout=10,
                )
                self.assertEqual(result.returncode, code, result.stdout + result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                if code == 0:
                    self.assertIn("usage: corral", result.stdout)
                    self.assertEqual(result.stderr, "")

    def test_remote_help_hides_daemon_but_internal_route_still_dispatches(self):
        self.assertNotIn("_serve", remote_cli.build_parser().format_help())
        with mock.patch.object(remote_cli, "_cmd_serve", return_value=0) as serve:
            self.assertEqual(remote_cli.main(["_serve"]), 0)
        serve.assert_called_once()

    def test_account_alias_help_uses_the_invoked_command(self):
        for command in ("login", "logout", "whoami"):
            with self.subTest(command=command), mock.patch("sys.stdout") as stdout:
                with self.assertRaises(SystemExit) as raised:
                    remote_cli.main([command, "--help"], prog="corral")
                self.assertEqual(raised.exception.code, 0)
                output = "".join(call.args[0] for call in stdout.write.call_args_list)
                self.assertIn(f"usage: corral {command}", output)
                self.assertNotIn(f"usage: corral remote {command}", output)

    def test_session_runtime_help_tracks_active_allowlist(self):
        checked = 0
        for spec in agent_api.COMMANDS:
            for option in spec.get("args", []):
                if option["flags"] == ["--runtime"]:
                    checked += 1
                    self.assertNotIn("kimi", option["kwargs"]["help"])
        self.assertGreater(checked, 0)

    def test_scanner_import_preserves_sesskit_cache_binding(self):
        script = """
from unittest import mock
from corral import cache
with mock.patch("sesskit.cache.set_cache") as bind:
    import corral.scan
    bind.assert_called_once_with(cache.get_cache())
"""
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
