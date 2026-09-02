"""Temporary candidate workspace and path-level mutation guardrails."""
from __future__ import annotations

import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Sequence

from .types import TARGET_IMMUTABLE_DENYLIST, TARGET_MUTABLE_ALLOWLIST, TARGET_MUTABLE_DENYLIST


class SandboxError(Exception):
    """Raised when a candidate attempts an out-of-policy mutation."""


def is_mutable(
    relative_path: str | Path,
    *,
    allowlist: Sequence[str] = TARGET_MUTABLE_ALLOWLIST,
    denylist: Sequence[str] = TARGET_MUTABLE_DENYLIST,
) -> bool:
    """Return True when a path is inside the mutable allowlist.

    The immutable denylist (eval, tests, traces, raw, git metadata) always
    wins, even for a source-specific allowlist that tries to include it.
    """
    normalized = PurePosixPath(str(relative_path).replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts:
        return False
    value = normalized.as_posix()

    effective_denylist = tuple(TARGET_IMMUTABLE_DENYLIST) + tuple(denylist)
    if any(value == denied.rstrip("/") or value.startswith(denied) for denied in effective_denylist):
        return False
    return any(value == allowed.rstrip("/") or value.startswith(allowed) for allowed in allowlist)


def assert_mutable(
    relative_path: str | Path,
    *,
    allowlist: Sequence[str] = TARGET_MUTABLE_ALLOWLIST,
    denylist: Sequence[str] = TARGET_MUTABLE_DENYLIST,
) -> None:
    if not is_mutable(relative_path, allowlist=allowlist, denylist=denylist):
        raise SandboxError(f"path not in candidate mutation allowlist: {relative_path}")


def _ignore_symlinks(directory: str, names: list[str]) -> set[str]:
    """Never follow symlinks out of the allowlisted tree (e.g. raw/ or wiki/ links)."""
    return {name for name in names if (Path(directory) / name).is_symlink()}


@contextmanager
def candidate_workspace(
    source: Path,
    parent: Path,
    *,
    allowlist: Sequence[str] = TARGET_MUTABLE_ALLOWLIST,
    denylist: Sequence[str] = TARGET_MUTABLE_DENYLIST,
):
    """Copy only mutable target files into an ephemeral candidate workspace."""
    source = source.resolve()
    if not source.is_dir():
        raise SandboxError(f"source repository not found: {source}")

    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix="brain-rsi-", dir=parent))
    repo = root / "repo"
    repo.mkdir()
    try:
        for allowed in allowlist:
            if not is_mutable(allowed.rstrip("/") or allowed, allowlist=allowlist, denylist=denylist):
                continue
            relative = Path(allowed.rstrip("/"))
            source_path = source / relative
            destination = repo / relative
            if source_path.is_symlink():
                continue
            if source_path.is_dir():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source_path, destination, ignore=_ignore_symlinks)
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
