"""Command-line interface for offline benchmark and guarded cycle runs."""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path

from .benchmark import BenchmarkReport, run_benchmark
from .candidate import FixtureCandidate, FixtureConfig
from .commits import CommitLog
from .cycle import CycleDecision, make_decision, make_search_decision, write_decision
from .gain import ControlLog, GainCriterion, GainError, evaluate_gain, suite_digest
from .ingest import SNAPSHOT_DIR, IngestError, ingest_source, load_manifest
from .journal import Journal
from .live import CcxClient, CcxError, CcxMaker
from .proposals import ImprovementProposal, ProposalError, check_novelty, load_proposals, write_proposal
from .reports import ReportError, ReportStore
from .review import review_candidate
from .triggers import FailureWindow, next_batch, status as trigger_status
from .versions import VersionError, VersionLedger
from .loader import load_eval_cases, select_cases
from .search import DEFAULT_STAGES, FixtureTreeMaker, SearchConfig, StageConfig, run_search
from .treeviz import render_tree_html
from .sandbox import candidate_workspace
from .sources import RegistryError, find_source, load_registry
from .validator import ValidationPolicy
from .types import DEFAULT_BUDGET_SECONDS, DEFAULT_BUDGET_STEPS, TARGET_MUTABLE_ALLOWLIST, TARGET_MUTABLE_DENYLIST

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BRAIN = PROJECT_ROOT.parent / "brain"
DEFAULT_REGISTRY = PROJECT_ROOT / "sources" / "registry.json"
DEFAULT_INGEST_ROOT = PROJECT_ROOT / "ingest"
DEFAULT_PROPOSALS = PROJECT_ROOT / "proposals"
DEFAULT_REPORTS = PROJECT_ROOT / "traces" / "reports.jsonl"
DEFAULT_COMMITS = PROJECT_ROOT / "traces" / "commits.jsonl"
DEFAULT_CONTROL = PROJECT_ROOT / "traces" / "control.jsonl"
DEFAULT_VERSIONS = PROJECT_ROOT / "patches" / "versions.jsonl"
DEFAULT_CRITERION = PROJECT_ROOT / "eval" / "gain_criterion.json"


def _make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brain-rsi")
    subcommands = parser.add_subparsers(dest="command", required=True)

    def add_shared(command: argparse.ArgumentParser) -> None:
        command.add_argument("--cases", type=Path, default=PROJECT_ROOT / "eval" / "cases.json")
        command.add_argument("--traces", type=Path, default=PROJECT_ROOT / "traces" / "runs.jsonl")
        command.add_argument("--baseline", default="baseline")
        command.add_argument("--candidate", default="candidate")
        command.add_argument("--budget-steps", type=int, default=DEFAULT_BUDGET_STEPS)
        command.add_argument("--budget-seconds", type=float, default=DEFAULT_BUDGET_SECONDS)
        command.add_argument(
            "--case-source",
            default=None,
            help="Run global cases plus cases grounded in this source id (default: every case).",
        )
        command.add_argument("--commit-log", type=Path, default=DEFAULT_COMMITS, help="Append-only outcome log.")
        command.add_argument("--control-log", type=Path, default=DEFAULT_CONTROL)
        command.add_argument("--gain-criterion", type=Path, default=DEFAULT_CRITERION)

    benchmark = subcommands.add_parser("benchmark", help="Compare fixture baseline and candidate.")
    add_shared(benchmark)

    cycle = subcommands.add_parser("cycle", help="Evaluate a candidate; never promote automatically.")
    add_shared(cycle)
    cycle.add_argument(
        "--write-decision",
        action="store_true",
        help="Write a review artifact under patches/; still does not modify brain.",
    )
    cycle.add_argument(
        "--snapshot-source",
        action="store_true",
        help="Demonstrate an ephemeral allowlisted snapshot of the source repository.",
    )
    cycle.add_argument("--source", type=Path, default=DEFAULT_BRAIN)
    cycle.add_argument(
        "--source-id",
        default=None,
        help="Registered source id; uses its path and allowlist for the snapshot (overrides --source).",
    )
    cycle.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    cycle.add_argument("--ingest-root", type=Path, default=DEFAULT_INGEST_ROOT)
    cycle.add_argument("--workspace-parent", type=Path, default=PROJECT_ROOT / "worktree")

    search = subcommands.add_parser(
        "search",
        help="Best-first tree search over scripted fixture candidates; journal + decision, never promotion.",
    )
    add_shared(search)
    search.add_argument("--tree", type=Path, default=PROJECT_ROOT / "fixtures" / "tree" / "demo.json")
    search.add_argument("--seed", type=int, default=0)
    search.add_argument("--num-drafts", type=int, default=2)
    search.add_argument("--debug-prob", type=float, default=0.5)
    search.add_argument("--max-debug-depth", type=int, default=2)
    search.add_argument(
        "--stage-iters",
        default=None,
        help="Override stage iteration caps, e.g. working=4,tuning=6,explore=6,ablation=8.",
    )
    search.add_argument("--max-evaluations", type=int, default=40)
    search.add_argument("--wall-seconds", type=float, default=900.0)
    search.add_argument("--journal-dir", type=Path, default=PROJECT_ROOT / "traces" / "journal")
    search.add_argument("--no-journal", action="store_true", help="Keep the journal in memory only.")
    search.add_argument("--write-decision", action="store_true", help="Write a review artifact under patches/.")
    search.add_argument(
        "--snapshot-source",
        action="store_true",
        help="Use the live target surface (agent/, .claude/skills/) as base files instead of the fixture's.",
    )
    search.add_argument("--source-id", default=None, help="Registered target id whose allowlist validates changes.")
    search.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    search.add_argument("--workspace-parent", type=Path, default=PROJECT_ROOT / "worktree")
    search.add_argument("--show-diff", action="store_true", help="Print the recommended unified diff.")
    search.add_argument(
        "--maker",
        choices=("fixture", "ccx"),
        default="fixture",
        help="fixture: scripted tree (offline). ccx: live models via the ccx CLI (requires --snapshot-source).",
    )
    search.add_argument("--maker-model", default=None, help="ccx model alias that proposes changes (non-gpt).")
    search.add_argument("--runner-model", default=None, help="ccx model alias that plays the agent under test.")
    search.add_argument(
        "--feedback-model", default=None, help="Optional ccx alias (different family than the maker) for advisory analysis."
    )
    search.add_argument("--ccx-timeout", type=float, default=120.0, help="Wall seconds per ccx call.")
    search.add_argument("--max-ccx-calls", type=int, default=61, help="Hard cap on ccx calls for the whole search.")
    search.add_argument("--max-ccx-tokens", type=int, default=1_000_000, help="Cap on cumulative ccx tokens (in+out).")
    search.add_argument(
        "--reviewer-models",
        default=None,
        help="Comma-separated ccx aliases (distinct families) that review the recommended diff; advisory only.",
    )
    search.add_argument("--meta-model", default=None, help="ccx alias (another family) that synthesises the reviews.")
    search.add_argument("--no-html", action="store_true", help="Do not write the HTML tree next to the journal.")
    search.add_argument(
        "--use-proposals",
        action="store_true",
        help="Seed successive drafts with the hypotheses of open proposals under --proposals-dir (after a novelty check).",
    )
    search.add_argument("--proposals-dir", type=Path, default=DEFAULT_PROPOSALS)
    search.add_argument(
        "--from-reports",
        action="store_true",
        help="Run only when the failure window is full (reports with score <= --max-score, at least --batch-size); "
        "the batch becomes the maker's reported-failure context and is marked consumed.",
    )
    search.add_argument("--reports", type=Path, default=DEFAULT_REPORTS)
    search.add_argument("--batch-size", type=int, default=FailureWindow.batch_size)
    search.add_argument("--max-score", type=float, default=FailureWindow.max_score)

    receipt = subcommands.add_parser("receipt", help="Issue a receipt for a real interaction (skill/session/case/task).")
    receipt.add_argument("kind", choices=("skill", "session", "case", "task"))
    receipt.add_argument("ref", help="Skill name, session name, case id or task id.")
    receipt.add_argument("--summary", default="", help="Short description (secret-scanned, no host paths).")
    receipt.add_argument("--reports", type=Path, default=DEFAULT_REPORTS)

    report = subcommands.add_parser("report", help="File a score/feedback report against one or more receipts.")
    report.add_argument("--receipt", action="append", required=True, help="Receipt id (repeatable).")
    report.add_argument("--score", type=float, default=None, help="0..1; omit for an unscored report (grading voided).")
    report.add_argument("--feedback", default="", help="What went wrong / right (secret-scanned).")
    report.add_argument("--source", choices=("human", "harness"), default="human")
    report.add_argument("--reports", type=Path, default=DEFAULT_REPORTS)

    reports = subcommands.add_parser("reports", help="Summarise receipts, reports and the failure window.")
    reports.add_argument("--reports", type=Path, default=DEFAULT_REPORTS)
    reports.add_argument("--batch-size", type=int, default=FailureWindow.batch_size)
    reports.add_argument("--max-score", type=float, default=FailureWindow.max_score)
    reports.add_argument("--json", action="store_true")

    commits = subcommands.add_parser("commits", help="Show the append-only outcome log (reviews, rejects, skips, publishes).")
    commits.add_argument("--commit-log", type=Path, default=DEFAULT_COMMITS)
    commits.add_argument("--last", type=int, default=20)
    commits.add_argument("--json", action="store_true")

    version = subcommands.add_parser("version", help="Version chain of the agent surface (recorded after human ACC only).")
    version.add_argument("action", choices=("check", "publish", "list"))
    version.add_argument("--root", type=Path, default=PROJECT_ROOT, help="Checkout whose surface is fingerprinted.")
    version.add_argument("--ledger", type=Path, default=DEFAULT_VERSIONS)
    version.add_argument("--decision", type=Path, default=None, help="Accepted decision artifact under patches/.")
    version.add_argument("--acc", default=None, help="Verbatim ACC line, e.g. 'ACC <owner> (review session, 14:05): apply diff X'.")
    version.add_argument("--note", default="")
    version.add_argument("--seed", action="store_true", help="Record the current surface as v1 without a decision.")
    version.add_argument("--commit-log", type=Path, default=DEFAULT_COMMITS)
    version.add_argument("--json", action="store_true")

    gain = subcommands.add_parser("gain", help="Preregistered gain criterion: control runs and improvement claims.")
    gain.add_argument("action", choices=("control", "claim", "show"))
    gain.add_argument("--runs", type=int, default=3, help="control: number of baseline-vs-baseline runs to append.")
    gain.add_argument("--decision", type=Path, default=None, help="claim: decision artifact to test.")
    gain.add_argument("--cases", type=Path, default=PROJECT_ROOT / "eval" / "cases.json")
    gain.add_argument("--case-source", default=None)
    gain.add_argument("--baseline", default="baseline")
    gain.add_argument("--budget-steps", type=int, default=DEFAULT_BUDGET_STEPS)
    gain.add_argument("--budget-seconds", type=float, default=DEFAULT_BUDGET_SECONDS)
    gain.add_argument("--control-log", type=Path, default=DEFAULT_CONTROL)
    gain.add_argument("--gain-criterion", type=Path, default=DEFAULT_CRITERION)
    gain.add_argument("--commit-log", type=Path, default=DEFAULT_COMMITS)
    gain.add_argument("--json", action="store_true")

    proposal = subcommands.add_parser("proposal", help="Ideation: list, create or novelty-check improvement proposals.")
    proposal.add_argument("action", choices=("list", "new", "check"))
    proposal.add_argument("name", nargs="?", default=None, help="Proposal name (new/check; check without name = all).")
    proposal.add_argument("--hypothesis", default=None)
    proposal.add_argument("--target-case", action="append", default=None)
    proposal.add_argument("--file", action="append", default=None, help="Surface file the proposal expects to touch.")
    proposal.add_argument("--risk", default="")
    proposal.add_argument("--proposals-dir", type=Path, default=DEFAULT_PROPOSALS)
    proposal.add_argument("--json", action="store_true")

    sources = subcommands.add_parser("sources", help="List registered second-brain sources.")
    sources.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)

    ingest = subcommands.add_parser(
        "ingest",
        help="Read-only ingest of allowlisted prompt/skill files from registered sources.",
    )
    ingest.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    ingest.add_argument("--ingest-root", type=Path, default=DEFAULT_INGEST_ROOT)
    ingest.add_argument("--source-id", action="append", default=None, help="Ingest only these ids (repeatable).")
    ingest.add_argument("--json", action="store_true", help="Print manifests as JSON.")
    return parser


def _load_fixture(candidate_id: str) -> FixtureCandidate:
    if Path(candidate_id).name != candidate_id:
        raise ValueError("fixture id must be a filename-safe identifier")
    path = PROJECT_ROOT / "fixtures" / f"{candidate_id}.json"
    if not path.is_file():
        raise FileNotFoundError(f"fixture not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in payload.items()):
        raise ValueError(f"fixture must be a string-to-string JSON object: {path}")
    return FixtureCandidate(FixtureConfig(candidate_id=candidate_id, responses=payload))


def _benchmark(args: argparse.Namespace) -> BenchmarkReport:
    return run_benchmark(
        select_cases(load_eval_cases(args.cases), args.case_source),
        _load_fixture(args.baseline),
        _load_fixture(args.candidate),
        budget_steps=args.budget_steps,
        budget_seconds=args.budget_seconds,
        traces_path=args.traces,
    )


def cmd_benchmark(args: argparse.Namespace) -> int:
    report = _benchmark(args)
    _print_report(report)
    return 0


def cmd_cycle(args: argparse.Namespace) -> int:
    source_path = args.source
    allowlist = TARGET_MUTABLE_ALLOWLIST
    denylist: tuple[str, ...] = TARGET_MUTABLE_DENYLIST
    source_id: str | None = None
    source_digest: str | None = None
    if args.source_id:
        spec = find_source(load_registry(args.registry, base_dir=PROJECT_ROOT), args.source_id)
        if not spec.is_target:
            raise RegistryError(
                f"source {spec.id!r} has role {spec.role!r}; only the role=target source may be an RSI candidate target"
            )
        source_id = spec.id
        allowlist = spec.allowlist
        denylist = spec.denylist
        ingested = args.ingest_root / spec.id / SNAPSHOT_DIR
        if ingested.is_dir():
            # Prefer the scrubbed, manifest-backed ingest snapshot over the live tree.
            source_path = ingested
            source_digest = str(load_manifest(args.ingest_root, spec.id).get("digest"))
            print(f"[cycle] source: {spec.id} ({spec.kind}) ingest snapshot digest {source_digest[:12]}")
        else:
            source_path = spec.resolved_path()
            print(f"[cycle] source: {spec.id} ({spec.kind}) live path {source_path} (not ingested yet)")
    workspace_context = (
        candidate_workspace(source_path, args.workspace_parent, allowlist=allowlist, denylist=denylist)
        if args.snapshot_source
        else nullcontext(None)
    )
    with workspace_context as workspace:
        if workspace is None:
            print("[cycle] DRY RUN: no source snapshot and no target mutation.")
        else:
            print(f"[cycle] ephemeral allowlisted snapshot: {workspace}")
        report = _benchmark(args)

    _print_report(report)
    suite = suite_digest(args.cases)
    decision = make_decision(
        report,
        source_id=source_id,
        source_digest=source_digest,
        suite_digest=suite,
        gain=_gain_for(args, baseline_id=report.baseline_id, suite=suite, candidate_total=report.candidate_total),
    )
    print(f"verdict: {report.verdict().short()} — {decision.verdict['gate_reason']}")
    print(f"decision: {'ACCEPT FOR HUMAN REVIEW' if decision.accepted_for_review else 'REJECT'}"
          + (f" ({decision.rejection_reason})" if decision.rejection_reason else ""))
    print("promotion: disabled; a human-reviewed patch or PR is required")
    path = None
    if args.write_decision:
        path = write_decision(decision, PROJECT_ROOT / "patches")
        print(f"decision artifact: {path}")
    _commit(args, decision, command="cycle", decision_path=path)
    return 0 if decision.accepted_for_review else 1


def _gain_for(args: argparse.Namespace, *, baseline_id: str, suite: str, candidate_total: float) -> dict | None:
    """Preregistered gain verdict when control runs exist for this baseline/suite; None otherwise."""
    criterion_path = getattr(args, "gain_criterion", DEFAULT_CRITERION)
    control_path = getattr(args, "control_log", DEFAULT_CONTROL)
    if not Path(criterion_path).is_file():
        return None
    controls = ControlLog(control_path).matching(baseline_id=baseline_id, suite=suite)
    if not controls:
        return None
    verdict = evaluate_gain(GainCriterion.load(Path(criterion_path)), controls, candidate_total)
    payload = verdict.to_dict()
    payload["criterion"] = str(criterion_path)
    return payload


def _commit(
    args: argparse.Namespace,
    decision: CycleDecision,
    *,
    command: str,
    decision_path: Path | None,
    skipped_reason: str | None = None,
) -> None:
    log_path = getattr(args, "commit_log", None)
    if log_path is None:
        return
    if skipped_reason is not None:
        outcome, reason = "skipped", skipped_reason
    elif decision.accepted_for_review:
        outcome, reason = "review", decision.verdict["gate_reason"] if decision.verdict else "accepted"
    else:
        outcome, reason = "rejected", decision.rejection_reason or "gate failed"
    rel = None
    if decision_path is not None:
        try:
            rel = str(decision_path.resolve().relative_to(PROJECT_ROOT))
        except ValueError:
            rel = str(decision_path)
    CommitLog(log_path).record(
        run_id=decision.run_id,
        command=command,
        outcome=outcome,
        reason=reason,
        baseline_id=decision.baseline_id,
        candidate_id=decision.candidate_id,
        baseline_total=decision.baseline_total,
        candidate_total=decision.candidate_total,
        verdict=decision.verdict,
        batch_id=decision.batch_id,
        decision_path=rel,
        meta={"suite_digest": decision.suite_digest, "gain": decision.gain},
    )
    print(f"commit log: {outcome} — {reason}")


def _parse_stage_iters(spec: str | None) -> tuple[StageConfig, ...]:
    if not spec:
        return DEFAULT_STAGES
    overrides: dict[str, int] = {}
    for item in spec.split(","):
        key, _, value = item.partition("=")
        if not value.strip().isdigit():
            raise ValueError(f"invalid --stage-iters entry {item!r}; expected kind=N")
        overrides[key.strip()] = int(value)
    unknown = set(overrides) - {stage.kind for stage in DEFAULT_STAGES}
    if unknown:
        raise ValueError(f"unknown stage kinds in --stage-iters: {sorted(unknown)}")
    return tuple(
        StageConfig(stage.name, stage.kind, overrides.get(stage.kind, stage.max_iters), stage.patience)
        for stage in DEFAULT_STAGES
    )


def _read_text_tree(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        data = path.read_bytes()
        if b"\x00" in data[:8192]:
            continue  # binary: not part of the text surface
        files[path.relative_to(root).as_posix()] = data.decode("utf-8", errors="replace")
    return files


def cmd_search(args: argparse.Namespace) -> int:
    cases = select_cases(load_eval_cases(args.cases), args.case_source)
    live = args.maker == "ccx"
    tree: dict = {}
    if live:
        if not args.snapshot_source:
            raise ValueError("--maker ccx requires --snapshot-source (base files must be the real agent surface)")
        if not args.maker_model or not args.runner_model:
            raise ValueError("--maker ccx requires --maker-model and --runner-model")
    else:
        tree_path = Path(args.tree)
        if not tree_path.is_file():
            raise FileNotFoundError(f"fixture tree not found: {tree_path}")
        tree = json.loads(tree_path.read_text(encoding="utf-8"))
        if not isinstance(tree, dict) or "proposals" not in tree:
            raise ValueError(f"fixture tree must be an object with 'proposals': {tree_path}")

    effective_budget_seconds = args.budget_seconds
    if live:
        minimum_calls = (2 * len(cases)) + 1  # baseline suite + one maker call + one candidate suite
        if args.max_ccx_calls < minimum_calls:
            raise ValueError(
                f"--max-ccx-calls {args.max_ccx_calls} cannot complete one comparison over {len(cases)} cases; "
                f"use at least {minimum_calls}"
            )
        live_floor = len(cases) * args.ccx_timeout + 30
        if effective_budget_seconds < live_floor:
            print(
                f"[search] budget_seconds raised {effective_budget_seconds:.0f} -> {live_floor:.0f} "
                "for a complete live suite"
            )
            effective_budget_seconds = live_floor

    allowlist = TARGET_MUTABLE_ALLOWLIST
    denylist: tuple[str, ...] = TARGET_MUTABLE_DENYLIST
    source_id: str | None = None
    source_path = PROJECT_ROOT
    if args.source_id:
        spec = find_source(load_registry(args.registry, base_dir=PROJECT_ROOT), args.source_id)
        if not spec.is_target:
            raise RegistryError(f"source {spec.id!r} has role {spec.role!r}; only the target may be searched")
        source_id, allowlist, denylist, source_path = spec.id, spec.allowlist, spec.denylist, spec.resolved_path()

    config = SearchConfig(
        num_drafts=args.num_drafts,
        debug_prob=args.debug_prob,
        max_debug_depth=args.max_debug_depth,
        stages=_parse_stage_iters(args.stage_iters),
        seed=args.seed,
        budget_steps=args.budget_steps,
        budget_seconds=effective_budget_seconds,
        max_evaluations=args.max_evaluations,
        wall_seconds=args.wall_seconds,
    )
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    suite = suite_digest(args.cases)
    store: ReportStore | None = None
    batch = None
    reported_failures: list[str] = []
    if args.from_reports:
        window = FailureWindow(max_score=args.max_score, batch_size=args.batch_size)
        store = ReportStore(args.reports)
        batch = next_batch(store, window)
        if batch is None:
            state = trigger_status(store, window)
            reason = (
                f"nothing batched: {state['pending_failures']} failing report(s) pending, "
                f"window needs {window.batch_size} (score <= {window.max_score:g})"
            )
            print(f"[search] {reason}; no proposal, no evaluation")
            if getattr(args, "commit_log", None):
                CommitLog(args.commit_log).record(run_id=run_id, command="search", outcome="skipped", reason=reason,
                                                  meta={"suite_digest": suite, "window": window.to_dict()})
                print(f"commit log: skipped — {reason}")
            return 0
        reported_failures = batch.failure_lines(store)
        print(f"[search] failure window full: {len(batch.reports)} report(s) batched as maker context")
        if not live:
            print("[search] note: the fixture maker ignores reported failures; they only steer --maker ccx")
    journal_path = None if args.no_journal else args.journal_dir / f"{run_id}.jsonl"
    journal = Journal(run_id, path=journal_path)

    workspace_context = (
        candidate_workspace(source_path, args.workspace_parent, allowlist=allowlist, denylist=denylist)
        if args.snapshot_source
        else nullcontext(None)
    )
    client: CcxClient | None = None
    models: dict[str, str] | None = None
    hypotheses: list[tuple[str, str]] = []
    proposals_used: list[dict] = []
    if args.use_proposals:
        all_proposals = load_proposals(args.proposals_dir)
        for item in all_proposals:
            if item.status != "open":
                continue
            novelty = check_novelty(
                item, all_proposals, patches_dir=PROJECT_ROOT / "patches", lessons_dir=PROJECT_ROOT / "wiki" / "lessons"
            )
            proposals_used.append({"name": item.name, "ok": novelty.ok, "blocking": novelty.blocking, "warnings": novelty.warnings})
            if novelty.ok:
                hypotheses.append((item.name, item.hypothesis))
                print(f"[proposals] {item.name}: open, novel" + (f" ({len(novelty.warnings)} warning(s))" if novelty.warnings else ""))
            else:
                print(f"[proposals] {item.name}: skipped — " + "; ".join(novelty.blocking))
        if not hypotheses:
            print("[proposals] no usable open proposals; drafts run without a hypothesis")
        if not live:
            print("[proposals] note: the fixture maker ignores hypotheses; they only steer --maker ccx")
    with workspace_context as workspace:
        if workspace is None:
            base_files = dict(tree.get("base_files", {}))
            print(f"[search] base files from fixture ({len(base_files)} files); no source snapshot.")
        else:
            base_files = _read_text_tree(workspace)
            print(f"[search] ephemeral allowlisted snapshot: {workspace} ({len(base_files)} text files)")
        if live:
            client = CcxClient(timeout_s=args.ccx_timeout, max_calls=args.max_ccx_calls, max_tokens=args.max_ccx_tokens)
            for model in filter(None, (args.maker_model, args.runner_model, args.feedback_model)):
                client.ensure_model(model)
            maker = CcxMaker(
                client,
                maker_model=args.maker_model,
                runner_model=args.runner_model,
                feedback_model=args.feedback_model,
                cases=cases,
                allowlist=allowlist,
                hypotheses=hypotheses,
            )
            baseline = maker.runner(args.baseline if args.baseline != "baseline" else "baseline-live", base_files)
            models = {"maker": args.maker_model, "runner": args.runner_model, "feedback": args.feedback_model or ""}
            print(f"[search] live maker={args.maker_model} runner={args.runner_model} feedback={args.feedback_model or '-'} "
                  f"(max {args.max_ccx_calls} ccx calls, {args.ccx_timeout:.0f}s each)")
        else:
            baseline = _load_fixture(args.baseline)
            maker = FixtureTreeMaker.from_dict(tree, base_responses=baseline._config.responses)
        result = run_search(
            cases,
            baseline,
            maker,
            base_files,
            config,
            validation=ValidationPolicy(allowlist=allowlist, denylist=denylist),
            journal=journal,
            reported_failures=reported_failures,
        )
    batch_id = None
    if store is not None and batch is not None:
        batch_id = store.record_batch(batch.report_ids, run_id=run_id, window=batch.window.to_dict()).batch_id
        print(f"[search] batch {batch_id} consumed by run {run_id}")

    print(f"run_id: {run_id}")
    print(f"journal: {journal_path or '(memory only)'}")
    root, best, rec = result.root, result.best, result.recommended
    print(f"baseline:    {root.candidate_id} = {root.total:.2f} / {root.max_points:.2f}")
    print(f"best:        {best.candidate_id} = {best.total:.2f} ({best.kind}, {best.change_size} changed lines)")
    print(f"recommended: {rec.candidate_id} = {rec.total:.2f} ({rec.kind}, {rec.change_size} changed lines)")
    print(f"evaluations: {result.evaluations} in {result.elapsed_s:.2f}s" + (f"; stopped: {result.stopped_reason}" if result.stopped_reason else ""))
    for stage in result.stages:
        print(f"  stage {stage['name']:<12} iters={stage['iters']:<2} {stage['completed']}")
    for node in result.journal:
        flag = "VIOLATION" if node.is_violation else "buggy" if node.is_buggy else "good"
        print(f"  {node.kind:<8} {node.stage:<12} {node.candidate_id:<40} {node.total:6.2f} {flag}")
    if result.ablation:
        print("ablation:")
        for row in result.ablation:
            print(f"  {row['hunk']:<28} {row.get('verdict','')}")
    if args.show_diff:
        print("recommended diff:")
        print(result.recommended_diff(), end="")
    artifact_journal = None
    if journal_path is not None:
        try:
            artifact_journal = journal_path.resolve().relative_to(PROJECT_ROOT)
        except ValueError:
            artifact_journal = journal_path
    reviews = None
    reviewer_models = [m.strip() for m in (args.reviewer_models or "").split(",") if m.strip()]
    if reviewer_models and result.recommended.id != result.root.id:
        if client is None:
            client = CcxClient(timeout_s=args.ccx_timeout, max_calls=args.max_ccx_calls, max_tokens=args.max_ccx_tokens)
        for model in reviewer_models + ([args.meta_model] if args.meta_model else []):
            client.ensure_model(model)
        node = result.recommended
        bundle = review_candidate(
            client,
            reviewer_models,
            diff=result.recommended_diff(),
            files=result.files_by_node[node.id],
            rationale=str(node.meta.get("rationale", "")),
            evidence={
                "baseline_total": result.root.total,
                "candidate_total": node.total,
                "max_points": node.max_points,
                "regressions": list(node.regressions) or "none",
                "runner_errors": list(node.runner_errors) or "none",
                "changed_files": list(node.changed_files),
                "change_size": node.change_size,
            },
            meta_model=args.meta_model,
        )
        reviews = bundle.to_dict()
        print("advisory reviews: " + ", ".join(f"{r.model}={r.verdict or 'n/a'}" for r in bundle.reviews)
              + (f"; meta={bundle.meta.get('verdict')}" if bundle.meta else ""))
    elif reviewer_models:
        print("advisory reviews: skipped (recommended node is the baseline)")
    usage = client.usage_summary() if client else None
    html_path = None
    if journal_path is not None and not args.no_html:
        html_path = journal_path.with_suffix(".html")
        html_path.write_text(
            render_tree_html(
                result.journal,
                best_id=result.best.id,
                recommended_id=result.recommended.id,
                stages=result.stages,
                ablation=result.ablation,
                diff=result.recommended_diff(),
                usage=usage,
                reviews=reviews,
            ),
            encoding="utf-8",
        )
        print(f"tree view: {html_path}")
    if usage:
        print(f"ccx usage: {usage['calls']} calls, {usage['input_tokens']} in / {usage['output_tokens']} out tokens, "
              f"${usage['cost_usd']:.4f}")
    artifact_html = None
    if html_path is not None:
        try:
            artifact_html = html_path.resolve().relative_to(PROJECT_ROOT)
        except ValueError:
            artifact_html = html_path
    decision = make_search_decision(
        result, source_id=source_id, journal_path=artifact_journal, usage=usage, models=models,
        reviews=reviews, html_path=artifact_html, proposals=proposals_used or None,
        suite_digest=suite, batch_id=batch_id,
        gain=_gain_for(args, baseline_id=result.root.candidate_id, suite=suite, candidate_total=result.recommended.total),
    )
    if decision.verdict:
        print(f"verdict: W{len(decision.verdict['wins'])} L{len(decision.verdict['losses'])} "
              f"T{decision.verdict['ties']} U{len(decision.verdict['unscored'])} — {decision.verdict['gate_reason']}")
    print(f"decision: {'ACCEPT FOR HUMAN REVIEW' if decision.accepted_for_review else 'REJECT'}"
          + (f" ({decision.rejection_reason})" if decision.rejection_reason else ""))
    print("promotion: disabled; a human-reviewed patch or PR is required")
    path = None
    if args.write_decision:
        path = write_decision(decision, PROJECT_ROOT / "patches")
        print(f"decision artifact: {path}")
    skipped = None
    if result.recommended.id == result.root.id:
        skipped = "no proposal beat the baseline: " + (result.stopped_reason or "best is the baseline")
    _commit(args, decision, command="search", decision_path=path, skipped_reason=skipped)
    return 0 if decision.accepted_for_review else 1


def cmd_receipt(args: argparse.Namespace) -> int:
    receipt = ReportStore(args.reports).issue_receipt(args.kind, args.ref, summary=args.summary)
    print(receipt.receipt_id)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    report = ReportStore(args.reports).report(args.receipt, score=args.score, feedback=args.feedback, source=args.source)
    score = "unscored" if report.score is None else f"{report.score:.2f}"
    print(f"{report.report_id}: {len(report.receipt_ids)} receipt(s), score {score}")
    return 0


def cmd_reports(args: argparse.Namespace) -> int:
    store = ReportStore(args.reports)
    window = FailureWindow(max_score=args.max_score, batch_size=args.batch_size)
    payload = {"store": store.summary(), "trigger": trigger_status(store, window)}
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    summary, trig = payload["store"], payload["trigger"]
    print(f"reports: {summary['reports']} ({summary['failing_reports']} failing, {summary['unscored_reports']} unscored), "
          f"receipts: {summary['receipts']} ({summary['unreported_receipts']} unreported), batches: {summary['batches']}")
    print(f"window: score <= {window.max_score:g}, batch {window.batch_size}; pending failures {trig['pending_failures']}; "
          + ("READY — `search --from-reports` will run" if trig["ready"] else f"needs {trig['missing']} more"))
    for receipt in store.unreported_receipts()[-10:]:
        print(f"  unreported {receipt.receipt_id} {receipt.kind}:{receipt.ref} {receipt.at[:19]}")
    return 0


def cmd_commits(args: argparse.Namespace) -> int:
    log = CommitLog(args.commit_log)
    entries = log.tail(args.last)
    if args.json:
        print(json.dumps([e.to_dict() for e in entries], indent=2, sort_keys=True))
        return 0
    counts = log.counts()
    print("  ".join(f"{k}={v}" for k, v in counts.items()))
    for entry in entries:
        totals = ""
        if entry.baseline_total is not None and entry.candidate_total is not None:
            totals = f" {entry.baseline_total:.2f}->{entry.candidate_total:.2f}"
        print(f"{entry.at[:19]} {entry.command:<7} {entry.outcome:<9}{totals} {entry.reason}")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    ledger = VersionLedger(args.ledger)
    if args.action == "list":
        for v in ledger:
            print(f"{v.version:<5} {v.at[:19]} {v.surface_digest[:12]} {len(v.files)} files  {v.acc[:70]}")
        if not len(ledger):
            print("no versions published yet (`version publish --seed --acc ...` records the current surface as v1)")
        return 0
    if args.action == "check":
        state = ledger.check(args.root)
        if args.json:
            print(json.dumps(state, indent=2, sort_keys=True))
        else:
            print(f"surface {state['status']}" + (f" (latest {state['latest']})" if state["latest"] else ""))
            for path in state["changed"][:20]:
                print(f"  differs: {path}")
        return 0 if state["status"] in ("current", "unversioned") else 1
    if not args.acc:
        raise VersionError("--acc is required to publish (promotion is a human decision)")
    version = ledger.publish(
        args.root,
        acc=args.acc,
        decision_path=args.decision,
        note=args.note,
        require_decision=not args.seed,
    )
    print(f"published {version.version}: digest {version.surface_digest[:12]}, {len(version.files)} files")
    if getattr(args, "commit_log", None):
        CommitLog(args.commit_log).record(
            run_id=version.run_id or version.version,
            command="publish",
            outcome="published",
            reason=version.acc,
            candidate_id=version.candidate_id or "",
            decision_path=version.decision_path,
            meta={"version": version.version, "surface_digest": version.surface_digest},
        )
    return 0


def cmd_gain(args: argparse.Namespace) -> int:
    criterion = GainCriterion.load(args.gain_criterion)
    log = ControlLog(args.control_log)
    suite = suite_digest(args.cases)
    if args.action == "show":
        payload = {"criterion": criterion.to_dict(), "suite_digest": suite,
                   "control_runs": [r.__dict__ for r in log.matching(baseline_id=args.baseline, suite=suite)]}
        print(json.dumps(payload, indent=2, sort_keys=True) if args.json else
              f"criterion: mean + {criterion.sd_multiplier:g} sd over >= {criterion.control_runs_min} control runs "
              f"(registered {criterion.registered_at}); suite {suite[:12]}; "
              f"{len(payload['control_runs'])} control run(s) for baseline {args.baseline!r}")
        return 0
    if args.action == "control":
        if args.runs < 1:
            raise ValueError("--runs must be >= 1")
        cases = select_cases(load_eval_cases(args.cases), args.case_source)
        baseline = _load_fixture(args.baseline)
        for _ in range(args.runs):
            report = run_benchmark(cases, baseline, _load_fixture(args.baseline),
                                   budget_steps=args.budget_steps, budget_seconds=args.budget_seconds)
            run = log.record(run_id=report.run_id, baseline_id=report.baseline_id, suite=suite,
                             total=report.candidate_total, max_points=report.max_points,
                             runner_errors=report.runner_errors())
            print(f"control {run.run_id}: {run.total:.2f} / {run.max_points:.2f}")
        controls = log.matching(baseline_id=args.baseline, suite=suite)
        print(f"{len(controls)} control run(s) recorded for baseline {args.baseline!r} on suite {suite[:12]}")
        return 0
    if args.decision is None or not args.decision.is_file():
        raise GainError("claim requires --decision <patches/...json>")
    decision = json.loads(args.decision.read_text(encoding="utf-8"))
    if decision.get("suite_digest") and decision["suite_digest"] != suite:
        raise GainError("decision was measured on a different suite digest; a claim needs the identical suite")
    controls = log.matching(baseline_id=str(decision["baseline_id"]), suite=suite)
    verdict = evaluate_gain(criterion, controls, float(decision["candidate_total"]))
    if args.json:
        print(json.dumps(verdict.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"gain claim: {'SUPPORTED' if verdict.claim else 'NOT SUPPORTED'} — {verdict.reason}")
        if not decision.get("accepted_for_review"):
            print("note: the decision itself was not accepted for review; a gain claim does not override the gates")
    if getattr(args, "commit_log", None):
        CommitLog(args.commit_log).record(
            run_id=str(decision.get("run_id", "")), command="gain", outcome="review" if verdict.claim else "rejected",
            reason=("gain claim supported: " if verdict.claim else "gain claim not supported: ") + verdict.reason,
            baseline_id=str(decision.get("baseline_id", "")), candidate_id=str(decision.get("candidate_id", "")),
            candidate_total=float(decision["candidate_total"]), decision_path=str(args.decision),
            meta={"suite_digest": suite, "gain": verdict.to_dict()},
        )
    return 0 if verdict.claim else 1


def cmd_proposal(args: argparse.Namespace) -> int:
    proposals = load_proposals(args.proposals_dir)
    if args.action == "list":
        if args.json:
            print(json.dumps([p.to_dict() for p in proposals], indent=2, ensure_ascii=False))
            return 0
        if not proposals:
            print(f"no proposals under {args.proposals_dir}")
        for item in proposals:
            print(f"{item.name:<32} {item.status:<9} {item.created:<10} {item.hypothesis[:80]}")
        return 0
    if args.action == "new":
        if not args.name or not args.hypothesis:
            raise ValueError("proposal new requires <name> and --hypothesis")
        item = ImprovementProposal(
            name=args.name,
            hypothesis=args.hypothesis,
            target_cases=args.target_case or [],
            surface_files=args.file or [],
            risk=args.risk,
        )
        report = check_novelty(item, proposals, patches_dir=PROJECT_ROOT / "patches", lessons_dir=PROJECT_ROOT / "wiki" / "lessons")
        _print_novelty(report)
        if not report.ok:
            print("not written: resolve the blocking findings first (or choose a new angle)")
            return 1
        path = write_proposal(item, args.proposals_dir)
        print(f"written: {path}")
        return 0
    # check
    targets = [p for p in proposals if args.name is None or p.name == args.name]
    if args.name is not None and not targets:
        raise ValueError(f"unknown proposal {args.name!r}")
    worst = 0
    reports = []
    for item in targets:
        report = check_novelty(item, proposals, patches_dir=PROJECT_ROOT / "patches", lessons_dir=PROJECT_ROOT / "wiki" / "lessons")
        reports.append(report.to_dict())
        if not args.json:
            _print_novelty(report)
        worst = max(worst, 0 if report.ok else 1)
    if args.json:
        print(json.dumps(reports, indent=2, ensure_ascii=False))
    return worst


def _print_novelty(report) -> None:
    print(f"proposal {report.name}: {'novel' if report.ok else 'BLOCKED'}")
    for line in report.blocking:
        print(f"  blocking: {line}")
    for line in report.warnings:
        print(f"  warning:  {line}")
    for line in report.prior_decisions:
        print(f"  prior:    {line}")
    for line in report.related_lessons:
        print(f"  lesson:   {line}")


def cmd_sources(args: argparse.Namespace) -> int:
    specs = load_registry(args.registry, base_dir=PROJECT_ROOT)
    for spec in specs:
        path = spec.resolved_path()
        status = "ok" if path.is_dir() else "MISSING"
        gen = f"gen{spec.generation}" if spec.generation else "-"
        print(f"{spec.id:<18} {spec.role:<8} {gen:<5} {spec.kind:<22} {status:<8} {path}")
        if spec.aliases:
            print(f"{'':<18} aliases: {', '.join(spec.aliases)}")
        print(f"{'':<18} allowlist: {', '.join(spec.allowlist)}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    specs = load_registry(args.registry, base_dir=PROJECT_ROOT)
    if args.source_id:
        specs = [find_source(specs, source_id) for source_id in args.source_id]
    else:
        # The live target is snapshotted per cycle (candidate_workspace); ingest is for archives.
        for spec in specs:
            if spec.is_target:
                print(f"[ingest] {spec.id}: role=target (live repo), skipped; archives only")
        specs = [spec for spec in specs if not spec.is_target]
    failures = 0
    for spec in specs:
        try:
            manifest = ingest_source(spec, args.ingest_root)
        except IngestError as exc:
            failures += 1
            print(f"[ingest] {spec.id}: ERROR {exc}", file=sys.stderr)
            continue
        if args.json:
            print(json.dumps(manifest.to_json(), sort_keys=True))
            continue
        head = (manifest.git_head or "no-git")[:12]
        dirty = " (dirty)" if manifest.git_dirty else ""
        print(
            f"[ingest] {spec.id}: {len(manifest.files)} files, {manifest.total_bytes} bytes, "
            f"{len(manifest.skipped)} skipped, head {head}{dirty}, digest {manifest.digest()[:12]}"
        )
        for skipped in manifest.skipped:
            if skipped.reason in {"symlink", "missing", "os metadata"}:
                continue
            print(f"           skip {skipped.path}: {skipped.reason}")
    return 1 if failures else 0


def _print_report(report: BenchmarkReport) -> None:
    print(f"run_id: {report.run_id}")
    print(f"baseline:  {report.baseline_id} = {report.baseline_total:.2f} / {report.max_points:.2f}")
    print(f"candidate: {report.candidate_id} = {report.candidate_total:.2f} / {report.max_points:.2f}")
    print(f"accepted: {report.accepted()}")
    print(f"regressions: {report.regressions() or 'none'}")
    print(f"critical regressions: {report.critical_regressions() or 'none'}")
    print(f"budget violations: {report.budget_violations() or 'none'}")
    print(f"runner errors: {report.runner_errors() or 'none'}")
    for baseline, candidate in zip(report.baseline_scores, report.candidate_scores):
        marker = "+" if candidate.points > baseline.points else "-" if candidate.points < baseline.points else "="
        print(f"  {marker} {candidate.case_id}: {candidate.points:.2f} vs {baseline.points:.2f}")


def main(argv: list[str] | None = None) -> int:
    args = _make_parser().parse_args(argv)
    try:
        if args.command == "benchmark":
            return cmd_benchmark(args)
        if args.command == "cycle":
            return cmd_cycle(args)
        if args.command == "search":
            return cmd_search(args)
        if args.command == "proposal":
            return cmd_proposal(args)
        if args.command == "sources":
            return cmd_sources(args)
        if args.command == "ingest":
            return cmd_ingest(args)
        if args.command == "receipt":
            return cmd_receipt(args)
        if args.command == "report":
            return cmd_report(args)
        if args.command == "reports":
            return cmd_reports(args)
        if args.command == "commits":
            return cmd_commits(args)
        if args.command == "version":
            return cmd_version(args)
        if args.command == "gain":
            return cmd_gain(args)
    except (FileNotFoundError, ValueError, RegistryError, IngestError, CcxError, ProposalError,
            ReportError, VersionError, GainError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
