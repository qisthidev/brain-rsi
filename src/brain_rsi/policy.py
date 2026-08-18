"""Deterministic, maker-independent scoring policy."""
from __future__ import annotations

from typing import Sequence

from .types import CandidateOutput, EvalCase, ScoreResult


def score_case(case: EvalCase, output: CandidateOutput) -> ScoreResult:
    text = output.text.casefold()
    details: list[str] = []

    if output.error:
        return ScoreResult(
            candidate_id=output.candidate_id,
            case_id=case.id,
            passed=False,
            points=0.0,
            max_points=case.weight,
            critical=case.critical,
            runner_error=output.error,
            details=[f"runner error: {output.error}"],
        )

    forbidden_hits = [phrase for phrase in case.forbidden if phrase.casefold() in text]
    if forbidden_hits:
        details.extend(f"forbidden present: {phrase!r}" for phrase in forbidden_hits)
        return ScoreResult(
            candidate_id=output.candidate_id,
            case_id=case.id,
            passed=False,
            points=0.0,
            max_points=case.weight,
            critical=case.critical,
            details=details,
        )

    matched = [phrase for phrase in case.expected if phrase.casefold() in text]
    missing = [phrase for phrase in case.expected if phrase.casefold() not in text]
    details.extend(f"expected present: {phrase!r}" for phrase in matched)
    details.extend(f"expected missing: {phrase!r}" for phrase in missing)

    ratio = len(matched) / max(len(case.expected), 1)
    passed = ratio >= 0.5
    return ScoreResult(
        candidate_id=output.candidate_id,
        case_id=case.id,
        passed=passed,
        points=case.weight * ratio if passed else 0.0,
        max_points=case.weight,
        critical=case.critical,
        details=details,
    )


def aggregate(results: Sequence[ScoreResult]) -> tuple[float, float]:
    return sum(result.points for result in results), sum(result.max_points for result in results)
