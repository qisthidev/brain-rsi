from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain_rsi.candidate import FixtureCandidate, FixtureConfig
from brain_rsi.cli import main
from brain_rsi.cycle import make_search_decision
from brain_rsi.journal import Journal, load_journal
from brain_rsi.loader import load_eval_cases
from brain_rsi.search import DEFAULT_STAGES, FixtureTreeMaker, SearchConfig, StageConfig, run_search

ROOT = Path(__file__).resolve().parents[1]


def setup(**config):
    cases = load_eval_cases(ROOT / "eval" / "cases.json")
    base = json.loads((ROOT / "fixtures" / "baseline.json").read_text(encoding="utf-8"))
    tree = json.loads((ROOT / "fixtures" / "tree" / "demo.json").read_text(encoding="utf-8"))
    maker = FixtureTreeMaker.from_dict(tree, base_responses=base)
    baseline = FixtureCandidate(FixtureConfig("baseline", base))
    return cases, baseline, maker, tree["base_files"], SearchConfig(**config)


class SearchTests(unittest.TestCase):
    def test_full_loop_is_deterministic_and_minimises_the_diff(self) -> None:
        outcomes = []
        for _ in range(2):
            cases, baseline, maker, base_files, config = setup(num_drafts=4, seed=7)
            result = run_search(cases, baseline, maker, base_files, config)
            outcomes.append([(n.kind, n.candidate_id, round(n.total, 3)) for n in result.journal])
            self.assertEqual(result.best.candidate_id, "improve-okf")
            self.assertIsNotNone(result.minimal)
            self.assertEqual(result.recommended.candidate_id, "improve-okf~minimal")
            self.assertEqual(result.recommended.total, result.best.total)
            self.assertLess(result.recommended.change_size, result.best.change_size)
            self.assertNotIn("Be concise.", result.recommended_diff())
            self.assertIn("OKF frontmatter", result.recommended_diff())
            self.assertTrue(result.accepted_for_review())
            stages = {s["name"]: s for s in result.stages}
            self.assertEqual(stages["s1_working"]["completed"], "found working candidate")
            inert = [row["hunk"] for row in result.ablation if row["verdict"] == "inert"]
            self.assertEqual(len(inert), 1)
            # poisoned candidates are recorded, never debugged, never parents
            violations = {n.candidate_id: n for n in result.journal.violation_nodes}
            self.assertEqual(set(violations), {"draft-forbidden-path", "draft-secret-leak", "improve-host-path"})
            for node in violations.values():
                self.assertEqual(result.journal.children(node), [])
                self.assertEqual(node.total, 0.0)
            debug = next(n for n in result.journal if n.kind == "debug")
            self.assertEqual(debug.debug_depth, 1)
            self.assertEqual(result.journal.get(debug.parent_id).candidate_id, "draft-checkpoint-and-stale")
        self.assertEqual(outcomes[0], outcomes[1])

    def test_no_working_candidate_stops_the_search(self) -> None:
        cases, baseline, maker, base_files, _ = setup()
        maker.proposals = [p for p in maker.proposals if p.id == "draft-checkpoint-and-stale"]
        config = SearchConfig(num_drafts=1, debug_prob=0.0, stages=DEFAULT_STAGES)
        result = run_search(cases, baseline, maker, base_files, config)
        self.assertEqual(result.stopped_reason, "no working candidate found in the working stage")
        self.assertIs(result.best, result.root)
        self.assertFalse(result.accepted_for_review())
        self.assertEqual([s["name"] for s in result.stages], ["s1_working"])

    def test_debug_depth_is_bounded(self) -> None:
        cases, baseline, maker, base_files, _ = setup()
        # make the debug attempt itself buggy so it would be re-debugged forever without a cap
        buggy = next(p for p in maker.proposals if p.id == "draft-checkpoint-and-stale")
        maker.proposals = [p for p in maker.proposals if p.id in {"draft-checkpoint-and-stale", "debug-remove-stale-rule"}]
        for p in maker.proposals:
            if p.id == "debug-remove-stale-rule":
                p.files.update(buggy.files)
        maker.proposals.append(type(buggy)(id="debug-2", kind="debug", parent="debug-remove-stale-rule", files=dict(buggy.files)))
        maker.proposals.append(type(buggy)(id="debug-3", kind="debug", parent="debug-2", files=dict(buggy.files)))
        config = SearchConfig(num_drafts=1, debug_prob=1.0, max_debug_depth=2,
                              stages=(StageConfig("s1_working", "working", max_iters=10),))
        result = run_search(cases, baseline, maker, base_files, config)
        depths = [n.debug_depth for n in result.journal if n.kind == "debug"]
        self.assertEqual(max(depths), 2)
        self.assertEqual(result.stopped_reason, "no working candidate found in the working stage")

    def test_budget_caps_stop_the_search(self) -> None:
        cases, baseline, maker, base_files, _ = setup()
        config = SearchConfig(num_drafts=4, max_evaluations=3)
        result = run_search(cases, baseline, maker, base_files, config)
        self.assertEqual(result.evaluations, 3)
        self.assertIn("max_evaluations", result.stopped_reason)

    def test_tuning_stage_rejects_new_files(self) -> None:
        cases, baseline, maker, base_files, _ = setup()
        draft = next(p for p in maker.proposals if p.id == "draft-log-rotation")
        maker.proposals = [draft, type(draft)(id="tune-new-file", kind="improve", parent="draft-log-rotation",
                                                files={".claude/skills/new/SKILL.md": "# new\n"})]
        config = SearchConfig(num_drafts=1, debug_prob=0.0, stages=(
            StageConfig("s1_working", "working", max_iters=2), StageConfig("s2_tuning", "tuning", max_iters=2)))
        result = run_search(cases, baseline, maker, base_files, config)
        tuned = next(n for n in result.journal if n.candidate_id == "tune-new-file")
        self.assertTrue(tuned.is_violation)
        self.assertTrue(any("tuning stage" in v for v in tuned.policy_violations))

    def test_journal_persists_and_decision_artifact_is_review_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            cases, baseline, maker, base_files, config = setup(num_drafts=4, seed=1)
            journal = Journal("run-x", path=path)
            result = run_search(cases, baseline, maker, base_files, config, journal=journal)
            loaded = load_journal(path)
            self.assertEqual(len(loaded), len(result.journal))
            self.assertEqual(loaded.best_node().id, result.journal.best_node().id)
            self.assertIn("search_end", [e["event"] for e in loaded.events])
            decision = make_search_decision(result, journal_path=Path("traces/journal/run-x.jsonl"))
            self.assertTrue(decision.accepted_for_review)
            self.assertEqual(decision.run_id, "run-x")
            self.assertIn("never automatic", decision.promotion)
            self.assertTrue(decision.search["recommended_node"]["is_minimal"])
            self.assertIn("+New wiki pages carry OKF frontmatter", decision.search["diff"])
            self.assertEqual(len(decision.search["policy_violations"]), 3)

    def test_cli_search_runs_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            code = main([
                "search", "--num-drafts", "4", "--seed", "2",
                "--journal-dir", directory, "--stage-iters", "working=4,tuning=2,explore=6,ablation=8",
            ])
            self.assertEqual(code, 0)
            self.assertEqual(len(list(Path(directory).glob("*.jsonl"))), 1)
            self.assertEqual(main(["search", "--no-journal", "--stage-iters", "bogus=1"]), 2)
