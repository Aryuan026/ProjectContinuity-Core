"""Official-SDK stdio MCP adapter over the sole ProjectContinuity front."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations

from .client import FrontClient, FrontClientError, load_private_token_file


DEFAULT_ENDPOINT = "http://127.0.0.1:8766/v1/invoke"
SERVER_INSTRUCTIONS = (
    "ProjectContinuity preserves current project handoff and reviewed engineering "
    "history. Start with list, then get project.handoff. Search only when the "
    "question needs more context. Update the handoff with its exact revision after "
    "durable work. Promote only an already-reviewed exact revision with provenance. "
    "Stages and Cases reference—but never replace—OpenSpec, Graphify, TeamAI, GitHub, "
    "external event, or personal-memory authorities. Exactly five tools are exposed."
)


def build_mcp(client: FrontClient) -> FastMCP:
    server = FastMCP("ProjectContinuity", instructions=SERVER_INSTRUCTIONS)

    @server.tool(
        name="list",
        description=(
            "Orient yourself in a project before acting. List its current Stages; "
            "then read project.handoff instead of asking the human to replay history."
        ),
        annotations=_read_annotations("List project Stages"),
        structured_output=True,
    )
    def list_stages(project_id: str, current: str = "") -> Dict[str, Any]:
        arguments = {"current": current} if current else {}
        return _invoke(client, "list", project_id, arguments)

    @server.tool(
        name="search",
        description=(
            "Follow a specific question across current Stages or reviewed historical "
            "Cases. Omit match for the no-vector defaults; Case keyword search works "
            "without an embedding provider. Use match=semantic only when vector "
            "retrieval is intentionally configured."
        ),
        annotations=_read_annotations("Search project continuity"),
        structured_output=True,
    )
    def search(
        project_id: str,
        query: str,
        scope: str = "stages",
        match: str = "",
        current: str = "",
        stage_id: str = "",
        context: int = 2,
        limit: int = 8,
        case_sensitive: bool = False,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {"query": query}
        if scope != "stages":
            arguments["scope"] = scope
        if match:
            arguments["match"] = match
        if current:
            arguments["current"] = current
        if stage_id:
            arguments["stage_id"] = stage_id
        if context != 2:
            arguments["context"] = context
        if limit != 8:
            arguments["limit"] = limit
        if case_sensitive:
            arguments["case_sensitive"] = True
        return _invoke(client, "search", project_id, arguments)

    @server.tool(
        name="get",
        description=(
            "Read one exact current Stage or reviewed Case. Start a resumed project "
            "with stage_id=project.handoff; use promotion_id when a historical Case "
            "identity is already known."
        ),
        annotations=_read_annotations("Get an exact Stage or Case"),
        structured_output=True,
    )
    def get(
        project_id: str, stage_id: str = "", promotion_id: str = ""
    ) -> Dict[str, Any]:
        arguments = {
            key: value
            for key, value in (
                ("stage_id", stage_id),
                ("promotion_id", promotion_id),
            )
            if value
        }
        return _invoke(client, "get", project_id, arguments)

    @server.tool(
        name="update",
        description=(
            "Keep the project handoff truthful after durable work or a verified "
            "change. This routine agent-owned CAS write uses the exact revision from "
            "get; do not request repeated confirmation when the configured role "
            "already authorizes the update."
        ),
        annotations=ToolAnnotations(
            title="Update a Stage with exact CAS",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def update(
        project_id: str,
        stage_id: str,
        body: str,
        expected_revision: str,
        mode: str = "replace",
    ) -> Dict[str, Any]:
        return _invoke(
            client,
            "update",
            project_id,
            {
                "stage_id": stage_id,
                "body": body,
                "expected_revision": expected_revision,
                "mode": mode,
            },
        )

    @server.tool(
        name="promote",
        description=(
            "Archive an already-reviewed exact Stage revision as a durable engineering "
            "Case. This is rare, explicit, and provenance-bound—not a substitute for "
            "routine handoff updates. Reuse the same idempotency key when recovering."
        ),
        annotations=ToolAnnotations(
            title="Promote a reviewed engineering Case",
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
        structured_output=True,
    )
    def promote(
        project_id: str,
        stage_id: str,
        source_revision: str,
        idempotency_key: str,
        provenance: List[Dict[str, Any]],
        review_authority: Dict[str, Any],
        promotion_kind: str = "engineering_case",
        schema_version: int = 1,
        corrects: Optional[List[str]] = None,
        supersedes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        arguments: Dict[str, Any] = {
            "stage_id": stage_id,
            "source_revision": source_revision,
            "idempotency_key": idempotency_key,
            "provenance": provenance,
            "review_authority": review_authority,
        }
        if promotion_kind != "engineering_case":
            arguments["promotion_kind"] = promotion_kind
        if schema_version != 1:
            arguments["schema_version"] = schema_version
        if corrects:
            arguments["corrects"] = corrects
        if supersedes:
            arguments["supersedes"] = supersedes
        return _invoke(client, "promote", project_id, arguments)

    return server


def _read_annotations(title: str) -> ToolAnnotations:
    return ToolAnnotations(
        title=title,
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )


def _invoke(
    client: FrontClient,
    tool: str,
    project_id: str,
    arguments: Mapping[str, Any],
) -> Dict[str, Any]:
    try:
        return {"result": client.invoke(tool, project_id, arguments)}
    except FrontClientError as exc:
        raise ToolError(str(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="project-continuity-mcp")
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--token-file", required=True, type=Path)
    parser.add_argument("--timeout", default=20.0, type=float)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        client = FrontClient(
            args.endpoint,
            load_private_token_file(args.token_file),
            timeout=args.timeout,
        )
    except FrontClientError as exc:
        raise SystemExit("project-continuity-mcp: %s" % exc) from exc
    build_mcp(client).run(transport="stdio")


if __name__ == "__main__":
    main()
