"""Session-end and waiting pushes from PushNotifier + SessionHub status hooks."""

from __future__ import annotations

import json
import os
import tempfile
import threading
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
        """回执失败保留待确认：同 completion 下次扫描重试，不记已发（精确计数）。"""
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
        self._fail(self.sent[0][3], "transport")
        round_key = self.notifier._round_key(session, "completed")
        self.assertFalse(self.notifier._already_sent(round_key, "d1"))
        self.assertEqual(len(self.notifier._pending), 1)
        # 同一轮直接重调 hook 被在途抑制（不重复入队）。
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 1)
        # 退避到期后扫描驱动重试：恰好再发一帧。
        for entry in self.notifier._pending.values():
            entry["ts"] -= 3600.0
        self.notifier.retry_due([dict(session)])
        self.assertEqual(len(self.sent), 2)
        self.assertNotEqual(self.sent[0][3], self.sent[1][3])
        self.assertFalse(self.notifier._already_sent(round_key, "d1"))

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
        """多设备部分成功：已接受设备不重发，只重试被拒设备（精确计数）。"""
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
        # d1 接受、d2 被拒（transient，保留待确认）。
        self._ok(first_id)
        self._fail(second_id, "transport")
        round_key = self.notifier._round_key(session, "completed")
        self.assertTrue(self.notifier._already_sent(round_key, "d1"))
        self.assertFalse(self.notifier._already_sent(round_key, "d2"))
        self.assertEqual(len(self.notifier._pending), 1)
        # 同一轮直接重调 hook：d1 已发跳过，d2 在途抑制——零新帧。
        self.notifier._last_sent.clear()
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 2)
        # 退避到期后扫描驱动：只重发 d2 一帧（总数 3），d1 永不重发。
        for entry in self.notifier._pending.values():
            entry["ts"] -= 3600.0
        self.notifier.retry_due([dict(session)])
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

    def test_apple_5xx_earliest_retry_900s(self) -> None:
        """Apple 5xx receipts retry no earlier than 900s, persist across restart."""
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
        self.notifier.on_push_receipt(
            self.sent[0][3],
            {"ok": False, "code": "transport", "status": 503,
             "reason": "ServiceUnavailable", "apns_id": ""},
        )
        pending = list(self.notifier._pending.values())
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["status"], 503)
        now = time.time()
        for age in (60.0, 600.0):
            for entry in self.notifier._pending.values():
                entry["ts"] = now - age
            self.notifier.retry_due([dict(session)])
            self.assertEqual(len(self.sent), 1)
        for entry in self.notifier._pending.values():
            entry["ts"] = now - 901.0
        self.notifier.retry_due([dict(session)])
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(
            [e["attempts"] for e in self.notifier._pending.values()], [2]
        )
        # Restart keeps the 900s rule and the attempt count (same cap).
        rebooted = PushNotifier(
            self.notifier.state,
            self.host_private,
            sender=self._capture,
            sent_path=Path(self._tmp.name) / "push-sent.json",
            pending_path=Path(self._tmp.name) / "push-pending.json",
        )
        rebooted_pending = list(rebooted._pending.values())
        self.assertEqual(len(rebooted_pending), 1)
        self.assertEqual(rebooted_pending[0]["status"], 503)
        self.assertEqual(rebooted_pending[0]["attempts"], 2)
        for entry in rebooted._pending.values():
            entry["ts"] = time.time() - 901.0
        rebooted.retry_due([dict(session)])
        self.assertEqual(len(self.sent), 3)

    def test_concurrent_save_newer_state_wins(self) -> None:
        """Barrier-sequenced race: serialized atomic saves, newer wins on disk.

        Ordering is fully deterministic (events only, no sleeps): worker T1
        blocks inside its gated write while HOLDING the ledger lock; main waits
        for that gate, starts the accepting receipt on T2 (which must queue
        behind the lock), then releases. T1's stale snapshot ({P1, P2}) always
        lands first; T2's newer snapshot ({P2} + sent P1) always wins.
        """
        entered = threading.Event()
        release = threading.Event()
        orig_write = PushNotifier._write_atomic
        calls = []

        def gated(path, text):
            calls.append(path.name)
            if len(calls) == 2:
                entered.set()
                self.assertTrue(release.wait(timeout=10), "gate was never released")
            return orig_write(path, text)

        self.notifier._write_atomic = gated
        session = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "done",
            "completion_id": "100:10:done:aaaa",
        }
        # Phase 1: normal enqueue, write #1 completes. P1 id from sender frames.
        self.notifier.on_status_change(
            dict(session), sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 1)
        p1 = self.sent[0][3]
        round_key = self.notifier._round_key(session, "completed")
        # Phase 2: T1 tracks a second-round entry; its write #2 gates mid-lock.
        p2_entry = {
            "round": "pi:s1\x00200:20:done:bbbb\x00completed",
            "device": "d1",
            "kind": "completed",
            "session": "pi:s1",
            "completion": "200:20:done:bbbb",
            "ts": time.time(),
            "attempts": 1,
            "last_code": "",
            "status": 0,
            "parked": False,
        }
        worker = threading.Thread(
            target=self.notifier._track_pending, args=("p2-test", p2_entry)
        )
        worker.start()
        self.assertTrue(entered.wait(timeout=10), "gated write never started")
        accepter = threading.Thread(
            target=self.notifier.on_push_receipt,
            args=(p1, {"ok": True, "code": "ok", "status": 200,
                       "reason": "", "apns_id": "x"}),
        )
        accepter.start()
        release.set()
        worker.join(timeout=10)
        accepter.join(timeout=10)
        self.assertFalse(worker.is_alive(), "worker did not finish")
        self.assertFalse(accepter.is_alive(), "accepter did not finish")
        final_pending = json.loads(
            (Path(self._tmp.name) / "push-pending.json").read_text(encoding="utf-8")
        )
        final_sent = json.loads(
            (Path(self._tmp.name) / "push-sent.json").read_text(encoding="utf-8")
        )
        # Newer wins: P1 accepted (gone from pending, in sent), P2 kept.
        self.assertEqual(sorted(final_pending), ["p2-test"])
        self.assertIn(self.notifier._device_round_key(round_key, "d1"), final_sent)
        leftovers = [p.name for p in Path(self._tmp.name).iterdir() if p.suffix == ".tmp"]
        self.assertEqual(leftovers, [])
        # Reload: accepted round never resends.
        reloaded = PushNotifier(
            self.notifier.state,
            self.host_private,
            sender=self._capture,
            sent_path=Path(self._tmp.name) / "push-sent.json",
            pending_path=Path(self._tmp.name) / "push-pending.json",
        )
        sent_before = len(self.sent)
        reloaded.on_status_change(
            dict(session), sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), sent_before)

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

    def test_permanent_failure_parks_without_retry(self) -> None:
        """永久失败（bad_token）直接 parked：不记已发，后续扫描零重发。"""
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
        self._fail(self.sent[0][3], "bad_token")
        round_key = self.notifier._round_key(session, "completed")
        self.assertFalse(self.notifier._already_sent(round_key, "d1"))
        pending = list(self.notifier._pending.values())
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]["parked"])
        self.assertEqual(pending[0]["last_code"], "bad_token")
        for entry in self.notifier._pending.values():
            entry["ts"] -= 3600.0
        self.notifier.retry_due([dict(session)])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(len(self.notifier._pending), 1)

    def test_local_send_exception_keeps_retryable(self) -> None:
        """sender 抛错：待确认保留（attempts 计次），不记已发，退避后重发。"""
        calls: list[str] = []

        def flaky_sender(token: str, env: str, payload: bytes, push_id: str = "") -> None:
            calls.append(push_id)
            raise RuntimeError("relay down")

        self.notifier.set_sender(flaky_sender)
        session = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "done",
            "completion_id": "100:10:done:aaaa",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(calls), 1)
        round_key = self.notifier._round_key(session, "completed")
        self.assertFalse(self.notifier._already_sent(round_key, "d1"))
        pending = list(self.notifier._pending.values())
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["attempts"], 1)
        self.assertEqual(pending[0]["last_code"], "local_send")
        for entry in self.notifier._pending.values():
            entry["ts"] -= 3600.0
        self.notifier.retry_due([dict(session)])
        self.assertEqual(len(calls), 2)
        pending = list(self.notifier._pending.values())
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["attempts"], 2)
        self.assertFalse(self.notifier._already_sent(round_key, "d1"))

    def test_sync_success_receipt_marks_sent(self) -> None:
        """sender 内同步回执 ok：先落 pending，故回执能对上；恰好一帧。"""
        def inline_ok(token: str, env: str, payload: bytes, push_id: str = "") -> None:
            self.sent.append((token, env, payload, push_id))
            self.notifier.on_push_receipt(
                push_id,
                {"ok": True, "code": "ok", "status": 200, "reason": "", "apns_id": "x"},
            )

        self.notifier.set_sender(inline_ok)
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
        self.assertEqual(self.notifier._pending, {})
        round_key = self.notifier._round_key(session, "completed")
        self.assertTrue(self.notifier._already_sent(round_key, "d1"))
        for entry in self.notifier._pending.values():
            entry["ts"] -= 3600.0
        self.notifier.retry_due([dict(session)])
        self.assertEqual(len(self.sent), 1)

    def test_two_devices_no_receipt_exact_retry_each(self) -> None:
        """双设备无回执：超时后各恰好重发一帧（总数 4），attempts 均为 2。"""
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
        for entry in self.notifier._pending.values():
            entry["ts"] -= 3600.0
        self.notifier.retry_due([dict(session)])
        self.assertEqual(len(self.sent), 4)
        attempts = sorted(e["attempts"] for e in self.notifier._pending.values())
        self.assertEqual(attempts, [2, 2])
        self.assertEqual(len(self.notifier._pending), 2)
        # d1 接受后：下次只重发 d2（总数 5），d1 永不重发。
        d1_new = [pid for tok, _, _, pid in self.sent if tok == "a" * 64][-1]
        self._ok(d1_new)
        for entry in self.notifier._pending.values():
            entry["ts"] -= 3600.0
        self.notifier.retry_due([dict(session)])
        self.assertEqual(len(self.sent), 5)
        self.assertEqual(self.sent[4][0], "b" * 64)
        round_key = self.notifier._round_key(session, "completed")
        self.assertTrue(self.notifier._already_sent(round_key, "d1"))

    def test_new_round_within_throttle_sends(self) -> None:
        """节流窗内新 completion 必须发出；旧轮重放抑制（精确计数）。"""
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
        self.assertEqual(len(self.sent), 1)
        # 节流窗内新一轮：第二帧必须出。
        self.notifier.on_status_change(
            second, sesskit_titles.STATUS_DONE, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 2)
        # 旧轮重放（同 completion）：抑制，仍为 2。
        self.notifier.on_status_change(
            first, sesskit_titles.STATUS_DONE, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 2)

    def test_restart_retry_cap_parks(self) -> None:
        """重启后 attempts 已达上限：park，零新帧，跨重启仍有效。"""
        session = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "done",
            "completion_id": "100:10:done:aaaa",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        for entry in self.notifier._pending.values():
            entry["attempts"] = 5
            entry["ts"] -= 3600.0
        with self.notifier._lock:
            self.notifier._persist_pending_locked()
        rebooted = PushNotifier(
            self.notifier.state,
            self.host_private,
            sender=self._capture,
            sent_path=Path(self._tmp.name) / "push-sent.json",
            pending_path=Path(self._tmp.name) / "push-pending.json",
        )
        rebooted.retry_due([dict(session)])
        self.assertEqual(len(self.sent), 1)
        pending = list(rebooted._pending.values())
        self.assertEqual(len(pending), 1)
        self.assertTrue(pending[0]["parked"])

    def test_superseded_round_expires(self) -> None:
        """新一轮出现后：旧待确认丢弃，retry 零新帧。"""
        old = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "round one",
            "completion_id": "100:10:done:aaaa",
        }
        new = dict(old, completion_id="200:20:done:bbbb", last_agent="round two")
        self.notifier.on_status_change(
            old, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.sent), 1)
        for entry in self.notifier._pending.values():
            entry["ts"] -= 3600.0
        self.notifier.retry_due([new])
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.notifier._pending, {})


@unittest.skipUnless(_HAS_CRYPTO, _SKIP)
class ProductionAssemblyRetryTests(unittest.TestCase):
    """真实生产组装回归：daemon 注册绑定方法 hook，扫描驱动必须经 __self__ 生效。"""

    def setUp(self) -> None:
        from corral.remote.daemon import RemoteDaemon

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._env = mock.patch.dict(
            os.environ,
            {
                "CORRAL_STATE_DIR": self._tmp.name,
                "CORRAL_CACHE_DIR": self._tmp.name,
            },
            clear=False,
        )
        self._env.start()
        self.addCleanup(self._env.stop)
        self.device_private = crypto.generate_private_key_bytes()
        self.host_private = crypto.generate_private_key_bytes()
        state = RemoteState(
            host_id="host",
            host_name="Mac",
            relay_url="wss://example.invalid",
            relay_enabled=True,
            local_enabled=False,
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
        self.daemon = RemoteDaemon(state)

    def test_bound_hook_retry_resolves_and_retries(self) -> None:
        hub = self.daemon.hub
        push = self.daemon.push
        # 生产接线：绑定方法，没有 retry_due 属性——旧驱动在此永远取不到。
        self.assertIs(hub._status_hook.__self__, push)
        self.assertIsNone(getattr(hub._status_hook, "retry_due", None))
        path = Path(self._tmp.name) / "pi.jsonl"
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
        session["mtime"] = time.time()
        hub._detect_status_changes()
        # sender 为真实 RelayClient（无 loop，帧丢弃但无异常）：恰好一条待确认。
        self.assertEqual(len(push._pending), 1)
        first_id = next(iter(push._pending))
        self.assertEqual(push._sent_rounds, {})
        # 无跃迁再次扫描：只有经 __self__ 解析的重试驱动能动作；恰好替换一条。
        for entry in push._pending.values():
            entry["ts"] -= 3600.0
        hub._detect_status_changes()
        self.assertEqual(len(push._pending), 1)
        second_id = next(iter(push._pending))
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(push._pending[second_id]["attempts"], 2)
        self.assertEqual(push._sent_rounds, {})


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
