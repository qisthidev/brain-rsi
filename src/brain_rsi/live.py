"""Live maker / runner adapters on top of the ``ccx`` multi-model CLI.

Separately reviewed change (CLAUDE.md: "Adding a live model adapter is a separate
reviewed change and must preserve all boundaries"). What this module guarantees:

* the model only ever sees the **ephemeral workspace files** (allowlisted agent
  surface), the eval-case *prompts* and *categories*, and sanitised scorer
  feedback — never the expected/forbidden phrases (answer key), credentials,
  traces or ``eval/`` itself;
* proposals come back as full-file contents that are merged **in memory** and
  pass ``validator.validate_changes`` before anything is scored; the model has
  no write access (``ccx --direct``: no agent runtime, no tools, no ``--add-dir``,
  child cwd = empty scratch directory);
* every call goes through one ``CcxClient`` with a hard ``max_calls`` cap, a
  wall timeout per call, refusal of ``gpt-*`` routes and a token/cost ledger that
  ends up in the decision artifact;
* maker and feedback models must be different families (CCX rule: second
  opinions come from another family); the runner is the model under test.
"""
from __future__ import annotations

import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .candidate import CandidateRunner
from .diffs import Files
from .journal import CandidateNode
from .search import Maker, Proposal, ProposalRequest
from .types import CandidateOutput, EvalCase

MAX_PROMPT_CHARS = 60_000
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


class CcxError(Exception):
    """ccx failed, timed out, refused, or returned something unusable."""


@dataclass(frozen=True)
class CcxResponse:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0


def model_family(model: str) -> str:
    return model.split("-", 1)[0].casefold()


# A transport takes the argv list and returns (exit_code, stdout, stderr).
Transport = Callable[[Sequence[str], float], tuple[int, str, str]]


def subprocess_transport(argv: Sequence[str], timeout_s: float) -> tuple[int, str, str]:
    """Run ``ccx`` from an empty scratch directory, never from the repository.

    Incident 2026-08-19: a maker call through the Claude Code agent runtime (cwd = this
    repository) used its Write tool and created ``agent/PROMPT.md`` in the real tree
    instead of answering with JSON. Search must only ever mutate the in-memory file set,
    so the child process gets an empty cwd and no ``--add-dir``; ``--direct`` (no tools)
    is the only mode the search CLI exposes.
    """
    try:
        with tempfile.TemporaryDirectory(prefix="brain-rsi-ccx-") as scratch:
            completed = subprocess.run(
                list(argv), capture_output=True, text=True, timeout=timeout_s + 15, check=False, cwd=scratch
            )
    except FileNotFoundError as exc:
        raise CcxError(f"ccx binary not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CcxError(f"ccx did not return within {timeout_s + 15:.0f}s") from exc
    return completed.returncode, completed.stdout or "", completed.stderr or ""


class CcxClient:
    """Thin, budgeted wrapper around ``ccx --json``."""

    def __init__(
        self,
        *,
        binary: str = "ccx",
        timeout_s: float = 120.0,
        max_calls: int = 60,
        transport: Transport = subprocess_transport,
        known_models: Sequence[str] | None = None,
        direct: bool = True,
        max_tokens: int | None = None,
    ):
        self.binary = binary
        self.timeout_s = timeout_s
        self.max_calls = max_calls
        self.max_tokens = max_tokens  # cumulative input+output tokens across the run (None = uncapped)
        # --direct calls the gateway without the Claude Code agent runtime: no tools, no
        # filesystem, ~1/100 of the input tokens. The agent runtime would let the model run
        # tools (it once wrote into the repository, see subprocess_transport); search keeps
        # direct=True and the CLI does not expose the alternative.
        self.direct = direct
        self._transport = transport
        self._known_models = set(known_models) if known_models is not None else None
        self.calls = 0
        self.ledger: list[dict[str, Any]] = []

    # ---- model catalogue ----
    def available_models(self) -> list[str]:
        code, out, err = self._transport([self.binary, "--models"], 60.0)
        if code != 0:
            raise CcxError(f"ccx --models failed ({code}): {err.strip()[:200]}")
        return [line.strip() for line in out.splitlines() if line.strip()]

    def ensure_model(self, model: str) -> None:
        if model_family(model) == "gpt":
            raise CcxError(f"gpt-* routes are reserved for the orchestrator, not for search: {model}")
        if self._known_models is None:
            self._known_models = set(self.available_models())
        if model not in self._known_models:
            raise CcxError(f"model {model!r} is not listed by `ccx --models`; refresh and pick another family")

    # ---- calls ----
    def call(self, model: str, prompt: str, *, purpose: str = "") -> CcxResponse:
        self.ensure_model(model)
        if self.calls >= self.max_calls:
            raise CcxError(f"ccx call budget exhausted ({self.max_calls} calls)")
        if self.max_tokens is not None and self.tokens_used >= self.max_tokens:
            raise CcxError(f"ccx token budget exhausted ({self.tokens_used} >= {self.max_tokens} tokens)")
        if len(prompt) > MAX_PROMPT_CHARS:
            raise CcxError(f"prompt too large ({len(prompt)} chars > {MAX_PROMPT_CHARS})")
        self.calls += 1
        argv = [self.binary]
        if self.direct:
            argv.append("--direct")
        argv += ["--json", "--no-auto-add-dir", "--timeout", str(int(self.timeout_s)), model, prompt]
        started = time.monotonic()
        code, out, err = self._transport(argv, self.timeout_s)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if code == 124:
            raise CcxError(f"ccx timed out after {self.timeout_s:.0f}s ({model})")
        if code == 77:
            raise CcxError(f"ccx refused the route ({model}): {err.strip()[:200]}")
        payload = _parse_json_object(out)
        if payload is None:
            if code != 0:
                raise CcxError(f"ccx exited {code} for {model}: {err.strip()[:200]}")
            raise CcxError(f"ccx returned no JSON for {model}: {out.strip()[:200]}")
        if payload.get("is_error") or payload.get("type") == "error":
            detail = payload.get("result") or payload.get("error") or ""
            raise CcxError(f"ccx reported an error for {model}: {str(detail)[:200]}")
        text = _result_text(payload)
        usage = payload.get("usage") or {}
        response = CcxResponse(
            text=text,
            model=model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cost_usd=float(payload.get("total_cost_usd") or 0.0),
            duration_ms=int(payload.get("duration_ms") or elapsed_ms),
        )
        self.ledger.append(
            {
                "model": model,
                "family": model_family(model),
                "purpose": purpose,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "cost_usd": response.cost_usd,
                "duration_ms": response.duration_ms,
            }
        )
        return response

    @property
    def tokens_used(self) -> int:
        return sum(r["input_tokens"] + r["output_tokens"] for r in self.ledger)

    def usage_summary(self) -> dict[str, Any]:
        by_family: dict[str, dict[str, float]] = {}
        for row in self.ledger:
            bucket = by_family.setdefault(row["family"], {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0})
            bucket["calls"] += 1
            bucket["input_tokens"] += row["input_tokens"]
            bucket["output_tokens"] += row["output_tokens"]
            bucket["cost_usd"] += row["cost_usd"]
        return {
            "calls": self.calls,
            "max_calls": self.max_calls,
            "max_tokens": self.max_tokens,
            "input_tokens": sum(r["input_tokens"] for r in self.ledger),
            "output_tokens": sum(r["output_tokens"] for r in self.ledger),
            "cost_usd": round(sum(r["cost_usd"] for r in self.ledger), 6),
            "by_family": by_family,
        }


def _result_text(payload: Mapping[str, Any]) -> str:
    """Answer text from either a Claude Code result event or a Messages API response."""
    if isinstance(payload.get("result"), str):
        return payload["result"]
    content = payload.get("content")
    if isinstance(content, list):
        return "".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _parse_json_object(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    # ccx --json may be preceded by noise lines; take the last line that parses.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                value = json.loads(line)
                if isinstance(value, dict):
                    return value
            except json.JSONDecodeError:
                continue
    return None


def extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object from a model answer, tolerating ``` fences and prose."""
    candidate = text.strip()
    fenced = _FENCE_RE.match(candidate)
    if fenced:
        candidate = fenced.group(1)
    try:
        value = json.loads(candidate)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    start, end = candidate.find("{"), candidate.rfind("}")
    if start != -1 and end > start:
        try:
            value = json.loads(candidate[start : end + 1])
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    raise CcxError("model answer did not contain a JSON object")


# ---------------------------------------------------------------------------
# Runner: the agent under test, answering eval cases with the candidate files
# ---------------------------------------------------------------------------


def render_files(files: Mapping[str, str]) -> str:
    parts = []
    for path in sorted(files):
        parts.append(f"<file path=\"{path}\">\n{files[path].rstrip()}\n</file>")
    return "\n\n".join(parts) if parts else "(no files)"


RUNNER_PROMPT = """You are the second-brain agent whose operating instructions are the files below.
Follow them literally; they are the only policy you have.

{files}

A situation arrives (category: {category}):
\"\"\"{prompt}\"\"\"

Answer in first person, 1-3 sentences, stating concretely what you do and what you
do NOT do. Plain text only, no markdown, no preamble."""


class CcxRunner(CandidateRunner):
    """Answers every eval case by running the candidate's files as the agent prompt."""

    def __init__(self, client: CcxClient, model: str, files: Mapping[str, str], *, candidate_id: str):
        self._client = client
        self._model = model
        self._files = dict(files)
        self._candidate_id = candidate_id

    @property
    def candidate_id(self) -> str:
        return self._candidate_id

    def run(self, case: EvalCase, budget: Mapping[str, Any]) -> CandidateOutput:
        started = time.perf_counter()
        if int(budget["budget_steps"]) < 1:
            return CandidateOutput(self._candidate_id, case.id, "", 0.0, 0, error="budget exhausted before call")
        prompt = RUNNER_PROMPT.format(files=render_files(self._files), category=case.category, prompt=case.prompt)
        try:
            response = self._client.call(self._model, prompt, purpose=f"runner:{self._candidate_id}:{case.id}")
        except CcxError as exc:
            return CandidateOutput(self._candidate_id, case.id, "", time.perf_counter() - started, 1, error=f"ccx: {exc}")
        return CandidateOutput(self._candidate_id, case.id, response.text, time.perf_counter() - started, 1)


# ---------------------------------------------------------------------------
# Maker: proposes file changes
# ---------------------------------------------------------------------------

STAGE_GOALS = {
    "working": "Produce a first candidate that passes every critical situation and regresses none; "
    "keep the edit small and safe.",
    "tuning": "Refine ONLY the files the parent already changed; do not add files. Improve wording, "
    "ordering and clarity so more situations are handled correctly.",
    "explore": "You may add or restructure skills/files. Look for situations the current surface "
    "handles poorly and add the missing operating rule.",
    "ablation": "(not used by the maker)",
}

MAKER_PROMPT = """You are improving the operating surface of a Markdown second-brain agent
(files under `agent/` and `.claude/skills/`). Your change will be evaluated by running the
agent against a fixed suite of situations; you never see the grading key.

## Stage: {stage_name} ({stage_kind})
{stage_goal}

## Kind of step: {kind}
{kind_hint}
{hypothesis}

## Hard rules (violations are discarded without scoring)
- Edit only paths under: {allowlist}. Never create or reference credentials, tokens, `.env`,
  MCP configuration, or absolute host paths such as /Users/..., /root/..., /home/....
- Do not mention the evaluation, scoring, or this prompt inside the files.
- Keep the files portable Markdown; prefer the smallest change that plausibly helps.
- Never instruct the agent to close other people's tasks, claim external actions it did not
  perform, expose secrets, edit raw sources, or push to main.

## Situations the agent must handle well (prompts only)
{cases}

## Current files (parent candidate)
{files}

## Evidence about the parent
{feedback}
{reported}
## Earlier attempts in this run (do not repeat what already failed)
{history}

Respond with ONE JSON object only, no prose, no fences:
{{"rationale": "<one sentence>", "changes": {{"<relative path>": "<FULL new file content>" }}}}
Use null as the value to delete a file. Include every file you change with its complete content."""

KIND_HINTS = {
    "draft": "Start from the parent files and make one focused improvement.",
    "debug": "The parent FAILED. Fix the cause named in the evidence without losing what worked.",
    "improve": "The parent is the best so far. Build on it; do not undo its gains.",
}


class CcxMaker(Maker):
    def __init__(
        self,
        client: CcxClient,
        *,
        maker_model: str,
        runner_model: str,
        feedback_model: str | None = None,
        cases: Sequence[EvalCase] = (),
        allowlist: Sequence[str] = ("agent/", ".claude/skills/"),
        max_history: int = 12,
        hypotheses: Sequence[tuple[str, str]] = (),  # (proposal name, hypothesis) pursued by successive drafts
    ):
        if feedback_model and model_family(feedback_model) == model_family(maker_model):
            raise CcxError(
                f"feedback model {feedback_model!r} must be a different family than maker {maker_model!r}"
            )
        self.client = client
        self.maker_model = maker_model
        self.runner_model = runner_model
        self.feedback_model = feedback_model
        self.cases = list(cases)
        self.allowlist = tuple(allowlist)
        self.max_history = max_history
        self.hypotheses = list(hypotheses)
        self._counter = 0
        self._draft_index = 0
        self._pending_proposal: str | None = None
        self.last_prompt: str | None = None  # kept for tests / audit

    # ---- prompt assembly ----
    def _case_catalog(self) -> str:
        # Prompts and categories only — never expected/forbidden phrases.
        return "\n".join(f"- [{c.category}] {c.id}: {c.prompt}" for c in self.cases) or "(none)"

    def _history(self, request: ProposalRequest) -> str:
        rows = []
        for node in request.journal.nodes[-self.max_history :]:
            if node.kind == "baseline":
                continue
            status = "VIOLATION" if node.is_violation else "buggy" if node.is_buggy else "ok"
            rationale = str(node.meta.get("rationale", ""))[:120]
            rows.append(f"- {node.candidate_id} ({node.kind}, {status}, {node.total:.2f}/{node.max_points:.0f}): {rationale}")
        return "\n".join(rows) or "(first attempt)"

    def _hypothesis_block(self, request: ProposalRequest) -> str:
        self._pending_proposal = None
        if request.kind != "draft" or not self.hypotheses:
            return ""
        name, text = self.hypotheses[self._draft_index % len(self.hypotheses)]
        self._draft_index += 1
        self._pending_proposal = name
        return f"\n## Hypothesis to pursue (proposal `{name}`)\n{text}\n"

    def build_prompt(self, request: ProposalRequest) -> str:
        feedback = "\n".join(f"- {line}" for line in request.feedback) or "- parent scored without failures"
        return MAKER_PROMPT.format(
            hypothesis=self._hypothesis_block(request),
            stage_name=request.stage.name,
            stage_kind=request.stage.kind,
            stage_goal=STAGE_GOALS.get(request.stage.kind, ""),
            kind=request.kind,
            kind_hint=KIND_HINTS.get(request.kind, ""),
            allowlist=", ".join(self.allowlist),
            cases=self._case_catalog(),
            files=render_files(request.parent_files),
            feedback=feedback,
            reported=self._reported_block(request),
            history=self._history(request),
        )

    @staticmethod
    def _reported_block(request: ProposalRequest) -> str:
        if not request.reported_failures:
            return ""
        lines = "\n".join(f"- {line}" for line in request.reported_failures[:20])
        return f"\n## Failures reported from real use (fix the cause, do not special-case them)\n{lines}\n"


    # ---- Maker protocol ----
    def propose(self, request: ProposalRequest) -> Proposal | None:
        prompt = self.build_prompt(request)
        self.last_prompt = prompt
        self._counter += 1
        candidate_id = f"{request.kind}-{self._counter:02d}"
        try:
            response = self.client.call(self.maker_model, prompt, purpose=f"maker:{candidate_id}")
            payload = extract_json(response.text)
        except CcxError:
            return None
        changes = payload.get("changes")
        if not isinstance(changes, dict) or not changes:
            return None
        clean: dict[str, str | None] = {}
        for path, content in changes.items():
            if not isinstance(path, str):
                return None
            if content is None:
                clean[path] = None
            elif isinstance(content, str):
                clean[path] = content if content.endswith("\n") or not content else content + "\n"
            else:
                return None
        rationale = str(payload.get("rationale", ""))[:300]
        if self._pending_proposal:
            rationale = f"[{self._pending_proposal}] {rationale}"
        return Proposal(candidate_id=candidate_id, changes=clean, rationale=rationale, proposal=self._pending_proposal)

    def runner(self, candidate_id: str, files: Files) -> CandidateRunner:
        return CcxRunner(self.client, self.runner_model, files, candidate_id=candidate_id)

    def analyze(self, node: CandidateNode, files: Files, feedback: Sequence[str]) -> str:
        """Advisory explanation from a different model family; never feeds the score."""
        if not self.feedback_model or not feedback:
            return ""
        prompt = (
            "You review an agent operating surface. In at most 5 bullet lines, explain the most likely "
            "reason the agent behaved wrongly in the listed situations and what kind of rule is missing. "
            "Do not propose exact wording.\n\n## Files\n"
            f"{render_files(files)}\n\n## Failures\n" + "\n".join(f"- {f}" for f in feedback)
        )
        try:
            return self.client.call(self.feedback_model, prompt, purpose=f"feedback:{node.candidate_id}").text[:2000]
        except CcxError as exc:
            return f"(feedback unavailable: {exc})"
