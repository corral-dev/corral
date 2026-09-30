"""推送回执链路：协议解析、中继客户端分发、旧 sender 兼容。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from corral.remote import crypto, protocol
from corral.remote.config import PairedDevice, RemoteState
from corral.remote.push import PushNotifier
from corral.remote.transport.relay import RelayClient

_HAS_CRYPTO = crypto.available()
_SKIP = "未安装 remote 附加组件（pip install '.[remote]'）"


class FakeRelay:
    """最小中继客户端替身：只实现回执注册与 4 参数 send_push。"""

    def __init__(self) -> None:
        self.handler = None
        self.frames: list[tuple[str, str, bytes, str]] = []

    def set_receipt_handler(self, handler) -> None:
        self.handler = handler

    def send_push(self, token: str, env: str, payload: bytes, push_id: str = "") -> None:
        self.frames.append((token, env, payload, push_id))


def _receipt_frame(payload: dict) -> bytes:
    return protocol.encode_frame(
        protocol.FRAME_PUSH_RECEIPT,
        protocol.ZERO_CHANNEL,
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )


@unittest.skipUnless(_HAS_CRYPTO, _SKIP)
class PushReceiptDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.device_private = crypto.generate_private_key_bytes()
        self.host_private = crypto.generate_private_key_bytes()
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
        self.relay = FakeRelay()
        self.notifier = PushNotifier(
            state,
            self.host_private,
            sender=self.relay.send_push,
            sent_path=Path(self._tmp.name) / "push-sent.json",
            pending_path=Path(self._tmp.name) / "push-pending.json",
        )

    def test_set_sender_auto_registers_receipt_handler(self) -> None:
        handler = self.relay.handler
        self.assertIs(handler.__self__, self.notifier)
        self.assertEqual(handler.__func__, PushNotifier.on_push_receipt)

    def test_receipt_frame_marks_sent_per_device(self) -> None:
        from sesskit import titles as sesskit_titles

        session = {
            "key": "pi:s1",
            "title": "probe",
            "last_agent": "done",
            "completion_id": "100:10:done:aaaa",
        }
        self.notifier.on_status_change(
            session, sesskit_titles.STATUS_PENDING, sesskit_titles.STATUS_DONE
        )
        self.assertEqual(len(self.relay.frames), 1)
        push_id = self.relay.frames[0][3]
        self.assertTrue(push_id)
        # 中继回执经 RelayClient._on_frame 分发（无 socket，不走网络）。
        client = RelayClient.__new__(RelayClient)
        client._receipt_handler = self.notifier.on_push_receipt
        client._on_frame(_receipt_frame(
            {"id": push_id, "ok": True, "code": "ok", "status": 200,
             "reason": "", "apns_id": "apns-7"}
        ))
        round_key = self.notifier._round_key(session, "completed")
        self.assertTrue(self.notifier._already_sent(round_key, "d1"))

    def test_unknown_receipt_id_is_ignored(self) -> None:
        client = RelayClient.__new__(RelayClient)
        received = []
        client._receipt_handler = lambda pid, r: received.append((pid, r))
        client._on_frame(_receipt_frame({"id": "nope", "ok": True, "code": "ok"}))
        self.assertEqual(len(received), 1)
        # 推送层对未知 id 只计数。
        self.notifier.on_push_receipt("nope", {"ok": True, "code": "ok"})
        self.assertEqual(self.notifier._sent_rounds, {})

    def test_malformed_receipt_frame_does_not_raise(self) -> None:
        client = RelayClient.__new__(RelayClient)
        client._receipt_handler = lambda pid, r: (_ for _ in ()).throw(AssertionError("must not fire"))
        client._on_frame(
            protocol.encode_frame(protocol.FRAME_PUSH_RECEIPT, protocol.ZERO_CHANNEL, b"{bad")
        )

    def test_legacy_three_arg_sender_still_queues(self) -> None:
        from sesskit import titles as sesskit_titles

        calls: list[tuple] = []

        def legacy_sender(token: str, env: str, payload: bytes) -> None:
            calls.append((token, env, payload))

        notifier = PushNotifier(
            self.notifier.state,
            self.host_private,
            sender=legacy_sender,
            sent_path=Path(self._tmp.name) / "s2.json",
            pending_path=Path(self._tmp.name) / "p2.json",
        )
        notifier.on_status_change(
            {"key": "pi:s9", "title": "t", "last_agent": "x",
             "completion_id": "1:1:done:z"},
            sesskit_titles.STATUS_PENDING,
            sesskit_titles.STATUS_DONE,
        )
        self.assertEqual(len(calls), 1)
        # 无回执：记 queued + 待确认，不记已发。
        self.assertEqual(len(notifier._pending), 1)
        self.assertEqual(notifier._sent_rounds, {})


class ParsePushReceiptTests(unittest.TestCase):
    def test_unknown_code_falls_back_to_internal(self) -> None:
        parsed = protocol.parse_push_receipt(
            json.dumps({"id": "a", "ok": False, "code": "weird"}).encode()
        )
        self.assertEqual(parsed["code"], "internal")
        self.assertFalse(parsed["ok"])

    def test_bad_json_raises(self) -> None:
        with self.assertRaises(protocol.ProtocolError):
            protocol.parse_push_receipt(b"not json")


if __name__ == "__main__":
    unittest.main()
