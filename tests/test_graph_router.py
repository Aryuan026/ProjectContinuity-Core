import json
import os
from pathlib import Path
import subprocess

import pytest

from project_continuity.auth import AuthorizationError
from project_continuity.graph_router import (
    GraphArtifactError,
    GraphQueryError,
    GraphQueryRouter,
    GraphRegistry,
    GraphRegistryConflict,
    GraphRouterError,
)


COMMIT_A = "a" * 40
COMMIT_B = "b" * 40
OVERLAY_A = "sha256:" + "c" * 64
OVERLAY_B = "sha256:" + "d" * 64
NOW = "2026-08-24T21:30:00+08:00"


def _write_artifact(output_root: Path, revision: str, *, label: str = "alpha") -> None:
    graph_root = output_root / "graphify-out"
    graph_root.mkdir(parents=True, exist_ok=True)
    (graph_root / "graph.json").write_text(
        json.dumps(
            {
                "built_at_commit": revision,
                "directed": False,
                "graph": {},
                "links": [
                    {
                        "relation": "calls",
                        "source": "entry",
                        "target": "worker",
                    }
                ],
                "multigraph": False,
                "nodes": [
                    {"id": "entry", "label": label, "source_file": "src/app.py"},
                    {"id": "worker", "label": "worker", "source_file": "src/app.py"},
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (graph_root / "manifest.json").write_text(
        json.dumps({"src/app.py": {"ast_hash": "one", "semantic_hash": ""}}),
        encoding="utf-8",
    )


def _write_fake_graphify(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import json
import os
import sys

if sys.argv[1] == "--version":
    print("graphify 0.9.48")
    raise SystemExit(0)

graph = sys.argv[sys.argv.index("--graph") + 1]
payload = json.loads(open(graph, encoding="utf-8").read())
try:
    cache = os.path.join(os.path.dirname(graph), "cache")
    os.makedirs(cache, exist_ok=True)
    open(os.path.join(cache, "last_query_stamp"), "w", encoding="utf-8").write("mutated")
    stamp = "wrote"
except OSError:
    stamp = "blocked"
print(json.dumps({
    "budget": sys.argv[sys.argv.index("--budget") + 1],
    "built_at_commit": payload["built_at_commit"],
    "disable": os.environ.get("GRAPHIFY_QUERY_LOG_DISABLE"),
    "enable": os.environ.get("GRAPHIFY_QUERY_LOG_ENABLE"),
    "log": os.environ.get("GRAPHIFY_QUERY_LOG"),
    "openai_key": os.environ.get("OPENAI_API_KEY"),
    "question": sys.argv[2],
    "stamp": stamp,
}, sort_keys=True))
""",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _write_path_echo_graphify(path: Path, *, fail: bool = False) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import sys

if sys.argv[1] == "--version":
    print("graphify 0.9.48")
    raise SystemExit(0)

graph = sys.argv[sys.argv.index("--graph") + 1]
if %r:
    print("query failed for " + graph, file=sys.stderr)
    raise SystemExit(2)
print("Graph: " + graph)
print("answer")
"""
        % fail,
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _add_learning_sidecar(
    output_root: Path, kind: str, target_root: Path
) -> Path:
    sidecar = output_root / "graphify-out" / ".graphify_learning.json"
    payload = json.dumps(
        {
            "nodes": {
                "entry": {
                    "code_fingerprint": "",
                    "score": 1,
                    "status": "preferred",
                    "uses": 1,
                }
            },
            "version": 1,
        }
    )
    if kind == "file":
        sidecar.write_text(payload, encoding="utf-8")
    else:
        target = target_root / (output_root.name + "-learning.json")
        target.write_text(payload, encoding="utf-8")
        sidecar.symlink_to(target)
    return sidecar


def test_committed_and_two_same_base_overlays_never_collide(config) -> None:
    registry = GraphRegistry(config)
    committed_root = registry.committed_output_root(
        "alpha", COMMIT_A, "alpha-commit-a", create=True
    )
    overlay_a_root = registry.overlay_output_root(
        "alpha", COMMIT_A, OVERLAY_A, "alpha-overlay-a", create=True
    )
    overlay_b_root = registry.overlay_output_root(
        "alpha", COMMIT_A, OVERLAY_B, "alpha-overlay-b", create=True
    )
    assert len({committed_root, overlay_a_root, overlay_b_root}) == 3
    _write_artifact(committed_root, COMMIT_A, label="committed")
    _write_artifact(overlay_a_root, COMMIT_A, label="overlay-a")
    _write_artifact(overlay_b_root, COMMIT_A, label="overlay-b")

    committed = registry.register_committed(
        project_id="alpha",
        commit_sha=COMMIT_A,
        snapshot_id="alpha-commit-a",
        generated_at=NOW,
    )
    overlay_a = registry.register_overlay(
        project_id="alpha",
        base_sha=COMMIT_A,
        overlay_digest=OVERLAY_A,
        snapshot_id="alpha-overlay-a",
        evidence_time=NOW,
    )
    overlay_b = registry.register_overlay(
        project_id="alpha",
        base_sha=COMMIT_A,
        overlay_digest=OVERLAY_B,
        snapshot_id="alpha-overlay-b",
        evidence_time=NOW,
    )

    state = registry.readback()["projects"]["alpha"]
    assert state["current_canonical"] == committed.snapshot_id
    assert state["working_overlay"] == overlay_b.snapshot_id
    assert overlay_a.snapshot_id in state["artifacts"]
    assert registry.resolve("alpha", snapshot_id=overlay_a.snapshot_id) == overlay_a
    assert registry.resolve("alpha", selector="current_canonical") == committed
    assert registry.resolve("alpha", selector="working_overlay") == overlay_b
    assert registry.root.stat().st_mode & 0o077 == 0
    assert registry.registry_path.stat().st_mode & 0o077 == 0


def test_new_commit_advances_only_canonical_and_old_snapshot_remains(config) -> None:
    registry = GraphRegistry(config)
    for commit, snapshot in ((COMMIT_A, "alpha-a"), (COMMIT_B, "alpha-b")):
        root = registry.committed_output_root("alpha", commit, snapshot, create=True)
        _write_artifact(root, commit, label=snapshot)
        registry.register_committed(
            project_id="alpha",
            commit_sha=commit,
            snapshot_id=snapshot,
            generated_at=NOW,
        )

    assert registry.resolve("alpha", selector="current_canonical").snapshot_id == "alpha-b"
    assert registry.resolve("alpha", snapshot_id="alpha-a").commit_sha == COMMIT_A
    assert registry.readback()["projects"]["alpha"]["working_overlay"] is None


@pytest.mark.parametrize("failure", ("stale", "empty", "coverage"))
def test_failed_graph_never_moves_current_pointer(config, failure: str) -> None:
    registry = GraphRegistry(config)
    old_root = registry.committed_output_root("alpha", COMMIT_A, "healthy", create=True)
    _write_artifact(old_root, COMMIT_A)
    registry.register_committed(
        project_id="alpha",
        commit_sha=COMMIT_A,
        snapshot_id="healthy",
        generated_at=NOW,
    )
    broken_root = registry.committed_output_root("alpha", COMMIT_B, "broken", create=True)
    _write_artifact(broken_root, COMMIT_B)
    graph_path = broken_root / "graphify-out" / "graph.json"
    payload = json.loads(graph_path.read_text(encoding="utf-8"))
    if failure == "stale":
        payload["built_at_commit"] = COMMIT_A
        graph_path.write_text(json.dumps(payload), encoding="utf-8")
    elif failure == "empty":
        payload["nodes"] = []
        graph_path.write_text(json.dumps(payload), encoding="utf-8")
    else:
        (broken_root / "graphify-out" / "manifest.json").write_text(
            json.dumps({"src/other.py": {"ast_hash": "x"}}), encoding="utf-8"
        )

    with pytest.raises(GraphArtifactError):
        registry.register_committed(
            project_id="alpha",
            commit_sha=COMMIT_B,
            snapshot_id="broken",
            generated_at=NOW,
        )
    assert registry.resolve("alpha", selector="current_canonical").snapshot_id == "healthy"


def test_immutable_snapshot_refuses_identity_reuse(config) -> None:
    registry = GraphRegistry(config)
    first = registry.committed_output_root("alpha", COMMIT_A, "same", create=True)
    _write_artifact(first, COMMIT_A)
    registry.register_committed(
        project_id="alpha", commit_sha=COMMIT_A, snapshot_id="same", generated_at=NOW
    )
    second = registry.committed_output_root("alpha", COMMIT_B, "same", create=True)
    _write_artifact(second, COMMIT_B)
    with pytest.raises(GraphRegistryConflict, match="snapshot_id"):
        registry.register_committed(
            project_id="alpha", commit_sha=COMMIT_B, snapshot_id="same", generated_at=NOW
        )


def test_two_clients_query_same_snapshot_and_cross_project_acl_is_denied(
    config, monkeypatch, tmp_path: Path
) -> None:
    registry = GraphRegistry(config)
    root = registry.committed_output_root("alpha", COMMIT_A, "shared", create=True)
    _write_artifact(root, COMMIT_A)
    registry.register_committed(
        project_id="alpha", commit_sha=COMMIT_A, snapshot_id="shared", generated_at=NOW
    )
    executable = _write_fake_graphify(tmp_path / "graphify")
    router = GraphQueryRouter(config, executable)
    monkeypatch.setenv("GRAPHIFY_QUERY_LOG", str(tmp_path / "must-not-exist.log"))
    monkeypatch.setenv("GRAPHIFY_QUERY_LOG_ENABLE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-reach-query")

    reader = router.query(
        principal_id="reader-client",
        project_id="alpha",
        question="入口调用了谁？",
        selector="current_canonical",
    )
    writer = router.query(
        principal_id="writer-client",
        project_id="alpha",
        question="入口调用了谁？",
        snapshot_id="shared",
    )
    reader_result = json.loads(reader["result"])
    writer_result = json.loads(writer["result"])
    assert reader["snapshot_id"] == writer["snapshot_id"] == "shared"
    assert reader["stable_ref"] == writer["stable_ref"]
    assert reader_result["built_at_commit"] == writer_result["built_at_commit"] == COMMIT_A
    assert reader_result["disable"] == "1"
    assert reader_result["budget"] == "2000"
    assert reader_result["enable"] is None
    assert reader_result["log"] is None
    assert reader_result["openai_key"] is None
    assert reader_result["stamp"] == "wrote"
    assert not (tmp_path / "must-not-exist.log").exists()

    with pytest.raises(AuthorizationError, match="no role"):
        router.query(
            principal_id="reader-client",
            project_id="beta",
            question="越权查询",
            snapshot_id="shared",
        )


def test_query_results_and_errors_hide_managed_filesystem_paths(
    config, monkeypatch, tmp_path: Path
) -> None:
    registry = GraphRegistry(config)
    root = registry.committed_output_root("alpha", COMMIT_A, "shared", create=True)
    _write_artifact(root, COMMIT_A)
    registry.register_committed(
        project_id="alpha", commit_sha=COMMIT_A, snapshot_id="shared", generated_at=NOW
    )
    graph_path = registry.graph_path(
        registry.resolve("alpha", snapshot_id="shared")
    )
    success_router = GraphQueryRouter(
        config, _write_path_echo_graphify(tmp_path / "graphify-success")
    )
    success = success_router.query(
        principal_id="reader-client",
        project_id="alpha",
        question="入口调用了谁？",
        snapshot_id="shared",
    )
    assert success["result"].splitlines()[0] == "Graph: graph://alpha/shared"
    assert str(config.paths.data_root) not in success["result"]
    assert str(graph_path) not in success["result"]

    failing_router = GraphQueryRouter(
        config, _write_path_echo_graphify(tmp_path / "graphify-failure", fail=True)
    )
    with pytest.raises(GraphQueryError) as captured:
        failing_router.query(
            principal_id="reader-client",
            project_id="alpha",
            question="入口调用了谁？",
            snapshot_id="shared",
        )
    detail = str(captured.value)
    assert "query failed for graph://alpha/shared" in detail
    assert str(config.paths.data_root) not in detail
    assert str(graph_path) not in detail

    def time_out(command, **_kwargs):
        raise subprocess.TimeoutExpired(command, timeout=1)

    monkeypatch.setattr("project_continuity.graph_router.subprocess.run", time_out)
    with pytest.raises(GraphQueryError, match="timed out for graph://alpha/shared") as timed:
        success_router.query(
            principal_id="reader-client",
            project_id="alpha",
            question="入口调用了谁？",
            snapshot_id="shared",
        )
    assert str(config.paths.data_root) not in str(timed.value)
    assert str(graph_path) not in str(timed.value)


@pytest.mark.parametrize("sidecar_kind", ("file", "symlink"))
def test_learning_sidecar_is_rejected_at_register_resolve_and_query(
    config, monkeypatch, sidecar_kind: str, tmp_path: Path
) -> None:
    registry = GraphRegistry(config)
    healthy_root = registry.committed_output_root(
        "alpha", COMMIT_A, "healthy", create=True
    )
    _write_artifact(healthy_root, COMMIT_A)
    registry.register_committed(
        project_id="alpha",
        commit_sha=COMMIT_A,
        snapshot_id="healthy",
        generated_at=NOW,
    )

    candidate_root = registry.committed_output_root(
        "alpha", COMMIT_B, "candidate", create=True
    )
    _write_artifact(candidate_root, COMMIT_B)
    sidecar = _add_learning_sidecar(candidate_root, sidecar_kind, tmp_path)
    with pytest.raises(GraphArtifactError, match="learning sidecar"):
        registry.register_committed(
            project_id="alpha",
            commit_sha=COMMIT_B,
            snapshot_id="candidate",
            generated_at=NOW,
        )
    assert registry.readback()["projects"]["alpha"]["current_canonical"] == "healthy"

    sidecar.unlink()
    artifact = registry.register_committed(
        project_id="alpha",
        commit_sha=COMMIT_B,
        snapshot_id="candidate",
        generated_at=NOW,
    )
    router = GraphQueryRouter(config, _write_fake_graphify(tmp_path / "graphify"))
    sidecar = _add_learning_sidecar(candidate_root, sidecar_kind, tmp_path)
    state_before = registry.readback()

    with pytest.raises(GraphArtifactError, match="learning sidecar"):
        registry.resolve("alpha", snapshot_id="candidate")

    def query_must_not_run(*_args, **_kwargs):
        raise AssertionError("Graphify query ran with a learning sidecar")

    monkeypatch.setattr(
        "project_continuity.graph_router.subprocess.run", query_must_not_run
    )
    with pytest.raises(GraphArtifactError, match="learning sidecar"):
        router.query(
            principal_id="reader-client",
            project_id="alpha",
            question="入口调用了谁？",
            snapshot_id="candidate",
        )
    assert registry.readback() == state_before
    stored = state_before["projects"]["alpha"]["artifacts"]["candidate"]
    assert stored["stable_ref"] == artifact.stable_ref.as_dict()
    assert "learning=" not in str(stored)


def test_registered_artifact_tamper_is_not_silently_queried(config) -> None:
    registry = GraphRegistry(config)
    root = registry.committed_output_root("alpha", COMMIT_A, "tamper", create=True)
    _write_artifact(root, COMMIT_A)
    registry.register_committed(
        project_id="alpha", commit_sha=COMMIT_A, snapshot_id="tamper", generated_at=NOW
    )
    graph_path = root / "graphify-out" / "graph.json"
    graph_path.write_text(graph_path.read_text(encoding="utf-8") + " ", encoding="utf-8")

    with pytest.raises(GraphArtifactError, match="changed"):
        registry.resolve("alpha", snapshot_id="tamper")


def test_query_router_refuses_a_different_graphify_version(config, tmp_path: Path) -> None:
    executable = tmp_path / "graphify-old"
    executable.write_text(
        "#!/bin/sh\nprintf 'graphify 0.9.47\\n'\n", encoding="utf-8"
    )
    executable.chmod(0o700)

    with pytest.raises(GraphQueryError, match="exact graphify 0.9.48"):
        GraphQueryRouter(config, executable)


def test_registry_write_failure_keeps_previous_pointer(config, monkeypatch) -> None:
    registry = GraphRegistry(config)
    old_root = registry.committed_output_root("alpha", COMMIT_A, "old", create=True)
    _write_artifact(old_root, COMMIT_A)
    registry.register_committed(
        project_id="alpha", commit_sha=COMMIT_A, snapshot_id="old", generated_at=NOW
    )
    new_root = registry.committed_output_root("alpha", COMMIT_B, "new", create=True)
    _write_artifact(new_root, COMMIT_B)

    def fail_write(*_args, **_kwargs):
        raise OSError("injected write failure")

    monkeypatch.setattr("project_continuity.graph_router._write_json_atomic", fail_write)
    with pytest.raises(GraphRouterError, match="registry write failed"):
        registry.register_committed(
            project_id="alpha", commit_sha=COMMIT_B, snapshot_id="new", generated_at=NOW
        )

    state = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    assert state["projects"]["alpha"]["current_canonical"] == "old"
    assert "new" not in state["projects"]["alpha"]["artifacts"]


def test_registry_rejects_duplicate_keys_and_unsafe_permissions(config) -> None:
    registry = GraphRegistry(config)
    registry.root.mkdir(parents=True, mode=0o700)
    registry.root.chmod(0o700)
    registry.registry_path.write_text(
        '{"schema_version":1,"schema_version":1,"projects":{}}',
        encoding="utf-8",
    )
    registry.registry_path.chmod(0o600)
    with pytest.raises(GraphArtifactError, match="duplicate JSON key"):
        registry.readback()

    registry.registry_path.write_text(
        '{"schema_version":1,"projects":{}}', encoding="utf-8"
    )
    registry.registry_path.chmod(0o666)
    with pytest.raises(GraphArtifactError, match="unsafe permissions"):
        registry.readback()
