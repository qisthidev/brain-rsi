from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from brain_rsi.sources import (
    RegistryError,
    find_secret_signature,
    find_source,
    is_globally_denied,
    is_secret_filename,
    load_registry,
)


ROOT = Path(__file__).resolve().parents[1]


def write_registry(directory: Path, sources: list[dict]) -> Path:
    path = directory / "registry.json"
    path.write_text(json.dumps({"sources": sources}), encoding="utf-8")
    return path


class RegistryTests(unittest.TestCase):
    def test_project_registry_loads_and_has_unique_ids(self) -> None:
        specs = load_registry(ROOT / "sources" / "registry.json")
        self.assertTrue(specs)
        self.assertEqual(len({spec.id for spec in specs}), len(specs))
        self.assertEqual(find_source(specs, "brain").kind, "seahare-llm-wiki")

    def test_registry_rejects_denied_and_credential_allowlist_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for bad in ("raw/", "wiki/", "eval/cases.json", ".env", "agent/mcp.json"):
                path = write_registry(root, [{"id": "x", "path": ".", "kind": "k", "allowlist": [bad]}])
                with self.assertRaises(RegistryError, msg=bad):
                    load_registry(path)
            path = write_registry(root, [{"id": "x", "path": ".", "kind": "k", "allowlist": ["../secret.md"]}])
            with self.assertRaises(RegistryError):
                load_registry(path)

    def test_registry_rejects_duplicate_ids_and_bad_ids(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_registry(root, [{"id": "a", "path": ".", "kind": "k"}, {"id": "a", "path": ".", "kind": "k"}])
            with self.assertRaises(RegistryError):
                load_registry(path)
            path = write_registry(root, [{"id": "Bad Id", "path": ".", "kind": "k"}])
            with self.assertRaises(RegistryError):
                load_registry(path)

    def test_relative_paths_resolve_against_registry_dir(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = write_registry(root, [{"id": "a", "path": "../brain", "kind": "k"}])
            spec = load_registry(path)[0]
            self.assertEqual(spec.resolved_path(), root / "../brain")


class SecretHeuristicTests(unittest.TestCase):
    def test_denied_prefixes(self) -> None:
        for denied in ("raw/x.md", "wiki/index.md", "wikis/main/log.md", "eval/cases.json", ".git/config", "tmp/a"):
            self.assertTrue(is_globally_denied(denied), denied)
        self.assertFalse(is_globally_denied("agent/PROMPT.md"))

    def test_credential_filenames(self) -> None:
        for name in (".env", ".env.local", ".mcp.json", "mcp_config.json", "settings.local.json", "id_rsa", "x.pem", "bot$abc.json"):
            self.assertTrue(is_secret_filename(name), name)
        for name in ("SKILL.md", "PROMPT.md", "import.py", ".agy.yaml"):
            self.assertFalse(is_secret_filename(name), name)

    def test_content_signatures(self) -> None:
        self.assertIsNotNone(find_secret_signature("-----BEGIN OPENSSH PRIVATE KEY-----"))
        self.assertIsNotNone(find_secret_signature("token=ghp_" + "a" * 36))
        self.assertIsNotNone(find_secret_signature("key: sk-" + "b" * 40))
        self.assertIsNotNone(find_secret_signature("api_key = 'q7Hf9sKd0PzL2mNv8XwT4'"))
        self.assertIsNotNone(find_secret_signature('TOKEN = os.environ.get("X_INGEST_TOKEN", "0f1e2d3c4b5a69788796a5b4c3d2e1f0")'))
        self.assertIsNotNone(find_secret_signature('TOKEN = os.environ.get("X_INGEST_TOKEN", "staging-2026-fk-daily-token")'))
        self.assertIsNotNone(find_secret_signature("- Default bearer token: `svc-staging-2026-daily-abc`; override with env"))
        # Placeholders and keychain lookups are documentation, not secrets.
        self.assertIsNone(find_secret_signature('json.load(f).get("OPENAI_API_KEY", "")'))
        self.assertIsNone(find_secret_signature('["security","find-generic-password","-s","GEMINI_API_KEY","-w"]'))
        self.assertIsNone(find_secret_signature("commit 440650b fixed the token parser"))
        self.assertIsNone(find_secret_signature("the shared secret defined in `docker-compose.yml` is read at boot"))
        self.assertIsNone(find_secret_signature('API_KEY="llmw_dein_api_key_hier_einfuegen"'))
        self.assertIsNone(find_secret_signature("api_key: <YOUR_KEY_HERE_PLEASE>"))
        self.assertIsNone(find_secret_signature("security find-generic-password -s GEMINI_API_KEY -w"))
        self.assertIsNone(find_secret_signature("forbidden phrase list: api_key= here is the token"))


if __name__ == "__main__":
    unittest.main()
