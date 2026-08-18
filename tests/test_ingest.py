from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from brain_rsi.ingest import IngestError, ingest_source, load_manifest
from brain_rsi.sources import SourceSpec


def build_source(root: Path) -> Path:
    source = root / "source"
    (source / "agent").mkdir(parents=True)
    (source / "agent" / "PROMPT.md").write_text("# prompt\n", encoding="utf-8")
    (source / ".claude" / "skills" / "demo").mkdir(parents=True)
    (source / ".claude" / "skills" / "demo" / "SKILL.md").write_text("skill\n", encoding="utf-8")
    (source / ".claude" / "skills" / "demo" / "settings.local.json").write_text("{}", encoding="utf-8")
    (source / ".claude" / "skills" / "leak").mkdir()
    (source / ".claude" / "skills" / "leak" / "SKILL.md").write_text(
        "token=ghp_" + "z" * 36 + "\n", encoding="utf-8"
    )
    (source / ".claude" / "skills" / "big").mkdir()
    (source / ".claude" / "skills" / "big" / "SKILL.md").write_bytes(b"x" * 4096)
    (source / "raw").mkdir()
    (source / "raw" / "note.md").write_text("never copied\n", encoding="utf-8")
    (source / "wiki").mkdir()
    (source / "wiki" / "index.md").write_text("never copied\n", encoding="utf-8")
    (source / ".env").write_text("SECRET=1\n", encoding="utf-8")
    os.symlink(source / "wiki", source / ".claude" / "skills" / "wiki-link")
    return source


class IngestTests(unittest.TestCase):
    def test_ingest_copies_only_safe_allowlisted_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = build_source(root)
            spec = SourceSpec(
                id="demo",
                path=source,
                kind="test",
                allowlist=("agent/PROMPT.md", ".claude/skills/", ".env", "missing.md"),
                max_file_bytes=1024,
            )
            manifest = ingest_source(spec, root / "ingest")
            copied = sorted(item.path for item in manifest.files)
            self.assertEqual(copied, [".claude/skills/demo/SKILL.md", "agent/PROMPT.md"])
            reasons = {item.path: item.reason for item in manifest.skipped}
            self.assertEqual(reasons[".env"], "credential-like filename")
            self.assertEqual(reasons[".claude/skills/demo/settings.local.json"], "credential-like filename")
            self.assertEqual(reasons[".claude/skills/leak/SKILL.md"], "secret signature detected")
            self.assertEqual(reasons[".claude/skills/wiki-link"], "symlink")
            self.assertEqual(reasons["missing.md"], "missing")
            self.assertTrue(reasons[".claude/skills/big/SKILL.md"].startswith("exceeds max_file_bytes"))

            snapshot = root / "ingest" / "demo" / "repo"
            self.assertTrue((snapshot / "agent" / "PROMPT.md").is_file())
            self.assertFalse((snapshot / "raw").exists())
            self.assertFalse((snapshot / "wiki").exists())
            self.assertFalse((snapshot / ".env").exists())
            self.assertFalse((snapshot / ".claude" / "skills" / "leak").exists())

            loaded = load_manifest(root / "ingest", "demo")
            self.assertEqual(loaded["digest"], manifest.digest())
            self.assertEqual(loaded["file_count"], 2)
            # Source is untouched.
            self.assertTrue((source / "raw" / "note.md").is_file())
            self.assertEqual(sorted(p.name for p in source.iterdir()), [".claude", ".env", "agent", "raw", "wiki"])

    def test_ingest_is_deterministic_and_replaces_previous_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = build_source(root)
            spec = SourceSpec(id="demo", path=source, kind="test", allowlist=("agent/",))
            first = ingest_source(spec, root / "ingest")
            stale = root / "ingest" / "demo" / "repo" / "stale.md"
            stale.write_text("stale", encoding="utf-8")
            second = ingest_source(spec, root / "ingest")
            self.assertEqual(first.digest(), second.digest())
            self.assertFalse(stale.exists())

    def test_ingest_root_inside_source_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = build_source(root)
            spec = SourceSpec(id="demo", path=source, kind="test", allowlist=("agent/",))
            with self.assertRaises(IngestError):
                ingest_source(spec, source / "ingest")


if __name__ == "__main__":
    unittest.main()
