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
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----|-----BEGIN [A-Z ]*PRIVATE KEY-----",
        # OpenAI / Anthropic / LiteLLM-style keys: long unhyphenated base62 tail. Slugs such as
        # "sk-normalisasi-jpoint-2026" (Indonesian SK documents) have short segments and do not match.
        r"\bsk-proj-[A-Za-z0-9_\-]{40,}",
        r"\bsk-ant-[a-z0-9]+-[A-Za-z0-9_\-]{40,}",
        r"\bsk-(?:[A-Za-z0-9]+-)?[A-Za-z0-9]{20,}\b",
        r"\bgh[pousr]_[A-Za-z0-9]{30,}",
        r"\bgithub_pat_[A-Za-z0-9_]{30,}",
        r"\bxox[abprs]-[A-Za-z0-9\-]{10,}",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bAIza[0-9A-Za-z_\-]{35}\b",
        r"\bglpat-[A-Za-z0-9_\-]{20,}",
        r"\bhf_[A-Za-z0-9]{30,}",
        r"\btskey-[A-Za-z0-9\-]{20,}",
        r"\beyJ[A-Za-z0-9_\-]{20,}\.eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}",
        r"(?i)\b(api[_-]?key|api[_-]?token|secret[_-]?key|access[_-]?token|auth[_-]?token|password|passwd)\b\s*[:=]\s*['\"]?[A-Za-z0-9][A-Za-z0-9_\-/+=.]{15,}",
        r"(?i)\bbearer\s+[A-Za-z0-9][A-Za-z0-9_\-.=]{23,}",
        # Hardcoded fallbacks such as os.environ.get("X_TOKEN", "literal") or TOKEN", "literal"
        r"(?i)(token|secret|passw(or)?d|api[_-]?key|private[_-]?key)[A-Za-z0-9_\-]*[\"']\s*[:=]\s*[\"'][A-Za-z0-9][A-Za-z0-9_\-/+=.]{15,}[\"']",
        r"(?i)(environ\.get|getenv|\.get)\(\s*[\"'][A-Za-z0-9_]*(token|secret|passw(or)?d|api[_-]?key)[A-Za-z0-9_]*[\"']\s*,\s*[\"'][A-Za-z0-9][A-Za-z0-9_\-/+=.]{15,}[\"']",
        # Documented literal tokens: "bearer token: `value`" / "token is `value`"
        r"(?i)\b(bearer\s+token|api[_-]?key|access[_-]?token|secret)\b[^\n`\"']{0,30}[`\"'][A-Za-z0-9][A-Za-z0-9_\-+=]{15,}[`\"']",
        # Long hex blobs assigned to a credential word
        r"(?i)\b(token|secret|api[_-]?key)\b\s*[\"']?\s*[:=]\s*[\"']?[0-9a-f]{32,}\b",
        r"://[^/\s:@]+:[^/\s:@]{6,}@[^/\s]+",
    )
)

DEFAULT_MAX_FILE_BYTES = 512 * 1024

# Media / binary document extensions that an archive never carries into this repo. They stay in
# the archive-of-record (the legacy repository / frozen brain-v2 history) as path + sha256 only.
NOT_CARRIED_EXTENSIONS: frozenset[str] = frozenset(
    {".pdf", ".docx", ".doc", ".pptx", ".xlsx", ".epub", ".mobi", ".mp4", ".mov", ".mkv", ".webm",
     ".mp3", ".wav", ".m4a", ".flac", ".ogg", ".zip", ".tar", ".gz", ".7z", ".rar", ".bin", ".gguf",
     ".onnx", ".pt", ".safetensors", ".iso", ".dmg", ".exe", ".dll", ".so", ".dylib", ".psd", ".ai"}
)


def not_carried_reason(relative_path: str, keep: tuple[str, ...]) -> str | None:
    """Why a legacy file is recorded (path + hash) but not copied into brains/<id>/; None = carry it."""
    rel = PurePosixPath(relative_path).as_posix()
    if rel.rsplit("/", 1)[-1] == "MIGRATION.json":
        return None
    if PurePosixPath(rel).suffix.lower() in NOT_CARRIED_EXTENSIONS:
        return "media/binary document (archive-of-record only)"
    if keep and not any(rel == k.rstrip("/") or (k.endswith("/") and rel.startswith(k)) for k in keep):
        return "outside migrate_keep"
    return None


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
    # Original external repository this source was migrated from (read-only provenance).
    legacy_path: Path | None = None
    # "target": the one live brain whose allowlist a candidate may mutate.
    # "archive": a frozen earlier generation, ingested for lessons/raw material, never an RSI target.
    role: str = "target"
    generation: int | None = None
    # Path prefixes of the legacy tree that are carried into brains/<id>/ (empty = everything).
    migrate_keep: tuple[str, ...] = ()

    @property
    def is_target(self) -> bool:
        return self.role == "target"

    def resolved_path(self) -> Path:
        return self.path.expanduser()

    def resolved_legacy_path(self) -> Path | None:
        return None if self.legacy_path is None else self.legacy_path.expanduser()


_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SOURCE_ROLES = ("target", "archive")


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
    r"(?i)(your|dein|deine|hier|sicheres?|example|placeholder|changeme|redacted|dummy|sample|xxx|"
    r"os\.environ|<[^>]+>|\$\{?[A-Z_]+\}?|[:=]\s*['\"`]?[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_.]*['\"`]?\s*$)"
)

REDACTION_MARK = "[REDACTED-BY-BRAIN-RSI]"


def _iter_secret_spans(text: str):
    for pattern in SECRET_CONTENT_PATTERNS:
        for match in pattern.finditer(text):
            if pattern.pattern.startswith("(?i)") and _PLACEHOLDER_RE.search(match.group(0)):
                continue
            yield pattern, match


def redact_secrets(text: str) -> tuple[str, int]:
    """Replace every secret-looking span with REDACTION_MARK; return (text, count)."""
    spans = sorted({(m.start(), m.end()) for _, m in _iter_secret_spans(text)})
    if not spans:
        return text, 0
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    pieces: list[str] = []
    cursor = 0
    for start, end in merged:
        pieces.append(text[cursor:start])
        pieces.append(REDACTION_MARK)
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces), len(merged)


def find_secret_signature(text: str) -> str | None:
    """Return the matching pattern when text looks like it contains a real secret.

    Obvious documentation placeholders (``API_KEY="your_key_here"``) do not count,
    but well-formed vendor tokens always do.
    """
    for pattern, _match in _iter_secret_spans(text):
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
    targets = [spec.id for spec in specs if spec.is_target]
    if len(targets) > 1:
        raise RegistryError(f"at most one source may have role 'target', got {targets}")
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
    legacy_path: Path | None = None
    if item.get("legacy_path"):
        raw_legacy = Path(str(item["legacy_path"])).expanduser()
        legacy_path = raw_legacy if raw_legacy.is_absolute() else (base_dir / raw_legacy)

    allowlist = tuple(normalize_prefix(v) for v in item.get("allowlist", list(TARGET_MUTABLE_ALLOWLIST)))
    if not allowlist:
        raise RegistryError(f"source {source_id!r} must declare a non-empty allowlist")
    for entry in allowlist:
        if is_globally_denied(entry):
            raise RegistryError(f"source {source_id!r} allowlists a globally denied path: {entry!r}")
        if is_secret_filename(PurePosixPath(entry).name):
            raise RegistryError(f"source {source_id!r} allowlists a credential-like file: {entry!r}")

    role = str(item.get("role", "target"))
    if role not in SOURCE_ROLES:
        raise RegistryError(f"source {source_id!r} has invalid role {role!r} (expected one of {SOURCE_ROLES})")
    generation = item.get("generation")
    if generation is not None and (not isinstance(generation, int) or generation < 1):
        raise RegistryError(f"source {source_id!r} generation must be a positive integer")

    denylist = tuple(normalize_prefix(v) for v in item.get("denylist", []))
    migrate_keep = tuple(normalize_prefix(v) for v in item.get("migrate_keep", []))
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
        legacy_path=legacy_path,
        role=role,
        generation=generation,
        migrate_keep=migrate_keep,
    )


def find_source(specs: list[SourceSpec], source_id: str) -> SourceSpec:
    for spec in specs:
        if spec.id == source_id:
            return spec
    raise RegistryError(f"unknown source id: {source_id!r} (known: {[s.id for s in specs]})")
