"""Shared data types and safety constants."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_BUDGET_STEPS = 100
DEFAULT_BUDGET_SECONDS = 60.0

# A candidate may propose changes only to these paths in its isolated workspace.
# Mirrors sources/registry.json (source brain-v2, role target): the agent surface only.
TARGET_MUTABLE_ALLOWLIST = (
    "agent/",
    ".claude/skills/",
)

# CLAUDE.md is the safety contract, not the agent prompt: never mutable for the target.
# (Archive sources may still list their own CLAUDE.md for read-only ingest, so this is a
# target denylist, not part of the global immutable denylist below.)
TARGET_MUTABLE_DENYLIST = (
    "CLAUDE.md",
)

# The maker must never edit its own tests, scorer, traces, or promotion policy.
TARGET_IMMUTABLE_DENYLIST = (
    "raw/",
    "eval/",
    "tests/",
    "traces/",
    ".git/",
    ".github/",
)


@dataclass(frozen=True)
class EvalCase:
    id: str
    category: str
    prompt: str
    expected: tuple[str, ...]
    forbidden: tuple[str, ...]
    weight: float = 1.0
    critical: bool = False
    source: str = ""  # registered source id this case is grounded in; "" = global


@dataclass(frozen=True)
class CandidateOutput:
    candidate_id: str
    case_id: str
    text: str
    elapsed_s: float
    steps: int
    error: str | None = None


@dataclass
class ScoreResult:
    candidate_id: str
    case_id: str
    passed: bool
    points: float
    max_points: float
    critical: bool
    runner_error: str | None = None
    details: list[str] = field(default_factory=list)
    unscored: bool = False  # sentinel: grading voided by a runner error (never a fake zero)


@dataclass
class TraceRecord:
    run_id: str
    baseline_id: str
    candidate_id: str
    timestamp: str
    baseline_total: float
    candidate_total: float
    max_points: float
    accepted: bool
    regressions: list[str]
    critical_regressions: list[str]
    budget_violations: list[str]
    runner_errors: list[str]
    candidate_scores: list[ScoreResult]
    meta: dict[str, Any] = field(default_factory=dict)
