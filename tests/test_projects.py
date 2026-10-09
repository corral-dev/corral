"""项目发现与快捷启动 `corral <runtime> <project>` 分流。"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

import corral
from corral import projects
from corral.i18n import t
from corral.models import LaunchPlan


def _temp_root(td: str) -> Path:
    """把临时目录解析成真实路径再用作断言基准。

    **macOS 上不解析会全线假失败**：那里 `/var` 是指向 `/private/var` 的软链，
    `tempfile` 交回来的是 `/var/folders/...`，而 `projects.scan_git_roots` 会
    如实解析成 `/private/var/folders/...`（解析软链是它的既定行为，另有
    `test_scan_resolves_symlink_root` 专门守着），两边字符串对不上。Linux 上
    `/tmp` 不是软链，所以本机怎么跑都不复现。2026-07-31 macOS runner 上一次暴露
    7 个用例，此前一直被 macOS 作业挂死掩盖着没人看见。
    """
    return Path(td).resolve()


def _touch_git(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / ".git").mkdir(exist_ok=True)


class GitScanTests(unittest.TestCase):
    def setUp(self) -> None:
        projects.clear_filesystem_cache()

    def tearDown(self) -> None:
        projects.clear_filesystem_cache()

    @contextmanager
    def _forbid_scan_access(self, paths: list[Path]):
        """Reject protected metadata, marker and enumeration access, not just results."""
        forbidden = [str(path) for path in paths]
        scandir = os.scandir
        isdir = os.path.isdir
        readlink = os.readlink
        lstat = os.lstat
        marker = projects._has_git_marker

        def check(path):
            text = os.fspath(path)
            for parent in forbidden:
                self.assertFalse(text == parent or text.startswith(parent + os.sep), text)

        def checked_isdir(path):
            check(path)
            return isdir(path)

        def checked_marker(path):
            check(path)
            return marker(path)

        def checked_readlink(path, **kwargs):
            check(path)
            return readlink(path, **kwargs)

        def checked_lstat(path, **kwargs):
            check(path)
            return lstat(path, **kwargs)

        def checked_scandir(path):
            check(path)
            with scandir(path) as entries:
                for entry in entries:
                    wrapped = mock.Mock(wraps=entry)
                    wrapped.name = entry.name
                    wrapped.path = entry.path

                    def checked_entry_isdir(*, follow_symlinks=True, item=entry):
                        check(item.path)
                        if follow_symlinks:
                            check(os.path.realpath(item.path))
                        return item.is_dir(follow_symlinks=follow_symlinks)

                    wrapped.is_dir.side_effect = checked_entry_isdir
                    yield wrapped

        with (
            mock.patch.object(projects.os, "scandir", side_effect=checked_scandir),
            mock.patch.object(projects.os.path, "isdir", side_effect=checked_isdir),
            mock.patch.object(projects.os, "readlink", side_effect=checked_readlink),
            mock.patch.object(projects.os, "lstat", side_effect=checked_lstat),
            mock.patch.object(projects, "_has_git_marker", side_effect=checked_marker),
        ):
            yield

    def test_home_discovery_never_probes_personal_folders_or_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = _temp_root(td)
            protected = [home / name for name in (
                "Desktop", "Documents", "Downloads", "Pictures", "Movies", "Music", "Library",
            )]
            for index, path in enumerate(protected):
                _touch_git(path / "PrivateRepo")
                (home / f"alias{index}").symlink_to(path)
            (home / "deep_alias").symlink_to(home / "Documents" / "PrivateRepo")
            (home / "chain_alias").symlink_to("deep_alias")
            workspace = home / "Codes" / "VisibleRepo"
            _touch_git(workspace)
            with mock.patch.dict(os.environ, {"HOME": str(home)}), self._forbid_scan_access(protected):
                found = projects.scan_git_roots([str(home)], depth=4, use_cache=False)
            self.assertEqual(found, [str(workspace)])

    def test_safe_symlink_resolution_preserves_relative_parent_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            _touch_git(root / "real" / "VisibleRepo")
            (root / "links").mkdir()
            (root / "links" / "relative").symlink_to("../real")
            (root / "links" / "chain").symlink_to("relative/VisibleRepo/..")
            found = projects.scan_git_roots([str(root / "links")], depth=4, use_cache=False)
            self.assertEqual(found, [str(root / "real" / "VisibleRepo")])

    def test_discovery_never_probes_media_packages_or_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            packages = [root / name for name in (
                "Family.photoslibrary", "Archive.PHOTOLIBRARY", "Collection.musiclibrary",
            )]
            for index, package in enumerate(packages):
                _touch_git(package / "Internals")
                (root / f"alias{index}").symlink_to(package)
            _touch_git(root / "VisibleRepo")
            with self._forbid_scan_access(packages):
                found = projects.scan_git_roots([str(root)], depth=4, use_cache=False)
                for package in packages:
                    self.assertEqual(projects.scan_git_roots([str(package)], use_cache=False), [])
                    self.assertEqual(
                        projects.scan_git_roots([str(package / "Internals")], use_cache=False), [],
                    )
                self.assertEqual(projects.scan_git_roots([str(root / "alias0")], use_cache=False), [])
            self.assertEqual(found, [str(root / "VisibleRepo")])

    def test_configured_personal_project_root_is_explicit_scope(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = _temp_root(td)
            selected = home / "Documents" / "Projects"
            _touch_git(selected / "VisibleRepo")
            package = selected / "Family.photoslibrary"
            _touch_git(package / "Internals")
            other = home / "Downloads"
            _touch_git(other / "PrivateRepo")
            with (
                mock.patch.dict(os.environ, {"HOME": str(home), "CORRAL_PROJECT_ROOTS": str(selected)}),
                self._forbid_scan_access([package, other]),
            ):
                found = projects.scan_git_roots(use_cache=False)
            self.assertEqual(found, [str(selected / "VisibleRepo")])

    def test_personal_session_cwd_survives_without_filesystem_probes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = _temp_root(td)
            personal = home / "Documents"
            known = personal / "KnownRepo"
            _touch_git(known)
            with mock.patch.dict(os.environ, {"HOME": str(home)}), self._forbid_scan_access([personal]):
                found = projects.discover([str(known)], roots=[str(home)], use_cache=False)
            self.assertEqual([project.path for project in found], [str(known)])
            self.assertEqual(found[0].sources, frozenset({"session"}))

    def test_implicit_scan_prunes_macos_volumes_before_metadata(self) -> None:
        entry = mock.Mock(name="volume")
        entry.name = "Volumes"
        entry.path = "/Volumes"
        with (
            mock.patch.object(projects.sys, "platform", "darwin"),
            mock.patch.object(projects.os, "scandir", return_value=[entry]),
            mock.patch.object(projects, "_has_git_marker", return_value=False) as marker,
        ):
            projects._scan_one_root("/", 1, [], False, {})
        entry.is_dir.assert_not_called()
        marker.assert_called_once_with("/")

    def test_scan_finds_git_roots_and_skips_nested(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            _touch_git(root / "subswap")
            _touch_git(root / "a" / "b" / "LingoWeave")
            _touch_git(root / "subswap" / "vendor" / "nested")  # 应被剪枝

            found = projects.scan_git_roots([str(root)], depth=4, use_cache=False)
            self.assertEqual(
                set(found),
                {str(root / "subswap"), str(root / "a" / "b" / "LingoWeave")},
            )

    def test_scan_skips_stversions_syncthing_snapshots(self) -> None:
        """回归：Syncthing `.stversions` 里的 `.git` 不得当成项目。"""
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            real = root / "Codes" / "subswap"
            snap = root / "Codes" / ".stversions" / "subswap"
            _touch_git(real)
            _touch_git(snap)

            found = projects.scan_git_roots([str(root)], depth=4, use_cache=False)
            self.assertEqual(found, [str(real)])

    def test_scan_skips_dot_dirs_and_node_modules(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            _touch_git(root / "ok")
            _touch_git(root / "node_modules" / "pkg")
            _touch_git(root / ".cache" / "hidden")

            found = projects.scan_git_roots([str(root)], depth=4, use_cache=False)
            self.assertEqual(found, [str(root / "ok")])

    def test_scan_resolves_symlink_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            real = root / "real"
            link = root / "link"
            _touch_git(real / "corral")
            link.symlink_to(real)

            found = projects.scan_git_roots([str(link)], depth=4, use_cache=False)
            self.assertEqual(found, [str((real / "corral").resolve())])

    def test_scan_follows_symlinked_subdir(self) -> None:
        """回归：`~/Codes -> /elsewhere/Codes` 这类软链接子目录必须照样扫到项目。"""
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            home = root / "home"
            home.mkdir()
            elsewhere = root / "elsewhere" / "Codes"
            _touch_git(elsewhere / "AlphaForge" / "backend")
            (home / "Codes").symlink_to(elsewhere)

            found = projects.scan_git_roots([str(home)], depth=4, use_cache=False)
            # 收录的是真实路径，便于与会话 cwd 去重。
            self.assertEqual(found, [str((elsewhere / "AlphaForge" / "backend").resolve())])

    def test_scan_symlink_cycle_terminates(self) -> None:
        """软链接成环不得死循环。"""
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            _touch_git(root / "repo")
            (root / "loop").symlink_to(root)

            found = projects.scan_git_roots([str(root)], depth=4, use_cache=False)
            self.assertEqual(found, [str((root / "repo").resolve())])

    def test_corral_project_roots_env(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            _touch_git(root / "only")
            with mock.patch.dict(os.environ, {"CORRAL_PROJECT_ROOTS": str(root)}):
                projects.clear_filesystem_cache()
                found = projects.scan_git_roots(depth=2, use_cache=False)
            self.assertEqual(found, [str(root / "only")])

    def test_empty_corral_project_roots_skips_filesystem(self) -> None:
        with mock.patch.dict(os.environ, {"CORRAL_PROJECT_ROOTS": ""}):
            self.assertEqual(projects.configured_roots(), [])
            self.assertEqual(projects.scan_git_roots(use_cache=False), [])


class MatchResolveTests(unittest.TestCase):
    def _projects(self, *paths: str) -> list[projects.Project]:
        return projects.discover(paths, scan_filesystem=False)

    def test_fuzzy_case_insensitive_unique(self) -> None:
        items = self._projects("/Codes/SubSwap", "/Codes/corral")
        matched = projects.match_projects("subswap", items)
        self.assertEqual([p.path for p in matched], ["/Codes/SubSwap"])
        self.assertEqual(projects.resolve_query("SUB", items), "/Codes/SubSwap")

    def test_substring_hits_suppress_subsequence_noise(self) -> None:
        """回归：有子串命中时，不能再把 `java-platform` 这类子序列命中掺进候选。"""
        items = self._projects(
            "/Codes/AlphaForge/backend",
            "/Codes/AlphaForge/client-web",
            "/Codes/LLMPlatform/archive/java-platform",
        )
        matched = projects.match_projects("alpha", items)
        self.assertEqual(
            [p.path for p in matched],
            ["/Codes/AlphaForge/backend", "/Codes/AlphaForge/client-web"],
        )

    def test_subsequence_still_works_without_substring_hit(self) -> None:
        items = self._projects("/Codes/SubSwap")
        self.assertEqual(
            [p.path for p in projects.match_projects("sbswp", items)],
            ["/Codes/SubSwap"],
        )

    def test_zero_matches_raises(self) -> None:
        items = self._projects("/Codes/corral")
        with self.assertRaises(projects.ProjectResolveError) as ctx:
            projects.resolve_query("nope", items)
        self.assertEqual(str(ctx.exception), t("project.not_found", query="nope"))

    def test_multiple_matches_interactive_pick(self) -> None:
        items = self._projects("/a/cli", "/b/cli")
        stdin = io.StringIO("2\n")
        stdout = io.StringIO()
        cwd = projects.resolve_query(
            "cli", items, stdin=stdin, stdout=stdout, interactive=True,
        )
        self.assertEqual(cwd, "/b/cli")
        listing = "\n".join(
            f"  {index}. {project.label}  ({project.path})"
            for index, project in enumerate(projects.match_projects("cli", items), start=1)
        )
        self.assertIn(
            t("project.ambiguous_prompt", query="cli", listing=listing),
            stdout.getvalue(),
        )

    def test_multiple_matches_noninteractive_raises(self) -> None:
        items = self._projects("/a/cli", "/b/cli")
        listing = "\n".join(
            f"  {index}. {project.label}  ({project.path})"
            for index, project in enumerate(projects.match_projects("cli", items), start=1)
        )
        with self.assertRaises(projects.ProjectResolveError) as ctx:
            projects.resolve_query("cli", items, interactive=False)
        self.assertEqual(
            str(ctx.exception),
            t("project.ambiguous", query="cli", count=2, listing=listing),
        )


class ProjectEntriesTests(unittest.TestCase):
    def test_merges_git_and_session_stats(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            _touch_git(root / "diskonly")
            sessions = {
                "claude": [
                    {"cwd": str(root / "diskonly"), "mtime": 9},
                    {"cwd": str(root / "sessiononly"), "mtime": 5},
                ],
            }
            entries = projects.project_entries(
                sessions, roots=[str(root)], depth=3, use_cache=False,
            )
            by_key = {e["cwd_key"]: e for e in entries}
            self.assertEqual(by_key[str(root / "diskonly")]["count"], 1)
            self.assertEqual(by_key[str(root / "sessiononly")]["count"], 1)
            # 纯 git、无会话的项目 count=0（本例 diskonly 有会话；再造一个）
            _touch_git(root / "puregit")
            entries2 = projects.project_entries(
                sessions, roots=[str(root)], depth=3, use_cache=False,
            )
            pure = next(e for e in entries2 if e["cwd_key"] == str(root / "puregit"))
            self.assertEqual(pure["count"], 0)


class DirectLaunchProjectTests(unittest.TestCase):
    """`corral claude <project>` 与透传分流。"""

    def setUp(self) -> None:
        projects.clear_filesystem_cache()

    def tearDown(self) -> None:
        projects.clear_filesystem_cache()

    def test_passthrough_when_first_arg_is_flag(self) -> None:
        plan = LaunchPlan(("claude", "--dangerously-skip-permissions", "--print", "hi"), None)
        registry = mock.Mock()
        # 直启入口会先把第一个词按别名解析成运行时 id；假 registry 原样返回即可
        registry.resolve_id.side_effect = lambda token: token
        registry.build_passthrough_plan.return_value = plan
        registry.ids = ("claude",)

        with (
            mock.patch.object(corral, "keepalive") as keepalive_mock,
            mock.patch.object(corral, "execute_launch") as execute_launch,
            mock.patch.object(corral, "_require_tmux"),
            mock.patch.object(corral.sys.stdin, "isatty", return_value=False),
            mock.patch.object(corral.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = False
            keepalive_mock.new_session_ident.return_value = "xxxx"
            corral._dispatch_direct_launch(["claude", "--print", "hi"], registry)

        registry.build_passthrough_plan.assert_called_once_with("claude", ["--print", "hi"])
        execute_launch.assert_called_once_with(plan)

    def test_project_mode_builds_new_session_plan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = _temp_root(td)
            proj = root / "subswap"
            _touch_git(proj)
            new_plan = LaunchPlan(("claude", "--dangerously-skip-permissions"), str(proj))

            runtime = mock.Mock()
            runtime.build_new_session_plan.return_value = new_plan
            registry = mock.Mock()
            # 直启入口会先把第一个词按别名解析成运行时 id；假 registry 原样返回即可
            registry.resolve_id.side_effect = lambda token: token
            registry.get.return_value = runtime
            registry.scan_all.return_value = {"claude": []}
            registry.ids = ("claude",)

            with (
                mock.patch.dict(os.environ, {"CORRAL_PROJECT_ROOTS": str(root)}),
                mock.patch.object(corral, "keepalive") as keepalive_mock,
                mock.patch.object(corral, "execute_launch") as execute_launch,
                mock.patch.object(corral, "_require_tmux"),
                mock.patch.object(corral.sys.stdin, "isatty", return_value=False),
                mock.patch.object(corral.sys.stdout, "isatty", return_value=False),
            ):
                projects.clear_filesystem_cache()
                keepalive_mock.enabled.return_value = False
                keepalive_mock.new_session_ident.return_value = "xxxx"
                corral._dispatch_direct_launch(["claude", "subswap"], registry)

            registry.get.assert_called_with("claude")
            runtime.build_new_session_plan.assert_called_once_with(str(proj))
            registry.build_passthrough_plan.assert_not_called()
            execute_launch.assert_called_once_with(new_plan)

    def test_opencode_subcommand_is_passthrough_not_project_mode(self) -> None:
        """`corral opencode run …` 的首词是子命令，必须走透传而不是项目名匹配。

        回归：子命令曾把 run/stats 当成项目名去模糊匹配（实测会匹配出十几个项目、
        要求交互选择），导致 `corral opencode run 提示` 这种日常用法被拦死。
        """
        plan = LaunchPlan(("opencode", "run", "--auto", "提示"), None)
        registry = mock.Mock()
        registry.resolve_id.side_effect = lambda token: token
        registry.build_passthrough_plan.return_value = plan

        with (
            mock.patch.object(corral, "keepalive") as keepalive_mock,
            mock.patch.object(corral, "execute_launch") as execute_launch,
            mock.patch.object(corral, "_require_tmux"),
            mock.patch.object(corral.sys.stdin, "isatty", return_value=False),
            mock.patch.object(corral.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = False
            keepalive_mock.new_session_ident.return_value = "xxxx"
            corral._dispatch_direct_launch(
                ["opencode", "run", "提示"], registry,
            )

        registry.build_passthrough_plan.assert_called_once_with("opencode", ["run", "提示"])
        execute_launch.assert_called_once_with(plan)

    def test_opencode_other_subcommand_is_passthrough_too(self) -> None:
        """`corral opencode stats` 同样是透传（不带 --auto），不能被当成项目名。"""
        plan = LaunchPlan(("opencode", "stats"), None)
        registry = mock.Mock()
        registry.resolve_id.side_effect = lambda token: token
        registry.build_passthrough_plan.return_value = plan

        with (
            mock.patch.object(corral, "keepalive") as keepalive_mock,
            mock.patch.object(corral, "execute_launch") as execute_launch,
            mock.patch.object(corral, "_require_tmux"),
            mock.patch.object(corral.sys.stdin, "isatty", return_value=False),
            mock.patch.object(corral.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = False
            keepalive_mock.new_session_ident.return_value = "xxxx"
            corral._dispatch_direct_launch(["opencode", "stats"], registry)

        registry.build_passthrough_plan.assert_called_once_with("opencode", ["stats"])
        execute_launch.assert_called_once_with(plan)

    def test_project_mode_rejects_extra_args(self) -> None:
        registry = mock.Mock()
        # 直启入口会先把第一个词按别名解析成运行时 id；假 registry 原样返回即可
        registry.resolve_id.side_effect = lambda token: token
        with (
            mock.patch.object(corral, "_require_tmux"),
            mock.patch.object(corral.sys, "stderr", new_callable=io.StringIO) as err,
        ):
            with self.assertRaises(SystemExit) as ctx:
                corral._dispatch_direct_launch(["claude", "subswap", "extra"], registry)
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("不接受额外参数", err.getvalue())
        registry.build_passthrough_plan.assert_not_called()

    def test_no_keepalive_passthrough_with_flag_args(self) -> None:
        plan = LaunchPlan(("codex", "--dangerously-bypass-approvals-and-sandbox", "--resume", "x"), None)
        registry = mock.Mock()
        # 直启入口会先把第一个词按别名解析成运行时 id；假 registry 原样返回即可
        registry.resolve_id.side_effect = lambda token: token
        registry.build_passthrough_plan.return_value = plan

        with (
            mock.patch.object(corral, "keepalive") as keepalive_mock,
            mock.patch.object(corral, "execute_launch") as execute_launch,
            mock.patch.object(corral, "_require_tmux"),
            mock.patch.object(corral.sys.stdin, "isatty", return_value=False),
            mock.patch.object(corral.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = False
            corral._dispatch_direct_launch(
                ["--no-keepalive", "codex", "--resume", "x"], registry,
            )

        registry.build_passthrough_plan.assert_called_once_with("codex", ["--resume", "x"])
        execute_launch.assert_called_once_with(plan)


class LegacyDirectLaunchTestsUpdate(unittest.TestCase):
    """原 DirectLaunchTests 中依赖「裸位置参数透传」的用例改为 flag 透传。"""

    def test_passes_through_flag_args_and_wraps_with_keepalive(self) -> None:
        plan = LaunchPlan(("claude", "--dangerously-skip-permissions", "--print", "hi"), None)
        wrapped = LaunchPlan(("tmux", "-L", "corral-keepalive", "new-session"), None)
        registry = mock.Mock()
        # 直启入口会先把第一个词按别名解析成运行时 id；假 registry 原样返回即可
        registry.resolve_id.side_effect = lambda token: token
        registry.build_passthrough_plan.return_value = plan

        with (
            mock.patch.object(corral, "keepalive") as keepalive_mock,
            mock.patch.object(corral, "execute_launch") as execute_launch,
            mock.patch.object(corral, "_require_tmux"),
            mock.patch.object(corral.sys.stdin, "isatty", return_value=False),
            mock.patch.object(corral.sys.stdout, "isatty", return_value=False),
        ):
            keepalive_mock.enabled.return_value = True
            keepalive_mock.new_session_ident.return_value = "xxxx"
            keepalive_mock.wrap_plan.return_value = wrapped
            corral._dispatch_direct_launch(["claude", "--print", "hi"], registry)

        registry.build_passthrough_plan.assert_called_once_with("claude", ["--print", "hi"])
        keepalive_mock.wrap_plan.assert_called_once_with(plan, "claude", "xxxx")
        execute_launch.assert_called_once_with(wrapped)


if __name__ == "__main__":
    unittest.main()
