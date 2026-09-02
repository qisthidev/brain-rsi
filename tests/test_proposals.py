from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain_rsi.cli import main
from brain_rsi.proposals import ImprovementProposal, ProposalError, check_novelty, load_proposals, write_proposal

ROOT = Path(__file__).resolve().parents[1]


def prop(name, hypothesis, status="open", **kw):
    return ImprovementProposal(name=name, hypothesis=hypothesis, status=status, **kw)


class ProposalTests(unittest.TestCase):
    def test_schema_validation(self) -> None:
        with self.assertRaises(ProposalError):
            prop("Bad Name", "x")
        with self.assertRaises(ProposalError):
            prop("ok-name", "   ")
        with self.assertRaises(ProposalError):
            prop("ok-name", "x", status="maybe")

    def test_write_load_and_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            d = Path(directory)
            path = write_proposal(prop("log-rotation-rule", "teach monthly log rotation newest on top"), d)
            self.assertTrue(path.is_file())
            with self.assertRaises(ProposalError):
                write_proposal(prop("log-rotation-rule", "again"), d)
            (d / "bad.json").write_text('{"name": "bad", "hypothesis": "h", "extra": 1}', encoding="utf-8")
            with self.assertRaises(ProposalError):
                load_proposals(d)
            (d / "bad.json").unlink()
            loaded = load_proposals(d)
            self.assertEqual(loaded[0].name, "log-rotation-rule")
            self.assertEqual(loaded[0].source_path, str(path))

    def test_novelty_against_proposals_decisions_and_lessons(self) -> None:
        me = prop("close-tasks-guard", "never close or complete tasks created by other people; leave the task open and report",
                  target_cases=["never-close-foreign-task"], surface_files=["agent/PROMPT.md"])
        rejected = prop("close-guard-old", "never complete tasks created by other people, leave the task open, report", status="rejected")
        unrelated = prop("okf-frontmatter", "new wiki pages carry OKF frontmatter with type", status="open")
        report = check_novelty(me, [rejected, unrelated])
        self.assertFalse(report.ok)
        self.assertTrue(any("close-guard-old" in b for b in report.blocking))
        with tempfile.TemporaryDirectory() as directory:
            lessons = Path(directory)
            (lessons / "index.md").write_text(
                "# Lessons\n\n- Never close or complete a task created by other people; leave it open and report.\n",
                encoding="utf-8",
            )
            report2 = check_novelty(me, [unrelated], lessons_dir=lessons)
            self.assertTrue(report2.ok)
            self.assertTrue(report2.related_lessons, "lesson mentions closing other people's tasks")
        with tempfile.TemporaryDirectory() as directory:
            patches = Path(directory)
            (patches / "x.json").write_text(json.dumps({
                "accepted_for_review": False,
                "search": {"recommended_node": {"rationale": "never close tasks created by other people; leave task open and report", "changed_files": ["agent/PROMPT.md"]}},
            }), encoding="utf-8")
            report3 = check_novelty(me, [], patches_dir=patches)
            self.assertTrue(report3.prior_decisions)
            self.assertFalse(report3.ok)

    def test_cli_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(main(["proposal", "new", "demo-rule", "--hypothesis", "state readiness and what is blocked", "--proposals-dir", directory]), 0)
            self.assertEqual(main(["proposal", "new", "demo-rule-2", "--hypothesis", "state readiness and what is blocked too", "--proposals-dir", directory]), 1)
            self.assertEqual(main(["proposal", "list", "--proposals-dir", directory]), 0)
            self.assertEqual(main(["proposal", "check", "--proposals-dir", directory]), 0)
            self.assertEqual(main(["proposal", "check", "nope", "--proposals-dir", directory]), 2)
