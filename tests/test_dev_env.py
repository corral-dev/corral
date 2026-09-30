from __future__ import annotations

import contextlib
import io
import json
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

    def test_json_envelope_has_stable_top_level_fields(self) -> None:
        result = dev_env.envelope({"ready": False})
        self.assertEqual(set(result), {"ok", "data", "error", "meta"})
        self.assertIsNone(result["error"])
        failed = dev_env.envelope(error={"code": "not_ready", "message": "prepare first"})
        self.assertFalse(failed["ok"])
        self.assertIsNone(failed["data"])


if __name__ == "__main__":
    unittest.main()
