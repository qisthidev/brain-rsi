# brain-rsi

A separate, offline-first harness for testing whether a proposed change to the sibling [`brain`](../brain) agent is measurably better and still safe.

This project is intentionally **not** an autonomous overnight ratchet yet. It establishes the evaluation and containment layer that must exist before a live model is allowed to propose prompt or skill mutations.

## What it provides

- Immutable JSON evaluation suite (30 cases) covering QUESTION, COORDINATION, IMPLEMENTATION, and critical policies, including cases derived from the gen-1..3 lessons backlog (`wiki/lessons/index.md` §4).
- Deterministic scoring independent from the candidate.
- Baseline-versus-candidate comparison on the exact same cases.
- Automatic rejection of regressions, especially critical safety regressions.
- Step and wall-time budgets measured by the harness (runner-reported usage cannot understate wall time).
- Fail-closed runner isolation: exceptions, malformed outputs, identity spoofing, and incomplete suites are recorded and rejected.
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

## Migrated legacy brains (`brains/<id>/`)

This repository carries only the knowledge / agent-surface subset of each archived brain, selected by the registry's `migrate_keep`; the full legacy trees (media, books, third-party app code) stay in a frozen archive-of-record and in the legacy repositories. Every file that is not carried is still listed in `brains/<id>/MIGRATION.json` under `not_carried` with its sha256 (`scripts/prune_archive.py`).

The archives were originally migrated in full with:

```bash
python3 scripts/migrate_legacy.py            # all sources; --source-id X, --dry-run available
```

For each source in `sources/registry.json` the script reads the source's git `HEAD` tree (`legacy_path`), skips credential-like filenames, submodule stubs, symlinks and files > 50 MB, replaces secret-looking spans in text with `[REDACTED-BY-BRAIN-RSI]`, and copies everything else to `brains/<id>/` together with `brains/<id>/MIGRATION.json` (source head/remote, per-file SHA-256, skipped + redacted files, digest). Legacy repositories are only read. Re-running replaces `brains/<id>/`.

| id | legacy path | content |
|---|---|---|
| `brain` | `~/projects/brain` (`../brain`) | Seahare LLM wiki: `raw/`, `wiki/`, notes, docs, scripts, agent prompts, skills |
| `personal-archive` | `~/archive-wiki` | archived LLM Wiki home; committed `raw/`, `wiki/`, `exports/`, `projects/`, skills |
| `brain-client` | `~/projects/brain-client` | LLMWikiNG/OKF app + `wikis/main`, `wikis/client-a`, prompts, skills |

The three archive entries are **examples**: replace their `legacy_path`/`remote` with your own earlier brains, or delete them.

The registry `path` of every source points at its in-repo copy, so RSI cycles run against `brains/<id>/` and never against the external repositories.

## Registered sources and ingest

`sources/registry.json` lists every second brain this harness knows about, each with a `role` and a `generation`:

| role | meaning |
|---|---|
| `target` | exactly one — this repository itself (`path: "."`, generation 4). Its allowlist (`agent/`, `.claude/skills/`) is the **only** surface a candidate may mutate. `CLAUDE.md` is the safety contract and is deliberately outside it. |
| `archive` | generations 1–3 (`personal-archive`, `brain`, `brain-client`) kept under `brains/<id>/`. Read-only material for ingest, `wiki/lessons/`, and eval cases; `cycle --source-id` refuses them. |

The distilled experience of the archives lives in `wiki/lessons/` (`index.md` = cross-generation synthesis, principles P1–P10, and the eval-case backlog).

Each entry declares its own **mutable allowlist** (prompts, operating rules, skills). Raw sources, wiki content, credentials, and tool wiring (`.env*`, `*.mcp.json`, `settings.local.json`, keys) are excluded by a global denylist that a registry entry cannot override.

```bash
PYTHONPATH=src python3 -m brain_rsi.cli sources          # list registered sources
PYTHONPATH=src python3 -m brain_rsi.cli ingest           # read-only ingest of all sources
PYTHONPATH=src python3 -m brain_rsi.cli ingest --source-id brain
```

`ingest` copies only allowlisted files into `ingest/<id>/repo/`, never follows symlinks, refuses credential-like filenames and secret content signatures, and writes `ingest/<id>/manifest.json` (per-file SHA-256, source git head, skipped files with reasons, and a stable `digest`). The source repository is never written to. Re-running replaces the previous snapshot.

Eval cases may carry a `source` field. `--case-source <id>` runs the global cases plus the cases grounded in that source; `cycle --source-id <id>` snapshots that source's allowlist (preferring the scrubbed ingest snapshot when present) and records the source id and ingest digest in the decision artifact.

```bash
PYTHONPATH=src python3 -m brain_rsi.cli cycle --source-id brain --case-source brain --snapshot-source --write-decision
```

## Tree search over candidates (`search`)

`cycle` compares one baseline with one candidate. `search` (2026-08-19, offline) runs the
best-first tree search borrowed from AI-Scientist-v2 — see
`wiki/research/ai-scientist-v2-untuk-brain-v2.md` — with every gate kept deterministic:

| Module | Role |
|---|---|
| `journal.py` | `CandidateNode` / `Journal`: the append-only tree of evaluated candidates (`draft`, `debug`, `improve`, `ablate`), persisted as JSONL under `traces/journal/<run>.jsonl`. Best node = highest total, then fewest changed lines, then earliest; an LLM never picks it. `node_from_report` turns an existing `cycle` report into the first two nodes. |
| `validator.py` | Runs **before** scoring: every changed path must be inside the mutable allowlist and outside the denylists; credential-like filenames, secret signatures, newly introduced host paths (`/Users/…`, `/root/…`, `C:\Users\…`), binary or oversized content and no-op changes poison the node (`policy_violations`). A poisoned node is never debugged and never a parent. |
| `diffs.py` | Per-file hunks (pure insertions split per line) so a single prompt rule can be switched off; `ablate`, `unified_diff`. |
| `search.py` | `run_search`: stages `working → tuning → explore → ablation` with hard `max_iters`, `patience`, `num_drafts`, `debug_prob`, `max_debug_depth`, plus a search-wide `max_evaluations` / `wall_seconds` on top of the per-suite step/second budgets. The tuning stage may only touch files its parent touched. The ablation stage reverts each hunk of the best node, keeps only hunks that carry score and proposes a **minimal diff** when it preserves the score. |
| `cycle.make_search_decision` | Decision artifact with the journal summary, stage log, ablation table, policy violations and the recommended unified diff — still review-only. |

The `Maker` is the only LLM-facing seam; the shipped `FixtureTreeMaker` is scripted
(`fixtures/tree/demo.json`: proposals keyed by kind + parent, answers derived from the files
through `rules`), so the whole loop runs without a model.

### Live maker via `ccx` (`--maker ccx`)

`live.py` adds `CcxClient` / `CcxMaker` / `CcxRunner` on top of the `ccx` multi-model CLI
(`~/.local/bin/ccx`, CLIProxyAPI). Boundaries it keeps:

- the model sees only the ephemeral workspace files (allowlisted surface), the eval-case
  **prompts and categories**, and sanitised scorer feedback (which behaviours were forbidden /
  how many required behaviours were missing) — never the expected phrases, `eval/`, traces or
  credentials; `ccx` is called with `--direct` (no agent runtime, no tools), `--no-auto-add-dir`,
  from an empty scratch cwd, so the model cannot touch any file (an agent-runtime maker once
  wrote into `agent/` directly — that mode is no longer exposed);
- proposals are full-file contents merged in memory and run through `validator.py` before
  scoring; the runner (the agent under test) answers each case with the candidate files as its
  only instructions;
- one budgeted client: `--max-ccx-calls` hard cap, `--ccx-timeout` per call, `gpt-*` refused,
  maker and `--feedback-model` must be different families, and a token/cost ledger lands in the
  decision artifact (`search.usage`, `search.models`).

```bash
PYTHONPATH=src python3 -m brain_rsi.cli search --maker ccx --snapshot-source --source-id brain-v2 \
  --maker-model gemini-3-flash --runner-model gemini-3-flash --feedback-model deepseek-v4-flash \
  --num-drafts 1 --stage-iters working=1,tuning=0,explore=0,ablation=0 --max-ccx-calls 61 --write-decision
```

Budget rule of thumb: every scored node costs one `ccx` call per eval case (30 today), and every
proposal costs one maker call. A baseline plus one complete candidate therefore needs at least
`2 × case_count + 1` calls (61 calls for 30 cases); live search now rejects a smaller cap before
spending any calls. Check `ccx --models` first, and prefer `gemini-*` / `deepseek-*` / `cmc-*`
routes. `--max-ccx-calls` is a strict pre-call cap; `--max-ccx-tokens` blocks subsequent calls once
the reported cumulative usage reaches or crosses the cap (a provider response can overshoot it).
The complete usage ledger is printed and stored.

### Advisory reviews, tree view, proposals

- `--reviewer-models a,b[,c] [--meta-model d]` (`review.py`): reviewers from **distinct
  families** score the recommended diff on a fixed rubric (contract, clarity, evidence, risk,
  minimality) and an "area chair" model synthesises them. Stored under `search.reviews` in the
  decision artifact and shown in the HTML — advisory only, never part of `accepted_for_review`.
- Every journaled run also writes `traces/journal/<run>.html` (`treeviz.py`): a self-contained
  page with the candidate tree, stages, ablation table, violations, reviews and the diff.
- `proposal new|list|check` (`proposals.py`, files under `proposals/`): ideation with a
  deterministic novelty check against other proposals, earlier decision artifacts and
  `wiki/lessons/`. `search --use-proposals --maker ccx` seeds successive drafts with the open,
  novel hypotheses; status changes stay manual.

```bash
PYTHONPATH=src python3 -m brain_rsi.cli search --num-drafts 4 --show-diff
PYTHONPATH=src python3 -m brain_rsi.cli search --num-drafts 4 --write-decision --snapshot-source --source-id brain-v2
PYTHONPATH=src python3 -m brain_rsi.cli search --stage-iters working=4,tuning=2,explore=6,ablation=8 --seed 3
```

Exit status 1 means the recommended node does not qualify for review (no improvement,
regression, violation, or budget stop); the journal is written either way.

## Quick start

Python 3.11+ is sufficient; the runtime has no third-party dependencies.

```bash
cd brain-rsi

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
4. Both contenders complete without runner errors and stay within the configured budgets.
5. Baseline and candidate use the same immutable eval suite.

Passing is not permission to deploy. Promotion remains a manual patch/PR operation reviewed outside the candidate loop.

## Daily operations

The same checkout can double as a daily second-brain workspace: `log/`, `wiki/` and `raw/` are content, never a candidate surface. Writing them is not an RSI cycle and does not change any boundary above.

## Layout

```text
CLAUDE.md                 safety contract for agents working here
eval/cases.json           immutable evaluation cases (global + per-source)
fixtures/                 offline baseline/candidate/regression outputs
sources/registry.json     registered second-brain sources (in-repo path, legacy_path, allowlists)
brains/<id>/              full migrated legacy content + MIGRATION.json (scripts/migrate_legacy.py)
ingest/<id>/              scrubbed allowlisted prompt/skill snapshots per source (ignored; regenerable)
scripts/migrate_legacy.py one-shot, read-only migration of legacy brains into brains/
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
