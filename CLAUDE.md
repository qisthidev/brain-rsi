# brain-rsi Safety Contract

This repository evaluates proposed improvements to its own agent surface. It is not allowed to promote its own changes.

## Non-negotiable boundaries

1. Treat every `legacy_path` in `sources/registry.json` as read-only unless the owner explicitly authorizes a separate reviewed implementation task. `scripts/migrate_legacy.py` and `ingest` only read. The migrated copies under `brains/<id>/` are frozen archives (registry `role: archive`) — read-only material for ingest, `wiki/lessons/`, and eval cases, never RSI targets. The only RSI target is this repository itself (registry `role: target`) and its mutable surface is limited to `agent/` and `.claude/skills/`; a candidate edits that surface only in the ephemeral workspace. `raw/`, `wiki/`, `wikis/`, `log/` anywhere are content, not mutable surface.
2. Never edit `eval/`, `tests/`, scoring policy, or acceptance gates as part of the same candidate being evaluated.
3. Never read or copy credentials, `.env*`, private keys, tokens, git credentials, or MCP configuration.
4. Candidate mutations are limited to the allowlist in `src/brain_rsi/types.py` (or a registered source's allowlist in `sources/registry.json`, which can never override the global denylist) and occur only in an ephemeral workspace.
5. Never commit to, merge into, push, or force-push the target's `main` from a candidate.
6. A passing candidate may only produce a decision artifact for human review. Passing is not permission to promote.
7. Reject any critical regression, budget violation, fabricated external action, secret exposure, raw-source mutation, or attempt to close another person's task.
8. Use bounded steps and wall time. Repeated failures must stop rather than retry indefinitely.
9. Traces are append-only observations. Never rewrite historical traces to improve a score.
10. Do not claim RSI improvement unless the same immutable suite was run against both baseline and candidate.
11. `ingest/` snapshots contain only allowlisted prompt/skill files that passed the secret scan. Never widen an allowlist to include `raw/`, `wiki/`, `wikis/`, credentials, or MCP configuration; never hand-copy files into `ingest/`.

## Development checks

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m brain_rsi.cli benchmark
PYTHONPATH=src python3 -m brain_rsi.cli cycle
PYTHONPATH=src python3 -m brain_rsi.cli ingest
```

The default CLI uses offline fixtures. Adding a live model adapter is a separate reviewed change and must preserve all boundaries above.

## Daily content (optional)

The same checkout may serve as the live second brain: `log/`, `wiki/`, `raw/` are content written directly to the repo. Writing content is not an RSI cycle and changes none of the boundaries above: `agent/` + `.claude/skills/` stay the only candidate surface, `eval/`/`tests/`/scorer are untouched, and every commit is secret-scanned first.
