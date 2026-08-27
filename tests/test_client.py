from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
from pathlib import Path
import socket
from threading import Thread
from urllib.error import HTTPError, URLError

import pytest

from project_continuity.client import (
    FrontClient,
    FrontClientError,
    load_private_token_file,
)


TOKEN = "local-reader-token-00000000000000000001"


class Response:
    def __init__(self, payload, *, status=200):
        self.status = status
        self._content = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size=-1):
        return self._content[:size]


def _token_file(path: Path) -> Path:
    path.write_text(TOKEN + "\n", encoding="ascii")
    path.chmod(0o600)
    return path


@contextmanager
def _serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _redirect_handler(status: int, location: str, seen: list[str]):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.headers.get("Authorization", ""))
            self.send_response(status)
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, *_args):
            pass

    return Handler


def _capture_handler(seen: list[str]):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.headers.get("Authorization", ""))
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *_args):
            pass

    return Handler


def test_private_token_file_is_read_without_exposing_its_value(tmp_path: Path) -> None:
    token_path = _token_file(tmp_path / "reader.token")
    assert load_private_token_file(token_path) == TOKEN

    token_path.chmod(0o644)
    with pytest.raises(FrontClientError, match="unsafe_token_file") as error:
        load_private_token_file(token_path)
    assert TOKEN not in str(error.value)


def test_token_file_rejects_valid_and_broken_symlink_chains(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    _token_file(outside / "reader.token")
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    with pytest.raises(FrontClientError, match="unsafe_token_file"):
        load_private_token_file(alias / "reader.token")

    broken = tmp_path / "broken"
    broken.symlink_to(tmp_path / "absent", target_is_directory=True)
    with pytest.raises(FrontClientError, match="unsafe_token_file"):
        load_private_token_file(broken / "reader.token")


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com/v1/invoke",
        "https://user:pass@example.com/v1/invoke",
        "https://example.com/v1/invoke?token=secret",
        "https://example.com/v1/invoke#fragment",
        "https://example.com/other",
        "https://example.com:bad/v1/invoke",
        "https://example.com\\v1\\invoke",
    ],
)
def test_front_client_rejects_unsafe_endpoints(endpoint: str) -> None:
    with pytest.raises(FrontClientError):
        FrontClient(endpoint, TOKEN)


def test_front_client_sends_the_exact_envelope_and_returns_result(monkeypatch) -> None:
    seen = {}

    def fake_open(request, *, timeout):
        seen["url"] = request.full_url
        seen["authorization"] = request.headers["Authorization"]
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return Response({"ok": True, "result": {"revision": "a" * 16}})

    monkeypatch.setattr("project_continuity.client._open_front", fake_open)
    client = FrontClient("http://127.0.0.1:8766/v1/invoke", TOKEN, timeout=7)
    result = client.invoke("get", "alpha", {"stage_id": "project.handoff"})

    assert result == {"revision": "a" * 16}
    assert seen["url"] == "http://127.0.0.1:8766/v1/invoke"
    assert seen["authorization"] == "Bearer " + TOKEN
    assert seen["body"] == {
        "tool": "get",
        "project_id": "alpha",
        "arguments": {"stage_id": "project.handoff"},
    }
    assert seen["timeout"] == 7


def test_front_client_preserves_typed_error_without_token_or_endpoint(monkeypatch) -> None:
    payload = {
        "ok": False,
        "error": "capability_unavailable",
        "capability": "case_semantic_search",
    }

    def fail(*_args, **_kwargs):
        raise HTTPError(
            "http://127.0.0.1:8766/v1/invoke",
            503,
            "Unavailable",
            {},
            BytesIO(json.dumps(payload).encode("utf-8")),
        )

    monkeypatch.setattr("project_continuity.client._open_front", fail)
    client = FrontClient("http://127.0.0.1:8766/v1/invoke", TOKEN)
    with pytest.raises(FrontClientError) as error:
        client.invoke("search", "alpha", {"scope": "cases", "query": "故障"})
    assert error.value.receipt == {**payload, "status": 503}
    assert TOKEN not in str(error.value)
    assert "127.0.0.1" not in str(error.value)


def test_front_client_closes_malformed_http_error_body(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise HTTPError(
            "http://127.0.0.1:8766/v1/invoke",
            503,
            "Unavailable",
            {},
            BytesIO(b"not-json /private/path"),
        )

    monkeypatch.setattr("project_continuity.client._open_front", fail)
    client = FrontClient("http://127.0.0.1:8766/v1/invoke", TOKEN)
    with pytest.raises(FrontClientError) as error:
        client.invoke("list", "alpha", {})
    assert error.value.receipt == {"ok": False, "error": "invalid_front_response"}
    assert "private" not in str(error.value)


@pytest.mark.parametrize(
    "failure,expected",
    [
        (TimeoutError(), "front_timeout"),
        (URLError(socket.timeout()), "front_timeout"),
        (URLError(OSError("private path")), "front_unavailable"),
    ],
)
def test_front_client_network_failures_are_bounded(monkeypatch, failure, expected) -> None:
    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr("project_continuity.client._open_front", fail)
    client = FrontClient("https://continuity.example/mcp/v1/invoke".replace("/mcp", ""), TOKEN)
    with pytest.raises(FrontClientError) as error:
        client.invoke("list", "alpha", {})
    assert error.value.receipt == {"ok": False, "error": expected}
    assert "private path" not in str(error.value)


@pytest.mark.parametrize("status", [301, 302, 303])
def test_front_client_rejects_redirect_without_forwarding_bearer(status: int) -> None:
    redirected_requests: list[str] = []
    initial_requests: list[str] = []
    with _serve(_capture_handler(redirected_requests)) as target_port:
        location = (
            f"http://127.0.0.1:{target_port}/private/path?token=redirect-secret"
        )
        with _serve(_redirect_handler(status, location, initial_requests)) as source_port:
            client = FrontClient(
                f"http://127.0.0.1:{source_port}/v1/invoke", TOKEN
            )
            with pytest.raises(FrontClientError) as error:
                client.invoke("list", "alpha", {})

    assert initial_requests == ["Bearer " + TOKEN]
    assert redirected_requests == []
    assert error.value.receipt == {
        "ok": False,
        "error": "front_redirect_rejected",
    }
    assert TOKEN not in str(error.value)
    assert location not in str(error.value)
    assert "private/path" not in str(error.value)
