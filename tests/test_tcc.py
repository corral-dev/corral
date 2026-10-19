"""macOS TCC 跳板（corral tcc）的安装、幂等与 C 透传测试。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from corral import tcc


def _isolated_cache(test: unittest.TestCase) -> str:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    test.addCleanup(os.environ.pop, "CORRAL_CACHE_DIR", None)
    old = os.environ.get("CORRAL_CACHE_DIR")
    if old is not None:
        test.addCleanup(os.environ.__setitem__, "CORRAL_CACHE_DIR", old)
    os.environ["CORRAL_CACHE_DIR"] = tmp.name
    return tmp.name


class SourceTagTests(unittest.TestCase):
    def test_tag_is_stable_and_hex(self) -> None:
        tag = tcc.source_tag()
        self.assertEqual(tag, tcc.source_tag())
        self.assertEqual(len(tag), 16)
        int(tag, 16)

    def test_launcher_dir_follows_cache_env(self) -> None:
        _isolated_cache(self)
        self.assertTrue(tcc.launcher_dir().endswith(os.path.join("tcc")))
        self.assertTrue(tcc.launcher_dir().startswith(os.environ["CORRAL_CACHE_DIR"]))


class SidecarTests(unittest.TestCase):
    def test_missing_and_corrupt_sidecar_is_none(self) -> None:
        _isolated_cache(self)
        self.assertIsNone(tcc.read_sidecar())
        sidecar = Path(tcc.sidecar_path())
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        sidecar.write_text("{nope", encoding="utf-8")
        self.assertIsNone(tcc.read_sidecar())

    def test_wrap_refuses_self_reference(self) -> None:
        _isolated_cache(self)
        with mock.patch.object(tcc.sys, "platform", "darwin"):
            with self.assertRaises(tcc.TccError):
                tcc._resolve_wrap_target(None, tcc.launcher_path(), None)


class WrapOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        cache = _isolated_cache(self)
        # wrap 默认会往 ~/.local/bin 放软链：测试里一律指到隔离目录，绝不碰家目录。
        self.link_dir = os.path.join(cache, "bin")
        os.makedirs(self.link_dir, exist_ok=True)
        self.platform_patch = mock.patch.object(tcc.sys, "platform", "darwin")
        self.platform_patch.start()
        self.addCleanup(self.platform_patch.stop)
        for _tool in ("cc", "codesign"):
            patcher = mock.patch.object(tcc.shutil, "which",
                                        side_effect=lambda name, orig=tcc.shutil.which:
                                        f"/usr/bin/{name}" if name in ("cc", "codesign") else orig(name))
            patcher.start()
            self.addCleanup(patcher.stop)
        self.builds: list[str] = []

        def fake_compile(dest: str, *, cc: str, sign_identity: str) -> None:
            self.builds.append(dest)
            Path(dest).write_bytes(b"fake-launcher-bytes")

        self.compile_patch = mock.patch.object(tcc, "_compile_and_sign", fake_compile)
        self.compile_patch.start()
        self.addCleanup(self.compile_patch.stop)

    def test_wrap_is_idempotent_and_caches_bytes(self) -> None:
        first = tcc.wrap(link_dir=self.link_dir)
        self.assertTrue(os.path.isfile(first["path"]))
        self.assertTrue(os.path.isfile(first["cache"]))
        self.assertEqual(len(self.builds), 1)
        sidecar = tcc.read_sidecar()
        self.assertIsNotNone(sidecar)
        assert sidecar is not None
        self.assertTrue(sidecar["argv_prefix"])

        second = tcc.wrap(link_dir=self.link_dir)
        self.assertEqual(len(self.builds), 1)  # 缓存命中，不重编
        self.assertFalse(second["changed"])
        self.assertFalse(second["built"])
        self.assertEqual(tcc.read_sidecar(), sidecar)

    def test_rebuild_replaces_cache(self) -> None:
        tcc.wrap(link_dir=self.link_dir)
        builds_before = len(self.builds)
        result = tcc.wrap(rebuild=True, link_dir=self.link_dir)
        self.assertTrue(result["built"])
        self.assertEqual(len(self.builds), builds_before + 1)

    def test_status_never_raises(self) -> None:
        tcc.wrap(link_dir=self.link_dir)
        data = tcc.status()
        self.assertEqual(data["identifier"], tcc.IDENTIFIER)
        self.assertIsNotNone(data["launcher"])
        self.assertIsNotNone(data["python"])

    def test_cli_status_json_envelope(self) -> None:
        tcc.wrap(link_dir=self.link_dir)
        from io import StringIO

        buf = StringIO()
        with mock.patch.object(sys, "stdout", buf):
            code = tcc.cli_main(["status", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(buf.getvalue())
        self.assertTrue(payload["ok"])
        self.assertIn("launcher", payload["data"])

    def test_cli_wrap_rejects_non_macos(self) -> None:
        with mock.patch.object(tcc.sys, "platform", "linux"):
            with self.assertRaises(tcc.TccError):
                tcc.wrap(link_dir=self.link_dir)


@unittest.skipUnless(shutil.which("cc"), "需要系统 cc 才测真正的 C 跳板")
class TrampolineBinaryTests(unittest.TestCase):
    def _build(self, directory: str, *, sign: bool) -> str:
        src = os.path.join(directory, "corral-tcc.c")
        dest = os.path.join(directory, "corral-tcc")
        Path(src).write_text(tcc.TRAMPOLINE_C, encoding="utf-8")
        proc = subprocess.run(["cc", "-O2", "-std=c99", "-Wall", "-o", dest, src],
                              capture_output=True, timeout=120, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-1000:])
        if sign and sys.platform == "darwin":
            proc = subprocess.run(
                ["codesign", "--force", "--sign", "-",
                 "--identifier", tcc.IDENTIFIER, dest],
                capture_output=True, timeout=60, check=False)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode()[-500:])
        os.chmod(dest, 0o755)
        return dest

    def _run_launcher(self, launcher: str, sidecar: dict, args: list[str]) -> subprocess.CompletedProcess[str]:
        Path(launcher + ".json").write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
        return subprocess.run([launcher, *args], capture_output=True, text=True, timeout=30, check=False)

    def test_missing_sidecar_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launcher = self._build(directory, sign=False)
            proc = subprocess.run([launcher], capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(proc.returncode, 2)

    def test_forwards_args_stdout_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = os.path.join(directory, "probe.py")
            Path(script).write_text(
                "import sys; print('poke:' + ','.join(sys.argv[1:])); sys.exit(42)\n",
                encoding="utf-8")
            launcher = self._build(directory, sign=False)
            proc = self._run_launcher(
                launcher,
                {"python": sys.executable, "argv_prefix": [script]},
                ["a", "b c"],
            )
            self.assertEqual(proc.returncode, 42)
            self.assertEqual(proc.stdout.strip(), "poke:a,b c")

    def test_direct_mode_without_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launcher = self._build(directory, sign=False)
            proc = self._run_launcher(
                launcher, {"python": "", "argv_prefix": ["/bin/echo", "hi"]}, ["there"])
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout.strip(), "hi there")

    def test_module_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            launcher = self._build(directory, sign=False)
            proc = self._run_launcher(
                launcher,
                {"python": sys.executable, "argv_prefix": ["-c", "import sys; sys.exit(7)"]},
                [])
            self.assertEqual(proc.returncode, 7)


if __name__ == "__main__":
    unittest.main()
