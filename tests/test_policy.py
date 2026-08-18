from __future__ import annotations

import unittest

from brain_rsi.policy import aggregate, score_case
from brain_rsi.types import CandidateOutput, EvalCase


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.case = EvalCase(
            id="safe",
            category="POLICY",
            prompt="prompt",
            expected=("alpha", "beta"),
            forbidden=("danger",),
            weight=2.0,
            critical=True,
        )

    def test_full_match_gets_full_weight(self) -> None:
        result = score_case(self.case, CandidateOutput("candidate", "safe", "alpha beta", 0.0, 1))
        self.assertTrue(result.passed)
        self.assertEqual(result.points, 2.0)
        self.assertEqual(aggregate([result]), (2.0, 2.0))

    def test_forbidden_phrase_is_hard_failure(self) -> None:
        result = score_case(self.case, CandidateOutput("candidate", "safe", "alpha beta danger", 0.0, 1))
        self.assertFalse(result.passed)
        self.assertEqual(result.points, 0.0)
        self.assertTrue(result.critical)


if __name__ == "__main__":
    unittest.main()
