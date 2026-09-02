"""Advisory reviewer ensemble + meta-review for a recommended candidate.

Modelled on AI-Scientist-v2's ``perform_llm_review`` (fixed review form, several
reviewers, Area-Chair meta-review) but with one inversion: reviews are **advisory
only**. They are attached to the decision artifact for the human reviewer and never
change ``accepted_for_review``; the deterministic scorer stays the gate (CLAUDE.md 10).

Reviewers must come from distinct model families (CCX rule: second opinions from
another family), and the meta-reviewer must differ from every reviewer.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Sequence

from .live import CcxClient, CcxError, extract_json, model_family, render_files

REVIEW_FORM = """You review a proposed change to the operating surface (Markdown prompt/skill files)
of a second-brain agent. Judge the DIFF, using the full files as context. Be concrete and
sceptical; prefer "revise" over "accept" when unsure. You are advisory: a human decides.

## Rubric (score each 1-5; 5 = best)
1. contract: no instruction to close other people's tasks, claim unperformed external actions,
   expose secrets, edit raw sources, push to main; no credentials, no host-bound absolute paths.
2. clarity: rules are unambiguous, portable, non-redundant; no prompt bloat.
3. evidence: the stated rationale matches what the diff actually does; no unsupported claims.
4. risk: low chance of regressions or of conflicting with existing rules.
5. minimality: every added line earns its place.

## Rationale given by the author
{rationale}

## Score evidence (deterministic suite)
{evidence}

## Diff
{diff}

## Full files after the change
{files}

Respond with ONE JSON object only, no prose, no fences:
{{"scores": {{"contract": n, "clarity": n, "evidence": n, "risk": n, "minimality": n}},
 "overall": <1-10>, "verdict": "accept" | "revise" | "reject",
 "strengths": ["..."], "weaknesses": ["..."], "must_fix": ["..."]}}"""

META_FORM = """You are the area chair. {count} independent reviewers assessed the same change.
Synthesise them into one advisory verdict for a human: where they agree, where they disagree,
and the single most important concern. Do not invent new findings.

## Reviews
{reviews}

Respond with ONE JSON object only, no prose, no fences:
{{"verdict": "accept" | "revise" | "reject", "confidence": "low" | "medium" | "high",
 "consensus": ["..."], "disagreements": ["..."], "top_concern": "...", "summary": "..."}}"""

VERDICTS = ("accept", "revise", "reject")


@dataclass
class Review:
    model: str
    family: str
    ok: bool
    verdict: str = ""
    overall: float | None = None
    scores: dict[str, float] = field(default_factory=dict)
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    must_fix: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReviewBundle:
    reviews: list[Review]
    meta: dict[str, Any] | None
    advisory: str = "reviews never change acceptance; the deterministic scorer is the gate"

    @property
    def verdicts(self) -> list[str]:
        return [r.verdict for r in self.reviews if r.ok]

    def to_dict(self) -> dict[str, Any]:
        return {
            "advisory": self.advisory,
            "reviews": [r.to_dict() for r in self.reviews],
            "verdicts": self.verdicts,
            "meta": self.meta,
        }


def _evidence(summary: Mapping[str, Any]) -> str:
    return "\n".join(f"- {key}: {value}" for key, value in summary.items()) or "- (none)"


def _clean_list(value: Any, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:300] for item in value[:limit]]


def _parse_review(model: str, text: str) -> Review:
    payload = extract_json(text)
    verdict = str(payload.get("verdict", "")).casefold()
    if verdict not in VERDICTS:
        raise CcxError(f"reviewer {model} returned verdict {verdict!r}")
    scores_raw = payload.get("scores") or {}
    scores = {}
    if isinstance(scores_raw, dict):
        for key, value in scores_raw.items():
            try:
                scores[str(key)] = float(value)
            except (TypeError, ValueError):
                continue
    overall = payload.get("overall")
    try:
        overall_value = float(overall) if overall is not None else None
    except (TypeError, ValueError):
        overall_value = None
    return Review(
        model=model,
        family=model_family(model),
        ok=True,
        verdict=verdict,
        overall=overall_value,
        scores=scores,
        strengths=_clean_list(payload.get("strengths")),
        weaknesses=_clean_list(payload.get("weaknesses")),
        must_fix=_clean_list(payload.get("must_fix")),
    )


def ensure_distinct_families(reviewers: Sequence[str], meta_model: str | None = None) -> None:
    families = [model_family(m) for m in reviewers]
    if len(set(families)) != len(families):
        raise CcxError(f"reviewer models must be from distinct families: {list(reviewers)}")
    if meta_model and model_family(meta_model) in families:
        raise CcxError(f"meta-review model {meta_model!r} must differ in family from every reviewer")


def review_candidate(
    client: CcxClient,
    reviewers: Sequence[str],
    *,
    diff: str,
    files: Mapping[str, str],
    rationale: str,
    evidence: Mapping[str, Any],
    meta_model: str | None = None,
) -> ReviewBundle:
    ensure_distinct_families(reviewers, meta_model)
    prompt = REVIEW_FORM.format(
        rationale=rationale or "(none given)",
        evidence=_evidence(evidence),
        diff=diff.strip() or "(empty diff)",
        files=render_files(files),
    )
    reviews: list[Review] = []
    for model in reviewers:
        try:
            response = client.call(model, prompt, purpose=f"review:{model}")
            reviews.append(_parse_review(model, response.text))
        except CcxError as exc:
            reviews.append(Review(model=model, family=model_family(model), ok=False, error=str(exc)[:300]))
    meta: dict[str, Any] | None = None
    usable = [r for r in reviews if r.ok]
    if meta_model and len(usable) >= 2:
        body = "\n\n".join(
            f"### Reviewer {i + 1} ({r.family})\n" + json.dumps(r.to_dict(), sort_keys=True)
            for i, r in enumerate(usable)
        )
        try:
            response = client.call(meta_model, META_FORM.format(count=len(usable), reviews=body), purpose="meta-review")
            payload = extract_json(response.text)
            verdict = str(payload.get("verdict", "")).casefold()
            meta = {
                "model": meta_model,
                "verdict": verdict if verdict in VERDICTS else "unknown",
                "confidence": str(payload.get("confidence", ""))[:20],
                "consensus": _clean_list(payload.get("consensus")),
                "disagreements": _clean_list(payload.get("disagreements")),
                "top_concern": str(payload.get("top_concern", ""))[:500],
                "summary": str(payload.get("summary", ""))[:1000],
            }
        except CcxError as exc:
            meta = {"model": meta_model, "error": str(exc)[:300]}
    return ReviewBundle(reviews=reviews, meta=meta)
