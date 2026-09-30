from __future__ import annotations

import contextlib
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dev_env  # noqa: E402


class DevEnvironmentTests(unittest.TestCase):
    def _checkout(self, root: Path, name: str = "corral") -> Path:
        package = "corral" if name == "corral" else "sesskit"
        root.mkdir(parents=True, exist_ok=True)
        (root / "pyproject.toml").write_text(
            f'[project]\nname = "{name}"\nversion = "1.2.3"\n', encoding="utf-8"
        )
        source = root / "src" / package
        source.mkdir(parents=True)
        (source / "__init__.py").write_text("__version__ = '1.2.3'\n", encoding="utf-8")
        return root

    def test_explicit_supported_checkout_is_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = self._checkout(Path(temp))
            resolved, name, version = dev_env.validate_repo(str(repo))
        self.assertEqual(resolved, repo.resolve())
        self.assertEqual(name, "corral")
        self.assertEqual(version, "1.2.3")

    def test_unknown_checkout_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = self._checkout(Path(temp), name="other-tool")
            with self.assertRaisesRegex(ValueError, "supported targets"):
                dev_env.validate_repo(str(repo))

    def test_doctor_is_read_only_when_environment_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = self._checkout(Path(temp))
            before = sorted(path.relative_to(repo).as_posix() for path in repo.rglob("*"))
            with mock.patch.object(dev_env.shutil, "which", return_value=None):
                data, ready = dev_env.doctor(str(repo))
            after = sorted(path.relative_to(repo).as_posix() for path in repo.rglob("*"))
        self.assertFalse(ready)
        self.assertIn("checkout .venv is missing", data["blockers"])
        self.assertEqual(before, after)

    def test_prepare_refuses_a_venv_symlink_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            repo = self._checkout(base / "repo")
            outside = base / "existing-environment"
            outside.mkdir()
            marker = outside / "keep"
            marker.write_text("unchanged", encoding="utf-8")
            (repo / ".venv").symlink_to(outside, target_is_directory=True)
            with mock.patch.object(dev_env.shutil, "which", return_value="/bin/uv"):
                ok, log_path, message = dev_env._prepare(repo, "corral", None)
            self.assertFalse(ok)
            self.assertIsNone(log_path)
            self.assertIn("symlink", message)
            self.assertEqual(marker.read_text(encoding="utf-8"), "unchanged")

    def test_run_logs_full_output_and_returns_a_bounded_tail(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = self._checkout(Path(temp))
            code, log_path, tail = dev_env._run_logged(
                [sys.executable, "-c", "for i in range(45): print('line-' + str(i))"], repo=repo
            )
            full_log = log_path.read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        self.assertIn("line-0", full_log)
        self.assertIn("line-44", full_log)
        self.assertLessEqual(len(tail.splitlines()), dev_env._SUMMARY_LINES)
        self.assertLessEqual(max(map(len, tail.splitlines())), 500)

    def test_run_uses_selected_checkout_source_and_virtualenv(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = self._checkout(Path(temp))
            captured: dict[str, object] = {}

            def fake_run(
                argv: list[str],
                *,
                repo: Path,
                env: dict[str, str],
                **_kwargs: object,
            ) -> tuple[int, Path, str]:
                captured["argv"] = argv
                captured["env"] = env
                captured["repo"] = repo
                return 0, repo / ".venv" / "dev-env-logs" / "test.log", "done"

            with (
                mock.patch.object(dev_env, "doctor", return_value=({"ready": True}, True)),
                mock.patch.object(dev_env, "_run_logged", side_effect=fake_run),
                contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                status = dev_env.main(["--json", "run", "--repo", str(repo), "--", "python", "-m", "unittest"])
            result = json.loads(output.getvalue())

        self.assertEqual(status, 0)
        self.assertEqual(result["ok"], True)
        self.assertEqual(Path(captured["argv"][0]), dev_env._venv_python(repo).resolve())
        self.assertEqual(Path(captured["env"]["PYTHONPATH"]), (repo / "src").resolve())
        self.assertEqual(captured["env"]["CORRAL_ISOLATE_MANAGED_HOSTS"], "1")

    def _corral_checkout(self, root: Path) -> Path:
        root.mkdir(parents=True, exist_ok=True)
        (root / "pyproject.toml").write_text(
            '[project]\nname = "corral"\nversion = "9.9.9"\n'
            'dependencies = ["textual>=8.0"]\n'
            "[project.optional-dependencies]\n"
            'remote = ["websockets>=13"]\n',
            encoding="utf-8",
        )
        source = root / "src" / "corral"
        source.mkdir(parents=True)
        (source / "__init__.py").write_text("__version__ = '9.9.9'\n", encoding="utf-8")
        (root / "uv.lock").write_text(
            '[[package]]\nname = "corral"\nversion = "9.9.9"\n'
            '[[package]]\nname = "textual"\nversion = "8.2.8"\n'
            '[[package]]\nname = "websockets"\nversion = "17.0.1"\n',
            encoding="utf-8",
        )
        return root

    def test_lock_status_uses_native_resolution_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = self._checkout(Path(temp))
            status = dev_env._lock_status(repo, "corral", None)
        self.assertEqual(status["state"], "missing")

    def test_pin_source_table_matches_pyproject(self) -> None:
        scripts_dir = Path(dev_env.__file__).resolve().parent
        sys.path.insert(0, str(scripts_dir))
        import sesskit_dep  # noqa: E402

        try:
            table = sesskit_dep.uv_source_table()
            self.assertIn(sesskit_dep.WHEEL_URL, table)
            pyproject = (scripts_dir.parent / "pyproject.toml").read_text(encoding="utf-8")
            self.assertIn(table.strip(), pyproject)
            self.assertIn(f'"sesskit>={sesskit_dep.VERSION}"', pyproject)
            # curl|bash has no local helper: install.sh's hard-coded fallback
            # stayed on 0.2.0 through two 0.2.1 releases (2026-09-30).
            install_sh = (scripts_dir.parent / "install.sh").read_text(encoding="utf-8")
            self.assertIn(sesskit_dep.wheel_requirement(), install_sh)
        finally:
            sys.path.remove(str(scripts_dir))

    def test_dev_ruff_pin_matches_entry(self) -> None:
        scripts_dir = Path(dev_env.__file__).resolve().parent
        pyproject = (scripts_dir.parent / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r"\[dependency-groups\].*?ruff==([0-9.]+)", pyproject, re.DOTALL)
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.group(1), dev_env.RUFF_VERSION)

    def test_artifact_verdict_accepts_metadata_digest(self) -> None:
        pin = {"version": "0.2.1", "url": "https://example.invalid/sesskit.whl", "sha256": "abc"}
        installed = {
            "version": "0.2.1",
            "direct_url": {"url": pin["url"], "archive_info": {"hashes": {"sha256": "abc"}}},
        }
        verdict = dev_env._artifact_verdict(pin, installed, None)
        self.assertTrue(verdict["digest_verified"])
        self.assertTrue(verdict["published_artifact_matches"])

    def test_artifact_verdict_rejects_bare_noop_audit(self) -> None:
        pin = {"version": "0.2.1", "url": "https://example.invalid/sesskit.whl", "sha256": "abc"}
        installed = {"version": "0.2.1", "direct_url": {"url": pin["url"]}}
        bare = dev_env._artifact_verdict(pin, installed, None)
        self.assertTrue(bare["version_matches"])
        self.assertTrue(bare["source_url_matches"])
        self.assertFalse(bare["digest_verified"])
        self.assertFalse(bare["digest_verified_by_installer_receipt"])
        unflagged = {
            "pin_version": "0.2.1",
            "pin_url": pin["url"],
            "pin_sha256": "abc",
            "installed_version": "0.2.1",
        }
        no_flag = dev_env._artifact_verdict(pin, installed, unflagged)
        self.assertFalse(no_flag["digest_verified"])
        receipt = dict(unflagged, installer_verified=True)
        held = dev_env._artifact_verdict(pin, installed, receipt)
        self.assertTrue(held["digest_verified"])
        self.assertTrue(held["published_artifact_matches"])
        self.assertTrue(held["digest_verified_by_installer_receipt"])
        stale_receipt = dict(receipt, pin_sha256="stale")
        stale = dev_env._artifact_verdict(pin, installed, stale_receipt)
        self.assertFalse(stale["digest_verified"])
        self.assertFalse(stale["published_artifact_matches"])

    def test_receipt_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = self._checkout(Path(temp))
            pin = {"version": "0.2.1", "url": "https://example.invalid/s.whl", "sha256": "abc"}
            self.assertIsNone(dev_env._read_receipt(repo))
            path = dev_env._write_receipt(repo, pin, "0.2.1", {"url": pin["url"]})
            loaded = dev_env._read_receipt(repo)
        self.assertTrue(str(path).endswith(dev_env._RECEIPT_NAME))
        self.assertEqual((loaded or {})["pin_sha256"], "abc")

    def test_prepare_dry_run_changes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = self._corral_checkout(Path(temp))
            before = sorted(path.relative_to(repo).as_posix() for path in repo.rglob("*"))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                status = dev_env.main(["--json", "prepare", "--repo", str(repo), "--dry-run"])
            result = json.loads(output.getvalue())
            after = sorted(path.relative_to(repo).as_posix() for path in repo.rglob("*"))
        self.assertEqual(status, 0)
        self.assertTrue(result["ok"])
        self.assertTrue(result["meta"]["dry_run"])
        self.assertGreaterEqual(len(result["data"]["steps"]), 2)
        self.assertEqual(before, after)

    def test_run_dry_run_resolves_without_executing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            repo = self._corral_checkout(Path(temp))
            before = sorted(path.relative_to(repo).as_posix() for path in repo.rglob("*"))
            with contextlib.redirect_stdout(io.StringIO()) as output:
                status = dev_env.main(
                    ["--json", "run", "--repo", str(repo), "--dry-run", "--", "python", "-m", "pytest", "-q"]
                )
            result = json.loads(output.getvalue())
            after = sorted(path.relative_to(repo).as_posix() for path in repo.rglob("*"))
        self.assertEqual(status, 0)
        self.assertTrue(result["meta"]["dry_run"])
        self.assertEqual(result["data"]["cwd"], repo.name)
        self.assertEqual(result["data"]["pythonpath"], str((repo / "src").resolve()))
        self.assertEqual(before, after)

    def test_json_envelope_has_stable_top_level_fields(self) -> None:
        result = dev_env.envelope({"ready": False})
        self.assertEqual(set(result), {"ok", "data", "error", "meta"})
        self.assertIsNone(result["error"])
        failed = dev_env.envelope(error={"code": "not_ready", "message": "prepare first"})
        self.assertFalse(failed["ok"])
        self.assertIsNone(failed["data"])


if __name__ == "__main__":
    unittest.main()
