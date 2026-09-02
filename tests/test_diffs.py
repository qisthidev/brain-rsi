from __future__ import annotations

import unittest

from brain_rsi.diffs import ablate, all_hunks, apply_hunks, change_size, changed_paths, file_hunks, merge_files, unified_diff


class DiffTests(unittest.TestCase):
    def test_inserts_split_per_line_and_ablate_single_hunk(self) -> None:
        base = "a\nb\n"
        new = "a\nx\ny\nb\nz\n"
        hunks = file_hunks("f", base, new)
        self.assertEqual([h.tag for h in hunks], ["insert"] * 3)
        self.assertEqual([h.new_lines for h in hunks], [("x\n",), ("y\n",), ("z\n",)])
        self.assertEqual(apply_hunks(base, hunks), new)
        self.assertEqual(apply_hunks(base, hunks, skip={"f#1"}), "a\nx\nb\nz\n")
        self.assertEqual(apply_hunks(base, hunks, skip={h.key for h in hunks}), base)

    def test_replace_and_delete_are_atomic(self) -> None:
        base = "one\ntwo\nthree\n"
        new = "one\nTWO\n"
        hunks = file_hunks("f", base, new)
        self.assertEqual([h.tag for h in hunks], ["replace"])
        self.assertEqual(apply_hunks(base, hunks), new)
        self.assertEqual(apply_hunks(base, hunks, skip={"f#0"}), base)

    def test_file_creation_and_deletion(self) -> None:
        created = file_hunks("n", None, "hello\n")
        self.assertEqual(created[0].tag, "whole-file")
        self.assertEqual(apply_hunks(None, created), "hello\n")
        self.assertIsNone(apply_hunks(None, created, skip={"n#0"}))
        deleted = file_hunks("d", "bye\n", None)
        self.assertIsNone(apply_hunks("bye\n", deleted))
        self.assertEqual(apply_hunks("bye\n", deleted, skip={"d#0"}), "bye\n")

    def test_file_set_helpers(self) -> None:
        base = {"agent/PROMPT.md": "a\n", "keep.md": "k\n"}
        new = merge_files(base, {"agent/PROMPT.md": "a\nb\nc\n", "new.md": "n\n", "keep.md": None})
        self.assertEqual(changed_paths(base, new), ["agent/PROMPT.md", "keep.md", "new.md"])
        keys = [h.key for h in all_hunks(base, new)]
        self.assertEqual(keys, ["agent/PROMPT.md#0", "agent/PROMPT.md#1", "keep.md#0", "new.md#0"])
        self.assertEqual(change_size(base, new), 4)
        without = ablate(base, new, {"agent/PROMPT.md#1", "keep.md#0"})
        self.assertEqual(without, {"agent/PROMPT.md": "a\nb\n", "keep.md": "k\n", "new.md": "n\n"})
        diff = unified_diff(base, new)
        self.assertIn("+++ b/agent/PROMPT.md", diff)
        self.assertIn("--- /dev/null", diff)
        self.assertIn("+++ /dev/null", diff)
