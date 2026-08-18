from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain_rsi.loader import SafetyError, load_eval_cases, select_cases

ROOT = Path(__file__).resolve().parents[1]


class LoaderTests(unittest.TestCase):
    def test_duplicate_ids_are_rejected(self) -> None:
        case = {
            "id": "duplicate",
            "category": "QUESTION",
            "prompt": "prompt",
            "expected": ["answer"],
            "forbidden": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cases.json"
            path.write_text(json.dumps([case, case]), encoding="utf-8")
            with self.assertRaises(SafetyError):
                load_eval_cases(path)

    def test_select_cases_keeps_global_and_source_cases(self) -> None:
        cases = load_eval_cases(ROOT / "eval" / "cases.json")
        selected = select_cases(cases, "brain")
        ids = {case.id for case in selected}
        self.assertIn("never-close-foreign-task", ids)  # global
        self.assertIn("brain-log-rotation", ids)  # grounded in brain
        other = select_cases(cases, "another-source")
        self.assertNotIn("brain-log-rotation", {case.id for case in other})
        self.assertEqual(select_cases(cases, None), cases)
        with self.assertRaises(SafetyError):
            select_cases([case for case in cases if case.source], "unknown-source")


if __name__ == "__main__":
    unittest.main()
