from __future__ import annotations

import asyncio
from io import BytesIO
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from project_continuity.authority_layers import READ_DEADLINE_SECONDS
from project_continuity.client import FrontClient, FrontClientError
from project_continuity.mcp_server import SERVER_INSTRUCTIONS, build_mcp, build_parser
from project_continuity.server import INTEGRATED_HISTORY_TIMEOUT_SECONDS
from project_continuity.truth_plane import EXTERNAL_LAYERS


class FakeClient:
    def __init__(self) -> None:
        self.calls = []
        self.error = None

    def invoke(self, tool, project_id, arguments):
        if self.error is not None:
            raise self.error
        self.calls.append((tool, project_id, arguments))
        return {"tool": tool, "project_id": project_id, "arguments": arguments}


def _run(awaitable):
    return asyncio.run(awaitable)


def test_mcp_exposes_exactly_five_progressive_tools_with_truthful_annotations() -> None:
    tools = _run(build_mcp(FakeClient()).list_tools())
    assert [tool.name for tool in tools] == ["list", "search", "get", "update", "promote"]
    assert [tool.annotations.readOnlyHint for tool in tools] == [
        True,
        True,
        True,
        False,
        False,
    ]
    assert all(tool.annotations.openWorldHint is False for tool in tools)
    assert tools[-1].annotations.idempotentHint is True
    assert "Start with list" in SERVER_INSTRUCTIONS[:512]
    assert "Exactly five tools" in SERVER_INSTRUCTIONS


def test_mcp_routes_the_native_five_tools_without_actor_or_principal_claims() -> None:
    client = FakeClient()
    server = build_mcp(client)
    reference = {
        "authority": "github",
        "object_id": "commit:abc",
        "version": "abc",
        "digest": "sha256:" + "a" * 64,
        "producer": "github",
        "provenance": {},
    }
    calls = [
        ("list", {"project_id": "alpha"}),
        (
            "search",
            {
                "project_id": "alpha",
                "query": "故障",
                "scope": "cases",
                "match": "keyword",
            },
        ),
        ("get", {"project_id": "alpha", "stage_id": "project.handoff"}),
        (
            "update",
            {
                "project_id": "alpha",
                "stage_id": "project.handoff",
                "body": "next",
                "expected_revision": "a" * 16,
            },
        ),
        (
            "promote",
            {
                "project_id": "alpha",
                "stage_id": "project.handoff",
                "source_revision": "a" * 16,
                "idempotency_key": "case-1",
                "provenance": [reference],
                "review_authority": reference,
            },
        ),
    ]
    for name, arguments in calls:
        _run(server.call_tool(name, arguments))

    assert [call[0] for call in client.calls] == [
        "list",
        "search",
        "get",
        "update",
        "promote",
    ]
    assert client.calls[1][2] == {
        "query": "故障",
        "scope": "cases",
        "match": "keyword",
    }
    assert all("actor" not in call[2] and "principal_id" not in call[2] for call in client.calls)


def test_mcp_defaults_to_integrated_search_and_forwards_exact_resource_ref() -> None:
    client = FakeClient()
    server = build_mcp(client)
    reference = {
        "authority": "graphify",
        "object_id": "graph:alpha:snapshot-one",
        "version": "a" * 40,
        "digest": "sha256:" + "b" * 64,
        "producer": "graphify@0.9.48",
        "provenance": {"project_id": "alpha"},
    }

    _run(server.call_tool("search", {"project_id": "alpha", "query": "调用链"}))
    _run(
        server.call_tool(
            "get", {"project_id": "alpha", "resource_ref": reference}
        )
    )

    assert client.calls == [
        ("search", "alpha", {"query": "调用链"}),
        ("get", "alpha", {"resource_ref": reference}),
    ]


def test_mcp_surfaces_only_the_typed_front_receipt() -> None:
    client = FakeClient()
    client.error = FrontClientError(
        {
            "ok": False,
            "error": "capability_unavailable",
            "capability": "case_semantic_search",
        }
    )
    with pytest.raises(ToolError) as caught:
        _run(
            build_mcp(client).call_tool(
                "search",
                {"project_id": "alpha", "query": "故障", "scope": "cases"},
            )
        )
    assert "capability_unavailable" in str(caught.value)
    assert "provider path" not in str(caught.value)


@pytest.mark.parametrize(
    ("status", "backend_error"),
    [(503, "backend_busy"), (504, "backend_timeout")],
)
def test_mcp_tool_error_preserves_archive_in_progress_state(
    monkeypatch, status: int, backend_error: str
) -> None:
    payload = {
        "ok": False,
        "error": backend_error,
        "capability": "case_archive",
        "operation_state": "in_progress",
        "private": "/srv/private/archive",
    }

    def fail(*_args, **_kwargs):
        raise HTTPError(
            "http://127.0.0.1:8766/v1/invoke",
            status,
            "Unavailable",
            {},
            BytesIO(json.dumps(payload).encode("utf-8")),
        )

    monkeypatch.setattr("project_continuity.client._open_front", fail)
    client = FrontClient(
        "http://127.0.0.1:8766/v1/invoke",
        "local-reader-token-00000000000000000001",
        timeout=90,
    )
    with pytest.raises(ToolError) as caught:
        _run(
            build_mcp(client).call_tool(
                "get",
                {"project_id": "alpha", "promotion_id": "promotion:" + "a" * 64},
            )
        )

    receipt = str(caught.value)
    assert '"error": "%s"' % backend_error in receipt
    assert '"operation_state": "in_progress"' in receipt
    assert "/srv/" not in receipt


def test_mcp_default_transport_timeout_outlives_archive_deadline() -> None:
    args = build_parser().parse_args(["--token-file", str(Path("reader.token"))])
    assert args.timeout == 90.0
    assert args.timeout > (
        len(EXTERNAL_LAYERS) * READ_DEADLINE_SECONDS
        + INTEGRATED_HISTORY_TIMEOUT_SECONDS
    )
