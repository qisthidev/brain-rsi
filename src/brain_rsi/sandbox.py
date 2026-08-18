"""Temporary candidate workspace and path-level mutation guardrails."""
from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from .types import TARGET_IMMUTABLE_DENYLIST, TARGET_MUTABLE_ALLOWLIST


class SandboxError(Exception):
    """Raised when a candidate attempts an out-of-policy mutation."""


def is_mutable(relative_path: str | Path) -> bool:
    normalized = PurePosixPath(str(relative_path).replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        return False
    value = normalized.as_posix()

    if any(value == denied.rstrip("/") or value.startswith(denied) for denied in TARGET_IMMUTABLE_DENYLIST):
        return False
    return any(value == allowed.rstrip("/") or value.startswith(allowed) for allowed in TARGET_MUTABLE_ALLOWLIST)


def assert_mutable(relative_path: str | Path) -> None:
    if not is_mutable(relative_path):
        raise SandboxError(f"path not in candidate mutation allowlist: {relative_path}")


@contextmanager
def candidate_workspace(source: Path, parent: Path):
    """Copy only mutable target files into an ephemeral candidate workspace."""
    source = source.resolve()
    if not source.is_dir():
        raise SandboxError(f"source repository not found: {source}")

    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="brain-rsi-", dir=parent))
    repo = root / "repo"
    repo.mkdir()
    try:
        for allowed in TARGET_MUTABLE_ALLOWLIST:
            relative = Path(allowed.rstrip("/"))
            source_path = source / relative
            destination = repo / relative
            if source_path.is_dir():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source_path, destination)
            elif source_path.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination)
        (root / "CONTRACT.txt").write_text(
            "Ephemeral candidate workspace. Only allowlisted prompt/skill files are present. "
            "Evaluation, traces, raw sources, credentials, git metadata, and main are unavailable.\n",
            encoding="utf-8",
        )
        yield repo
    finally:
        shutil.rmtree(root, ignore_errors=True)
