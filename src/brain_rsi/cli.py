"""Command-line interface for offline benchmark and guarded cycle runs."""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext
from pathlib import Path

from .benchmark import BenchmarkReport, run_benchmark
from .candidate import FixtureCandidate, FixtureConfig
from .cycle import make_decision, write_decision
from .ingest import SNAPSHOT_DIR, IngestError, ingest_source, load_manifest
from .loader import load_eval_cases, select_cases
from .sandbox import candidate_workspace
from .sources import RegistryError, find_source, load_registry
from .types import DEFAULT_BUDGET_SECONDS, DEFAULT_BUDGET_STEPS, TARGET_MUTABLE_ALLOWLIST

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BRAIN = PROJECT_ROOT.parent / "brain"
DEFAULT_REGISTRY = PROJECT_ROOT / "sources" / "registry.json"
DEFAULT_INGEST_ROOT = PROJECT_ROOT / "ingest"


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
    denylist: tuple[str, ...] = ()
    source_id: str | None = None
    source_digest: str | None = None
    if args.source_id:
        spec = find_source(load_registry(args.registry, base_dir=PROJECT_ROOT), args.source_id)
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
    decision = make_decision(report, source_id=source_id, source_digest=source_digest)
    print(f"decision: {'ACCEPT FOR HUMAN REVIEW' if decision.accepted_for_review else 'REJECT'}")
    print("promotion: disabled; a human-reviewed patch or PR is required")
    if args.write_decision:
        path = write_decision(decision, PROJECT_ROOT / "patches")
        print(f"decision artifact: {path}")
    return 0 if decision.accepted_for_review else 1


def cmd_sources(args: argparse.Namespace) -> int:
    specs = load_registry(args.registry, base_dir=PROJECT_ROOT)
    for spec in specs:
        path = spec.resolved_path()
        status = "ok" if path.is_dir() else "MISSING"
        print(f"{spec.id:<18} {spec.kind:<22} {status:<8} {path}")
        if spec.aliases:
            print(f"{'':<18} aliases: {', '.join(spec.aliases)}")
        print(f"{'':<18} allowlist: {', '.join(spec.allowlist)}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    specs = load_registry(args.registry, base_dir=PROJECT_ROOT)
    if args.source_id:
        specs = [find_source(specs, source_id) for source_id in args.source_id]
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
        if args.command == "sources":
            return cmd_sources(args)
        if args.command == "ingest":
            return cmd_ingest(args)
    except (FileNotFoundError, ValueError, RegistryError, IngestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
