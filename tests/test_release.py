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


if __name__ == "__main__":
    unittest.main()
