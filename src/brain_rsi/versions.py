"""Version chain for the agent surface, recorded only after a human ACC.

Reef publishes an accepted harness as a versioned artifact and every client
checks for a newer version at start ("Update with … / Skip"). Here promotion
stays manual (CLAUDE.md rules 5–6): a human applies the reviewed diff to
``agent/`` and ``.claude/skills/`` themselves, then records the resulting
surface as a version with the ACC text. This module never writes to the
surface; it only fingerprints it and appends to the ledger.

* ``publish`` refuses a decision artifact that was not accepted for review, a
  missing/malformed ACC line (``ACC <who> (...): <action>``), or a
  surface whose digest already is the latest version (nothing new to publish);
* ``check`` compares any checkout's surface digest with the latest published
  version: ``current``, ``behind`` (update available) or ``ahead`` (unpublished
  local changes) — the session decides, nothing is applied automatically.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

from .types import TARGET_MUTABLE_ALLOWLIST

ACC_RE = re.compile(r"^ACC\s+\S+.*\S", re.IGNORECASE)


class VersionError(Exception):
    pass


def surface_files(root: Path, allowlist: Sequence[str] = TARGET_MUTABLE_ALLOWLIST) -> dict[str, str]:
    """sha256 per text file inside the allowlisted surface (symlinks/binaries skipped)."""
    files: dict[str, str] = {}
    for prefix in allowlist:
        base = root / prefix
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_symlink() or not path.is_file():
                continue
            data = path.read_bytes()
            if b"\x00" in data[:8192]:
                continue
            files[path.relative_to(root).as_posix()] = hashlib.sha256(data).hexdigest()
    return files


def surface_digest(files: dict[str, str]) -> str:
    payload = json.dumps(sorted(files.items()), separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class Version:
    version: str  # v1, v2, ...
    at: str
    surface_digest: str
    files: dict[str, str]
    acc: str
    decision_path: str | None = None
    decision_sha256: str | None = None
    run_id: str | None = None
    candidate_id: str | None = None
    note: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class VersionLedger:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._versions: list[Version] = []
        if self.path.is_file():
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        self._versions.append(Version(**json.loads(line)))

    def __iter__(self) -> Iterator[Version]:
        return iter(self._versions)

    def __len__(self) -> int:
        return len(self._versions)

    @property
    def latest(self) -> Version | None:
        return self._versions[-1] if self._versions else None

    def _append(self, version: Version) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(version.to_dict(), sort_keys=True, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        self._versions.append(version)

    def publish(
        self,
        root: Path,
        *,
        acc: str,
        decision_path: Path | None = None,
        note: str = "",
        allowlist: Sequence[str] = TARGET_MUTABLE_ALLOWLIST,
        require_decision: bool = True,
    ) -> Version:
        acc = (acc or "").strip()
        if not ACC_RE.match(acc):
            raise VersionError("ACC text missing or malformed; expected the DAILY-OPS form 'ACC <who> (...): <action>'")
        decision_sha = run_id = candidate_id = None
        if decision_path is not None:
            if not decision_path.is_file():
                raise VersionError(f"decision artifact not found: {decision_path}")
            raw = decision_path.read_bytes()
            decision = json.loads(raw.decode("utf-8"))
            if not decision.get("accepted_for_review", False):
                raise VersionError("decision artifact was not accepted for review; refusing to publish")
            decision_sha = hashlib.sha256(raw).hexdigest()
            run_id = str(decision.get("run_id") or "") or None
            candidate_id = str(decision.get("candidate_id") or "") or None
            if any(v.decision_sha256 == decision_sha for v in self._versions):
                raise VersionError("this decision artifact was already published as a version")
        elif require_decision:
            raise VersionError("a decision artifact is required (pass require_decision=False for a seed version)")
        files = surface_files(root, allowlist)
        if not files:
            raise VersionError(f"no text files under the surface {list(allowlist)} in {root}")
        digest = surface_digest(files)
        if self.latest is not None and self.latest.surface_digest == digest:
            raise VersionError(f"surface is identical to {self.latest.version}; nothing new to publish")
        version = Version(
            version=f"v{len(self._versions) + 1}",
            at=datetime.now(timezone.utc).isoformat(),
            surface_digest=digest,
            files=files,
            acc=acc,
            decision_path=str(decision_path) if decision_path else None,
            decision_sha256=decision_sha,
            run_id=run_id,
            candidate_id=candidate_id,
            note=note,
        )
        self._append(version)
        return version

    def check(self, root: Path, allowlist: Sequence[str] = TARGET_MUTABLE_ALLOWLIST) -> dict[str, Any]:
        files = surface_files(root, allowlist)
        digest = surface_digest(files)
        latest = self.latest
        if latest is None:
            return {"status": "unversioned", "latest": None, "local_digest": digest, "changed": []}
        if latest.surface_digest == digest:
            return {"status": "current", "latest": latest.version, "local_digest": digest, "changed": []}
        known = {v.surface_digest: v.version for v in self._versions}
        changed = sorted(
            path
            for path in set(files) | set(latest.files)
            if files.get(path) != latest.files.get(path)
        )
        if digest in known:
            return {"status": "behind", "latest": latest.version, "local_version": known[digest], "local_digest": digest, "changed": changed}
        return {"status": "ahead", "latest": latest.version, "local_digest": digest, "changed": changed}
