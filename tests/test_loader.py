from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain_rsi.loader import SafetyError, load_eval_cases


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


if __name__ == "__main__":
    unittest.main()
