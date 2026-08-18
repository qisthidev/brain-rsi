# brain-rsi

A separate, offline-first harness for testing whether a proposed change to the sibling [`brain`](../brain) agent is measurably better and still safe.

This project is intentionally **not** an autonomous overnight ratchet yet. It establishes the evaluation and containment layer that must exist before a live model is allowed to propose prompt or skill mutations.

## What it provides

- Immutable JSON evaluation suite covering QUESTION, COORDINATION, IMPLEMENTATION, and critical policies.
- Deterministic scoring independent from the candidate.
- Baseline-versus-candidate comparison on the exact same cases.
- Automatic rejection of regressions, especially critical safety regressions.
- Step and wall-time budgets.
- Append-only JSONL traces.
- Ephemeral source snapshot containing only allowlisted prompt/skill files.
- Decision artifacts that may be submitted for human review.
- No automatic write, commit, push, merge, or promotion into `../brain`.
- Offline fixtures, so the complete evaluation path runs without an API key or LLM.

## Trust boundary

```text
../brain (read-only source)
        |
        | copy allowlisted files only
        v
worktree/<temporary>/repo
        |
        | candidate proposal (future live adapter)
        v
immutable eval/cases.json ---> deterministic scorer
        |                             |
        +------- baseline ------------+
        +------- candidate -----------+
                                      v
                       accept for review / reject
                                      |
                           human-reviewed patch or PR
```

The candidate maker must not be able to alter `eval/`, `tests/`, scorer code, acceptance gates, traces, raw sources, credentials, git metadata, or `brain/main`.

## Migrating a legacy brain into the repo (optional)

An instance repository can hold the full committed content of its legacy second brains:

```bash
python3 scripts/migrate_legacy.py            # all sources; --source-id X, --dry-run available
```

For each source the script reads the git `HEAD` tree of `legacy_path` (or `path`), skips credential-like filenames, submodule stubs, symlinks and files > 50 MB, replaces secret-looking spans in text with `[REDACTED-BY-BRAIN-RSI]`, and writes `brains/<id>/` plus `brains/<id>/MIGRATION.json` (source head/remote, per-file SHA-256, skipped + redacted lists, digest). Point the registry `path` at `brains/<id>` afterwards so RSI cycles run in-repo. `brains/` is git-ignored in this public template.

## Registered sources and ingest

`sources/registry.json` lists every second brain this harness may evaluate. Each entry declares its own **mutable allowlist** (prompts, operating rules, skills). Raw sources, wiki content, credentials, and tool wiring (`.env*`, `*.mcp.json`, `settings.local.json`, keys) are excluded by a global denylist that a registry entry cannot override. Relative paths resolve against the project root.

```bash
PYTHONPATH=src python3 -m brain_rsi.cli sources          # list registered sources
PYTHONPATH=src python3 -m brain_rsi.cli ingest           # read-only ingest of all sources
PYTHONPATH=src python3 -m brain_rsi.cli ingest --source-id brain
```

`ingest` copies only allowlisted files into `ingest/<id>/repo/`, never follows symlinks, refuses credential-like filenames and secret content signatures, and writes `ingest/<id>/manifest.json` (per-file SHA-256, source git head, skipped files with reasons, and a stable `digest`). The source repository is never written to. Re-running replaces the previous snapshot. `ingest/` is ignored by Git here; an instance repository may choose to commit its scrubbed snapshots.

Eval cases may carry a `source` field. `--case-source <id>` runs the global cases plus the cases grounded in that source; `cycle --source-id <id>` snapshots that source's allowlist (preferring the scrubbed ingest snapshot when present) and records the source id and ingest digest in the decision artifact.

```bash
PYTHONPATH=src python3 -m brain_rsi.cli cycle --source-id brain --case-source brain --snapshot-source --write-decision
```

## Quick start

Python 3.11+ is sufficient; the runtime has no third-party dependencies.

```bash
cd /Users/rama/orca/projects/brain-rsi

PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m brain_rsi.cli benchmark
PYTHONPATH=src python3 -m brain_rsi.cli cycle
```

`benchmark` appends an observation to `traces/runs.jsonl` (ignored by Git). `cycle` is dry-run-safe by default: it evaluates fixtures but neither snapshots nor changes `brain`.

To demonstrate the allowlisted ephemeral snapshot and write a review decision:

```bash
PYTHONPATH=src python3 -m brain_rsi.cli cycle \
  --snapshot-source \
  --write-decision
```

The snapshot is deleted after the run. The decision under `patches/` says only whether the candidate qualifies for human review; it is never promoted automatically.

To verify that a critical regression is rejected:

```bash
PYTHONPATH=src python3 -m brain_rsi.cli cycle --candidate regressed
```

That command exits with status 1.

## Acceptance policy

A candidate is accepted for review only when all conditions hold:

1. It scores at least `0.01` points above the baseline.
2. No case that passed in the baseline becomes a failure.
3. No critical case regresses.
4. Both contenders stay within the configured budgets.
5. Baseline and candidate use the same immutable eval suite.

Passing is not permission to deploy. Promotion remains a manual patch/PR operation reviewed outside the candidate loop.

## Layout

```text
CLAUDE.md                 safety contract for agents working here
eval/cases.json           immutable evaluation cases (global + per-source)
fixtures/                 offline baseline/candidate/regression outputs
sources/registry.json     registered second-brain sources (path, optional legacy_path, allowlists)
brains/<id>/              optional full migrated legacy content + MIGRATION.json (ignored here)
scripts/migrate_legacy.py read-only migration of a legacy brain's git HEAD into brains/
ingest/<id>/              scrubbed allowlisted snapshots + manifests per source (ignored)
src/brain_rsi/            runner, scorer, sandbox, sources, ingest, benchmark, cycle CLI
tests/                    independent regression tests
traces/                   append-only local run records (ignored)
patches/                  local review decisions (ignored)
worktree/                 ephemeral allowlisted snapshots (ignored)
```

## Adding a live adapter later

A future adapter can implement `CandidateRunner` in `src/brain_rsi/candidate.py`. Before enabling it, it must:

- execute only inside `candidate_workspace`;
- receive no secrets or unrelated raw content;
- enforce subprocess timeout and process termination itself;
- expose token/cost/step measurements;
- write a patch rather than modifying `brain`;
- use a checker independent from the candidate maker;
- keep eval cases and acceptance policy inaccessible to candidate mutation.

Installing `recursive-improve`, adding Claude/OpenAI calls, scheduling `/ratchet`, or allowing automatic promotion are deliberately out of scope for this initial safe scaffold and require separate verification and approval.
