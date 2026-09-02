"""Candidate journal: an append-only tree of evaluated candidates.

Borrowed from AI-Scientist-v2's ``Journal``/``Node`` (treesearch/journal.py) but
with two deliberate inversions (see wiki/research/ai-scientist-v2-untuk-brain-v2.md):

* the best node is chosen deterministically from scores, never by an LLM;
* nodes that violate the mutation policy are poisoned (never debugged, never parents).

The journal is persisted as JSONL, one record per line, append-only (CLAUDE.md
rule 9): a run header, then one ``node`` line per evaluated candidate, interleaved
with ``event`` lines for stage transitions. Nothing is ever rewritten.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from .benchmark import BenchmarkReport
from .types import ScoreResult

NODE_KINDS = ("baseline", "draft", "debug", "improve", "ablate")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CandidateNode:
    """One evaluated candidate (a set of allowlisted file contents) in the search tree."""

    id: str
    kind: str  # one of NODE_KINDS
    stage: str
    candidate_id: str
    parent_id: str | None
    total: float
    max_points: float
    changed_files: tuple[str, ...] = ()
    change_size: int = 0  # changed lines vs the baseline files
    passed_cases: tuple[str, ...] = ()
    regressions: tuple[str, ...] = ()  # vs the baseline root
    critical_regressions: tuple[str, ...] = ()
    budget_violations: tuple[str, ...] = ()
    runner_errors: tuple[str, ...] = ()
    policy_violations: tuple[str, ...] = ()  # validator findings -> poisoned node
    debug_depth: int = 0
    analysis: str = ""  # advisory text only; never feeds the score
    created_at: str = field(default_factory=_now)
    scores: list[ScoreResult] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in NODE_KINDS:
            raise ValueError(f"unknown node kind {self.kind!r}")

    # ---- status ----
    @property
    def is_violation(self) -> bool:
        """Poisoned: touched a forbidden path, leaked a secret, etc. Never a parent."""
        return bool(self.policy_violations)

    @property
    def is_buggy(self) -> bool:
        """Evaluated but unacceptable (regression / critical / budget). Debuggable."""
        return bool(self.regressions or self.critical_regressions or self.runner_errors)

    @property
    def is_good(self) -> bool:
        return not self.is_violation and not self.is_buggy

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["scores"] = [asdict(score) for score in self.scores]
        return payload

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CandidateNode":
        data = dict(data)
        data["scores"] = [ScoreResult(**score) for score in data.get("scores", [])]
        for key in (
            "changed_files",
            "passed_cases",
            "regressions",
            "critical_regressions",
            "budget_violations",
            "runner_errors",
            "policy_violations",
        ):
            data[key] = tuple(data.get(key, ()))
        return cls(**data)


def new_node_id() -> str:
    return uuid.uuid4().hex[:12]


def node_from_report(
    report: BenchmarkReport,
    *,
    kind: str = "draft",
    stage: str = "cycle",
    parent_id: str | None = None,
    changed_files: Iterable[str] = (),
    change_size: int = 0,
    debug_depth: int = 0,
    node_id: str | None = None,
) -> tuple[CandidateNode, CandidateNode]:
    """Backwards-compatible bridge: turn today's one-shot ``cycle`` report into
    a (baseline root, candidate child) pair so an existing run is journal node #1."""
    root = CandidateNode(
        id=parent_id or new_node_id(),
        kind="baseline",
        stage=stage,
        candidate_id=report.baseline_id,
        parent_id=None,
        total=report.baseline_total,
        max_points=report.max_points,
        passed_cases=tuple(score.case_id for score in report.baseline_scores if score.passed),
        budget_violations=tuple(
            item for item in report.budget_violations() if item.startswith(f"{report.baseline_id}:")
        ),
        runner_errors=tuple(
            item for item in report.runner_errors() if item.startswith(f"{report.baseline_id}:")
        ),
        scores=list(report.baseline_scores),
    )
    child = CandidateNode(
        id=node_id or new_node_id(),
        kind=kind,
        stage=stage,
        candidate_id=report.candidate_id,
        parent_id=root.id,
        total=report.candidate_total,
        max_points=report.max_points,
        changed_files=tuple(changed_files),
        change_size=change_size,
        passed_cases=tuple(score.case_id for score in report.candidate_scores if score.passed),
        regressions=tuple(report.regressions()),
        critical_regressions=tuple(report.critical_regressions()),
        budget_violations=tuple(report.budget_violations()),
        runner_errors=tuple(
            item for item in report.runner_errors() if item.startswith(f"{report.candidate_id}:")
        ),
        debug_depth=debug_depth,
        scores=list(report.candidate_scores),
    )
    return root, child


class JournalError(Exception):
    """Raised on an inconsistent journal (unknown parent, duplicate id, bad file)."""


class Journal:
    """In-memory tree plus optional append-only JSONL persistence."""

    def __init__(self, run_id: str, *, path: Path | None = None):
        self.run_id = run_id
        self.path = Path(path) if path else None
        self.nodes: list[CandidateNode] = []
        self.events: list[dict[str, Any]] = []
        self._by_id: dict[str, CandidateNode] = {}
        if self.path is not None and not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._append_line({"type": "run", "run_id": run_id, "created_at": _now()})

    # ---- mutation (append only) ----
    def append(self, node: CandidateNode) -> CandidateNode:
        if node.id in self._by_id:
            raise JournalError(f"duplicate node id {node.id}")
        if node.parent_id is not None and node.parent_id not in self._by_id:
            raise JournalError(f"unknown parent {node.parent_id} for node {node.id}")
        if node.parent_id is None and self.nodes:
            raise JournalError("only the first (baseline) node may be a root")
        self.nodes.append(node)
        self._by_id[node.id] = node
        self._append_line({"type": "node", "run_id": self.run_id, **node.to_dict()})
        return node

    def event(self, name: str, **payload: Any) -> dict[str, Any]:
        record = {"type": "event", "run_id": self.run_id, "event": name, "at": _now(), **payload}
        self.events.append(record)
        self._append_line(record)
        return record

    def _append_line(self, record: dict[str, Any]) -> None:
        if self.path is None:
            return
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # ---- queries ----
    def __len__(self) -> int:
        return len(self.nodes)

    def __iter__(self) -> Iterator[CandidateNode]:
        return iter(self.nodes)

    def get(self, node_id: str) -> CandidateNode:
        try:
            return self._by_id[node_id]
        except KeyError as exc:
            raise JournalError(f"unknown node {node_id}") from exc

    @property
    def root(self) -> CandidateNode | None:
        return self.nodes[0] if self.nodes else None

    @property
    def draft_nodes(self) -> list[CandidateNode]:
        return [node for node in self.nodes if node.kind == "draft"]

    @property
    def good_nodes(self) -> list[CandidateNode]:
        return [node for node in self.nodes if node.is_good]

    @property
    def buggy_nodes(self) -> list[CandidateNode]:
        return [node for node in self.nodes if node.is_buggy and not node.is_violation]

    @property
    def violation_nodes(self) -> list[CandidateNode]:
        return [node for node in self.nodes if node.is_violation]

    def children(self, node: CandidateNode) -> list[CandidateNode]:
        return [child for child in self.nodes if child.parent_id == node.id]

    def is_leaf(self, node: CandidateNode) -> bool:
        return not self.children(node)

    def tree_root(self, node: CandidateNode) -> CandidateNode:
        """The draft ancestor that identifies this subtree (the baseline for itself)."""
        current = node
        while current.parent_id is not None and current.kind != "draft":
            current = self.get(current.parent_id)
        return current

    def debuggable_nodes(self, max_debug_depth: int) -> list[CandidateNode]:
        return [
            node
            for node in self.buggy_nodes
            if self.is_leaf(node) and node.debug_depth < max_debug_depth
        ]

    def best_node(self, *, among: Iterable[CandidateNode] | None = None) -> CandidateNode | None:
        """Deterministic best: highest total, then fewest changed lines, then earliest.

        The baseline root competes too, so "no candidate beat the baseline" is
        expressed as ``best_node() is root``.
        """
        pool = list(among) if among is not None else self.good_nodes
        if not pool:
            return None
        order = {node.id: index for index, node in enumerate(self.nodes)}
        return min(pool, key=lambda node: (-node.total, node.change_size, order[node.id]))

    def summary(self) -> dict[str, Any]:
        best = self.best_node()
        return {
            "run_id": self.run_id,
            "nodes": len(self.nodes),
            "good": len(self.good_nodes),
            "buggy": len(self.buggy_nodes),
            "violations": len(self.violation_nodes),
            "by_kind": {kind: sum(1 for n in self.nodes if n.kind == kind) for kind in NODE_KINDS},
            "baseline_total": self.root.total if self.root else None,
            "best_id": best.id if best else None,
            "best_total": best.total if best else None,
            "max_points": self.root.max_points if self.root else None,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "nodes": [node.to_dict() for node in self.nodes],
            "events": list(self.events),
        }


def load_journal(path: str | Path) -> Journal:
    """Rebuild a journal from its JSONL file (read-only; does not re-open for append)."""
    file_path = Path(path)
    if not file_path.is_file():
        raise JournalError(f"journal not found: {file_path}")
    journal: Journal | None = None
    with file_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise JournalError(f"{file_path}:{line_number}: invalid JSON") from exc
            record_type = record.get("type")
            if record_type == "run":
                if journal is not None:
                    raise JournalError(f"{file_path}:{line_number}: second run header")
                journal = Journal(str(record["run_id"]))
                continue
            if journal is None:
                raise JournalError(f"{file_path}:{line_number}: record before run header")
            if record_type == "node":
                data = {k: v for k, v in record.items() if k not in {"type", "run_id"}}
                journal.append(CandidateNode.from_dict(data))
            elif record_type == "event":
                journal.events.append(record)
            else:
                raise JournalError(f"{file_path}:{line_number}: unknown record type {record_type!r}")
    if journal is None:
        raise JournalError(f"{file_path}: empty journal")
    return journal
