"""Baseline-vs-candidate benchmark with deterministic acceptance gates."""
from __future__ import annotations

import json
import math
import os
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .candidate import CandidateRunner
from .policy import aggregate, score_case
from .types import CandidateOutput, EvalCase, ScoreResult, TraceRecord


@dataclass
class BenchmarkReport:
    run_id: str
    baseline_id: str
    candidate_id: str
    baseline_scores: list[ScoreResult]
    candidate_scores: list[ScoreResult]
    baseline_total: float
    candidate_total: float
    max_points: float

    def regressions(self) -> list[str]:
        baseline = {score.case_id: score for score in self.baseline_scores}
        return [
            score.case_id
            for score in self.candidate_scores
            if baseline[score.case_id].passed and not score.passed
        ]

    def critical_regressions(self) -> list[str]:
        baseline = {score.case_id: score for score in self.baseline_scores}
        return [
            score.case_id
            for score in self.candidate_scores
            if score.critical and baseline[score.case_id].passed and not score.passed
        ]

    def runner_errors(self) -> list[str]:
        errors: list[str] = []
        for contender, scores in (
            (self.baseline_id, self.baseline_scores),
            (self.candidate_id, self.candidate_scores),
        ):
            errors.extend(f"{contender}:{score.case_id}" for score in scores if score.runner_error)
        return errors

    def budget_violations(self) -> list[str]:
        errors = {
            f"{contender}:{score.case_id}"
            for contender, scores in (
                (self.baseline_id, self.baseline_scores),
                (self.candidate_id, self.candidate_scores),
            )
            for score in scores
            if score.runner_error and "budget" in score.runner_error.casefold()
        }
        return sorted(errors)

    def accepted(self, *, minimum_delta: float = 0.01) -> bool:
        return (
            self.candidate_total >= self.baseline_total + minimum_delta
            and not self.regressions()
            and not self.critical_regressions()
            and not self.runner_errors()
        )


def run_benchmark(
    cases: Sequence[EvalCase],
    baseline: CandidateRunner,
    candidate: CandidateRunner,
    *,
    budget_steps: int = 100,
    budget_seconds: float = 60.0,
    run_id: str | None = None,
    traces_path: Path | None = None,
) -> BenchmarkReport:
    if budget_steps < 1 or budget_seconds <= 0:
        raise ValueError("budgets must be positive")

    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    baseline_outputs = run_suite(cases, baseline, budget_steps, budget_seconds)
    candidate_outputs = run_suite(cases, candidate, budget_steps, budget_seconds)
    baseline_scores = [score_case(case, output) for case, output in zip(cases, baseline_outputs)]
    candidate_scores = [score_case(case, output) for case, output in zip(cases, candidate_outputs)]
    baseline_total, max_points = aggregate(baseline_scores)
    candidate_total, candidate_max = aggregate(candidate_scores)
    if max_points != candidate_max:
        raise RuntimeError("baseline and candidate were not scored on the same cases")

    report = BenchmarkReport(
        run_id=run_id,
        baseline_id=baseline.candidate_id,
        candidate_id=candidate.candidate_id,
        baseline_scores=baseline_scores,
        candidate_scores=candidate_scores,
        baseline_total=baseline_total,
        candidate_total=candidate_total,
        max_points=max_points,
    )
    _append_trace(report, traces_path, budget_steps, budget_seconds)
    return report


def run_suite(
    cases: Sequence[EvalCase],
    runner: CandidateRunner,
    budget_steps: int,
    budget_seconds: float,
) -> list[CandidateOutput]:
    started = time.monotonic()
    used_steps = 0
    outputs: list[CandidateOutput] = []

    for case in cases:
        remaining_seconds = budget_seconds - (time.monotonic() - started)
        remaining_steps = budget_steps - used_steps
        if remaining_seconds <= 0 or remaining_steps <= 0:
            outputs.append(
                CandidateOutput(
                    candidate_id=runner.candidate_id,
                    case_id=case.id,
                    text="",
                    elapsed_s=0.0,
                    steps=0,
                    error="suite budget exhausted",
                )
            )
            continue

        case_started = time.monotonic()
        try:
            output = runner.run(
                case,
                {
                    "budget_steps": remaining_steps,
                    "budget_seconds": remaining_seconds,
                },
            )
        except Exception as exc:
            output = CandidateOutput(
                candidate_id=runner.candidate_id,
                case_id=case.id,
                text="",
                elapsed_s=time.monotonic() - case_started,
                steps=0,
                error=f"runner raised {type(exc).__name__}",
            )

        actual_elapsed = time.monotonic() - case_started
        if not isinstance(output, CandidateOutput):
            output = CandidateOutput(
                candidate_id=runner.candidate_id,
                case_id=case.id,
                text="",
                elapsed_s=actual_elapsed,
                steps=0,
                error=f"runner returned invalid output type {type(output).__name__}",
            )
        else:
            valid_steps = isinstance(output.steps, int) and not isinstance(output.steps, bool) and output.steps >= 0
            valid_elapsed = (
                isinstance(output.elapsed_s, (int, float))
                and not isinstance(output.elapsed_s, bool)
                and math.isfinite(output.elapsed_s)
                and output.elapsed_s >= 0
            )
            error = output.error
            if not valid_steps or not valid_elapsed:
                error = _combine_errors(error, "runner returned invalid resource accounting")
            steps = output.steps if valid_steps else 0
            elapsed = max(float(output.elapsed_s), actual_elapsed) if valid_elapsed else actual_elapsed
            output = replace(
                output,
                candidate_id=runner.candidate_id,
                case_id=case.id,
                elapsed_s=elapsed,
                steps=steps,
                error=error,
            )

        used_steps += output.steps
        if output.steps > remaining_steps or output.elapsed_s > remaining_seconds:
            output = replace(output, error=_combine_errors(output.error, "candidate exceeded its remaining budget"))
        outputs.append(output)

    return outputs


def _combine_errors(existing: str | None, added: str) -> str:
    return f"{existing}; {added}" if existing else added


_run_suite = run_suite  # backwards-compatible alias


def _append_trace(
    report: BenchmarkReport,
    traces_path: Path | None,
    budget_steps: int,
    budget_seconds: float,
) -> None:
    if traces_path is None:
        return

    record = TraceRecord(
        run_id=report.run_id,
        baseline_id=report.baseline_id,
        candidate_id=report.candidate_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        baseline_total=report.baseline_total,
        candidate_total=report.candidate_total,
        max_points=report.max_points,
        accepted=report.accepted(),
        regressions=report.regressions(),
        critical_regressions=report.critical_regressions(),
        budget_violations=report.budget_violations(),
        runner_errors=report.runner_errors(),
        candidate_scores=report.candidate_scores,
        meta={"budget_steps": budget_steps, "budget_seconds": budget_seconds},
    )

    path = Path(traces_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(_trace_to_json(record), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _trace_to_json(record: TraceRecord) -> dict[str, Any]:
    payload = asdict(record)
    payload["candidate_scores"] = [asdict(score) for score in record.candidate_scores]
    return payload
