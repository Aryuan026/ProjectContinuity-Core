from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path
import threading

from project_continuity.client import FrontClient
from project_continuity.cognee_adapter import CogneeCaseRecord
from project_continuity.config import load_config
from project_continuity.evidence import StableRef
from project_continuity.front import CognitionFront
from project_continuity.mcp_server import build_mcp
from project_continuity.server import create_server
from project_continuity.truth_plane import EXTERNAL_LAYERS, IntegratedTruthPlane

from conftest import write_config


TOKEN = "reader-client-token-000000000000000001"
PROMOTION_ID = "promotion:" + "a" * 64


def _reference(authority: str, layer: str) -> StableRef:
    return StableRef(
        authority=authority,
        object_id="%s:alpha:one" % authority,
        version="b" * 40,
        digest="sha256:" + sha256(layer.encode("utf-8")).hexdigest(),
        producer=authority + "@integration",
        provenance=(("project_id", "alpha"),),
        projection="reviewed",
    )


class ReadLayer:
    def __init__(self, layer: str, authority: str) -> None:
        self.layer = layer
        self.authority = authority
        self.reference = _reference(authority, layer)

    def status(self, principal_id: str, project_id: str):
        assert (principal_id, project_id) == ("reader-client", "alpha")
        return {"current": self.reference.as_dict()}

    def search(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        *,
        limit: int,
        selector: str = "",
    ):
        assert (principal_id, project_id, query) == (
            "reader-client",
            "alpha",
            "接班",
        )
        assert limit == 8
        assert selector == ""
        return [{"stable_ref": self.reference.as_dict(), "summary": self.layer}]

    def get(self, principal_id: str, project_id: str, reference: StableRef):
        assert (principal_id, project_id) == ("reader-client", "alpha")
        assert reference == self.reference
        return {"body": self.layer + " truth", "stable_ref": reference.as_dict()}

    def update(
        self,
        principal_id: str,
        project_id: str,
        operation: str,
        arguments: dict,
        *,
        expected_revision: str,
    ):
        assert (principal_id, project_id) == ("writer-client", "alpha")
        assert expected_revision == self.reference.version
        return {
            "actor": "writer-agent",
            "arguments": arguments,
            "changed": True,
            "ok": True,
            "operation": operation,
        }


class StageService:
    def __init__(self) -> None:
        self.body = "接班进入 R4a。"
        self.revision = "1" * 16

    def list_stages(self, current: str = ""):
        assert current == ""
        return {
            "currents": [
                {
                    "id": "project",
                    "stages": [
                        {
                            "id": "project.handoff",
                            "title": "Current handoff",
                            "body": self.body,
                            "revision": self.revision,
                        }
                    ],
                }
            ],
            "title": "Alpha",
        }

    def search_stages(self, query: str, *_args):
        assert query == "接班"
        return {
            "results": [
                {
                    "stage_id": "project.handoff",
                    "revision": self.revision,
                    "snippet": "接班进入 R4a。",
                }
            ]
        }

    def get_stage(self, stage_id: str):
        assert stage_id == "project.handoff"
        return {
            "body": self.body,
            "id": stage_id,
            "revision": self.revision,
        }

    def update_stage(
        self,
        stage_id: str,
        body: str,
        mode: str,
        expected_revision: str,
        actor: str,
    ):
        assert stage_id == "project.handoff"
        assert mode == "replace"
        assert expected_revision == self.revision
        assert actor == "writer-agent"
        before = self.revision
        self.body = body
        self.revision = "2" * 16
        return {
            "actor": actor,
            "before_revision": before,
            "changed": True,
            "id": stage_id,
            "revision": self.revision,
        }


class ArchiveBackend:
    async def status(self, project_id: str):
        assert project_id == "alpha"
        return {
            "dataset_name": "project-continuity-alpha",
            "partial_cases": 0,
            "ready_cases": 1,
        }

    async def search(self, project_id: str, query: str, **_arguments):
        assert (project_id, query) == ("alpha", "接班")
        return [{"promotion_id": PROMOTION_ID, "snippet": "历史接班 Case"}]

    async def lookup(self, project_id: str, promotion_id: str):
        assert (project_id, promotion_id) == ("alpha", PROMOTION_ID)
        return CogneeCaseRecord(
            project_id="alpha",
            promotion_id=PROMOTION_ID,
            data_id="case-data-one",
            envelope_digest="sha256:" + "c" * 64,
            source_digest="sha256:" + "d" * 64,
            content="历史接班 Case",
            content_digest="sha256:" + "e" * 64,
            metadata={},
        )

    async def upsert(self, _case):  # pragma: no cover - R4a is read-only
        raise AssertionError("R4a integration must not promote")


def _mcp_result(server, tool: str, arguments: dict):
    _content, structured = asyncio.run(server.call_tool(tool, arguments))
    return structured["result"]["result"]


def _credential_directory(config, root: Path) -> Path:
    root.mkdir(mode=0o700)
    for principal in config.principals:
        path = root / (principal.principal_id + ".token")
        path.write_text(_token(principal.principal_id) + "\n", encoding="ascii")
        path.chmod(0o600)
    return root


def _token(principal_id: str) -> str:
    return TOKEN if principal_id == "reader-client" else (
        principal_id + "-token-000000000000000000000000"
    )


def test_five_tool_mcp_reaches_the_real_integrated_six_layer_front(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path / "runtime"))
    stage_service = StageService()
    authorities = ("graphify", "openspec", "teamai", "github")
    plane = IntegratedTruthPlane(
        tuple(
            ReadLayer(layer, authority)
            for layer, authority in zip(EXTERNAL_LAYERS, authorities)
        )
    )
    front = CognitionFront(
        config,
        service_factory=lambda _path: stage_service,
        cognee_backend=ArchiveBackend(),
        truth_plane=plane,
    )
    credentials = _credential_directory(config, tmp_path / "credentials")
    http_server = create_server(config, credentials, port=0, front=front)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:
        port = http_server.server_address[1]
        mcp = build_mcp(
            FrontClient(
                "http://127.0.0.1:%d/v1/invoke" % port,
                TOKEN,
                timeout=90,
            )
        )
        listed = _mcp_result(mcp, "list", {"project_id": "alpha"})
        searched = _mcp_result(
            mcp, "search", {"project_id": "alpha", "query": "接班"}
        )
        code_ref = listed["truth_plane"]["layers"]["code"]["current"]
        fetched = _mcp_result(
            mcp,
            "get",
            {"project_id": "alpha", "resource_ref": code_ref},
        )
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=3)

    expected_layers = ["current", "history", *EXTERNAL_LAYERS]
    assert listed["coverage"] == {
        "consulted": expected_layers,
        "matched": expected_layers,
        "unavailable": {},
        "failed": {},
        "complete": True,
    }
    assert listed["history_archive"]["ready_cases"] == 1
    assert searched["coverage"] == {
        "consulted": expected_layers,
        "matched": expected_layers,
        "unavailable": {},
        "failed": {},
        "complete": True,
    }
    assert searched["results"]["history"][0]["promotion_id"] == PROMOTION_ID
    assert fetched["layer"] == "code"
    assert fetched["result"]["body"] == "code truth"


def test_same_five_tool_update_preserves_stage_cas_and_routes_authority_write(
    tmp_path: Path,
) -> None:
    config = load_config(write_config(tmp_path / "runtime"))
    stage_service = StageService()
    authorities = ("graphify", "openspec", "teamai", "github")
    plane = IntegratedTruthPlane(
        tuple(
            ReadLayer(layer, authority)
            for layer, authority in zip(EXTERNAL_LAYERS, authorities)
        )
    )
    front = CognitionFront(
        config,
        service_factory=lambda _path: stage_service,
        cognee_backend=ArchiveBackend(),
        truth_plane=plane,
    )
    credentials = _credential_directory(config, tmp_path / "credentials")
    http_server = create_server(config, credentials, port=0, front=front)
    thread = threading.Thread(target=http_server.serve_forever, daemon=True)
    thread.start()
    try:
        port = http_server.server_address[1]
        mcp = build_mcp(
            FrontClient(
                "http://127.0.0.1:%d/v1/invoke" % port,
                _token("writer-client"),
                timeout=90,
            )
        )
        stage = _mcp_result(
            mcp,
            "update",
            {
                "project_id": "alpha",
                "stage_id": "project.handoff",
                "body": "接班进入 R4b。",
                "expected_revision": "1" * 16,
            },
        )
        decision = _mcp_result(
            mcp,
            "update",
            {
                "project_id": "alpha",
                "target": "decisions",
                "operation": "prepare_change",
                "parameters": {"change_id": "r4b-parity"},
                "expected_revision": "b" * 40,
            },
        )
    finally:
        http_server.shutdown()
        http_server.server_close()
        thread.join(timeout=3)

    assert stage == {
        "actor": "writer-agent",
        "before_revision": "1" * 16,
        "changed": True,
        "id": "project.handoff",
        "revision": "2" * 16,
    }
    assert decision == {
        "layer": "decisions",
        "result": {
            "actor": "writer-agent",
            "arguments": {"change_id": "r4b-parity"},
            "changed": True,
            "ok": True,
            "operation": "prepare_change",
        },
    }
