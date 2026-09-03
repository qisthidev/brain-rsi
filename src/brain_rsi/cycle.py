"""Improvement-cycle decision artifacts; promotion intentionally stays manual."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from typing import TYPE_CHECKING, Any

from .benchmark import BenchmarkReport

if TYPE_CHECKING:  # avoids an import cycle at runtime
    from .search import SearchResult


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
    runner_errors: list[str]
    promotion: str
    created_at: str
    source_id: str | None = None
    source_digest: str | None = None
    search: dict[str, Any] | None = None  # tree-search summary (journal, stages, ablation, diff)
    verdict: dict[str, Any] | None = None  # per-case win/loss/tie/unscored (verdict.py)
    rejection_reason: str = ""  # empty when accepted for review
    suite_digest: str | None = None  # sha256 of eval/cases.json the run used (CLAUDE.md rule 10)
    batch_id: str | None = None  # failure batch (triggers.py) that opened this run, if any
    gain: dict[str, Any] | None = None  # preregistered gain verdict vs control runs (gain.py)


def make_decision(
    report: BenchmarkReport,
    *,
    source_id: str | None = None,
    source_digest: str | None = None,
    suite_digest: str | None = None,
    batch_id: str | None = None,
    gain: dict[str, Any] | None = None,
) -> CycleDecision:
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
        runner_errors=report.runner_errors(),
        promotion="human-reviewed patch or pull request required; never automatic",
        created_at=datetime.now(timezone.utc).isoformat(),
        source_id=source_id,
        source_digest=source_digest,
        verdict=report.verdict().to_dict(),
        rejection_reason=report.rejection_reason(),
        suite_digest=suite_digest,
        batch_id=batch_id,
        gain=gain,
    )


def write_decision(decision: CycleDecision, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{decision.run_id}-{decision.candidate_id}.json"
    with path.open("x", encoding="utf-8") as handle:
        json.dump(asdict(decision), handle, indent=2, sort_keys=True)
        handle.write("\n")
    return path


def make_search_decision(
    result: "SearchResult",
    *,
    source_id: str | None = None,
    source_digest: str | None = None,
    journal_path: Path | None = None,
    minimum_delta: float = 0.01,
    usage: dict[str, Any] | None = None,
    models: dict[str, str] | None = None,
    reviews: dict[str, Any] | None = None,
    html_path: Path | None = None,
    proposals: list[dict[str, Any]] | None = None,
    suite_digest: str | None = None,
    batch_id: str | None = None,
    gain: dict[str, Any] | None = None,
) -> CycleDecision:
    """Decision artifact for a tree-search run: the recommended node (ablation-
    minimised best when it keeps the score) versus the baseline root. Like
    ``make_decision`` it only says whether the candidate qualifies for human
    review; promotion stays manual."""
    root = result.root
    node = result.recommended
    best = result.best
    return CycleDecision(
        run_id=result.journal.run_id,
        baseline_id=root.candidate_id,
        candidate_id=node.candidate_id,
        baseline_total=root.total,
        candidate_total=node.total,
        max_points=root.max_points,
        accepted_for_review=result.accepted_for_review(minimum_delta),
        regressions=list(node.regressions),
        critical_regressions=list(node.critical_regressions),
        budget_violations=list(node.budget_violations),
        runner_errors=list(node.runner_errors),
        promotion="human-reviewed patch or pull request required; never automatic",
        created_at=datetime.now(timezone.utc).isoformat(),
        source_id=source_id,
        source_digest=source_digest,
        verdict=node.meta.get("verdict"),
        rejection_reason=result.rejection_reason(minimum_delta),
        suite_digest=suite_digest,
        batch_id=batch_id,
        gain=gain,
        search={
            "journal_path": str(journal_path) if journal_path else None,
            "journal": result.journal.summary(),
            "evaluations": result.evaluations,
            "elapsed_s": round(result.elapsed_s, 3),
            "stopped_reason": result.stopped_reason,
            "stages": result.stages,
            "best_node": {
                "id": best.id,
                "candidate_id": best.candidate_id,
                "kind": best.kind,
                "total": best.total,
                "changed_files": list(best.changed_files),
                "change_size": best.change_size,
                "runner_errors": list(best.runner_errors),
                "rationale": str(best.meta.get("rationale", "")),
            },
            "recommended_node": {
                "id": node.id,
                "candidate_id": node.candidate_id,
                "kind": node.kind,
                "total": node.total,
                "changed_files": list(node.changed_files),
                "change_size": node.change_size,
                "runner_errors": list(node.runner_errors),
                "is_minimal": result.minimal is not None and node.id == result.minimal.id,
                "rationale": str(node.meta.get("rationale", "")),
                "proposal": node.meta.get("proposal"),
            },
            "ablation": result.ablation,
            "policy_violations": [
                {"node": n.id, "candidate_id": n.candidate_id, "violations": list(n.policy_violations)}
                for n in result.journal.violation_nodes
            ],
            "diff": result.recommended_diff(),
            "usage": usage,
            "models": models,
            "reviews": reviews,
            "html_path": str(html_path) if html_path else None,
            "proposals": proposals,
        },
    )
