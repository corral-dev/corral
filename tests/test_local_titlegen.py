"""No-key title routing, process isolation, and bounded Agent failure handling."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from corral import titlegen, titles


class LocalTitleGeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "XDG_CONFIG_HOME": str(self.root),
            "CORRAL_SESSION_ID": "parent-session",
            "CORRAL_RUNTIME": "pi",
            "CORRAL_PI_CLAIM_PATH": "parent-claim",
            "CLAUDECODE": "nested-parent",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_missing_gateway_uses_local_and_configured_gateway_never_probes_local(self):
        local = mock.Mock()
        with (
            mock.patch.object(titlegen, "_LOCAL_GENERATORS", (local,)),
            mock.patch.object(titlegen._GENERATOR, "is_available", return_value=False),
        ):
            self.assertEqual(titlegen.available_generators(), (local,))
        local.is_available.reset_mock()
        with (
            mock.patch.object(titlegen, "_LOCAL_GENERATORS", (local,)),
            mock.patch.object(titlegen._GENERATOR, "is_available", return_value=True),
        ):
            self.assertEqual(titlegen.available_generators(), (titlegen._GENERATOR,))
        local.is_available.assert_not_called()

    def test_unsupported_native_flags_are_not_treated_as_a_usable_backend(self):
        local = titlegen.LocalTitleGenerator("claude", "claude", (), ("--no-session-persistence",))
        with (
            mock.patch.object(titlegen.shutil, "which", return_value="/fixture/claude"),
            mock.patch.object(titlegen, "_run_local", return_value="old CLI"),
        ):
            self.assertTrue(local.is_available())
            self.assertIsNone(local.generate("never submitted", 1))

    def test_first_paint_candidate_discovery_never_starts_a_process(self):
        with (
            mock.patch.object(titlegen._GENERATOR, "is_available", return_value=False),
            mock.patch.object(titlegen.shutil, "which", return_value="/fixture/agent"),
            mock.patch.object(titlegen.subprocess, "Popen", side_effect=AssertionError("first paint spawned a CLI")),
        ):
            self.assertEqual(len(titlegen.available_generators()), 5)

    def test_opencode_non_event_json_does_not_break_title_generation(self):
        local = next(item for item in titlegen._LOCAL_GENERATORS if item.id == "opencode")
        with (
            mock.patch.object(local, "_supports_invocation", return_value=True),
            mock.patch.object(titlegen, "_run_local", return_value='[]\n{"type":"text","part":"invalid"}\n'),
        ):
            self.assertIsNone(local.generate("private excerpt", 2))

    def test_each_backend_uses_stdin_private_workspace_and_preserves_auth_environment(self):
        for local in titlegen._LOCAL_GENERATORS:
            with self.subTest(runtime=local.id):
                def run(argv, prompt, timeout, cwd, env, local=local):
                    self.assertEqual(prompt, "private excerpt")
                    self.assertNotIn(prompt, argv)
                    self.assertTrue((cwd.parent / ".sesskit-ignore").exists())
                    self.assertEqual(env.get("HOME"), os.environ.get("HOME"))
                    self.assertNotIn("CORRAL_SESSION_ID", env)
                    self.assertNotIn("CORRAL_RUNTIME", env)
                    self.assertNotIn("CORRAL_PI_CLAIM_PATH", env)
                    self.assertNotIn("CLAUDECODE", env)
                    self.assertEqual(env["CORRAL_SHIM_ACTIVE"], "1")
                    self.assertEqual(timeout, 9)
                    self.assertNotIn("--model", argv)
                    if local.id == "claude":
                        self.assertIn("--no-session-persistence", argv)
                        self.assertNotIn("--bare", argv)
                        self.assertEqual(argv[argv.index("--tools") + 1], "")
                    elif local.id == "codex":
                        self.assertIn("--ephemeral", argv)
                        self.assertIn("read-only", argv)
                        (cwd / "answer.txt").write_text('{"fixture": "Fix login"}', encoding="utf-8")
                    elif local.id == "pi":
                        self.assertIn("--no-session", argv)
                        self.assertIn("--no-tools", argv)
                    elif local.id == "opencode":
                        self.assertIn("--standalone", argv)
                        self.assertEqual(Path(env["OPENCODE_DB"]).parent, cwd)
                        return json.dumps({"type": "reasoning", "part": {"text": "ignored"}}) + "\n" + json.dumps({
                            "type": "text", "part": {"text": '{"fixture": "Fix login"}'},
                        })
                    else:
                        self.assertEqual(Path(env["CURSOR_DATA_DIR"]).parent, cwd)
                        self.assertEqual(env.get("CURSOR_CONFIG_DIR"), os.environ.get("CURSOR_CONFIG_DIR"))
                        deny = json.loads((cwd / ".cursor/cli.json").read_text())["permissions"]["deny"]
                        self.assertIn("Shell(*)", deny)
                    return '{"fixture": "Fix login"}'

                with (
                    mock.patch.object(local, "_supports_invocation", return_value=True),
                    mock.patch.object(titlegen, "_run_local", side_effect=run),
                ):
                    self.assertEqual(local.generate("private excerpt", 9), '{"fixture": "Fix login"}')
                self.assertEqual(list((self.root / "corral/title-worker").iterdir()), [
                    self.root / "corral/title-worker/.sesskit-ignore",
                ])

    def test_process_receives_piped_prompt_and_returns_actual_output(self):
        script = "import sys; print(sys.stdin.read().upper())"
        result = titlegen._run_local([sys.executable, "-c", script], "piped input", 3, self.root, titlegen._job_env())
        self.assertEqual(result, "PIPED INPUT\n")

    def test_timeout_kills_the_owned_descendant(self):
        pid_file = self.root / "child.pid"
        script = (
            "import subprocess,sys,time; from pathlib import Path; "
            "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
            f"Path({str(pid_file)!r}).write_text(str(p.pid)); time.sleep(60)"
        )
        start = time.monotonic()
        self.assertIsNone(titlegen._run_local([sys.executable, "-c", script], "", 1, self.root, titlegen._job_env()))
        self.assertLess(time.monotonic() - start, 5)
        pid = int(pid_file.read_text())
        # A killed orphan may briefly remain a zombie; it must not be running.
        status = titlegen.subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
        self.assertTrue(not status.stdout.strip() or status.stdout.strip().startswith("Z"), status.stdout)

    def test_local_pipeline_accepts_all_five_source_runtimes_without_gateway(self):
        sessions = [{"source": source, "id": "sample",
                     "first_user_msg": "Fix login errors", "fallback_title": "Fix login errors"}
                    for source in ("claude", "codex", "opencode", "cursor", "pi")]
        local = titlegen.LocalTitleGenerator("pi", "pi", (), ())

        def answer(prompt, timeout):
            payload = json.loads(prompt.split("\n\n", 1)[1])
            return json.dumps({item["id"]: "Fix login errors" for item in payload})

        cache = {}
        with (
            mock.patch.object(titlegen, "available_generators", return_value=(local,)),
            mock.patch.object(local, "generate", side_effect=answer),
            mock.patch.object(titles, "CACHE_FILE", str(self.root / "titles.json")),
        ):
            self.assertEqual(len(titles.refresh_titles(sessions, cache)), 5)
        for session in sessions:
            self.assertEqual(titles.resolve_initial_title(session, cache), ("Fix login errors", False))

    def test_failed_local_backend_is_only_probed_once_per_pass(self):
        failed = titlegen.LocalTitleGenerator("claude", "claude", (), ())
        good = titlegen.LocalTitleGenerator("pi", "pi", (), ())
        sessions = [{"source": "codex", "id": str(i), "fallback_title": "Fix login errors"} for i in range(4)]
        with (
            mock.patch.object(titlegen, "available_generators", return_value=(failed, good)),
            mock.patch.object(failed, "generate", return_value=None) as bad,
            mock.patch.object(good, "generate", side_effect=lambda prompt, timeout: json.dumps({
                json.loads(prompt.split("\n\n", 1)[1])[0]["id"]: "Fix login errors",
            })),
            mock.patch.object(titles, "save_cache"),
        ):
            self.assertEqual(len(titles.refresh_titles(sessions, {})), 4)
        self.assertEqual(bad.call_count, 1)


if __name__ == "__main__":
    unittest.main()
