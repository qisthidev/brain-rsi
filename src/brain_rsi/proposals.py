"""Improvement proposals (ideation) with a novelty check against history.

AI-Scientist-v2 generates ideas, checks novelty against the literature, then
runs the tree search on each. Our analogue:

* a proposal is a small JSON file under ``proposals/`` (outside the mutable
  surface) — ``name``, ``hypothesis``, ``target_cases``, ``surface_files``,
  ``risk``, ``status``;
* "literature" = this repository's own memory: earlier proposals, decision
  artifacts under ``patches/`` (what was tried and rejected), and the distilled
  lessons under ``wiki/lessons/``;
* ``check_novelty`` is deterministic and advisory except for exact/near duplicates
  of an open or rejected proposal, which are reported as blocking so the loop
  does not re-run what already failed (lesson P8: a candidate that repeats a
  failure with an existing lesson is a regression).

Open proposals can seed the live maker: draft *k* pursues hypothesis *k*.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

STATUSES = ("open", "tried", "accepted", "rejected", "withdrawn")
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_./-]{2,}")
_STOP = {
    "the", "and", "for", "that", "this", "with", "into", "from", "agent", "rule", "rules", "add", "make",
    "should", "when", "then", "not", "never", "always", "yang", "dan", "untuk", "dengan", "agar", "tidak",
    "atau", "pada", "dari", "ini", "itu", "jangan", "harus",
}


class ProposalError(Exception):
    pass


@dataclass
class ImprovementProposal:
    name: str
    hypothesis: str
    target_cases: list[str] = field(default_factory=list)
    surface_files: list[str] = field(default_factory=list)
    risk: str = ""
    status: str = "open"
    created: str = ""
    notes: list[str] = field(default_factory=list)
    source_path: str = ""

    def __post_init__(self) -> None:
        if not self.name or not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", self.name):
            raise ProposalError(f"proposal name must be a kebab-case slug: {self.name!r}")
        if not self.hypothesis.strip():
            raise ProposalError(f"proposal {self.name}: hypothesis is required")
        if self.status not in STATUSES:
            raise ProposalError(f"proposal {self.name}: unknown status {self.status!r}")
        if not self.created:
            self.created = datetime.now(timezone.utc).date().isoformat()

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("source_path", None)
        return data


def tokens(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.casefold()) if w not in _STOP}


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def load_proposal(path: Path) -> ImprovementProposal:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ProposalError(f"{path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ProposalError(f"{path}: proposal must be a JSON object")
    allowed = {"name", "hypothesis", "target_cases", "surface_files", "risk", "status", "created", "notes"}
    unknown = set(data) - allowed
    if unknown:
        raise ProposalError(f"{path}: unknown keys {sorted(unknown)}")
    proposal = ImprovementProposal(**data)
    proposal.source_path = str(path)
    return proposal


def load_proposals(directory: Path) -> list[ImprovementProposal]:
    directory = Path(directory)
    if not directory.is_dir():
        return []
    proposals = [load_proposal(p) for p in sorted(directory.glob("*.json"))]
    names = [p.name for p in proposals]
    if len(names) != len(set(names)):
        raise ProposalError("proposal names must be unique")
    return proposals


def write_proposal(proposal: ImprovementProposal, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{proposal.name}.json"
    if path.exists():
        raise ProposalError(f"proposal already exists: {path}")
    path.write_text(json.dumps(proposal.to_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Novelty check
# ---------------------------------------------------------------------------


@dataclass
class NoveltyReport:
    name: str
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    related_lessons: list[str] = field(default_factory=list)
    prior_decisions: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.blocking

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "ok": self.ok}


def _decision_records(patches_dir: Path) -> list[dict[str, Any]]:
    records = []
    if not patches_dir.is_dir():
        return records
    for path in sorted(patches_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(data, dict):
            data["_path"] = path.name
            records.append(data)
    return records


def _lesson_lines(lessons_dir: Path) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    if not lessons_dir.is_dir():
        return lines
    for path in sorted(lessons_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            # headings, table rows and bullets carry the distilled rules
            if stripped.startswith(("#", "|", "-", "*")) and len(stripped) > 12:
                lines.append((path.name, stripped.lstrip("#|-* ").strip()))
    return lines


def check_novelty(
    proposal: ImprovementProposal,
    others: Sequence[ImprovementProposal],
    *,
    patches_dir: Path | None = None,
    lessons_dir: Path | None = None,
    duplicate_threshold: float = 0.6,
    related_threshold: float = 0.25,
    max_related: int = 5,
) -> NoveltyReport:
    report = NoveltyReport(name=proposal.name)
    mine = tokens(proposal.hypothesis) | tokens(" ".join(proposal.target_cases))

    for other in others:
        if other.name == proposal.name and other.source_path == proposal.source_path:
            continue
        if other.name == proposal.name:
            report.blocking.append(f"name already used by {other.source_path or 'another proposal'}")
            continue
        score = jaccard(mine, tokens(other.hypothesis) | tokens(" ".join(other.target_cases)))
        if score >= duplicate_threshold:
            where = f"{other.name} ({other.status})"
            if other.status in ("rejected", "withdrawn", "tried"):
                report.blocking.append(f"near-duplicate of {where}, similarity {score:.2f}: do not re-run a failed idea without a new angle")
            elif other.status == "accepted":
                report.warnings.append(f"near-duplicate of {where}, similarity {score:.2f}: already landed")
            else:
                report.blocking.append(f"near-duplicate of open proposal {where}, similarity {score:.2f}")
        elif score >= related_threshold:
            report.warnings.append(f"overlaps {other.name} ({other.status}), similarity {score:.2f}")

    if patches_dir is not None:
        for record in _decision_records(patches_dir):
            search = record.get("search") or {}
            node = search.get("recommended_node") or {}
            rationale = str(node.get("rationale", "") or "")
            files = set(node.get("changed_files") or [])
            score = jaccard(mine, tokens(rationale))
            same_surface = bool(files & set(proposal.surface_files))
            if score >= related_threshold or (same_surface and score >= related_threshold / 2):
                verdict = "accepted for review" if record.get("accepted_for_review") else "rejected"
                line = f"{record['_path']}: {verdict}, similarity {score:.2f}" + (", same files" if same_surface else "")
                report.prior_decisions.append(line)
                if not record.get("accepted_for_review") and score >= duplicate_threshold:
                    report.blocking.append(f"a near-identical attempt was already rejected ({record['_path']})")

    if lessons_dir is not None:
        scored = []
        for file_name, line in _lesson_lines(lessons_dir):
            score = jaccard(mine, tokens(line))
            if score >= related_threshold / 2:
                scored.append((score, f"{file_name}: {line[:140]}"))
        scored.sort(reverse=True)
        report.related_lessons = [text for _, text in scored[:max_related]]
    return report
