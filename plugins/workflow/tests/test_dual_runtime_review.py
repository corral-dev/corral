"""Behavior checks for the committed-patch review playbook."""

import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace

from corral_workflow.engine import compile_workflow_script

_SCRIPT = Path(__file__).resolve().parents[1] / "examples/dual-runtime-review.py"
_PATCH = "diff --git a/calc.py b/calc.py\n@@ -1 +1 @@\n-return sum(items)\n+return sum(items) + 1"
_FINDING = {"path": "calc.py", "line": 1, "claim": "Adds one", "evidence": "+return sum(items) + 1"}


class DualRuntimeReviewTests(unittest.TestCase):
    def run_review(self, *, patch=_PATCH, second_runtime="codex", verdicts=None):
        calls = []

        async def shell(command, *, step_key, **kwargs):
            calls.append((step_key, command))
            output = {"base-commit": "a" * 40, "head-commit": "b" * 40, "snapshot": patch}[step_key]
            return SimpleNamespace(output=output)

        async def parallel(items, apply, **kwargs):
            return await asyncio.gather(*(apply(item) for item in items))

        async def agent(prompt, *, step_key, read_only, **kwargs):
            self.assertTrue(read_only)
            self.assertIn(patch, prompt)
            calls.append((step_key, kwargs))
            if step_key == "verify":
                self.assertIn('"id": "1"', prompt)
                return SimpleNamespace(ok=True, value={"verdicts": verdicts})
            runtime = "pi" if step_key == "review-pi" else second_runtime
            return SimpleNamespace(ok=True, observed_runtime=runtime, value={"findings": [_FINDING]})

        env = {
            "args": {"base": "HEAD^"},
            "phase": lambda name: None,
            "shell": shell,
            "parallel": parallel,
            "agent": agent,
        }
        exec(compile_workflow_script(_SCRIPT.read_text(encoding="utf-8"), str(_SCRIPT)), env)
        return asyncio.run(env["__corral_workflow_main__"]()), calls

    def test_empty_patch_never_starts_a_worker(self):
        result, calls = self.run_review(patch="")
        self.assertEqual(result["status"], "no_textual_changes")
        self.assertEqual([key for key, _ in calls], ["base-commit", "head-commit", "snapshot"])

    def test_two_distinct_reviewers_and_verification_use_same_snapshot(self):
        verdict = {"id": "1", "verdict": "confirmed", "reason": "Unconditionally increments the result."}
        result, calls = self.run_review(verdicts=[verdict])
        self.assertEqual(result["findings"], [{**_FINDING, **verdict}])
        self.assertEqual(result["runtimes"], ["pi", "codex"])
        self.assertEqual(dict(calls)["review-other"]["exclude_runtimes"], ["pi"])
        self.assertEqual(dict(calls)["snapshot"][-3:], ["a" * 40, "b" * 40, "--"])

    def test_same_runtime_cannot_count_as_independent(self):
        with self.assertRaisesRegex(RuntimeError, "two distinct runtimes"):
            self.run_review(second_runtime="pi")

    def test_verifier_cannot_drop_a_candidate(self):
        with self.assertRaisesRegex(RuntimeError, "exactly one verdict"):
            self.run_review(verdicts=[])

    def test_oversized_patch_does_not_start_reviewers(self):
        with self.assertRaisesRegex(ValueError, "exceeds 24 KiB"):
            self.run_review(patch="x" * 24_577)
