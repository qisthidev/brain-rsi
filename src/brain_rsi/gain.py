"""Preregistered gain criterion: claim improvement only against a control run.

CLAUDE.md rule 10 forbids claiming RSI improvement unless the same immutable
suite was run against baseline and candidate. One baseline run says nothing
about variance; a live runner is stochastic. Following the SkillClaw campaign
(``stats.py``: a gain is real only when it beats the control mean by more than
``k`` control standard deviations, one sided), the criterion is fixed in
``eval/gain_criterion.json`` *before* any candidate is measured, and control
runs (baseline vs baseline, same suite digest) are appended to
``traces/control.jsonl``.

``claim`` never changes a decision artifact: it reports whether the artifact's
candidate total clears the preregistered threshold for its suite digest, and
records the answer in the commit log.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class GainError(Exception):
    pass


def suite_digest(cases_path: Path) -> str:
    return hashlib.sha256(Path(cases_path).read_bytes()).hexdigest()


@dataclass(frozen=True)
class GainCriterion:
    control_runs_min: int
    sd_multiplier: float
    one_sided: bool
    metric: str
    registered_at: str
    note: str = ""

    @classmethod
    def load(cls, path: Path) -> "GainCriterion":
        if not path.is_file():
            raise GainError(f"gain criterion not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        try:
            criterion = cls(
                control_runs_min=int(data["control_runs_min"]),
                sd_multiplier=float(data["sd_multiplier"]),
                one_sided=bool(data.get("one_sided", True)),
                metric=str(data.get("metric", "candidate_total")),
                registered_at=str(data["registered_at"]),
                note=str(data.get("note", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GainError(f"invalid gain criterion {path}: {exc}") from exc
        if criterion.control_runs_min < 2 or criterion.sd_multiplier <= 0:
            raise GainError("gain criterion needs control_runs_min >= 2 and sd_multiplier > 0")
        if criterion.metric != "candidate_total":
            raise GainError("only the candidate_total metric is supported")
        return criterion

    def to_dict(self) -> dict[str, Any]:
        return {
            "control_runs_min": self.control_runs_min,
            "sd_multiplier": self.sd_multiplier,
            "one_sided": self.one_sided,
            "metric": self.metric,
            "registered_at": self.registered_at,
            "note": self.note,
        }


@dataclass(frozen=True)
class ControlRun:
    at: str
    run_id: str
    baseline_id: str
    suite_digest: str
    total: float
    max_points: float
    meta: dict[str, Any] = field(default_factory=dict)


class ControlLog:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def append(self, run: ControlRun) -> ControlRun:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(run.__dict__, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return run

    def record(self, *, run_id: str, baseline_id: str, suite: str, total: float, max_points: float, **meta: Any) -> ControlRun:
        return self.append(
            ControlRun(
                at=datetime.now(timezone.utc).isoformat(),
                run_id=run_id,
                baseline_id=baseline_id,
                suite_digest=suite,
                total=total,
                max_points=max_points,
                meta=dict(meta),
            )
        )

    def __iter__(self) -> Iterator[ControlRun]:
        if not self.path.is_file():
            return iter(())
        runs: list[ControlRun] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    runs.append(ControlRun(**json.loads(line)))
        return iter(runs)

    def matching(self, *, baseline_id: str, suite: str) -> list[ControlRun]:
        return [run for run in self if run.baseline_id == baseline_id and run.suite_digest == suite]


@dataclass(frozen=True)
class GainVerdict:
    claim: bool
    reason: str
    n_control: int
    control_mean: float | None
    control_sd: float | None
    threshold: float | None
    candidate_total: float
    excess_sd: float | None
    degenerate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def evaluate_gain(criterion: GainCriterion, controls: list[ControlRun], candidate_total: float) -> GainVerdict:
    n = len(controls)
    if n < criterion.control_runs_min:
        return GainVerdict(
            claim=False,
            reason=f"only {n} control run(s) on this suite/baseline; criterion needs {criterion.control_runs_min}",
            n_control=n,
            control_mean=None,
            control_sd=None,
            threshold=None,
            candidate_total=candidate_total,
            excess_sd=None,
        )
    totals = [run.total for run in controls]
    mean = statistics.fmean(totals)
    sd = statistics.stdev(totals) if n > 1 else 0.0
    if sd == 0.0 or not math.isfinite(sd):
        # Deterministic control (e.g. fixtures): no variance means the preregistered
        # "k sd above the mean" test cannot be applied, so no gain claim is supported.
        above = candidate_total > mean
        return GainVerdict(
            claim=False,
            reason=(
                f"control has no variance (sd=0 over {n} runs), so the criterion cannot be applied; "
                f"candidate {candidate_total:.2f} is {'above' if above else 'not above'} the control mean "
                f"{mean:.2f} but a claim needs a stochastic (live) runner"
            ),
            n_control=n,
            control_mean=mean,
            control_sd=0.0,
            threshold=mean,
            candidate_total=candidate_total,
            excess_sd=None,
            degenerate=True,
        )
    threshold = mean + criterion.sd_multiplier * sd
    excess = (candidate_total - mean) / sd
    claim = candidate_total > threshold
    return GainVerdict(
        claim=claim,
        reason=(
            f"candidate {candidate_total:.2f} vs control mean {mean:.2f} ± {sd:.2f} (n={n}); "
            f"threshold {threshold:.2f} = mean + {criterion.sd_multiplier:g} sd; excess {excess:+.2f} sd"
        ),
        n_control=n,
        control_mean=mean,
        control_sd=sd,
        threshold=threshold,
        candidate_total=candidate_total,
        excess_sd=excess,
    )
