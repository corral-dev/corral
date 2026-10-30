"""Handoff source notice: one English sentence, best-effort, never blocking."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from corral.handoff_notice import HANDOFF_SOURCE_NOTICE, notify_handoff_source


class NoticeTextTests(unittest.TestCase):
    def test_single_english_sentence_with_ack_word(self) -> None:
        self.assertTrue(HANDOFF_SOURCE_NOTICE.strip().endswith("received."))
        self.assertNotRegex(HANDOFF_SOURCE_NOTICE, r"[\u4e00-\u9fff]")
        terminators = sum(HANDOFF_SOURCE_NOTICE.count(mark) for mark in (".", "!", "?"))
        self.assertEqual(terminators, 1)
        lowered = HANDOFF_SOURCE_NOTICE.lower()
        self.assertIn("handed off", lowered)
        self.assertIn("notification", lowered)
        self.assertIn("stop all work immediately", lowered)
        self.assertIn("received", lowered)


class NotifyTests(unittest.TestCase):
    def _live(self, name="corral-claude-abc123"):
        return {"source": "claude", "keepalive_name": name}

    def test_no_keepalive_name_skips_quietly(self) -> None:
        with (
            mock.patch("corral.liveness.is_alive") as alive,
            mock.patch("corral.embed.paste_detailed") as paste,
        ):
            self.assertFalse(notify_handoff_source({"source": "claude"}))
            alive.assert_not_called()
            paste.assert_not_called()

    def test_shell_source_never_notified(self) -> None:
        with (
            mock.patch("corral.liveness.is_alive") as alive,
            mock.patch("corral.embed.paste_detailed") as paste,
        ):
            self.assertFalse(notify_handoff_source({"source": "shell", "keepalive_name": "corral-shell-x"}))
            alive.assert_not_called()
            paste.assert_not_called()

    def test_dead_pane_skips_injection(self) -> None:
        with (
            mock.patch("corral.liveness.is_alive", return_value=False) as alive,
            mock.patch("corral.embed.paste_detailed") as paste,
            mock.patch("corral.embed.send_key_detailed") as send_key,
        ):
            self.assertFalse(notify_handoff_source(self._live(), SimpleNamespace(interrupt_keys=("Escape",))))
            alive.assert_called_once()
            paste.assert_not_called()
            send_key.assert_not_called()

    def test_live_pane_interrupt_then_paste_then_enter(self) -> None:
        order: list[str] = []
        with (
            mock.patch("corral.liveness.is_alive", return_value=True),
            mock.patch("corral.embed.paste_detailed", return_value=SimpleNamespace(ok=True)) as paste,
            mock.patch("corral.embed.send_key_detailed", return_value=SimpleNamespace(ok=True)) as send_key,
        ):
            send_key.side_effect = lambda name, *keys: order.append(f"key:{'+'.join(keys)}") or SimpleNamespace(ok=True)
            paste.side_effect = lambda name, text: order.append(f"paste:{text}") or SimpleNamespace(ok=True)
            self.assertTrue(notify_handoff_source(self._live(), SimpleNamespace(interrupt_keys=("Escape",))))
            self.assertEqual(order[0], "key:Escape")
            self.assertEqual(order[1], f"paste:{HANDOFF_SOURCE_NOTICE}")
            self.assertEqual(order[2], "key:Enter")
            paste.assert_called_once()

    def test_paste_failure_sends_no_enter(self) -> None:
        with (
            mock.patch("corral.liveness.is_alive", return_value=True),
            mock.patch("corral.embed.paste_detailed", return_value=SimpleNamespace(ok=False)),
            mock.patch("corral.embed.send_key_detailed") as send_key,
        ):
            self.assertFalse(notify_handoff_source(self._live()))
            send_key.assert_not_called()

    def test_injection_exception_never_raises(self) -> None:
        with mock.patch("corral.liveness.is_alive", side_effect=RuntimeError("boom")):
            self.assertFalse(notify_handoff_source(self._live()))


if __name__ == "__main__":
    unittest.main()
