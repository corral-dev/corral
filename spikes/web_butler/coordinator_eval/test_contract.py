"""Deterministic tests for the coordinator command boundary (contract.validate).

No LLM calls. Each rule in contract.validate has at least one passing and one
failing case here, using small inline fixtures rather than the scenarios' checks.
"""

import unittest

import contract

PROJECTS = {"Corral": ["Corral/cli", "Corral/ios"], "Notely": ["Notely/web"]}
ASSISTANTS = {"claude": "usable", "codex": "usable", "cursor": "quota exhausted"}


def task(id_, state="running", report=None):
    return {"id": id_, "state": state, "worker_report": report}


def create(id_, project="Corral/cli", assistant=None, depends_on=None, anchors=None):
    return {
        "type": "create_task",
        "id": id_,
        "project": project,
        "assistant": assistant,
        "instruction": "do it",
        "anchors": anchors or [],
        "depends_on": depends_on or [],
    }


def run(actions, document=None, ledger=None, projects=None, assistants=None):
    return contract.validate(
        actions,
        document if document is not None else [],
        ledger if ledger is not None else [],
        projects if projects is not None else PROJECTS,
        assistants if assistants is not None else ASSISTANTS,
    )


class TestQuoteContiguity(unittest.TestCase):
    def test_exact_quote_accepted(self):
        doc = ["Corral 列表加个搜索框"]
        self.assertEqual(run([create("a", anchors=["列表加个搜索框"])], document=doc), [])

    def test_quote_not_contiguous_rejected(self):
        doc = ["Corral 列表加个搜索框"]
        errors = run([create("a", anchors=["列表搜索框"])], document=doc)
        self.assertTrue(any("not a contiguous span" in e for e in errors))

    def test_quote_joining_two_blocks_rejected(self):
        doc = ["Corral 列表加个", "搜索框"]
        errors = run([create("a", anchors=["加个搜索框"])], document=doc)
        self.assertTrue(any("not a contiguous span" in e for e in errors))

    def test_struck_characters_keep_positions(self):
        doc = ["Corral 列表加个~~索~~搜索框"]
        # Including the struck character is contiguous as the owner sees it.
        self.assertEqual(run([create("a", anchors=["加个索搜索框"])], document=doc), [])
        # Skipping the struck character joins live text across a strike: rejected.
        errors = run([create("a", anchors=["加个搜索框"])], document=doc)
        self.assertTrue(any("not a contiguous span" in e for e in errors))

    def test_coordinator_markers_ignored(self):
        doc = ["A<!--coordinator-->Q<!--/coordinator-->B"]
        # Marker tags are stripped, so the surrounding text is contiguous.
        self.assertEqual(run([create("a", anchors=["AQB"])], document=doc), [])
        # The raw marker characters are not part of the block text.
        errors = run([create("a", anchors=["A<!--coordinator-->Q"])], document=doc)
        self.assertTrue(any("not a contiguous span" in e for e in errors))

    def test_empty_anchor_rejected(self):
        errors = run([create("a", anchors=[""])])
        self.assertTrue(any("not a contiguous span" in e for e in errors))


class TestLedgerStates(unittest.TestCase):
    def test_update_task_states(self):
        self.assertEqual(run([{"type": "update_task", "id": "t1"}], ledger=[task("t1", "queued")]), [])
        errors = run([{"type": "update_task", "id": "t1"}], ledger=[task("t1", "running")])
        self.assertTrue(any("not allowed on a running task" in e for e in errors))

    def test_reanchor_states(self):
        self.assertEqual(run([{"type": "reanchor", "id": "t1"}], ledger=[task("t1", "running")]), [])
        errors = run([{"type": "reanchor", "id": "t1"}], ledger=[task("t1", "done")])
        self.assertTrue(any("not allowed on a done task" in e for e in errors))

    def test_steer_states(self):
        self.assertEqual(run([{"type": "steer", "id": "t1"}], ledger=[task("t1", "running")]), [])
        errors = run([{"type": "steer", "id": "t1"}], ledger=[task("t1", "queued")])
        self.assertTrue(any("not allowed on a queued task" in e for e in errors))

    def test_stop_states(self):
        self.assertEqual(run([{"type": "stop", "id": "t1"}], ledger=[task("t1", "blocked")]), [])
        errors = run([{"type": "stop", "id": "t1"}], ledger=[task("t1", "done")])
        self.assertTrue(any("not allowed on a done task" in e for e in errors))

    def test_reassign_states(self):
        action = {"type": "reassign", "id": "t1", "assistant": "claude"}
        self.assertEqual(run([action], ledger=[task("t1", "queued")]), [])
        errors = run([action], ledger=[task("t1", "done")])
        self.assertTrue(any("not allowed on a done task" in e for e in errors))

    def test_mark_done_states(self):
        doc = ["Corral 手机端会话列表加搜索框"]
        report = "Added a search field. Ran 42 tests and checked a screenshot."
        ok = {"type": "mark_done", "id": "t1", "quote": "Corral 手机端会话列表加搜索框",
              "evidence": "Ran 42 tests and checked a screenshot"}
        self.assertEqual(run([ok], document=doc, ledger=[task("t1", "running", report)]), [])
        errors = run([ok], document=doc, ledger=[task("t1", "queued", report)])
        self.assertTrue(any("not allowed on a queued task" in e for e in errors))

    def test_reopen_as_followup_states(self):
        action = {"type": "reopen_as_followup", "of": "t1", "id": "new1"}
        self.assertEqual(run([action], ledger=[task("t1", "done")]), [])
        errors = run([action], ledger=[task("t1", "running")])
        self.assertTrue(any("not allowed on a running task" in e for e in errors))


class TestUnknownTask(unittest.TestCase):
    def test_unknown_task_id_rejected(self):
        errors = run([{"type": "steer", "id": "nope"}])
        self.assertTrue(any("does not exist" in e for e in errors))


class TestNewTaskIds(unittest.TestCase):
    def test_create_task_missing_id(self):
        errors = run([create(None)])
        self.assertTrue(any("missing or already used" in e for e in errors))

    def test_duplicate_new_ids_in_same_round(self):
        errors = run([create("a"), create("a")])
        self.assertEqual(len(errors), 1)
        self.assertTrue(any("missing or already used" in e for e in errors))

    def test_new_id_collides_with_ledger(self):
        errors = run([create("a")], ledger=[task("a", "queued")])
        self.assertTrue(any("missing or already used" in e for e in errors))

    def test_reopen_missing_and_duplicate_id(self):
        self.assertTrue(
            any("missing or already used" in e
                for e in run([{"type": "reopen_as_followup", "of": "t1", "id": None}],
                             ledger=[task("t1", "done")]))
        )
        errors = run(
            [{"type": "reopen_as_followup", "of": "t1", "id": "n"},
             {"type": "reopen_as_followup", "of": "t1", "id": "n"}],
            ledger=[task("t1", "done")],
        )
        self.assertEqual(len(errors), 1)
        self.assertTrue(any("missing or already used" in e for e in errors))


class TestDependencies(unittest.TestCase):
    def test_dependency_in_ledger_ok(self):
        self.assertEqual(run([create("a", depends_on=["dep"])], ledger=[task("dep", "queued")]), [])

    def test_dependency_earlier_in_same_round_ok(self):
        self.assertEqual(run([create("b"), create("a", depends_on=["b"])]), [])

    def test_unknown_dependency_rejected(self):
        errors = run([create("a", depends_on=["ghost"])])
        self.assertTrue(any("dependency 'ghost' does not exist" in e for e in errors))

    def test_forward_dependency_rejected(self):
        errors = run([create("a", depends_on=["b"]), create("b")])
        self.assertTrue(any("dependency 'b' does not exist" in e for e in errors))


class TestComponents(unittest.TestCase):
    def test_known_component_ok(self):
        self.assertEqual(run([create("a", project="Corral/ios")]), [])

    def test_unknown_component_rejected(self):
        errors = run([create("a", project="Corral/nope")])
        self.assertTrue(any("is not a known component" in e for e in errors))


class TestAssistants(unittest.TestCase):
    def test_create_task_usable_assistant_ok(self):
        self.assertEqual(run([create("a", assistant="claude")]), [])

    def test_create_task_no_assistant_ok(self):
        self.assertEqual(run([create("a")]), [])

    def test_create_task_unusable_assistant_rejected(self):
        errors = run([create("a", assistant="cursor")])
        self.assertTrue(any("is not usable now" in e for e in errors))

    def test_reassign_usable_assistant_ok(self):
        self.assertEqual(
            run([{"type": "reassign", "id": "t1", "assistant": "claude"}], ledger=[task("t1", "queued")]),
            [],
        )

    def test_reassign_unusable_assistant_rejected(self):
        errors = run([{"type": "reassign", "id": "t1", "assistant": "cursor"}],
                     ledger=[task("t1", "queued")])
        self.assertTrue(any("is not usable now" in e for e in errors))

    def test_reassign_missing_assistant_rejected(self):
        errors = run([{"type": "reassign", "id": "t1"}], ledger=[task("t1", "queued")])
        self.assertTrue(any("reassign needs a usable assistant" in e for e in errors))


class TestMarkDoneEvidence(unittest.TestCase):
    def setUp(self):
        self.doc = ["Corral 手机端会话列表加搜索框"]
        self.report = "Added a search field. Ran 42 tests and checked a screenshot."

    def test_evidence_exact_substring_accepted(self):
        action = {"type": "mark_done", "id": "t1", "quote": "Corral 手机端会话列表加搜索框",
                  "evidence": "Ran 42 tests and checked a screenshot"}
        self.assertEqual(run([action], document=self.doc, ledger=[task("t1", "running", self.report)]), [])

    def test_evidence_missing_rejected(self):
        action = {"type": "mark_done", "id": "t1", "quote": "Corral 手机端会话列表加搜索框"}
        errors = run([action], document=self.doc, ledger=[task("t1", "running", self.report)])
        self.assertTrue(any("evidence must be copied exactly" in e for e in errors))

    def test_evidence_not_in_report_rejected(self):
        action = {"type": "mark_done", "id": "t1", "quote": "Corral 手机端会话列表加搜索框",
                  "evidence": "deployed to production"}
        errors = run([action], document=self.doc, ledger=[task("t1", "running", self.report)])
        self.assertTrue(any("evidence must be copied exactly" in e for e in errors))


class TestAskNear(unittest.TestCase):
    def test_ask_near_contiguous_ok(self):
        doc = ["登录后立刻跳回登录页，把这个 bug 修了"]
        self.assertEqual(run([{"type": "ask", "near": "登录后立刻跳回登录页", "text": "what?"}],
                             document=doc), [])

    def test_ask_near_not_contiguous_rejected(self):
        doc = ["登录后立刻跳回登录页，把这个 bug 修了"]
        errors = run([{"type": "ask", "near": "登录 修了", "text": "what?"}], document=doc)
        self.assertTrue(any("not a contiguous span" in e for e in errors))


if __name__ == "__main__":
    unittest.main()
