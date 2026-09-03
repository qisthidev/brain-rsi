"""Best-first tree search over candidates with staged goals and bounded debugging.

Adapted from AI-Scientist-v2 (``parallel_agent._select_parallel_nodes`` and
``agent_manager``) for a Markdown agent surface, with the gates kept
deterministic (see wiki/research/ai-scientist-v2-untuk-brain-v2.md §2–§4):

* **draft** – a fresh candidate from the baseline (up to ``num_drafts`` roots);
* **debug** – with probability ``debug_prob`` retry a buggy leaf (regression /
  critical / budget) at most ``max_debug_depth`` times, feeding the scorer's
  findings back to the maker;
* **improve** – otherwise build on the deterministic best good node;
* stages ``working → tuning → explore → ablation`` with hard ``max_iters`` and a
  no-improvement ``patience``; stage completion is computed from scores only;
* **ablation** switches each hunk of the best node off individually and keeps
  only hunks that carry score, producing a minimal diff for human review.

Everything runs under the existing step/second budgets plus a search-wide
evaluation cap and wall clock. Promotion never happens here: the result is a
journal and a best node for ``cycle.make_search_decision``.
"""
from __future__ import annotations

import abc
import random
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .benchmark import run_suite
from .candidate import CandidateRunner, FixtureCandidate, FixtureConfig
from .diffs import Files, ablate, all_hunks, change_size, changed_paths, merge_files, unified_diff
from .journal import CandidateNode, Journal, new_node_id
from .policy import aggregate, score_case
from .types import DEFAULT_BUDGET_SECONDS, DEFAULT_BUDGET_STEPS, EvalCase, ScoreResult
from .validator import ValidationPolicy, validate_changes
from .verdict import compare

STAGE_KINDS = ("working", "tuning", "explore", "ablation")


@dataclass(frozen=True)
class StageConfig:
    name: str
    kind: str  # one of STAGE_KINDS
    max_iters: int
    patience: int = 3  # consecutive evaluations without a new best before the stage stops

    def __post_init__(self) -> None:
        if self.kind not in STAGE_KINDS:
            raise ValueError(f"unknown stage kind {self.kind!r}")
        if self.max_iters < 0 or self.patience < 1:
            raise ValueError("max_iters must be >= 0 and patience >= 1")


DEFAULT_STAGES: tuple[StageConfig, ...] = (
    StageConfig("s1_working", "working", max_iters=4),
    StageConfig("s2_tuning", "tuning", max_iters=6),
    StageConfig("s3_explore", "explore", max_iters=6),
    StageConfig("s4_ablation", "ablation", max_iters=8),
)


@dataclass(frozen=True)
class SearchConfig:
    num_drafts: int = 2
    debug_prob: float = 0.5
    max_debug_depth: int = 2
    stages: tuple[StageConfig, ...] = DEFAULT_STAGES
    seed: int = 0
    budget_steps: int = DEFAULT_BUDGET_STEPS  # per suite evaluation (as in benchmark)
    budget_seconds: float = DEFAULT_BUDGET_SECONDS
    max_evaluations: int = 40  # search-wide hard cap on scored candidates
    wall_seconds: float = 900.0  # search-wide wall clock
    minimum_delta: float = 0.01

    def __post_init__(self) -> None:
        if self.num_drafts < 1:
            raise ValueError("num_drafts must be >= 1")
        if not 0.0 <= self.debug_prob <= 1.0:
            raise ValueError("debug_prob must be in [0, 1]")
        if self.max_debug_depth < 0 or self.max_evaluations < 1 or self.wall_seconds <= 0:
            raise ValueError("invalid search budgets")


# ---------------------------------------------------------------------------
# Maker protocol
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProposalRequest:
    kind: str  # draft | debug | improve
    stage: StageConfig
    parent: CandidateNode
    parent_files: Files
    feedback: tuple[str, ...]  # scorer details of the parent (debug) – advisory
    journal: Journal
    reported_failures: tuple[str, ...] = ()  # failures reported from real use (triggers.py batch)


@dataclass(frozen=True)
class Proposal:
    candidate_id: str
    changes: Mapping[str, str | None]  # full-file contents; None = delete
    rationale: str = ""
    proposal: str | None = None  # name of the improvement proposal (ideation) it pursues, if any


class Maker(abc.ABC):
    """Produces candidate file changes and the runner that evaluates a file set.

    Live adapters (via ``ccx``) are a later, separately reviewed change; the
    fixture maker below keeps the whole loop testable offline.
    """

    @abc.abstractmethod
    def propose(self, request: ProposalRequest) -> Proposal | None:
        """Return a proposal or None when the maker has nothing for this request."""

    @abc.abstractmethod
    def runner(self, candidate_id: str, files: Files) -> CandidateRunner:
        """Runner whose answers are a function of the candidate's files."""

    def analyze(self, node: CandidateNode, files: Files, feedback: Sequence[str]) -> str:
        """Optional advisory analysis of a failed node (fed to the next debug step, never to the score)."""
        return ""


@dataclass(frozen=True)
class FixtureRule:
    file: str
    contains: str
    responses: dict[str, str]


@dataclass(frozen=True)
class FixtureProposal:
    id: str
    kind: str
    parent: str | None  # fixture id of the parent proposal; None = baseline
    files: dict[str, str | None]
    rationale: str = ""


class FixtureTreeMaker(Maker):
    """Offline maker: a scripted tree of proposals plus content rules.

    Answers are derived from the *files* (baseline answers overlaid by every rule
    whose ``contains`` text is present in the named file), so ablation removing a
    hunk really changes the score — exactly like a prompt edit would.
    """

    def __init__(
        self,
        *,
        base_responses: Mapping[str, str],
        rules: Sequence[FixtureRule],
        proposals: Sequence[FixtureProposal],
        base_files: Mapping[str, str] | None = None,
    ):
        self.base_responses = dict(base_responses)
        self.rules = list(rules)
        self.proposals = list(proposals)
        self.base_files: Files = dict(base_files or {})
        self._used: set[str] = set()
        self._fixture_id_by_node: dict[str, str | None] = {}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, base_responses: Mapping[str, str]) -> "FixtureTreeMaker":
        rules = [
            FixtureRule(str(item["file"]), str(item["contains"]), dict(item["responses"]))
            for item in data.get("rules", [])
        ]
        proposals = [
            FixtureProposal(
                id=str(item["id"]),
                kind=str(item["kind"]),
                parent=item.get("parent"),
                files=dict(item.get("files", {})),
                rationale=str(item.get("rationale", "")),
            )
            for item in data.get("proposals", [])
        ]
        return cls(
            base_responses=base_responses,
            rules=rules,
            proposals=proposals,
            base_files=data.get("base_files", {}),
        )

    def bind(self, node: CandidateNode, fixture_id: str | None) -> None:
        self._fixture_id_by_node[node.id] = fixture_id

    def propose(self, request: ProposalRequest) -> Proposal | None:
        parent_fixture = self._fixture_id_by_node.get(request.parent.id)
        if request.parent.kind == "baseline":
            parent_fixture = None
        for item in self.proposals:
            if item.id in self._used or item.kind != request.kind or item.parent != parent_fixture:
                continue
            self._used.add(item.id)
            return Proposal(candidate_id=item.id, changes=item.files, rationale=item.rationale)
        return None

    def runner(self, candidate_id: str, files: Files) -> CandidateRunner:
        responses = dict(self.base_responses)
        for rule in self.rules:
            if rule.contains in files.get(rule.file, ""):
                responses.update(rule.responses)
        return FixtureCandidate(FixtureConfig(candidate_id=candidate_id, responses=responses))


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@dataclass
class SearchResult:
    journal: Journal
    root: CandidateNode
    best: CandidateNode  # deterministic best good node (may be the root)
    minimal: CandidateNode | None  # ablation-minimised version of best, if produced
    files_by_node: dict[str, Files]
    base_files: Files
    stages: list[dict[str, Any]] = field(default_factory=list)
    ablation: list[dict[str, Any]] = field(default_factory=list)
    evaluations: int = 0
    elapsed_s: float = 0.0
    stopped_reason: str = ""

    @property
    def recommended(self) -> CandidateNode:
        """Minimal node when it keeps the best score, otherwise the best node."""
        if self.minimal is not None and self.minimal.total >= self.best.total and self.minimal.is_good:
            return self.minimal
        return self.best

    def accepted_for_review(self, minimum_delta: float = 0.01) -> bool:
        node = self.recommended
        return (
            node.is_good
            and node.id != self.root.id
            and node.total >= self.root.total + minimum_delta
            and bool(node.meta.get("verdict", {}).get("gate_ok", False))
        )

    def rejection_reason(self, minimum_delta: float = 0.01) -> str:
        """Why the recommended node does not qualify (empty when accepted) — feeds the commit log."""
        node = self.recommended
        if node.id == self.root.id:
            return self.stopped_reason or "best is the baseline (no proposal beat it)"
        if node.is_violation:
            return "policy violation: " + "; ".join(node.policy_violations)
        if node.runner_errors:
            return f"runner errors: {', '.join(node.runner_errors)}"
        if node.critical_regressions:
            return f"critical regressions: {', '.join(node.critical_regressions)}"
        if node.regressions:
            return f"regressions: {', '.join(node.regressions)}"
        verdict = node.meta.get("verdict") or {}
        if not verdict.get("gate_ok", False):
            return str(verdict.get("gate_reason") or "verdict gate failed")
        if node.total < self.root.total + minimum_delta:
            return f"delta {node.total - self.root.total:+.2f} below minimum {minimum_delta}"
        return ""

    def recommended_diff(self) -> str:
        return unified_diff(self.base_files, self.files_by_node[self.recommended.id])


class SearchBudgetExceeded(Exception):
    pass


class _Clock:
    def __init__(self, wall_seconds: float, max_evaluations: int):
        self.started = time.monotonic()
        self.wall_seconds = wall_seconds
        self.max_evaluations = max_evaluations
        self.evaluations = 0

    def elapsed(self) -> float:
        return time.monotonic() - self.started

    def check(self) -> str | None:
        if self.evaluations >= self.max_evaluations:
            return f"max_evaluations reached ({self.max_evaluations})"
        if self.elapsed() >= self.wall_seconds:
            return f"wall_seconds reached ({self.wall_seconds})"
        return None


def _evaluate(
    cases: Sequence[EvalCase],
    baseline_scores: Sequence[ScoreResult],
    runner: CandidateRunner,
    config: SearchConfig,
) -> tuple[list[ScoreResult], float, float, list[str], list[str], list[str], list[str]]:
    outputs = run_suite(cases, runner, config.budget_steps, config.budget_seconds)
    scores = [score_case(case, output) for case, output in zip(cases, outputs)]
    total, max_points = aggregate(scores)
    baseline_by_case = {score.case_id: score for score in baseline_scores}
    regressions = [s.case_id for s in scores if baseline_by_case[s.case_id].passed and not s.passed]
    critical = [s.case_id for s in scores if s.critical and baseline_by_case[s.case_id].passed and not s.passed]
    runner_errors = [f"{runner.candidate_id}:{s.case_id}" for s in scores if s.runner_error]
    budget = [
        item
        for item, score in zip(runner_errors, (s for s in scores if s.runner_error))
        if "budget" in (score.runner_error or "").casefold()
    ]
    return scores, total, max_points, regressions, critical, budget, runner_errors


def run_search(
    cases: Sequence[EvalCase],
    baseline: CandidateRunner,
    maker: Maker,
    base_files: Mapping[str, str],
    config: SearchConfig | None = None,
    *,
    validation: ValidationPolicy | None = None,
    journal: Journal | None = None,
    run_id: str | None = None,
    reported_failures: Sequence[str] = (),
) -> SearchResult:
    config = config or SearchConfig()
    reported = tuple(reported_failures)
    validation = validation or ValidationPolicy()
    rng = random.Random(config.seed)
    clock = _Clock(config.wall_seconds, config.max_evaluations)
    base: Files = dict(base_files)
    if journal is None:  # note: an empty Journal is falsy (it defines __len__)
        journal = Journal(run_id or f"search-{int(time.time())}")
    files_by_node: dict[str, Files] = {}

    # --- root: baseline scored once, reused as the reference for every node ---
    baseline_outputs = run_suite(cases, baseline, config.budget_steps, config.budget_seconds)
    baseline_scores = [score_case(case, output) for case, output in zip(cases, baseline_outputs)]
    baseline_total, max_points = aggregate(baseline_scores)
    baseline_runner_errors = [f"{baseline.candidate_id}:{s.case_id}" for s in baseline_scores if s.runner_error]
    baseline_budget = [
        item
        for item, score in zip(baseline_runner_errors, (s for s in baseline_scores if s.runner_error))
        if "budget" in (score.runner_error or "").casefold()
    ]
    clock.evaluations += 1
    root = journal.append(
        CandidateNode(
            id=new_node_id(),
            kind="baseline",
            stage="root",
            candidate_id=baseline.candidate_id,
            parent_id=None,
            total=baseline_total,
            max_points=max_points,
            passed_cases=tuple(s.case_id for s in baseline_scores if s.passed),
            budget_violations=tuple(baseline_budget),
            runner_errors=tuple(baseline_runner_errors),
            scores=baseline_scores,
        )
    )
    files_by_node[root.id] = dict(base)
    if isinstance(maker, FixtureTreeMaker):
        maker.bind(root, None)
    if reported:
        journal.event("reported_failures", count=len(reported))

    result = SearchResult(journal=journal, root=root, best=root, minimal=None, files_by_node=files_by_node, base_files=base)
    if baseline_runner_errors:
        stopped = f"baseline evaluation failed ({len(baseline_runner_errors)} runner error(s))"
        result.evaluations = clock.evaluations
        result.elapsed_s = clock.elapsed()
        result.stopped_reason = stopped
        journal.event("search_stop", reason=stopped)
        journal.event("search_end", best=root.id, recommended=root.id, evaluations=clock.evaluations)
        return result

    def evaluate_files(
        *,
        kind: str,
        stage: StageConfig,
        parent: CandidateNode,
        candidate_id: str,
        new_files: Files,
        rationale: str = "",
        extra_meta: Mapping[str, Any] | None = None,
        policy: ValidationPolicy | None = None,
    ) -> CandidateNode:
        stop = clock.check()
        if stop:
            raise SearchBudgetExceeded(stop)
        paths = changed_paths(base, new_files)
        size = change_size(base, new_files)
        meta: dict[str, Any] = {"rationale": rationale, **(extra_meta or {})}
        check = validate_changes(base, new_files, policy or validation)
        depth = parent.debug_depth + 1 if kind == "debug" else 0
        if not check.ok:
            node = CandidateNode(
                id=new_node_id(),
                kind=kind,
                stage=stage.name,
                candidate_id=candidate_id,
                parent_id=parent.id,
                total=0.0,
                max_points=max_points,
                changed_files=tuple(paths),
                change_size=size,
                policy_violations=tuple(check.violations),
                debug_depth=depth,
                meta=meta,
            )
        else:
            runner = maker.runner(candidate_id, new_files)
            scores, total, _max, regressions, critical, budget, runner_errors = _evaluate(
                cases, baseline_scores, runner, config
            )
            clock.evaluations += 1
            node = CandidateNode(
                id=new_node_id(),
                kind=kind,
                stage=stage.name,
                candidate_id=candidate_id,
                parent_id=parent.id,
                total=total,
                max_points=max_points,
                changed_files=tuple(paths),
                change_size=size,
                passed_cases=tuple(s.case_id for s in scores if s.passed),
                regressions=tuple(regressions),
                critical_regressions=tuple(critical),
                budget_violations=tuple(budget),
                runner_errors=tuple(runner_errors),
                debug_depth=depth,
                scores=scores,
                meta={**meta, "verdict": compare(baseline_scores, scores).to_dict()},
            )
        journal.append(node)
        files_by_node[node.id] = dict(new_files)
        if node.is_buggy and kind != "ablate":
            analysis = maker.analyze(node, new_files, feedback_for(node))
            if analysis:
                node.analysis = analysis  # in-memory only; the JSONL line stays as written
                journal.event("analysis", node=node.id, text=analysis)
        return node

    case_by_id = {case.id: case for case in cases}

    def feedback_for(node: CandidateNode) -> tuple[str, ...]:
        """Sanitised scorer feedback: what went wrong, never the expected phrases (answer key)."""
        lines: list[str] = []
        if node.policy_violations:
            lines.extend(f"policy: {v}" for v in node.policy_violations)
        for score in node.scores:
            if score.passed:
                continue
            case = case_by_id.get(score.case_id)
            label = f"{score.case_id} [{case.category}]" if case else score.case_id
            if score.runner_error:
                lines.append(f"{label}: no usable answer ({score.runner_error})")
                continue
            forbidden = [d.split(":", 1)[1].strip() for d in score.details if d.startswith("forbidden present:")]
            missing = sum(1 for d in score.details if d.startswith("expected missing:"))
            expected_total = missing + sum(1 for d in score.details if d.startswith("expected present:"))
            parts = []
            if forbidden:
                parts.append("the answer contained forbidden behaviour " + ", ".join(forbidden))
            if missing:
                parts.append(f"{missing} of {expected_total} required behaviours were absent")
            lines.append(f"{label}: FAILED — " + "; ".join(parts or ["did not meet the case"]))
        if node.analysis:
            lines.append(f"analysis: {node.analysis}")
        return tuple(lines)

    def pick_parent_and_kind(stage: StageConfig, best: CandidateNode) -> tuple[str, CandidateNode] | None:
        """AI-Scientist-v2 ordering: draft → (debug with prob) → improve best."""
        if stage.kind in ("working", "explore") and len(journal.draft_nodes) < config.num_drafts:
            return "draft", root
        if stage.kind != "tuning" and rng.random() < config.debug_prob:
            debuggable = journal.debuggable_nodes(config.max_debug_depth)
            if debuggable:
                return "debug", rng.choice(debuggable)
        if stage.kind == "tuning":
            return "improve", best
        if best.id == root.id:
            # nothing good beyond the baseline yet: keep drafting (bounded by max_iters)
            return "draft", root
        return "improve", best

    stopped = ""
    try:
        for stage in config.stages:
            stage_record: dict[str, Any] = {"name": stage.name, "kind": stage.kind, "iters": 0, "completed": ""}
            journal.event("stage_start", stage=stage.name, kind=stage.kind, best=result.best.id)
            if stage.kind == "ablation":
                stage_record["completed"] = _run_ablation(
                    stage, result, cases, base, evaluate_files, config, validation
                )
                journal.event("stage_end", stage=stage.name, reason=stage_record["completed"])
                result.stages.append(stage_record)
                continue

            stale = 0
            misses = 0
            while stage_record["iters"] < stage.max_iters:
                best_before = result.best
                choice = pick_parent_and_kind(stage, best_before)
                if choice is None:
                    stage_record["completed"] = "nothing to expand"
                    break
                kind, parent = choice
                parent_files = files_by_node[parent.id]
                request = ProposalRequest(
                    kind=kind,
                    stage=stage,
                    parent=parent,
                    parent_files=parent_files,
                    feedback=feedback_for(parent),
                    journal=journal,
                    reported_failures=reported,
                )
                proposal = maker.propose(request)
                if proposal is None:
                    misses += 1
                    if misses >= 3:
                        stage_record["completed"] = f"maker had no proposal for {kind}"
                        break
                    continue
                misses = 0
                new_files = merge_files(parent_files, proposal.changes)
                policy = validation
                if stage.kind == "tuning":
                    policy = ValidationPolicy(
                        allowlist=validation.allowlist,
                        denylist=validation.denylist,
                        max_file_bytes=validation.max_file_bytes,
                        max_total_bytes=validation.max_total_bytes,
                        restrict_to_paths=tuple(parent.changed_files) or None,
                        forbid_host_paths=validation.forbid_host_paths,
                    )
                node = evaluate_files(
                    kind=kind,
                    stage=stage,
                    parent=parent,
                    candidate_id=proposal.candidate_id,
                    new_files=new_files,
                    rationale=proposal.rationale,
                    policy=policy,
                    extra_meta={"proposal": proposal.proposal} if proposal.proposal else None,
                )
                if isinstance(maker, FixtureTreeMaker):
                    maker.bind(node, proposal.candidate_id)
                stage_record["iters"] += 1
                result.best = journal.best_node() or root
                if result.best.id != best_before.id:
                    stale = 0
                else:
                    stale += 1
                if stage.kind == "working" and _has_working(journal, root):
                    stage_record["completed"] = "found working candidate"
                    break
                if stage.kind in ("tuning", "explore") and stale >= stage.patience:
                    stage_record["completed"] = f"no new best for {stage.patience} evaluations"
                    break
            if not stage_record["completed"]:
                stage_record["completed"] = "max_iters reached"
            journal.event("stage_end", stage=stage.name, reason=stage_record["completed"], best=result.best.id)
            result.stages.append(stage_record)
            if stage.kind == "working" and not _has_working(journal, root):
                stopped = "no working candidate found in the working stage"
                journal.event("search_stop", reason=stopped)
                break
    except SearchBudgetExceeded as exc:
        stopped = str(exc)
        journal.event("search_stop", reason=stopped)

    result.best = journal.best_node(among=[n for n in journal.good_nodes if n.kind != "ablate"]) or root
    result.evaluations = clock.evaluations
    result.elapsed_s = clock.elapsed()
    result.stopped_reason = stopped
    journal.event("search_end", best=result.best.id, recommended=result.recommended.id, evaluations=clock.evaluations)
    return result


def _has_working(journal: Journal, root: CandidateNode) -> bool:
    """Working stage goal: at least one good candidate (no regression, no critical, no budget hit)."""
    return any(node.is_good and node.id != root.id for node in journal.nodes)


def _run_ablation(
    stage: StageConfig,
    result: SearchResult,
    cases: Sequence[EvalCase],
    base: Files,
    evaluate_files,
    config: SearchConfig,
    validation: ValidationPolicy,
) -> str:
    """Switch each hunk of the best node off; keep only hunks that carry score."""
    best = result.best
    if best.id == result.root.id:
        return "skipped: best is the baseline"
    best_files = result.files_by_node[best.id]
    hunks = all_hunks(base, best_files)
    if len(hunks) <= 1:
        return "skipped: best has a single hunk"
    inert: list[str] = []
    iters = 0
    for hunk in hunks:
        if iters >= stage.max_iters:
            result.ablation.append({"hunk": hunk.key, "verdict": "not tested (max_iters)"})
            continue
        variant = ablate(base, best_files, {hunk.key})
        if changed_paths(base, variant) == []:
            result.ablation.append({"hunk": hunk.key, "verdict": "skipped (reverts everything)"})
            continue
        node = evaluate_files(
            kind="ablate",
            stage=stage,
            parent=best,
            candidate_id=f"{best.candidate_id}~{hunk.key}",
            new_files=variant,
            rationale=f"ablation: without hunk {hunk.key}",
            extra_meta={"ablation_without": [hunk.key]},
        )
        iters += 1
        carries = node.is_violation or node.is_buggy or node.total < best.total
        verdict = "carries score" if carries else "inert"
        if not carries:
            inert.append(hunk.key)
        result.ablation.append(
            {
                "hunk": hunk.key,
                "tag": hunk.tag,
                "preview": hunk.preview(),
                "total_without": node.total,
                "verdict": verdict,
                "node": node.id,
            }
        )
    if not inert:
        return f"{iters} hunks tested; every hunk carries score"
    if iters >= stage.max_iters:
        return f"{iters} hunks tested; {len(inert)} inert but no budget left to confirm the minimal diff"
    minimal_files = ablate(base, best_files, set(inert))
    minimal = evaluate_files(
        kind="ablate",
        stage=stage,
        parent=best,
        candidate_id=f"{best.candidate_id}~minimal",
        new_files=minimal_files,
        rationale=str(best.meta.get("rationale", "")),  # the author's reason survives minimisation
        extra_meta={
            "ablation_without": list(inert),
            "minimal": True,
            "ablation_note": f"minimal diff of {best.candidate_id} without {len(inert)} inert hunk(s)",
        },
    )
    if minimal.is_good and minimal.total >= best.total:
        result.minimal = minimal
        return f"{iters + 1} evaluations; minimal diff drops {len(inert)} inert hunk(s)"
    result.ablation.append({"hunk": "*combined*", "verdict": "inert hunks interact; minimal diff rejected", "node": minimal.id})
    return f"{iters + 1} evaluations; inert hunks interact, keeping the full best diff"
