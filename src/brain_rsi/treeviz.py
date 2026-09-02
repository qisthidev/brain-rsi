"""Self-contained HTML view of a search journal for the human reviewer.

Counterpart of AI-Scientist-v2's ``tree_export`` / ``viz_templates``: one static
file (inline CSS, no scripts, no external assets) showing the candidate tree,
stage log, ablation table, policy violations and the recommended diff. It is a
*view* of the append-only journal — nothing here feeds back into scoring.
"""
from __future__ import annotations

import html
from typing import Any, Mapping, Sequence

from .journal import CandidateNode, Journal

_CSS = """
body{font:14px/1.45 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:2rem;color:#222;background:#fff}
h1,h2{font-weight:600}h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:2rem}
code,pre{font:12px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace}
pre{background:#f6f8fa;padding:.8rem;overflow-x:auto;border:1px solid #ddd}
ul.tree{list-style:none;padding-left:1.2rem;border-left:1px dotted #bbb}
ul.tree>li{margin:.25rem 0}
.node{display:inline-block;padding:.15rem .5rem;border-radius:4px;border:1px solid #ccc}
.good{background:#eaf7ea;border-color:#8c8}.buggy{background:#fff4e5;border-color:#e8b060}
.violation{background:#fdeaea;border-color:#e08080}.best{outline:2px solid #2a7;outline-offset:1px}
.recommended{outline:2px dashed #27a;outline-offset:3px}
.kind{font-size:11px;text-transform:uppercase;letter-spacing:.04em;color:#555;margin-right:.4rem}
table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:.25rem .5rem;text-align:left;vertical-align:top}
th{background:#f3f3f3}.muted{color:#666}.del{color:#a00}.add{color:#070}
"""


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _status(node: CandidateNode) -> str:
    return "violation" if node.is_violation else "buggy" if node.is_buggy else "good"


def _node_html(node: CandidateNode, journal: Journal, best_id: str | None, recommended_id: str | None) -> str:
    classes = ["node", _status(node)]
    if node.id == best_id:
        classes.append("best")
    if node.id == recommended_id:
        classes.append("recommended")
    detail = []
    if node.regressions:
        detail.append("regressions: " + ", ".join(node.regressions))
    if node.critical_regressions:
        detail.append("critical: " + ", ".join(node.critical_regressions))
    if node.budget_violations:
        detail.append("budget: " + ", ".join(node.budget_violations))
    if node.policy_violations:
        detail.append("policy: " + "; ".join(node.policy_violations))
    rationale = str(node.meta.get("rationale", "") or "")
    title = " | ".join(detail + ([rationale] if rationale else []))
    label = (
        f'<span class="kind">{_esc(node.kind)}</span><strong>{_esc(node.candidate_id)}</strong> '
        f'{node.total:.2f}/{node.max_points:.0f} <span class="muted">{_esc(node.stage)} · {node.change_size} lines'
        + (f" · debug depth {node.debug_depth}" if node.debug_depth else "")
        + "</span>"
    )
    out = [f'<span class="{" ".join(classes)}" title="{_esc(title)}">{label}</span>']
    if detail:
        out.append(f'<div class="muted" style="font-size:12px;margin-left:1rem">{_esc(" | ".join(detail))}</div>')
    if node.analysis:
        out.append(f'<div class="muted" style="font-size:12px;margin-left:1rem">analysis: {_esc(node.analysis[:400])}</div>')
    children = journal.children(node)
    if children:
        out.append('<ul class="tree">')
        for child in children:
            out.append("<li>" + _node_html(child, journal, best_id, recommended_id) + "</li>")
        out.append("</ul>")
    return "".join(out)


def _diff_html(diff: str) -> str:
    lines = []
    for line in diff.splitlines():
        cls = "add" if line.startswith("+") and not line.startswith("+++") else "del" if line.startswith("-") and not line.startswith("---") else ""
        lines.append(f'<span class="{cls}">{_esc(line)}</span>' if cls else _esc(line))
    return "<pre>" + "\n".join(lines) + "</pre>" if lines else "<p class='muted'>(empty diff)</p>"


def render_tree_html(
    journal: Journal,
    *,
    best_id: str | None = None,
    recommended_id: str | None = None,
    stages: Sequence[Mapping[str, Any]] = (),
    ablation: Sequence[Mapping[str, Any]] = (),
    diff: str = "",
    usage: Mapping[str, Any] | None = None,
    reviews: Mapping[str, Any] | None = None,
    title: str | None = None,
) -> str:
    root = journal.root
    summary = journal.summary()
    parts = [
        "<!doctype html><meta charset='utf-8'>",
        f"<title>{_esc(title or f'brain-rsi search {journal.run_id}')}</title>",
        f"<style>{_CSS}</style>",
        f"<h1>brain-rsi search <code>{_esc(journal.run_id)}</code></h1>",
        "<p class='muted'>Append-only journal view. Promotion never happens here: a human reviews the recommended diff.</p>",
        "<h2>Summary</h2><table>",
    ]
    for key in ("nodes", "good", "buggy", "violations", "baseline_total", "best_total", "max_points"):
        parts.append(f"<tr><th>{_esc(key)}</th><td>{_esc(summary.get(key))}</td></tr>")
    if usage:
        parts.append(f"<tr><th>ccx usage</th><td>{_esc(usage.get('calls'))} calls, {_esc(usage.get('input_tokens'))} in / {_esc(usage.get('output_tokens'))} out tokens</td></tr>")
    parts.append("</table>")
    parts.append("<h2>Tree</h2><p class='muted'>solid outline = best, dashed = recommended (ablation-minimised)</p>")
    if root is not None:
        parts.append('<ul class="tree"><li>' + _node_html(root, journal, best_id, recommended_id) + "</li></ul>")
    if stages:
        parts.append("<h2>Stages</h2><table><tr><th>stage</th><th>kind</th><th>iters</th><th>completed</th></tr>")
        for stage in stages:
            parts.append(f"<tr><td>{_esc(stage.get('name'))}</td><td>{_esc(stage.get('kind'))}</td><td>{_esc(stage.get('iters'))}</td><td>{_esc(stage.get('completed'))}</td></tr>")
        parts.append("</table>")
    if ablation:
        parts.append("<h2>Ablation</h2><table><tr><th>hunk</th><th>verdict</th><th>total without</th><th>preview</th></tr>")
        for row in ablation:
            parts.append(
                f"<tr><td><code>{_esc(row.get('hunk'))}</code></td><td>{_esc(row.get('verdict'))}</td>"
                f"<td>{_esc(row.get('total_without', ''))}</td><td><pre>{_esc(row.get('preview', ''))}</pre></td></tr>"
            )
        parts.append("</table>")
    if journal.violation_nodes:
        parts.append("<h2>Policy violations (poisoned, never scored)</h2><ul>")
        for node in journal.violation_nodes:
            parts.append(f"<li><strong>{_esc(node.candidate_id)}</strong>: {_esc('; '.join(node.policy_violations))}</li>")
        parts.append("</ul>")
    if reviews:
        parts.append("<h2>Advisory reviews</h2><p class='muted'>" + _esc(reviews.get("advisory", "")) + "</p>")
        for review in reviews.get("reviews", []):
            if not review.get("ok"):
                parts.append(f"<p><strong>{_esc(review.get('model'))}</strong>: unavailable ({_esc(review.get('error'))})</p>")
                continue
            parts.append(
                f"<p><strong>{_esc(review.get('model'))}</strong> — verdict <em>{_esc(review.get('verdict'))}</em>, overall {_esc(review.get('overall'))}; "
                f"scores {_esc(review.get('scores'))}<br>weaknesses: {_esc('; '.join(review.get('weaknesses', [])))}<br>"
                f"must fix: {_esc('; '.join(review.get('must_fix', [])))}</p>"
            )
        meta = reviews.get("meta")
        if meta:
            parts.append(f"<p><strong>meta-review ({_esc(meta.get('model'))})</strong>: {_esc(meta.get('verdict', meta.get('error', '')))} — {_esc(meta.get('summary', ''))}<br>top concern: {_esc(meta.get('top_concern', ''))}</p>")
    parts.append("<h2>Recommended diff</h2>")
    parts.append(_diff_html(diff))
    parts.append("<h2>Events</h2><pre>")
    for event in journal.events:
        extras = {k: v for k, v in event.items() if k not in {"type", "run_id", "event", "at"}}
        parts.append(_esc(f"{event.get('at', '')}  {event.get('event')}  {extras}"))
    parts.append("</pre>")
    return "\n".join(parts) + "\n"
