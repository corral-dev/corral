#!/usr/bin/env python3
"""Behavioral tests for the sanitized public-source exporter.

Each test drives scripts/export_public_source.py as a fresh subprocess
against a disposable Git repository and asserts observable outcomes:
output bytes, exit codes, receipt shape and cleanup. Tests never import
the script internals, so they verify behavior rather than mirroring code.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ENTRY = Path(__file__).resolve().parent.parent / "scripts" / "export_public_source.py"


class ExportHarness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="corral-export-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")

    def git(self, *args):
        result = subprocess.run(
            ["git", "-C", str(self.repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def commit_all(self, message):
        self.git("add", "-A")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD")

    def run_export(self, *args):
        return subprocess.run(
            ["python3", str(ENTRY), *args],
            capture_output=True,
            text=True,
            check=False,
        )

    def export(self, output_name="public", extra=()):
        output = self.root / output_name
        result = self.run_export("--source", str(self.repo), "--output", str(output), *extra)
        return result, output

    def receipt_of(self, result):
        return json.loads(result.stdout)


class HistoryBoundaryTests(ExportHarness):
    def test_untracked_git_and_dirty_files_excluded(self):
        (self.repo / "app.py").write_text("print('committed')\n")
        head = self.commit_all("base")
        (self.repo / "app.py").write_text("print('dirty edit')\n")
        (self.repo / "TOP-SECRET.txt").write_text("candidate secret\n")
        result, output = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = self.receipt_of(result)
        self.assertEqual(receipt["commit"], head)
        self.assertEqual((output / "app.py").read_text(), "print('committed')\n")
        self.assertFalse((output / "TOP-SECRET.txt").exists())
        self.assertFalse((output / ".git").exists())
        self.assertEqual(receipt["git_history"], "excluded")
        self.assertEqual(receipt["untracked"], "excluded")

    def test_exact_commit_used_despite_dirty_workspace(self):
        (self.repo / "data.txt").write_text("version one\n")
        first = self.commit_all("first")
        (self.repo / "data.txt").write_text("version two\n")
        self.commit_all("second")
        (self.repo / "data.txt").write_text("dirty version\n")
        result, output = self.export(extra=("--commit", first))
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = self.receipt_of(result)
        self.assertEqual(receipt["commit"], first)
        self.assertEqual(receipt["requested_commit"], first)
        self.assertEqual((output / "data.txt").read_text(), "version one\n")

    def test_unknown_commit_rejected_before_mutation(self):
        (self.repo / "a.txt").write_text("a\n")
        self.commit_all("base")
        result, output = self.export(extra=("--commit", "deadbee" * 6))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())


class ContentFidelityTests(ExportHarness):
    def test_binary_and_executable_preserved(self):
        binary = bytes(range(256)) * 4 + b"\x00\xff binary tail"
        (self.repo / "blob.bin").write_bytes(binary)
        script = self.repo / "run.sh"
        script.write_text("#!/bin/sh\necho hi\n")
        script.chmod(0o755)
        (self.repo / "plain.txt").write_text("plain\n")
        self.commit_all("assets")
        result, output = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((output / "blob.bin").read_bytes(), binary)
        self.assertTrue(os.access(output / "run.sh", os.X_OK))
        mode = stat.S_IMODE(os.stat(output / "run.sh").st_mode)
        self.assertEqual(mode, 0o755)
        receipt = self.receipt_of(result)
        by_path = {entry["path"]: entry for entry in receipt["files"]}
        self.assertEqual(by_path["blob.bin"]["sha256"], hashlib.sha256(binary).hexdigest())
        self.assertEqual(by_path["run.sh"]["mode"], "100755")
        self.assertEqual(by_path["plain.txt"]["mode"], "100644")
        expected = subprocess.run(
            ["git", "-C", str(self.repo), "show", "HEAD:plain.txt"],
            capture_output=True,
            check=True,
        ).stdout
        self.assertEqual((output / "plain.txt").read_bytes(), expected)

    def test_exact_exclusion_and_byte_equality(self):
        (self.repo / "keep.py").write_text("keep\n")
        (self.repo / "internal-notes.md").write_text("internal\n")
        self.commit_all("base")
        recipe = self.root / "recipe.json"
        recipe.write_text(json.dumps({"exclude": ["internal-notes.md"]}))
        result, output = self.export(extra=("--recipe", str(recipe)))
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = self.receipt_of(result)
        self.assertEqual((output / "keep.py").read_text(), "keep\n")
        self.assertFalse((output / "internal-notes.md").exists())
        self.assertEqual(receipt["exclusions"], ["internal-notes.md"])
        self.assertFalse(receipt["files"][0]["transformed"])


class RejectionTests(ExportHarness):
    def test_symlink_rejected_without_output(self):
        (self.repo / "real.txt").write_text("real\n")
        os.symlink("real.txt", self.repo / "link.txt")
        self.git("add", "-A")
        self.git("commit", "-qm", "symlink")
        result, output = self.export()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symlink", result.stderr.lower() + result.stdout.lower())
        self.assertFalse(output.exists())

    def test_gitmodules_and_submodule_rejected(self):
        (self.repo / ".gitmodules").write_text('[submodule "dep"]\n\tpath = dep\n\turl = https://example.invalid/dep.git\n')
        self.git("update-index", "--add", "--cacheinfo", "160000", "4b825dc642cb6eb9a060e54bf8d69288fbee4904", "dep")
        self.git("commit", "-qm", "nested")
        result, output = self.export()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_output_clobber_refused(self):
        (self.repo / "a.txt").write_text("a\n")
        self.commit_all("base")
        output = self.root / "public"
        output.mkdir()
        sentinel = output / "sentinel.txt"
        sentinel.write_text("other task\n")
        result = self.run_export("--source", str(self.repo), "--output", str(output))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(sentinel.read_text(), "other task\n")

    def test_relative_source_and_output_rejected(self):
        result = self.run_export("--source", "relative/path", "--output", str(self.root / "out"))
        self.assertEqual(result.returncode, 2)

    def test_unsafe_recipe_paths_rejected(self):
        (self.repo / "a.txt").write_text("a\n")
        self.commit_all("base")
        recipe = self.root / "recipe.json"
        recipe.write_text(json.dumps({"exclude": ["../outside.txt"]}))
        result, output = self.export(extra=("--recipe", str(recipe)))
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())


class PrivacyTests(ExportHarness):
    def test_private_replacement_values_not_emitted(self):
        (self.repo / "config.py").write_text('ENDPOINT = "https://private-relay-abc123.example.net"\n')
        (self.repo / "notes.md").write_text("token = PRIVATE-TOKEN-abc123\n")
        self.commit_all("base")
        full = self.root / "full-note.md"
        full.write_text("See the private runbook section 7.\n")
        recipe = self.root / "recipe.json"
        recipe.write_text(
            json.dumps(
                {
                    "replace": {
                        "config.py": {
                            "replacements": [
                                {
                                    "old": "https://private-relay-abc123.example.net",
                                    "new": "https://relay.example.invalid",
                                }
                            ]
                        },
                        "notes.md": {"replacement_file": "full-note.md"},
                    }
                }
            )
        )
        receipt_path = self.root / "receipt.json"
        result, output = self.export(extra=("--recipe", str(recipe), "--receipt", str(receipt_path)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("https://relay.example.invalid", (output / "config.py").read_text())
        combined = result.stdout + receipt_path.read_text()
        self.assertNotIn("private-relay-abc123", combined)
        self.assertNotIn("PRIVATE-TOKEN-abc123", combined)
        tree_text = b"".join(path.read_bytes() for path in sorted(output.rglob("*")) if path.is_file())
        self.assertNotIn(b"PRIVATE-TOKEN-abc123", tree_text)
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(
            receipt["transforms"],
            {"config.py": ["replacements"], "notes.md": ["replacement_file"]},
        )

    def test_forbidden_value_stops_success_and_cleans_up(self):
        (self.repo / "doc.md").write_text("Contact INTERNAL-HOST-xyz for access.\n")
        self.commit_all("base")
        recipe = self.root / "recipe.json"
        recipe.write_text(json.dumps({"forbidden_strings": ["INTERNAL-HOST-xyz"]}))
        result, output = self.export(extra=("--recipe", str(recipe)))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists(), "failed export must clean up its output directory")
        self.assertNotIn("INTERNAL-HOST-xyz", result.stdout)

    def test_forbidden_list_checker_input(self):
        (self.repo / "doc.md").write_text("Contains FORBIDDEN-MARKER-42.\n")
        self.commit_all("base")
        checker = self.root / "checker.txt"
        checker.write_text("FORBIDDEN-MARKER-42\n")
        result, output = self.export(extra=("--forbidden-list", str(checker)))
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_personal_path_rejected_placeholder_allowed(self):
        (self.repo / "bad.md").write_text("stored at /Users/somebody Else/docs\n")
        (self.repo / "good.md").write_text("stored at /Users/<name>/docs and /Users/example/docs\n")
        self.commit_all("base")
        result, output = self.export()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())
        (self.repo / "bad.md").write_text("no paths here\n")
        self.git("add", "-A")
        self.git("commit", "-qm", "cleaned")
        result, output = self.export()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_ui_and_resources_cannot_be_transformed(self):
        ui_file = self.repo / "apple" / "Shared" / "UI" / "Widget.swift"
        ui_file.parent.mkdir(parents=True)
        ui_file.write_text("struct Widget {}\n")
        resources = self.repo / "apple" / "Shared" / "Resources" / "strings.xcstrings"
        resources.parent.mkdir(parents=True)
        resources.write_text("{}\n")
        view = self.repo / "apple" / "iOS" / "SessionView.swift"
        view.parent.mkdir(parents=True)
        view.write_text("struct SessionView {}\n")
        (self.repo / "tool.py").write_text("tool\n")
        self.commit_all("base")
        recipe = self.root / "recipe.json"
        for target, old in (
            ("apple/Shared/UI/Widget.swift", "Widget"),
            ("apple/Shared/Resources/strings.xcstrings", "{}"),
            ("apple/iOS/SessionView.swift", "View"),
        ):
            recipe.write_text(
                json.dumps({"replace": {target: {"replacements": [{"old": old, "new": "Changed"}]}}})
            )
            result, output = self.export(extra=("--recipe", str(recipe)))
            self.assertEqual(result.returncode, 2, target)
            self.assertFalse(output.exists())
        recipe.write_text(json.dumps({"exclude": ["apple/Shared/UI/Widget.swift"]}))
        result, output = self.export(output_name="public-excl", extra=("--recipe", str(recipe)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((output / "apple" / "Shared" / "UI" / "Widget.swift").exists())
        self.assertEqual((output / "apple" / "iOS" / "SessionView.swift").read_text(), "struct SessionView {}\n")

    def test_core_transform_allowed(self):
        adapter = self.repo / "apple" / "Shared" / "Core" / "Store" / "HostKeychain.swift"
        adapter.parent.mkdir(parents=True)
        adapter.write_text('let group = "HARDCODED-TEAM-ACCESS-GROUP"\n')
        (self.repo / "apple" / "Shared" / "UI" / "Widget.swift").parent.mkdir(parents=True)
        (self.repo / "apple" / "Shared" / "UI" / "Widget.swift").write_text("struct Widget {}\n")
        self.commit_all("base")
        replacement = self.root / "public-adapter.swift"
        replacement.write_text("let group = infoPlistSharedAccessGroup()\n")
        recipe = self.root / "recipe.json"
        recipe.write_text(
            json.dumps(
                {
                    "replace": {
                        "apple/Shared/Core/Store/HostKeychain.swift": {"replacement_file": "public-adapter.swift"}
                    }
                }
            )
        )
        result, output = self.export(extra=("--recipe", str(recipe)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (output / "apple" / "Shared" / "Core" / "Store" / "HostKeychain.swift").read_text(),
            "let group = infoPlistSharedAccessGroup()\n",
        )
        self.assertEqual((output / "apple" / "Shared" / "UI" / "Widget.swift").read_text(), "struct Widget {}\n")
        receipt = self.receipt_of(result)
        self.assertEqual(
            receipt["transforms"],
            {"apple/Shared/Core/Store/HostKeychain.swift": ["replacement_file"]},
        )


class ReceiptShapeTests(ExportHarness):
    def test_receipt_bounded_and_names_only(self):
        (self.repo / "a.txt").write_text("secret-free\n")
        head = self.commit_all("base")
        result, _ = self.export()
        receipt = self.receipt_of(result)
        self.assertTrue(receipt["ok"])
        self.assertEqual(receipt["tool"], "export_public_source")
        self.assertEqual(receipt["receipt_version"], 1)
        self.assertEqual(receipt["commit"], head)
        self.assertEqual(receipt["file_count"], len(receipt["files"]))
        self.assertLessEqual(len(result.stdout), 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
