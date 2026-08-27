"""Private credential loading and a bounded client for the existing front."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import stat
from typing import Any, Dict, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


MAX_RESPONSE_BYTES = 10 * 1024 * 1024
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{32,256}$")


class FrontClientError(RuntimeError):
    """A bounded, secret-free receipt for a failed front call."""

    def __init__(self, receipt: Mapping[str, Any]) -> None:
        self.receipt = dict(receipt)
        super().__init__(json.dumps(self.receipt, ensure_ascii=False, sort_keys=True))


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        raise FrontClientError({"ok": False, "error": "front_redirect_rejected"})


_FRONT_OPENER = build_opener(_RejectRedirects())


def _open_front(request: Request, *, timeout: float) -> Any:
    return _FRONT_OPENER.open(request, timeout=timeout)


def path_has_symlink(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def load_private_token_file(path: Path) -> str:
    """Read one owner-only token without accepting aliases or multiline data."""

    token_path = Path(path)
    if (
        not token_path.is_absolute()
        or token_path != Path(os.path.abspath(token_path))
        or token_path.is_symlink()
        or path_has_symlink(token_path)
        or not token_path.is_file()
    ):
        raise FrontClientError({"ok": False, "error": "unsafe_token_file"})
    try:
        token_stat = token_path.stat()
        raw = token_path.read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise FrontClientError({"ok": False, "error": "unreadable_token_file"}) from exc
    if not stat.S_ISREG(token_stat.st_mode) or token_stat.st_mode & 0o077:
        raise FrontClientError({"ok": False, "error": "unsafe_token_file"})
    if hasattr(os, "geteuid") and token_stat.st_uid != os.geteuid():
        raise FrontClientError({"ok": False, "error": "unsafe_token_file"})
    token = raw[:-1] if raw.endswith("\n") else raw
    if "\n" in token or "\r" in token or not TOKEN_PATTERN.fullmatch(token):
        raise FrontClientError({"ok": False, "error": "malformed_token_file"})
    return token


class FrontClient:
    """Forward one MCP tool call to the sole authenticated cognition front."""

    def __init__(self, endpoint: str, token: str, *, timeout: float = 20.0) -> None:
        self.endpoint = _validated_endpoint(endpoint)
        if not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token):
            raise FrontClientError({"ok": False, "error": "malformed_token"})
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
            raise FrontClientError({"ok": False, "error": "invalid_timeout"})
        if timeout <= 0 or timeout > 60:
            raise FrontClientError({"ok": False, "error": "invalid_timeout"})
        self._token = token
        self.timeout = float(timeout)

    def invoke(
        self, tool: str, project_id: str, arguments: Mapping[str, Any]
    ) -> Any:
        payload = json.dumps(
            {"tool": tool, "project_id": project_id, "arguments": dict(arguments)},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        request = Request(
            self.endpoint,
            data=payload,
            method="POST",
            headers={
                "Authorization": "Bearer " + self._token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with _open_front(request, timeout=self.timeout) as response:
                status = response.status
                result = _read_json_response(response)
        except HTTPError as exc:
            try:
                result = _read_json_response(exc)
            except FrontClientError:
                raise
            except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as parse_error:
                raise FrontClientError(
                    {"ok": False, "error": "invalid_front_response"}
                ) from parse_error
            raise FrontClientError(_error_receipt(exc.code, result)) from exc
        except (TimeoutError, socket.timeout) as exc:
            raise FrontClientError({"ok": False, "error": "front_timeout"}) from exc
        except URLError as exc:
            reason = exc.reason
            error = (
                "front_timeout"
                if isinstance(reason, (TimeoutError, socket.timeout))
                else "front_unavailable"
            )
            raise FrontClientError({"ok": False, "error": error}) from exc
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise FrontClientError({"ok": False, "error": "invalid_front_response"}) from exc
        if status != 200 or not isinstance(result, dict) or result.get("ok") is not True:
            raise FrontClientError(_error_receipt(status, result))
        if set(result) != {"ok", "result"}:
            raise FrontClientError({"ok": False, "error": "invalid_front_response"})
        return result["result"]


def _validated_endpoint(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or any(character.isspace() or ord(character) < 32 for character in value)
    ):
        raise FrontClientError({"ok": False, "error": "invalid_front_endpoint"})
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise FrontClientError({"ok": False, "error": "invalid_front_endpoint"}) from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path != "/v1/invoke"
    ):
        raise FrontClientError({"ok": False, "error": "invalid_front_endpoint"})
    if parsed.scheme == "http" and hostname != "127.0.0.1":
        raise FrontClientError({"ok": False, "error": "insecure_front_endpoint"})
    return value


def _read_json_response(response: Any) -> Any:
    content = response.read(MAX_RESPONSE_BYTES + 1)
    if len(content) > MAX_RESPONSE_BYTES:
        raise FrontClientError({"ok": False, "error": "front_response_too_large"})
    return json.loads(content.decode("utf-8"))


def _error_receipt(status: int, value: Any) -> Dict[str, Any]:
    receipt: Dict[str, Any] = {"ok": False, "error": "front_error", "status": status}
    if isinstance(value, dict):
        for key in ("error", "capability", "detail"):
            item = value.get(key)
            if isinstance(item, str) and 0 < len(item) <= 2_000:
                receipt[key] = item
    return receipt
