"""Immutable eval-case loader with schema validation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .types import EvalCase


class SafetyError(Exception):
    """Raised when immutable evaluation input is invalid."""


def load_eval_cases(path: str | Path) -> list[EvalCase]:
    eval_path = Path(path)
    if not eval_path.is_file():
        raise SafetyError(f"eval cases path not found: {eval_path}")

    data = json.loads(eval_path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        raise SafetyError("eval cases must be a non-empty JSON list")

    cases = [_parse_case(item) for item in data]
    ids = [case.id for case in cases]
    if len(ids) != len(set(ids)):
        raise SafetyError("eval case ids must be unique")
    return cases


def _parse_case(item: Any) -> EvalCase:
    if not isinstance(item, dict):
        raise SafetyError("each eval case must be an object")
    required = {"id", "category", "prompt", "expected", "forbidden"}
    missing = required - item.keys()
    if missing:
        raise SafetyError(f"case missing keys: {sorted(missing)}")
    if not item["expected"]:
        raise SafetyError(f"case {item['id']!r} must define expected phrases")

    weight = float(item.get("weight", 1.0))
    if weight <= 0:
        raise SafetyError(f"case {item['id']!r} weight must be positive")

    return EvalCase(
        id=str(item["id"]),
        category=str(item["category"]),
        prompt=str(item["prompt"]),
        expected=tuple(str(value) for value in item["expected"]),
        forbidden=tuple(str(value) for value in item["forbidden"]),
        weight=weight,
        critical=bool(item.get("critical", False)),
    )
