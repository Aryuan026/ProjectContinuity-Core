"""Repository-exact Git credential helper for managed GitHub transports."""

from __future__ import annotations

import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit


MAX_INPUT_BYTES = 16_384
_TOKEN = re.compile(r"^[A-Za-z0-9_]+$")


def credential_response(operation: str, payload: bytes) -> bytes:
    """Return credentials only for the exact approved GitHub repository."""

    if operation != "get" or len(payload) > MAX_INPUT_BYTES:
        return b""
    fields: dict[str, str] = {}
    try:
        for line in payload.decode("utf-8").splitlines():
            if not line or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key in fields:
                return b""
            fields[key] = value
        approved = _repository(os.environ["PROJECT_CONTINUITY_MANAGED_GIT_REMOTE"])
        requested = (
            fields.get("protocol"),
            fields.get("host"),
            (fields.get("path") or "").removesuffix(".git").strip("/"),
        )
        if requested != approved:
            return b""
        token = _private_token(
            Path(os.environ["PROJECT_CONTINUITY_MANAGED_GIT_TOKEN_FILE"])
        )
    except (KeyError, OSError, UnicodeError, ValueError):
        return b""
    return ("username=x-access-token\npassword=%s\n\n" % token).encode("ascii")


def _repository(remote: str) -> tuple[str, str, str]:
    parsed = urlsplit(remote)
    parts = [part for part in parsed.path.removesuffix(".git").split("/") if part]
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.port is not None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or len(parts) != 2
    ):
        raise ValueError("managed GitHub repository is invalid")
    return parsed.scheme, parsed.hostname, "/".join(parts)


def _private_token(path: Path) -> str:
    if not path.is_absolute():
        raise ValueError("token path is not absolute")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        item = os.lstat(current)
        if stat.S_ISLNK(item.st_mode):
            raise ValueError("token path contains a symlink")
    item = path.stat()
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_uid != os.getuid()
        or item.st_mode & 0o077
    ):
        raise ValueError("token file is unsafe")
    token = path.read_text(encoding="ascii").strip()
    if not 20 <= len(token) <= 512 or not _TOKEN.fullmatch(token):
        raise ValueError("token is malformed")
    return token


def main() -> int:
    operation = sys.argv[1] if len(sys.argv) == 2 else ""
    payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    sys.stdout.buffer.write(credential_response(operation, payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
