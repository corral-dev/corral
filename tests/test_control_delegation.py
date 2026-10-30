"""Shared-control delegation: Corral consumes the extracted implementation.

Generic store/correlation mechanics delegate to ``agent_session_control``
while Corral keeps target selection, admission policy, managed identity,
defaults and terminal hooks. These tests pin both sides: legacy
namespaces/values stay byte-exact AND the generic code paths are the shared
ones (no duplicated store SQL, marker regex, digest or correlation
algorithms beside the library).
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from corral import control, control_store, control_worker
from corral.control_store import ControlError, RunStore
from corral.runtime import default_registry

SRC = str(Path(__file__).resolve().parents[1] / "src")
ACTIVE_IDS = ("claude", "codex", "opencode", "cursor", "pi")


class MarkerNamespaceTests(unittest.TestCase):
    RUN = "run_" + "c" * 32

    def test_legacy_wire_format_is_byte_exact(self):
        self.assertEqual(control.marker(self.RUN, "initial"), f"[Corral run={self.RUN} message=initial]")
        self.assertEqual(control.MARKER_NAMESPACE, "Corral")
        wired = control.wire_prompt(self.RUN, "initial", "fix it")
        self.assertTrue(wired.startswith("fix it\n\n[Corral run="))
        self.assertEqual(control.own_message(wired, self.RUN), "initial")

    def test_namespaced_default_matches_legacy(self):
        self.assertEqual(control.marker_with_namespace(self.RUN, "m1"), control.marker(self.RUN, "m1"))
        self.assertEqual(
            control.wire_prompt_with_namespace(self.RUN, "m1", "p"), control.wire_prompt(self.RUN, "m1", "p")
        )
        self.assertEqual(control.own_message_with_namespace(control.wire_prompt(self.RUN, "m1", "p"), self.RUN), "m1")

    def test_other_namespace_does_not_cross_match(self):
        foreign = control.wire_prompt_with_namespace(self.RUN, "m1", "p", namespace="Other")
        self.assertIsNone(control.own_message(foreign, self.RUN))
        self.assertEqual(control.own_message_with_namespace(foreign, self.RUN, namespace="Other"), "m1")

    def test_other_run_marker_does_not_match(self):
        other = "run_" + "d" * 32
        wired = control.wire_prompt(other, "initial", "fix")
        self.assertIsNone(control.own_message(wired, self.RUN))


class StoreConfigTests(unittest.TestCase):
    def test_library_config_preserves_legacy_values(self):
        config = control_store.library_store_config()
        self.assertEqual(config["root"], control_store.control_home())
        self.assertEqual(config["namespace"], "corral")
        self.assertEqual(config["supervisor"], "corral-supervisor")
        self.assertEqual(config["supervisor"], control_store.SUPERVISOR)
        self.assertEqual(config["schema_version"], control_store.SCHEMA_VERSION)
        self.assertEqual(set(config["settled_states"]), set(control_store.SETTLED))
        self.assertEqual(config["max_active_runs"], control_store.MAX_ACTIVE_RUNS)

    def test_store_root_override_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(control_store.store_root(Path(tmp)), Path(tmp))
            self.assertEqual(control_store.library_store_config(tmp)["root"], Path(tmp))

    def test_reads_still_never_create_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "control")
            with self.assertRaises(ControlError):
                store.get("run_missing")
            self.assertFalse(store.root.exists())

    def test_exit_codes_unchanged(self):
        self.assertEqual(
            (
                control_store.EXIT_ERROR,
                control_store.EXIT_USAGE,
                control_store.EXIT_NOT_FOUND,
                control_store.EXIT_CONFLICT,
                control_store.EXIT_TIMEOUT,
            ),
            (1, 2, 3, 5, 6),
        )


class ActorNamespaceTests(unittest.TestCase):
    def test_default_actor_is_local_user(self):
        env = {
            k: v
            for k, v in os.environ.items()
            if k not in {"CORRAL_ACTOR", "CORRAL_RUNTIME", "SC_RUNTIME", "CORRAL_SESSION_ID", "SC_SESSION_ID"}
        }
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch("corral.legacy_names.getenv", return_value=None):
                self.assertEqual(control.caller_identity()["actor"], "local-user")
        self.assertEqual(control_store.DEFAULT_ACTOR, "local-user")


class PlanDelegationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.registry = default_registry()
        self.runtimes = {rt.id: rt for rt in self.registry if rt.id in ACTIVE_IDS}
        self.assertEqual(set(self.runtimes), set(ACTIVE_IDS))

    def test_control_profile_shape_for_all_active_runtimes(self):
        for rid, runtime in self.runtimes.items():
            profile = control.control_profile(runtime)
            for key in (
                "runtime",
                "display_name",
                "executable",
                "executable_aliases",
                "available",
                "interrupt_keys",
                "auto_approve_args",
                "model_supported",
                "session_id_supported",
                "continue_supported",
                "fork_supported",
                "mcp_supported",
            ):
                self.assertIn(key, profile, rid)
            self.assertEqual(profile["runtime"], rid)
            self.assertIsInstance(profile["available"], bool)
            self.assertTrue(profile["interrupt_keys"], rid)
            self.assertFalse(profile["mcp_supported"], rid)
        self.assertFalse(self.runtimes["opencode"].control_profile()["model_supported"])
        self.assertTrue(self.runtimes["pi"].control_profile()["session_id_supported"])

    def test_create_plan_matches_adapter_builders(self):
        for rid, runtime in self.runtimes.items():
            plan = control.library_create_plan(runtime, self.tmp.name)
            expected = runtime.build_new_session_plan(self.tmp.name)
            self.assertEqual(plan["argv"], tuple(expected.argv), rid)
            self.assertEqual(plan["cwd"], expected.cwd, rid)
            self.assertEqual(plan["env"], {}, rid)
            self.assertIsInstance(plan["argv"], tuple)

    def test_create_plan_never_emits_shell_commands(self):
        for _rid, runtime in self.runtimes.items():
            plan = control.library_create_plan(runtime, self.tmp.name)
            self.assertTrue(plan["argv"])
            self.assertNotIn(" ", plan["argv"][0])

    def test_create_plan_model_override_matches_fresh_plan(self):
        for rid, runtime in self.runtimes.items():
            if rid == "opencode":
                with self.assertRaises(ControlError) as caught:
                    control.library_create_plan(runtime, self.tmp.name, model="some-model")
                self.assertEqual(caught.exception.code, "model_selection_unsupported")
                continue
            plan = control.library_create_plan(runtime, self.tmp.name, model="probe-model")
            expected = control.fresh_plan(runtime, self.tmp.name, "probe-model")
            self.assertEqual(plan["argv"], tuple(expected.argv), rid)

    def test_create_plan_native_id_binding(self):
        claude = control.library_create_plan(self.runtimes["claude"], self.tmp.name, native_id="abc-123")
        self.assertIn("--session-id", claude["argv"])
        self.assertIn("abc-123", claude["argv"])
        pi = control.library_create_plan(self.runtimes["pi"], self.tmp.name, native_id="host-ident")
        self.assertIn("--session-id", pi["argv"])
        self.assertIn("host-ident", pi["argv"])
        for rid in ("codex", "cursor", "opencode"):
            with self.assertRaises(ControlError) as caught:
                control.library_create_plan(self.runtimes[rid], self.tmp.name, native_id="abc-123")
            self.assertEqual(caught.exception.code, "session_id_unsupported", rid)

    def test_create_plan_rejects_mcp_before_launch(self):
        with self.assertRaises(ControlError) as caught:
            control.library_create_plan(self.runtimes["claude"], self.tmp.name, mcp_servers=("s1",))
        self.assertEqual(caught.exception.code, "mcp_unsupported")

    def test_resume_plan_uses_exact_session_without_rescan(self):
        seen: dict = {}

        def fake_build(session):
            seen["session"] = session
            return SimpleNamespace(argv=("exe", "--resume", "x"), cwd=self.tmp.name)

        runtime = SimpleNamespace(
            id="fake",
            build_resume_plan=fake_build,
            scan_sessions=mock.Mock(side_effect=AssertionError("must not rescan")),
        )

        def fail_scan(*args, **kwargs):  # pragma: no cover - guard only
            raise AssertionError("must not rescan")

        runtime.scan_sessions = fail_scan
        session = {"id": "exact-id", "path": "/tmp/h.jsonl", "cwd": self.tmp.name}
        plan = control.library_resume_plan(runtime, session)
        self.assertEqual(plan, {"argv": ("exe", "--resume", "x"), "cwd": self.tmp.name, "env": {}})
        self.assertIs(seen["session"], session)

    def test_resume_plan_fails_without_exact_identity(self):
        runtime = self.runtimes["claude"]
        for bad in ({}, {"id": "x"}, {"path": "y"}, {"id": "x", "path": ""}):
            with self.assertRaises(ControlError) as caught:
                control.library_resume_plan(runtime, bad)
            self.assertEqual(caught.exception.code, "usage_error")

    def test_resume_plan_rejects_mcp_before_launch(self):
        session = {"id": "x", "path": "/tmp/h.jsonl", "cwd": self.tmp.name}
        with self.assertRaises(ControlError) as caught:
            control.library_resume_plan(self.runtimes["claude"], session, mcp_servers=("s1",))
        self.assertEqual(caught.exception.code, "mcp_unsupported")


class SharedImplementationTests(unittest.TestCase):
    def test_store_is_the_shared_implementation(self):
        import agent_session_control.errors as lib_errors
        import agent_session_control.store as lib_store

        self.assertTrue(issubclass(RunStore, lib_store.RunStore))
        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "control")
            self.assertEqual(store.path, Path(tmp) / "control" / "runs.sqlite3")
            self.assertEqual(store.namespace, "default")
            self.assertEqual(store.supervisor, "corral-supervisor")
        self.assertIs(control_store.digest, lib_store.digest)
        self.assertIs(control_store.run_version, lib_store.run_version)
        self.assertIs(ControlError, lib_errors.ControlError)
        for name in ("EXIT_ERROR", "EXIT_USAGE", "EXIT_NOT_FOUND", "EXIT_CONFLICT", "EXIT_TIMEOUT"):
            self.assertEqual(getattr(control_store, name), getattr(lib_errors, name))
        self.assertEqual(set(control_store.TERMINAL), set(lib_store.TERMINAL))
        self.assertEqual(set(control_store.SETTLED), set(lib_store.SETTLED))

    def test_correlation_is_the_shared_implementation(self):
        import agent_session_control.correlation as lib_correlation

        self.assertIs(control.build_result, lib_correlation.build_result)
        self.assertIs(control.compact_event, lib_correlation.compact_event)
        self.assertEqual(control.MARKER_NAMESPACE, "Corral")
        # marker/wire/own_message/correlate pin the legacy namespace, nothing else.
        run = "run_" + "e" * 32
        self.assertEqual(control.marker(run, "m"), lib_correlation.marker(run, "m", namespace="Corral"))
        self.assertEqual(
            control.wire_prompt(run, "m", "p"), lib_correlation.wire_prompt(run, "m", "p", namespace="Corral")
        )

    def test_no_duplicated_store_or_correlation_algorithms(self):
        src = Path(__file__).resolve().parents[1] / "src" / "corral"
        for name in ("control_store.py", "control.py"):
            text = (src / name).read_text()
            for snippet in (
                "CREATE TABLE",
                "hashlib",
                "sha256",
                "run_[0-9a-f]",
                "re.compile",
                "AUTOINCREMENT",
                "BEGIN IMMEDIATE",
            ):
                self.assertNotIn(snippet, text, f"{name} duplicates shared logic: {snippet}")

    def test_hooks_are_real_shared_port_callables(self):
        from agent_session_control.supervisor import SupervisorHooks

        with tempfile.TemporaryDirectory() as tmp:
            store = RunStore(Path(tmp) / "control")
            run, _ = store.create(
                "t",
                "k-hooks",
                {"mode": "submit", "runtime": "claude", "session_ref": "claude:x"},
                {"delivery": "accepted", "execution": "queued"},
            )
            hub = SimpleNamespace(registry=SimpleNamespace(get=lambda rid: SimpleNamespace(id="claude")))
            hooks, state = control_worker.corral_hooks(store, run["run_id"], hub)
            self.assertIsInstance(hooks, SupervisorHooks)
            for field in (
                "resolve_target",
                "target_busy",
                "target_waiting",
                "host_fresh",
                "prepare",
                "submit",
                "verify",
                "interrupt",
                "answer",
                "steer",
                "open_observation",
                "poll_observation",
                "close_observation",
                "baseline_of",
                "bind_native",
                "is_alive",
                "screen_hash",
                "phase",
                "list_questions",
            ):
                self.assertTrue(callable(getattr(hooks, field)), field)
            self.assertIsNone(hooks.close_owned)
            try:
                state["locks"].close()
            except Exception:  # noqa: BLE001 - teardown must not fail assertions
                pass


class LightweightImportTests(unittest.TestCase):
    def test_control_imports_load_no_acp_or_textual(self):
        probe = (
            "import sys; "
            "import corral.control, corral.control_store, corral.control_cli; "
            "bad = sorted(m for m in sys.modules "
            "if m == 'textual' or m.startswith('textual.') "
            "or m in ('acp', 'agent_client_protocol') "
            "or m.startswith('agent_client_protocol')); "
            "print('BAD:' + ','.join(bad) if bad else 'CLEAN')"
        )
        path_entries = [SRC]
        try:
            import os.path as _p

            import agent_session_control

            path_entries.append(_p.dirname(_p.dirname(agent_session_control.__file__)))
        except ImportError:
            self.skipTest("agent_session_control is not importable here; nothing to probe")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(path_entries)}
        out = subprocess.run(
            [sys.executable, "-c", probe], capture_output=True, text=True, env=env, check=True
        ).stdout.strip()
        self.assertEqual(out, "CLEAN")


if __name__ == "__main__":
    unittest.main()
