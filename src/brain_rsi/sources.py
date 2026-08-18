"""Registry of second-brain sources that may be ingested for RSI evaluation.

Every source declares its own mutable allowlist (prompts, operating rules,
skills). Raw sources, wiki content, credentials, and tool configuration are
never part of an allowlist; a global denylist enforces that regardless of what
the registry says.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from .types import TARGET_IMMUTABLE_DENYLIST, TARGET_MUTABLE_ALLOWLIST


class RegistryError(Exception):
    """Raised when the source registry is malformed or violates the contract."""


# Path prefixes that can never be ingested or mutated, whatever a registry says.
GLOBAL_DENYLIST_PREFIXES: tuple[str, ...] = TARGET_IMMUTABLE_DENYLIST + (
    "wiki/",
    "wikis/",
    "exports/",
    "backups/",
    "data/",
    "prod-debug/",
    "scratch/",
    "tmp/",
    "node_modules/",
    ".venv/",
    ".obsidian/",
)

# File names / globs that are treated as credentials or tool wiring, never copied.
SECRET_FILENAME_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^\.env(\..*)?$",
        r"^\.?mcp\.json$",
        r"^mcp_config\.json$",
        r"^settings\.local\.json$",
        r"^scheduled_tasks\.json$",
        r"^opencode\.json$",
        r"^config\.json$",
        r"^\.netrc$",
        r"^\.npmrc$",
        r"^\.pypirc$",
        r"^id_(rsa|dsa|ecdsa|ed25519)(\.pub)?$",
        r".*\.(pem|key|p12|pfx|jks|keystore)$",
        r".*\.secret$",
        r"^bot\$.*\.json$",
        r"^(credentials|secrets?|tokens?)(\..*)?$",
        r".*service[-_]account.*\.json$",
    )
)

# Content signatures of leaked secrets. Files matching any pattern are skipped.
SECRET_CONTENT_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern)
    for pattern in (
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"\bsk-(proj-|ant-)?[A-Za-z0-9_\-]{20,}",
        r"\bgh[pousr]_[A-Za-z0-9]{30,}",
        r"\bgithub_pat_[A-Za-z0-9_]{30,}",
        r"\bxox[abprs]-[A-Za-z0-9\-]{10,}",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bAIza[0-9A-Za-z_\-]{35}\b",
        r"\bglpat-[A-Za-z0-9_\-]{20,}",
        r"\bhf_[A-Za-z0-9]{30,}",
        r"\btskey-[A-Za-z0-9\-]{20,}",
        r"(?i)\b(api[_-]?key|api[_-]?token|secret[_-]?key|access[_-]?token|auth[_-]?token|password|passwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+=.]{16,}",
        r"(?i)\bbearer\s+[A-Za-z0-9_\-.=]{24,}",
        # Hardcoded fallbacks such as os.environ.get("X_TOKEN", "literal") or TOKEN", "literal"
        r"(?i)(token|secret|passw(or)?d|api[_-]?key|private[_-]?key)[A-Za-z0-9_\-]*[\"']\s*[,:=]\s*[\"'][A-Za-z0-9_\-/+=.]{16,}[\"']",
        # Documented literal tokens: "bearer token: `value`" / "token is `value`"
        r"(?i)\b(bearer\s+token|api[_-]?key|access[_-]?token|secret)\b[^\n`\"']{0,30}[`\"'][A-Za-z0-9_\-+=]{16,}[`\"']",
        # Long bare hex blobs next to a credential word
        r"(?i)(token|secret|api[_-]?key)[^\n]{0,40}\b[0-9a-f]{32,}\b",
        r"://[^/\s:@]+:[^/\s:@]{6,}@[^/\s]+",
    )
)

DEFAULT_MAX_FILE_BYTES = 512 * 1024


@dataclass(frozen=True)
class SourceSpec:
    id: str
    path: Path
    kind: str
    description: str = ""
    remote: str = ""
    aliases: tuple[str, ...] = ()
    allowlist: tuple[str, ...] = TARGET_MUTABLE_ALLOWLIST
    denylist: tuple[str, ...] = ()
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    notes: tuple[str, ...] = field(default_factory=tuple)

    def resolved_path(self) -> Path:
        return self.path.expanduser()


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


def normalize_prefix(value: str) -> str:
    normalized = PurePosixPath(str(value).replace("\\", "/"))
    if normalized.is_absolute() or ".." in normalized.parts or not normalized.parts:
        raise RegistryError(f"invalid relative path in registry: {value!r}")
    text = normalized.as_posix()
    return text + "/" if str(value).endswith("/") else text


def is_globally_denied(relative_path: str) -> bool:
    value = normalize_prefix(relative_path)
    for denied in GLOBAL_DENYLIST_PREFIXES:
        if value == denied.rstrip("/") or value.startswith(denied):
            return True
    return False


def is_secret_filename(name: str) -> bool:
    return any(pattern.match(name) for pattern in SECRET_FILENAME_PATTERNS)


_PLACEHOLDER_RE = re.compile(
    r"(?i)(your|dein|deine|hier|sicheres?|example|placeholder|changeme|redacted|dummy|sample|xxx|<[^>]+>|\$\{?[A-Z_]+\}?)"
)


def find_secret_signature(text: str) -> str | None:
    """Return the matching pattern when text looks like it contains a real secret.

    Obvious documentation placeholders (``API_KEY="your_key_here"``) do not count,
    but well-formed vendor tokens always do.
    """
    for pattern in SECRET_CONTENT_PATTERNS:
        for match in pattern.finditer(text):
            if pattern.pattern.startswith("(?i)") and _PLACEHOLDER_RE.search(match.group(0)):
                continue
            return pattern.pattern
    return None


def load_registry(path: str | Path, *, base_dir: Path | None = None) -> list[SourceSpec]:
    registry_path = Path(path)
    if not registry_path.is_file():
        raise RegistryError(f"source registry not found: {registry_path}")
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise RegistryError("registry must be an object with a 'sources' list")
    base_dir = base_dir or registry_path.parent
    specs = [_parse_spec(item, base_dir) for item in payload["sources"]]
    ids = [spec.id for spec in specs]
    if len(ids) != len(set(ids)):
        raise RegistryError("source ids must be unique")
    return specs


def _parse_spec(item: Any, base_dir: Path) -> SourceSpec:
    if not isinstance(item, dict):
        raise RegistryError("each source must be an object")
    for key in ("id", "path", "kind"):
        if key not in item:
            raise RegistryError(f"source missing key: {key!r}")
    source_id = str(item["id"])
    if not _ID_RE.match(source_id):
        raise RegistryError(f"invalid source id: {source_id!r}")

    raw_path = Path(str(item["path"])).expanduser()
    path = raw_path if raw_path.is_absolute() else (base_dir / raw_path)

    allowlist = tuple(normalize_prefix(v) for v in item.get("allowlist", list(TARGET_MUTABLE_ALLOWLIST)))
    if not allowlist:
        raise RegistryError(f"source {source_id!r} must declare a non-empty allowlist")
    for entry in allowlist:
        if is_globally_denied(entry):
            raise RegistryError(f"source {source_id!r} allowlists a globally denied path: {entry!r}")
        if is_secret_filename(PurePosixPath(entry).name):
            raise RegistryError(f"source {source_id!r} allowlists a credential-like file: {entry!r}")

    denylist = tuple(normalize_prefix(v) for v in item.get("denylist", []))
    max_bytes = int(item.get("max_file_bytes", DEFAULT_MAX_FILE_BYTES))
    if max_bytes <= 0:
        raise RegistryError(f"source {source_id!r} max_file_bytes must be positive")

    return SourceSpec(
        id=source_id,
        path=path,
        kind=str(item["kind"]),
        description=str(item.get("description", "")),
        remote=str(item.get("remote", "")),
        aliases=tuple(str(v) for v in item.get("aliases", [])),
        allowlist=allowlist,
        denylist=denylist,
        max_file_bytes=max_bytes,
        notes=tuple(str(v) for v in item.get("notes", [])),
    )


def find_source(specs: list[SourceSpec], source_id: str) -> SourceSpec:
    for spec in specs:
        if spec.id == source_id:
            return spec
    raise RegistryError(f"unknown source id: {source_id!r} (known: {[s.id for s in specs]})")
