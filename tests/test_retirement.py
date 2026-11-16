"""Retirement records, the §4.2 policy numbers, INV-15 resume, §7.6 restore.

The design is ``docs/design/HOSTED_SESSION_LIFECYCLE_DESIGN.md``: states §2,
signals §3, policy §4, invariants §5, per-runtime retire/resume §6, the owner
rulings in §6.5 and §7.6. Everything here is either pure data or a private tmux
socket; nothing touches a developer's live ``corral-keepalive`` sessions.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from sesskit.titles import STATUS_DONE, STATUS_NONE, STATUS_PENDING

from corral import keepalive, reclaim, reclaim_pass, retirement, split_layout
from corral.reclaim import Context, History, Hosted, PassReport
from corral.runtime.pi import PiRuntime

NOW = 10_000_000.0
HOUR = 3600.0
MIN = 60.0


def host(name: str = "corral-pi-aaaa1111", **overrides) -> Hosted:
    fields = dict(
        name=name, runtime_id="pi", ident=name.rsplit("-", 1)[-1], socket="corral-keepalive",
        created_at=NOW - 4 * HOUR, session_activity=NOW - 3 * HOUR, window_activity=NOW - 3 * HOUR,
    )
    fields.update(overrides)
    return Hosted(**fields)


def done(**overrides) -> History:
    fields = dict(
        status_tag=STATUS_DONE, active_at=NOW - 3 * HOUR, runtime_id="pi",
        session_id="aaaa1111-full-id", history_path="/tmp/pi/aaaa1111.jsonl", cwd="/tmp/proj",
    )
    fields.update(overrides)
    return History(**fields)


def ctx(**overrides) -> Context:
    return Context(now=NOW, **overrides)


def verdict(name: str = "corral-pi-aaaa1111", **overrides) -> reclaim.Verdict:
    fields = dict(
        name=name, runtime_id="pi", ident=name.rsplit("-", 1)[-1], socket="corral-keepalive",
        created_at=NOW - 4 * HOUR, session_activity=NOW - 3 * HOUR, window_activity=NOW - 3 * HOUR,
    )
    fields.update(overrides)
    return reclaim.evaluate(
        Hosted(**fields),
        [done(session_id=fields["ident"] + "-full-id")],
        ctx(),
    )


class CacheIsolated(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = mock.patch.dict(os.environ, {"CORRAL_CACHE_DIR": self.temp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)


# ------------------------------------------------------- §3.6 pressure hysteresis


class PressureHysteresisTests(unittest.TestCase):
    def state(self):
        return reclaim_pass.PressureState()

    def test_one_blip_does_not_arm(self) -> None:
        state = reclaim_pass.advance(self.state(), True, NOW)
        self.assertFalse(state.armed)
        # A quiet read resets the run of true reads.
        state = reclaim_pass.advance(state, False, NOW + 61)
        self.assertFalse(state.armed)
        self.assertEqual(state.true_count, 0)

    def test_two_reads_at_least_a_minute_apart_arm(self) -> None:
        state = reclaim_pass.advance(self.state(), True, NOW)
        state = reclaim_pass.advance(state, True, NOW + 61)
        self.assertTrue(state.armed)

    def test_two_reads_too_close_together_do_not_arm(self) -> None:
        state = reclaim_pass.advance(self.state(), True, NOW)
        state = reclaim_pass.advance(state, True, NOW + 10)
        self.assertFalse(state.armed)
        self.assertEqual(state.true_count, 2)

    def test_disarming_needs_ten_quiet_minutes(self) -> None:
        state = reclaim_pass.advance(self.state(), True, NOW)
        state = reclaim_pass.advance(state, True, NOW + 61)
        self.assertTrue(state.armed)
        state = reclaim_pass.advance(state, False, NOW + 62)
        self.assertTrue(state.armed, "one quiet read must not disarm")
        state = reclaim_pass.advance(state, False, NOW + 62 + 599)
        self.assertTrue(state.armed)
        state = reclaim_pass.advance(state, False, NOW + 62 + 601)
        self.assertFalse(state.armed)

    def test_state_persists_across_processes(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "pass.json"
        first = reclaim_pass.observe(True, NOW, path=path)
        self.assertFalse(first.armed)
        second = reclaim_pass.observe(True, NOW + 61, path=path)
        self.assertTrue(second.armed)
        self.assertTrue(reclaim_pass.load_state(path).armed)
        # A corrupt file degrades to a fresh, unarmed state, never an exception.
        path.write_text("{not json", encoding="utf-8")
        self.assertFalse(reclaim_pass.load_state(path).armed)

    def test_knobs_come_from_the_product_env_prefixes(self) -> None:
        with mock.patch.dict(os.environ, {
            "CORRAL_RECLAIM_PRESSURE_SAMPLES": "1",
            "CORRAL_RECLAIM_PRESSURE_DISARM_MINUTES": "2",
        }, clear=True):
            knobs = reclaim.pressure_knobs()
        self.assertEqual(knobs["samples"], 1)
        self.assertEqual(knobs["disarm_seconds"], 120.0)
        with mock.patch.dict(os.environ, {"SC_RECLAIM_PRESSURE_SAMPLES": "4"}, clear=True):
            self.assertEqual(reclaim.pressure_knobs()["samples"], 4)
        with mock.patch.dict(os.environ, {"CORRAL_RECLAIM_PRESSURE_SAMPLES": "nope"}, clear=True):
            self.assertEqual(reclaim.pressure_knobs()["samples"], reclaim_pass.PRESSURE_SAMPLES)

    def test_ladder_only_applies_while_pressure_is_armed(self) -> None:
        state = self.state()
        self.assertIsNone(reclaim_pass.ladder_minutes(state, armed=False))
        self.assertEqual(reclaim_pass.ladder_minutes(state, armed=True), 120.0)
        state = reclaim_pass.escalate(state, over_budget=True)
        self.assertEqual(reclaim_pass.ladder_minutes(state, armed=True), 90.0)
        for _ in range(10):
            state = reclaim_pass.escalate(state, over_budget=True)
        self.assertEqual(reclaim_pass.ladder_minutes(state, armed=True), 30.0)
        # Under budget again: the ladder resets in one pass.
        self.assertEqual(reclaim_pass.escalate(state, over_budget=False).budget_steps, 0)


# ------------------------------------------------------------- §3.1 CPU time delta


class CpuDeltaTests(unittest.TestCase):
    def sample(self, cpu: float, *, pane: int = 100, offset: float = 60.0, at: float = NOW):
        return reclaim_pass.CpuSample(pane_pid=pane, start_offset=offset, cpu_seconds=cpu, sampled_at=at)

    def test_zero_delta_is_proof_of_quiet(self) -> None:
        self.assertEqual(
            reclaim_pass.cpu_delta(
                self.sample(10.0), self.sample(10.0, offset=120.0, at=NOW + 60),
            ),
            0.0,
        )

    def test_nonzero_delta_is_reported(self) -> None:
        self.assertAlmostEqual(
            reclaim_pass.cpu_delta(
                self.sample(10.0), self.sample(10.5, offset=120.0, at=NOW + 60),
            ),
            0.5,
        )

    def test_replaced_or_respawned_process_is_unknown(self) -> None:
        self.assertIsNone(reclaim_pass.cpu_delta(self.sample(10.0), self.sample(10.0, pane=200, at=NOW + 60)))
        self.assertIsNone(reclaim_pass.cpu_delta(
            self.sample(10.0, offset=60), self.sample(10.0, offset=5, at=NOW + 60),
        ))
        self.assertIsNone(reclaim_pass.cpu_delta(self.sample(10.0), self.sample(10.0, offset=60.0, at=NOW)))

    def test_a_shrinking_tree_is_unknown_not_quiet(self) -> None:
        # A descendant exited, so its CPU time left the sum: the delta is negative
        # and must not read as "zero work".
        self.assertIsNone(
            reclaim_pass.cpu_delta(self.sample(20.0), self.sample(5.0, offset=120.0, at=NOW + 60)),
        )

    def test_observe_cpu_needs_two_samples(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "cpu.json"
        readings = {"corral-pi-aaaa1111": (100, 5.0, "1:00")}
        self.assertEqual(reclaim_pass.observe_cpu(readings, NOW, path=path), {})
        later = {"corral-pi-aaaa1111": (100, 5.25, "2:00")}
        self.assertAlmostEqual(reclaim_pass.observe_cpu(later, NOW + 60, path=path)["corral-pi-aaaa1111"], 0.25)

    def test_a_busy_cpu_delta_blocks_retirement(self) -> None:
        entry = done()
        quiet = reclaim.evaluate(host(), [entry], ctx(cpu={"corral-pi-aaaa1111": 0.0}))
        self.assertTrue(quiet.reclaim)
        busy = reclaim.evaluate(host(), [entry], ctx(cpu={"corral-pi-aaaa1111": 5.0}))
        self.assertFalse(busy.reclaim)
        self.assertEqual(busy.reason, "cpu_active")
        self.assertEqual(busy.state, "working")
        self.assertEqual(busy.evidence["cpu_delta_s"], 5.0)
        # A missing sample is unknown: it neither vetoes nor permits.
        unknown = reclaim.evaluate(host(), [entry], ctx())
        self.assertTrue(unknown.reclaim)
        self.assertIsNone(unknown.evidence["cpu_delta_s"])

    def test_snapshot_ps_parses_cpu_and_elapsed_columns(self) -> None:
        from corral import busycheck

        out = (
            "  PID  PPID      TIME ELAPSED COMMAND\n"
            "  100     1   0:45.84   43:16 /opt/pi/bin/pi\n"
            "  101   100   1:02:03.50   2-01:00:00 worker --flag\n"
            "  102   101   0:00.00       0:09 broken\n"
        )
        table = busycheck.snapshot_ps(lambda *a, **k: out)
        assert table is not None
        self.assertEqual([proc.pid for proc in table], [100, 101, 102])
        self.assertAlmostEqual(table[1].cpu_seconds, 3723.5)
        self.assertEqual(table[1].etimes, "2-01:00:00")
        self.assertEqual(table[1].command, "worker --flag")
        self.assertAlmostEqual(busycheck.tree_cpu_seconds(100, table), 45.84 + 3723.5)

    def test_shared_background_server_detection(self) -> None:
        from corral import busycheck

        table = [busycheck.Proc(1, 0, "opencode serve --port 4096")]
        self.assertTrue(busycheck.shared_background_servers(table, "opencode"))
        self.assertFalse(busycheck.shared_background_servers([busycheck.Proc(1, 0, "pi")], "opencode"))
        self.assertIsNone(busycheck.shared_background_servers(None, "opencode"))


# ------------------------------------------------- §4.2 policy numbers, D1 floor


class PolicyNumberTests(unittest.TestCase):
    def test_floor_and_default_threshold(self) -> None:
        context = ctx()
        self.assertEqual(context.effective_idle_minutes, 120.0)
        self.assertEqual(context.threshold_reason, "default")
        self.assertEqual(context.floor_minutes, 30.0)

    def test_d1_pressure_never_goes_below_the_floor(self) -> None:
        context = ctx(pressure=True)
        self.assertEqual(context.threshold_minutes, 10.0)
        self.assertEqual(context.effective_idle_minutes, 30.0)
        self.assertEqual(context.threshold_reason, "pressure")
        self.assertEqual(
            reclaim.evaluate(host(session_activity=NOW - 29 * MIN), [done()], context).reason, "floor",
        )
        self.assertTrue(reclaim.evaluate(host(session_activity=NOW - 31 * MIN), [done()], context).reclaim)

    def test_ladder_cannot_break_the_floor_either(self) -> None:
        context = ctx(pressure=True, budget_threshold_minutes=30.0)
        self.assertEqual(context.effective_idle_minutes, 30.0)
        self.assertEqual(context.threshold_reason, "budget_pressure")
        floored = ctx(pressure=True, budget_threshold_minutes=1.0)
        self.assertEqual(floored.effective_idle_minutes, 30.0)

    def test_budget_and_per_pass_numbers(self) -> None:
        self.assertEqual(ctx().budget, reclaim.RESIDENT_BUDGET)
        self.assertEqual(ctx(pressure=True).budget, reclaim.PRESSURE_RESIDENT_BUDGET)
        normal = [verdict(f"corral-pi-bbbb{i:04d}") for i in range(8)]
        pressure = [verdict(f"corral-pi-cccc{i:04d}") for i in range(8)]
        normal_hosts = [v.host for v in normal]
        pressure_hosts = [v.host for v in pressure]
        self.assertEqual(len(reclaim.decide(normal_hosts, records_from(normal_hosts), ctx())), 3)
        self.assertEqual(
            len(reclaim.decide(pressure_hosts, records_from(pressure_hosts), ctx(pressure=True))), 6,
        )

    def test_budget_env_zero_disables_the_lever(self) -> None:
        with mock.patch.dict(os.environ, {"CORRAL_RECLAIM_BUDGET": "0"}, clear=True):
            self.assertEqual(reclaim.policy_from_env(NOW).resident_budget, 0)
        with mock.patch.dict(os.environ, {"CORRAL_RECLAIM_BUDGET": "junk"}, clear=True):
            self.assertEqual(reclaim.policy_from_env(NOW).resident_budget, reclaim.RESIDENT_BUDGET)

    def test_policy_view_exposes_the_effective_number(self) -> None:
        with mock.patch.object(reclaim, "_LAST_REPORT", None), \
                mock.patch.object(reclaim_pass, "load_state", return_value=reclaim_pass.PressureState()):
            view = reclaim.policy_view(now=NOW)
        self.assertEqual(view["effective_idle_minutes"], 120.0)
        self.assertEqual(view["threshold_reason"], "default")
        report = PassReport(at=NOW, effective_idle_minutes=30.0, threshold_reason="pressure", parked=4)
        with mock.patch.object(reclaim, "_LAST_REPORT", report):
            view = reclaim.policy_view()
        self.assertEqual((view["effective_idle_minutes"], view["threshold_reason"]), (30.0, "pressure"))
        self.assertEqual(view["parked"], 4)


# ---------------------------------- §5 INV-4/13 floor, resume grace and cooldown


class GraceAndCooldownTests(unittest.TestCase):
    def test_creation_grace_is_thirty_minutes(self) -> None:
        self.assertEqual(reclaim.evaluate(host(created_at=NOW - 29 * MIN), [done()], ctx()).reason, "grace")

    def test_resume_grace_is_ten_minutes(self) -> None:
        resumed = {"corral-pi-aaaa1111": NOW - 5 * MIN}
        blocked = reclaim.evaluate(host(), [done()], ctx(resumed_at=resumed))
        self.assertFalse(blocked.reclaim)
        self.assertEqual(blocked.reason, "resume_grace")
        self.assertEqual(blocked.evidence["resumed_age_min"], 5.0)
        old = {"corral-pi-aaaa1111": NOW - 11 * MIN}
        self.assertTrue(reclaim.evaluate(host(), [done()], ctx(resumed_at=old)).reclaim)

    def test_resume_marker_matches_however_it_was_keyed(self) -> None:
        for key in ("corral-pi-aaaa1111", "pi:aaaa1111", "pi:aaaa1111-full-id"):
            with self.subTest(key=key):
                verdict_ = reclaim.evaluate(host(), [done()], ctx(resumed_at={key: NOW - MIN}))
                self.assertEqual(verdict_.reason, "resume_grace")

    def test_cooldown_blocks_a_second_retirement(self) -> None:
        recent = {"corral-pi-aaaa1111": NOW - 5 * MIN}
        blocked = reclaim.evaluate(host(), [done()], ctx(retired_at=recent))
        self.assertEqual(blocked.reason, "cooldown")
        self.assertEqual(blocked.evidence["cooldown_remaining_min"], 25.0)
        old = {"corral-pi-aaaa1111": NOW - 31 * MIN}
        self.assertTrue(reclaim.evaluate(host(), [done()], ctx(retired_at=old)).reclaim)

    def test_cooldown_survives_the_process_through_the_record(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / retirement.RECORD_FILE
        record = retirement.RetirementRecord(
            name="corral-pi-aaaa1111", runtime_id="pi", ident="aaaa1111", retired_at=NOW,
        )
        self.assertTrue(retirement.append_record(record, path=path))
        self.assertEqual(
            retirement.retired_at_map(now=NOW + 60, window_seconds=1800, path=path),
            {"corral-pi-aaaa1111": NOW},
        )
        # A failed kill is not a retirement and must not cool the session down.
        failed = retirement.RetirementRecord(
            name="corral-pi-bbbb2222", runtime_id="pi", ident="bbbb2222",
            retired_at=NOW, outcome="kill_failed",
        )
        self.assertTrue(retirement.append_record(failed, path=path))
        self.assertNotIn(
            "corral-pi-bbbb2222",
            retirement.retired_at_map(now=NOW + 60, window_seconds=1800, path=path),
        )
        self.assertEqual(retirement.retired_at_map(now=NOW + 4000, window_seconds=1800, path=path), {})


def records_from(hosts):
    return {h.name: [done(session_id=h.ident + "-full-id")] for h in hosts}


class PolicyNumberDecideTests(unittest.TestCase):
    def test_per_pass_cap_is_three_then_six(self) -> None:
        normal = [host(f"corral-pi-bbbb{i:04d}") for i in range(8)]
        self.assertEqual(len(reclaim.decide(normal, records_from(normal), ctx())), 3)
        self.assertEqual(len(reclaim.decide(normal, records_from(normal), ctx(pressure=True))), 6)

    def test_ties_at_the_cap_are_broken_by_measured_footprint(self) -> None:
        hosts = [host(f"corral-pi-dddd{i:04d}") for i in range(5)]
        history = records_from(hosts)
        measured = {hosts[0].name: 100, hosts[1].name: 300, hosts[2].name: 200,
                    hosts[3].name: 900, hosts[4].name: 50}
        with mock.patch.object(reclaim, "_footprint_bytes", side_effect=lambda v, c: measured[v.name]) as probe:
            picked = reclaim.decide(hosts, history, ctx(max_per_pass=2))
        self.assertEqual(
            [verdict.name for verdict in picked],
            ["corral-pi-dddd0003", "corral-pi-dddd0001"],
        )
        self.assertLessEqual(probe.call_count, 2 + 4)

    def test_no_footprint_probe_without_a_tie(self) -> None:
        hosts = [host(f"corral-pi-eeee{i:04d}", session_activity=NOW - (i + 1) * HOUR - MIN) for i in range(4)]
        history = {
            h.name: [done(session_id=h.ident, active_at=NOW - (index + 1) * HOUR - MIN)]
            for index, h in enumerate(hosts)
        }
        with mock.patch.object(reclaim, "_footprint_bytes", side_effect=AssertionError("no fork wanted")):
            picked = reclaim.decide(hosts, history, ctx(max_per_pass=2))
        self.assertEqual([verdict.name for verdict in picked], ["corral-pi-eeee0003", "corral-pi-eeee0002"])


# --------------------------------------------------------------- §5 invariant tests


class ApplyInvariantTests(CacheIsolated):
    def setUp(self) -> None:
        super().setUp()
        patcher = mock.patch.object(reclaim.observe, "event")
        self.events = patcher.start()
        self.addCleanup(patcher.stop)

    def _apply(self, verdicts, context=None, **overrides):
        report = overrides.pop("report", PassReport(at=NOW))
        kwargs = dict(
            kill=lambda h: True,
            after_kill=lambda n: None,
            record=lambda record: True,
            identity=lambda name, socket: retirement.PaneIdentity(pane_pid=1, server_pid=2),
            resume_guard=lambda *a, **k: None,
        )
        kwargs.update(overrides)
        return reclaim.apply(verdicts, context or ctx(), report=report, **kwargs), report

    def test_inv6_a_replaced_pane_cancels_the_retirement(self) -> None:
        seen = iter([
            retirement.PaneIdentity(1, 2),
            retirement.PaneIdentity(3, 4),
        ])
        killed: list[str] = []
        reclaimed, report = self._apply(
            [verdict()], identity=lambda name, socket: next(seen),
            kill=lambda h: killed.append(h.name) or True,
        )
        self.assertEqual(reclaimed, [])
        self.assertEqual(killed, [])
        self.assertEqual(report.blocked, {"pane_replaced": 1})

    def test_inv6_an_unreadable_pane_blocks(self) -> None:
        reclaimed, report = self._apply([verdict()], identity=lambda name, socket: None)
        self.assertEqual(reclaimed, [])
        self.assertEqual(report.blocked, {"pane_unknown": 1})

    def test_d2_a_failed_record_blocks_the_kill_and_is_counted(self) -> None:
        killed: list[str] = []
        reclaimed, report = self._apply(
            [verdict()], record=lambda record: False, kill=lambda h: killed.append(h.name) or True,
        )
        self.assertEqual(reclaimed, [])
        self.assertEqual(killed, [])
        self.assertEqual(report.blocked, {"audit_unwritable": 1})
        self.assertEqual(report.skipped, [{"session": "corral-pi-aaaa1111", "reason": "audit_unwritable"}])

    def test_d2_a_failing_audit_event_also_blocks(self) -> None:
        self.events.side_effect = RuntimeError("log gone")
        killed: list[str] = []
        reclaimed, report = self._apply([verdict()], kill=lambda h: killed.append(h.name) or True)
        self.assertEqual(reclaimed, [])
        self.assertEqual(killed, [])
        self.assertEqual(report.blocked, {"audit_unwritable": 1})

    def test_inv15_no_retirement_without_a_guaranteed_resume(self) -> None:
        killed: list[str] = []
        reclaimed, report = self._apply(
            [verdict()], resume_guard=lambda *a, **k: "resume_unverified",
            kill=lambda h: killed.append(h.name) or True,
        )
        self.assertEqual(reclaimed, [])
        self.assertEqual(killed, [])
        self.assertEqual(report.blocked, {"resume_unverified": 1})

    def test_inv11_no_retirement_during_a_server_operation(self) -> None:
        lock = Path(self.temp.name) / retirement.SERVER_OPERATION_LOCK
        lock.write_text("migrating", encoding="utf-8")
        killed: list[str] = []
        reclaimed, report = self._apply([verdict()], kill=lambda h: killed.append(h.name) or True)
        self.assertEqual(reclaimed, [])
        self.assertEqual(killed, [])
        self.assertEqual(report.blocked, {"server_operation": 1})
        # A stale lock no longer blocks for ever.
        os.utime(lock, (time.time() - 7200, time.time() - 7200))
        reclaimed, _ = self._apply([verdict()], Context(now=time.time()))
        self.assertEqual(reclaimed, ["corral-pi-aaaa1111"])

    def test_inv7_a_concurrent_resume_wins(self) -> None:
        canonical = "pi:aaaa1111"
        # flock is per open file description, so a second descriptor in this
        # process contends exactly like a resume in another Corral process.
        real_lock = retirement.session_lock
        with mock.patch.object(
            retirement, "session_lock",
            lambda key, **kw: real_lock(key, wait_seconds=0.05),
        ), real_lock(canonical):
            killed: list[str] = []
            reclaimed, report = self._apply([verdict()], kill=lambda h: killed.append(h.name) or True)
        self.assertEqual(reclaimed, [])
        self.assertEqual(killed, [])
        self.assertEqual(report.blocked, {"resume_race": 1})

    def test_inv10_a_failed_kill_is_not_retried_or_counted(self) -> None:
        killed: list[str] = []
        verdicts = [verdict(), verdict("corral-pi-aaaa2222")]
        reclaimed, report = self._apply(
            verdicts, kill=lambda h: killed.append(h.name) or False,
        )
        self.assertEqual(reclaimed, [])
        self.assertEqual(killed, ["corral-pi-aaaa1111", "corral-pi-aaaa2222"])
        self.assertEqual(report.blocked, {"kill_failed": 2})

    def test_dry_run_audits_but_kills_nothing(self) -> None:
        killed: list[str] = []
        reclaimed, report = self._apply(
            [verdict()], ctx(dry_run=True), kill=lambda h: killed.append(h.name) or True,
        )
        self.assertEqual(reclaimed, [])
        self.assertEqual(killed, [])
        self.assertEqual(report.skipped, [{"session": "corral-pi-aaaa1111", "reason": "dry_run"}])
        self.assertEqual(self.events.call_args.args[0], "reclaim_would")

    def test_dry_run_is_a_single_env_switch(self) -> None:
        with mock.patch.dict(os.environ, {"CORRAL_RECLAIM_DRY_RUN": "1"}, clear=True):
            self.assertTrue(reclaim.policy_from_env(NOW).dry_run)
        with mock.patch.dict(os.environ, {"CORRAL_RECLAIM_DRY_RUN": "0"}, clear=True):
            self.assertFalse(reclaim.policy_from_env(NOW).dry_run)


class RecordContentTests(CacheIsolated):
    def test_record_carries_every_field_the_verdict_used(self) -> None:
        original = verdict()
        identity = retirement.PaneIdentity(pane_pid=41, server_pid=42)
        record = reclaim._record_for(original, ctx(), identity)
        self.assertEqual((record.name, record.runtime_id, record.ident), ("corral-pi-aaaa1111", "pi", "aaaa1111"))
        self.assertEqual(record.session_id, "aaaa1111-full-id")
        self.assertEqual(record.history_path, "/tmp/pi/aaaa1111.jsonl")
        self.assertEqual(record.cwd, "/tmp/proj")
        self.assertEqual(record.state, "parked")
        self.assertEqual((record.pane_pid, record.server_pid), (41, 42))
        for key in ("idle_min", "status_tag", "effective_threshold_min", "threshold_reason", "floor_min",
                    "cpu_delta_s", "pressure", "created_age_min", "runtime", "socket"):
            self.assertIn(key, record.evidence)
        self.assertTrue(record.resume_ready)
        self.assertEqual(record.resume_argv[0], "pi")

    def test_pi_resume_plan_is_the_runtime_plan(self) -> None:
        record = reclaim._record_for(verdict(), ctx(), retirement.PaneIdentity(1, 2))
        plan = retirement.resume_plan(record)
        expected = PiRuntime().build_resume_plan({
            "source": "pi", "id": record.session_id, "path": record.history_path, "cwd": record.cwd,
        })
        assert plan is not None
        self.assertEqual(plan.argv, expected.argv)
        self.assertEqual(plan.argv[0], "pi")
        self.assertIn("--session", plan.argv)
        self.assertIn(record.history_path, plan.argv)
        self.assertEqual(plan.cwd, expected.cwd)

    def test_record_write_is_verified(self) -> None:
        record = retirement.RetirementRecord(
            name="corral-pi-aaaa1111", runtime_id="pi", ident="aaaa1111", retired_at=NOW,
        )
        self.assertTrue(retirement.append_record(record))
        read_back = retirement.read_records()
        self.assertEqual(len(read_back), 1)
        self.assertEqual(read_back[0].name, record.name)

    def test_unwritable_record_path_reports_failure(self) -> None:
        blocked_path = Path(self.temp.name) / "nope" / "x.jsonl"
        blocked_path.parent.write_text("I am a file, not a directory", encoding="utf-8")
        record = retirement.RetirementRecord(name="x", runtime_id="pi", ident="x", retired_at=NOW)
        self.assertFalse(retirement.append_record(record, path=blocked_path))
        self.assertEqual(retirement.read_records(path=blocked_path), [])

    def test_malformed_lines_are_skipped(self) -> None:
        path = Path(self.temp.name) / retirement.RECORD_FILE
        path.write_text(
            '{"name": "a", "retired_at": 5, "runtime_id": "pi", "ident": "a"}\nnot json\n\n',
            encoding="utf-8",
        )
        self.assertEqual([record.name for record in retirement.read_records(path=path)], ["a"])

    def test_resume_block_reason_checks_the_machine(self) -> None:
        with mock.patch.object(retirement.shutil, "which", return_value="/usr/bin/pi"):
            self.assertIsNone(retirement.resume_block_reason(
                "pi", session_id="x", history_path=__file__, cwd="/tmp",
            ))
            self.assertEqual(
                retirement.resume_block_reason("pi", session_id="x", history_path="/nope/x.jsonl", cwd="/tmp"),
                "history_unreadable",
            )
            self.assertEqual(
                retirement.resume_block_reason("nope", session_id="x", history_path="", cwd=""),
                "resume_unverified",
            )
        with mock.patch.object(retirement.shutil, "which", return_value=None):
            self.assertEqual(
                retirement.resume_block_reason("pi", session_id="x", history_path=__file__, cwd="/tmp"),
                "resume_unverified",
            )


# ------------------------------------------------------- §6 per-runtime constraints


class RuntimeConstraintTests(unittest.TestCase):
    def test_codex_never_retires_without_a_verified_transcript(self) -> None:
        entry = done(runtime_id="codex", session_id="bbbb2222", history_path="/tmp/codex.jsonl")
        codex = host("corral-codex-bbbb2222", runtime_id="codex", ident="bbbb2222")
        self.assertTrue(retirement.runtime_requires_transcript("codex"))
        result = reclaim.evaluate(codex, [entry], ctx())
        self.assertFalse(result.reclaim)
        self.assertEqual(result.reason, "runtime_transcript_unverified")
        self.assertEqual(result.state, "unknown")

    def test_codex_retires_once_the_transcript_is_verified_clean(self) -> None:
        from corral.busycheck import BusyChecker, Proc

        entry = done(runtime_id="codex", session_id="bbbb2222", history_path="/nope/codex.jsonl")
        codex = host("corral-codex-bbbb2222", runtime_id="codex", ident="bbbb2222")
        checker = BusyChecker(
            ps_table=[Proc(100, 1, "/opt/codex/codex")], pane_pids={codex.name: 100},
            history_paths={codex.name: "/nope/codex.jsonl"},
        )
        state = reclaim.BackgroundState(checker=checker)
        with mock.patch.object(reclaim.BackgroundState, "check_transcript",
                               return_value=(False, {"checked": True})):
            result = reclaim.evaluate(codex, [entry], ctx(background=state))
        self.assertTrue(result.reclaim)

    def test_opencode_with_a_shared_server_is_never_retired(self) -> None:
        entry = done(runtime_id="opencode", session_id="ses_123", history_path="/tmp/oc.db")
        oc = host("corral-opencode-cccc3333", runtime_id="opencode", ident="cccc3333")
        self.assertTrue(retirement.runtime_shares_background_server("opencode"))
        blocked = reclaim.evaluate(oc, [entry], ctx(shared_opencode_server=True))
        self.assertFalse(blocked.reclaim)
        self.assertEqual(blocked.reason, "opencode_shared_server")
        self.assertTrue(blocked.evidence["opencode_shared_server"])
        without = reclaim.evaluate(oc, [entry], ctx(shared_opencode_server=False))
        self.assertTrue(without.reclaim)
        self.assertFalse(without.evidence["opencode_shared_server"])

    def test_pending_status_names_waiting_on_tool(self) -> None:
        pending = reclaim.evaluate(host(), [done(status_tag=STATUS_PENDING)], ctx())
        self.assertEqual((pending.reason, pending.state), ("history_pending", "waiting_on_tool"))
        unknown = reclaim.evaluate(host(), [done(status_tag=STATUS_NONE)], ctx())
        self.assertEqual((unknown.reason, unknown.state), ("history_unknown", "unknown"))

    def test_waiting_on_user_is_named_apart_from_working(self) -> None:
        pair = ("pi", "aaaa1111-full-id")
        waiting = reclaim.evaluate(host(), [done()], ctx(waiting_pairs={pair}, busy_pairs={pair}))
        self.assertEqual((waiting.reason, waiting.state), ("busy", "waiting_on_user"))
        working = reclaim.evaluate(host(), [done()], ctx(busy_pairs={pair}))
        self.assertEqual(working.state, "working")


# --------------------------------------- §6.5 / §7.6: restore and retry, no user action


class RestoreTests(CacheIsolated):
    def setUp(self) -> None:
        super().setUp()
        self.events: list[tuple] = []
        patcher = mock.patch.object(
            retirement, "_event", side_effect=lambda name, **fields: self.events.append((name, fields)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        # These cases measure the §6.5/§7.6 restore bookkeeping, not whether the
        # machine happens to have the runtime CLI installed. The INV-15 probe
        # does depend on PATH (a Linux CI runner has none of them), so present a
        # machine that has every runtime; the probe itself is covered by
        # test_resume_block_reason_checks_the_machine and
        # test_a_runtime_missing_from_this_machine_is_never_attempted.
        installed = mock.patch.object(
            retirement.shutil, "which", side_effect=lambda name, *args, **kwargs: f"/usr/bin/{name}",
        )
        installed.start()
        self.addCleanup(installed.stop)

    def test_restored_once_and_the_retry_sequence_is_cleared(self) -> None:
        calls: list[int] = []
        result = retirement.restore_session(
            "pi:aaaa", lambda: calls.append(1), runtime_id="pi", session_id="aaaa",
            history_path=__file__, cwd="/tmp",
        )
        self.assertEqual((result.state, calls), ("restored", [1]))
        self.assertEqual(retirement.load_resume_retries(), {})

    def test_a_failure_schedules_corrals_own_retry(self) -> None:
        def fail():
            raise RuntimeError("pane did not come up")

        result = retirement.restore_session(
            "pi:bbbb", fail, runtime_id="pi", session_id="bbbb", history_path=__file__, cwd="/tmp",
        )
        self.assertEqual(result.state, "pending")
        self.assertEqual(result.attempts, 1)
        self.assertGreater(result.next_in_seconds or 0, 0)
        stored = retirement.load_resume_retries()
        self.assertEqual(list(stored), ["pi:bbbb"])
        self.assertEqual([name for name, _ in self.events], ["resume_retry"])
        self.assertEqual(self.events[0][1]["cause"], "RuntimeError")
        self.assertEqual(self.events[0][1]["attempt"], 1)
        self.assertEqual(retirement.due_resume_retries(now=time.time()), [])
        self.assertEqual(len(retirement.due_resume_retries(now=time.time() + 3600)), 1)

    def test_repeated_failures_are_bounded_then_reported_once(self) -> None:
        def fail():
            raise RuntimeError("still failing")

        key = "pi:cccc"
        first = retirement.restore_session(key, fail, now=NOW, history_path=__file__)
        self.assertEqual((first.state, first.attempts), ("pending", 1))
        second = retirement.restore_session(key, fail, now=NOW + 3600, history_path=__file__)
        self.assertEqual((second.state, second.attempts), ("pending", 2))
        third = retirement.restore_session(key, fail, now=NOW + 7200, history_path=__file__)
        self.assertEqual((third.state, third.attempts), ("pending", 3))
        final = retirement.restore_session(key, fail, now=NOW + 10800, history_path=__file__)
        self.assertEqual(final.state, "unresolvable")
        self.assertEqual(retirement.load_resume_retries(), {})
        self.assertEqual([name for name, _ in self.events], ["resume_retry", "resume_retry", "resume_retry",
                                                            "resume_failed_terminal"])
        record = retirement.read_records()[-1]
        self.assertEqual((record.outcome, record.reason), ("resume_failed", "RuntimeError"))
        self.assertEqual(record.history_path, __file__)  # the content stays reachable

    def test_a_runtime_saying_the_record_is_gone_is_terminal_at_once(self) -> None:
        class Gone(RuntimeError):
            code = "not_found"

        def gone():
            raise Gone("thread gone")

        result = retirement.restore_session(
            "pi:dddd", gone, now=NOW, history_path="/tmp/pi/dddd.jsonl",
        )
        self.assertEqual(result.state, "unresolvable")
        self.assertEqual(result.cause, "not_found")
        self.assertEqual(retirement.load_resume_retries(), {})
        self.assertEqual(retirement.read_records()[-1].history_path, "/tmp/pi/dddd.jsonl")

    def test_an_unverified_runtime_is_never_attempted(self) -> None:
        calls: list[int] = []
        with mock.patch.object(retirement, "resume_block_reason", return_value="resume_unverified"):
            result = retirement.restore_session(
                "pi:eeee", lambda: calls.append(1), runtime_id="pi", session_id="eeee", now=NOW,
            )
        self.assertEqual((result.state, calls), ("unresolvable", []))
        self.assertEqual(retirement.read_records()[-1].reason, "resume_unverified")

    def test_a_runtime_missing_from_this_machine_is_never_attempted(self) -> None:
        # The machine probe, not the retry sequence: a runtime whose executable
        # is not on PATH cannot be guaranteed to resume, so nothing is tried.
        calls: list[int] = []
        with mock.patch.object(retirement.shutil, "which", return_value=None):
            result = retirement.restore_session(
                "pi:hhhh", lambda: calls.append(1), runtime_id="pi", session_id="hhhh",
                history_path=__file__, cwd="/tmp", now=NOW,
            )
        self.assertEqual((result.state, result.cause, calls), ("unresolvable", "resume_unverified", []))
        self.assertEqual(retirement.load_resume_retries(), {})
        self.assertEqual(retirement.read_records()[-1].reason, "resume_unverified")

    def test_the_rate_cap_is_three_in_flight_and_not_a_total(self) -> None:
        calls: list[str] = []
        blocked = retirement.restore_session(
            "pi:ffff", lambda: calls.append("x"), runtime_id="pi", session_id="f",
            in_flight=retirement.RESTORE_IN_FLIGHT_MAX,
        )
        self.assertEqual((blocked.state, calls), ("rate_limited", []))
        allowed = retirement.restore_session(
            "pi:ffff", lambda: calls.append("x"), runtime_id="pi", session_id="f",
            in_flight=retirement.RESTORE_IN_FLIGHT_MAX - 1, history_path=__file__,
        )
        self.assertEqual(allowed.state, "restored")
        self.assertEqual(calls, ["x"])

    def test_nothing_gates_the_restore_on_a_setting(self) -> None:
        # §7.6 supersedes D3: no env knob can disable the healing path.
        calls: list[int] = []
        with mock.patch.dict(
            os.environ,
            {"CORRAL_CACHE_DIR": self.temp.name, "CORRAL_RECLAIM_RESUME_ON_SERVER_LOSS": "0"},
            clear=True,
        ):
            result = retirement.restore_session(
                "pi:gggg", lambda: calls.append(1), history_path=__file__,
            )
        self.assertEqual((result.state, calls), ("restored", [1]))

    def test_restore_order_is_working_then_waiting_then_the_rest(self) -> None:
        entries = [
            {"key": "a", "state": "parked", "retired_at": 30},
            {"key": "b", "state": "waiting_on_user", "retired_at": 20},
            {"key": "c", "state": "working", "retired_at": 10},
            {"key": "d", "state": "working", "retired_at": 40},
        ]
        ordered = [entry["key"] for entry in retirement.ordered_restore_candidates(entries)]
        self.assertEqual(ordered, ["d", "c", "b", "a"])


# ------------------------------------------------------------ pass-level integration


class PassHarness(CacheIsolated):
    """One reclaim pass with fake hosts and no tmux contact at all."""

    def setUp(self) -> None:
        super().setUp()
        self.hosts: list[Hosted] = []
        self.sessions: list[dict] = []
        self.killed: list[str] = []
        self.events: list[tuple] = []

        def kill(host):
            self.killed.append(host.name)
            return True

        # Captured before the patch below, so the stand-in can still write durably.
        self.append = retirement.append_record

        def record_event(name, **fields):
            self.events.append((name, fields))

        for patcher in (
            mock.patch.object(reclaim, "probe_hosted", side_effect=lambda: list(self.hosts)),
            mock.patch.object(reclaim, "memory_pressure", return_value=False),
            mock.patch.object(reclaim, "pressure_facts", return_value={"swap_ratio": None, "pressure_level": None}),
            mock.patch.object(reclaim, "_background_state", return_value=reclaim.BackgroundState()),
            mock.patch.object(reclaim, "_kill_direct", side_effect=self._kill_direct),
            mock.patch.object(reclaim, "_after_kill"),
            mock.patch.object(reclaim, "_STARTED_AT", time.monotonic() - 1000),
            mock.patch.object(reclaim.observe, "event", side_effect=record_event),
            mock.patch.object(reclaim.hosted_lifecycle, "refresh_inventory"),
            mock.patch.object(reclaim.retirement, "pane_identity",
                              return_value=retirement.PaneIdentity(pane_pid=7, server_pid=8)),
            mock.patch.object(reclaim.retirement, "resume_block_reason", return_value=None),
            mock.patch.object(reclaim.retirement, "append_record", side_effect=self._record),
            mock.patch("corral.attention.AttentionStore.busy_pairs", return_value=[]),
            mock.patch("corral.attention.AttentionStore.waiting_pairs", return_value=[]),
            mock.patch("corral.embed.live_host_viewer_names", return_value=set()),
            mock.patch("corral.split_layout.pinned_keys_effective", return_value=set()),
            mock.patch("corral.control_store.RunStore.active_hosts", return_value=set()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.records: list[retirement.RetirementRecord] = []
        self._kill = kill

    def _record(self, record):
        self.records.append(record)
        return self.append(record)

    def _kill_direct(self, name, socket):
        self.killed.append(name)
        return True

    def add_host(self, name: str, *, idle_minutes: float, **overrides) -> Hosted:
        when = time.time() - idle_minutes * MIN
        fields = dict(
            name=name, runtime_id="pi", ident=name.rsplit("-", 1)[-1], socket="corral-keepalive",
            created_at=when - HOUR, session_activity=when, window_activity=when,
        )
        fields.update(overrides)
        session_host = Hosted(**fields)
        self.hosts.append(session_host)
        history = Path(self.temp.name) / f"{session_host.ident}.jsonl"
        history.write_text('{"type":"message"}\n', encoding="utf-8")
        self.sessions.append({
            "source": "pi", "id": session_host.ident + "-full", "keepalive_name": session_host.name,
            "status_tag": STATUS_DONE, "event_time": when, "mtime": when, "file_mtime": when,
            "path": str(history), "cwd": self.temp.name,
        })
        return session_host

    def run_pass(self, now: float) -> list[str]:
        return reclaim.maybe_reclaim(lambda: [dict(session) for session in self.sessions], now=now)


class HysteresisPassTests(PassHarness):
    def test_a_single_pressure_blip_never_retires(self) -> None:
        self.add_host("corral-pi-aaaa1111", idle_minutes=60)
        with mock.patch.object(reclaim, "memory_pressure", return_value=True):
            self.assertEqual(self.run_pass(time.time()), [])
        self.assertFalse(reclaim.last_pass().pressure)
        with mock.patch.object(reclaim, "memory_pressure", return_value=False):
            self.assertEqual(self.run_pass(time.time() + 61), [])
        self.assertEqual(self.killed, [])

    def test_two_armed_reads_retire_earlier_than_the_normal_threshold(self) -> None:
        self.add_host("corral-pi-bbbb2222", idle_minutes=60)
        with mock.patch.object(reclaim, "memory_pressure", return_value=True):
            self.assertEqual(self.run_pass(time.time()), [])
            reclaimed = self.run_pass(time.time() + 61)
        self.assertEqual(reclaimed, ["corral-pi-bbbb2222"])
        self.assertTrue(reclaim.last_pass().pressure)
        self.assertEqual(reclaim.last_pass().effective_idle_minutes, 30.0)
        self.assertEqual(reclaim.policy_view()["threshold_reason"], "pressure")

    def test_budget_overrun_alone_never_shortens_the_threshold(self) -> None:
        for index in range(15):
            self.add_host(f"corral-pi-cccc{index:04d}", idle_minutes=60)
        self.assertEqual(self.run_pass(time.time()), [])
        report = reclaim.last_pass()
        assert report is not None
        self.assertTrue(report.over_budget)
        self.assertFalse(report.pressure)
        self.assertEqual(report.effective_idle_minutes, 120.0)
        self.assertEqual(report.threshold_reason, "default")
        self.assertEqual(self.killed, [])
        blocked = [fields for name, fields in self.events if name == "reclaim_blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0]["blocked"].get("recent_activity"), 15)
        self.assertEqual(blocked[0]["parked"], 15)

    def test_the_ladder_steps_one_per_armed_pass(self) -> None:
        for index in range(15):
            self.add_host(f"corral-pi-dddd{index:04d}", idle_minutes=55)
        effective: list[float] = []
        with mock.patch.dict(os.environ, {"CORRAL_RECLAIM_PRESSURE_IDLE_MINUTES": "60"}), \
                mock.patch.object(reclaim, "memory_pressure", return_value=True):
            for step in range(5):
                self.run_pass(time.time() + step * 61)
                effective.append(reclaim.last_pass().effective_idle_minutes)
        self.assertEqual(effective, [120.0, 60.0, 60.0, 60.0, 45.0])
        self.assertTrue(self.killed, "the ladder must converge on the 55-minute sessions")

    def test_inv13_cooldown_blocks_a_second_retirement(self) -> None:
        self.add_host("corral-pi-eeee5555", idle_minutes=180)
        first = self.run_pass(time.time())
        self.assertEqual(first, ["corral-pi-eeee5555"])
        second = self.run_pass(time.time() + 61)
        self.assertEqual(second, [])
        report = reclaim.last_pass()
        assert report is not None
        self.assertEqual(report.blocked.get("cooldown"), 1)
        self.assertEqual(len(self.killed), 1)


# ----------------------------------------------- §6.2 pi: retire then really resume


class PiResumeRealTmuxTests(unittest.TestCase):
    """End to end on a private socket: a pi session is retired and resumed."""

    NAME = "corral-pi-abcd1234"

    def setUp(self) -> None:
        if shutil.which("tmux") is None:
            self.skipTest("tmux not installed")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.socket = f"rc-resume-{os.getpid()}-{int(time.time() * 1000) % 100000}"
        self.bin = Path(self.temp.name) / "bin"
        self.bin.mkdir()
        self.argv_log = Path(self.temp.name) / "pi-argv.txt"
        stub = self.bin / "pi"
        stub.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$CORRAL_TEST_ARGV\"\nsleep 600\n", encoding="utf-8",
        )
        stub.chmod(0o755)
        env = {"CORRAL_CACHE_DIR": self.temp.name, "CORRAL_TEST_ARGV": str(self.argv_log),
               "PATH": f"{self.bin}:{os.environ.get('PATH', '')}"}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("CORRAL_ISOLATE_MANAGED_HOSTS", None)
        self.env = keepalive.tmux_env()
        self.history = Path(self.temp.name) / "abcd1234.jsonl"
        self.history.write_text('{"type": "message", "id": "m1"}\n', encoding="utf-8")
        self.digest = hashlib.sha256(self.history.read_bytes()).hexdigest()
        self.addCleanup(self._kill_server)
        for patch in (
            mock.patch.object(reclaim, "_SOCKETS", (self.socket,)),
            mock.patch.object(reclaim, "_STARTED_AT", time.monotonic() - 1000),
            mock.patch.object(reclaim, "memory_pressure", return_value=False),
            mock.patch.object(reclaim.hosted_lifecycle, "refresh_inventory"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        split_layout.reset_default_layout_db()
        self.addCleanup(split_layout.reset_default_layout_db)
        self._tmux("-f", "/dev/null", "new-session", "-d", "-s", self.NAME, "-x", "80", "-y", "24",
                   "--", "sleep", "600")

    def _tmux(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux", "-L", self.socket, *args], capture_output=True, text=True, env=self.env, timeout=15,
        )

    def _kill_server(self) -> None:
        self._tmux("kill-server")
        with contextlib.suppress(OSError):
            os.unlink(f"/tmp/tmux-{os.getuid()}/{self.socket}")

    def test_retire_keeps_history_and_the_recorded_plan_resumes_it(self) -> None:
        created = float(self._tmux("list-sessions", "-F", "#{session_created}").stdout.split()[0])
        old = created - 3 * HOUR
        session = {
            "source": "pi", "id": "abcd1234-full-id", "keepalive_name": self.NAME,
            "status_tag": STATUS_DONE, "event_time": old, "mtime": old, "file_mtime": old,
            "path": str(self.history), "cwd": self.temp.name,
        }
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            # Look three hours ahead: tmux stamped the pane "now", so the
            # creation grace and the 120-minute threshold need a later clock.
            reclaimed = reclaim.maybe_reclaim(lambda: [session], now=time.time() + 3 * HOUR)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))
        self.assertEqual(reclaimed, [self.NAME])
        self.assertNotIn(self.NAME, self._tmux("list-sessions", "-F", "#{session_name}").stdout)

        # §10.2 item 1: the history file is byte-identical after retirement.
        self.assertEqual(hashlib.sha256(self.history.read_bytes()).hexdigest(), self.digest)

        record = retirement.read_records()[-1]
        self.assertEqual((record.name, record.runtime_id, record.outcome), (self.NAME, "pi", "retired"))
        self.assertTrue(record.resume_ready)
        plan = retirement.resume_plan(record)
        assert plan is not None
        self.assertEqual(tuple(plan.argv), tuple(record.resume_argv))
        self.assertEqual(plan.argv[0], "pi")
        self.assertIn("--session", plan.argv)
        self.assertIn(str(self.history), plan.argv)

        # §8.4: resuming is the ordinary native path, here the recorded argv
        # against a stand-in `pi` so no real agent or quota is involved.
        resumed = subprocess.run(
            ["tmux", "-L", self.socket, "new-session", "-d", "-s", record.name,
             "-c", record.resume_cwd or self.temp.name, "--", *record.resume_argv],
            capture_output=True, text=True, env=self.env, timeout=15,
        )
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        for _ in range(50):
            if self.argv_log.exists():
                break
            time.sleep(0.1)
        self.assertTrue(self.argv_log.exists(), "the resume command never ran")
        argv = self.argv_log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(argv[0], "--approve")
        self.assertEqual(argv[1], "--session")
        self.assertEqual(argv[2], str(self.history))
        self.assertIn(self.NAME, self._tmux("list-sessions", "-F", "#{session_name}").stdout)


if __name__ == "__main__":
    unittest.main()
