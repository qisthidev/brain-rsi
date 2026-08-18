from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from brain_rsi.sandbox import SandboxError, assert_mutable, candidate_workspace, is_mutable


class SandboxTests(unittest.TestCase):
    def test_path_allowlist(self) -> None:
        self.assertTrue(is_mutable("agent/PROMPT.md"))
        self.assertTrue(is_mutable(".claude/skills/example/SKILL.md"))
        self.assertFalse(is_mutable("eval/cases.json"))
        self.assertFalse(is_mutable("raw/sources/note.md"))
        self.assertFalse(is_mutable("../brain/.env"))
        with self.assertRaises(SandboxError):
            assert_mutable("tests/test_policy.py")

    def test_workspace_copies_only_allowlisted_material(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            (source / "agent").mkdir(parents=True)
            (source / "agent" / "PROMPT.md").write_text("prompt", encoding="utf-8")
            (source / "raw").mkdir()
            (source / "raw" / "secret.txt").write_text("do not copy", encoding="utf-8")
            with candidate_workspace(source, root / "worktrees") as workspace:
                self.assertTrue((workspace / "agent" / "PROMPT.md").is_file())
                self.assertFalse((workspace / "raw").exists())
            self.assertEqual(list((root / "worktrees").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
