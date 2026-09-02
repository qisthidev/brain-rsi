#!/usr/bin/env python3
"""Prune an already-migrated archive under brains/<id>/ down to its registry ``migrate_keep``.

Used once when brain-v2.1 was carved out of brain-v2 (2026-08-19): the full legacy trees
(470 MB, mostly media, books, and third-party app code) stay in the archive-of-record
(the frozen ``qisthidev/brain-v2`` history and the legacy repositories); this repository
carries only the knowledge/agent-surface subset. Every removed file is kept in
``MIGRATION.json`` under ``not_carried`` with its original sha256, so provenance survives.

Idempotent and read-only with respect to the legacy repositories.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from brain_rsi.sources import load_registry, not_carried_reason  # noqa: E402


def prune(source_id: str, root: Path, keep: tuple[str, ...], *, dry_run: bool) -> dict:
    manifest_path = root / "MIGRATION.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    kept, not_carried = [], list(manifest.get("not_carried", []))
    removed_bytes = 0
    for item in manifest["files"]:
        reason = not_carried_reason(item["path"], keep)
        if reason is None:
            kept.append(item)
            continue
        not_carried.append({**item, "reason": reason})
        removed_bytes += item["bytes"]
        target = root / item["path"]
        if not dry_run and target.exists():
            target.unlink()
    # Files present on disk but absent from the manifest (should not happen) are left alone.
    if not dry_run:
        for directory in sorted((p for p in root.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            if not any(directory.iterdir()):
                directory.rmdir()
    hasher = hashlib.sha256()
    for item in kept:
        hasher.update(f"{item['path']}\0{item['sha256']}\n".encode("utf-8"))
    manifest.update(
        {
            "files": kept,
            "not_carried": sorted(not_carried, key=lambda i: i["path"]),
            "migrate_keep": list(keep),
            "file_count": len(kept),
            "total_bytes": sum(i["bytes"] for i in kept),
            "not_carried_count": len(not_carried),
            "not_carried_bytes": sum(i["bytes"] for i in not_carried),
            "carried_digest": hasher.hexdigest(),
            "pruned_note": "digest = original full-tree digest; carried_digest = files present in this repo",
        }
    )
    if not dry_run:
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", "utf-8")
    return {"kept": len(kept), "removed_bytes": removed_bytes, "not_carried": len(not_carried)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", type=Path, default=PROJECT_ROOT / "sources" / "registry.json")
    parser.add_argument("--brains", type=Path, default=PROJECT_ROOT / "brains")
    parser.add_argument("--source-id", action="append")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    specs = load_registry(args.registry, base_dir=PROJECT_ROOT)
    for spec in specs:
        if args.source_id and spec.id not in set(args.source_id):
            continue
        root = args.brains / spec.id
        if spec.is_target:
            continue
        if not (root / "MIGRATION.json").is_file():
            print(f"[prune] {spec.id}: no {root}/MIGRATION.json, skipping", file=sys.stderr)
            continue
        if not spec.migrate_keep:
            print(f"[prune] {spec.id}: no migrate_keep in registry, nothing to prune")
            continue
        result = prune(spec.id, root, spec.migrate_keep, dry_run=args.dry_run)
        print(
            f"[prune] {spec.id}: kept {result['kept']} files, not carried {result['not_carried']} "
            f"({result['removed_bytes'] / 1e6:.1f} MB removed){' (dry run)' if args.dry_run else ''}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
