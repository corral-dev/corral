"""corral.updater：版本比较、安装渠道判定、最新版查询、忽略状态持久化。"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from corral import updater


class VersionCompareTests(unittest.TestCase):
    def test_version_tuple_strips_leading_v(self) -> None:
        self.assertEqual(updater._version_tuple("v0.20.0"), (0, 20, 0))
        self.assertEqual(updater._version_tuple("0.20.0"), (0, 20, 0))

    def test_version_tuple_tolerates_prerelease_suffix(self) -> None:
        self.assertEqual(updater._version_tuple("0.20.0-rc1"), (0, 20, 0))

    def test_is_newer_true_when_later(self) -> None:
        self.assertTrue(updater.is_newer("0.20.1", current=(0, 20, 0)))
        self.assertTrue(updater.is_newer("v0.21.0", current=(0, 20, 0)))

    def test_is_newer_false_when_equal_or_older(self) -> None:
        self.assertFalse(updater.is_newer("0.20.0", current=(0, 20, 0)))
        self.assertFalse(updater.is_newer("0.19.9", current=(0, 20, 0)))


class ChannelDetectionTests(unittest.TestCase):
    def _with_pkg_file(self, path: str):
        import corral

        return mock.patch.object(corral, "__file__", os.path.join(path, "__init__.py"))

    def test_detects_brew_cellar_path(self) -> None:
        with self._with_pkg_file("/home/linuxbrew/.linuxbrew/Cellar/corral/0.20.0/lib/python3.12/site-packages/corral"):
            self.assertEqual(updater.detect_channel(), "brew")

    def test_detects_pip_user_site_packages(self) -> None:
        with mock.patch.object(updater, "_is_pipx_install", return_value=False):
            with mock.patch.object(
                updater.site, "getusersitepackages",
                return_value="/home/user/.local/lib/python3.12/site-packages",
            ):
                with mock.patch.object(updater.site, "getsitepackages", return_value=[]):
                    with self._with_pkg_file("/home/user/.local/lib/python3.12/site-packages/corral"):
                        self.assertEqual(updater.detect_channel(), "pip")

    def test_detects_dev_source_checkout(self) -> None:
        with mock.patch.object(updater, "_is_pipx_install", return_value=False):
            with mock.patch.object(
                updater.site, "getusersitepackages",
                return_value="/home/user/.local/lib/python3.12/site-packages",
            ):
                with mock.patch.object(updater.site, "getsitepackages", return_value=[]):
                    with self._with_pkg_file("/Users/demo/Codes/corral/cli/src/corral"):
                        self.assertEqual(updater.detect_channel(), "dev")

    def test_find_checkout_root_and_stale_warning(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = os.path.join(td, "cli")
            src = os.path.join(root, "src", "corral")
            os.makedirs(src)
            with open(os.path.join(root, "pyproject.toml"), "w", encoding="utf-8") as fh:
                fh.write('[project]\nname = "corral"\nversion = "0.0.0"\n')
            with open(os.path.join(src, "__init__.py"), "w", encoding="utf-8") as fh:
                fh.write("__version__ = '0.0.0'\n")
            self.assertEqual(updater.find_checkout_root(src), root)
            with self._with_pkg_file(src):
                self.assertTrue(updater.is_loaded_from_checkout(root))
                self.assertIsNone(updater.stale_source_warning(cwd=root))
            with self._with_pkg_file(
                "/home/u/.local/pipx/venvs/corral/lib/python3.12/site-packages/corral"
            ):
                warn = updater.stale_source_warning(cwd=root)
                self.assertIsNotNone(warn)
                assert warn is not None
                self.assertIn("dev-install.sh", warn)
                self.assertIn(root, warn)

    def test_detects_pipx_by_venv_metadata(self) -> None:
        # pipx 装的包路径同样含 site-packages，必须靠 venv 根的 pipx_metadata.json
        # 先于 pip 命中——那个 venv 里没有 pip，走 python -m pip 升级必然失败
        with tempfile.TemporaryDirectory() as td:
            with open(os.path.join(td, "pipx_metadata.json"), "w", encoding="utf-8") as fh:
                fh.write("{}")
            with mock.patch.object(updater.sys, "prefix", td), \
                 self._with_pkg_file(os.path.join(td, "lib", "python3.14", "site-packages", "corral")):
                self.assertEqual(updater.detect_channel(), "pipx")

    def test_detects_pipx_by_path_marker(self) -> None:
        with mock.patch.object(updater.sys, "prefix", "/usr"), \
             self._with_pkg_file("/home/u/.local/share/pipx/venvs/corral/lib/python3.12/site-packages/corral"):
            self.assertEqual(updater.detect_channel(), "pipx")

    def test_install_report_includes_paths(self) -> None:
        report = updater.install_report()
        self.assertIn("version", report)
        self.assertTrue(os.path.isabs(report["package_file"]))
        self.assertIn(report["channel"], ("brew", "pipx", "pip", "dev"))
        self.assertIn("loaded_from_checkout", report)
        self.assertIn("stale_source_warning", report)

    def test_is_updatable_for_managed_channels_only(self) -> None:
        self.assertTrue(updater.is_updatable("brew"))
        self.assertTrue(updater.is_updatable("pipx"))
        self.assertTrue(updater.is_updatable("pip"))
        self.assertFalse(updater.is_updatable("dev"))

    def test_update_command_brew(self) -> None:
        self.assertEqual(updater.update_command("0.21.0", "brew"), ["brew", "upgrade", "x0c/tap/corral"])

    def test_update_command_pip_user_site(self) -> None:
        with mock.patch.object(
            updater.site, "getusersitepackages",
            return_value="/home/user/.local/lib/python3.12/site-packages",
        ):
            with self._with_pkg_file("/home/user/.local/lib/python3.12/site-packages/corral"):
                cmd = updater.update_command("0.21.0", "pip", spec="WHEEL_URL")
        self.assertIn("--user", cmd)
        # SessKit 直链必须与 corral 目标同在一条 pip 命令里，否则 PyPI 找不到
        self.assertEqual(cmd[-1], "WHEEL_URL")
        self.assertEqual(cmd[-2], updater.sesskit_wheel_requirement())

    def test_update_command_pipx_uses_pipx_not_pip(self) -> None:
        # pipx venv 里没有 pip：命令必须交给 pipx 自己，且带 --force 才能覆盖已装应用
        cmd = updater.update_command("0.21.0", "pipx", spec="WHEEL_URL")
        assert cmd is not None
        self.assertIn("pipx", os.path.basename(cmd[0]))
        self.assertEqual(cmd[1:], ["install", "--force", "WHEEL_URL"])
        self.assertNotIn("pip", cmd[1:])

    def test_update_command_pipx_never_uses_pip_args(self) -> None:
        # pipx 1.0.0 会把 --pip-args 的值按空格拆散，带空格的 SessKit 直链会被
        # 切碎（`@` 成独立 requirement）。SessKit 预装走 PIP_FIND_LINKS，命令
        # 里禁止出现 --pip-args。
        cmd = updater.update_command("0.21.0", "pipx", spec="WHEEL_URL")
        assert cmd is not None
        self.assertNotIn("--pip-args", cmd)

    def test_update_command_carries_sesskit_direct_url(self) -> None:
        # pip 通道把 SessKit 直链（含 digest）与目标放在同一条命令里，不依赖 PyPI
        req = updater.sesskit_wheel_requirement()
        self.assertTrue(req.startswith("sesskit @ https://github.com/x0c/sesskit/"))
        self.assertIn("#sha256=", req)
        pip_cmd = updater.update_command("0.21.0", "pip", spec="WHEEL_URL")
        assert pip_cmd is not None
        self.assertIn(req, pip_cmd)

    def test_update_command_dev_returns_none(self) -> None:
        self.assertIsNone(updater.update_command("0.21.0", "dev"))


class InstallSpecTests(unittest.TestCase):
    """安装源解析：优先 Release 里适配本机的预编译包，查不到才退回源码。"""

    def _release_payload(self, names: list[str]) -> bytes:
        return json.dumps({
            "assets": [
                {"name": n, "browser_download_url": f"https://example.test/{n}"} for n in names
            ]
        }).encode("utf-8")

    def _urlopen(self, payload: bytes):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return payload

        return mock.patch.object(updater.urllib.request, "urlopen", return_value=_Resp())

    def test_picks_matching_wheel_for_current_platform(self) -> None:
        names = [
            "corral-0.21.0-cp310-abi3-macosx_10_12_x86_64.macosx_11_0_arm64.macosx_10_12_universal2.whl",
            "corral-0.21.0-cp310-abi3-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
            "corral-0.21.0-cp310-abi3-manylinux_2_17_aarch64.manylinux2014_aarch64.whl",
            "corral-0.21.0-cp310-abi3-musllinux_1_2_x86_64.whl",
            "corral-0.21.0-cp310-abi3-musllinux_1_2_aarch64.whl",
            "corral-0.21.0.tar.gz",
        ]
        with self._urlopen(self._release_payload(names)):
            url = updater.release_asset_url("0.21.0")
        if updater.platform.system() in ("Darwin", "Linux"):
            self.assertIsNotNone(url)
            assert url is not None
            self.assertTrue(url.endswith(".whl"))
            self.assertIn("0.21.0", url)
        else:
            self.assertIsNone(url)

    def test_falls_back_to_source_when_no_matching_asset(self) -> None:
        with self._urlopen(self._release_payload(["corral-0.21.0.tar.gz"])):
            self.assertEqual(
                updater.install_spec("0.21.0"),
                "git+https://github.com/corral-dev/corral.git@v0.21.0",
            )

    def test_network_failure_falls_back_to_source(self) -> None:
        with mock.patch.object(
            updater.urllib.request, "urlopen",
            side_effect=updater.urllib.error.URLError("boom"),
        ):
            self.assertEqual(
                updater.install_spec("0.21.0"),
                "git+https://github.com/corral-dev/corral.git@v0.21.0",
            )


class SesskitPinSyncTests(unittest.TestCase):
    """updater 里的 SessKit 镜像 pin 必须与 scripts/sesskit_dep.py 同源一致."""

    def test_updater_pin_matches_sesskit_dep(self) -> None:
        import pathlib
        import sys as _sys

        scripts = str(pathlib.Path(__file__).resolve().parent.parent / "scripts")
        _sys.path.insert(0, scripts)
        try:
            import sesskit_dep

            self.assertEqual(updater._SESSKIT_VERSION, sesskit_dep.VERSION)
            self.assertEqual(
                updater.sesskit_wheel_requirement(), sesskit_dep.wheel_requirement()
            )
        finally:
            _sys.path.remove(scripts)
            _sys.modules.pop("sesskit_dep", None)


class EnsureSesskitWheelTests(unittest.TestCase):
    """_ensure_sesskit_wheel：下载 + digest 校验，任何失败返回 None 且不抛异常."""

    def _patch_cache(self, tmpdir: str):
        return mock.patch.object(updater, "CACHE_DIR", tmpdir)

    def _urlopen_with(self, payload: bytes):
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return payload

        return mock.patch.object(updater.urllib.request, "urlopen", return_value=_Resp())

    def test_downloads_and_caches_verified_wheel(self) -> None:
        import hashlib

        payload = b"fake-wheel-bytes"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as td, \
             self._patch_cache(td), \
             self._urlopen_with(payload), \
             mock.patch.object(updater, "_SESSKIT_WHEEL_SHA256", digest):
            first = updater._ensure_sesskit_wheel()
            self.assertIsNotNone(first)
            assert first is not None
            self.assertTrue(os.path.isfile(first))
            # 第二次命中缓存，不再发网络请求
            with mock.patch.object(
                updater.urllib.request, "urlopen",
                side_effect=AssertionError("must use cache"),
            ):
                self.assertEqual(updater._ensure_sesskit_wheel(), first)

    def test_digest_mismatch_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
             self._patch_cache(td), \
             self._urlopen_with(b"tampered-bytes"), \
             mock.patch.object(updater, "_SESSKIT_WHEEL_SHA256", "0" * 64):
            self.assertIsNone(updater._ensure_sesskit_wheel())

    def test_network_error_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td, \
             self._patch_cache(td), \
             mock.patch.object(
                 updater.urllib.request, "urlopen",
                 side_effect=updater.urllib.error.URLError("boom"),
             ):
            self.assertIsNone(updater._ensure_sesskit_wheel())


class RunUpdateTests(unittest.TestCase):
    """run_update：brew 渠道的假成功兜底——退出 0 不等于真的升级了。"""

    def _completed(self, returncode=0, stdout="", stderr=""):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)

    def test_brew_success_when_installed_version_reaches_latest(self) -> None:
        with mock.patch.object(updater.subprocess, "run", return_value=self._completed(0, "ok")), \
             mock.patch.object(updater, "_brew_installed_version", return_value=(0, 21, 0)):
            ok, out = updater.run_update("0.21.0", "brew")
        self.assertTrue(ok)

    def test_brew_reports_failure_when_upgrade_is_noop(self) -> None:
        # brew upgrade 退出 0 但装的还是旧版本（本地配方过期）→ 必须报失败而非假成功
        with mock.patch.object(updater.subprocess, "run", return_value=self._completed(0, "Already up-to-date")), \
             mock.patch.object(updater, "_brew_installed_version", return_value=(0, 20, 0)):
            ok, out = updater.run_update("0.21.0", "brew")
        self.assertFalse(ok)
        self.assertIn("brew update", out)

    def test_brew_clears_no_auto_update_env(self) -> None:
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["env"] = kwargs.get("env")
            return self._completed(0, "ok")

        with mock.patch.dict(updater.os.environ, {"HOMEBREW_NO_AUTO_UPDATE": "1"}, clear=False), \
             mock.patch.object(updater.subprocess, "run", side_effect=fake_run), \
             mock.patch.object(updater, "_brew_installed_version", return_value=(0, 21, 0)):
            ok, _ = updater.run_update("0.21.0", "brew")
        self.assertTrue(ok)
        self.assertNotIn("HOMEBREW_NO_AUTO_UPDATE", captured["env"])

    def test_brew_success_when_version_unqueryable(self) -> None:
        # 查不到已安装版本时不阻断，避免把查询失败误判成升级失败
        with mock.patch.object(updater.subprocess, "run", return_value=self._completed(0, "ok")), \
             mock.patch.object(updater, "_brew_installed_version", return_value=None):
            ok, _ = updater.run_update("0.21.0", "brew")
        self.assertTrue(ok)

    def test_nonzero_returncode_is_failure(self) -> None:
        with mock.patch.object(updater.subprocess, "run", return_value=self._completed(1, "", "boom")):
            ok, out = updater.run_update("0.21.0", "brew")
        self.assertFalse(ok)
        self.assertIn("boom", out)

    def test_pipx_missing_reports_actionable_reason(self) -> None:
        with mock.patch.object(updater.shutil, "which", return_value=None):
            ok, out = updater.run_update("0.21.0", "pipx")
        self.assertFalse(ok)
        self.assertIn("pipx", out)

    def test_pipx_success_runs_pipx_install_force(self) -> None:
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return self._completed(0, "installed package corral 0.21.0")

        with mock.patch.object(updater.shutil, "which", return_value="/usr/local/bin/pipx"), \
             mock.patch.object(updater, "install_spec", return_value="WHEEL_URL"), \
             mock.patch.object(
                 updater, "_ensure_sesskit_wheel",
                 return_value="/tmp/cached/sesskit-0.2.2-py3-none-any.whl",
             ), \
             mock.patch.object(updater.subprocess, "run", side_effect=fake_run):
            ok, _ = updater.run_update("0.21.0", "pipx")
        self.assertTrue(ok)
        self.assertEqual(captured["cmd"], ["/usr/local/bin/pipx", "install", "--force", "WHEEL_URL"])
        # SessKit 预装走 PIP_FIND_LINKS（不用 --pip-args，见上）
        self.assertEqual(captured["env"].get("PIP_FIND_LINKS"), "/tmp/cached")

    def test_pipx_respects_existing_pip_find_links(self) -> None:
        def fake_run(cmd, **kwargs):
            return self._completed(0, "ok")

        with mock.patch.dict(updater.os.environ, {"PIP_FIND_LINKS": "/user/links"}, clear=False), \
             mock.patch.object(updater.shutil, "which", return_value="/usr/local/bin/pipx"), \
             mock.patch.object(updater, "install_spec", return_value="WHEEL_URL"), \
             mock.patch.object(updater.subprocess, "run", side_effect=fake_run) as spy, \
             mock.patch.object(updater, "_ensure_sesskit_wheel") as ensure:
            ok, _ = updater.run_update("0.21.0", "pipx")
        self.assertTrue(ok)
        ensure.assert_not_called()
        self.assertEqual(spy.call_args.kwargs["env"].get("PIP_FIND_LINKS"), "/user/links")


class FetchLatestTests(unittest.TestCase):
    def test_parses_tag_name_and_strips_v(self) -> None:
        payload = json.dumps({"tag_name": "v0.21.0"}).encode("utf-8")

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return payload

        with mock.patch.object(updater.urllib.request, "urlopen", return_value=_Resp()):
            self.assertEqual(updater.fetch_latest(), "0.21.0")

    def test_returns_none_on_network_error(self) -> None:
        with mock.patch.object(
            updater.urllib.request, "urlopen",
            side_effect=updater.urllib.error.URLError("boom"),
        ):
            self.assertIsNone(updater.fetch_latest())

    def test_returns_none_on_malformed_json(self) -> None:
        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"not json"

        with mock.patch.object(updater.urllib.request, "urlopen", return_value=_Resp()):
            self.assertIsNone(updater.fetch_latest())


class DismissStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.state_file = os.path.join(self._tmpdir.name, "update.json")
        patcher = mock.patch.object(updater, "STATE_FILE", self.state_file)
        patcher.start()
        self.addCleanup(patcher.stop)
        dir_patcher = mock.patch.object(updater, "CACHE_DIR", self._tmpdir.name)
        dir_patcher.start()
        self.addCleanup(dir_patcher.stop)

    def test_should_prompt_false_when_not_newer(self) -> None:
        with mock.patch.object(updater, "current_version", return_value=(0, 20, 0)):
            self.assertFalse(updater.should_prompt("0.20.0"))
            self.assertFalse(updater.should_prompt("0.19.0"))

    def test_should_prompt_true_for_newer_version_first_time(self) -> None:
        with mock.patch.object(updater, "current_version", return_value=(0, 20, 0)):
            self.assertTrue(updater.should_prompt("0.21.0"))

    def test_dismiss_suppresses_same_version_same_day(self) -> None:
        with mock.patch.object(updater, "current_version", return_value=(0, 20, 0)):
            updater.mark_dismissed("0.21.0")
            self.assertFalse(updater.should_prompt("0.21.0"))

    def test_dismiss_does_not_suppress_next_day(self) -> None:
        with mock.patch.object(updater, "current_version", return_value=(0, 20, 0)):
            updater.mark_dismissed("0.21.0")
            with mock.patch.object(updater, "_today", return_value="2099-01-01"):
                self.assertTrue(updater.should_prompt("0.21.0"))

    def test_dismiss_does_not_suppress_a_newer_version(self) -> None:
        with mock.patch.object(updater, "current_version", return_value=(0, 20, 0)):
            updater.mark_dismissed("0.21.0")
            self.assertTrue(updater.should_prompt("0.22.0"))


class CliUpdateTests(unittest.TestCase):
    """`corral update` 终端子命令：三条主路径（dev 无法自动升级 / 已是最新 /
    有新版本并成功升级），全部走 stdout 断言，不发真实网络请求。"""

    def _capture(self, **patches) -> tuple[int, str]:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with mock.patch.multiple(updater, **patches), redirect_stdout(buf):
            code = updater.cli_update()
        return code, buf.getvalue()

    def test_dev_channel_prints_manual_hint_and_exits_nonzero(self) -> None:
        code, out = self._capture(detect_channel=lambda: "dev")
        self.assertEqual(code, 1)
        self.assertIn("corral-dev/corral", out)

    def test_network_failure_prints_check_failed_and_exits_nonzero(self) -> None:
        code, out = self._capture(
            detect_channel=lambda: "pip",
            is_updatable=lambda channel=None: True,
            fetch_latest=lambda timeout=3.0: None,
        )
        self.assertEqual(code, 1)

    def test_already_latest_prints_message_and_exits_zero(self) -> None:
        code, out = self._capture(
            detect_channel=lambda: "pip",
            is_updatable=lambda channel=None: True,
            fetch_latest=lambda timeout=3.0: "0.1.0",
            current_version=lambda: (9, 9, 9),
        )
        self.assertEqual(code, 0)
        self.assertIn("9.9.9", out)

    def test_newer_version_runs_update_and_reports_success(self) -> None:
        code, out = self._capture(
            detect_channel=lambda: "pip",
            is_updatable=lambda channel=None: True,
            fetch_latest=lambda timeout=3.0: "9.9.9",
            current_version=lambda: (0, 1, 0),
            run_update=lambda latest, channel=None: (True, "install ok"),
        )
        self.assertEqual(code, 0)
        self.assertIn("9.9.9", out)
        self.assertIn("install ok", out)

    def test_newer_version_update_failure_exits_nonzero(self) -> None:
        code, out = self._capture(
            detect_channel=lambda: "pip",
            is_updatable=lambda channel=None: True,
            fetch_latest=lambda timeout=3.0: "9.9.9",
            current_version=lambda: (0, 1, 0),
            run_update=lambda latest, channel=None: (False, "boom"),
        )
        self.assertEqual(code, 1)
        self.assertIn("boom", out)


if __name__ == "__main__":
    unittest.main()
