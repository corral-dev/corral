#!/usr/bin/env python3
"""Pure helper tests for the remote session-open benchmark.

Only selection, percentile, size-class, redaction and rendering helpers are
covered. No real session history, cache, daemon or corral runtime is touched.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "remote_open_benchmark.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("remote_open_benchmark", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["remote_open_benchmark"] = module
    spec.loader.exec_module(module)
    return module


BENCH = _load_module()


def _key(session: dict) -> str:
    return f"{session['source']}:{session['id']}"


def _session(runtime: str, size_mb: float, ident: str, path: str | None = None) -> dict:
    return {
        "source": runtime,
        "id": ident,
        "size_kb": size_mb * 1024.0,
        "path": path or f"/private/secret/{runtime}/{ident}.jsonl",
        "title": f"secret title {ident}",
    }


class SampleSelectionTests(unittest.TestCase):
    def test_picks_largest_per_runtime_in_active_order(self) -> None:
        sessions = [
            _session("codex", 60.0, "c-small"),
            _session("claude", 30.0, "a-large"),
            _session("codex", 80.0, "c-large"),
            _session("claude", 5.0, "a-small"),
            _session("kimi", 999.0, "dormant"),
            _session("shell", 999.0, "plain"),
        ]
        selected = BENCH.select_samples(sessions, 1, _key)
        self.assertEqual([item.id for item in selected], ["claude#1", "codex#1"])
        self.assertEqual(selected[0].runtime, "claude")
        self.assertAlmostEqual(selected[0].size_mb, 30.0)
        self.assertAlmostEqual(selected[1].size_mb, 80.0)
        self.assertEqual(selected[1].size_class, "50-100MB")

    def test_selection_is_order_independent_and_ranked(self) -> None:
        sessions = [
            _session("pi", 10.0, "p1"),
            _session("pi", 40.0, "p2"),
            _session("pi", 20.0, "p3"),
            _session("cursor", 50.0, "c1"),
            _session("cursor", 2.0, "c2"),
        ]
        first = BENCH.select_samples(sessions, 2, _key)
        shuffled = BENCH.select_samples(list(reversed(sessions)), 2, _key)
        self.assertEqual([item.id for item in first], [item.id for item in shuffled])
        self.assertEqual([item.id for item in first], ["cursor#1", "cursor#2", "pi#1", "pi#2"])
        self.assertEqual([item.rank for item in first], [1, 2, 1, 2])
        self.assertAlmostEqual(first[0].size_mb, 50.0)
        self.assertAlmostEqual(first[1].size_mb, 2.0)
        self.assertAlmostEqual(first[2].size_mb, 40.0)

    def test_zero_samples_returns_empty(self) -> None:
        self.assertEqual(BENCH.select_samples([_session("codex", 1.0, "x")], 0, _key), [])

    def test_public_view_never_contains_key_path_or_title(self) -> None:
        session = _session("codex", 65.0, "0192-secret-uuid")
        sample = BENCH.select_samples([session], 1, _key)[0]
        payload = json.dumps(sample.public(), ensure_ascii=False)
        self.assertNotIn(sample.key, payload)
        self.assertNotIn(sample.path, payload)
        self.assertNotIn(session["title"], payload)
        self.assertNotIn("secret", payload)
        self.assertEqual(sample.public()["id"], "codex#1")
        self.assertEqual(sample.public()["size_class"], "50-100MB")


class SamplePairTests(unittest.TestCase):
    def test_prefers_two_different_runtimes(self) -> None:
        samples = [
            BENCH.Sample("cursor", 1, 127.0, "100MB+", "cursor:1", "/x/1"),
            BENCH.Sample("cursor", 2, 66.0, "50-100MB", "cursor:2", "/x/2"),
            BENCH.Sample("codex", 1, 63.0, "50-100MB", "codex:1", "/x/3"),
        ]
        pair = BENCH.sample_pair(samples)
        self.assertEqual([item.id for item in pair], ["cursor#1", "codex#1"])

    def test_falls_back_to_two_sessions_of_one_runtime(self) -> None:
        samples = [
            BENCH.Sample("cursor", 1, 127.0, "100MB+", "cursor:1", "/x/1"),
            BENCH.Sample("cursor", 2, 66.0, "50-100MB", "cursor:2", "/x/2"),
        ]
        pair = BENCH.sample_pair(samples)
        self.assertEqual([item.id for item in pair], ["cursor#1", "cursor#2"])

    def test_one_or_zero_sessions(self) -> None:
        only = [BENCH.Sample("pi", 1, 38.0, "10-50MB", "pi:1", "/x/1")]
        self.assertEqual(len(BENCH.sample_pair(only)), 1)
        self.assertEqual(BENCH.sample_pair([]), [])


class PercentileTests(unittest.TestCase):
    def test_interpolated_percentiles(self) -> None:
        values = [10.0, 20.0, 30.0, 40.0]
        self.assertEqual(BENCH.percentile(values, 0), 10.0)
        self.assertEqual(BENCH.percentile(values, 50), 25.0)
        self.assertEqual(BENCH.percentile(values, 100), 40.0)
        self.assertAlmostEqual(BENCH.percentile(values, 95), 38.5)

    def test_single_value_and_empty(self) -> None:
        self.assertEqual(BENCH.percentile([7.5], 95), 7.5)
        self.assertIsNone(BENCH.percentile([], 50))

    def test_summary_shape(self) -> None:
        summary = BENCH.summarize([1.0, 2.0, 3.0, 4.0])
        self.assertEqual(summary["n"], 4)
        self.assertEqual(summary["samples_ms"], [1.0, 2.0, 3.0, 4.0])
        self.assertEqual(summary["min_ms"], 1.0)
        self.assertEqual(summary["p50_ms"], 2.5)
        self.assertEqual(summary["max_ms"], 4.0)
        self.assertEqual(BENCH.summarize([]), {"n": 0, "samples_ms": []})


class SizeClassTests(unittest.TestCase):
    def test_size_classes(self) -> None:
        cases = {
            0.0: "<1MB",
            0.99: "<1MB",
            1.0: "1-10MB",
            9.99: "1-10MB",
            10.0: "10-50MB",
            49.9: "10-50MB",
            50.0: "50-100MB",
            99.9: "50-100MB",
            100.0: "100MB+",
            250.0: "100MB+",
        }
        for size, expected in cases.items():
            self.assertEqual(BENCH.size_class(size), expected, size)

    def test_size_prefers_size_kb_and_never_needs_a_real_path(self) -> None:
        self.assertEqual(BENCH.session_size_mb({"size_kb": 1024.0, "path": "/missing"}), 1.0)
        self.assertEqual(BENCH.session_size_mb({"path": "/definitely/missing"}), 0.0)
        self.assertEqual(BENCH.session_size_mb({}), 0.0)


class RedactionTests(unittest.TestCase):
    def test_redacts_paths_keys_and_uuids(self) -> None:
        text = (
            "open failed for /Users/alice/.claude/projects/-Users-alice/x.jsonl "
            "key claude:0b1c2d3e-1111-2222-3333-444455556666"
        )
        cleaned = BENCH.redact(text)
        self.assertNotIn("/Users/alice", cleaned)
        self.assertNotIn("claude:0b1c", cleaned)
        self.assertNotIn("1111-2222", cleaned)
        self.assertIn("<path>", cleaned)
        self.assertIn("<key>", cleaned)

    def test_error_text_keeps_the_exception_type(self) -> None:
        message = BENCH.redacted_error(FileNotFoundError("/private/history/session.jsonl"))
        self.assertTrue(message.startswith("FileNotFoundError:"))
        self.assertNotIn("/private/history", message)


class MarkdownTests(unittest.TestCase):
    def _result(self) -> dict:
        return {
            "schema": 1,
            "kind": "corral-remote-open-benchmark",
            "started_at": "2026-10-25T00:00:00Z",
            "duration_ms": 12345.6,
            "timed_out": False,
            "machine": "local",
            "platform": {"system": "Darwin", "machine": "arm64", "python": "3.12.12"},
            "corral_version": "0.24.999",
            "load_average": {"start": [10.0, 20.0, 30.0], "end": [9.0, 19.0, 29.0]},
            "config": {"samples_per_runtime": 1, "scan_limit": 200, "page_limit": 1500},
            "environment": {
                "cache_mode": "isolated-temp",
                "shared_index_seeded": True,
                "shared_index_age_s": 3.5,
            },
            "definitions": BENCH._DEFINITIONS,
            "load": {
                "ms": 7000.0,
                "mode": "shared-index-fixture",
                "scan_shared": True,
                "scan_cache_hit_all": True,
                "session_count": 10,
                "counts": {"claude": 2, "codex": 3, "opencode": 1, "cursor": 2, "pi": 2, "kimi": 0},
            },
            "refresh": {"n": 3, "p50_ms": 100.0, "max_ms": 200.0, "samples": [{"scan_shared": True}]},
            "refresh_state": {"n": 5, "p50_ms": 20.0, "max_ms": 40.0, "samples": []},
            "list_snapshot": {
                "n": 3,
                "p50_ms": 30.0,
                "max_ms": 40.0,
                "samples": [],
                "rows": 80,
                "total": 10,
                "json_bytes": 74400,
            },
            "cold_load": {"ms": 4000.0},
            "sessions": [
                {
                    **BENCH.Sample("codex", 1, 65.0, "50-100MB", "codex:secret", "/secret/x").public(),
                    "page_cold_ms": 2100.0,
                    "page_warm_ms": 1.2,
                    "prompts_cold_ms": 3000.0,
                    "prompts_warm_ms": 0.4,
                    "prompts_after_page_ms": 80.0,
                    "detail_ms": 2.0,
                    "messages": 80,
                }
            ],
            "concurrency": {
                "pair": ["codex#1", "cursor#1"],
                "n": 2,
                "serial_sum_ms": 4000.0,
                "concurrent_open_wall_ms": 2100.0,
                "ratio": 1.905,
            },
            "errors": [],
        }

    def test_tables_are_rendered_without_identifiers(self) -> None:
        markdown = BENCH.render_markdown(self._result())
        self.assertIn("### Host baseline", markdown)
        self.assertIn("### First page and Your prompts", markdown)
        self.assertIn("### Concurrent vs serial open", markdown)
        self.assertIn("codex#1", markdown)
        self.assertIn("2100.0", markdown)
        self.assertIn("1.91", markdown)
        self.assertNotIn("codex:secret", markdown)
        self.assertNotIn("/secret/x", markdown)
        self.assertNotIn("secret title", markdown)

    def test_timed_out_and_cold_load_variants_do_not_raise(self) -> None:
        result = self._result()
        result["timed_out"] = True
        result["cold_load"] = {"timed_out": True, "budget_s": 45.0}
        result["sessions"] = []
        markdown = BENCH.render_markdown(result)
        self.assertIn("timed out", markdown)
        self.assertIn("| — | — |", markdown)


class HelpAndTimestampTests(unittest.TestCase):
    def test_help_contains_comparison_contract(self) -> None:
        parser = BENCH._build_parser()
        help_text = parser.format_help()
        self.assertIn("how to compare", help_text)
        self.assertIn("concurrent_open_wall_ms", help_text)
        self.assertIn("serial_sum_ms", help_text)
        self.assertIn("transcript-cold", help_text)

    def test_timestamp_is_iso_utc(self) -> None:
        stamp = BENCH.utc_now()
        self.assertTrue(stamp.endswith("Z"))
        self.assertIn("T", stamp)
        self.assertEqual(len(stamp), 20)


if __name__ == "__main__":
    unittest.main()
