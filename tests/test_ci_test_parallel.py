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
        # 新计时/覆盖字段：跑了多少 id 就回报多少 id。
        self.assertEqual(len(shard.test_ids), shard.tests_run)
        self.assertTrue(shard.test_ids[0].startswith("test_i18n."))
        self.assertGreaterEqual(len(shard.class_seconds), 1)

    def test_run_names_load_failure_is_structured(self) -> None:
        # unittest 把缺失模块变成 _FailedTest 占位用例：失败带确定性 id，
        # 覆盖审计会把它判为 extra（相对枚举基准），整轮硬失败。
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
        module_shards, class_shards, remainder = ci_test._plan_shards(id_map, None)
        self.assertEqual(module_shards, [["test_fast"]])
        self.assertEqual(class_shards, [])
        self.assertEqual(remainder, ["test_ui"])

    def test_plan_shards_partial_proof_splits_remainder_by_class(self) -> None:
        id_map = {
            "test_ui": {
                "test_ui.Safe": ["test_ui.Safe.test_1"],
                "test_ui.Unproven": ["test_ui.Unproven.test_1"],
            },
        }
        module_shards, class_shards, remainder = ci_test._plan_shards(
            id_map, frozenset({"test_ui.Safe", "test_other.Ghost"})
        )
        # 部分证明：已证明类进隔离 shard，未证明类逐类进串行余量——
        # 余量若用整模块名会与 shard 重复执行 Safe。
        self.assertEqual(module_shards, [])
        self.assertEqual(class_shards, [["test_ui.Safe"]])
        self.assertEqual(remainder, ["test_ui.Unproven"])

    def test_plan_shards_stale_manifest_entries_ignored(self) -> None:
        id_map = {"test_ui": {"test_ui.Safe": ["test_ui.Safe.test_1"]}}
        _, class_shards, remainder = ci_test._plan_shards(
            id_map, frozenset({"test_ui.Safe", "test_ui.Renamed", "test_gone.C"})
        )
        self.assertEqual(class_shards, [["test_ui.Safe"]])
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
        # 模拟 shard 回报：隔离 shard 跑 Safe，余量工序跑 Unproven。
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
        # 若余量误用整模块名，Safe 会被跑两次 → audit 判 extra。
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
        # 类键是 module.Class（一层剥离）；双层剥离会退化成模块名，
        # 导致分片名单永远对不上（回归：2026-09-30 跟进修）。
        self.assertEqual(len(result.class_seconds), 1)
        self.assertIn("_Sample", next(iter(result.class_seconds)))


if __name__ == "__main__":
    unittest.main()
