from __future__ import annotations

import json
import unittest

from brain_rsi.journal import CandidateNode, Journal
from brain_rsi.live import CcxClient, CcxError
from brain_rsi.review import ensure_distinct_families, review_candidate
from brain_rsi.treeviz import render_tree_html
from test_live import FakeTransport


def client(answers):
    return CcxClient(transport=FakeTransport(answers))


REVIEW = {"scores": {"contract": 5, "clarity": 4, "evidence": 4, "risk": 4, "minimality": 3}, "overall": 7,
          "verdict": "revise", "strengths": ["clear"], "weaknesses": ["one redundant line"], "must_fix": []}
META = {"verdict": "revise", "confidence": "medium", "consensus": ["minor bloat"], "disagreements": [], "top_concern": "redundancy", "summary": "ok"}


class ReviewTests(unittest.TestCase):
    def test_distinct_families_enforced(self) -> None:
        with self.assertRaises(CcxError):
            ensure_distinct_families(["gemini-3-flash", "gemini-pro-agent"])
        with self.assertRaises(CcxError):
            ensure_distinct_families(["gemini-3-flash", "grok-4.5"], meta_model="grok-4.20")
        ensure_distinct_families(["gemini-3-flash", "grok-4.5"], meta_model="deepseek-v4-flash")

    def test_reviews_are_parsed_and_failures_are_recorded(self) -> None:
        c = client([json.dumps(REVIEW), "garbage", json.dumps(META)])
        bundle = review_candidate(
            c, ["gemini-3-flash", "grok-4.5"], diff="+x", files={"agent/PROMPT.md": "x\n"},
            rationale="r", evidence={"baseline_total": 1, "candidate_total": 2}, meta_model="deepseek-v4-flash",
        )
        self.assertEqual([r.ok for r in bundle.reviews], [True, False])
        self.assertEqual(bundle.verdicts, ["revise"])
        self.assertEqual(bundle.reviews[0].scores["contract"], 5.0)
        self.assertIsNone(bundle.meta)  # only one usable review -> no meta-review
        self.assertIn("advisory", bundle.to_dict())
        c2 = client([json.dumps(REVIEW), json.dumps({**REVIEW, "verdict": "accept"}), json.dumps(META)])
        bundle2 = review_candidate(c2, ["gemini-3-flash", "grok-4.5"], diff="+x", files={}, rationale="", evidence={}, meta_model="deepseek-v4-flash")
        self.assertEqual(bundle2.meta["verdict"], "revise")
        self.assertEqual(bundle2.verdicts, ["revise", "accept"])
        prompt = next(call for call in c2._transport.calls if call[1] != "--models")[-1]
        self.assertIn("+x", prompt)
        self.assertIn("advisory", prompt)

    def test_tree_html_is_self_contained_and_escaped(self) -> None:
        journal = Journal("r<1>")
        journal.append(CandidateNode(id="root", kind="baseline", stage="root", candidate_id="b", parent_id=None, total=1, max_points=2))
        journal.append(CandidateNode(id="a", kind="draft", stage="s1", candidate_id="<script>", parent_id="root", total=2, max_points=2,
                                     meta={"rationale": "x & y"}, policy_violations=("p",)))
        journal.event("stage_start", stage="s1")
        page = render_tree_html(journal, best_id="root", recommended_id="root", stages=[{"name": "s1", "kind": "working", "iters": 1, "completed": "ok"}],
                                ablation=[{"hunk": "f#0", "verdict": "inert", "preview": "+ <b>"}], diff="--- a\n+++ b\n-old\n+new",
                                usage={"calls": 1, "input_tokens": 2, "output_tokens": 3},
                                reviews={"advisory": "adv", "reviews": [{"model": "m", "ok": True, "verdict": "accept", "overall": 8, "scores": {}, "weaknesses": [], "must_fix": []}], "meta": {"model": "mm", "verdict": "accept", "summary": "s", "top_concern": "t"}})
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertIn("r&lt;1&gt;", page)
        self.assertNotIn("http", page)  # no external assets
        self.assertIn('class="add"', page)
        self.assertIn("meta-review", page)
        self.assertIn("Policy violations", page)
