from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain_rsi.benchmark import run_benchmark
from brain_rsi.candidate import FixtureCandidate, FixtureConfig
from brain_rsi.loader import load_eval_cases


ROOT = Path(__file__).resolve().parents[1]


def fixture(name: str) -> FixtureCandidate:
    responses = json.loads((ROOT / "fixtures" / f"{name}.json").read_text(encoding="utf-8"))
    return FixtureCandidate(FixtureConfig(name, responses))


class BenchmarkTests(unittest.TestCase):
    def test_candidate_is_accepted_without_regressions(self) -> None:
        report = run_benchmark(load_eval_cases(ROOT / "eval" / "cases.json"), fixture("baseline"), fixture("candidate"))
        self.assertTrue(report.accepted())
        self.assertEqual(report.regressions(), [])
        self.assertGreater(report.candidate_total, report.baseline_total)

    def test_critical_regression_is_rejected(self) -> None:
        report = run_benchmark(load_eval_cases(ROOT / "eval" / "cases.json"), fixture("baseline"), fixture("regressed"))
        self.assertFalse(report.accepted())
        self.assertIn("never-close-foreign-task", report.critical_regressions())

    def test_budget_violation_prevents_acceptance(self) -> None:
        cases = load_eval_cases(ROOT / "eval" / "cases.json")
        baseline = fixture("baseline")
        responses = json.loads((ROOT / "fixtures" / "candidate.json").read_text(encoding="utf-8"))
        expensive = FixtureCandidate(FixtureConfig("expensive", responses, step_cost=2))
        report = run_benchmark(cases, baseline, expensive, budget_steps=8)
        self.assertFalse(report.accepted())
        self.assertTrue(report.budget_violations())

    def test_trace_is_append_only_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs.jsonl"
            cases = load_eval_cases(ROOT / "eval" / "cases.json")
            run_benchmark(cases, fixture("baseline"), fixture("candidate"), traces_path=path, run_id="one")
            run_benchmark(cases, fixture("baseline"), fixture("candidate"), traces_path=path, run_id="two")
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["run_id"] for row in rows], ["one", "two"])


if __name__ == "__main__":
    unittest.main()
