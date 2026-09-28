"""`cycle` CLI on a fresh clone: snapshot defaults to the registry target, bad sources exit 2."""
from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from brain_rsi.cli import main


class CycleCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        (self.tmp / "worktree").mkdir()
        (self.tmp / "ingest").mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *extra: str) -> tuple[int, str, str]:
        argv = [
            "cycle",
            "--traces", str(self.tmp / "runs.jsonl"),
            "--commit-log", str(self.tmp / "commits.jsonl"),
            "--control-log", str(self.tmp / "control.jsonl"),
            "--workspace-parent", str(self.tmp / "worktree"),
            "--ingest-root", str(self.tmp / "ingest"),
            *extra,
        ]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_snapshot_without_source_args_uses_registry_target(self) -> None:
        code, out, _ = self._run("--snapshot-source")
        self.assertEqual(code, 0, out)
        self.assertIn("[cycle] source: brain-rsi", out)
        self.assertIn("ephemeral allowlisted snapshot", out)
        self.assertEqual(list((self.tmp / "worktree").iterdir()), [], "snapshot must be deleted after the run")

    def test_missing_source_exits_2_without_traceback(self) -> None:
        code, out, err = self._run("--snapshot-source", "--source", "/nonexistent")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("error: "), err)
        self.assertNotIn("Traceback", out + err)


if __name__ == "__main__":
    unittest.main()
