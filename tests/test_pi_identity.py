"""Pi 会话身份桥测试：扩展安装、claim 读取与扫描精确绑定。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from corral import pi_identity
from corral.models import LaunchPlan
from corral.runtime.host_extension import corral_host_extension


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _claim(
    instance_id: str,
    session_id: str,
    pid: int,
    *,
    state: str = "active",
    updated: datetime | None = None,
    sequence: int = 1,
    session_file: str | None = None,
) -> dict:
    return {
        "protocolVersion": 1,
        "extensionVersion": pi_identity.EXTENSION_VERSION,
        "instanceId": instance_id,
        "pid": pid,
        "instanceNonce": "n" * 16,
        "state": state,
        "sessionId": session_id,
        "sessionFile": session_file,
        "cwd": "/tmp/proj",
        "parentSession": None,
        "reason": "startup",
        "updatedAt": _iso(updated or datetime.now(timezone.utc)),
        "sequence": sequence,
    }


class InstallerTests(unittest.TestCase):
    def test_install_creates_entry_and_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            status = pi_identity.ensure_extension_installed(td)
            entry = Path(td) / "extensions" / "corral-session-identity" / "index.ts"
            manifest = entry.parent / "corral-manifest.json"
            self.assertEqual(status["status"], "installed")
            self.assertTrue(entry.exists())
            self.assertEqual(entry.read_bytes(), pi_identity.asset_path().read_bytes())
            data = json.loads(manifest.read_text())
            self.assertEqual(data["owner"], "corral")
            self.assertEqual(data["claimProtocol"], 1)

    def test_install_is_idempotent_and_upgrades_changed_asset(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            first = pi_identity.ensure_extension_installed(td)
            self.assertEqual(first["status"], "installed")
            again = pi_identity.ensure_extension_installed(td)
            self.assertEqual(again["status"], "ok")
            entry = Path(td) / "extensions" / "corral-session-identity" / "index.ts"
            entry.write_text("// 被篡改/旧版本内容", encoding="utf-8")
            fixed = pi_identity.ensure_extension_installed(td)
            self.assertEqual(fixed["status"], "installed")
            self.assertEqual(entry.read_bytes(), pi_identity.asset_path().read_bytes())

    def test_foreign_directory_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            entry = (
                Path(td) / "extensions" / "corral-session-identity" / "index.ts"
            )
            entry.parent.mkdir(parents=True)
            entry.write_text("用户自己的同名扩展", encoding="utf-8")
            with self.assertRaises(pi_identity.PiExtensionInstallError):
                pi_identity.ensure_extension_installed(td)
            self.assertEqual(entry.read_text(encoding="utf-8"), "用户自己的同名扩展")

    def test_foreign_manifest_owner_refuses_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            entry = (
                Path(td) / "extensions" / "corral-session-identity" / "index.ts"
            )
            entry.parent.mkdir(parents=True)
            entry.write_text("x", encoding="utf-8")
            (entry.parent / "corral-manifest.json").write_text(
                json.dumps({"owner": "someone-else"}), encoding="utf-8"
            )
            with self.assertRaises(pi_identity.PiExtensionInstallError):
                pi_identity.ensure_extension_installed(td)


class ClaimReaderTests(unittest.TestCase):
    def test_read_claims_skips_broken_and_reads_valid(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            directory = pi_identity.claims_dir(td)
            directory.mkdir(parents=True)
            good = directory / "inst1.json"
            good.write_text(json.dumps(_claim("inst1", "sess-a", 11)), encoding="utf-8")
            (directory / "broken.json").write_text("{半份", encoding="utf-8")
            claims = pi_identity.read_claims(td)
            self.assertEqual([c["sessionId"] for c in claims], ["sess-a"])

    def test_claim_is_live_checks_protocol_state_and_ttl(self) -> None:
        now = datetime.now(timezone.utc)
        self.assertTrue(pi_identity.claim_is_live(_claim("i", "s", 1), now))
        self.assertFalse(pi_identity.claim_is_live(None, now))
        expired = _claim("i", "s", 1, updated=now - timedelta(seconds=pi_identity.CLAIM_TTL_SECONDS + 5))
        self.assertFalse(pi_identity.claim_is_live(expired, now))
        shutdown = _claim("i", "s", 1, state="shutdown")
        self.assertFalse(pi_identity.claim_is_live(shutdown, now))
        bad_protocol = _claim("i", "s", 1)
        bad_protocol["protocolVersion"] = 99
        self.assertFalse(pi_identity.claim_is_live(bad_protocol, now))
        no_session = _claim("i", "", 1)
        self.assertFalse(pi_identity.claim_is_live(no_session, now))

    def test_read_claim_rejects_path_traversal(self) -> None:
        self.assertIsNone(pi_identity.read_claim("../escape"))
        self.assertIsNone(pi_identity.read_claim(""))

    def test_live_agent_phases_maps_working_from_live_claims(self) -> None:
        now = datetime.now(timezone.utc)
        with tempfile.TemporaryDirectory() as td:
            directory = pi_identity.claims_dir(td)
            directory.mkdir(parents=True)
            working = _claim("inst-w", "sess-w", 21, updated=now)
            working["agentPhase"] = "working"
            working["agentPhaseEvent"] = "agent_start"
            working["agentPhaseAt"] = _iso(now)
            idle = _claim("inst-i", "sess-i", 22, updated=now)
            idle["agentPhase"] = "idle"
            expired = _claim(
                "inst-e",
                "sess-e",
                23,
                updated=now - timedelta(seconds=pi_identity.CLAIM_TTL_SECONDS + 5),
            )
            expired["agentPhase"] = "working"
            (directory / "inst-w.json").write_text(json.dumps(working), encoding="utf-8")
            (directory / "inst-i.json").write_text(json.dumps(idle), encoding="utf-8")
            (directory / "inst-e.json").write_text(json.dumps(expired), encoding="utf-8")
            phases = pi_identity.live_agent_phases(td, now=now)
            self.assertEqual(phases["sess-w"]["phase"], "working")
            self.assertEqual(phases["sess-i"]["phase"], "idle")
            self.assertNotIn("sess-e", phases)

    def test_instance_env_pairs_shape(self) -> None:
        pairs = pi_identity.instance_env_pairs("abc", "/root")
        self.assertEqual(pairs[0], "-e")
        self.assertIn(f"{pi_identity.INSTANCE_ENV}=abc", pairs)
        self.assertIn(
            f"{pi_identity.CLAIM_PATH_ENV}=/root/corral-session-identity/claims/v1/abc.json",
            pairs,
        )
        self.assertIn(f"{pi_identity.PI_SESSION_DIR_ENV}=", pairs)


class RuntimePlanTests(unittest.TestCase):
    def test_bind_hosted_ident_never_injects_session_dir(self) -> None:
        from corral.runtime.pi import bind_hosted_ident

        new = bind_hosted_ident(LaunchPlan(("pi", "--approve"), "/tmp/p"), "abcd1234")
        self.assertEqual(
            new.argv, ("pi", "--approve", "--session-id", "abcd1234")
        )
        resume = bind_hosted_ident(
            LaunchPlan(("pi", "--approve", "--session", "/tmp/a.jsonl"), "/tmp/p"),
            "abcd1234",
        )
        self.assertEqual(
            resume.argv, ("pi", "--approve", "--session", "/tmp/a.jsonl")
        )


class ClaimLiveFlagTests(unittest.TestCase):
    """claim 是 live 归属第一权威：只按精确 session id 绑定，缺失时不猜。"""

    def setUp(self) -> None:
        from corral.scan import pi as scan_pi

        self.scan_pi = scan_pi
        scan_pi.reset_live_session_overrides()

    def tearDown(self) -> None:
        self.scan_pi.reset_live_session_overrides()

    def _write_pi_session(self, root: Path, session_id: str, cwd: str) -> Path:
        path = root / f"2026-08-26T00-00-00-000Z_{session_id}.jsonl"
        path.write_text(
            "\n".join(
                [
                    json.dumps(
                        {
                            "type": "session",
                            "id": session_id,
                            "timestamp": "2026-08-26T00:00:00Z",
                            "cwd": cwd,
                        }
                    ),
                    json.dumps(
                        {
                            "type": "message",
                            "id": f"u-{session_id}",
                            "parentId": None,
                            "timestamp": "2026-08-26T00:00:01Z",
                            "message": {
                                "role": "user",
                                "content": [{"type": "text", "text": "问题"}],
                            },
                        }
                    ),
                ]
            ),
            encoding="utf-8",
        )
        return path

    def test_claim_binds_exact_session_and_beats_cwd_pairing(self) -> None:
        """两个同 cwd 裸 TUI：claim 指哪条绑哪条，不吃 cwd 启发式的亏。"""
        with tempfile.TemporaryDirectory() as td:
            cwd = str(Path(td) / "proj")
            Path(cwd).mkdir()
            newer_id, claimed_id = "newest123", "claimed999"
            # newest 更新（cwd 配对若生效会抢到它），但 claim 指向 claimed。
            self._write_pi_session(Path(td), newer_id, cwd)
            self._write_pi_session(Path(td), claimed_id, cwd)
            real_cwd = os.path.realpath(cwd)
            claim = _claim("inst-a", claimed_id, 31)
            cmdlines = {
                11: "pi --approve",
                31: "pi --approve",
            }
            with mock.patch.object(self.scan_pi, "SESSIONS_DIR", td), mock.patch.object(
                self.scan_pi, "live_processes",
                return_value=[(11, real_cwd), (31, real_cwd)],
            ), mock.patch.object(
                self.scan_pi, "process_command_line",
                side_effect=lambda pid: cmdlines[pid],
            ), mock.patch.object(
                self.scan_pi, "open_file_paths", return_value={11: [], 31: []}
            ), mock.patch.object(
                self.scan_pi, "process_start_time", return_value=None
            ), mock.patch.object(
                self.scan_pi, "process_environ",
                side_effect=lambda pid, **kwargs: (
                    {pi_identity.INSTANCE_ENV: "inst-a"} if pid == 31 else {}
                ),
            ), mock.patch.object(
                # Claims flow through the host extension provider, which reads
                # `corral.pi_identity` — patch the Corral reader, not SessKit.
                pi_identity, "read_claims", return_value=[claim]
            ):
                sessions = self.scan_pi.scan_sessions(limit=10, host=corral_host_extension())
            by_id = {item["id"]: item for item in sessions}
            self.assertTrue(by_id[claimed_id]["live"])
            self.assertEqual(by_id[claimed_id]["pid"], 31)
            # 另一个进程没有 claim 也没有别的正向证据：宁可不被绑定。
            self.assertFalse(by_id[newer_id]["live"])

    def test_managed_process_without_claim_stays_provisional(self) -> None:
        """托管进程（env 有 instance）但没有有效 claim：不回落 cwd/mtime 猜测。"""
        with tempfile.TemporaryDirectory() as td:
            cwd = str(Path(td) / "proj")
            Path(cwd).mkdir()
            session_id = "history1"
            self._write_pi_session(Path(td), session_id, cwd)
            real_cwd = os.path.realpath(cwd)
            with mock.patch.object(self.scan_pi, "SESSIONS_DIR", td), mock.patch.object(
                self.scan_pi, "live_processes", return_value=[(41, real_cwd)]
            ), mock.patch.object(
                self.scan_pi, "process_command_line", return_value="pi --approve"
            ), mock.patch.object(
                self.scan_pi, "open_file_paths", return_value={41: []}
            ), mock.patch.object(
                self.scan_pi, "process_start_time", return_value=None
            ), mock.patch.object(
                self.scan_pi, "process_environ",
                side_effect=lambda pid, **kwargs: (
                    {pi_identity.INSTANCE_ENV: "inst-b"} if pid == 41 else {}
                ),
            ), mock.patch.object(
                pi_identity, "read_claims", return_value=[]
            ):
                sessions = self.scan_pi.scan_sessions(limit=10, host=corral_host_extension())
            by_id = {item["id"]: item for item in sessions}
            self.assertFalse(by_id[session_id]["live"])
            self.assertIsNone(by_id[session_id]["pid"])

    def test_claim_with_instance_mismatch_is_ignored(self) -> None:
        """claim 的 instanceId 与 env 注入值不一致（陈旧 claim）：不采信。"""
        with tempfile.TemporaryDirectory() as td:
            cwd = str(Path(td) / "proj")
            Path(cwd).mkdir()
            session_id = "mismatch1"
            self._write_pi_session(Path(td), session_id, cwd)
            real_cwd = os.path.realpath(cwd)
            claim = _claim("other-instance", session_id, 51)
            with mock.patch.object(self.scan_pi, "SESSIONS_DIR", td), mock.patch.object(
                self.scan_pi, "live_processes", return_value=[(51, real_cwd)]
            ), mock.patch.object(
                self.scan_pi, "process_command_line", return_value="pi --approve"
            ), mock.patch.object(
                self.scan_pi, "open_file_paths", return_value={51: []}
            ), mock.patch.object(
                self.scan_pi, "process_start_time", return_value=None
            ), mock.patch.object(
                self.scan_pi, "process_environ",
                side_effect=lambda pid, **kwargs: (
                    {pi_identity.INSTANCE_ENV: "inst-c"} if pid == 51 else {}
                ),
            ), mock.patch.object(
                pi_identity, "read_claims", return_value=[claim]
            ):
                sessions = self.scan_pi.scan_sessions(limit=10, host=corral_host_extension())
            by_id = {item["id"]: item for item in sessions}
            self.assertFalse(by_id[session_id]["live"])

    def test_native_claim_without_env_still_binds(self) -> None:
        """裸 Pi（无托管 env）的 native claim 同样精确绑定。"""
        with tempfile.TemporaryDirectory() as td:
            cwd = str(Path(td) / "proj")
            Path(cwd).mkdir()
            session_id = "native001"
            self._write_pi_session(Path(td), session_id, cwd)
            real_cwd = os.path.realpath(cwd)
            claim = _claim("native-xyz", session_id, 61)
            with mock.patch.object(self.scan_pi, "SESSIONS_DIR", td), mock.patch.object(
                self.scan_pi, "live_processes", return_value=[(61, real_cwd)]
            ), mock.patch.object(
                self.scan_pi, "process_command_line", return_value="pi --approve"
            ), mock.patch.object(
                self.scan_pi, "open_file_paths", return_value={61: []}
            ), mock.patch.object(
                self.scan_pi, "process_start_time", return_value=None
            ), mock.patch.object(
                self.scan_pi, "process_environ", return_value={}
            ), mock.patch.object(
                pi_identity, "read_claims", return_value=[claim]
            ):
                sessions = self.scan_pi.scan_sessions(limit=10, host=corral_host_extension())
            by_id = {item["id"]: item for item in sessions}
            self.assertTrue(by_id[session_id]["live"])
            self.assertEqual(by_id[session_id]["pid"], 61)

    def test_claim_session_file_binds_when_session_id_misses(self) -> None:
        """header id 对不上 claim.sessionId 时，精确 sessionFile 仍是正向证据。"""
        with tempfile.TemporaryDirectory() as td:
            cwd = str(Path(td) / "proj")
            Path(cwd).mkdir()
            real_id = "0198abcdef01234567890123456789ab"
            path = self._write_pi_session(Path(td), real_id, cwd)
            real_cwd = os.path.realpath(cwd)
            claim = _claim("inst-file", "abcd1234", 81, session_file=str(path))
            with mock.patch.object(self.scan_pi, "SESSIONS_DIR", td), mock.patch.object(
                self.scan_pi, "live_processes", return_value=[(81, real_cwd)]
            ), mock.patch.object(
                self.scan_pi, "process_command_line", return_value="pi --approve"
            ), mock.patch.object(
                self.scan_pi, "open_file_paths", return_value={81: []}
            ), mock.patch.object(
                self.scan_pi, "process_start_time", return_value=None
            ), mock.patch.object(
                self.scan_pi, "process_environ",
                side_effect=lambda pid, **kwargs: (
                    {pi_identity.INSTANCE_ENV: "inst-file"} if pid == 81 else {}
                ),
            ), mock.patch.object(
                pi_identity, "read_claims", return_value=[claim]
            ):
                sessions = self.scan_pi.scan_sessions(limit=10, host=corral_host_extension())
            by_id = {item["id"]: item for item in sessions}
            self.assertTrue(by_id[real_id]["live"])
            self.assertEqual(by_id[real_id]["pid"], 81)

    def test_switching_claim_keeps_old_identity_live(self) -> None:
        """switching 期间保留旧会话 live，等新 session_start 覆盖。"""
        with tempfile.TemporaryDirectory() as td:
            cwd = str(Path(td) / "proj")
            Path(cwd).mkdir()
            old_id = "oldsess1"
            self._write_pi_session(Path(td), old_id, cwd)
            real_cwd = os.path.realpath(cwd)
            claim = _claim("inst-d", old_id, 71, state="switching")
            with mock.patch.object(self.scan_pi, "SESSIONS_DIR", td), mock.patch.object(
                self.scan_pi, "live_processes", return_value=[(71, real_cwd)]
            ), mock.patch.object(
                self.scan_pi, "process_command_line", return_value="pi --approve"
            ), mock.patch.object(
                self.scan_pi, "open_file_paths", return_value={71: []}
            ), mock.patch.object(
                self.scan_pi, "process_start_time", return_value=None
            ), mock.patch.object(
                self.scan_pi, "process_environ",
                side_effect=lambda pid, **kwargs: (
                    {pi_identity.INSTANCE_ENV: "inst-d"} if pid == 71 else {}
                ),
            ), mock.patch.object(
                pi_identity, "read_claims", return_value=[claim]
            ):
                sessions = self.scan_pi.scan_sessions(limit=10, host=corral_host_extension())
            by_id = {item["id"]: item for item in sessions}
            self.assertTrue(by_id[old_id]["live"])


if __name__ == "__main__":
    unittest.main()
