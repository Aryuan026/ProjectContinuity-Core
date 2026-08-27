"""Bounded, shared evidence hygiene for future scan and maintain callers."""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Dict, Mapping, Optional, Tuple


DEFAULT_MAX_DEPTH = 5
DEFAULT_MAX_ITEMS = 50
DEFAULT_MAX_STRING = 500
MAX_PROJECTION_STRING = 500
REDACTED = "[REDACTED]"
_EXCLUDED_PARTS = frozenset(
    {".git", ".venv", ".pytest_cache", "__pycache__", "node_modules", "graphify-out"}
)
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "access_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "private_key",
    "secret",
    "signature",
    "token",
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_AUTH_HEADER = re.compile(
    r"(?im)\b((?:proxy-)?authorization)\s*:\s*[^\r\n]+"
)
_QUOTED_ASSIGNMENT_DOUBLE = re.compile(
    r'(?i)("([A-Za-z][A-Za-z0-9_.-]*)"\s*:\s*)"(?:\\.|[^"\\])*"'
)
_QUOTED_ASSIGNMENT_SINGLE = re.compile(
    r"(?i)('([A-Za-z][A-Za-z0-9_.-]*)'\s*:\s*)'(?:\\.|[^'\\])*'"
)
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s@]+@")
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?key|credential|key|password|secret|sig|"
    r"signature|token)=)[^&#\s]+"
)
_ASSIGNMENT = re.compile(
    r"(?im)\b([A-Za-z][A-Za-z0-9_.-]*)(\s*[:=])"
    r"([^,;\r\n]*?)(?=[ \t]+[A-Za-z][A-Za-z0-9_.-]*\s*[:=]|[,;\r\n]|$)"
)


@dataclass(frozen=True)
class StableRef:
    authority: str
    object_id: str
    version: str
    digest: str
    producer: str
    provenance: Tuple[Tuple[str, str], ...] = ()
    projection: Optional[str] = None

    def __post_init__(self) -> None:
        for field_name in ("authority", "object_id", "version", "producer"):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or not value
                or value != value.strip()
            ):
                raise ValueError(
                    "%s must be a trimmed non-empty string" % field_name
                )
        if not isinstance(self.digest, str) or not _valid_sha256_digest(self.digest):
            raise ValueError("digest must be sha256:<64 lowercase hex characters>")
        if not isinstance(self.provenance, tuple):
            raise ValueError("provenance must be an immutable tuple")
        normalized = []
        seen = set()
        for entry in self.provenance:
            if not isinstance(entry, tuple) or len(entry) != 2:
                raise ValueError("each provenance entry must be a two-string tuple")
            key, value = entry
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("each provenance entry must be a two-string tuple")
            if not key or not value or key != key.strip() or value != value.strip():
                raise ValueError(
                    "provenance strings must be non-empty and have no edge whitespace"
                )
            if key in seen:
                raise ValueError("duplicate provenance key: %s" % key)
            seen.add(key)
            normalized.append((key, value))
        object.__setattr__(self, "provenance", tuple(sorted(normalized)))
        if self.projection is not None:
            if (
                not isinstance(self.projection, str)
                or not self.projection
                or self.projection != self.projection.strip()
                or len(self.projection) > MAX_PROJECTION_STRING
            ):
                raise ValueError("projection must be a bounded non-empty string")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StableRef":
        if not isinstance(value, dict):
            raise ValueError("stable ref serialization must be a mapping")
        expected = {
            "authority",
            "object_id",
            "version",
            "digest",
            "producer",
            "provenance",
        }
        unknown = set(value) - (expected | {"projection"})
        missing = expected - set(value)
        if unknown or missing:
            raise ValueError("stable ref serialization has unknown or missing keys")
        provenance = value["provenance"]
        if not isinstance(provenance, dict):
            raise ValueError("serialized provenance must be a mapping")
        return cls(
            authority=value["authority"],
            object_id=value["object_id"],
            version=value["version"],
            digest=value["digest"],
            producer=value["producer"],
            provenance=tuple(provenance.items()),
            projection=value.get("projection"),
        )

    def as_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "authority": self.authority,
            "object_id": self.object_id,
            "version": self.version,
            "digest": self.digest,
            "producer": self.producer,
            "provenance": dict(self.provenance),
        }
        if self.projection is not None:
            result["projection"] = self.projection
        return result


def is_excluded_path(value: Any) -> bool:
    """Return whether a path belongs to the shared evidence exclusion set."""

    path = Path(value)
    if any(part in _EXCLUDED_PARTS for part in path.parts):
        return True
    if path.name == ".env" or path.name.startswith(".env."):
        return True
    return path.suffix.lower() in {".key", ".pem"}


def sanitize_evidence(
    value: Any,
    *,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_string: int = DEFAULT_MAX_STRING,
) -> Any:
    """Return a bounded preview with sensitive values removed."""

    if max_depth < 0 or max_items < 1 or max_string < 1:
        raise ValueError("evidence bounds must be positive")
    return _sanitize(value, 0, max_depth, max_items, max_string)


def _sanitize(value: Any, depth: int, max_depth: int, max_items: int, max_string: int) -> Any:
    if depth > max_depth:
        return "[MAX_DEPTH]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _sanitize_string(value, max_string)
    if isinstance(value, bytes):
        return "<bytes:%d>" % len(value)
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        items = list(value.items())
        for key, item in items[:max_items]:
            safe_key = _sanitize_string(str(key), max_string)
            if _sensitive_key(str(key)):
                result[safe_key] = REDACTED
            else:
                result[safe_key] = _sanitize(
                    item, depth + 1, max_depth, max_items, max_string
                )
        if len(items) > max_items:
            result["__truncated_items__"] = len(items) - max_items
        return result
    if isinstance(value, (list, tuple)):
        result = [
            _sanitize(item, depth + 1, max_depth, max_items, max_string)
            for item in value[:max_items]
        ]
        if len(value) > max_items:
            result.append("[%d items truncated]" % (len(value) - max_items))
        return result
    return "<%s>" % type(value).__name__


def _sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _redact_quoted_assignment(match: re.Match, quote: str) -> str:
    if not _sensitive_key(match.group(2)):
        return match.group(0)
    return match.group(1) + quote + REDACTED + quote


def _redact_assignment(match: re.Match) -> str:
    if not _sensitive_key(match.group(1)):
        return match.group(0)
    value = match.group(3)
    leading_space = value[: len(value) - len(value.lstrip(" \t"))]
    return match.group(1) + match.group(2) + leading_space + REDACTED


def _sanitize_string(value: str, max_string: int) -> str:
    result = _QUOTED_ASSIGNMENT_DOUBLE.sub(
        lambda match: _redact_quoted_assignment(match, '"'), value
    )
    result = _QUOTED_ASSIGNMENT_SINGLE.sub(
        lambda match: _redact_quoted_assignment(match, "'"), result
    )
    result = _AUTH_HEADER.sub(
        lambda match: match.group(1) + ": " + REDACTED, result
    )
    result = _URL_USERINFO.sub(lambda match: match.group(1) + REDACTED + "@", result)
    result = _QUERY_SECRET.sub(lambda match: match.group(1) + REDACTED, result)
    result = _ASSIGNMENT.sub(_redact_assignment, result)
    result = _BEARER.sub("Bearer " + REDACTED, result)
    if len(result) > max_string:
        return result[:max_string] + "...[truncated]"
    return result


def _valid_sha256_digest(value: str) -> bool:
    return (
        len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )
