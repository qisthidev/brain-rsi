"""Text-level diff helpers for candidate file sets.

A candidate is represented as ``Files = dict[relative_posix_path, content]``.
``None`` as content means "file deleted". Hunks are computed per file with
``difflib`` so the ablation stage can switch individual hunks off (AI-Scientist-v2
stage 4 "component analysis", applied to prompt/skill edits instead of code).
"""
from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Iterable, Mapping

Files = dict[str, str]


@dataclass(frozen=True)
class Hunk:
    file: str
    index: int  # position within the file's hunk list
    tag: str  # replace | delete | insert | whole-file
    old_start: int
    old_end: int
    new_start: int
    new_end: int
    old_lines: tuple[str, ...]
    new_lines: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.file}#{self.index}"

    @property
    def size(self) -> int:
        return max(len(self.old_lines), len(self.new_lines))

    def preview(self, limit: int = 3) -> str:
        removed = [f"- {line}" for line in self.old_lines[:limit]]
        added = [f"+ {line}" for line in self.new_lines[:limit]]
        more = self.size - limit
        tail = [f"  … (+{more} more lines)"] if more > 0 else []
        return "\n".join(removed + added + tail)


def _lines(text: str | None) -> list[str]:
    return text.splitlines(keepends=True) if text else []


def file_hunks(file: str, base: str | None, new: str | None, *, split_inserts: bool = True) -> list[Hunk]:
    """Hunks turning ``base`` into ``new`` for one file (empty when equal).

    Pure insertions are split per line (``split_inserts``) so that each added
    rule of a prompt is an independently ablatable unit."""
    if base == new:
        return []
    if base is None or new is None:
        # Creation or deletion: one atomic hunk; ablating it restores the base state.
        old = tuple(_lines(base))
        fresh = tuple(_lines(new))
        return [Hunk(file, 0, "whole-file", 0, len(old), 0, len(fresh), old, fresh)]
    old_lines = _lines(base)
    new_lines = _lines(new)
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    hunks: list[Hunk] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag == "insert" and split_inserts:
            # One hunk per inserted line so ablation can switch single rules off
            # (a prompt edit is usually a block of adjacent new lines).
            for offset, line in enumerate(new_lines[j1:j2]):
                hunks.append(
                    Hunk(file, len(hunks), "insert", i1, i1, j1 + offset, j1 + offset + 1, (), (line,))
                )
            continue
        hunks.append(
            Hunk(
                file=file,
                index=len(hunks),
                tag=tag,
                old_start=i1,
                old_end=i2,
                new_start=j1,
                new_end=j2,
                old_lines=tuple(old_lines[i1:i2]),
                new_lines=tuple(new_lines[j1:j2]),
            )
        )
    return hunks


def changed_paths(base: Mapping[str, str], new: Mapping[str, str | None]) -> list[str]:
    paths = set(base) | set(new)
    return sorted(path for path in paths if base.get(path) != new.get(path))


def all_hunks(base: Mapping[str, str], new: Mapping[str, str | None]) -> list[Hunk]:
    hunks: list[Hunk] = []
    for path in changed_paths(base, new):
        hunks.extend(file_hunks(path, base.get(path), new.get(path)))
    return hunks


def change_size(base: Mapping[str, str], new: Mapping[str, str | None]) -> int:
    return sum(hunk.size for hunk in all_hunks(base, new))


def apply_hunks(base: str | None, hunks: Iterable[Hunk], *, skip: Iterable[str] = ()) -> str | None:
    """Rebuild the new content of one file from its hunks, leaving out ``skip`` keys."""
    skipped = set(skip)
    hunk_list = [hunk for hunk in hunks]
    if not hunk_list:
        return base
    if hunk_list[0].tag == "whole-file":
        hunk = hunk_list[0]
        if hunk.key in skipped:
            return base
        return "".join(hunk.new_lines) if hunk.new_lines else None
    old_lines = _lines(base)
    out: list[str] = []
    cursor = 0
    for hunk in hunk_list:
        out.extend(old_lines[cursor : hunk.old_start])
        if hunk.key in skipped:
            out.extend(hunk.old_lines)
        else:
            out.extend(hunk.new_lines)
        cursor = hunk.old_end
    out.extend(old_lines[cursor:])
    return "".join(out)


def ablate(base: Mapping[str, str], new: Mapping[str, str | None], skip: Iterable[str]) -> Files:
    """Return the candidate file set with the hunks named in ``skip`` reverted."""
    skipped = set(skip)
    result: Files = {path: content for path, content in base.items()}
    for path in changed_paths(base, new):
        hunks = file_hunks(path, base.get(path), new.get(path))
        content = apply_hunks(base.get(path), hunks, skip=skipped)
        if content is None:
            result.pop(path, None)
        else:
            result[path] = content
    return result


def unified_diff(base: Mapping[str, str], new: Mapping[str, str | None], *, context: int = 2) -> str:
    chunks: list[str] = []
    for path in changed_paths(base, new):
        before = _lines(base.get(path))
        after = _lines(new.get(path))
        chunks.append(
            "".join(
                difflib.unified_diff(
                    before,
                    after,
                    fromfile=f"a/{path}" if path in base else "/dev/null",
                    tofile=f"b/{path}" if new.get(path) is not None else "/dev/null",
                    n=context,
                )
            )
        )
    return "".join(chunks)


def merge_files(base: Mapping[str, str], changes: Mapping[str, str | None]) -> Files:
    """Apply full-file ``changes`` (None = delete) on top of ``base``."""
    result: Files = dict(base)
    for path, content in changes.items():
        if content is None:
            result.pop(path, None)
        else:
            result[path] = content
    return result
