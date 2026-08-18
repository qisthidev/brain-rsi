# brain-rsi Safety Contract

This repository evaluates proposed improvements to the sibling `brain` agent. It is not allowed to promote its own changes.

## Non-negotiable boundaries

1. Treat `../brain` as read-only unless Rama explicitly authorizes a separate reviewed implementation task.
2. Never edit `eval/`, `tests/`, scoring policy, or acceptance gates as part of the same candidate being evaluated.
3. Never read or copy credentials, `.env*`, private keys, tokens, git credentials, or MCP configuration.
4. Candidate mutations are limited to the allowlist in `src/brain_rsi/types.py` and occur only in an ephemeral workspace.
5. Never commit to, merge into, push, or force-push `brain/main`.
6. A passing candidate may only produce a decision artifact for human review. Passing is not permission to promote.
7. Reject any critical regression, budget violation, fabricated external action, secret exposure, raw-source mutation, or attempt to close another person's task.
8. Use bounded steps and wall time. Repeated failures must stop rather than retry indefinitely.
9. Traces are append-only observations. Never rewrite historical traces to improve a score.
10. Do not claim RSI improvement unless the same immutable suite was run against both baseline and candidate.

## Development checks

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
PYTHONPATH=src python3 -m brain_rsi.cli benchmark
PYTHONPATH=src python3 -m brain_rsi.cli cycle
```

The default CLI uses offline fixtures. Adding a live model adapter is a separate reviewed change and must preserve all boundaries above.
