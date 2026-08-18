"""Candidate runner protocol and an offline fixture adapter."""
from __future__ import annotations

import abc
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .types import CandidateOutput, EvalCase


class CandidateRunner(abc.ABC):
    @property
    @abc.abstractmethod
    def candidate_id(self) -> str:
        """Stable identifier included in benchmark traces."""

    def setup(self, workspace: Path) -> None:
        """Optional setup in an isolated candidate workspace."""

    @abc.abstractmethod
    def run(self, case: EvalCase, budget: Mapping[str, Any]) -> CandidateOutput:
        """Run one case while honoring the supplied remaining budget."""


@dataclass(frozen=True)
class FixtureConfig:
    candidate_id: str
    responses: dict[str, str]
    step_cost: int = 1


class FixtureCandidate(CandidateRunner):
    """Offline runner returning canned responses keyed by eval-case id."""

    def __init__(self, config: FixtureConfig):
        self._config = config

    @property
    def candidate_id(self) -> str:
        return self._config.candidate_id

    def run(self, case: EvalCase, budget: Mapping[str, Any]) -> CandidateOutput:
        started = time.perf_counter()
        if self._config.step_cost > int(budget["budget_steps"]):
            return CandidateOutput(
                candidate_id=self.candidate_id,
                case_id=case.id,
                text="",
                elapsed_s=0.0,
                steps=0,
                error="fixture step cost exceeds remaining budget",
            )
        text = self._config.responses.get(case.id, "")
        return CandidateOutput(
            candidate_id=self.candidate_id,
            case_id=case.id,
            text=text,
            elapsed_s=time.perf_counter() - started,
            steps=self._config.step_cost,
        )
