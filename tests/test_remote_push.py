"""Session-end and waiting pushes from PushNotifier + SessionHub status hooks."""

from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sesskit import titles as sesskit_titles

from corral.remote import crypto
from corral.remote.config import PairedDevice, RemoteState
from corral.remote.push import PushNotifier
from corral.remote.sessions import SessionHub

_HAS_CRYPTO = crypto.available()
_SKIP = "未安装 remote 附加组件（pip install '.[remote]'）"


def _session(
    *,
    sid: str = "s1",
    status_tag: str = sesskit_titles.STATUS_PENDING,
    last_agent: str = "PONG",
    attention: str = "none",
) -> dict:
    return {
        "source": "pi",
        "id": sid,
        "short_id": sid,
        "cwd": "/tmp/proj",
        "cwd_display": "/tmp/proj",
        "mtime": 1_700_000_000.0,
        "display_time": "01-01 12:00",
        "size_kb": 1.0,
        "status_tag": status_tag,
        "live": True,
        "keepalive_name": None,
        "fallback_title": "probe",
        "attention_kind": attention,
        "last_user_msg": "ping",
        "last_agent_msg": last_agent,
        "path": "/tmp/hist.jsonl",
    }


@unittest.skipUnless(_HAS_CRYPTO, _SKIP)
class PushNotifierSessionEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.device_private = crypto.generate_private_key_bytes()
        self.host_private = crypto.generate_private_key_bytes()
        self.sent: list[tuple[str, str, bytes]] = []
        state = RemoteState(
            host_id="host",
            host_name="Mac",
            devices=[
                PairedDevice(
                    id="d1",
                    name="iPhone",
                    public_key=crypto.public_key_bytes(self.device_private).hex(),
                    paired_at=1.0,
                    push_token="a" * 64,
                    push_env="sandbox",
                )
            ],
        )
        self.notifier = PushNotifier(
            state,
            self.host_private,
            sender=self._capture,
            sent_path=Path(self._tmp.name) / "push-sent.json",
            pending_path=Path(self._tmp.name) / "push-pending.json",
        )

    def _capture(self, token: str, env: str, payload: bytes, push_id: str = "") -> None:
        self.sent.append((token, env, payload, push_id))

    def _open(self, sealed_b64: bytes) -> dict:
        sealed = __import__("base64").b64decode(sealed_b64)
        plain = crypto.open_from_host(
            self.device_private,
            crypto.public_key_bytes(self.host_private),
            sealed,
        )
        return json.loads(plain.decode("utf-8"))

    def _ok(self, push_id: str, **fields) -> None:
        receipt = {"ok": True, "code": "ok", "status": 200, "reason": "", "apns_id": "apns-1"}
        receipt.update(fields)
        self.notifier.on_push_receipt(push_id, receipt)

    def _fail(self, push_id: str, code: str = "transport") -> None:
        self.notifier.on_push_receipt(
            push_id, {"ok": False, "code": code, "status": 0, "reason": "", "apns_id": ""}
        )

    def test_pending_to_done_sends_completed(self) -> None:
        session = {
            "key": "pi:s1",
            "title": "probe",
            "runtime": "pi",
            "cwd_display": "/tmp",
            "last_agent": "all good",
            "completion_id": "100:10:done:aaaa",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 1)
        payload = self._open(self.sent[0][2])
        self.assertEqual(payload["kind"], "completed")
        self.assertEqual(payload["body"], "all good")
        self.assertEqual(payload["key"], "pi:s1")

    def test_done_without_completion_id_is_silent(self) -> None:
        """已完成但缺 completion_id（老 SessKit / Cursor 弱证据）不推。"""
        session = {"key": "pi:s1", "title": "probe", "last_agent": "maybe done"}
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(self.sent, [])

    def test_second_round_same_session_sends_again(self) -> None:
        """同一会话 120 秒内完成两轮：不同 completion_id 都推。"""
        first = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "round one",
            "completion_id": "100:10:done:aaaa",
        }
        second = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "round two",
            "completion_id": "200:20:done:bbbb",
        }
        self.notifier.on_status_change(
            first, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.notifier._last_sent.clear()  # 绕过 120 秒节流，验证去重键本身
        self.notifier.on_status_change(
            second, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 2)

    def test_same_round_does_not_resend(self) -> None:
        """同一轮重复跃迁：节流挡住；回执确认后即使清节流也不重推。"""
        session = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "done",
            "completion_id": "100:10:done:aaaa",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        # 节流窗内重复跃迁被挡住（未清节流）。
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 1)
        # 回执确认后：按设备已记已发，清节流重推也不发。
        self._ok(self.sent[0][3])
        self.notifier._last_sent.clear()
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 1)

    def test_enqueue_alone_never_marks_sent(self) -> None:
        """仅入队（无回执）不记已发：旧中继永不被当成功。"""
        session = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "done",
            "completion_id": "100:10:done:aaaa",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 1)
        round_key = self.notifier._round_key(session, "completed")
        self.assertFalse(self.notifier._already_sent(round_key, "d1"))

    def test_receipt_ok_marks_per_device(self) -> None:
        session = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "done",
            "completion_id": "100:10:done:aaaa",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self._ok(self.sent[0][3])
        round_key = self.notifier._round_key(session, "completed")
        self.assertTrue(self.notifier._already_sent(round_key, "d1"))

    def test_receipt_failure_retries_same_round(self) -> None:
        """回执失败清节流：下次扫描同一轮可重推，不记已发。"""
        session = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "done",
            "completion_id": "100:10:done:aaaa",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self._fail(self.sent[0][3], "no_push_config")
        round_key = self.notifier._round_key(session, "completed")
        self.assertFalse(self.notifier._already_sent(round_key, "d1"))
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(self.sent[0][3], self.sent[1][3])

    def test_completed_pref_off_is_silent(self) -> None:
        self.notifier.state.devices[0].notify_completed = False
        session = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "done",
            "completion_id": "100:10:done:aaaa",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(self.sent, [])

    def test_aborted_pref_off_blocks_aborted_only(self) -> None:
        self.notifier.state.devices[0].notify_aborted = False
        aborted = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "quota",
            "completion_id": "100:10:aborted:cccc",
        }
        self.notifier.on_status_change(
            aborted, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_ABORTED
        )
        self.assertEqual(self.sent, [])
        done = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "done",
            "completion_id": "200:20:done:dddd",
        }
        self.notifier.on_status_change(
            done, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 1)

    def test_pending_to_aborted_keeps_error_body(self) -> None:
        session = {
            "key": "pi:s1",
            "title": "probe",
            "runtime": "pi",
            "last_agent": "429 weekly limit",
            "completion_id": "100:10:aborted:cccc",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_ABORTED
        )
        payload = self._open(self.sent[0][2])
        self.assertEqual(payload["kind"], "aborted")
        self.assertIn("429", payload["body"])

    def test_done_to_done_is_silent(self) -> None:
        session = {"key": "pi:s1", "title": "x", "last_agent": "same"}
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_DONE, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(self.sent, [])

    def test_waiting_still_fires(self) -> None:
        session = {
            "key": "pi:s1",
            "title": "probe",
            "runtime": "pi",
            "last_agent": "pick one",
        }
        self.notifier.on_attention_change(session, "working", "waiting")
        payload = self._open(self.sent[0][2])
        self.assertEqual(payload["kind"], "waiting")
        self.assertEqual(payload["body"], "pick one")

    def test_throttle_same_kind(self) -> None:
        session = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "done",
            "completion_id": "100:10:done:aaaa",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 1)

    def test_restart_does_not_resend(self) -> None:
        """重启后已确认的轮次不重推：已发集合按设备落盘，新实例读盘。"""
        session = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "done",
            "completion_id": "100:10:done:aaaa",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 1)
        self._ok(self.sent[0][3])
        rebooted = PushNotifier(
            self.notifier.state,
            self.host_private,
            sender=self._capture,
            sent_path=Path(self._tmp.name) / "push-sent.json",
            pending_path=Path(self._tmp.name) / "push-pending.json",
        )
        rebooted.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 1)

    def test_restart_retries_unacked_round(self) -> None:
        """重启前未回执的轮次：待确认落盘，新实例经 retry_due 重发。"""
        session = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "done",
            "completion_id": "100:10:done:aaaa",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 1)
        rebooted = PushNotifier(
            self.notifier.state,
            self.host_private,
            sender=self._capture,
            sent_path=Path(self._tmp.name) / "push-sent.json",
            pending_path=Path(self._tmp.name) / "push-pending.json",
        )
        self.assertEqual(len(rebooted._pending), 1)
        # 回执超时：把待确认时间拨到过去，retry_due 应当重发同一轮。
        for entry in rebooted._pending.values():
            entry["ts"] -= 3600.0
        rebooted.retry_due([dict(session, kind="completed")])
        self.assertEqual(len(self.sent), 2)

    def test_partial_device_success_retries_only_failed(self) -> None:
        """多设备部分成功：已接受设备不重发，只重试被拒设备。"""
        second_private = crypto.generate_private_key_bytes()
        self.notifier.state.devices.append(
            PairedDevice(
                id="d2",
                name="iPad",
                public_key=crypto.public_key_bytes(second_private).hex(),
                paired_at=1.0,
                push_token="b" * 64,
                push_env="production",
            )
        )
        session = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "done",
            "completion_id": "100:10:done:aaaa",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 2)
        first_id = self.sent[0][3]
        second_id = self.sent[1][3]
        # d1 接受、d2 被拒。
        self._ok(first_id)
        self._fail(second_id, "bad_token")
        round_key = self.notifier._round_key(session, "completed")
        self.assertTrue(self.notifier._already_sent(round_key, "d1"))
        self.assertFalse(self.notifier._already_sent(round_key, "d2"))
        # 同一轮再推：只有 d2 出帧（d1 已记已发）。
        self.notifier._last_sent.clear()
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 3)
        self.assertEqual(self.sent[2][0], "b" * 64)

    def test_done_done_new_round_fires_through_hub(self) -> None:
        """SessionHub DONE→DONE 新一轮（completion_id 变）会调 hook 并推送。"""
        hub = SessionHub()
        hub.set_status_hook(self.notifier.on_status_change)
        path = Path(self._tmp.name) / "hub.jsonl"
        path.write_text("", encoding="utf-8")
        session = _session(
            status_tag=sesskit_titles.STATUS_PENDING, last_agent="working"
        )
        session["path"] = str(path)
        session["mtime"] = 1_700_000_000.0
        hub.store.sessions = {"pi": [session]}
        hub._snapshot_status()
        session["status_tag"] = sesskit_titles.STATUS_DONE
        session["completion_id"] = "100:10:done:aaaa"
        session["last_agent_msg"] = "round one done"
        session["mtime"] = 1_700_000_100.0
        hub._detect_status_changes()
        self.assertEqual(len(self.sent), 1)
        # 同一轮再次扫描：不重推。
        hub._detect_status_changes()
        self.assertEqual(len(self.sent), 1)
        # 新一轮：completion_id 变 + 节流窗外 -> 再推一条。
        session["completion_id"] = "200:20:done:bbbb"
        session["last_agent_msg"] = "round two done"
        session["mtime"] = 1_700_000_200.0
        self.notifier._last_sent.clear()
        hub._detect_status_changes()
        self.assertEqual(len(self.sent), 2)

    def test_skips_device_without_token(self) -> None:
        self.notifier.state.devices[0].push_token = ""
        self.notifier.on_status_change(
            {
                "key": "pi:s1",
                "title": "x",
                "last_agent": "y",
                "completion_id": "100:10:done:aaaa",
            },
            sesskit_titles.STATUS_PENDING,
            sesskit_titles.STATUS_DONE,
        )
        self.assertEqual(self.sent, [])


class SessionHubStatusHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(
            os.environ, {"CORRAL_CACHE_DIR": self._tmp.name}, clear=False
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.hub = SessionHub()
        self.calls: list[tuple[str, str]] = []
        self.hub.set_status_hook(self._hook)

    def _hook(self, session: dict, previous: str, current: str) -> None:
        self.calls.append((previous, current))

    def test_first_snapshot_does_not_push(self) -> None:
        path = Path(self._tmp.name) / "pi.jsonl"
        path.write_text("", encoding="utf-8")
        session = _session(status_tag=sesskit_titles.STATUS_DONE)
        session["path"] = str(path)
        self.hub.store.sessions = {"pi": [session]}
        self.hub._snapshot_status()
        self.hub._detect_status_changes()
        self.assertEqual(self.calls, [])

    def test_fresh_new_terminal_session_notifies(self) -> None:
        """Session that first appears already DONE still notifies when mtime is fresh."""
        path = Path(self._tmp.name) / "pi.jsonl"
        path.write_text("", encoding="utf-8")
        session = _session(status_tag=sesskit_titles.STATUS_DONE, last_agent="PONG")
        session["path"] = str(path)
        session["mtime"] = time.time()
        self.hub.store.sessions = {"pi": [session]}
        # No snapshot — key is brand new to the hub
        self.hub._detect_status_changes()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1], sesskit_titles.STATUS_DONE)

    def test_stale_new_terminal_session_is_silent(self) -> None:
        path = Path(self._tmp.name) / "pi.jsonl"
        path.write_text("", encoding="utf-8")
        session = _session(status_tag=sesskit_titles.STATUS_ABORTED, last_agent="old")
        session["path"] = str(path)
        session["mtime"] = time.time() - 10_000
        self.hub.store.sessions = {"pi": [session]}
        self.hub._detect_status_changes()
        self.assertEqual(self.calls, [])

    def test_pending_to_done_invokes_hook(self) -> None:
        path = Path(self._tmp.name) / "pi.jsonl"
        path.write_text("", encoding="utf-8")
        session = _session(status_tag=sesskit_titles.STATUS_PENDING)
        session["path"] = str(path)
        self.hub.store.sessions = {"pi": [session]}
        self.hub._snapshot_status()
        session["status_tag"] = sesskit_titles.STATUS_DONE
        session["last_agent_msg"] = "finished"
        self.hub._detect_status_changes()
        self.assertEqual(
            self.calls,
            [(sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE)],
        )

    def test_aborted_invokes_hook(self) -> None:
        path = Path(self._tmp.name) / "pi.jsonl"
        path.write_text("", encoding="utf-8")
        session = _session(status_tag=sesskit_titles.STATUS_PENDING)
        session["path"] = str(path)
        self.hub.store.sessions = {"pi": [session]}
        self.hub._snapshot_status()
        session["status_tag"] = sesskit_titles.STATUS_ABORTED
        session["last_agent_msg"] = "quota"
        self.hub._detect_status_changes()
        self.assertEqual(self.calls[-1][1], sesskit_titles.STATUS_ABORTED)


if __name__ == "__main__":
    unittest.main()
