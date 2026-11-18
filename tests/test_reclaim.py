"""Silent reclaim of inactive hosted sessions.

Pure decision tests use plain data. The one real-tmux test runs on a private
socket with the cache directory redirected to a temp dir, so it can never see or
touch a developer's live ``corral-keepalive`` sessions.
"""

from __future__ import annotations

import contextlib
import io
import os
import pty
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sesskit.titles import STATUS_ABORTED, STATUS_DONE, STATUS_NONE, STATUS_PENDING

from corral import embed, keepalive, reclaim, retirement, split_layout
from corral.attention import AttentionEvidence, AttentionStore
from corral.reclaim import Context, History, Hosted

NOW = 10_000_000.0
HOUR = 3600.0
MIN = 60.0


def host(name: str = "corral-claude-aaaa1111", **overrides) -> Hosted:
    fields = dict(
        name=name, runtime_id="claude", ident=name.rsplit("-", 1)[-1], socket="corral-keepalive",
        created_at=NOW - 4 * HOUR, session_activity=NOW - 3 * HOUR, window_activity=NOW - 3 * HOUR,
    )
    fields.update(overrides)
    return Hosted(**fields)


def done(**overrides) -> History:
    fields = dict(
        status_tag=STATUS_DONE, active_at=NOW - 3 * HOUR, runtime_id="claude", session_id="aaaa1111-full-id",
    )
    fields.update(overrides)
    return History(**fields)


def ctx(**overrides) -> Context:
    return Context(now=NOW, **overrides)


class EvaluateTests(unittest.TestCase):
    def test_inactive_finished_session_is_reclaimable(self) -> None:
        verdict = reclaim.evaluate(host(), [done()], ctx())
        self.assertTrue(verdict.reclaim)
        self.assertEqual(verdict.reason, "inactive")
        self.assertEqual(verdict.evidence["idle_min"], 180.0)
        self.assertEqual(verdict.evidence["status_tag"], STATUS_DONE)

    def test_aborted_outcome_counts_as_finished(self) -> None:
        verdict = reclaim.evaluate(host(), [done(status_tag=STATUS_ABORTED)], ctx())
        self.assertTrue(verdict.reclaim)

    def test_each_protection_holds_on_its_own(self) -> None:
        cases = {
            "history_unknown": (host(), [], ctx()),
            "caller_protected": (host(), [done(protect=True)], ctx()),
            "grace": (host(created_at=NOW - 10 * MIN), [done()], ctx()),
            "viewed": (host(), [done()], ctx(viewed={"corral-claude-aaaa1111"})),
            "attached_client": (host(foreign_clients=1), [done()], ctx()),
            "pinned": (host(), [done()], ctx(pinned_keys={"claude:aaaa1111-full-id"})),
            "busy": (host(), [done()], ctx(busy_pairs={("claude", "aaaa1111-full-id")})),
            "recent_activity": (host(session_activity=NOW - 45 * MIN), [done()], ctx()),
            "floor": (host(session_activity=NOW - 5 * MIN), [done()], ctx()),
        }
        for reason, (session, entries, context) in cases.items():
            with self.subTest(reason=reason):
                verdict = reclaim.evaluate(session, entries, context)
                self.assertFalse(verdict.reclaim)
                self.assertEqual(verdict.reason, reason)

    def test_busy_matches_by_hosted_ident_prefix_when_history_id_differs(self) -> None:
        # The busy table knows the full session id; the tmux name only carries 8 chars.
        entries = [done(session_id="other-id")]
        verdict = reclaim.evaluate(host(), entries, ctx(busy_pairs={("claude", "aaaa1111-0000-full")}))
        self.assertEqual(verdict.reason, "busy")

    def test_pinned_matches_the_ident_key_too(self) -> None:
        verdict = reclaim.evaluate(host(), [done(session_id="zzz")], ctx(pinned_keys={"claude:aaaa1111"}))
        self.assertEqual(verdict.reason, "pinned")

    def test_pending_and_unknown_history_block(self) -> None:
        cases = {
            "history_pending": done(status_tag=STATUS_PENDING),
            "history_unknown": done(status_tag=STATUS_NONE),
        }
        for reason, entry in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(reclaim.evaluate(host(), [entry], ctx()).reason, reason)
        self.assertEqual(reclaim.evaluate(host(), [done(active_at=None)], ctx()).reason, "history_unknown")

    def test_unknown_tmux_or_creation_time_blocks(self) -> None:
        card = History(status_tag=STATUS_PENDING, provisional=True, runtime_id="claude", session_id="aaaa1111")
        cases = (
            ({"session_activity": 0.0}, [done()], "tmux_unknown"),
            ({"window_activity": 0.0}, [card], "tmux_unknown"),  # placeholders have only output time
            ({"created_at": 0.0}, [done()], "grace"),
        )
        for overrides, entries, reason in cases:
            with self.subTest(overrides=overrides):
                self.assertEqual(reclaim.evaluate(host(**overrides), entries, ctx()).reason, reason)

    def test_history_backed_session_needs_old_input_and_old_history(self) -> None:
        self.assertEqual(
            reclaim.evaluate(host(session_activity=NOW - 30 * MIN), [done()], ctx()).reason, "recent_activity",
        )
        recent_history = done(active_at=NOW - 30 * MIN)
        self.assertEqual(reclaim.evaluate(host(), [recent_history], ctx()).reason, "recent_activity")

    def test_redrawing_idle_tui_does_not_block_a_finished_session(self) -> None:
        # Idle Codex/OpenCode panes keep printing; that must not keep a finished session alive.
        redrawing = host(window_activity=NOW - 10)
        verdict = reclaim.evaluate(redrawing, [done()], ctx())
        self.assertTrue(verdict.reclaim)
        self.assertFalse(verdict.evidence["window_gates"])
        self.assertEqual(verdict.evidence["window_idle_min"], 0.2)  # still audited
        self.assertEqual(verdict.evidence["idle_min"], 180.0)  # ...but never part of the idle clock
        with mock.patch.object(reclaim, "WINDOW_OUTPUT_GATES_HISTORY", True):
            strict = reclaim.evaluate(redrawing, [done()], ctx())
        # 10 s of output is below the 30-minute absolute floor, so the floor is
        # the binding reason now (D1); the point is that output gated it at all.
        self.assertEqual(strict.reason, "floor")
        self.assertTrue(strict.evidence["window_gates"])

    def test_placeholder_session_is_still_gated_on_terminal_output(self) -> None:
        card = History(status_tag=STATUS_PENDING, provisional=True, runtime_id="claude", session_id="aaaa1111")
        self.assertEqual(reclaim.evaluate(host(window_activity=NOW - 10), [card], ctx()).reason, "floor")
        self.assertTrue(reclaim.evaluate(host(), [card], ctx()).reclaim)

    def test_normal_threshold_is_60_minutes(self) -> None:
        just_under = host(window_activity=NOW - 59 * MIN, session_activity=NOW - 59 * MIN)
        exactly = host(window_activity=NOW - 60 * MIN, session_activity=NOW - 60 * MIN)
        entry = done(active_at=NOW - 90 * MIN)
        self.assertFalse(reclaim.evaluate(just_under, [entry], ctx()).reclaim)
        self.assertTrue(reclaim.evaluate(exactly, [entry], ctx()).reclaim)

    def test_pressure_keeps_60_minute_threshold_and_explicit_override_respects_floor(self) -> None:
        # 2026-11-18 ruling: pressure does not lower the 60-minute threshold.
        idle_35 = host(window_activity=NOW - 35 * MIN, session_activity=NOW - 35 * MIN)
        idle_65 = host(window_activity=NOW - 65 * MIN, session_activity=NOW - 65 * MIN)
        entry = done(active_at=NOW - 90 * MIN)
        self.assertFalse(reclaim.evaluate(idle_35, [entry], ctx(pressure=True)).reclaim)
        self.assertEqual(reclaim.evaluate(idle_35, [entry], ctx(pressure=True)).reason, "recent_activity")
        pressured = reclaim.evaluate(idle_65, [entry], ctx(pressure=True))
        self.assertTrue(pressured.reclaim)
        self.assertEqual(pressured.evidence["threshold_min"], 60.0)
        # Explicit env overrides still cannot break the 30-minute safety floor.
        idle_15 = host(window_activity=NOW - 15 * MIN, session_activity=NOW - 15 * MIN)
        custom = ctx(pressure=True, pressure_idle_minutes=10.0)
        self.assertEqual(reclaim.evaluate(idle_15, [entry], custom).reason, "floor")
        self.assertTrue(reclaim.evaluate(idle_35, [entry], custom).reclaim)

    def test_the_floor_is_not_a_product_setting(self) -> None:
        with mock.patch.dict(os.environ, {"CORRAL_RECLAIM_FLOOR_MINUTES": "1"}, clear=True):
            self.assertEqual(reclaim.policy_from_env(NOW).floor_minutes, 30.0)
        self.assertEqual(reclaim.Context(now=NOW).floor_minutes, reclaim.ABSOLUTE_IDLE_FLOOR_MINUTES)

    def test_pressure_never_overrides_a_protection(self) -> None:
        verdict = reclaim.evaluate(host(foreign_clients=2), [done()], ctx(pressure=True))
        self.assertEqual(verdict.reason, "attached_client")
        young = host(created_at=NOW - 5 * MIN)
        self.assertEqual(reclaim.evaluate(young, [done()], ctx(pressure=True)).reason, "grace")

    def test_provisional_card_with_no_history_is_reclaimable_when_idle(self) -> None:
        card = History(status_tag=STATUS_PENDING, provisional=True, runtime_id="claude", session_id="aaaa1111")
        verdict = reclaim.evaluate(host(), [card], ctx())
        self.assertTrue(verdict.reclaim)
        self.assertTrue(verdict.evidence["provisional"])
        self.assertNotIn("history_idle_min", verdict.evidence)

    def test_provisional_card_keeps_every_other_protection(self) -> None:
        card = History(status_tag=STATUS_PENDING, provisional=True, runtime_id="claude", session_id="aaaa1111")
        self.assertEqual(reclaim.evaluate(host(window_activity=NOW - MIN), [card], ctx()).reason, "floor")
        self.assertEqual(reclaim.evaluate(host(created_at=NOW - MIN), [card], ctx()).reason, "grace")
        self.assertEqual(
            reclaim.evaluate(host(), [card], ctx(busy_pairs={("claude", "aaaa1111")})).reason, "busy",
        )

    def test_real_history_outranks_a_leftover_provisional_card(self) -> None:
        card = History(status_tag=STATUS_PENDING, provisional=True)
        pending_real = done(status_tag=STATUS_PENDING)
        self.assertEqual(reclaim.evaluate(host(), [card, pending_real], ctx()).reason, "history_pending")


class DecideTests(unittest.TestCase):
    def test_normal_keeps_eight_newest_parked_and_retires_all_excess_without_cap(self) -> None:
        hosts, history = [], {}
        for index, idle_hours in enumerate((2, 12, 5, 9, 4, 11, 3, 10, 6, 8, 7, 13)):
            name = f"corral-claude-aaaa{index:04d}"
            when = NOW - idle_hours * HOUR
            hosts.append(host(name, window_activity=when, session_activity=when))
            history[name] = [done(active_at=when, session_id=f"s{index}")]
        picked = reclaim.decide(hosts, history, ctx())
        # 12 parked -> keep newest 8 (idle 2..9 h), retire 4 oldest (13, 12, 11, 10 h) in one pass.
        self.assertEqual([v.name for v in picked], [
            "corral-claude-aaaa0011",
            "corral-claude-aaaa0001",
            "corral-claude-aaaa0005",
            "corral-claude-aaaa0007",
        ])
        # Under memory pressure, keep 0 resident and retire all 12 in one pass.
        pressured = reclaim.decide(hosts, history, ctx(pressure=True))
        self.assertEqual(len(pressured), 12)

    def test_resident_budget_is_configurable_and_counts_all_parked_sessions(self) -> None:
        hosts = [host(f"corral-claude-bbbb{i:04d}", session_activity=NOW - (i + 2) * HOUR) for i in range(4)]
        history = {
            h.name: [done(active_at=NOW - (i + 2) * HOUR, session_id=h.name)]
            for i, h in enumerate(hosts)
        }
        self.assertEqual(reclaim.decide(hosts, history, ctx()), [])
        self.assertEqual(len(reclaim.decide(hosts, history, ctx(resident_budget=2))), 2)
        self.assertEqual(len(reclaim.decide(hosts, history, ctx(resident_budget=0))), 4)
        # Younger (< 60 min) parked sessions still count toward the 8 resident slots.
        young_hosts = [host(f"corral-claude-yyyy{i:04d}", session_activity=NOW - 40 * MIN) for i in range(6)]
        for h in young_hosts:
            history[h.name] = [done(active_at=NOW - 40 * MIN, session_id=h.name)]
        # 6 young + 4 old = 10 parked -> excess = 2 -> only 2 oldest of the 4 eligible die.
        picked = reclaim.decide(young_hosts + hosts, history, ctx())
        self.assertEqual([v.name for v in picked], ["corral-claude-bbbb0003", "corral-claude-bbbb0002"])

    def test_hosted_session_missing_from_the_caller_list_is_left_alone(self) -> None:
        self.assertEqual(reclaim.decide([host()], {}, ctx()), [])


class ApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self.temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _verdicts(self, count: int, idle_hours: float = 3) -> list[reclaim.Verdict]:
        when = NOW - idle_hours * HOUR
        return [
            reclaim.evaluate(
                host(f"corral-claude-cccc{i:04d}", window_activity=when, session_activity=when),
                [done(active_at=when, session_id=f"c{i}", cwd="/tmp", history_path=f"/tmp/c{i}.jsonl")],
                ctx(),
            )
            for i in range(count)
        ]

    def _apply(self, verdicts, context, **overrides):
        """Apply with the retirement side effects injected: no real cache, no kill."""
        kwargs = dict(
            kill=lambda host: True,
            after_kill=lambda name: None,
            record=lambda record: True,
            identity=lambda name, socket: retirement.PaneIdentity(pane_pid=1, server_pid=2),
            resume_guard=lambda *a, **k: None,
        )
        kwargs.update(overrides)
        return reclaim.apply(verdicts, context, **kwargs)

    def test_audit_event_is_written_before_each_kill_with_evidence(self) -> None:
        order: list[str] = []
        with mock.patch.object(reclaim.observe, "event", side_effect=lambda *a, **k: order.append(f"event:{a[0]}")):
            reclaimed = self._apply(
                self._verdicts(1), ctx(), kill=lambda h: order.append("kill") or True,
            )
        self.assertEqual(reclaimed, ["corral-claude-cccc0000"])
        self.assertEqual(order, ["event:reclaim", "kill"])

    def test_audit_event_carries_the_evidence(self) -> None:
        events: list[tuple] = []
        with mock.patch.object(reclaim.observe, "event", side_effect=lambda *a, **k: events.append((a, k))):
            self._apply(self._verdicts(1), ctx())
        (args, fields), = events
        self.assertEqual(args, ("reclaim",))
        self.assertEqual(fields["session"], "corral-claude-cccc0000")
        for key in ("reason", "state", "runtime", "status_tag", "idle_min", "window_idle_min",
                    "session_idle_min", "history_idle_min", "threshold_min", "effective_threshold_min",
                    "pressure", "created_age_min", "provisional"):
            self.assertIn(key, fields)

    def test_failed_kill_is_reported_and_not_counted(self) -> None:
        names: list[str] = []
        with mock.patch.object(reclaim.observe, "event", side_effect=lambda *a, **k: names.append(a[0])):
            reclaimed = self._apply(self._verdicts(1), ctx(), kill=lambda h: False)
        self.assertEqual(reclaimed, [])
        self.assertEqual(names, ["reclaim", "reclaim_failed"])

    def test_pass_stops_when_pressure_clears_and_spares_resident_budget(self) -> None:
        # 10 parked sessions idle >= 60 min; under pressure all 10 are queued,
        # then pressure clears after the 1st kill, so the pass stops once 8 remain.
        verdicts = self._verdicts(10, idle_hours=2)
        killed: list[str] = []
        report = reclaim.PassReport(at=NOW, parked=10)
        with mock.patch.object(reclaim.observe, "event"):
            reclaimed = self._apply(
                verdicts, ctx(pressure=True),
                kill=lambda h: killed.append(h.name) or True,
                pressure_fn=lambda: False,
                report=report,
            )
        self.assertEqual(len(reclaimed), 2)
        self.assertEqual(len(killed), 2)

        # Also respects custom pressure_idle_minutes clearing mid-pass.
        custom = ctx(pressure=True, idle_minutes=60, pressure_idle_minutes=10)
        young = [
            reclaim.evaluate(
                host(f"corral-claude-dddd{i:04d}", window_activity=NOW - 35 * MIN, session_activity=NOW - 35 * MIN),
                [done(active_at=NOW - 35 * MIN, session_id=f"d{i}")], custom,
            )
            for i in range(2)
        ]
        old = reclaim.evaluate(
            host("corral-claude-dddd0009"), [done(session_id="d9")], custom,
        )
        killed_custom: list[str] = []
        with mock.patch.object(reclaim.observe, "event"):
            reclaimed_custom = self._apply(
                [old, *young], custom,
                kill=lambda h: killed_custom.append(h.name) or True,
                pressure_fn=lambda: False,
            )
        self.assertEqual(reclaimed_custom, ["corral-claude-dddd0009"])
        self.assertEqual(killed_custom, ["corral-claude-dddd0009"])

    def test_pressure_that_persists_keeps_going(self) -> None:
        with mock.patch.object(reclaim.observe, "event"):
            reclaimed = self._apply(
                self._verdicts(10, idle_hours=2), ctx(pressure=True),
                pressure_fn=lambda: True,
                report=reclaim.PassReport(at=NOW, parked=10),
            )
        self.assertEqual(len(reclaimed), 10)


class MemoryPressureTests(unittest.TestCase):
    def test_native_sysctl_decodes_darwin_values(self) -> None:
        swap = struct.pack("=QQQII", 100 * 1024**2, 40 * 1024**2, 60 * 1024**2, 4096, 1)
        with mock.patch.object(reclaim, "_sysctlbyname", return_value=swap):
            self.assertAlmostEqual(reclaim._parse_swap_ratio(reclaim._sysctl_native("vm.swapusage")), 0.6)
        for size in (4, 8):
            for level in (1, 2, 4):
                raw = level.to_bytes(size, sys.byteorder, signed=True)
                with self.subTest(size=size, level=level), \
                        mock.patch.object(reclaim, "_sysctlbyname", return_value=raw):
                    self.assertEqual(reclaim._sysctl_native("kern.memorystatus_vm_pressure_level"), str(level))

    def test_native_sysctl_failure_or_bad_size_falls_back_to_command(self) -> None:
        for name in ("vm.swapusage", "kern.memorystatus_vm_pressure_level"):
            for raw in (None, b"", b"bad"):
                with self.subTest(name=name, raw=raw), \
                        mock.patch.object(reclaim.sys, "platform", "darwin"), \
                        mock.patch.object(reclaim, "_sysctlbyname", return_value=raw), \
                        mock.patch.object(reclaim.subprocess, "check_output", return_value=b"fallback") as command:
                    self.assertEqual(reclaim._sysctl(name), "fallback")
                    self.assertEqual(command.call_args.args[0], ["sysctl", "-n", name])

    def test_native_sysctl_success_avoids_subprocess(self) -> None:
        with mock.patch.object(reclaim.sys, "platform", "darwin"), \
                mock.patch.object(reclaim, "_sysctlbyname", return_value=(2).to_bytes(4, sys.byteorder)), \
                mock.patch.object(reclaim.subprocess, "check_output") as command:
            self.assertEqual(reclaim._sysctl("kern.memorystatus_vm_pressure_level"), "2")
            command.assert_not_called()

    def test_parse_swap_ratio(self) -> None:
        text = "total = 10240.00M  used = 9115.06M  free = 1124.94M  (encrypted)"
        self.assertAlmostEqual(reclaim._parse_swap_ratio(text), 9115.06 / 10240.0, places=4)
        self.assertAlmostEqual(reclaim._parse_swap_ratio("total = 2.00G used = 512.00M free = 1.50G"), 0.25)
        self.assertIsNone(reclaim._parse_swap_ratio("total = 0.00M  used = 0.00M  free = 0.00M"))
        self.assertIsNone(reclaim._parse_swap_ratio("garbage"))

    def test_darwin_pressure_ignores_sticky_swap_and_follows_the_level(self) -> None:
        def sysctl(values):
            return lambda name: values.get(name)

        cases = (
            # Swap used/total is informational: 60% used on a calm machine is not pressure.
            ({"vm.swapusage": "total = 100.00M used = 60.00M free = 40.00M"}, False),
            ({"vm.swapusage": "total = 100.00M used = 59.00M free = 41.00M",
              "kern.memorystatus_vm_pressure_level": "1\n"}, False),
            ({"vm.swapusage": "total = 100.00M used = 1.00M free = 99.00M",
              "kern.memorystatus_vm_pressure_level": "2\n"}, True),
            ({"vm.swapusage": "total = 100.00M used = 1.00M free = 99.00M",
              "kern.memorystatus_vm_pressure_level": "4\n"}, True),
            ({}, False),
        )
        for values, expected in cases:
            with self.subTest(values=values), mock.patch.object(reclaim, "_sysctl", sysctl(values)):
                self.assertEqual(reclaim._darwin_pressure(), expected)

    def test_pressure_facts_still_report_swap_for_the_audit(self) -> None:
        values = {
            "vm.swapusage": "total = 100.00M used = 60.00M free = 40.00M",
            "kern.memorystatus_vm_pressure_level": "1\n",
        }
        with mock.patch.object(reclaim.sys, "platform", "darwin"), \
                mock.patch.object(reclaim, "_sysctl", lambda name: values.get(name)):
            facts = reclaim.pressure_facts()
        self.assertAlmostEqual(facts["swap_ratio"], 0.6)
        self.assertEqual(facts["pressure_level"], 1)

    def test_linux_parsers_and_thresholds(self) -> None:
        psi = "some avg10=0.00 avg60=12.50 avg300=3.00 total=1\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=0\n"
        self.assertEqual(reclaim._parse_psi_avg60(psi), 12.5)
        self.assertIsNone(reclaim._parse_psi_avg60("full avg60=1.0"))
        meminfo = "MemTotal:       16000000 kB\nMemFree: 1 kB\nMemAvailable:    1200000 kB\n"
        self.assertAlmostEqual(reclaim._parse_mem_available_ratio(meminfo), 0.075)
        files = {
            "/proc/pressure/memory": "some avg10=0 avg60=1.00 avg300=0 total=1\n",
            "/proc/meminfo": meminfo,
        }
        with mock.patch.object(reclaim, "_read", side_effect=lambda path: files[path]):
            self.assertTrue(reclaim._linux_pressure())
        files["/proc/meminfo"] = "MemTotal: 100 kB\nMemAvailable: 50 kB\n"
        with mock.patch.object(reclaim, "_read", side_effect=lambda path: files[path]):
            self.assertFalse(reclaim._linux_pressure())

    def test_any_probe_failure_means_no_pressure(self) -> None:
        with mock.patch.object(reclaim.sys, "platform", "darwin"), \
                mock.patch.object(reclaim, "_darwin_pressure", side_effect=RuntimeError("boom")):
            self.assertFalse(reclaim.memory_pressure())
        with mock.patch.object(reclaim.sys, "platform", "freebsd"):
            self.assertFalse(reclaim.memory_pressure())


class PolicyTests(unittest.TestCase):
    def test_enabled_by_default_and_off_with_env(self) -> None:
        with mock.patch.object(reclaim.shutil, "which", return_value="/usr/bin/tmux"):
            with mock.patch.dict(os.environ, {}, clear=True):
                self.assertTrue(reclaim.enabled())
            for name in ("CORRAL_RECLAIM", "PICKUP_RECLAIM", "SC_RECLAIM"):
                with self.subTest(name=name), mock.patch.dict(os.environ, {name: "0"}, clear=True):
                    self.assertFalse(reclaim.enabled())
            with mock.patch.dict(os.environ, {"CORRAL_RECLAIM": "1"}, clear=True):
                self.assertTrue(reclaim.enabled())

    def test_disabled_without_tmux(self) -> None:
        with mock.patch.object(reclaim.shutil, "which", return_value=None), \
                mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(reclaim.enabled())

    def test_minute_overrides_and_bad_values(self) -> None:
        env = {"CORRAL_RECLAIM_IDLE_MINUTES": "45", "CORRAL_RECLAIM_PRESSURE_IDLE_MINUTES": "3"}
        with mock.patch.dict(os.environ, env, clear=True):
            policy = reclaim.policy_from_env(NOW)
        self.assertEqual((policy.idle_minutes, policy.pressure_idle_minutes), (45.0, 3.0))
        for raw in ("abc", "0", "-5", ""):
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"CORRAL_RECLAIM_IDLE_MINUTES": raw}, clear=True):
                self.assertEqual(reclaim.policy_from_env(NOW).idle_minutes, reclaim.DEFAULT_IDLE_MINUTES)
        with mock.patch.dict(os.environ, {"CORRAL_RECLAIM_IDLE_MINUTES": "0.2"}, clear=True):
            self.assertEqual(reclaim.policy_from_env(NOW).idle_minutes, 1.0)


class HistoryMappingTests(unittest.TestCase):
    def test_history_from_session_takes_the_newest_timestamp(self) -> None:
        entry = reclaim.history_from_session({
            "source": "codex", "id": "abc", "status_tag": STATUS_DONE,
            "event_time": 100.0, "mtime": 300.0, "file_mtime": 200.0,
        })
        self.assertEqual((entry.runtime_id, entry.session_id, entry.active_at), ("codex", "abc", 300.0))

    def test_missing_or_bad_timestamps_are_unknown_not_zero(self) -> None:
        entry = reclaim.history_from_session({
            "source": "codex", "id": "abc", "status_tag": STATUS_DONE,
            "event_time": None, "mtime": 0, "file_mtime": True,
        })
        self.assertIsNone(entry.active_at)

    def test_history_by_name_groups_and_ignores_unhosted(self) -> None:
        grouped = reclaim.history_by_name([
            {"source": "claude", "id": "1", "keepalive_name": "corral-claude-11111111", "mtime": 5.0},
            {"source": "claude", "id": "1", "keepalive_name": "corral-claude-11111111", "provisional": True},
            {"source": "claude", "id": "2"},
        ])
        self.assertEqual(list(grouped), ["corral-claude-11111111"])
        self.assertEqual(len(grouped["corral-claude-11111111"]), 2)

    def test_reclaim_protect_flag_is_carried(self) -> None:
        self.assertTrue(reclaim.history_from_session({"reclaim_protect": True}).protect)
        self.assertFalse(reclaim.history_from_session({}).protect)


class ProbeTests(unittest.TestCase):
    def _fake_tmux(self, sessions: str | None, clients: str | None):
        def run(socket, *args):
            return sessions if args[0] == "list-sessions" else clients
        return run

    def test_control_mode_clients_do_not_count_but_terminal_clients_do(self) -> None:
        sessions = (
            "corral-claude-aaaa1111|1000|2000|3000\n"
            "corral-codex-bbbb2222|1000|2000|3000\n"
            "not-managed|1|2|3\n"
            "corral-broken|1|2\n"
        )
        clients = "corral-claude-aaaa1111|1\ncorral-codex-bbbb2222|0\ncorral-codex-bbbb2222|\n|0\n"
        with mock.patch.object(reclaim, "_tmux_out", self._fake_tmux(sessions, clients)), \
                mock.patch.object(reclaim, "_SOCKETS", ("test-socket",)):
            hosted = reclaim.probe_hosted()
        by_name = {h.name: h for h in hosted}
        self.assertEqual(sorted(by_name), ["corral-claude-aaaa1111", "corral-codex-bbbb2222"])
        self.assertEqual(by_name["corral-claude-aaaa1111"].foreign_clients, 0)
        self.assertEqual(by_name["corral-codex-bbbb2222"].foreign_clients, 2)
        self.assertEqual(by_name["corral-claude-aaaa1111"].created_at, 1000.0)
        self.assertEqual(by_name["corral-claude-aaaa1111"].window_activity, 3000.0)
        self.assertEqual(by_name["corral-claude-aaaa1111"].runtime_id, "claude")
        self.assertEqual(by_name["corral-claude-aaaa1111"].socket, "test-socket")

    def test_socket_is_skipped_when_either_listing_fails(self) -> None:
        sessions = "corral-claude-aaaa1111|1000|2000|3000\n"
        for fake in (self._fake_tmux(sessions, None), self._fake_tmux(None, "")):
            with self.subTest(), mock.patch.object(reclaim, "_tmux_out", fake), \
                    mock.patch.object(reclaim, "_SOCKETS", ("test-socket",)):
                self.assertEqual(reclaim.probe_hosted(), [])

    def test_isolation_env_hides_real_sockets(self) -> None:
        with mock.patch.dict(os.environ, {"CORRAL_ISOLATE_MANAGED_HOSTS": "1"}), \
                mock.patch.object(reclaim, "_tmux_out", side_effect=AssertionError("must not probe")):
            self.assertEqual(reclaim.probe_hosted(), [])


class HelperApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        patcher = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self.temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)

    def test_busy_pairs_lists_working_and_waiting_only(self) -> None:
        store = AttentionStore(Path(self.temp.name) / "att.sqlite3")
        for session_id, phase, question in (
            ("w", "working", None), ("q", "waiting", "call-1"), ("i", "idle", None),
        ):
            store.record_event(
                "codex", session_id,
                AttentionEvidence(phase=phase, activity_token=f"t-{session_id}", question_token=question,
                                  observed_at=2, source="observer"),
            )
        self.assertEqual(sorted(store.busy_pairs()), [("codex", "q"), ("codex", "w")])
        self.assertEqual(store.working_pairs(), [("codex", "w")])

    def test_busy_pairs_is_none_when_the_database_is_unavailable(self) -> None:
        store = AttentionStore(Path(self.temp.name) / "att.sqlite3")
        with mock.patch.object(AttentionStore, "_open", return_value=None):
            self.assertIsNone(store.busy_pairs())
            self.assertEqual(store.working_pairs(), [])  # legacy contract unchanged

    def test_live_viewers_are_read_only_and_follow_staleness_and_pid(self) -> None:
        embed.desired_host_size("corral-claude-viewed001", "viewer-a", 80, 24)
        self.addCleanup(embed.release_host_view, "corral-claude-viewed001", "viewer-a")
        self.assertIn("corral-claude-viewed001", embed.live_host_viewer_names())
        conn = embed._connect_host_viewers()
        conn.execute(
            "INSERT INTO host_viewers (session_name, viewer_id, width, height, pid, updated_at)"
            " VALUES ('stale-one','v',80,24,?,?)", (os.getpid(), time.monotonic() - 60),
        )
        conn.execute(
            "INSERT INTO host_viewers (session_name, viewer_id, width, height, pid, updated_at)"
            " VALUES ('dead-pid','v',80,24,?,?)",
            (2**31 - 2, time.monotonic()),
        )
        conn.commit()
        conn.close()
        live = embed.live_host_viewer_names()
        self.assertNotIn("stale-one", live)
        self.assertNotIn("dead-pid", live)
        conn = embed._connect_host_viewers()
        remaining = {row[0] for row in conn.execute("SELECT session_name FROM host_viewers")}
        conn.close()
        self.assertLessEqual({"stale-one", "dead-pid", "corral-claude-viewed001"}, remaining)  # nothing deleted

    def test_live_viewers_is_none_when_the_registry_cannot_be_read(self) -> None:
        with mock.patch.object(embed, "_connect_host_viewers", return_value=None):
            self.assertIsNone(embed.live_host_viewer_names())

    def test_pinned_keys_cover_individual_pins_and_pinned_group_members(self) -> None:
        db = split_layout.SidebarLayoutDB(Path(self.temp.name) / "layout.sqlite3")
        self.addCleanup(db.close)
        db.toggle_session_pin("claude:solo")
        db.set_group("/proj", ["claude:m1", "codex:m2"])
        db.set_group("/other", ["claude:x1", "codex:x2"])
        group_id = next(g.group_id for g in db.read().groups.values() if g.project_cwd == "/proj")
        db.toggle_group_pin(group_id)
        with mock.patch.object(split_layout, "default_layout_db", return_value=db):
            keys = split_layout.pinned_keys_effective()
        self.assertEqual(keys, {"claude:solo", "claude:m1", "codex:m2"})

    def test_pinned_keys_is_none_when_the_layout_database_is_unavailable(self) -> None:
        db = split_layout.SidebarLayoutDB(Path(self.temp.name) / "layout.sqlite3")
        self.addCleanup(db.close)
        with mock.patch.object(db, "_open", return_value=None), \
                mock.patch.object(split_layout, "default_layout_db", return_value=db):
            self.assertIsNone(split_layout.pinned_keys_effective())


class EntryPointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        for patcher in (
            mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self.temp.name}),
            mock.patch.object(reclaim, "_STARTED_AT", time.monotonic() - 1000),
            mock.patch.object(reclaim.shutil, "which", return_value="/usr/bin/tmux"),
            # These tests pin the non-background plumbing; the background
            # verdict has its own tests (test_busycheck.py) and must not let
            # the real machine's pane map classify the fake hosts below.
            mock.patch.object(
                reclaim, "_background_state", return_value=reclaim.BackgroundState(),
            ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.temp.cleanup)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.provider = lambda: [{"source": "claude", "id": "aaaa1111", "keepalive_name": "corral-claude-aaaa1111"}]

    def _stamp(self) -> Path:
        return Path(self.temp.name) / reclaim._STAMP_NAME

    def test_no_provider_means_unknown_history_and_no_work(self) -> None:
        with mock.patch.object(reclaim, "probe_hosted", side_effect=AssertionError("must not probe")):
            self.assertEqual(reclaim.maybe_reclaim(None, now=NOW), [])
        self.assertFalse(self._stamp().exists())

    def test_disabled_does_nothing(self) -> None:
        with mock.patch.dict(os.environ, {"CORRAL_RECLAIM": "0"}), \
                mock.patch.object(reclaim, "probe_hosted", side_effect=AssertionError("must not probe")):
            self.assertEqual(reclaim.maybe_reclaim(self.provider, now=NOW), [])

    def test_young_process_never_reclaims_and_does_not_burn_the_slot(self) -> None:
        with mock.patch.object(reclaim, "_STARTED_AT", time.monotonic()), \
                mock.patch.object(reclaim, "probe_hosted", side_effect=AssertionError("must not probe")):
            self.assertEqual(reclaim.maybe_reclaim(self.provider, now=NOW), [])
        self.assertFalse(self._stamp().exists())

    def test_throttled_machine_wide_to_one_pass_per_five_minutes(self) -> None:
        with mock.patch.object(reclaim, "probe_hosted", return_value=[]) as probe:
            reclaim.maybe_reclaim(self.provider, now=NOW)
            reclaim.maybe_reclaim(self.provider, now=NOW + 299)
            self.assertEqual(probe.call_count, 1)
            reclaim.maybe_reclaim(self.provider, now=NOW + 301)
            self.assertEqual(probe.call_count, 2)

    def test_pass_in_flight_elsewhere_blocks_a_second_pass(self) -> None:
        fd = reclaim._claim_slot(NOW)
        self.assertIsNotNone(fd)
        try:
            with mock.patch.object(reclaim, "probe_hosted", side_effect=AssertionError("must not probe")):
                self.assertEqual(reclaim.maybe_reclaim(self.provider, now=NOW + 500), [])
        finally:
            reclaim._release_slot(fd)

    def test_never_raises_and_stays_silent(self) -> None:
        def explode():
            raise RuntimeError("provider failed")

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(reclaim, "probe_hosted", return_value=[host()]):
            self.assertEqual(reclaim.maybe_reclaim(explode, now=NOW), [])
            self.assertEqual(reclaim.maybe_reclaim(explode, now=NOW + 500), [])
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))

    def _provider_for(self, hosted: Hosted, **extra):
        session = {
            "source": hosted.runtime_id, "id": hosted.ident + "-full-id", "keepalive_name": hosted.name,
            "status_tag": STATUS_DONE, "event_time": NOW - 4 * HOUR, "mtime": NOW - 4 * HOUR, **extra,
        }
        return lambda: [session]

    def test_unreadable_protection_source_blocks_the_whole_pass(self) -> None:
        old = host()
        for target, attr in (
            (AttentionStore, "busy_pairs"),
            (embed, "live_host_viewer_names"),
            (split_layout, "pinned_keys_effective"),
        ):
            with self.subTest(source=attr), \
                    mock.patch.object(reclaim, "probe_hosted", return_value=[old]), \
                    mock.patch.object(target, attr, return_value=None), \
                    mock.patch.object(reclaim, "apply", side_effect=AssertionError("must not kill")):
                self.assertEqual(reclaim.maybe_reclaim(self._provider_for(old), now=NOW), [])
            self._stamp().unlink(missing_ok=True)

    def test_full_pass_hands_only_the_inactive_session_to_apply_and_prints_nothing(self) -> None:
        old = host()
        unlisted = host("corral-codex-bbbb2222", runtime_id="codex", ident="bbbb2222")
        handed: list[list[str]] = []

        def fake_apply(verdicts, context, **kwargs):
            handed.append([v.name for v in verdicts])
            return handed[-1]

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.dict(os.environ, {"CORRAL_RECLAIM_BUDGET": "0"}), \
                mock.patch.object(reclaim, "probe_hosted", return_value=[old, unlisted]), \
                mock.patch.object(reclaim, "memory_pressure", return_value=False), \
                mock.patch.object(reclaim, "apply", side_effect=fake_apply):
            result = reclaim.maybe_reclaim(self._provider_for(old), now=NOW)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.assertEqual(result, [old.name])
        self.assertEqual(handed, [[old.name]])  # the unlisted session has unknown history


class RealTmuxTests(unittest.TestCase):
    """End to end on a private tmux socket: idle sessions die, protected ones live."""

    NAMES = {
        "done": "corral-claude-aaaa1111",
        "pending": "corral-codex-bbbb2222",
        "provisional": "corral-pi-cccc3333",
        "terminal": "corral-claude-dddd4444",
        "control": "corral-claude-eeee5555",
        "viewed": "corral-claude-ffff6666",
    }

    def setUp(self) -> None:
        if shutil.which("tmux") is None:
            self.skipTest("tmux not installed")
        self.temp = tempfile.TemporaryDirectory()
        self.socket = f"rc-test-{os.getpid()}-{int(time.time() * 1000) % 100000}"
        self.env = keepalive.tmux_env()
        env_patch = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self.temp.name})
        env_patch.start()
        os.environ.pop("CORRAL_ISOLATE_MANAGED_HOSTS", None)
        for patcher in (
            env_patch,
            mock.patch.object(reclaim, "_SOCKETS", (self.socket,)),
            mock.patch.object(reclaim, "_STARTED_AT", time.monotonic() - 1000),
            mock.patch.object(reclaim, "memory_pressure", return_value=False),
            # INV-15's machine check needs the runtime installed and a readable
            # history file; these fixtures only have synthetic session dicts.
            mock.patch.object(reclaim.retirement, "resume_block_reason", return_value=None),
        ):
            if patcher is not env_patch:
                patcher.start()
            self.addCleanup(patcher.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self._kill_server)
        self.procs: list[subprocess.Popen] = []
        self.addCleanup(self._stop_clients)
        for name in self.NAMES.values():
            self._tmux("-f", "/dev/null", "new-session", "-d", "-s", name, "-x", "80", "-y", "24", "--", "sleep", "600")

    def _tmux(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux", "-L", self.socket, *args], capture_output=True, text=True, env=self.env, timeout=10,
        )

    def _kill_server(self) -> None:
        self._tmux("kill-server")
        # tmux leaves the socket file behind once the last session is gone.
        with contextlib.suppress(OSError):
            os.unlink(f"/tmp/tmux-{os.getuid()}/{self.socket}")

    def _stop_clients(self) -> None:
        for proc in self.procs:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.terminate()
            with contextlib.suppress(Exception):
                proc.wait(timeout=5)

    def _survivors(self) -> set[str]:
        return set(self._tmux("list-sessions", "-F", "#{session_name}").stdout.split())

    def _wait_for_clients(self, count: int) -> None:
        for _ in range(60):
            if len(self._tmux("list-clients", "-F", "#{client_pid}").stdout.split()) >= count:
                return
            time.sleep(0.1)
        self.fail(f"expected {count} tmux clients to attach")

    def _attach_terminal_client(self, name: str) -> None:
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        env = {**self.env, "TERM": "xterm-256color"}
        self.procs.append(subprocess.Popen(
            ["tmux", "-L", self.socket, "attach", "-t", name],
            stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True,
        ))
        os.close(slave)

    def _attach_control_client(self, name: str) -> None:
        self.procs.append(subprocess.Popen(
            ["tmux", "-L", self.socket, "-C", "attach", "-t", name],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.env,
        ))

    def test_idle_finished_sessions_are_stopped_and_protected_ones_survive(self) -> None:
        self._attach_terminal_client(self.NAMES["terminal"])
        self._attach_control_client(self.NAMES["control"])
        self._wait_for_clients(2)
        embed.desired_host_size(self.NAMES["viewed"], "viewer-real", 80, 24)
        self.addCleanup(embed.release_host_view, self.NAMES["viewed"], "viewer-real")

        real_now = time.time()
        old = real_now - 4 * HOUR

        def entry(key: str, status: str, **extra) -> dict:
            return {
                "source": self.NAMES[key].split("-")[1], "id": self.NAMES[key].rsplit("-", 1)[1] + "-full",
                "keepalive_name": self.NAMES[key], "status_tag": status, "event_time": old, "mtime": old,
                "file_mtime": old, **extra,
            }

        sessions = [
            entry("done", STATUS_DONE),
            entry("pending", STATUS_PENDING),
            entry("provisional", STATUS_PENDING, provisional=True),
            entry("terminal", STATUS_DONE),
            entry("control", STATUS_DONE),
            entry("viewed", STATUS_DONE),
        ]
        events: list[tuple] = []
        out, err = io.StringIO(), io.StringIO()
        # tmux stamped the sessions "now"; look 3 hours ahead so they read as idle past the grace period.
        later = real_now + 3 * HOUR
        # Normal mode keeps up to 8 parked sessions resident (here only 3 are parked).
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(reclaim.observe, "event", side_effect=lambda *a, **k: events.append((a, k))):
            normal_reclaimed = reclaim.maybe_reclaim(lambda: sessions, now=later)
        self.assertEqual(normal_reclaimed, [])
        self.assertEqual(len(self._survivors()), 6)

        # Armed memory pressure (two reads >= 60 s apart, passes >= 300 s apart)
        # drops the resident holdback to 0 and retires every eligible session >= 60 min.
        events.clear()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(reclaim, "memory_pressure", return_value=True), \
                mock.patch.object(reclaim.observe, "event", side_effect=lambda *a, **k: events.append((a, k))):
            self.assertEqual(reclaim.maybe_reclaim(lambda: sessions, now=later + 305), [])
            reclaimed = reclaim.maybe_reclaim(lambda: sessions, now=later + 610)

        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""), "reclaim must be silent")
        # A finished session, an unused placeholder, and one watched only by a control-mode
        # client are stopped; pending history, a terminal client and a live viewer protect the rest.
        expected = {self.NAMES["done"], self.NAMES["provisional"], self.NAMES["control"]}
        self.assertEqual(set(reclaimed), expected)
        survivors = self._survivors()
        self.assertEqual(survivors, {self.NAMES[k] for k in ("pending", "terminal", "viewed")})
        audited = {fields["session"] for args, fields in events if args == ("reclaim",)}
        self.assertEqual(audited, expected)
        for args, fields in events:
            if args == ("reclaim",):
                self.assertGreaterEqual(fields["idle_min"], 60)
                self.assertEqual(fields["state"], "parked")
        # One pass event per pass carries the budget arithmetic (§4.4 step 3).
        passes = [fields for args, fields in events if args == ("reclaim_pass",)]
        self.assertEqual(len(passes), 2)
        self.assertEqual(passes[-1]["hosted"], 6)
        self.assertIn("parked", passes[-1])

        # Throttled: an immediate second pass does nothing even though it could.
        self.assertEqual(reclaim.maybe_reclaim(lambda: sessions, now=later + 615), [])


if __name__ == "__main__":
    unittest.main()
