"""Receipts and feedback reports: the observation channel from real use.

Borrowed from Reef's serve → observe step (``x-reef-agent-record-id`` receipts and
``/reef/report``): every real interaction of the agent surface (a skill invocation,
a session, a case run) can be issued a *receipt*; later a human or a harness files
a *report* against one or more receipts with a score in [0, 1] and free-text
feedback. Reports are the only signal that comes from outside the fixed eval
suite, and the failure window in ``triggers.py`` batches them into search runs.

Storage is one append-only JSONL file (CLAUDE.md rule 9): ``receipt``, ``report``
and ``batch`` records. Nothing is rewritten; "consumed" is expressed by a later
``batch`` record that references report ids.

Boundaries kept:

* a report against an unknown receipt is refused (fail closed);
* summaries and feedback are secret-scanned before they are stored, and host
  paths are refused — the file is content that later reaches a maker prompt;
* the store never touches the agent surface, ``eval/`` or the scorer.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from .sources import find_secret_signature
from .validator import HOST_PATH_RE

RECEIPT_KINDS = ("skill", "session", "case", "task")
REPORT_SOURCES = ("human", "harness")
MAX_TEXT_CHARS = 2000


class ReportError(Exception):
    """Invalid receipt/report input or an inconsistent store."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def clean_text(value: str, *, label: str) -> str:
    """Reject secrets and host paths in free text; trim to a bounded length."""
    text = str(value or "").strip()
    if len(text) > MAX_TEXT_CHARS:
        raise ReportError(f"{label} exceeds {MAX_TEXT_CHARS} characters")
    signature = find_secret_signature(text)
    if signature:
        raise ReportError(f"{label} looks like it contains a secret ({signature}); refused")
    if HOST_PATH_RE.search(text):
        raise ReportError(f"{label} contains a host-bound absolute path; refused")
    return text


@dataclass(frozen=True)
class Receipt:
    receipt_id: str
    at: str
    kind: str  # one of RECEIPT_KINDS
    ref: str  # skill name, session name, case id, task id
    summary: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "receipt", **asdict(self)}


@dataclass(frozen=True)
class Report:
    report_id: str
    at: str
    receipt_ids: tuple[str, ...]
    score: float | None  # None = unscored (an error voided grading), never a fake zero
    feedback: str
    source: str = "human"  # one of REPORT_SOURCES
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def unscored(self) -> bool:
        return self.score is None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["receipt_ids"] = list(self.receipt_ids)
        return {"type": "report", **payload}


@dataclass(frozen=True)
class Batch:
    batch_id: str
    at: str
    report_ids: tuple[str, ...]
    run_id: str
    window: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["report_ids"] = list(self.report_ids)
        return {"type": "batch", **payload}


class ReportStore:
    """Append-only receipts/reports/batches in one JSONL file."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._receipts: dict[str, Receipt] = {}
        self._reports: dict[str, Report] = {}
        self._batches: list[Batch] = []
        if self.path.is_file():
            self._load()

    # ---- persistence ----
    def _load(self) -> None:
        with self.path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ReportError(f"{self.path}:{line_number}: invalid JSON") from exc
                kind = record.pop("type", None)
                if kind == "receipt":
                    item = Receipt(**record)
                    self._receipts[item.receipt_id] = item
                elif kind == "report":
                    record["receipt_ids"] = tuple(record.get("receipt_ids", ()))
                    report = Report(**record)
                    self._reports[report.report_id] = report
                elif kind == "batch":
                    record["report_ids"] = tuple(record.get("report_ids", ()))
                    self._batches.append(Batch(**record))
                else:
                    raise ReportError(f"{self.path}:{line_number}: unknown record type {kind!r}")

    def _append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    # ---- mutation (append only) ----
    def issue_receipt(self, kind: str, ref: str, *, summary: str = "", meta: dict[str, Any] | None = None) -> Receipt:
        if kind not in RECEIPT_KINDS:
            raise ReportError(f"unknown receipt kind {kind!r}; expected one of {RECEIPT_KINDS}")
        ref = clean_text(ref, label="ref")
        if not ref:
            raise ReportError("receipt ref must not be empty")
        receipt = Receipt(
            receipt_id=_new_id("rcpt"),
            at=_now(),
            kind=kind,
            ref=ref,
            summary=clean_text(summary, label="summary"),
            meta=dict(meta or {}),
        )
        self._receipts[receipt.receipt_id] = receipt
        self._append(receipt.to_dict())
        return receipt

    def report(
        self,
        receipt_ids: Sequence[str],
        *,
        score: float | None,
        feedback: str = "",
        source: str = "human",
        meta: dict[str, Any] | None = None,
    ) -> Report:
        if not receipt_ids:
            raise ReportError("a report must reference at least one receipt")
        unknown = [rid for rid in receipt_ids if rid not in self._receipts]
        if unknown:
            raise ReportError(f"unknown receipt id(s): {unknown}")
        if score is not None:
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise ReportError("score must be a number in [0, 1] or null")
            score = float(score)
            if not 0.0 <= score <= 1.0 or score != score:
                raise ReportError("score must be within [0, 1]")
        if source not in REPORT_SOURCES:
            raise ReportError(f"unknown report source {source!r}; expected one of {REPORT_SOURCES}")
        feedback = clean_text(feedback, label="feedback")
        if score is None and not feedback:
            raise ReportError("an unscored report must carry feedback explaining why grading was voided")
        report = Report(
            report_id=_new_id("rpt"),
            at=_now(),
            receipt_ids=tuple(dict.fromkeys(receipt_ids)),
            score=score,
            feedback=feedback,
            source=source,
            meta=dict(meta or {}),
        )
        self._reports[report.report_id] = report
        self._append(report.to_dict())
        return report

    def record_batch(self, report_ids: Sequence[str], *, run_id: str, window: dict[str, Any] | None = None) -> Batch:
        unknown = [rid for rid in report_ids if rid not in self._reports]
        if unknown:
            raise ReportError(f"unknown report id(s): {unknown}")
        already = set(self.batched_report_ids()) & set(report_ids)
        if already:
            raise ReportError(f"report(s) already batched: {sorted(already)}")
        batch = Batch(
            batch_id=_new_id("batch"),
            at=_now(),
            report_ids=tuple(dict.fromkeys(report_ids)),
            run_id=run_id,
            window=dict(window or {}),
        )
        self._batches.append(batch)
        self._append(batch.to_dict())
        return batch

    # ---- queries ----
    def receipts(self) -> list[Receipt]:
        return list(self._receipts.values())

    def reports(self) -> list[Report]:
        return list(self._reports.values())

    def batches(self) -> list[Batch]:
        return list(self._batches)

    def get_receipt(self, receipt_id: str) -> Receipt:
        try:
            return self._receipts[receipt_id]
        except KeyError as exc:
            raise ReportError(f"unknown receipt {receipt_id}") from exc

    def get_report(self, report_id: str) -> Report:
        try:
            return self._reports[report_id]
        except KeyError as exc:
            raise ReportError(f"unknown report {report_id}") from exc

    def batched_report_ids(self) -> set[str]:
        return {rid for batch in self._batches for rid in batch.report_ids}

    def unbatched_reports(self) -> list[Report]:
        batched = self.batched_report_ids()
        return [report for report in self._reports.values() if report.report_id not in batched]

    def unreported_receipts(self) -> list[Receipt]:
        reported = {rid for report in self._reports.values() for rid in report.receipt_ids}
        return [receipt for receipt in self._receipts.values() if receipt.receipt_id not in reported]

    def summary(self) -> dict[str, Any]:
        reports = self.reports()
        scored = [r for r in reports if r.score is not None]
        return {
            "path": str(self.path),
            "receipts": len(self._receipts),
            "reports": len(reports),
            "unscored_reports": len(reports) - len(scored),
            "failing_reports": sum(1 for r in scored if r.score == 0.0),
            "unbatched_reports": len(self.unbatched_reports()),
            "unreported_receipts": len(self.unreported_receipts()),
            "batches": len(self._batches),
        }

    def __iter__(self) -> Iterator[Report]:
        return iter(self._reports.values())


def describe_failures(store: ReportStore, reports: Iterable[Report]) -> list[str]:
    """Human-readable lines for a maker prompt: what failed in real use.

    Only the receipt kind/ref/summary and the reporter's feedback are exposed,
    never the eval answer key (reports are not eval cases).
    """
    lines: list[str] = []
    for report in reports:
        refs = []
        for rid in report.receipt_ids:
            receipt = store.get_receipt(rid)
            label = f"{receipt.kind}:{receipt.ref}"
            if receipt.summary:
                label += f" ({receipt.summary})"
            refs.append(label)
        score = "unscored" if report.score is None else f"score {report.score:.2f}"
        feedback = report.feedback or "(no feedback text)"
        lines.append(f"{', '.join(refs)} — {score} — {feedback}")
    return lines
