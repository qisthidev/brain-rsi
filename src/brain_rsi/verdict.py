"""Per-case win / loss / tie verdict with an explicit *unscored* bucket.

Reef's harness gate publishes on wins vs losses per task and treats an episode
error as "not graded" (a sentinel, never a fake zero). ``BenchmarkReport`` and
the search already reject any runner error outright; this module adds the
per-case verdict so a decision artifact, trace or commit-log entry says *which*
cases carried the delta, and so a candidate can never be accepted on total
points alone while quietly losing points on some case.

Rules (deterministic, maker-independent):

* a case where either side has a runner error is ``unscored`` — it is neither a
  win nor a loss and never contributes to the gate;
* ``win``: candidate points > baseline points; ``loss``: candidate < baseline;
  ``tie`` otherwise;
* ``regressions`` (pass→fail) are a subset of losses and stay a hard reject in
  the existing gates; the verdict adds ``losses == 0`` as a gate condition.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from .types import ScoreResult


@dataclass(frozen=True)
class CaseVerdict:
    case_id: str
    outcome: str  # win | loss | tie | unscored
    baseline_points: float
    candidate_points: float
    critical: bool = False
    reason: str = ""


@dataclass
class Verdict:
    cases: list[CaseVerdict] = field(default_factory=list)

    @property
    def wins(self) -> list[str]:
        return [c.case_id for c in self.cases if c.outcome == "win"]

    @property
    def losses(self) -> list[str]:
        return [c.case_id for c in self.cases if c.outcome == "loss"]

    @property
    def ties(self) -> list[str]:
        return [c.case_id for c in self.cases if c.outcome == "tie"]

    @property
    def unscored(self) -> list[str]:
        return [c.case_id for c in self.cases if c.outcome == "unscored"]

    @property
    def all_tie(self) -> bool:
        return bool(self.cases) and all(c.outcome == "tie" for c in self.cases)

    def gate(self) -> tuple[bool, str]:
        """Reef-style publish condition on top of the existing gates."""
        if self.unscored:
            return False, f"{len(self.unscored)} case(s) unscored (runner error on one side)"
        if self.losses:
            return False, f"{len(self.losses)} case(s) lost points: {', '.join(self.losses)}"
        if not self.wins:
            return False, "no case won points (all tie)"
        return True, f"{len(self.wins)} win(s), 0 losses, {len(self.ties)} tie(s)"

    def to_dict(self) -> dict[str, Any]:
        ok, reason = self.gate()
        return {
            "wins": self.wins,
            "losses": self.losses,
            "ties": len(self.ties),
            "unscored": self.unscored,
            "gate_ok": ok,
            "gate_reason": reason,
        }

    def short(self) -> str:
        return f"W{len(self.wins)} L{len(self.losses)} T{len(self.ties)} U{len(self.unscored)}"


def compare(baseline: Sequence[ScoreResult], candidate: Sequence[ScoreResult]) -> Verdict:
    """Pair scores by case id; both sides must cover the same cases."""
    base_by_id = {score.case_id: score for score in baseline}
    cand_by_id = {score.case_id: score for score in candidate}
    if set(base_by_id) != set(cand_by_id):
        raise ValueError("baseline and candidate were not scored on the same cases")
    verdict = Verdict()
    for case_id in (score.case_id for score in baseline):
        base, cand = base_by_id[case_id], cand_by_id[case_id]
        if base.runner_error or cand.runner_error:
            side = "baseline" if base.runner_error else "candidate"
            if base.runner_error and cand.runner_error:
                side = "both sides"
            verdict.cases.append(
                CaseVerdict(case_id, "unscored", base.points, cand.points, cand.critical, f"runner error on {side}")
            )
            continue
        if cand.points > base.points:
            outcome = "win"
        elif cand.points < base.points:
            outcome = "loss"
        else:
            outcome = "tie"
        reason = ""
        if base.passed and not cand.passed:
            reason = "regression (pass -> fail)"
        elif not base.passed and cand.passed:
            reason = "newly passing"
        verdict.cases.append(CaseVerdict(case_id, outcome, base.points, cand.points, cand.critical, reason))
    return verdict
