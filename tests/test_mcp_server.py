from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from project_continuity.client import FrontClientError
from project_continuity.mcp_server import SERVER_INSTRUCTIONS, build_mcp


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
