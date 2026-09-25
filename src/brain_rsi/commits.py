"""Commit log: one append-only line per loop outcome, including the skips.

Reef's scenario commit log records every step — ``skipped: no proposal`` as
much as a publish — so an idle loop is distinguishable from a broken one.
``traces/runs.jsonl`` and the journal only know about evaluated candidates; this
log also records the runs that never evaluated anything and *why*.

Outcomes:

* ``review``   – the decision artifact says ACCEPT FOR HUMAN REVIEW (still no promotion);
* ``rejected`` – evaluated, gate failed (regression, loss, budget, verdict);
* ``skipped``  – nothing evaluated: ``nothing batched``, ``no proposal``,
  ``baseline evaluation failed``, ``best is the baseline``, ``all tie``;
* ``published`` – a human recorded a version (``versions.py``), with the ACC text.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

OUTCOMES = ("review", "rejected", "skipped", "published")


@dataclass(frozen=True)
class CommitEntry:
    at: str
    run_id: str
    command: str  # cycle | search | publish
    outcome: str  # one of OUTCOMES
    reason: str
    baseline_id: str = ""
    candidate_id: str = ""
    baseline_total: float | None = None
    candidate_total: float | None = None
    verdict: dict[str, Any] | None = None
    batch_id: str | None = None
    decision_path: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CommitLog:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def append(self, entry: CommitEntry) -> CommitEntry:
        if entry.outcome not in OUTCOMES:
            raise ValueError(f"unknown outcome {entry.outcome!r}; expected one of {OUTCOMES}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_dict(), sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        return entry

    def record(self, *, run_id: str, command: str, outcome: str, reason: str, **fields: Any) -> CommitEntry:
        return self.append(
            CommitEntry(
                at=datetime.now(timezone.utc).isoformat(),
                run_id=run_id,
                command=command,
                outcome=outcome,
                reason=reason,
                **fields,
            )
        )

    def __iter__(self) -> Iterator[CommitEntry]:
        if not self.path.is_file():
            return iter(())
        entries: list[CommitEntry] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line:
                    entries.append(CommitEntry(**json.loads(line)))
        return iter(entries)

    def tail(self, count: int = 20) -> list[CommitEntry]:
        return list(self)[-count:]

    def counts(self) -> dict[str, int]:
        result = {outcome: 0 for outcome in OUTCOMES}
        for entry in self:
            result[entry.outcome] = result.get(entry.outcome, 0) + 1
        return result
