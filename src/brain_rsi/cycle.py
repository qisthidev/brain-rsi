"""Improvement-cycle decision artifacts; promotion intentionally stays manual."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .benchmark import BenchmarkReport


@dataclass(frozen=True)
class CycleDecision:
    run_id: str
    baseline_id: str
    candidate_id: str
    baseline_total: float
    candidate_total: float
    max_points: float
    accepted_for_review: bool
    regressions: list[str]
    critical_regressions: list[str]
    budget_violations: list[str]
    promotion: str
    created_at: str


def make_decision(report: BenchmarkReport) -> CycleDecision:
    return CycleDecision(
        run_id=report.run_id,
        baseline_id=report.baseline_id,
        candidate_id=report.candidate_id,
        baseline_total=report.baseline_total,
        candidate_total=report.candidate_total,
        max_points=report.max_points,
        accepted_for_review=report.accepted(),
        regressions=report.regressions(),
        critical_regressions=report.critical_regressions(),
        budget_violations=report.budget_violations(),
        promotion="human-reviewed patch or pull request required; never automatic",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def write_decision(decision: CycleDecision, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{decision.run_id}-{decision.candidate_id}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(asdict(decision), handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path
