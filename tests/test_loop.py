"""Reef-inspired loop pieces: receipts/reports, failure window, verdict, commit log,
version chain and the preregistered gain criterion."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain_rsi.benchmark import run_benchmark
from brain_rsi.candidate import FixtureCandidate, FixtureConfig
from brain_rsi.cli import main
from brain_rsi.commits import CommitLog
from brain_rsi.gain import ControlLog, GainCriterion, GainError, evaluate_gain, suite_digest
from brain_rsi.loader import load_eval_cases
from brain_rsi.reports import ReportError, ReportStore, describe_failures
from brain_rsi.search import FixtureTreeMaker, SearchConfig, run_search
from brain_rsi.triggers import FailureWindow, next_batch, pending_failures
from brain_rsi.types import CandidateOutput, EvalCase, ScoreResult
from brain_rsi.verdict import compare
from brain_rsi.versions import VersionError, VersionLedger, surface_files

ROOT = Path(__file__).resolve().parents[1]


def _fixture(name: str) -> FixtureCandidate:
    return FixtureCandidate(FixtureConfig(name, json.loads((ROOT / "fixtures" / f"{name}.json").read_text())))


class ReportStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ReportStore(Path(self.tmp.name) / "reports.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_receipt_report_roundtrip_is_append_only(self) -> None:
        receipt = self.store.issue_receipt("skill", "greeting", summary="briefing pagi")
        report = self.store.report([receipt.receipt_id], score=0.0, feedback="lupa Today Focus")
        reloaded = ReportStore(self.store.path)
        self.assertEqual([r.receipt_id for r in reloaded.receipts()], [receipt.receipt_id])
        self.assertEqual(reloaded.get_report(report.report_id).feedback, "lupa Today Focus")
        self.assertEqual(reloaded.summary()["failing_reports"], 1)
        lines = self.store.path.read_text().splitlines()
        self.assertEqual([json.loads(l)["type"] for l in lines], ["receipt", "report"])

    def test_unknown_receipt_bad_score_and_secret_are_refused(self) -> None:
        with self.assertRaises(ReportError):
            self.store.report(["rcpt-missing"], score=1.0)
        receipt = self.store.issue_receipt("case", "x")
        with self.assertRaises(ReportError):
            self.store.report([receipt.receipt_id], score=1.5)
        with self.assertRaises(ReportError):
            self.store.report([receipt.receipt_id], score=0.0, feedback="key sk-live-abcdefghijklmnopqrstuvwxyz0123456789")
        with self.assertRaises(ReportError):
            self.store.issue_receipt("skill", "x", summary="see /Users/someone/secret.md")
        with self.assertRaises(ReportError):
            self.store.report([receipt.receipt_id], score=None)  # unscored needs feedback
        self.assertEqual(self.store.summary()["reports"], 0)

    def test_failure_window_batches_only_failures_once(self) -> None:
        ids = [self.store.issue_receipt("task", f"t{i}").receipt_id for i in range(4)]
        self.store.report([ids[0]], score=0.0, feedback="a")
        self.store.report([ids[1]], score=1.0, feedback="pass")
        self.store.report([ids[2]], score=None, feedback="voided")
        window = FailureWindow(max_score=0.0, batch_size=2)
        self.assertIsNone(next_batch(self.store, window))
        self.store.report([ids[3]], score=0.0, feedback="b")
        batch = next_batch(self.store, window)
        self.assertIsNotNone(batch)
        self.assertEqual(len(batch.reports), 2)
        lines = batch.failure_lines(self.store)
        self.assertTrue(lines[0].startswith("task:t0"))
        self.store.record_batch(batch.report_ids, run_id="run-1", window=window.to_dict())
        self.assertEqual(pending_failures(self.store, window), [])
        self.assertIsNone(next_batch(self.store, window))
        with self.assertRaises(ReportError):
            self.store.record_batch(batch.report_ids, run_id="run-2")
        # unscored reports count only when asked for
        self.assertEqual(len(pending_failures(self.store, FailureWindow(batch_size=1, include_unscored=True))), 1)
        self.assertEqual(describe_failures(self.store, [])[:], [])


class VerdictTests(unittest.TestCase):
    @staticmethod
    def _score(case_id: str, points: float, *, passed: bool = True, error: str | None = None) -> ScoreResult:
        return ScoreResult("c", case_id, passed, points, 2.0, False, runner_error=error, unscored=bool(error))

    def test_win_loss_tie_unscored_and_gate(self) -> None:
        base = [self._score("a", 1.0), self._score("b", 2.0), self._score("c", 2.0), self._score("d", 0.0, passed=False)]
        cand = [self._score("a", 2.0), self._score("b", 1.0), self._score("c", 2.0), self._score("d", 0.0, error="boom")]
        verdict = compare(base, cand)
        self.assertEqual(verdict.wins, ["a"])
        self.assertEqual(verdict.losses, ["b"])
        self.assertEqual(verdict.ties, ["c"])
        self.assertEqual(verdict.unscored, ["d"])
        ok, reason = verdict.gate()
        self.assertFalse(ok)
        self.assertIn("unscored", reason)
        clean = compare(base[:3], [cand[0], base[1], cand[2]])
        self.assertTrue(clean.gate()[0])
        self.assertFalse(compare(base[:3], base[:3]).gate()[0])  # all tie is not a win
        with self.assertRaises(ValueError):
            compare(base[:2], cand[:3])

    def test_runner_error_is_unscored_sentinel_not_zero(self) -> None:
        from brain_rsi.policy import score_case

        case = EvalCase("x", "Q", "p", ("alpha",), (), weight=2.0)
        errored = score_case(case, CandidateOutput("c", "x", "", 0.0, 0, error="runner raised"))
        self.assertTrue(errored.unscored)
        plain_fail = score_case(case, CandidateOutput("c", "x", "nothing", 0.0, 1))
        self.assertFalse(plain_fail.unscored)

    def test_point_loss_without_pass_flip_is_rejected(self) -> None:
        cases = [EvalCase("k", "Q", "p", ("a", "b"), (), weight=2.0), EvalCase("m", "Q", "p", ("z",), (), weight=1.5)]
        base = FixtureCandidate(FixtureConfig("base", {"k": "a b", "m": ""}))
        cand = FixtureCandidate(FixtureConfig("cand", {"k": "a", "m": "z"}))  # k: 2.0 -> 1.0 still passes; m: 0 -> 1
        report = run_benchmark(cases, base, cand)
        self.assertEqual(report.regressions(), [])
        self.assertGreater(report.candidate_total, report.baseline_total)
        self.assertFalse(report.accepted())
        self.assertIn("lost points", report.rejection_reason())

    def test_search_nodes_carry_verdicts(self) -> None:
        cases = load_eval_cases(ROOT / "eval" / "cases.json")
        base = json.loads((ROOT / "fixtures" / "baseline.json").read_text())
        tree = json.loads((ROOT / "fixtures" / "tree" / "demo.json").read_text())
        maker = FixtureTreeMaker.from_dict(tree, base_responses=base)
        result = run_search(cases, FixtureCandidate(FixtureConfig("baseline", base)), maker, tree["base_files"],
                            SearchConfig(num_drafts=4, seed=7), reported_failures=["skill:greeting — score 0.00 — x"])
        self.assertTrue(result.accepted_for_review())
        self.assertEqual(result.rejection_reason(), "")
        verdict = result.recommended.meta["verdict"]
        self.assertTrue(verdict["gate_ok"])
        self.assertGreaterEqual(len(verdict["wins"]), 1)
        self.assertEqual(verdict["losses"], [])
        self.assertTrue(any(e["event"] == "reported_failures" for e in result.journal.events))


class CommitLogTests(unittest.TestCase):
    def test_records_every_outcome_including_skips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = CommitLog(Path(tmp) / "commits.jsonl")
            log.record(run_id="r1", command="search", outcome="skipped", reason="nothing batched")
            log.record(run_id="r2", command="cycle", outcome="review", reason="ok", baseline_total=1.0, candidate_total=2.0)
            with self.assertRaises(ValueError):
                log.record(run_id="r3", command="cycle", outcome="promoted", reason="never")
            entries = list(log)
            self.assertEqual([e.outcome for e in entries], ["skipped", "review"])
            self.assertEqual(log.counts()["skipped"], 1)
            self.assertEqual(log.tail(1)[0].run_id, "r2")


class VersionLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "agent").mkdir()
        (self.root / "agent" / "PROMPT.md").write_text("v1\n")
        (self.root / ".claude" / "skills" / "x").mkdir(parents=True)
        (self.root / ".claude" / "skills" / "x" / "SKILL.md").write_text("skill\n")
        (self.root / "wiki").mkdir()
        (self.root / "wiki" / "note.md").write_text("content, not surface\n")
        self.decision = self.root / "decision.json"
        self.decision.write_text(json.dumps({"accepted_for_review": True, "run_id": "r1", "candidate_id": "c1"}))
        self.ledger = VersionLedger(self.root / "versions.jsonl")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_surface_fingerprint_ignores_content(self) -> None:
        files = surface_files(self.root)
        self.assertEqual(sorted(files), [".claude/skills/x/SKILL.md", "agent/PROMPT.md"])

    def test_publish_requires_acc_and_accepted_decision_then_check_tracks_state(self) -> None:
        with self.assertRaises(VersionError):
            self.ledger.publish(self.root, acc="", decision_path=self.decision)
        with self.assertRaises(VersionError):
            self.ledger.publish(self.root, acc="oke deh", decision_path=self.decision)
        rejected = self.root / "rejected.json"
        rejected.write_text(json.dumps({"accepted_for_review": False}))
        with self.assertRaises(VersionError):
            self.ledger.publish(self.root, acc="ACC owner (x): y", decision_path=rejected)
        with self.assertRaises(VersionError):
            self.ledger.publish(self.root, acc="ACC owner (x): y")  # decision required unless seeding
        self.assertEqual(self.ledger.check(self.root)["status"], "unversioned")
        v1 = self.ledger.publish(self.root, acc="ACC owner (review session, 10:00): apply diff c1", decision_path=self.decision)
        self.assertEqual(v1.version, "v1")
        self.assertEqual(self.ledger.check(self.root)["status"], "current")
        with self.assertRaises(VersionError):  # same decision twice
            self.ledger.publish(self.root, acc="ACC owner (x): again", decision_path=self.decision)
        (self.root / "agent" / "PROMPT.md").write_text("v2 local edit\n")
        state = self.ledger.check(self.root)
        self.assertEqual(state["status"], "ahead")
        self.assertEqual(state["changed"], ["agent/PROMPT.md"])
        second = self.root / "d2.json"
        second.write_text(json.dumps({"accepted_for_review": True, "run_id": "r2", "candidate_id": "c2"}))
        self.ledger.publish(self.root, acc="ACC owner (x): c2", decision_path=second)
        (self.root / "agent" / "PROMPT.md").write_text("v1\n")  # a stale checkout
        state = VersionLedger(self.ledger.path).check(self.root)
        self.assertEqual(state["status"], "behind")
        self.assertEqual(state["local_version"], "v1")
        self.assertEqual(len(VersionLedger(self.ledger.path)), 2)


class GainTests(unittest.TestCase):
    def test_criterion_is_loaded_and_validated(self) -> None:
        criterion = GainCriterion.load(ROOT / "eval" / "gain_criterion.json")
        self.assertGreaterEqual(criterion.control_runs_min, 3)
        self.assertEqual(criterion.sd_multiplier, 2.0)
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "c.json"
            bad.write_text(json.dumps({"control_runs_min": 1, "sd_multiplier": 2, "registered_at": "x"}))
            with self.assertRaises(GainError):
                GainCriterion.load(bad)

    def test_claim_needs_controls_variance_and_margin(self) -> None:
        criterion = GainCriterion.load(ROOT / "eval" / "gain_criterion.json")
        with tempfile.TemporaryDirectory() as tmp:
            log = ControlLog(Path(tmp) / "control.jsonl")
            self.assertFalse(evaluate_gain(criterion, [], 10.0).claim)
            for total in (30.0, 30.0, 30.0):
                log.record(run_id="r", baseline_id="b", suite="s", total=total, max_points=40)
            degenerate = evaluate_gain(criterion, log.matching(baseline_id="b", suite="s"), 41.5)
            self.assertFalse(degenerate.claim)
            self.assertTrue(degenerate.degenerate)
            log2 = ControlLog(Path(tmp) / "control2.jsonl")
            for total in (29.0, 30.0, 31.0):
                log2.record(run_id="r", baseline_id="b", suite="s", total=total, max_points=40)
            controls = log2.matching(baseline_id="b", suite="s")
            self.assertTrue(evaluate_gain(criterion, controls, 32.5).claim)  # mean 30, sd 1 -> threshold 32
            self.assertFalse(evaluate_gain(criterion, controls, 31.9).claim)
            self.assertEqual(log2.matching(baseline_id="b", suite="other"), [])
        self.assertEqual(len(suite_digest(ROOT / "eval" / "cases.json")), 64)


class LoopCliTests(unittest.TestCase):
    def test_from_reports_skips_then_runs_and_logs_commits(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            common = ["--reports", str(t / "r.jsonl"), "--commit-log", str(t / "c.jsonl")]
            search = ["search", "--no-journal", "--no-html", "--from-reports", "--batch-size", "2",
                      "--traces", str(t / "runs.jsonl"), *common]
            self.assertEqual(main(search), 0)
            self.assertEqual([e.outcome for e in CommitLog(t / "c.jsonl")], ["skipped"])
            store = ReportStore(t / "r.jsonl")
            r1 = store.issue_receipt("skill", "greeting").receipt_id
            r2 = store.issue_receipt("session", "Kbt Product").receipt_id
            self.assertEqual(main(["report", "--receipt", r1, "--score", "0", "--feedback", "a", "--reports", str(t / "r.jsonl")]), 0)
            self.assertEqual(main(["report", "--receipt", r2, "--score", "0", "--feedback", "b", "--reports", str(t / "r.jsonl")]), 0)
            self.assertEqual(main(search), 0)
            entries = list(CommitLog(t / "c.jsonl"))
            self.assertEqual([e.outcome for e in entries], ["skipped", "review"])
            self.assertIsNotNone(entries[1].batch_id)
            self.assertTrue(entries[1].verdict["gate_ok"])
            self.assertEqual(len(ReportStore(t / "r.jsonl").batches()), 1)
            self.assertEqual(main(search), 0)  # batch consumed: skipped again
            self.assertEqual(CommitLog(t / "c.jsonl").counts()["skipped"], 2)
            self.assertEqual(main(["reports", *common[:2], "--json"]), 0)
            self.assertEqual(main(["commits", "--commit-log", str(t / "c.jsonl")]), 0)

    def test_cycle_regressed_is_rejected_with_verdict_and_gain_control_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            argv = ["cycle", "--candidate", "regressed", "--traces", str(t / "runs.jsonl"),
                    "--commit-log", str(t / "c.jsonl"), "--control-log", str(t / "k.jsonl")]
            self.assertEqual(main(argv), 1)
            entry = list(CommitLog(t / "c.jsonl"))[-1]
            self.assertEqual(entry.outcome, "rejected")
            self.assertTrue(entry.verdict["losses"])
            trace = json.loads((t / "runs.jsonl").read_text().splitlines()[-1])
            self.assertIn("verdict", trace["meta"])
            self.assertEqual(main(["gain", "control", "--runs", "3", "--control-log", str(t / "k.jsonl"),
                                   "--commit-log", str(t / "c.jsonl")]), 0)
            self.assertEqual(len(list(ControlLog(t / "k.jsonl"))), 3)
            self.assertEqual(main(["gain", "show", "--control-log", str(t / "k.jsonl")]), 0)
            decision = t / "d.json"
            decision.write_text(json.dumps({"accepted_for_review": True, "run_id": "r", "candidate_id": "c",
                                            "baseline_id": "baseline", "candidate_total": 41.5,
                                            "suite_digest": suite_digest(ROOT / "eval" / "cases.json")}))
            self.assertEqual(main(["gain", "claim", "--decision", str(decision), "--control-log", str(t / "k.jsonl"),
                                   "--commit-log", str(t / "c.jsonl")]), 1)  # deterministic control: no claim
            stale = t / "stale.json"
            stale.write_text(json.dumps({"baseline_id": "baseline", "candidate_total": 41.5, "suite_digest": "0" * 64}))
            self.assertEqual(main(["gain", "claim", "--decision", str(stale), "--control-log", str(t / "k.jsonl")]), 2)

    def test_version_cli_refuses_without_acc(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            t = Path(tmp)
            self.assertEqual(main(["version", "check", "--ledger", str(t / "v.jsonl")]), 0)
            self.assertEqual(main(["version", "publish", "--seed", "--ledger", str(t / "v.jsonl"),
                                   "--commit-log", str(t / "c.jsonl")]), 2)
            self.assertEqual(main(["version", "publish", "--seed", "--acc", "ACC owner (x): seed v1",
                                   "--ledger", str(t / "v.jsonl"), "--commit-log", str(t / "c.jsonl")]), 0)
            self.assertEqual(main(["version", "check", "--ledger", str(t / "v.jsonl")]), 0)
            self.assertEqual(main(["version", "list", "--ledger", str(t / "v.jsonl")]), 0)
            self.assertEqual(list(CommitLog(t / "c.jsonl"))[-1].outcome, "published")


if __name__ == "__main__":
    unittest.main()
