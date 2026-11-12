"""OpenCode 预钉身份：launch 时生成完整 ses_ id，精确绑定不再靠时间猜。"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from corral import keepalive, liveness, opencode_identity
from corral.models import LaunchPlan
from corral.runtime import host_extension
from corral.store import SessionStore


def _blank_plan(cwd: str = "/tmp") -> LaunchPlan:
    return LaunchPlan(("opencode", "--auto"), cwd)


def _handoff_plan(cwd: str = "/tmp") -> LaunchPlan:
    return LaunchPlan(("opencode", "--auto", "--prompt", "do work"), cwd)


class NewSessionIdTests(unittest.TestCase):
    def test_format_and_short_prefix_traceability(self) -> None:
        full = opencode_identity.new_session_id("486da5c2")
        self.assertTrue(opencode_identity.is_full_session_id(full))
        self.assertTrue(full.startswith("ses_486da5c2"))
        self.assertEqual(len(full), len("ses_") + 26)

    def test_ids_are_unique(self) -> None:
        self.assertNotEqual(
            opencode_identity.new_session_id("486da5c2"),
            opencode_identity.new_session_id("486da5c2"),
        )

    def test_short_ident_is_rejected_as_full(self) -> None:
        self.assertFalse(opencode_identity.is_full_session_id("486da5c2"))
        self.assertFalse(opencode_identity.is_full_session_id(""))
        # 比 SessKit 更严：含连字符的 ses_ 串在这里判 miss（只用于自生成 id
        # 与 claim 校验，不影响 SessKit 侧解析）。
        self.assertFalse(opencode_identity.is_full_session_id("ses_abc-def"))


class ShouldBindPlanTests(unittest.TestCase):
    def test_blank_new_binds(self) -> None:
        self.assertTrue(opencode_identity.should_bind_plan(("opencode", "--auto")))

    def test_handoff_new_binds(self) -> None:
        self.assertTrue(opencode_identity.should_bind_plan(
            ("opencode", "--auto", "--prompt", "do work")))

    def test_handoff_prompt_with_embedded_session_text_still_binds(self) -> None:
        # 接力 --prompt 文本是单个 argv 元素：内部的 "-s ses_…" 字样不得按
        # flag 处理（token 精确比对），否则接力新建永远钉不上。
        self.assertTrue(opencode_identity.should_bind_plan(
            ("opencode", "--auto", "--prompt",
             "continue -s ses_oldhistory00000000000000 here")))

    def test_resume_with_s_already_present_does_not_bind(self) -> None:
        self.assertFalse(opencode_identity.should_bind_plan(
            ("opencode", "--auto", "-s", "ses_abc123")))
        self.assertFalse(opencode_identity.should_bind_plan(
            ("opencode", "--auto", "--session=ses_abc123")))
        self.assertFalse(opencode_identity.should_bind_plan(
            ("opencode", "--auto", "--session", "ses_abc123")))

    def test_post_prompt_session_flag_means_no_bind(self) -> None:
        # prompt 后的独立 -s token 是真正的重复 flag：注出双 -s 在不同
        # opencode 版本行为不一（取后者 / 用法错误），一律不钉。
        self.assertFalse(opencode_identity.should_bind_plan(
            ("opencode", "--auto", "--prompt", "hi", "-s", "ses_x")))

    def test_bare_trailing_s_means_no_bind(self) -> None:
        # 裸尾 -s 视为已带 flag：补第二个只是双 flag，不如交 opencode 报错。
        self.assertFalse(opencode_identity.should_bind_plan(
            ("opencode", "--auto", "-s")))

    def test_full_path_executable_binds(self) -> None:
        self.assertTrue(opencode_identity.should_bind_plan(
            ("/usr/local/bin/opencode", "--auto")))

    def test_flag_value_named_like_subcommand_still_binds(self) -> None:
        # --model 的值 run 不是子命令（与 SessKit _VALUE_FLAGS 跳值规则一致）。
        self.assertTrue(opencode_identity.should_bind_plan(
            ("opencode", "--auto", "--model", "run")))

    def test_sesskit_only_subcommand_does_not_bind(self) -> None:
        # attach 只在 SessKit 非 TUI 表里：任一表认为是子命令就不钉（保守并集）。
        self.assertFalse(opencode_identity.should_bind_plan(
            ("opencode", "attach")))

    def test_subcommands_never_bind(self) -> None:
        for argv in (
            ("opencode", "run", "--auto", "-s", "ses_x", "hi"),
            ("opencode", "run", "--auto", "hi"),
            ("opencode", "session", "export", "ses_x"),
            ("opencode", "serve", "--service"),
            ("opencode", "--auto", "run", "hi"),
        ):
            with self.subTest(argv=argv):
                self.assertFalse(opencode_identity.should_bind_plan(argv))

    def test_other_executable_never_binds(self) -> None:
        self.assertFalse(opencode_identity.should_bind_plan(("claude", "--resume")))


class BindHostedIdentTests(unittest.TestCase):
    def test_blank_plan_gets_s_before_end_and_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                plan, env = opencode_identity.bind_hosted_ident(
                    _blank_plan(), "486da5c2")
            self.assertIn("-s", plan.argv)
            at = plan.argv.index("-s")
            full = plan.argv[at + 1]
            self.assertTrue(opencode_identity.is_full_session_id(full))
            self.assertTrue(full.startswith("ses_486da5c2"))
            self.assertEqual(
                env, ["-e", f"{opencode_identity.SESSION_ID_ENV}={full}"])
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                self.assertEqual(
                    opencode_identity.ses_id_for_ident("486da5c2"), full)

    def test_same_ident_relaunch_reuses_fresh_claim(self) -> None:
        # 同 ident 短时间内重复 launch 复用已写 claim，避免同 pane 双 id。
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                plan1, _ = opencode_identity.bind_hosted_ident(
                    _blank_plan(), "aabbccdd")
                plan2, _ = opencode_identity.bind_hosted_ident(
                    _blank_plan(), "aabbccdd")
            full1 = plan1.argv[plan1.argv.index("-s") + 1]
            full2 = plan2.argv[plan2.argv.index("-s") + 1]
            self.assertEqual(full1, full2)
            self.assertEqual(list(claims.glob("*.json")), [claims / "aabbccdd.json"])

    def test_handoff_plan_inserts_s_before_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                plan, _env = opencode_identity.bind_hosted_ident(
                    _handoff_plan(), "486da5c2")
            prompt_at = plan.argv.index("--prompt")
            s_at = plan.argv.index("-s")
            # SessKit 只看 --prompt 之前的旗标：-s 必须落在 prompt 前。
            self.assertLess(s_at, prompt_at)
            self.assertTrue(
                opencode_identity.is_full_session_id(plan.argv[s_at + 1]))

    def test_handoff_prompt_equals_form_inserts_s_before_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            original = LaunchPlan(("opencode", "--auto", "--prompt=do work"), "/tmp")
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                plan, _env = opencode_identity.bind_hosted_ident(
                    original, "486da5c2")
            prompt_at = next(
                i for i, t in enumerate(plan.argv) if t.startswith("--prompt"))
            s_at = plan.argv.index("-s")
            self.assertLess(s_at, prompt_at)

    def test_resume_plan_untouched_and_no_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            original = LaunchPlan(("opencode", "--auto", "-s", "ses_keepme"), "/tmp")
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                plan, env = opencode_identity.bind_hosted_ident(original, "ses_keep")
            self.assertEqual(plan.argv, original.argv)
            self.assertEqual(env, [])
            self.assertFalse(claims.exists(), "resume 路径不得写 claim")


class ClaimFileTests(unittest.TestCase):
    def test_corrupt_claim_is_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            claims.mkdir()
            (claims / "deadbeef.json").write_text("{not json", encoding="utf-8")
            (claims / "badver.json").write_text(
                json.dumps({"v": 999, "ses_id": "ses_" + "a" * 26}),
                encoding="utf-8")
            (claims / "badid.json").write_text(
                json.dumps({"v": 1, "ses_id": "not-a-session"}),
                encoding="utf-8")
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                self.assertIsNone(opencode_identity.ses_id_for_ident("deadbeef"))
                self.assertIsNone(opencode_identity.ses_id_for_ident("badver"))
                self.assertIsNone(opencode_identity.ses_id_for_ident("badid"))
                self.assertIsNone(opencode_identity.ses_id_for_ident("missing"))

    def test_minimal_claim_without_optional_keys_still_reads(self) -> None:
        # 前向兼容：只有 v + ses_id 的最小 claim 可读（runtime 缺省 opencode）。
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            claims.mkdir()
            full = "ses_" + "a" * 26
            (claims / "mini.json").write_text(
                json.dumps({"v": 1, "ses_id": full}), encoding="utf-8")
            (claims / "wrongrt.json").write_text(
                json.dumps({"v": 1, "ses_id": full, "runtime": "claude"}),
                encoding="utf-8")
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                self.assertEqual(opencode_identity.ses_id_for_ident("mini"), full)
                self.assertIsNone(opencode_identity.ses_id_for_ident("wrongrt"))

    def test_oversize_claim_is_a_miss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            claims.mkdir()
            (claims / "huge.json").write_text("x" * 8192, encoding="utf-8")
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                self.assertIsNone(opencode_identity.ses_id_for_ident("huge"))

    def test_illegal_ident_never_touches_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                for bad in ("", "/", "..", "a/b"):
                    self.assertFalse(opencode_identity.write_intent(
                        bad, "ses_" + "b" * 26, "/tmp"))
                    self.assertIsNone(opencode_identity.ses_id_for_ident(bad))
                    opencode_identity.discard(bad)  # 不抛
            self.assertFalse(claims.exists())

    def test_cache_disabled_skips_claim_io(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            with (
                mock.patch.object(opencode_identity, "CLAIM_DIR", claims),
                mock.patch.dict("os.environ", {"CORRAL_CACHE": "0"}),
            ):
                self.assertFalse(opencode_identity.write_intent(
                    "aabbccdd", "ses_" + "b" * 26, "/tmp"))
                self.assertIsNone(opencode_identity.ses_id_for_ident("aabbccdd"))
                self.assertEqual(opencode_identity.prune_stale(), 0)


class PruneStaleTests(unittest.TestCase):
    def _write(self, claims: Path, name: str, age_seconds: float) -> Path:
        claims.mkdir(parents=True, exist_ok=True)
        full = "ses_" + "c" * 26
        path = claims / f"{name}.json"
        path.write_text(
            json.dumps({"v": 1, "ses_id": full, "runtime": "opencode"}),
            encoding="utf-8")
        old = time.time() - age_seconds
        os.utime(path, (old, old))
        return path

    def test_prune_removes_only_expired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                fresh = self._write(claims, "fresh", age_seconds=10)
                stale = self._write(claims, "stale", age_seconds=3600)
                pruned = opencode_identity.prune_stale(max_age_seconds=60)
            self.assertEqual(pruned, 1)
            self.assertTrue(fresh.exists())
            self.assertFalse(stale.exists())

    def test_prune_clears_crash_tmp_dotfiles_and_keeps_notes(self) -> None:
        # write_intent 在 replace 前崩溃会留下无 .json 后缀的 dotfile。
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            claims.mkdir()
            dot = claims / ".stale-ab12.tmp"
            dot.write_text("{}", encoding="utf-8")
            notes = claims / "notes.txt"
            notes.write_text("do not touch", encoding="utf-8")
            old = time.time() - 3600
            os.utime(dot, (old, old))
            os.utime(notes, (old, old))
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                pruned = opencode_identity.prune_stale(max_age_seconds=60)
            self.assertEqual(pruned, 1)
            self.assertFalse(dot.exists())
            self.assertTrue(notes.exists())

    def test_prune_missing_dir_is_zero(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "nope"
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                self.assertEqual(opencode_identity.prune_stale(), 0)


class HostExtensionEnvTests(unittest.TestCase):
    def test_full_session_id_takes_priority_over_short_ident(self) -> None:
        env = {
            "CORRAL_OPENCODE_SESSION_ID": "ses_" + "d" * 26,
            "CORRAL_SESSION_ID": "486da5c2",
        }
        self.assertEqual(
            host_extension.session_id_from_env(env), "ses_" + "d" * 26)

    def test_short_ident_still_works_without_pinned_id(self) -> None:
        self.assertEqual(
            host_extension.session_id_from_env({"CORRAL_SESSION_ID": "abcd1234"}),
            "abcd1234")
        self.assertEqual(host_extension.session_id_from_env({}), "")
        self.assertEqual(host_extension.session_id_from_env(None), "")

    def test_new_env_key_is_whitelisted(self) -> None:
        self.assertIn(
            host_extension.SESSION_ID_ENV
            if hasattr(host_extension, "SESSION_ID_ENV")
            else "CORRAL_OPENCODE_SESSION_ID",
            host_extension.HOST_ENV_KEYS)
        from corral import legacy_names

        self.assertIn(
            "CORRAL_OPENCODE_SESSION_ID", legacy_names.PROCESS_ENV_KEYS)


class KeepaliveIntegrationTests(unittest.TestCase):
    def test_hosted_identity_pins_opencode_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                plan, env = keepalive.hosted_identity(
                    _blank_plan(), "opencode", "486da5c2")
            self.assertIn("-s", plan.argv)
            full = plan.argv[plan.argv.index("-s") + 1]
            self.assertTrue(opencode_identity.is_full_session_id(full))
            self.assertTrue(full.startswith("ses_486da5c2"))
            self.assertEqual(
                env, ["-e", f"{opencode_identity.SESSION_ID_ENV}={full}"])
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                self.assertEqual(
                    opencode_identity.ses_id_for_ident("486da5c2"), full)

    def test_hosted_identity_leaves_resume_and_run_alone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                resume = LaunchPlan(("opencode", "--auto", "-s", "ses_keep"), "/tmp")
                plan, env = keepalive.hosted_identity(resume, "opencode", "ses_keep")
                self.assertEqual(plan.argv, resume.argv)
                self.assertEqual(env, [])
                run = LaunchPlan(("opencode", "run", "--auto", "hi"), "/tmp")
                plan, env = keepalive.hosted_identity(run, "opencode", "aabbccdd")
                self.assertEqual(plan.argv, run.argv)
                self.assertEqual(env, [])
            # 非绑定路径不得写 claim（隔离真 ~/.cache，回归污染即失败）。
            self.assertFalse(claims.exists())

    def test_wrap_plan_embeds_pinned_id_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                wrapped = keepalive.wrap_plan(_blank_plan(), "opencode", "486da5c2")
            tail = wrapped.argv[wrapped.argv.index("--") + 1:]
            self.assertIn("-s", tail)
            full = tail[tail.index("-s") + 1]
            self.assertTrue(opencode_identity.is_full_session_id(full))
            env_pairs = [
                wrapped.argv[i + 1]
                for i, arg in enumerate(wrapped.argv) if arg == "-e"]
            self.assertIn(f"{opencode_identity.SESSION_ID_ENV}={full}", env_pairs)
            self.assertIn("CORRAL_SESSION_ID=486da5c2", env_pairs)
            self.assertIn("CORRAL_RUNTIME=opencode", env_pairs)


class LivenessOpencodeTests(unittest.TestCase):
    def test_short_pane_name_never_name_matches_opencode_history(self) -> None:
        session = {"source": "opencode", "id": "ses_" + "c" * 26}
        with (
            mock.patch.object(
                liveness, "_list_tmux_sessions",
                return_value=[["corral-opencode-486da5c2", "35704", "opencode"]],
            ),
            mock.patch.object(opencode_identity, "ses_id_for_ident", return_value=None),
        ):
            liveness.annotate([session])
        self.assertNotIn("keepalive_name", session)

    def test_claim_exact_attaches_without_pid(self) -> None:
        full = "ses_" + "d" * 26
        session = {"source": "opencode", "id": full}
        with (
            mock.patch.object(
                liveness, "_list_tmux_sessions",
                return_value=[["corral-opencode-486da5c2", "35704", "opencode"]],
            ),
            mock.patch.object(
                opencode_identity, "ses_id_for_ident", return_value=full),
        ):
            liveness.annotate([session])
        self.assertEqual(session.get("keepalive_name"), "corral-opencode-486da5c2")

    def test_claim_mismatch_attaches_nothing(self) -> None:
        session = {"source": "opencode", "id": "ses_" + "e" * 26}
        with (
            mock.patch.object(
                liveness, "_list_tmux_sessions",
                return_value=[["corral-opencode-486da5c2", "35704", "opencode"]],
            ),
            mock.patch.object(
                opencode_identity, "ses_id_for_ident",
                return_value="ses_" + "f" * 26),
        ):
            liveness.annotate([session])
        self.assertNotIn("keepalive_name", session)

    def test_end_to_end_real_claim_file_attaches(self) -> None:
        # 端到端：真 claim 文件，不 mock ses_id_for_ident。
        full = "ses_" + "0" * 25 + "1"
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                self.assertTrue(opencode_identity.write_intent(
                    "486da5c2", full, "/work"))
                session = {"source": "opencode", "id": full, "pid": 9999}
                with (
                    mock.patch.object(
                        liveness, "_list_tmux_sessions",
                        return_value=[["corral-opencode-486da5c2", "100", "opencode"]],
                    ),
                    mock.patch.object(
                        liveness, "_build_ppid_map", return_value={9999: 50}),
                ):
                    liveness.annotate([session])
                self.assertEqual(
                    session.get("keepalive_name"), "corral-opencode-486da5c2")

    def test_young_provisional_panes_skip_ancestor_chain(self) -> None:
        opencode_session = {"source": "opencode", "id": "ses_" + "e" * 26, "pid": 9999}
        claude_session = {"source": "claude", "id": "claude-1", "pid": 9999}
        rows = [["corral-opencode-486da5c2", "100"], ["corral-claude-abcd1234", "200"]]
        with (
            mock.patch.object(liveness, "_list_tmux_sessions", return_value=rows),
            mock.patch.object(liveness, "_build_ppid_map",
                              return_value={9999: 100, 100: 1, 200: 1}),
            mock.patch.object(opencode_identity, "ses_id_for_ident", return_value=None),
        ):
            # 年轻占位 pane：祖先链对谁都不贴。
            liveness.annotate(
                [dict(opencode_session), dict(claude_session)],
                provisional_names={"corral-opencode-486da5c2", "corral-claude-abcd1234"},
            )
            self.assertNotIn("keepalive_name", opencode_session)
            self.assertNotIn("keepalive_name", claude_session)

    def test_ancestor_chain_skips_claimed_opencode_pane(self) -> None:
        # 非年轻 + 有 claim 的预钉 pane：opencode 专属分支跳过祖先链。
        full = "ses_" + "e" * 26
        session = {"source": "opencode", "id": "ses_other" + "0" * 17, "pid": 9999}
        with (
            mock.patch.object(
                liveness, "_list_tmux_sessions",
                return_value=[["corral-opencode-486da5c2", "100", "opencode"]],
            ),
            mock.patch.object(
                liveness, "_build_ppid_map", return_value={9999: 100, 100: 1}),
            mock.patch.object(
                opencode_identity, "ses_id_for_ident", return_value=full),
        ):
            liveness.annotate([session])
        self.assertNotIn("keepalive_name", session)

    def test_ancestor_chain_keeps_working_for_other_runtimes(self) -> None:
        # 同样条件下 claude 仍走祖先链贴上（opencode 特判不得误伤他运行时）。
        session = {"source": "claude", "id": "claude-1", "pid": 9999}
        with (
            mock.patch.object(
                liveness, "_list_tmux_sessions",
                return_value=[["corral-claude-abcd1234", "200", "claude"]],
            ),
            mock.patch.object(
                liveness, "_build_ppid_map", return_value={9999: 200, 200: 1}),
        ):
            liveness.annotate([session])
        self.assertEqual(session.get("keepalive_name"), "corral-claude-abcd1234")

    def test_ancestor_chain_allows_unclaimed_old_opencode_pane(self) -> None:
        # 预钉前老 pane（无 claim、非年轻）：保留祖先链兼容，否则永不贴名。
        session = {"source": "opencode", "id": "ses_" + "e" * 26, "pid": 9999}
        with (
            mock.patch.object(
                liveness, "_list_tmux_sessions",
                return_value=[["corral-opencode-486da5c2", "100", "opencode"]],
            ),
            mock.patch.object(
                liveness, "_build_ppid_map", return_value={9999: 100, 100: 1}),
            mock.patch.object(
                opencode_identity, "ses_id_for_ident", return_value=None),
        ):
            liveness.annotate([session])
        self.assertEqual(
            session.get("keepalive_name"), "corral-opencode-486da5c2")


class StoreClaimTests(unittest.TestCase):
    def _store(self) -> SessionStore:
        return object.__new__(SessionStore)

    def test_opencode_claim_exact_wins_over_mtime_guess(self) -> None:
        store = self._store()
        full = "ses_" + "f" * 26
        other = "ses_" + "0" * 26
        now = 1_000_000.0
        store.sessions = {"opencode": [
            {"source": "opencode", "id": full, "cwd": "/work", "mtime": now - 100},
            {"source": "opencode", "id": other, "cwd": "/work", "mtime": now},
        ]}
        store._order = [f"opencode:{full}", f"opencode:{other}"]
        store.hosted = {}
        store._provisional = {}
        provisional = {"source": "opencode", "id": "486da5c2", "cwd": "/work",
                       "mtime": now}
        with (
            mock.patch.object(opencode_identity, "ses_id_for_ident", return_value=full),
            mock.patch("time.time", return_value=now),
        ):
            claimed = store._claim_unique_hosted_newcomer(
                "opencode:486da5c2", provisional)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], full)

    def test_opencode_pending_without_db_row_claims_nothing(self) -> None:
        store = self._store()
        full = "ses_" + "f" * 26
        now = 1_000_000.0
        # 同 cwd 有一条很新的旧卡：没有 claim 精确命中时也不许吃掉它。
        store.sessions = {"opencode": [
            {"source": "opencode", "id": "ses_" + "0" * 26,
             "cwd": "/work", "mtime": now - 1},
        ]}
        store._order = ["opencode:ses_" + "0" * 26]
        store.hosted = {}
        store._provisional = {}
        provisional = {"source": "opencode", "id": "486da5c2", "cwd": "/work",
                       "mtime": now}
        with (
            mock.patch.object(opencode_identity, "ses_id_for_ident", return_value=full),
            mock.patch("time.time", return_value=now),
        ):
            self.assertIsNone(store._claim_unique_hosted_newcomer(
                "opencode:486da5c2", provisional))

    def test_opencode_guards_refuse_hosted_and_named_cards(self) -> None:
        # id 命中但已被托管 / 已贴名：不抢。
        store = self._store()
        full = "ses_" + "f" * 26
        now = 1_000_000.0
        card = {"source": "opencode", "id": full, "cwd": "/work", "mtime": now}
        provisional = {"source": "opencode", "id": "486da5c2", "cwd": "/work",
                       "mtime": now}
        with (
            mock.patch.object(opencode_identity, "ses_id_for_ident", return_value=full),
            mock.patch("time.time", return_value=now),
        ):
            store.sessions = {"opencode": [dict(card)]}
            store._order = [f"opencode:{full}"]
            store.hosted = {f"opencode:{full}": "corral-opencode-other"}
            store._provisional = {}
            self.assertIsNone(store._claim_unique_hosted_newcomer(
                "opencode:486da5c2", provisional))
            store.sessions = {"opencode": [dict(card, keepalive_name="n")]}
            store.hosted = {}
            self.assertIsNone(store._claim_unique_hosted_newcomer(
                "opencode:486da5c2", provisional))

    def test_old_pane_without_claim_falls_back_to_mtime(self) -> None:
        # 预钉前老 pane（无 claim）：走有界 mtime 兼容（单候选）。
        store = self._store()
        now = 1_000_000.0
        card = {"source": "opencode", "id": "ses_" + "0" * 26,
                "cwd": "/work", "mtime": now - 1}
        store.sessions = {"opencode": [card]}
        store._order = ["opencode:ses_" + "0" * 26]
        store.hosted = {}
        store._provisional = {}
        provisional = {"source": "opencode", "id": "oldpane1", "cwd": "/work",
                       "mtime": now}
        with (
            mock.patch.object(opencode_identity, "ses_id_for_ident", return_value=None),
            mock.patch.dict("os.environ", {"CORRAL_CACHE": "1"}),
            mock.patch("time.time", return_value=now),
        ):
            claimed = store._claim_unique_hosted_newcomer(
                "opencode:oldpane1", provisional)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["id"], "ses_" + "0" * 26)

    def test_cache_disabled_without_claim_claims_nothing(self) -> None:
        # CORRAL_CACHE=0 且无 claim：绝不退回 mtime 猜（-s 主通道不受影响）。
        store = self._store()
        now = 1_000_000.0
        store.sessions = {"opencode": [
            {"source": "opencode", "id": "ses_" + "0" * 26,
             "cwd": "/work", "mtime": now - 1},
        ]}
        store._order = ["opencode:ses_" + "0" * 26]
        store.hosted = {}
        store._provisional = {}
        provisional = {"source": "opencode", "id": "486da5c2", "cwd": "/work",
                       "mtime": now}
        with (
            mock.patch.dict("os.environ", {"CORRAL_CACHE": "0"}),
            mock.patch("time.time", return_value=now),
        ):
            self.assertIsNone(store._claim_unique_hosted_newcomer(
                "opencode:486da5c2", provisional))

    def test_mismatched_claim_blocks_name_based_retirement(self) -> None:
        provisional = {"source": "opencode", "id": "486da5c2"}
        full = "ses_" + "f" * 26
        with mock.patch.object(
            opencode_identity, "ses_id_for_ident", return_value=full,
        ):
            self.assertTrue(SessionStore._is_mismatched_opencode_claim(
                provisional, {"source": "opencode", "id": "ses_other" + "0" * 17}))
            self.assertFalse(SessionStore._is_mismatched_opencode_claim(
                provisional, {"source": "opencode", "id": full}))
        with mock.patch.object(
            opencode_identity, "ses_id_for_ident", return_value=None,
        ):
            self.assertFalse(SessionStore._is_mismatched_opencode_claim(
                provisional, {"source": "opencode", "id": "ses_other" + "0" * 17}))
        self.assertFalse(SessionStore._is_mismatched_opencode_claim(
            {"source": "claude", "id": "x"}, {"source": "claude", "id": "y"}))

    def test_drop_provisional_discards_claim(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            claims = Path(directory) / "claims"
            with mock.patch.object(opencode_identity, "CLAIM_DIR", claims):
                full = "ses_" + "f" * 26
                self.assertTrue(opencode_identity.write_intent(
                    "486da5c2", full, "/work"))
                self.assertTrue((claims / "486da5c2.json").exists())
                store = self._store()
                store.lock = threading.Lock()
                key = "opencode:486da5c2"
                store._provisional = {
                    key: {"source": "opencode", "id": "486da5c2"}}
                store.hosted = {key: "corral-opencode-486da5c2"}
                store.sessions = {"opencode": []}
                store._order = [key]
                store.display_titles = {}
                store.generating = set()
                store.attention_states = {}
                store._cursor_attention_signatures = {}
                store._attention_evidence_cache = {}
                store._projects = None
                store._invalidate_conversation = lambda _k: None  # type: ignore[method-assign]
                store._drop_provisional(
                    key, {"source": "opencode", "id": "486da5c2"})
                self.assertNotIn(key, store._provisional)
                self.assertNotIn(key, store.hosted)
                self.assertFalse((claims / "486da5c2.json").exists())


if __name__ == "__main__":
    unittest.main()
