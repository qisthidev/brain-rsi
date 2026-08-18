"""Read-only ingest of allowlisted agent-facing files from registered sources.

Ingest never touches the source repository. It copies only files inside the
source's allowlist (prompts, operating rules, skills), refuses credentials and
tool wiring by name and by content signature, follows no symlinks, and writes a
manifest with SHA-256 digests so a later RSI cycle can prove which baseline it
evaluated.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .sandbox import is_mutable
from .sources import SourceSpec, find_secret_signature, is_globally_denied, is_secret_filename

MANIFEST_NAME = "manifest.json"
SNAPSHOT_DIR = "repo"


class IngestError(Exception):
    """Raised when a source cannot be ingested safely."""


@dataclass(frozen=True)
class IngestedFile:
    path: str
    sha256: str
    bytes: int


@dataclass(frozen=True)
class SkippedFile:
    path: str
    reason: str


@dataclass
class IngestManifest:
    source_id: str
    source_path: str
    kind: str
    description: str
    remote: str
    aliases: list[str]
    allowlist: list[str]
    denylist: list[str]
    git_head: str | None
    git_dirty: bool | None
    ingested_at: str
    files: list[IngestedFile] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(item.bytes for item in self.files)

    def digest(self) -> str:
        """Stable digest over (path, sha256) pairs; identifies the ingested baseline."""
        hasher = hashlib.sha256()
        for item in self.files:
            hasher.update(f"{item.path}\0{item.sha256}\n".encode("utf-8"))
        return hasher.hexdigest()

    def to_json(self) -> dict:
        payload = asdict(self)
        payload["files"] = [asdict(item) for item in self.files]
        payload["skipped"] = [asdict(item) for item in self.skipped]
        payload["file_count"] = len(self.files)
        payload["skipped_count"] = len(self.skipped)
        payload["total_bytes"] = self.total_bytes
        payload["digest"] = self.digest()
        return payload


def _git(source: Path, *args: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", "-C", str(source), *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _iter_candidate_files(source: Path, spec: SourceSpec):
    """Yield (relative_posix, absolute) for allowlisted entries, never following symlinks."""
    seen: set[str] = set()
    for allowed in spec.allowlist:
        relative = Path(allowed.rstrip("/"))
        absolute = source / relative
        if absolute.is_symlink():
            yield relative.as_posix(), absolute, "symlink"
            continue
        if absolute.is_file():
            rel = relative.as_posix()
            if rel not in seen:
                seen.add(rel)
                yield rel, absolute, None
            continue
        if not absolute.is_dir():
            yield relative.as_posix(), absolute, "missing"
            continue
        for dirpath, dirnames, filenames in os.walk(absolute, followlinks=False):
            dirnames.sort()
            filenames.sort()
            current = Path(dirpath)
            kept: list[str] = []
            for name in dirnames:
                child = current / name
                if child.is_symlink():
                    yield (child.relative_to(source)).as_posix(), child, "symlink"
                elif name in {"__pycache__", "node_modules", ".git"}:
                    yield (child.relative_to(source)).as_posix() + "/", child, "excluded directory"
                else:
                    kept.append(name)
            dirnames[:] = kept
            for name in filenames:
                child = current / name
                rel = child.relative_to(source).as_posix()
                if rel in seen:
                    continue
                seen.add(rel)
                if child.is_symlink():
                    yield rel, child, "symlink"
                else:
                    yield rel, child, None


def _classify(rel: str, absolute: Path, spec: SourceSpec) -> str | None:
    """Return a skip reason, or None when the file may be copied."""
    name = Path(rel).name
    if name == ".DS_Store":
        return "os metadata"
    if is_globally_denied(rel):
        return "globally denied path"
    if not is_mutable(rel, allowlist=spec.allowlist, denylist=spec.denylist):
        return "outside source allowlist"
    if is_secret_filename(name):
        return "credential-like filename"
    try:
        size = absolute.stat().st_size
    except OSError as exc:
        return f"stat failed: {exc.__class__.__name__}"
    if size > spec.max_file_bytes:
        return f"exceeds max_file_bytes ({size} > {spec.max_file_bytes})"
    try:
        data = absolute.read_bytes()
    except OSError as exc:
        return f"read failed: {exc.__class__.__name__}"
    if b"\x00" in data[:8192]:
        return "binary content"
    text = data.decode("utf-8", errors="replace")
    signature = find_secret_signature(text)
    if signature:
        return "secret signature detected"
    return None


def ingest_source(spec: SourceSpec, ingest_root: Path, *, now: datetime | None = None) -> IngestManifest:
    """Copy allowlisted files of one source into ingest_root/<id>/repo and write a manifest."""
    source = spec.resolved_path()
    if not source.is_dir():
        raise IngestError(f"source {spec.id!r} path not found: {source}")
    source = source.resolve()

    ingest_root = ingest_root.resolve()
    if source == ingest_root or ingest_root.is_relative_to(source):
        raise IngestError("ingest root must live outside the source repository")

    target_root = ingest_root / spec.id
    snapshot = target_root / SNAPSHOT_DIR
    if target_root.exists():
        shutil.rmtree(target_root)
    snapshot.mkdir(parents=True)

    manifest = IngestManifest(
        source_id=spec.id,
        source_path=str(source),
        kind=spec.kind,
        description=spec.description,
        remote=spec.remote,
        aliases=list(spec.aliases),
        allowlist=list(spec.allowlist),
        denylist=list(spec.denylist),
        git_head=_git(source, "rev-parse", "HEAD"),
        git_dirty=(lambda status: None if status is None else bool(status))(_git(source, "status", "--porcelain")),
        ingested_at=(now or datetime.now(timezone.utc)).isoformat(),
        notes=list(spec.notes),
    )

    for rel, absolute, reason in _iter_candidate_files(source, spec):
        if reason is None:
            reason = _classify(rel, absolute, spec)
        if reason is not None:
            manifest.skipped.append(SkippedFile(path=rel, reason=reason))
            continue
        data = absolute.read_bytes()
        destination = snapshot / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        manifest.files.append(IngestedFile(path=rel, sha256=hashlib.sha256(data).hexdigest(), bytes=len(data)))

    manifest.files.sort(key=lambda item: item.path)
    manifest.skipped.sort(key=lambda item: item.path)
    (target_root / MANIFEST_NAME).write_text(
        json.dumps(manifest.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def load_manifest(ingest_root: Path, source_id: str) -> dict:
    path = ingest_root / source_id / MANIFEST_NAME
    if not path.is_file():
        raise IngestError(f"no ingest manifest for {source_id!r}: {path}")
    return json.loads(path.read_text(encoding="utf-8"))
