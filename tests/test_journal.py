from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain_rsi.benchmark import run_benchmark
from brain_rsi.candidate import FixtureCandidate, FixtureConfig
from brain_rsi.journal import CandidateNode, Journal, JournalError, load_journal, node_from_report
from brain_rsi.loader import load_eval_cases
from brain_rsi.types import ScoreResult

ROOT = Path(__file__).resolve().parents[1]


def node(id_: str, kind: str, parent: str | None, total: float, **kw) -> CandidateNode:
    return CandidateNode(id=id_, kind=kind, stage="t", candidate_id=id_, parent_id=parent, total=total, max_points=10.0, **kw)


class JournalTests(unittest.TestCase):
    def test_append_enforces_tree_shape(self) -> None:
        journal = Journal("run")
        journal.append(node("root", "baseline", None, 5.0))
        with self.assertRaises(JournalError):
            journal.append(node("orphan", "draft", "missing", 1.0))
        with self.assertRaises(JournalError):
            journal.append(node("second-root", "draft", None, 1.0))
        with self.assertRaises(JournalError):
            journal.append(node("root", "draft", "root", 1.0))
        with self.assertRaises(ValueError):
            node("x", "mystery", "root", 1.0)

    def test_status_and_best_is_deterministic(self) -> None:
        journal = Journal("run")
        journal.append(node("root", "baseline", None, 5.0))
        journal.append(node("a", "draft", "root", 6.0, changed_files=("agent/PROMPT.md",), change_size=3))
        journal.append(node("b", "draft", "root", 6.0, changed_files=("agent/PROMPT.md",), change_size=1))
        journal.append(node("c", "draft", "root", 9.0, regressions=("x",)))
        journal.append(node("d", "draft", "root", 9.5, policy_violations=("eval/cases.json: outside mutable allowlist",)))
        journal.append(node("e", "debug", "c", 6.0, debug_depth=1, change_size=1))
        self.assertTrue(journal.get("c").is_buggy)
        self.assertTrue(journal.get("d").is_violation)
        self.assertFalse(journal.get("d").is_buggy)
        self.assertEqual([n.id for n in journal.good_nodes], ["root", "a", "b", "e"])
        self.assertEqual([n.id for n in journal.buggy_nodes], ["c"])  # violations are not debuggable
        # highest total, then smallest change, then earliest: b (6.0, size 1) beats a and e (same size, later)
        self.assertEqual(journal.best_node().id, "b")
        self.assertEqual([n.id for n in journal.debuggable_nodes(max_debug_depth=2)], [])  # c has a child
        journal.append(node("f", "draft", "root", 1.0, critical_regressions=("k",)))
        self.assertEqual([n.id for n in journal.debuggable_nodes(max_debug_depth=2)], ["f"])
        self.assertEqual([n.id for n in journal.debuggable_nodes(max_debug_depth=0)], [])
        self.assertEqual(journal.tree_root(journal.get("e")).id, "c")
        self.assertEqual(journal.summary()["best_id"], "b")

    def test_jsonl_is_append_only_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "j.jsonl"
            journal = Journal("run-1", path=path)
            journal.append(node("root", "baseline", None, 5.0, scores=[ScoreResult("b", "c1", True, 1.0, 1.0, False)]))
            journal.event("stage_start", stage="s1")
            journal.append(node("a", "draft", "root", 6.0, changed_files=("agent/PROMPT.md",), meta={"rationale": "x"}))
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual([json.loads(line)["type"] for line in lines], ["run", "node", "event", "node"])
            loaded = load_journal(path)
            self.assertEqual(loaded.run_id, "run-1")
            self.assertEqual([n.id for n in loaded], ["root", "a"])
            self.assertEqual(loaded.get("root").scores[0].case_id, "c1")
            self.assertEqual(loaded.get("a").changed_files, ("agent/PROMPT.md",))
            self.assertEqual(loaded.events[0]["event"], "stage_start")
            # appending more never rewrites earlier lines
            journal.append(node("b", "draft", "root", 4.0))
            self.assertEqual(path.read_text(encoding="utf-8").splitlines()[:4], lines)

    def test_existing_cycle_report_becomes_first_nodes(self) -> None:
        cases = load_eval_cases(ROOT / "eval" / "cases.json")
        base = json.loads((ROOT / "fixtures" / "baseline.json").read_text(encoding="utf-8"))
        cand = json.loads((ROOT / "fixtures" / "regressed.json").read_text(encoding="utf-8"))
        report = run_benchmark(cases, FixtureCandidate(FixtureConfig("baseline", base)), FixtureCandidate(FixtureConfig("regressed", cand)))
        root, child = node_from_report(report)
        journal = Journal("bridge")
        journal.append(root)
        journal.append(child)
        self.assertEqual(root.total, report.baseline_total)
        self.assertTrue(child.is_buggy)
        self.assertIn("never-close-foreign-task", child.critical_regressions)
        self.assertIs(journal.best_node(), root)
