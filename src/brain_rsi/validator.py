"""Validate a candidate's file changes *before* they are scored.

A node that fails validation is poisoned in the journal (``policy_violations``):
it is never debugged and never becomes a parent. Checks mirror CLAUDE.md rules
1, 3, 4 and the gen-1..3 lessons (secrets F2, host paths F5):

* every changed path must be inside the mutable allowlist and outside the
  global + source denylists (``sandbox.is_mutable``);
* credential-like filenames and secret-looking content are rejected;
* host-bound absolute paths (``/Users/…``, ``/root/…``, ``/home/…``, ``C:\\``)
  may not be *introduced* (pre-existing occurrences in the base are tolerated
  so a candidate is not blamed for legacy content it did not write);
* binary content, oversized files and empty (no-op) changes are rejected;
* a tuning-stage candidate may only touch files its parent already touched.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

from .diffs import changed_paths, file_hunks
from .sandbox import is_mutable
from .sources import find_secret_signature, is_globally_denied, is_secret_filename
from .types import TARGET_MUTABLE_ALLOWLIST, TARGET_MUTABLE_DENYLIST

HOST_PATH_RE = re.compile(r"(?<![\w/.-])(/Users/[A-Za-z0-9._-]+|/root/|/home/[A-Za-z0-9._-]+|[A-Za-z]:\\\\?Users\\)")
DEFAULT_MAX_FILE_BYTES = 256_000
DEFAULT_MAX_TOTAL_BYTES = 1_000_000


@dataclass(frozen=True)
class ValidationPolicy:
    allowlist: Sequence[str] = TARGET_MUTABLE_ALLOWLIST
    denylist: Sequence[str] = TARGET_MUTABLE_DENYLIST
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_total_bytes: int = DEFAULT_MAX_TOTAL_BYTES
    restrict_to_paths: tuple[str, ...] | None = None  # tuning stage: parent's changed files
    forbid_host_paths: bool = True


@dataclass
class ValidationResult:
    violations: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def _new_lines(base: str | None, new: str | None) -> Iterable[str]:
    for hunk in file_hunks("_", base, new):
        yield from hunk.new_lines


def validate_changes(
    base: Mapping[str, str],
    new: Mapping[str, str | None],
    policy: ValidationPolicy | None = None,
) -> ValidationResult:
    policy = policy or ValidationPolicy()
    result = ValidationResult()
    paths = changed_paths(base, new)
    result.changed_files = list(paths)
    if not paths:
        result.violations.append("no-op: candidate changed nothing")
        return result

    total = 0
    for path in paths:
        name = path.rsplit("/", 1)[-1]
        if not is_mutable(path, allowlist=policy.allowlist, denylist=policy.denylist):
            result.violations.append(f"{path}: outside mutable allowlist")
            continue
        try:
            if is_globally_denied(path):
                result.violations.append(f"{path}: globally denied path")
                continue
        except Exception:  # RegistryError on malformed paths
            result.violations.append(f"{path}: malformed path")
            continue
        if policy.restrict_to_paths is not None and path not in policy.restrict_to_paths:
            result.violations.append(f"{path}: tuning stage may only edit files already touched by its parent")
        if is_secret_filename(name):
            result.violations.append(f"{path}: credential-like filename")
        content = new.get(path)
        if content is None:
            continue  # deletion: nothing more to inspect
        if "\x00" in content[:8192]:
            result.violations.append(f"{path}: binary content")
            continue
        size = len(content.encode("utf-8"))
        total += size
        if size > policy.max_file_bytes:
            result.violations.append(f"{path}: exceeds max_file_bytes ({size} > {policy.max_file_bytes})")
        introduced = "".join(_new_lines(base.get(path), content))
        signature = find_secret_signature(introduced)
        if signature:
            result.violations.append(f"{path}: secret signature detected in introduced text")
        if policy.forbid_host_paths:
            hit = HOST_PATH_RE.search(introduced)
            if hit:
                result.violations.append(f"{path}: host-bound absolute path introduced ({hit.group(0)!r})")
    if total > policy.max_total_bytes:
        result.violations.append(f"total changed bytes {total} exceed max_total_bytes {policy.max_total_bytes}")
    return result
