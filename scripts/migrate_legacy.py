#!/usr/bin/env python3
"""Migrate the committed content of legacy second brains into brains/<id>/.

For every source in sources/registry.json this script:

1. reads the source's committed tree (``git archive HEAD``) — never the working
   tree, so uncommitted junk, ignored caches, and on-disk secrets are excluded;
2. skips credential-like filenames, git submodule stubs, symlinks, and files
   above ``--max-file-mb``; in text files, replaces secret-looking spans with
   ``[REDACTED-BY-BRAIN-RSI]`` (heuristics from ``brain_rsi.sources``) so the
   surrounding knowledge survives;
3. copies everything else verbatim into ``brains/<id>/`` (replacing a previous
   migration of that id) and writes ``brains/<id>/MIGRATION.json`` with the
   source head, per-file SHA-256 (of the migrated bytes), skipped and redacted
   files with reasons/counts, and a digest.

The source repositories are only read.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from brain_rsi.sources import is_secret_filename, load_registry, not_carried_reason, redact_secrets  # noqa: E402

TEXT_SNIFF_BYTES = 8192


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


def _is_text(data: bytes) -> bool:
    return b"\x00" not in data[:TEXT_SNIFF_BYTES]


def migrate(
    source_id: str, source: Path, destination: Path, *, max_bytes: int, dry_run: bool, keep: tuple[str, ...] = ()
) -> dict:
    head = _git(source, "rev-parse", "HEAD")
    remote = subprocess.run(
        ["git", "-C", str(source), "remote", "get-url", "origin"], capture_output=True, text=True
    ).stdout.strip()
    submodules = set()
    tree = subprocess.run(
        ["git", "-C", str(source), "ls-tree", "-r", "-z", "HEAD"], check=True, capture_output=True
    ).stdout
    for entry in tree.split(b"\0"):
        if entry and entry.split(b" ", 2)[1] == b"commit":
            submodules.add(entry.split(b"\t", 1)[1].decode("utf-8", "replace"))

    manifest = {
        "source_id": source_id,
        "source_path": str(source),
        "source_remote": remote,
        "source_head": head,
        "migrated_at": datetime.now(timezone.utc).isoformat(),
        "max_file_bytes": max_bytes,
        "migrate_keep": list(keep),
        "files": [],
        "not_carried": [],
        "redacted": [],
        "skipped": [{"path": path, "reason": "git submodule (not vendored)"} for path in sorted(submodules)],
    }

    with tempfile.TemporaryDirectory(prefix=f"migrate-{source_id}-") as tmp:
        archive = Path(tmp) / "head.tar"
        with archive.open("wb") as handle:
            subprocess.run(["git", "-C", str(source), "archive", "--format=tar", "HEAD"], check=True, stdout=handle)
        extracted = Path(tmp) / "tree"
        extracted.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(extracted, filter="data")

        if not dry_run:
            if destination.exists():
                shutil.rmtree(destination)
            destination.mkdir(parents=True)

        for dirpath, dirnames, filenames in os.walk(extracted, followlinks=False):
            dirnames.sort()
            for name in sorted(filenames):
                absolute = Path(dirpath) / name
                rel = absolute.relative_to(extracted).as_posix()
                if absolute.is_symlink():
                    manifest["skipped"].append({"path": rel, "reason": "symlink"})
                    continue
                if is_secret_filename(name):
                    manifest["skipped"].append({"path": rel, "reason": "credential-like filename"})
                    continue
                size = absolute.stat().st_size
                if size > max_bytes:
                    manifest["skipped"].append({"path": rel, "reason": f"exceeds max file size ({size} bytes)"})
                    continue
                data = absolute.read_bytes()
                reason = not_carried_reason(rel, keep)
                if reason:
                    manifest["not_carried"].append(
                        {"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "reason": reason}
                    )
                    continue
                if _is_text(data):
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        text = None
                    if text is not None:
                        redacted_text, count = redact_secrets(text)
                        if count:
                            manifest["redacted"].append({"path": rel, "count": count})
                            data = redacted_text.encode("utf-8")
                manifest["files"].append({"path": rel, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)})
                if not dry_run:
                    target = destination / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(data)
                    shutil.copystat(absolute, target)

    manifest["files"].sort(key=lambda item: item["path"])
    manifest["not_carried"].sort(key=lambda item: item["path"])
    manifest["redacted"].sort(key=lambda item: item["path"])
    manifest["skipped"].sort(key=lambda item: item["path"])
    hasher = hashlib.sha256()
    for item in manifest["files"]:
        hasher.update(f"{item['path']}\0{item['sha256']}\n".encode("utf-8"))
    manifest["digest"] = hasher.hexdigest()
    manifest["file_count"] = len(manifest["files"])
    manifest["not_carried_count"] = len(manifest["not_carried"])
    manifest["not_carried_bytes"] = sum(item["bytes"] for item in manifest["not_carried"])
    manifest["skipped_count"] = len(manifest["skipped"])
    manifest["redacted_count"] = len(manifest["redacted"])
    manifest["total_bytes"] = sum(item["bytes"] for item in manifest["files"])
    if not dry_run:
        (destination / "MIGRATION.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", "utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", type=Path, default=PROJECT_ROOT / "sources" / "registry.json")
    parser.add_argument("--destination", type=Path, default=PROJECT_ROOT / "brains")
    parser.add_argument("--source-id", action="append", help="Migrate only these ids (repeatable).")
    parser.add_argument("--max-file-mb", type=float, default=50.0, help="Skip files larger than this (GitHub hard-blocks 100 MB).")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    specs = load_registry(args.registry, base_dir=PROJECT_ROOT)
    if args.source_id:
        specs = [spec for spec in specs if spec.id in set(args.source_id)]
    status = 0
    for spec in specs:
        if spec.resolved_legacy_path() is None:
            print(f"[migrate] {spec.id}: role={spec.role}, no legacy_path, nothing to migrate", file=sys.stderr)
            continue
        source = spec.resolved_legacy_path()
        if not (source / ".git").exists():
            print(f"[migrate] {spec.id}: {source} is not a git repository, skipping", file=sys.stderr)
            status = 1
            continue
        manifest = migrate(
            spec.id,
            source,
            args.destination / spec.id,
            max_bytes=int(args.max_file_mb * 1024 * 1024),
            dry_run=args.dry_run,
            keep=spec.migrate_keep,
        )
        print(
            f"[migrate] {spec.id}: {manifest['file_count']} files, {manifest['total_bytes'] / 1e6:.1f} MB, "
            f"{manifest['skipped_count']} skipped, {manifest['not_carried_count']} not carried "
            f"({manifest['not_carried_bytes'] / 1e6:.1f} MB), {manifest['redacted_count']} redacted, "
            f"head {manifest['source_head'][:12]}, digest {manifest['digest'][:12]}"
            f"{' (dry run)' if args.dry_run else ''}"
        )
        for skipped in manifest["skipped"]:
            print(f"           skip {skipped['path']}: {skipped['reason']}")
        for redacted in manifest["redacted"]:
            print(f"           redact {redacted['path']}: {redacted['count']} span(s)")
    return status


if __name__ == "__main__":
    sys.exit(main())
