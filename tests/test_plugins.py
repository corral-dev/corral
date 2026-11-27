"""Tests for the external executable plugin runtime, Core contracts, and Wave 1 workflow plugin."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from corral import agent_api, plugins, reclaim, state_contracts
from corral.cli_contract import EXIT_CONFLICT, EXIT_NOT_FOUND, EXIT_OK, EXIT_USAGE
from corral.remote.service import RemoteService
from corral.store import SessionStore


def _write_dummy_plugin(
    root: Path,
    *,
    name: str = "demo",
    commands: list[str] | None = None,
    entrypoint_rel: str = "bin/corral-demo",
    script_body: str | None = None,
    describe_commands: list[dict] | None = None,
    hide_prefixes: list[str] | None = None,
    remote_namespaces: list[str] | None = None,
) -> Path:
    pdir = root / name
    pdir.mkdir(parents=True, exist_ok=True)
    ep = pdir / entrypoint_rel
    ep.parent.mkdir(parents=True, exist_ok=True)
    body = script_body or (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv[1:], 'env': {\n"
        "    'CORRAL_PLUGIN_NAME': os.environ.get('CORRAL_PLUGIN_NAME'),\n"
        "    'CORRAL_STATE_DIR': os.environ.get('CORRAL_STATE_DIR'),\n"
        "    'CORRAL_ACTIVE_RUNTIMES': os.environ.get('CORRAL_ACTIVE_RUNTIMES'),\n"
        "}}))\n"
    )
    ep.write_text(body, encoding="utf-8")
    os.chmod(ep, 0o755)
    manifest: dict = {
        "schema_version": 1,
        "name": name,
        "version": "0.1.0",
        "description": f"Demo plugin {name}",
        "core_api": ">=1.0,<2.0",
        "runtime": "executable",
        "commands": commands or [name],
        "entrypoint": entrypoint_rel,
        "session_hide_prefixes": hide_prefixes or [],
        "remote_namespaces": remote_namespaces or [],
    }
    if describe_commands is not None:
        (pdir / "describe.json").write_text(json.dumps({"commands": describe_commands}), encoding="utf-8")
        manifest["describe_file"] = "describe.json"
    (pdir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return pdir


class PluginManifestAndIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.xdg = Path(self.tmp.name) / "xdg"
        self.src = Path(self.tmp.name) / "src"
        self.src.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_manifest_validation_rejects_bad_schema_traversal_and_reserved_commands(self) -> None:
        pdir = self.src / "bad"
        pdir.mkdir()
        (pdir / "ep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        os.chmod(pdir / "ep", 0o755)

        with self.assertRaises(plugins.PluginError):
            plugins.validate_manifest({"schema_version": 99}, pdir)

        with self.assertRaises(plugins.PluginError):
            plugins.validate_manifest(
                {
                    "schema_version": 1,
                    "name": "Bad_Name",
                    "version": "1.0",
                    "commands": ["bad"],
                    "entrypoint": "ep",
                },
                pdir,
            )

        with self.assertRaises(plugins.PluginError):
            plugins.validate_manifest(
                {
                    "schema_version": 1,
                    "name": "good",
                    "version": "1.0",
                    "commands": ["good"],
                    "entrypoint": "../../etc/passwd",
                },
                pdir,
            )

        with self.assertRaises(plugins.PluginError) as ctx:
            plugins.validate_manifest(
                {
                    "schema_version": 1,
                    "name": "shadow",
                    "version": "1.0",
                    "commands": ["list"],
                    "entrypoint": "ep",
                },
                pdir,
            )
        self.assertEqual(ctx.exception.exit_code, EXIT_CONFLICT)

    def test_command_collision_and_corrupt_index_fallback(self) -> None:
        p1 = _write_dummy_plugin(self.src, name="alpha", commands=["sharedcmd"])
        p2 = _write_dummy_plugin(self.src, name="beta", commands=["sharedcmd"])
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg)}, clear=False):
            plugins.install_plugin(str(p1))
            with self.assertRaises(plugins.PluginError) as ctx:
                plugins.install_plugin(str(p2))
            self.assertEqual(ctx.exception.exit_code, EXIT_CONFLICT)

            # Corrupt index.json -> load_index falls back to scanning plugin.json
            idx_path = plugins.plugins_dir() / "index.json"
            idx_path.write_text("{corrupt json", encoding="utf-8")
            resolved = plugins.resolve_command("sharedcmd")
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved["name"], "alpha")

    def test_lifecycle_enable_disable_remove_doctor_and_purge_state(self) -> None:
        p1 = _write_dummy_plugin(
            self.src,
            name="demo",
            commands=["demo", "dm"],
            describe_commands=[{"name": "demo run", "help": "Run demo", "risk": "read", "args": []}],
            remote_namespaces=["demo."],
        )
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg)}, clear=False):
            dry = plugins.install_plugin(str(p1), dry_run=True)
            self.assertTrue(dry["dry_run"])
            self.assertFalse((plugins.plugins_dir() / "demo").exists())

            res = plugins.install_plugin(str(p1))
            self.assertEqual(res["name"], "demo")
            self.assertEqual(plugins.remote_capabilities()["demo"]["namespaces"], ["demo."])

            plugins.set_plugin_enabled("demo", enabled=False)
            self.assertIsNone(plugins.resolve_command("demo"))
            self.assertEqual(plugins.remote_capabilities(), {})

            plugins.set_plugin_enabled("demo", enabled=True)
            self.assertIsNotNone(plugins.resolve_command("dm"))

            doc = plugins.doctor_plugins(fix=True)
            self.assertTrue(doc["healthy"])
            self.assertTrue(doc["index_exists"])

            state_contracts.register_hidden_session(session_key="codex:h1", owner="demo")
            plugins.remove_plugin("demo", purge_state=True)
            self.assertIsNone(plugins.resolve_command("demo"))
            self.assertEqual(state_contracts.list_hidden_sessions(owner="demo"), [])


class CoreContractsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.xdg = Path(self.tmp.name) / "xdg"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_hidden_sessions_contract_filters_store_and_agent_api(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg)}, clear=False):
            state_contracts.register_hidden_session(
                session_key="codex:hidden-db",
                owner="workflow",
                keepalive_name="corral-wf-hidden",
                group_id="wf_1",
            )
            normal = {"source": "codex", "id": "human-1", "title": "Normal", "first_user_msg": "Hello", "mtime": 200.0}
            by_db = {"source": "codex", "id": "hidden-db", "title": "Child", "first_user_msg": "Work", "mtime": 199.0}
            by_prefix = {
                "source": "claude",
                "id": "hidden-prefix",
                "title": "Child 2",
                "first_user_msg": "[corral-wf:wf_1:s2]\nRun step",
                "mtime": 198.0,
            }

            store = SessionStore(50)
            filtered = store._drop_tombstoned_sessions({"codex": [normal, by_db], "claude": [by_prefix]})
            self.assertEqual([s["id"] for s in filtered["codex"]], ["human-1"])
            self.assertEqual(filtered["claude"], [])

            fake_codex = SimpleNamespace(id="codex", display_name="Codex", scan=lambda limit: [normal, by_db])
            fake_claude = SimpleNamespace(id="claude", display_name="Claude", scan=lambda limit: [by_prefix])

            class _FakeReg:
                def __iter__(self):
                    return iter([fake_codex, fake_claude])

                def get(self, rid):
                    return fake_codex if rid == "codex" else fake_claude

            args_def = SimpleNamespace(
                since=None,
                until=None,
                compact=False,
                format="json",
                top=None,
                fields=None,
                runtime=None,
                limit=50,
                fresh=True,
                status=None,
                cwd=None,
                live=False,
                keepalive=False,
                out=None,
                include_hidden=False,
                include_workflow=False,
            )
            with (
                mock.patch.object(
                    agent_api,
                    "_list_scan",
                    return_value=(
                        {"codex": [normal, by_db], "claude": [by_prefix]},
                        {},
                        {"scan_source": "local", "index_age_s": None},
                    ),
                ),
                mock.patch.object(agent_api.liveness, "annotate"),
                mock.patch.object(agent_api, "_attach_attention"),
                mock.patch.object(agent_api.titles, "load_cache", return_value={}),
                mock.patch.object(
                    agent_api,
                    "session_payload",
                    side_effect=lambda s, c, r, f=None: {"id": s["id"], "runtime": r.id},
                ),
            ):
                res = agent_api.cmd_list(args_def, _FakeReg())
                self.assertEqual([s["id"] for s in res["data"]["sessions"]], ["human-1"])
                self.assertEqual(res["data"]["hidden_sessions"], 2)
                self.assertEqual(res["data"]["hidden_workflow_sessions"], 2)

                args_inc = SimpleNamespace(**{**args_def.__dict__, "include_hidden": True})
                res_all = agent_api.cmd_list(args_inc, _FakeReg())
                self.assertEqual(len(res_all["data"]["sessions"]), 3)
                self.assertEqual(res_all["data"]["hidden_sessions"], 0)

    def test_host_leases_protect_reclaim_and_fail_closed_when_corrupt(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg)}, clear=False):
            now = time.time()
            self.assertEqual(state_contracts.active_leased_hosts(now=now), set())

            state_contracts.acquire_host_lease(
                owner="workflow",
                lease_id="wf1:s1",
                keepalive_name="corral-leased-pane",
                pid=os.getpid(),
                ttl_seconds=120.0,
            )
            self.assertEqual(state_contracts.active_leased_hosts(now=now), {"corral-leased-pane"})

            with (
                mock.patch("corral.attention.AttentionStore.busy_pairs", return_value=set()),
                mock.patch("corral.attention.AttentionStore.waiting_pairs", return_value=set()),
                mock.patch("corral.embed.live_host_viewer_names", return_value=set()),
                mock.patch("corral.split_layout.pinned_keys_effective", return_value=set()),
                mock.patch("corral.control_store.RunStore.active_hosts", return_value=set()),
                mock.patch("corral.reclaim._background_state", return_value=reclaim.BackgroundState(frozenset(), {})),
                mock.patch("corral.reclaim.memory_pressure", return_value=False),
            ):
                ctx = reclaim._build_context(now, [], {})
                self.assertIsNotNone(ctx)
                self.assertIn("corral-leased-pane", ctx.viewed)

            state_contracts.release_host_lease(owner="workflow", lease_id="wf1:s1")
            self.assertEqual(state_contracts.active_leased_hosts(now=now), set())

            # Corrupt DB -> fail closed (returns None, blocking reclaim pass)
            db_path = state_contracts.host_leases_db_path()
            db_path.write_bytes(b"not a sqlite db")
            self.assertIsNone(state_contracts.active_leased_hosts(now=now))
            self.assertEqual(
                reclaim._unreadable_context_source(set(), set(), set(), set(), set(), None),
                "host_leases",
            )


class EndToEndWorkflowPluginTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.xdg = Path(self.tmp.name) / "xdg"
        self.cli_root = Path(__file__).resolve().parent.parent
        self.wf_plugin_src = self.cli_root / "plugins" / "workflow"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_corral(self, *args: str, extra_path: Path | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["XDG_CONFIG_HOME"] = str(self.xdg)
        env["CORRAL_PLUGIN_NO_EXEC"] = "1"
        env["CORRAL_WORKFLOW_ROOT"] = str(self.xdg / "corral" / "workflows")
        src_dir = str(self.cli_root / "src")
        env["PYTHONPATH"] = src_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        if extra_path is not None:
            env["PATH"] = str(extra_path) + os.pathsep + env.get("PATH", "")
        return subprocess.run(
            [sys.executable, "-m", "corral", *args],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_uninstalled_official_hint_install_dispatch_describe_and_path_fallback(self) -> None:
        # 1. Calling workflow before install yields clear structured install hint
        miss = self._run_corral("workflow", "catalog", "--json")
        self.assertEqual(miss.returncode, EXIT_NOT_FOUND)
        miss_env = json.loads(miss.stdout)
        self.assertFalse(miss_env["ok"])
        self.assertEqual(miss_env["error"]["code"], "plugin_not_installed")
        self.assertTrue(any("plugin install workflow" in cmd for cmd in miss_env["error"]["next_commands"]))

        # 2. Install editable workflow plugin via CLI
        inst = self._run_corral("plugin", "install", "--editable", str(self.wf_plugin_src), "--json")
        self.assertEqual(inst.returncode, EXIT_OK, inst.stderr or inst.stdout)
        inst_env = json.loads(inst.stdout)
        self.assertTrue(inst_env["ok"])
        self.assertEqual(inst_env["data"]["name"], "workflow")

        # 3. `corral describe` & `corral describe workflow` & `corral describe workflow run` read describe.json
        desc_all = self._run_corral("describe", "--json")
        self.assertEqual(desc_all.returncode, EXIT_OK, desc_all.stderr)
        desc_all_data = json.loads(desc_all.stdout)["data"]
        self.assertTrue(any(p["name"] == "workflow" for p in desc_all_data["plugins"]))

        desc_wf = self._run_corral("describe", "workflow", "--json")
        self.assertEqual(desc_wf.returncode, EXIT_OK, desc_wf.stderr)
        wf_cmds = json.loads(desc_wf.stdout)["data"]["commands"]
        self.assertTrue(any(c["name"] == "workflow run" for c in wf_cmds))

        desc_leaf = self._run_corral("describe", "wf", "run", "--json")
        self.assertEqual(desc_leaf.returncode, EXIT_OK, desc_leaf.stderr)
        self.assertEqual(json.loads(desc_leaf.stdout)["data"]["name"], "workflow run")

        # 4. Dispatch `corral workflow catalog --compact` & `corral wf list --json`
        cat = self._run_corral("workflow", "catalog", "--compact")
        self.assertEqual(cat.returncode, EXIT_OK, cat.stderr or cat.stdout)
        cat_env = json.loads(cat.stdout)
        self.assertTrue(cat_env["ok"])
        self.assertIn("profiles", cat_env["data"])
        self.assertIn("targets", cat_env["data"])

        wfls = self._run_corral("wf", "list", "--json")
        self.assertEqual(wfls.returncode, EXIT_OK, wfls.stderr or wfls.stdout)
        self.assertTrue(json.loads(wfls.stdout)["ok"])

        # 5. Remote hello capabilities expose installed plugin namespace
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.xdg)}, clear=False):
            caps = RemoteService._remote_plugin_capabilities(SimpleNamespace())
            self.assertEqual(caps["workflow"]["namespaces"], ["workflow."])

        # 6. PATH fallback for `corral-<cmd>` executables
        bin_dir = Path(self.tmp.name) / "bin"
        bin_dir.mkdir()
        ext_bin = bin_dir / "corral-echoext"
        ext_bin.write_text(
            f"#!{sys.executable}\nimport json, sys\nprint(json.dumps({{'ext': sys.argv[1:]}}))\n",
            encoding="utf-8",
        )
        os.chmod(ext_bin, 0o755)
        ext = self._run_corral("echoext", "--hello", "world", extra_path=bin_dir)
        self.assertEqual(ext.returncode, EXIT_OK, ext.stderr)
        self.assertEqual(json.loads(ext.stdout)["ext"], ["--hello", "world"])

        # 7. Internal CLI lease + hidden session contract
        lacq = self._run_corral(
            "internal", "lease", "acquire",
            "--owner", "ext", "--lease-id", "l1", "--keepalive", "tmux-p1",
        )
        self.assertEqual(lacq.returncode, EXIT_OK, lacq.stderr)
        lrel = self._run_corral("internal", "lease", "release", "--owner", "ext", "--lease-id", "l1")
        self.assertEqual(lrel.returncode, EXIT_OK, lrel.stderr)

        # 8. Core upgrade simulation: even if a fresh Core venv runs with same XDG_CONFIG_HOME,
        # plugin persists cleanly without reinstall.
        ls = self._run_corral("plugin", "list", "--format", "tsv")
        self.assertEqual(ls.returncode, EXIT_OK)
        self.assertIn("workflow\t0.1.0\ttrue\tworkflow,wf", ls.stdout)
        _ = sqlite3  # keep sqlite3 imported check cleanly used if needed
        _ = EXIT_USAGE


if __name__ == "__main__":
    unittest.main()
