"""Failure-window batching: a search runs only when enough real failures piled up.

Reef's harness evolution opens the evolve path only from recorded traffic that
fell into the report window (``max_score`` / ``batch_size``); a pass never
triggers anything. Here the same rule sits in front of ``search``: pending
reports whose score is within the window are grouped into one batch, the batch
becomes the maker's "reported failures" context, and the store records which
run consumed it. No batch → no proposal → no evaluation (and the commit log says
so, see ``commits.py``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .reports import Report, ReportStore, describe_failures

DEFAULT_MAX_SCORE = 0.0
DEFAULT_BATCH_SIZE = 3


@dataclass(frozen=True)
class FailureWindow:
    max_score: float = DEFAULT_MAX_SCORE  # reports with score <= max_score are failures
    batch_size: int = DEFAULT_BATCH_SIZE  # minimum failing reports before a search runs
    include_unscored: bool = False  # unscored reports (voided grading) never count by default

    def __post_init__(self) -> None:
        if not 0.0 <= self.max_score <= 1.0:
            raise ValueError("max_score must be within [0, 1]")
        if self.batch_size < 1:
            raise ValueError("batch_size must be >= 1")

    def admits(self, report: Report) -> bool:
        if report.score is None:
            return self.include_unscored
        return report.score <= self.max_score

    def to_dict(self) -> dict[str, Any]:
        return {"max_score": self.max_score, "batch_size": self.batch_size, "include_unscored": self.include_unscored}


@dataclass(frozen=True)
class PendingBatch:
    reports: tuple[Report, ...]
    window: FailureWindow

    @property
    def report_ids(self) -> tuple[str, ...]:
        return tuple(report.report_id for report in self.reports)

    def failure_lines(self, store: ReportStore) -> list[str]:
        return describe_failures(store, self.reports)


def pending_failures(store: ReportStore, window: FailureWindow | None = None) -> list[Report]:
    """Unbatched reports inside the window, oldest first."""
    window = window or FailureWindow()
    pending = [report for report in store.unbatched_reports() if window.admits(report)]
    pending.sort(key=lambda report: report.at)
    return pending


def next_batch(store: ReportStore, window: FailureWindow | None = None) -> PendingBatch | None:
    """The batch that would trigger a search, or None when the window is not full.

    Everything pending goes into one batch (Reef batches the whole day), so a
    late report is not left behind for the next trigger.
    """
    window = window or FailureWindow()
    pending = pending_failures(store, window)
    if len(pending) < window.batch_size:
        return None
    return PendingBatch(reports=tuple(pending), window=window)


def status(store: ReportStore, window: FailureWindow | None = None) -> dict[str, Any]:
    window = window or FailureWindow()
    pending = pending_failures(store, window)
    return {
        "window": window.to_dict(),
        "pending_failures": len(pending),
        "ready": len(pending) >= window.batch_size,
        "missing": max(window.batch_size - len(pending), 0),
    }
