#!/usr/bin/env python3
"""Exercise the public Linux full-front and fresh wheel MCP arrival contract."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
from datetime import timedelta
from hashlib import sha256
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
from urllib.request import urlopen

from mcp import ClientSession, StdioServerParameters, stdio_client


PROJECT_ID = "r4c-cold-start"
STAGE_ID = "project.handoff"
IDEMPOTENCY_KEY = "r4c-public-provider-free-v1"
PROVIDER_ENV = (
    "EMBEDDING_PROVIDER",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS",
    "LLM_API_KEY",
    "OPENAI_API_KEY",
)
COGNEE_RUNTIME_ENV = (
    "PYTHON_DOTENV_DISABLED",
    "GRAPH_DATABASE_PROVIDER",
    "GRAPH_DATABASE_SUBPROCESS_ENABLED",
    "DATA_ROOT_DIRECTORY",
    "SYSTEM_ROOT_DIRECTORY",
    "CACHE_ROOT_DIRECTORY",
    "COGNEE_LOGS_DIR",
)


def _sha(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


def _reference(name: str) -> dict:
    return {
        "authority": "github",
        "object_id": "github:r4c-cold-start:%s" % name,
        "version": "c" * 40,
        "digest": _sha(name),
        "producer": "r4c-linux-cold-start",
        "provenance": {"scope": "public-ci"},
        "projection": "reviewed",
    }


def _write_inputs(root: Path) -> tuple[Path, Path, Path]:
    private = root / "private"
    install = root / "install"
    data = root / "data"
    state = root / "state"
    credentials = root / "credentials"
    for directory in (private, install, data, state, credentials):
        directory.mkdir(mode=0o700)
    config = private / "config.toml"
    config.write_text(
        """schema_version = 1

[paths]
install_root = "{install}"
data_root = "{data}"
state_root = "{state}"

[[projects]]
id = "{project_id}"
repo_url = "https://github.com/example/r4c-cold-start"

[[principals]]
id = "cold-start-agent"
actor = "cold-start-agent"
[principals.roles]
{project_id} = "promoter"
""".format(
            install=install,
            data=data,
            state=state,
            project_id=PROJECT_ID,
        ),
        encoding="utf-8",
    )
    config.chmod(0o600)

    token_file = credentials / "cold-start-agent.token"
    token_file.write_text(secrets.token_urlsafe(32) + "\n", encoding="ascii")
    token_file.chmod(0o600)

    stage = data / "projects" / PROJECT_ID / "turritopsis" / "stages.json"
    stage.parent.mkdir(parents=True, mode=0o700)
    stage.write_text(
        json.dumps(
            {
                "title": "R4c cold start",
                "subtitle": "Fresh public package arrival",
                "version": 1,
                "currents": [
                    {
                        "id": "project",
                        "name": "Project",
                        "blurb": "Current project cognition",
                        "stages": [
                            {
                                "id": STAGE_ID,
                                "title": "Current handoff",
                                "body": (
                                    "# R4c provider-free cold start\n\n"
                                    "A reviewed synthetic Case proves the native keyword "
                                    "archive, exact replay, and client-neutral arrival."
                                ),
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return config, credentials, token_file


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _front_environment(root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for name in (*PROVIDER_ENV, *COGNEE_RUNTIME_ENV):
        environment.pop(name, None)
    home = root / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    environment.update(
        {
            "HOME": str(home),
            "XDG_CACHE_HOME": str(root / "xdg-cache"),
            "XDG_CONFIG_HOME": str(root / "xdg-config"),
            "PROJECT_CONTINUITY_CASE_ARCHIVE_MODE": "keyword",
        }
    )
    return environment


@contextmanager
def _front(command: Path, config: Path, credentials: Path, root: Path, port: int):
    error_log = root / "front.stderr.log"
    error_stream = error_log.open("w+", encoding="utf-8")
    process = subprocess.Popen(
        [
            str(command),
            "--config",
            str(config),
            "--credentials-dir",
            str(credentials),
            "--port",
            str(port),
        ],
        env=_front_environment(root),
        stdout=subprocess.DEVNULL,
        stderr=error_stream,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                error_stream.flush()
                error_stream.seek(0)
                detail = error_stream.read()[-4_000:]
                raise RuntimeError(
                    "front exited before health readback: %s\n%s"
                    % (process.returncode, detail)
                )
            try:
                with urlopen(
                    "http://127.0.0.1:%d/health" % port, timeout=1
                ) as response:
                    health = json.load(response)
                if health.get("status") == "front_ready":
                    break
            except OSError:
                time.sleep(0.2)
        else:
            raise RuntimeError("front did not become healthy")
        yield
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        error_stream.close()


def _success(result):
    if result.isError:
        detail = " ".join(
            str(getattr(item, "text", "")) for item in result.content
        )
        raise AssertionError("MCP call failed: %s" % detail)
    structured = result.structuredContent
    if not isinstance(structured, dict):
        raise AssertionError("MCP success omitted structured output")
    return structured["result"]["result"]


def _error_text(result) -> str:
    if not result.isError:
        raise AssertionError("MCP call unexpectedly succeeded")
    return " ".join(str(getattr(item, "text", "")) for item in result.content)


async def _arrival(
    mcp_command: Path,
    endpoint: str,
    token_file: Path,
    *,
    expected: dict | None = None,
) -> dict:
    parameters = StdioServerParameters(
        command=str(mcp_command),
        args=[
            "--endpoint",
            endpoint,
            "--token-file",
            str(token_file),
            "--timeout",
            "90",
        ],
    )
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(
            read_stream,
            write_stream,
            read_timeout_seconds=timedelta(seconds=120),
        ) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = [tool.name for tool in tools.tools]
            if names != ["list", "search", "get", "update", "promote"]:
                raise AssertionError("fresh MCP did not expose exactly five tools")

            listed = _success(await session.call_tool("list", {"project_id": PROJECT_ID}))
            stage = _success(
                await session.call_tool(
                    "get", {"project_id": PROJECT_ID, "stage_id": STAGE_ID}
                )
            )
            if listed["coverage"]["failed"]:
                raise AssertionError("fresh list reported failed authority layers")
            if not set(("code", "decisions", "collaboration", "delivery")).issubset(
                listed["coverage"]["unavailable"]
            ):
                raise AssertionError("optional authority absence was not reported")

            request = {
                "project_id": PROJECT_ID,
                "stage_id": STAGE_ID,
                "source_revision": stage["revision"],
                "idempotency_key": IDEMPOTENCY_KEY,
                "provenance": [_reference("source")],
                "review_authority": _reference("review"),
            }
            receipt = _success(await session.call_tool("promote", request))
            archive = _success(
                await session.call_tool("list", {"project_id": PROJECT_ID})
            )["history_archive"]
            found = _success(
                await session.call_tool(
                    "search",
                    {
                        "project_id": PROJECT_ID,
                        "query": "provider-free cold start",
                        "scope": "cases",
                        "match": "keyword",
                    },
                )
            )
            case = _success(
                await session.call_tool(
                    "get",
                    {
                        "project_id": PROJECT_ID,
                        "promotion_id": receipt["promotion_id"],
                    },
                )
            )
            semantic = await session.call_tool(
                "search",
                {
                    "project_id": PROJECT_ID,
                    "query": "semantic-only question",
                    "scope": "cases",
                    "match": "semantic",
                },
            )
            if "capability_unavailable" not in _error_text(semantic):
                raise AssertionError("unconfigured semantic search did not return typed HOLD")
            if not any(
                item["promotion_id"] == receipt["promotion_id"]
                for item in found
            ):
                raise AssertionError("keyword search did not find the promoted Case")
            if case["promotion_id"] != receipt["promotion_id"] or not case["ready"]:
                raise AssertionError("exact Case readback did not preserve ready identity")
            if archive != {
                "available": True,
                "archive_mode": "keyword",
                "dataset_name": "project-continuity-%s" % PROJECT_ID,
                "partial_cases": 0,
                "ready_cases": 1,
            }:
                raise AssertionError("provider-free archive status did not become ready")
            if case["data_id"] != receipt["backend_data_id"]:
                raise AssertionError("Case and receipt Data identities diverged")
            stage_after = _success(
                await session.call_tool(
                    "get", {"project_id": PROJECT_ID, "stage_id": STAGE_ID}
                )
            )
            if stage["revision"] != stage_after["revision"]:
                raise AssertionError("promotion mutated the source Stage")
            if expected is not None and receipt != expected:
                raise AssertionError("restart/replay changed the promotion receipt")
            return receipt


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--front-command", required=True, type=Path)
    parser.add_argument("--mcp-command", required=True, type=Path)
    parser.add_argument("--skill", required=True, type=Path)
    args = parser.parse_args()

    skill = args.skill.read_text(encoding="utf-8")
    for instruction in (
        "Call `list`",
        'get(stage_id="project.handoff")',
        'search(scope="cases", match="keyword")',
        "capability_unavailable",
    ):
        if instruction not in skill:
            raise SystemExit("packaged Skill omitted arrival instruction: %s" % instruction)

    with tempfile.TemporaryDirectory(prefix="project-continuity-r4c-") as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        config, credentials, token_file = _write_inputs(root)
        port = _free_port()
        endpoint = "http://127.0.0.1:%d/v1/invoke" % port
        with _front(args.front_command, config, credentials, root, port):
            first = asyncio.run(_arrival(args.mcp_command, endpoint, token_file))
        with _front(args.front_command, config, credentials, root, port):
            second = asyncio.run(
                _arrival(args.mcp_command, endpoint, token_file, expected=first)
            )

    print(
        json.dumps(
            {
                "archive_mode": "keyword",
                "case_ready": True,
                "five_tools": True,
                "fresh_wheel_mcp": True,
                "promotion_id": second["promotion_id"],
                "restart_replay_identity": "unchanged",
                "semantic_without_provider": "capability_unavailable",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
