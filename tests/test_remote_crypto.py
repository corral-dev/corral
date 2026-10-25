"""corral.remote 的加密与协议层。

这一层出错的后果不是「功能不好用」而是「别人能看到你的代码」，所以用例重心放在
反面路径：改一个字节要能被发现、重放与乱序要被拒、冒充开发机要解不开。
"""

from __future__ import annotations

import unittest

from corral.remote import crypto, protocol

_HAS_CRYPTO = crypto.available()
_SKIP = "未安装 remote 附加组件（pip install '.[remote]'）"


def _handshake_pair():
    """跑一遍完整握手，返回（开发机通道, 设备通道, 设备握手态）。"""
    host = crypto.Handshake(crypto.generate_private_key_bytes())
    device = crypto.Handshake(crypto.generate_private_key_bytes())
    host_channel = host.accept(device.static_public, device.ephemeral_public)
    device_channel = device.complete(host.static_public, host.ephemeral_public)
    return host_channel, device_channel, host, device


@unittest.skipUnless(_HAS_CRYPTO, _SKIP)
class SecureChannelTests(unittest.TestCase):
    def test_round_trip_both_directions(self):
        host, device, _, _ = _handshake_pair()
        self.assertEqual(device.decrypt(host.encrypt("中文".encode())), "中文".encode())
        self.assertEqual(host.decrypt(device.encrypt(b"reply")), b"reply")

    def test_each_side_sees_the_peer_identity(self):
        host_channel, device_channel, host, device = _handshake_pair()
        self.assertEqual(host_channel.peer_static_public, device.static_public)
        self.assertEqual(device_channel.peer_static_public, host.static_public)

    def test_tampered_ciphertext_is_rejected(self):
        host, device, _, _ = _handshake_pair()
        frame = bytearray(host.encrypt(b"payload"))
        frame[-1] ^= 0x01
        with self.assertRaises(crypto.ChannelError):
            device.decrypt(bytes(frame))

    def test_replayed_frame_is_rejected(self):
        host, device, _, _ = _handshake_pair()
        frame = host.encrypt(b"once")
        self.assertEqual(device.decrypt(frame), b"once")
        with self.assertRaises(crypto.ChannelError):
            device.decrypt(frame)

    def test_out_of_order_frames_are_rejected(self):
        """计数器跳号说明有人在重排，宁可断开也不要接着解。"""
        host, device, _, _ = _handshake_pair()
        first = host.encrypt(b"1")
        second = host.encrypt(b"2")
        with self.assertRaises(crypto.ChannelError):
            device.decrypt(second)
        self.assertEqual(device.decrypt(first), b"1")

    def test_another_device_cannot_read_the_conversation(self):
        host, _, _, _ = _handshake_pair()
        _, eavesdropper, _, _ = _handshake_pair()
        with self.assertRaises(crypto.ChannelError):
            eavesdropper.decrypt(host.encrypt(b"secret"))

    def test_impersonating_the_host_breaks_the_channel(self):
        """中继冒充开发机时设备侧必须解不开——这是零知识转发的底线。"""
        host = crypto.Handshake(crypto.generate_private_key_bytes())
        device = crypto.Handshake(crypto.generate_private_key_bytes())
        host_channel = host.accept(device.static_public, device.ephemeral_public)
        impostor = crypto.public_key_bytes(crypto.generate_private_key_bytes())
        device_channel = device.complete(impostor, host.ephemeral_public)
        with self.assertRaises(crypto.ChannelError):
            device_channel.decrypt(host_channel.encrypt(b"secret"))

    def test_malformed_public_key_is_rejected(self):
        host = crypto.Handshake(crypto.generate_private_key_bytes())
        with self.assertRaises(crypto.ChannelError):
            host.accept(b"too short", b"\x00" * 32)

    def test_short_frame_is_rejected(self):
        _, device, _, _ = _handshake_pair()
        with self.assertRaises(crypto.ChannelError):
            device.decrypt(b"\x00" * 8)


@unittest.skipUnless(_HAS_CRYPTO, _SKIP)
class PushSealTests(unittest.TestCase):
    def test_push_payload_round_trip(self):
        device_private = crypto.generate_private_key_bytes()
        host_private = crypto.generate_private_key_bytes()
        plaintext = "等你回答".encode()
        sealed = crypto.seal_for_device(
            host_private, crypto.public_key_bytes(device_private), plaintext
        )
        opened = crypto.open_from_host(
            device_private, crypto.public_key_bytes(host_private), sealed
        )
        self.assertEqual(opened, plaintext)

    def test_each_push_uses_fresh_salt(self):
        """同样的内容两次封装必须不同，否则中继能从密文长度以外看出重复。"""
        host_private = crypto.generate_private_key_bytes()
        target = crypto.public_key_bytes(crypto.generate_private_key_bytes())
        first = crypto.seal_for_device(host_private, target, b"same")
        second = crypto.seal_for_device(host_private, target, b"same")
        self.assertNotEqual(first, second)

    def test_another_device_cannot_open_the_push(self):
        host_private = crypto.generate_private_key_bytes()
        target = crypto.public_key_bytes(crypto.generate_private_key_bytes())
        sealed = crypto.seal_for_device(host_private, target, b"hi")
        with self.assertRaises(crypto.ChannelError):
            crypto.open_from_host(
                crypto.generate_private_key_bytes(),
                crypto.public_key_bytes(host_private),
                sealed,
            )


@unittest.skipUnless(_HAS_CRYPTO, _SKIP)
class PairingCodeTests(unittest.TestCase):
    def test_codes_avoid_lookalike_characters_and_do_not_repeat(self):
        codes = {crypto.new_pairing_code() for _ in range(200)}
        self.assertEqual(len(codes), 200)
        for code in codes:
            self.assertFalse(set(code) & set("OI01"), code)

    def test_comparison_tolerates_manual_typing(self):
        self.assertTrue(crypto.codes_equal("abcd-efgh", "ABCDEFGH"))
        self.assertTrue(crypto.codes_equal("ABCD EFGH", "abcdefgh"))
        self.assertFalse(crypto.codes_equal("abcdefgh", "abcdefgi"))

    def test_empty_code_never_matches(self):
        """没生成配对码时不能被一个空码蒙混过关。"""
        self.assertFalse(crypto.codes_equal("", ""))
        self.assertFalse(crypto.codes_equal("---", ""))


class FrameTests(unittest.TestCase):
    def test_frame_round_trip(self):
        channel = bytes(range(protocol.CHANNEL_ID_LEN))
        raw = protocol.encode_frame(protocol.FRAME_DATA, channel, b"payload")
        self.assertEqual(raw[0], protocol.FRAME_VERSION)
        kind, got_channel, payload = protocol.decode_frame(raw)
        self.assertEqual(kind, protocol.FRAME_DATA)
        self.assertEqual(got_channel, channel)
        self.assertEqual(payload, b"payload")

    def test_v1_shaped_frame_is_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode_frame(bytes([protocol.FRAME_DATA]) + bytes(16))

    def test_bad_channel_length_is_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.encode_frame(protocol.FRAME_DATA, b"short", b"")

    def test_truncated_frame_is_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.decode_frame(b"\x01\x02")

    def test_json_messages_round_trip(self):
        message = protocol.loads(
            protocol.dumps(protocol.request(7, protocol.M_SESSIONS_LIST, {"limit": 3}))
        )
        self.assertEqual(message["id"], 7)
        self.assertEqual(message["m"], protocol.M_SESSIONS_LIST)
        self.assertEqual(message["p"], {"limit": 3})

    def test_compressed_messages_round_trip(self):
        message = protocol.request(7, protocol.M_SESSION_WATCH, {"key": "codex:abc"})
        message["payload"] = "连续助手输出" * 2000
        packed = protocol.pack(message, compress=True)
        self.assertLess(len(packed), len(protocol.dumps(message)))
        self.assertEqual(packed[:2], b"CR")
        self.assertEqual(packed[3], 1)
        # Apple Compression 的 COMPRESSION_ZLIB 实际输出 raw DEFLATE，不是 0x78 zlib 头。
        self.assertNotEqual(packed[8], 0x78)
        self.assertEqual(protocol.unpack(packed), message)

    def test_unpack_accepts_zlib_wrapped_payload(self):
        import zlib

        message = protocol.request(7, protocol.M_SESSIONS_LIST)
        raw = protocol.dumps({**message, "payload": "x" * 2000})
        body = zlib.compress(raw, level=5)
        packed = b"CR" + bytes([1, 1]) + len(raw).to_bytes(4, "big") + body
        self.assertEqual(protocol.unpack(packed)["m"], protocol.M_SESSIONS_LIST)

    def test_uncompressed_messages_remain_backward_compatible(self):
        message = protocol.request(7, protocol.M_SESSIONS_LIST)
        raw = protocol.dumps(message)
        self.assertEqual(protocol.unpack(raw), message)

    def test_compressed_payload_rejects_trailing_data(self):
        message = protocol.request(7, protocol.M_SESSIONS_LIST)
        packed = protocol.pack({**message, "payload": "x" * 2000}, compress=True)
        with self.assertRaises(protocol.ProtocolError):
            protocol.unpack(packed + b"tail")

    def test_error_carries_code_and_message(self):
        message = protocol.loads(
            protocol.dumps(protocol.error(1, protocol.E_UNAUTHORIZED, "没配对"))
        )
        self.assertFalse(message["ok"])
        self.assertEqual(message["e"]["code"], protocol.E_UNAUTHORIZED)
        self.assertEqual(message["e"]["message"], "没配对")

    def test_non_object_message_is_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.loads(b"[1,2,3]")

    def test_invalid_json_is_rejected(self):
        with self.assertRaises(protocol.ProtocolError):
            protocol.loads(b"\xff\xfe not json")


@unittest.skipUnless(_HAS_CRYPTO, _SKIP)
class HostIdentityTests(unittest.TestCase):
    def test_routing_id_matches_spec_vector(self):
        pub = bytes(range(32))
        self.assertEqual(crypto.routing_id_from_x25519(pub), "xp47bhr5gnbtbbmjviugjxbnxm")

    def test_host_assertion_round_trip_shape(self):
        key = crypto.generate_host_key_bytes()
        nonce = b"\x01" * 16
        header = crypto.sign_host_assertion(key, "abc", 1_700_000_000, nonce)
        parts = header.split(".")
        self.assertEqual(parts[0], "v2")
        self.assertEqual(parts[1], "abc")
        self.assertEqual(parts[2], "1700000000")
        self.assertEqual(len(parts), 5)

    def test_host_lane_attach_assertion_shape(self):
        key = crypto.generate_host_key_bytes()
        nonce = b"\x02" * 16
        header = crypto.sign_host_lane_attach(
            key, "abc", "node-a", 9, "bulk", 1_700_000_000, nonce
        )
        parts = header.split(".")
        self.assertEqual(parts[0], "v2attach")
        self.assertEqual(parts[1], "abc")
        self.assertEqual(parts[2], "node-a")
        self.assertEqual(parts[3], "9")
        self.assertEqual(parts[4], "bulk")
        self.assertEqual(parts[5], "1700000000")
        self.assertEqual(len(parts), 8)


@unittest.skipUnless(_HAS_CRYPTO, _SKIP)
class HostChannelBackpressureTests(unittest.TestCase):
    """数据面队列满不得 close 控制面；控制面被灌爆仍按今天的行为断开自己。"""

    def setUp(self) -> None:
        import os
        import tempfile

        from corral.remote.service import Connection, RemoteService
        from corral.remote.transport.channel import HostChannel

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old_cache = os.environ.get("CORRAL_CACHE_DIR")
        os.environ["CORRAL_CACHE_DIR"] = self._tmp.name
        self.addCleanup(self._restore_cache)
        self._Connection = Connection
        self._HostChannel = HostChannel
        self.service = RemoteService()
        self.closed: list[str] = []

    def _restore_cache(self) -> None:
        import os

        if self._old_cache is None:
            os.environ.pop("CORRAL_CACHE_DIR", None)
        else:
            os.environ["CORRAL_CACHE_DIR"] = self._old_cache

    def _channel(self, *, queue_size: int = 1):
        channel = self._HostChannel(
            self.service,
            crypto.generate_private_key_bytes(),
            b"\x00" * 16,
            lambda _t, _p: None,
            queue_size=queue_size,
        )
        channel._ensure_worker = lambda: None  # 不消费队列，才能测满
        return channel

    def test_data_plane_overflow_does_not_close_control(self):
        control = self._Connection("aa" * 32, lambda _m: None)
        self.service.attach(control)
        control.close_hook = lambda: self.closed.append("control")
        channel = self._channel()
        channel._owns_logical_connection = False
        channel._confirmed = True
        channel._connection = control
        channel._queue.put_nowait((protocol.FRAME_DATA, b"fill"))
        channel.submit(protocol.FRAME_DATA, b"overflow")
        # The overflowing data plane resets itself explicitly (close/rebind);
        # the control connection and its close hook stay untouched.
        self.assertTrue(channel._closed)
        self.assertFalse(control.closed)
        self.assertIn(control, self.service._connections)
        self.assertEqual(self.closed, [])

    def test_control_plane_overflow_still_closes_itself(self):
        control = self._Connection("aa" * 32, lambda _m: None)
        self.service.attach(control)
        channel = self._channel()
        channel._owns_logical_connection = True
        channel._confirmed = True
        channel._connection = control
        control.close_hook = channel.close
        channel._queue.put_nowait((protocol.FRAME_DATA, b"fill"))
        channel.submit(protocol.FRAME_DATA, b"overflow")
        self.assertTrue(channel._closed)
        self.assertTrue(control.closed)

    def test_bad_data_bind_closes_only_data_channel(self):
        from corral.remote.crypto import ChannelError

        control = self._Connection("aa" * 32, lambda _m: None)
        self.service.attach(control)
        control.close_hook = lambda: self.closed.append("control")
        channel = self._channel(queue_size=8)
        channel._pending_device_key = control.device_public_key
        with self.assertRaises(ChannelError):
            channel._confirm_and_attach(
                protocol.request(
                    1, protocol.M_HELLO, {"plane": "data", "bind": "nope", "name": "iPhone"}
                )
            )
        channel.close()
        self.assertFalse(control.closed)
        self.assertEqual(self.closed, [])
        self.assertTrue(channel._closed)


if __name__ == "__main__":
    unittest.main()
