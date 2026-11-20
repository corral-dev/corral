"""scripts/release.py helpers: versions, notes, scope, warning and failure gates."""

from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("release_tool", ROOT / "scripts" / "release.py")
assert SPEC and SPEC.loader
release = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = release
SPEC.loader.exec_module(release)

PROJECT = """settings:
  base:
    MARKETING_VERSION: "1.0.200"
    CURRENT_PROJECT_VERSION: "211"
targets:
  CorralMac:
    settings:
      base:
        MARKETING_VERSION: "0.3.156"
        CURRENT_PROJECT_VERSION: "160"
"""

CHANGELOG = """# Changelog

Intro.

## Unreleased

Release: minor

### English

- Added a thing.

### 简体中文

- 新增一项功能。

## 0.24.356

### English

- Old.
"""


class ContextLogTests(unittest.TestCase):
    def test_fresh_direct_phase_log_creates_parent_and_preserves_existing_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = object.__new__(release.Context)
            ctx.logs = Path(tmp) / "fresh-release" / "logs"
            log = ctx.log("ios.deliver")
            log.write_text("confirmed delivery")
            self.assertEqual(ctx.log("ios.deliver"), log)
            self.assertEqual(log.read_text(), "confirmed delivery")


class VersionTests(unittest.TestCase):
    def test_steps(self):
        self.assertEqual(release.next_version("0.24.356", "patch"), "0.24.357")
        self.assertEqual(release.next_version("0.24.356", "minor"), "0.25.0")
        self.assertEqual(release.next_version("0.24.356", "major"), "1.0.0")

    def test_apple_blocks_are_distinct(self):
        self.assertEqual(release.apple_versions(PROJECT), {"ios": ("1.0.200", 211), "mac": ("0.3.156", 160)})
        bumped = release.bump_apple_project(PROJECT, {"mac": ("0.3.157", 161)})
        self.assertEqual(release.apple_versions(bumped), {"ios": ("1.0.200", 211), "mac": ("0.3.157", 161)})

    def test_cli_files_bump_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            for name in release.CLI_VERSION_FILES:
                (repo / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, repo / name)
            current = release.cli_version(repo)
            new = release.next_version(current, "patch")
            release.bump_cli_files(repo, current, new)
            for name, template in release.CLI_VERSION_FILES.items():
                self.assertIn(template.format(new), (repo / name).read_text(), name)
            with self.assertRaises(release.Failure):
                release.bump_cli_files(repo, current, new)


class NotesTests(unittest.TestCase):
    def test_unreleased_reads_step_and_readiness(self):
        info = release.unreleased(CHANGELOG)
        self.assertEqual((info["notes"], info["step"], info["hold"]), ("ready", "minor", None))
        self.assertTrue(info["body"].startswith("### English"))
        self.assertNotIn("Release:", info["body"])

    def test_missing_language_or_hold(self):
        self.assertEqual(release.unreleased(CHANGELOG.replace("### 简体中文", "### Other"))["notes"], "missing")
        held = release.unreleased(CHANGELOG.replace("Release: minor", "Release: hold phone acceptance"))
        self.assertEqual(held["hold"], "phone acceptance")
        self.assertEqual(release.unreleased("# Changelog\n")["notes"], "missing")

    def test_promote_keeps_history(self):
        promoted = release.promote_unreleased(CHANGELOG, "0.25.0")
        self.assertNotIn("## Unreleased", promoted)
        self.assertNotIn("Release: minor", promoted)
        self.assertLess(promoted.index("## 0.25.0"), promoted.index("## 0.24.356"))
        self.assertIn("- Added a thing.\n\n### 简体中文", promoted)

    def test_apple_heading(self):
        self.assertEqual(release.apple_heading({"mac": ("0.3.157", 161), "ios": ("1.0.201", 212)}),
                         "Mac 0.3.157 (161) / iPhone 1.0.201 (212)")


class ScopeTests(unittest.TestCase):
    def test_product_paths(self):
        files = ["docs/a.md", "tests/test_x.py", "src/corral/ui/x.py", "Cargo.lock", "scripts/release.py"]
        self.assertEqual(release.changed_product_files(files, "cli"), ["src/corral/ui/x.py", "Cargo.lock"])
        apple = ["Shared/UI/A.swift", "macOS/B.swift", "iOS/C.swift", "docs/x.md", "Tests/T.swift"]
        self.assertEqual(release.changed_product_files(apple, "mac"), ["Shared/UI/A.swift", "macOS/B.swift"])
        self.assertEqual(release.changed_product_files(apple, "ios"), ["Shared/UI/A.swift", "iOS/C.swift"])


class GateTests(unittest.TestCase):
    LOG = (
        "/tmp/x/Shared/UI/Chat/ChatMarkdown.swift:12:5: warning: conformance of 'NSFont' to 'Sendable' is unavailable\n"
        "    |                 `- warning: conformance of 'NSFont' to 'Sendable' is unavailable\n"
        "2026-10-27 10:17:37.272 appintentsmetadataprocessor[96143:826261] warning: "
        "Metadata extraction skipped, no AppIntents.framework dependency found\n"
    ) + """--- xcodebuild: WARNING: Using the first of multiple matching destinations:
    export SUPPRESS_WARNINGS\\=YES
/tmp/x/macOS/New.swift:3:1: warning: 'foo' is deprecated
warning: linker stderr: ignoring deprecated linker optimization setting "1"
"""

    def test_warnings_are_normalized_and_deduplicated(self):
        self.assertEqual(release.warnings_in(self.LOG + self.LOG), [
            "ChatMarkdown.swift: conformance of 'NSFont' to 'Sendable' is unavailable",
            "appintentsmetadataprocessor: Metadata extraction skipped, no AppIntents.framework dependency found",
            "xcodebuild: Using the first of multiple matching destinations:",
            "New.swift: 'foo' is deprecated",
            'warning: linker stderr: ignoring deprecated linker optimization setting "1"',
        ])

    def test_only_unregistered_warnings_block(self):
        register = ROOT.parent / "apple/scripts/release_known_issues.json"
        if not register.exists():
            self.skipTest("the Apple repository is absent from public CLI checkouts")
        known = release.load_known([register])["warnings"]
        matched, new = release.unknown_warnings(release.warnings_in(self.LOG), known, "apple")
        self.assertIn("chat-markdown-nsfont-sendable", matched)
        self.assertEqual(new, ["New.swift: 'foo' is deprecated",
                               'warning: linker stderr: ignoring deprecated linker optimization setting "1"'])

    def test_linux_failures_match_registered_ids_including_subtests(self):
        known = release.load_known([ROOT / "scripts/release_known_issues.json"])["test_failures"]
        failed = ["test_titlegen.GatewayTitleGeneratorTests.test_missing_url_with_key_makes_no_request (model=None)",
                  "test_new.T.test_regression"]
        matched, new = release.unknown_failures(failed, known, "linux")
        self.assertEqual(matched, [failed[0].split(" (")[0]])
        self.assertEqual(new, ["test_new.T.test_regression"])
        self.assertEqual(release.unknown_failures(failed[:1], known, "macos")[1], failed[:1])

    def test_registers_are_complete(self):
        apple = ROOT.parent / "apple/scripts/release_known_issues.json"
        for path in (ROOT / "scripts/release_known_issues.json", apple):
            if not path.exists():
                continue  # The Apple repository is absent from public CLI checkouts.
            data = json.loads(path.read_text())
            for entry in data["warnings"]:
                re.compile(entry["match"])
                self.assertTrue({"id", "scopes", "origin", "reason", "risk", "follow_up", "recorded"} <= set(entry))
            for entry in data.get("test_failures", []):
                self.assertTrue({"id", "platform", "reason", "follow_up", "recorded"} <= set(entry))

    def test_ci_report_reads_last_line(self):
        log = ('x\nCORRAL_CI_TEST_REPORT:{"phase": "first-pass"}\n'
               'CORRAL_CI_TEST_REPORT:{"phase": "final", "failed_ids": []}\n')
        self.assertEqual(release.ci_report(log)["phase"], "final")
        self.assertIsNone(release.ci_report("no report"))


class ViewTests(unittest.TestCase):
    def test_status_view_is_compact_and_names_next_step(self):
        state = {"id": "r1", "mode": "release", "status": "failed", "created": 0, "finished": 125,
                 "versions": {"cli": {"from": "1.0.0", "to": "1.0.1"}},
                 "phases": {"cli.gate": {"status": "succeeded", "started": 0, "finished": 65},
                            "cli.build": {"status": "failed", "started": 65, "finished": 125,
                                          "error": {"reason": "1 new warning(s)", "excerpt": ["x"], "log": None}}}}
        view = release.status_view(state)
        self.assertEqual(view["phases"], {"cli.gate": "succeeded 1m05s", "cli.build": "failed 1m00s"})
        self.assertEqual(view["failed"], [{"phase": "cli.build", "reason": "1 new warning(s)", "excerpt": ["x"]}])
        self.assertEqual(view["next"], "fix the cause, then resume")


class CoupledReleaseTests(unittest.TestCase):
    def _args(self, components: str | None = None, bump: str | None = None):
        return type("Args", (), {"components": components, "bump": bump})()

    def test_sync_next_version_picks_highest_base_and_step(self) -> None:
        self.assertEqual(release.sync_next_version("1.0.0", "1.0.2", "patch", "minor"), "1.1.0")
        self.assertEqual(release.sync_next_version("1.2.0", "1.0.0", "major", "patch"), "2.0.0")
        self.assertEqual(
            release.sync_next_version("1.0.5", "1.0.3", "patch", "patch", explicit_step="major"),
            "2.0.0",
        )

    def test_plan_couples_cli_and_mac_with_auto_sync_and_leaves_ios_independent(self) -> None:
        plans = {
            "cli": {
                "current": "1.0.0", "next": "1.0.1", "product_files": 2,
                "notes": "ready", "commits": ["a"], "recommended": True,
            },
            "mac": {
                "current": "1.0.0", "next": "1.0.1", "product_files": 0,
                "notes": "missing", "commits": [], "recommended": False,
            },
            "ios": {
                "current": "1.0.221", "next": "1.0.222", "product_files": 0,
                "notes": "missing", "commits": [], "recommended": False,
            },
        }
        apple_vers = {"mac": ("1.0.0", 193), "ios": ("1.0.221", 230)}
        with mock.patch.object(release, "unreleased", side_effect=[
            {"notes": "ready", "step": "minor", "hold": None, "body": "ok"},
            {"notes": "missing", "step": "patch", "hold": None, "body": ""},
        ]), \
             mock.patch.object(release, "apple_versions", return_value=apple_vers), \
             mock.patch.object(release, "cli_version", return_value="1.0.0"), \
             mock.patch.object(release, "published", return_value="1.0.0"), \
             mock.patch.object(release, "component_plan", side_effect=lambda c, *a: dict(plans[c])), \
             mock.patch.object(release, "git", return_value=""), \
             mock.patch.object(release, "latest_state", return_value=None):
            report = release.plan()
        self.assertEqual(report["recommended"], ["cli", "mac"])
        self.assertEqual(report["components"]["cli"]["next"], "1.1.0")
        self.assertEqual(report["components"]["mac"]["next"], "1.1.0")
        self.assertEqual(report["components"]["mac"]["notes"], "auto_sync")
        self.assertTrue(report["components"]["mac"]["sync_only"])
        self.assertEqual(report["components"]["ios"]["next"], "1.0.222")
        self.assertFalse(report["components"]["ios"]["recommended"])

    def test_plan_hold_on_either_side_blocks_both_coupled_components(self) -> None:
        plans = {
            "cli": {
                "current": "1.0.0", "next": "1.0.1", "product_files": 2,
                "notes": "ready", "commits": ["a"], "recommended": True,
            },
            "mac": {
                "current": "1.0.0", "next": "1.0.1", "product_files": 1,
                "notes": "ready", "commits": ["b"], "hold": "waiting", "recommended": False,
            },
            "ios": {
                "current": "1.0.221", "next": "1.0.222", "product_files": 0,
                "notes": "missing", "commits": [], "recommended": False,
            },
        }
        apple_vers = {"mac": ("1.0.0", 193), "ios": ("1.0.221", 230)}
        with mock.patch.object(release, "unreleased", side_effect=[
            {"notes": "ready", "step": "patch", "hold": None, "body": "ok"},
            {"notes": "ready", "step": "patch", "hold": "waiting", "body": "ok"},
        ]), \
             mock.patch.object(release, "apple_versions", return_value=apple_vers), \
             mock.patch.object(release, "cli_version", return_value="1.0.0"), \
             mock.patch.object(release, "published", return_value="1.0.0"), \
             mock.patch.object(release, "component_plan", side_effect=lambda c, *a: dict(plans[c])), \
             mock.patch.object(release, "git", return_value=""), \
             mock.patch.object(release, "latest_state", return_value=None):
            report = release.plan()
        self.assertEqual(report["recommended"], [])
        self.assertEqual(report["components"]["cli"]["coupled_blocked"], "coupled with mac (hold: waiting)")

    def test_start_couples_cli_and_mac_and_keeps_ios_only(self) -> None:
        fake_plan = {
            "blockers": [],
            "recommended": ["cli", "mac"],
            "components": {
                "cli": {"current": "1.0.0", "next": "1.0.1", "product_files": 0, "notes": "missing"},
                "mac": {"current": "1.0.0", "next": "1.0.1", "product_files": 1, "notes": "ready"},
                "ios": {"current": "1.0.221", "next": "1.0.222", "product_files": 1, "notes": "ready"},
            },
        }
        apple_vers = {"mac": ("1.0.0", 193), "ios": ("1.0.221", 230)}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(release, "RELEASES", Path(tmp)), \
                 mock.patch.object(
                     release.time, "strftime", side_effect=["20261120-000001", "20261120-000002"],
                 ), \
                 mock.patch.object(release, "plan", return_value=json.loads(json.dumps(fake_plan))), \
                 mock.patch.object(release, "apple_versions", return_value=apple_vers), \
                 mock.patch.object(release, "unreleased", side_effect=[
                     {"notes": "missing", "step": "patch", "hold": None, "body": ""},
                     {"notes": "ready", "step": "patch", "hold": None, "body": "mac fix"},
                 ]), \
                 mock.patch.object(release, "prepare_clone", return_value="deadbeef"), \
                 mock.patch.object(release, "taskboard"), \
                 mock.patch.object(release, "spawn_drivers"):
                view = release.start(self._args("mac"), rehearse=False)
            state_files = list(Path(tmp).glob("*/state.json"))
            self.assertEqual(len(state_files), 1)
            state = json.loads(state_files[0].read_text())
            self.assertEqual(state["components"], ["cli", "mac"])
            self.assertTrue(state["versions"]["cli"].get("sync_only"))
            self.assertEqual(view["versions"]["cli"], "1.0.1")
            self.assertEqual(view["versions"]["mac"], "1.0.1")

            with mock.patch.object(release, "RELEASES", Path(tmp)), \
                 mock.patch.object(release.time, "strftime", side_effect=["20261120-000003"]), \
                 mock.patch.object(release, "plan", return_value=json.loads(json.dumps(fake_plan))), \
                 mock.patch.object(release, "apple_versions", return_value=apple_vers), \
                 mock.patch.object(release, "unreleased", side_effect=[
                     {"notes": "missing", "step": "patch", "hold": None, "body": ""},
                     {"notes": "ready", "step": "patch", "hold": None, "body": "ios fix"},
                 ]), \
                 mock.patch.object(release, "prepare_clone", return_value="deadbeef"), \
                 mock.patch.object(release, "taskboard"), \
                 mock.patch.object(release, "spawn_drivers"):
                ios_view = release.start(self._args("ios"), rehearse=False)
            self.assertEqual(list(ios_view["versions"]), ["ios"])

    def test_bump_prepends_sync_only_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cli_repo = root / "cli"
            mac_repo = root / "apple"
            ws = root / "ws"
            cli_repo.mkdir()
            mac_repo.mkdir()
            ws.mkdir()
            (cli_repo / "pyproject.toml").write_text('version = "1.0.0"\n')
            (cli_repo / "CHANGELOG.md").write_text("# Changelog\n\n## 1.0.0\n\n### English\n\n- Old.\n")
            (mac_repo / "project.yml").write_text(PROJECT.replace("0.3.156", "1.0.0"))
            (mac_repo / "CHANGELOG.md").write_text("# Changelog\n\n## Mac 1.0.0 (160)\n\n### English\n\n- Old.\n")
            cli_vers = {"cli": {"from": "1.0.0", "to": "1.0.1", "sync_only": True}}
            mac_vers = {"mac": {"from": "1.0.0", "to": "1.0.1", "build": 161, "sync_only": True}}
            with mock.patch.object(release, "bump_cli_files", return_value=["pyproject.toml"]), \
                 mock.patch.object(release, "git") as git_mock:
                release.bump(cli_repo, cli_vers, ws, rehearse=False)
                release.bump(mac_repo, mac_vers, ws, rehearse=False)
            self.assertIn("Synchronized version 1.0.1 with Corral Mac", (cli_repo / "CHANGELOG.md").read_text())
            self.assertIn("与 Corral Mac 同步发布 1.0.1 版本", (ws / "cli-notes.md").read_text())
            self.assertIn("Synchronized version 1.0.1 with Corral CLI", (mac_repo / "CHANGELOG.md").read_text())
            self.assertEqual(git_mock.call_count, 2)

    def test_wait_for_cli_verify_passes_and_fails_fast(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            ctx = object.__new__(release.Context)
            ctx.workspace = ws
            release.write_state(ws, {
                "workspace": str(ws),
                "phases": {
                    "cli.verify": {"lane": "cli", "status": "succeeded"},
                    "mac.publish": {"lane": "mac", "status": "running"},
                },
            })
            release.wait_for_cli_verify(ctx, poll_interval=0.0, timeout=0.1)

            release.write_state(ws, {
                "workspace": str(ws),
                "phases": {
                    "cli.build": {"lane": "cli", "status": "failed"},
                    "cli.verify": {"lane": "cli", "status": "pending"},
                    "mac.publish": {"lane": "mac", "status": "running"},
                },
            })
            with self.assertRaises(release.Failure):
                release.wait_for_cli_verify(ctx, poll_interval=0.0, timeout=0.1)


if __name__ == "__main__":
    unittest.main()
