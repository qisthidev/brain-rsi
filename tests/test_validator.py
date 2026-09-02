from __future__ import annotations

import unittest

from brain_rsi.validator import ValidationPolicy, validate_changes

BASE = {"agent/PROMPT.md": "Rules.\nSee /Users/legacy/path (pre-existing).\n"}


class ValidatorTests(unittest.TestCase):
    def test_clean_change_passes(self) -> None:
        result = validate_changes(BASE, {"agent/PROMPT.md": BASE["agent/PROMPT.md"] + "Answer first.\n"})
        self.assertTrue(result.ok, result.violations)
        self.assertEqual(result.changed_files, ["agent/PROMPT.md"])

    def test_noop_is_rejected(self) -> None:
        self.assertIn("no-op: candidate changed nothing", validate_changes(BASE, dict(BASE)).violations)

    def test_forbidden_paths(self) -> None:
        new = dict(BASE)
        new.update({"eval/cases.json": "[]", "CLAUDE.md": "x", "wiki/page.md": "y", "../escape.md": "z"})
        violations = validate_changes(BASE, new, ValidationPolicy(allowlist=("agent/", ".claude/skills/"))).violations
        self.assertTrue(any(v.startswith("eval/cases.json") for v in violations))
        self.assertTrue(any(v.startswith("CLAUDE.md") for v in violations))
        self.assertTrue(any(v.startswith("wiki/page.md") for v in violations))
        self.assertTrue(any(v.startswith("../escape.md") for v in violations))

    def test_secret_signature_and_credential_filename(self) -> None:
        new = dict(BASE)
        new["agent/PROMPT.md"] += "key sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnop1234567890\n"
        new[".claude/skills/x/.env"] = "TOKEN=1"
        violations = validate_changes(BASE, new).violations
        self.assertTrue(any("secret signature" in v for v in violations))
        self.assertTrue(any("credential-like filename" in v for v in violations))

    def test_host_path_only_when_introduced(self) -> None:
        # Re-flowing the pre-existing line is fine; adding a new /Users path is not.
        ok = validate_changes(BASE, {"agent/PROMPT.md": BASE["agent/PROMPT.md"].replace("Rules.", "Rules!")})
        self.assertTrue(ok.ok, ok.violations)
        bad = validate_changes(BASE, {"agent/PROMPT.md": BASE["agent/PROMPT.md"] + "Read /Users/owner/x.md\n"})
        self.assertTrue(any("host-bound absolute path" in v for v in bad.violations))
        win = validate_changes(BASE, {"agent/PROMPT.md": BASE["agent/PROMPT.md"] + "Open C:\\Users\\owner\\x.md\n"})
        self.assertTrue(any("host-bound absolute path" in v for v in win.violations))

    def test_tuning_restriction_binary_and_size(self) -> None:
        policy = ValidationPolicy(restrict_to_paths=("agent/PROMPT.md",), max_file_bytes=20)
        new = {"agent/PROMPT.md": "x" * 30 + "\n", ".claude/skills/new/SKILL.md": "hi\x00bin", "agent/OTHER.md": "o\n"}
        violations = validate_changes(BASE, new, policy).violations
        self.assertTrue(any("exceeds max_file_bytes" in v for v in violations))
        self.assertTrue(any("binary content" in v for v in violations))
        self.assertTrue(any("tuning stage may only edit" in v and ".claude/skills/new/SKILL.md" in v for v in violations))
        self.assertTrue(any("tuning stage may only edit" in v and "agent/OTHER.md" in v for v in violations))
        self.assertTrue(validate_changes(BASE, {"agent/OTHER.md": "o\n"}).ok)  # agent/ is the surface
        self.assertFalse(validate_changes(BASE, {"CLAUDE.md": "x\n"}).ok)  # contract is not
