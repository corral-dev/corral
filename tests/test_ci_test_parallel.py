"""ci-test parallel sharding helpers (no full suite)."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import unittest

SCRIPT_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "ci-test.py"
SPEC = importlib.util.spec_from_file_location("ci_test", SCRIPT_PATH)
assert SPEC and SPEC.loader
ci_test = importlib.util.module_from_spec(SPEC)
# dataclasses need the module registered before exec_module.
sys.modules[SPEC.name] = ci_test
SPEC.loader.exec_module(ci_test)


class CiTestParallelHelpers(unittest.TestCase):
    def test_serial_lane_covers_ui_and_embed(self) -> None:
        self.assertIn("test_ui", ci_test._SERIAL_MODULES)
        self.assertIn("test_embed", ci_test._SERIAL_MODULES)
        self.assertNotIn("test_remote_crypto", ci_test._SERIAL_MODULES)

    def test_discover_modules_lists_test_files(self) -> None:
        modules = ci_test._discover_modules()
        self.assertIn("test_ui", modules)
        self.assertIn("test_ci_stamp", modules)
        self.assertEqual(modules, sorted(modules))

    def test_parse_worker_output_reads_result_line(self) -> None:
        payload = {
            "ok": False,
            "failed_ids": ["test_x.T.test_a"],
            "tests_run": 3,
            "seconds": 1.25,
            "modules": ["test_x"],
        }
        blob = (
            "ok tests...\n"
            f"{ci_test._RESULT_PREFIX}{json.dumps(payload)}\n"
        )
        shard = ci_test._parse_worker_output(blob, ["test_x"], returncode=1)
        self.assertFalse(shard.ok)
        self.assertEqual(shard.failed_ids, ("test_x.T.test_a",))
        self.assertEqual(shard.tests_run, 3)
        self.assertIn("ok tests...", shard.output)
        self.assertNotIn(ci_test._RESULT_PREFIX, shard.output)

    def test_parse_worker_output_missing_marker_is_hard_fail(self) -> None:
        shard = ci_test._parse_worker_output("boom\n", ["test_x"], returncode=1)
        self.assertFalse(shard.ok)
        self.assertEqual(shard.failed_ids, ())
        self.assertIn("boom", shard.output)

    def test_run_modules_loads_top_level_test_module(self) -> None:
        shard = ci_test._run_modules_in_process(["test_i18n"])
        self.assertTrue(shard.ok, shard.output[-500:])
        self.assertGreaterEqual(shard.tests_run, 1)
        self.assertEqual(shard.modules, ("test_i18n",))
        # New timing/coverage fields: report exactly the ids that ran.
        self.assertEqual(len(shard.test_ids), shard.tests_run)
        self.assertTrue(shard.test_ids[0].startswith("test_i18n."))
        self.assertGreaterEqual(len(shard.class_seconds), 1)

    def test_run_names_load_failure_is_structured(self) -> None:
        # unittest turns a missing module into a _FailedTest placeholder: the
        # failure carries a deterministic id, and the coverage audit flags it
        # as extra against the enumeration baseline (whole run hard-fails).
        shard = ci_test._run_names_in_process(["test_no_such_module_xyz"])
        self.assertFalse(shard.ok)
        self.assertEqual(
            shard.failed_ids,
            ("unittest.loader._FailedTest.test_no_such_module_xyz",),
        )
        ok, _, extra = ci_test._audit_coverage({}, [shard])
        self.assertFalse(ok)
        self.assertEqual(extra, list(shard.failed_ids))

    def test_plan_shards_without_helper_is_legacy(self) -> None:
        id_map = {
            "test_ui": {"test_ui.A": ["test_ui.A.test_1"]},
            "test_fast": {"test_fast.B": ["test_fast.B.test_1"]},
        }
        batches, workers, remainder = ci_test._plan_shards(id_map, None)
        self.assertEqual(batches, [["test_fast"]])
        self.assertEqual(workers, [])
        self.assertEqual(remainder, ["test_ui"])

    def test_plan_shards_partial_proof_splits_remainder_by_class(self) -> None:
        id_map = {
            "test_ui": {
                "test_ui.Safe": ["test_ui.Safe.test_1"],
                "test_ui.Unproven": ["test_ui.Unproven.test_1"],
            },
        }
        batches, workers, remainder = ci_test._plan_shards(
            id_map, frozenset({"test_ui.Safe", "test_other.Ghost"})
        )
        # Partial proof: proven classes go to isolated shards, unproven ones
        # enter the serial remainder by class -- a whole-module remainder would
        # re-run Safe a second time.
        self.assertEqual(batches, [])
        self.assertEqual(workers, [["test_ui.Safe"]])
        self.assertEqual(remainder, ["test_ui.Unproven"])

    def test_plan_shards_stale_manifest_entries_ignored(self) -> None:
        id_map = {"test_ui": {"test_ui.Safe": ["test_ui.Safe.test_1"]}}
        _, workers, remainder = ci_test._plan_shards(
            id_map, frozenset({"test_ui.Safe", "test_ui.Renamed", "test_gone.C"})
        )
        self.assertEqual(workers, [["test_ui.Safe"]])
        self.assertEqual(remainder, [])

    def test_split_module_audit_exactly_once(self) -> None:
        id_map = {
            "test_ui": {
                "test_ui.Safe": ["test_ui.Safe.test_1"],
                "test_ui.Unproven": ["test_ui.Unproven.test_1"],
            },
        }
        module_shards, class_shards, remainder = ci_test._plan_shards(
            id_map, frozenset({"test_ui.Safe"})
        )
        self.assertEqual(module_shards, [])
        self.assertEqual(class_shards, [["test_ui.Safe"]])
        self.assertEqual(remainder, ["test_ui.Unproven"])
        # Simulated shard reports: isolated shard runs Safe, remainder runs Unproven.
        safe_shard = self._shard(
            modules=("test_ui.Safe",),
            isolated=True,
            test_ids=("test_ui.Safe.test_1",),
        )
        rem_shard = self._shard(
            modules=("test_ui.Unproven",),
            isolated=False,
            test_ids=("test_ui.Unproven.test_1",),
        )
        ok, missing, extra = ci_test._audit_coverage(
            id_map, [safe_shard, rem_shard]
        )
        self.assertTrue(ok)
        self.assertEqual(missing, [])
        self.assertEqual(extra, [])
        # If the remainder wrongly used the whole module name, Safe would run
        # twice and the audit must flag extra.
        dup_shard = self._shard(
            modules=("test_ui",),
            test_ids=("test_ui.Safe.test_1", "test_ui.Unproven.test_1"),
        )
        ok, _, extra = ci_test._audit_coverage(id_map, [safe_shard, dup_shard])
        self.assertFalse(ok)
        self.assertIn("test_ui.Safe.test_1", extra)

    def test_plan_shards_fully_proven_module_splits(self) -> None:
        id_map = {
            "test_ui": {
                "test_ui.Safe1": ["test_ui.Safe1.test_1"],
                "test_ui.Safe2": ["test_ui.Safe2.test_1"],
            },
            "test_embed": {"test_embed.C": ["test_embed.C.test_1"]},
        }
        module_shards, class_shards, remainder = ci_test._plan_shards(
            id_map, frozenset({"test_ui.Safe1", "test_ui.Safe2"})
        )
        self.assertEqual(module_shards, [])
        self.assertEqual(
            sorted(class_shards), [["test_ui.Safe1"], ["test_ui.Safe2"]]
        )
        self.assertEqual(remainder, ["test_embed"])

    def test_plan_batches_ordinary_modules_deterministically(self) -> None:
        mods = {f"test_m{i:02d}": {f"test_m{i:02d}.C": [f"test_m{i:02d}.C.test_1"]}
                for i in range(10)}
        batches, workers, remainder = ci_test._plan_shards(mods, None)
        self.assertEqual(workers, [])
        self.assertEqual(remainder, [])
        flat = [m for b in batches for m in b]
        self.assertEqual(flat, sorted(mods))
        self.assertTrue(all(len(b) <= ci_test.MODULE_BATCH_SIZE for b in batches))
        # Stable across calls (deterministic input order, no hashing).
        again, _, _ = ci_test._plan_shards(mods, None)
        self.assertEqual(again, batches)

    def test_plan_groups_only_manifest_allowlisted_classes(self) -> None:
        id_map = {
            "test_ui": {
                "test_ui.G1": ["test_ui.G1.test_1", "test_ui.G1.test_2"],
                "test_ui.G2": ["test_ui.G2.test_1"],
                "test_ui.Solo": ["test_ui.Solo.test_1"],
            },
        }
        safe = frozenset({"test_ui.G1", "test_ui.G2", "test_ui.Solo"})
        _, workers, remainder = ci_test._plan_shards(
            id_map, safe, groupable=frozenset({"test_ui.G1", "test_ui.G2"})
        )
        self.assertEqual(workers, [["test_ui.Solo"], ["test_ui.G1", "test_ui.G2"]])
        self.assertEqual(remainder, [])

    def test_plan_groupable_requires_safe_membership(self) -> None:
        id_map = {
            "test_ui": {"test_ui.G1": ["test_ui.G1.test_1"]},
        }
        # G1 allowlisted for grouping but NOT proven safe: must stay serial.
        _, workers, remainder = ci_test._plan_shards(
            id_map, frozenset(), groupable=frozenset({"test_ui.G1"})
        )
        self.assertEqual(workers, [])
        self.assertEqual(remainder, ["test_ui"])

    def test_plan_group_cap_splits_large_groups(self) -> None:
        classes = {f"test_ui.G{i:02d}": [f"test_ui.G{i:02d}.test_1"] for i in range(30)}
        id_map = {"test_ui": classes}
        safe = frozenset(classes)
        _, workers, _ = ci_test._plan_shards(id_map, safe, groupable=safe)
        self.assertEqual(len(workers), 2)
        flat = sorted(c for w in workers for c in w)
        self.assertEqual(flat, sorted(classes))
        self.assertTrue(
            all(len(w) <= ci_test.GROUP_MAX_CLASSES for w in workers)
        )

    def test_plan_splits_heavyweight_class_into_method_chunks(self) -> None:
        ids = [f"test_ui.Whale.test_{i:02d}" for i in range(6)]
        id_map = {"test_ui": {"test_ui.Whale": ids}}
        _, workers, remainder = ci_test._plan_shards(
            id_map, frozenset({"test_ui.Whale"}),
            splittable={"test_ui.Whale": 3},
        )
        self.assertEqual(len(workers), 3)
        flat = sorted(t for w in workers for t in w)
        self.assertEqual(flat, ids)
        for chunk in workers:
            self.assertEqual(chunk, sorted(chunk))
        self.assertEqual(remainder, [])
        # Chunks audit exactly-once against the class method set.
        ok, missing, extra = ci_test._audit_coverage(
            id_map,
            [self._shard(modules=(f"chunk{i}",), test_ids=tuple(c))
             for i, c in enumerate(workers)],
        )
        self.assertTrue(ok)
        self.assertEqual(missing, [])
        self.assertEqual(extra, [])

    def test_plan_split_ignored_without_safe_or_when_stale(self) -> None:
        id_map = {"test_ui": {"test_ui.Whale": ["test_ui.Whale.test_1"]}}
        # Not proven safe: no split, serial remainder.
        _, workers, remainder = ci_test._plan_shards(
            id_map, frozenset(), splittable={"test_ui.Whale": 2}
        )
        self.assertEqual(workers, [])
        self.assertEqual(remainder, ["test_ui"])
        # Stale manifest class: ignored, remainder keeps the whole module.
        _, workers, remainder = ci_test._plan_shards(
            id_map,
            frozenset({"test_ui.Whale"}),
            splittable={"test_ui.Ghost": 2},
        )
        self.assertEqual(workers, [["test_ui.Whale"]])
        self.assertEqual(remainder, [])

    def test_order_phase2_heavier_first(self) -> None:
        id_map = {
            "test_big": {"test_big.C": [f"test_big.C.test_{i}" for i in range(10)]},
            "test_small": {"test_small.C": ["test_small.C.test_1"]},
            "test_ui": {"test_ui.W": [f"test_ui.W.test_{i}" for i in range(5)]},
        }
        ordered = ci_test._order_phase2(
            [["test_small"], ["test_big"]], [["test_ui.W"]], id_map
        )
        # UI workers submit before module batches (a UI class can cost a
        # minute; a whole ordinary module costs seconds), each tier heavier
        # first by live test count.
        self.assertEqual(
            ordered,
            [
                ("--run-shard", ["test_ui.W"]),
                ("--run-modules", ["test_big"]),
                ("--run-modules", ["test_small"]),
            ],
        )

    def test_group_blocks_respect_caps(self) -> None:
        id_map = {
            "test_ui": {f"test_ui.G{i:02d}": [f"test_ui.G{i:02d}.test_1"] for i in range(30)}
        }
        blocks = ci_test._group_blocks(sorted(id_map["test_ui"]), id_map)
        self.assertEqual(len(blocks), 2)
        flat = sorted(c for b, _ in blocks for c in b)
        self.assertEqual(flat, sorted(id_map["test_ui"]))
        self.assertTrue(all(len(b) <= ci_test.GROUP_MAX_CLASSES for b, _ in blocks))
        self.assertTrue(all(t <= ci_test.GROUP_MAX_TESTS for _, t in blocks))

    def test_kind_for_labels(self) -> None:
        self.assertEqual(
            ci_test._kind_for("--run-shard", ["test_ui.Whale.test_01"]), "ui-chunk"
        )
        self.assertEqual(ci_test._kind_for("--run-shard", ["test_ui.W"]), "ui-shard")
        self.assertEqual(
            ci_test._kind_for("--run-shard", ["test_ui.A", "test_ui.B"]), "ui-group"
        )
        self.assertEqual(ci_test._kind_for("--run-modules", ["test_x"]), "shard")

    def test_phase2_uses_full_jobs_budget_after_serial_remainder(self) -> None:
        # Phase 1 (serial remainder) is a blocking call that finishes before
        # the Phase-2 pool exists, so Phase 2 must use the full jobs budget
        # (regression: an obsolete jobs-1 reserve left one slot idle).
        import concurrent.futures as real_futures
        from unittest import mock

        events: list[tuple[str, tuple[str, ...]]] = []
        pools: list[int] = []
        real_pool = real_futures.ThreadPoolExecutor

        def pool_factory(max_workers: int) -> object:
            pools.append(max_workers)
            return real_pool(max_workers=max_workers)

        def fake_spawn_worker(
            flag: str, spec: list[str], kind: str = "shard"
        ) -> object:
            events.append((kind, tuple(spec)))
            return self._shard(modules=tuple(spec), tests_run=0, test_ids=())

        id_map = {
            "test_ui": {
                "test_ui.W": ["test_ui.W.test_1", "test_ui.W.test_2"],
                "test_ui.U": ["test_ui.U.test_1"],
            },
            "test_fast": {"test_fast.C": ["test_fast.C.test_1"]},
        }
        with (
            mock.patch.object(ci_test, "_list_all_ids", return_value=id_map),
            mock.patch.object(
                ci_test, "_load_safe_classes", return_value=frozenset({"test_ui.W"})
            ),
            mock.patch.object(
                ci_test, "_load_batch_manifest", return_value=(None, None)
            ),
            mock.patch.object(ci_test, "_spawn_worker", side_effect=fake_spawn_worker),
            mock.patch.object(
                ci_test,
                "_spawn_shard",
                side_effect=lambda spec: fake_spawn_worker("--run-modules", spec),
            ),
            mock.patch.object(
                ci_test.concurrent.futures,
                "ThreadPoolExecutor",
                side_effect=pool_factory,
            ),
            mock.patch.object(
                ci_test, "_audit_coverage", return_value=(True, [], [])
            ),
            mock.patch.object(ci_test, "_emit_machine_report", return_value={}),
        ):
            ok, failed = ci_test._run_suite_parallel(jobs=3)
        self.assertTrue(ok)
        self.assertEqual(failed, [])
        # Serial remainder ran to completion before any Phase-2 submission.
        self.assertEqual(events[0], ("serial-remainder", ("test_ui.U",)))
        self.assertEqual(
            sorted(events[1:]),
            sorted(
                [
                    ("shard", ("test_fast",)),
                    ("ui-shard", ("test_ui.W",)),
                ]
            ),
        )
        # Exactly one pool, sized to the full jobs budget.
        self.assertEqual(pools, [3])

    def test_machine_report_counts_retry(self) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            report = ci_test._emit_machine_report(
                phase="final",
                wall=10.0,
                jobs=6,
                shards=[self._shard()],
                expected_total=1,
                retried=3,
                retry_ok=True,
                coverage_ok=True,
                missing=[],
                extra=[],
            )
        self.assertEqual(report["phase"], "final")
        self.assertEqual(report["retried"], 3)
        self.assertTrue(report["retry_ok"])
        line = next(
            ln for ln in buf.getvalue().splitlines() if "CORRAL_CI_TEST_REPORT:" in ln
        )
        self.assertEqual(json.loads(line.split("CORRAL_CI_TEST_REPORT:")[1])["retried"], 3)

    def _shard(self, **kwargs: object) -> object:
        base = {
            "modules": ("m",),
            "ok": True,
            "failed_ids": (),
            "tests_run": 1,
            "seconds": 0.1,
            "output": "",
            "returncode": 0,
            "test_ids": ("test_m.C.test_1",),
        }
        base.update(kwargs)
        return ci_test._ShardResult(**base)  # type: ignore[arg-type]

    def test_audit_coverage_exact_match(self) -> None:
        id_map = {"test_m": {"test_m.C": ["test_m.C.test_1"]}}
        ok, missing, extra = ci_test._audit_coverage(id_map, [self._shard()])
        self.assertTrue(ok)
        self.assertEqual(missing, [])
        self.assertEqual(extra, [])

    def test_audit_coverage_missing_and_extra(self) -> None:
        id_map = {"test_m": {"test_m.C": ["test_m.C.test_1", "test_m.C.test_2"]}}
        ok, missing, extra = ci_test._audit_coverage(
            id_map, [self._shard(test_ids=("test_m.C.test_1", "test_m.C.test_9"))]
        )
        self.assertFalse(ok)
        self.assertEqual(missing, ["test_m.C.test_2"])
        self.assertEqual(extra, ["test_m.C.test_9"])

    def test_audit_coverage_duplicate_run_is_extra(self) -> None:
        id_map = {"test_m": {"test_m.C": ["test_m.C.test_1"]}}
        ok, _, extra = ci_test._audit_coverage(
            id_map, [self._shard(test_ids=("test_m.C.test_1", "test_m.C.test_1"))]
        )
        self.assertFalse(ok)
        self.assertEqual(extra, ["test_m.C.test_1"])

    def test_parse_worker_output_reads_new_fields(self) -> None:
        payload = {
            "ok": True,
            "failed_ids": [],
            "tests_run": 2,
            "seconds": 0.5,
            "modules": ["test_m.C"],
            "skipped": 1,
            "isolated": True,
            "class_seconds": {"test_m.C": 0.4},
            "slowest": [["test_m.C.test_1", 0.3]],
            "test_ids": ["test_m.C.test_1", "test_m.C.test_2"],
        }
        blob = f"{ci_test._RESULT_PREFIX}{json.dumps(payload)}\n"
        shard = ci_test._parse_worker_output(blob, ["test_m.C"], returncode=0)
        self.assertTrue(shard.ok)
        self.assertEqual(shard.skipped, 1)
        self.assertTrue(shard.isolated)
        self.assertEqual(shard.class_seconds, (("test_m.C", 0.4),))
        self.assertEqual(len(shard.test_ids), 2)

    def test_final_wall_covers_retry_on_main_path(self) -> None:
        # Main-path regression (no sleeps, fully stubbed clocks): with the
        # first pass stashed at suite_start=900 and perf_counter reading 990
        # at main's wall0, 995 at the first-pass print, then 1000 after the
        # stubbed retry, the emitted
        # final line must report wall 100.0 (retry included, not the
        # first-pass reuse) with truthful retried/retry_ok.
        import io
        from contextlib import redirect_stdout
        from unittest import mock

        failed = ["test_m.C.test_1"]
        first_pass_ctx = {
            "suite_start": 900.0,
            "wall": 60.0,
            "jobs": 6,
            "shards": [self._shard()],
            "expected_total": 1,
            "coverage_ok": True,
            "missing": [],
            "extra": [],
        }
        with (
            mock.patch.object(ci_test, "_maybe_use_checkout_env"),
            mock.patch.object(ci_test, "_run_ruff", return_value=0),
            mock.patch.object(ci_test, "_fingerprints", return_value=("fp", "env")),
            mock.patch.object(
                ci_test,
                "_run_suite_parallel",
                side_effect=lambda jobs: (
                    ci_test._LAST_FIRST_PASS.update(first_pass_ctx),
                    (False, failed),
                )[1],
            ),
            mock.patch.object(ci_test, "_retry_failed", return_value=True) as retry,
            mock.patch.object(ci_test, "_record_success") as record,
            mock.patch.object(
                ci_test.time, "perf_counter", side_effect=[990.0, 995.0, 1000.0]
            ),
        ):
            ci_test._LAST_FIRST_PASS.clear()
            buf = io.StringIO()
            try:
                with redirect_stdout(buf):
                    code = ci_test.main([])
            finally:
                ci_test._LAST_FIRST_PASS.clear()
        self.assertEqual(code, 0)
        retry.assert_called_once_with(failed)
        record.assert_called_once_with()
        finals = [
            json.loads(ln.split("CORRAL_CI_TEST_REPORT:")[1])
            for ln in buf.getvalue().splitlines()
            if "CORRAL_CI_TEST_REPORT:" in ln
        ]
        self.assertEqual(len(finals), 1)
        final = finals[0]
        self.assertEqual(final["phase"], "final")
        self.assertEqual(final["retried"], 1)
        self.assertTrue(final["retry_ok"])
        self.assertEqual(final["wall_seconds"], 100.0)

    def test_failure_output_contains_traceback(self) -> None:
        import io

        class _Fail(unittest.TestCase):
            def test_boom(self) -> None:
                raise AssertionError("synthetic diagnostic probe")

        buf = io.StringIO()
        result = unittest.TextTestRunner(
            stream=buf, verbosity=2, resultclass=ci_test._TimingResult
        ).run(unittest.TestLoader().loadTestsFromTestCase(_Fail))
        self.assertFalse(result.wasSuccessful())
        out = buf.getvalue()
        self.assertIn("FAIL:", out)
        self.assertIn("test_boom", out)
        self.assertIn("AssertionError: synthetic diagnostic probe", out)
        self.assertIn("Traceback", out)

    def test_error_output_contains_traceback(self) -> None:
        import io

        class _Err(unittest.TestCase):
            def test_kaboom(self) -> None:
                raise ValueError("synthetic error probe")

        buf = io.StringIO()
        result = unittest.TextTestRunner(
            stream=buf, verbosity=2, resultclass=ci_test._TimingResult
        ).run(unittest.TestLoader().loadTestsFromTestCase(_Err))
        self.assertFalse(result.wasSuccessful())
        out = buf.getvalue()
        self.assertIn("ERROR:", out)
        self.assertIn("ValueError: synthetic error probe", out)

    def test_emit_shard_failure_keeps_diagnostics_and_nonzero_exit(self) -> None:
        import io
        from contextlib import redirect_stdout

        failing = self._shard(
            ok=False,
            failed_ids=("test_m.C.test_1",),
            returncode=1,
            output=(
                "test_1 (test_m.C) ... FAIL\n"
                "Traceback (most recent call last):\n"
                "AssertionError: synthetic\n"
            ),
        )
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = ci_test._emit_shard(failing)
        self.assertNotEqual(code, 0)
        self.assertIn("Traceback", buf.getvalue())
        self.assertIn("CORRAL_CI_TEST_RESULT:", buf.getvalue())

    def test_final_wall_includes_retry_elapsed(self) -> None:
        import time

        ctx = {"suite_start": time.perf_counter() - 5.0, "wall": 4.0}
        final_wall = ci_test._final_wall_seconds(ctx)
        self.assertGreaterEqual(final_wall, 4.9)
        # The final line must cover suite start through end of retry;
        # reusing the first-pass wall clock is wrong.
        self.assertGreaterEqual(final_wall, ctx["wall"])

    def test_first_pass_report_phase_default(self) -> None:
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            report = ci_test._emit_machine_report(
                wall=10.0,
                jobs=6,
                shards=[self._shard()],
                expected_total=1,
                retried=0,
                retry_ok=False,
                coverage_ok=True,
                missing=[],
                extra=[],
            )
        self.assertEqual(report["phase"], "first-pass")
        self.assertEqual(report["retried"], 0)

    def test_timing_result_counts_skips(self) -> None:
        import io

        class _Sample(unittest.TestCase):
            def test_skip_me(self) -> None:
                self.skipTest("synthetic")

            def test_pass(self) -> None:
                pass

        suite = unittest.TestLoader().loadTestsFromTestCase(_Sample)
        buf = io.StringIO()
        result = unittest.TextTestRunner(
            stream=buf, verbosity=0, resultclass=ci_test._TimingResult
        ).run(suite)
        assert isinstance(result, ci_test._TimingResult)
        self.assertTrue(result.wasSuccessful())
        self.assertEqual(result.skipped_count, 1)
        self.assertEqual(result.testsRun, 2)
        # Class key must be module.Class (single strip); a double strip
        # degrades to the module name and the shard manifest can never match
        # (regression fixed 2026-09-30 follow-up).
        self.assertEqual(len(result.class_seconds), 1)
        self.assertIn("_Sample", next(iter(result.class_seconds)))


if __name__ == "__main__":
    unittest.main()
