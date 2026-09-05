from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import pytest

from project_continuity.evidence import StableRef
from project_continuity.graph_router import GraphRegistry
from project_continuity.github_resolver import GitHubResolverUnavailable
import project_continuity.truth_plane as truth_plane
from project_continuity.truth_plane import (
    EXTERNAL_LAYERS,
    GraphifyLayer,
    IntegratedTruthPlane,
    LayerUnavailable,
    TruthPlaneError,
    build_installed_truth_plane,
)
from project_continuity.truth_bindings import BINDINGS_RELATIVE_PATH


def _ref(authority: str, object_id: str) -> StableRef:
    return StableRef(
        authority=authority,
        object_id=object_id,
        version="a" * 40,
        digest="sha256:" + "b" * 64,
        producer=authority + "@test",
        provenance=(("project_id", "alpha"),),
        projection="reviewed",
    )


@dataclass
class FakeLayer:
    layer: str
    authority: str
    mode: str = "ready"

    def status(self, principal_id, project_id):
        assert principal_id == "reader-client"
        assert project_id == "alpha"
        if self.mode == "unavailable":
            raise LayerUnavailable(self.layer + "_not_installed")
        if self.mode == "failed":
            raise RuntimeError("private path /srv/must-not-leak")
        return {"current": _ref(self.authority, self.layer + ":alpha:one").as_dict()}

    def search(self, principal_id, project_id, query, *, limit, selector=""):
        self.status(principal_id, project_id)
        assert query == "为什么改"
        assert limit == 4
        assert selector in {"", "working_overlay"}
        return [{"summary": self.layer + " hit"}]

    def get(self, principal_id, project_id, reference):
        self.status(principal_id, project_id)
        return {"stable_ref": reference.as_dict(), "body": self.layer + " body"}

    def update(
        self,
        principal_id,
        project_id,
        operation,
        arguments,
        *,
        expected_revision,
    ):
        self.status(principal_id, project_id)
        return {
            "arguments": dict(arguments),
            "expected_revision": expected_revision,
            "operation": operation,
        }


def test_list_reports_every_external_layer_and_does_not_hide_absence() -> None:
    plane = IntegratedTruthPlane(
        (
            FakeLayer("code", "graphify"),
            FakeLayer("decisions", "openspec", "unavailable"),
            FakeLayer("collaboration", "teamai", "failed"),
        )
    )

    result = plane.list_layers("reader-client", "alpha")

    assert tuple(result["layers"]) == EXTERNAL_LAYERS
    assert result["layers"]["code"]["available"] is True
    assert result["layers"]["delivery"] == {
        "available": False,
        "reason": "not_configured",
    }
    assert result["coverage"] == {
        "consulted": ["code", "decisions", "collaboration"],
        "matched": ["code"],
        "unavailable": {
            "decisions": "decisions_not_installed",
            "delivery": "not_configured",
        },
        "failed": {"collaboration": "failed"},
        "complete": False,
    }
    assert "/srv/" not in str(result)


def test_federated_search_keeps_layer_partition_and_complete_coverage() -> None:
    plane = IntegratedTruthPlane(
        tuple(
            FakeLayer(layer, authority)
            for layer, authority in zip(
                EXTERNAL_LAYERS,
                ("graphify", "openspec", "teamai", "github"),
            )
        )
    )

    result = plane.search(
        "reader-client",
        "alpha",
        "为什么改",
        scopes=EXTERNAL_LAYERS,
        limit=4,
    )

    assert result["coverage"]["complete"] is True
    assert result["coverage"]["consulted"] == list(EXTERNAL_LAYERS)
    assert result["coverage"]["matched"] == list(EXTERNAL_LAYERS)
    assert result["results"]["code"] == [{"summary": "code hit"}]


def test_exact_get_routes_by_stable_ref_authority_and_rejects_unknown_owner() -> None:
    plane = IntegratedTruthPlane((FakeLayer("code", "graphify"),))
    reference = _ref("graphify", "graph:alpha:snapshot-one")

    result = plane.get("reader-client", "alpha", reference)
    assert result["layer"] == "code"
    assert result["result"]["stable_ref"] == reference.as_dict()

    with pytest.raises(LayerUnavailable, match="authority_not_configured"):
        plane.get("reader-client", "alpha", _ref("openspec", "decision:a:b"))


def test_duplicate_layer_or_authority_cannot_shadow_an_existing_owner() -> None:
    with pytest.raises(ValueError, match="unique"):
        IntegratedTruthPlane(
            (FakeLayer("code", "graphify"), FakeLayer("code", "other"))
        )
    with pytest.raises(ValueError, match="unique"):
        IntegratedTruthPlane(
            (FakeLayer("code", "shared"), FakeLayer("decisions", "shared"))
        )


def test_adapter_failure_is_bounded_before_leaving_the_truth_plane() -> None:
    class Broken(FakeLayer):
        def get(self, *_args, **_kwargs):
            raise RuntimeError("credential=/srv/private/token")

    plane = IntegratedTruthPlane((Broken("delivery", "github"),))
    with pytest.raises(TruthPlaneError, match="authority_failed") as caught:
        plane.get("reader-client", "alpha", _ref("github", "commit:alpha:one"))
    assert "/srv/" not in str(caught.value)


def test_typed_update_routes_to_exact_owner_without_growing_the_tool_surface() -> None:
    plane = IntegratedTruthPlane((FakeLayer("decisions", "openspec"),))

    result = plane.update(
        "reader-client",
        "alpha",
        "decisions",
        "prepare_change",
        {"change_id": "one"},
        expected_revision="a" * 40,
    )

    assert result == {
        "layer": "decisions",
        "result": {
            "arguments": {"change_id": "one"},
            "expected_revision": "a" * 40,
            "operation": "prepare_change",
        },
    }


def test_graphify_layer_consumes_an_existing_exact_artifact(config, tmp_path: Path) -> None:
    revision = "a" * 40
    registry = GraphRegistry(config)
    root = registry.committed_output_root(
        "alpha", revision, "alpha-reviewed", create=True
    )
    graph_root = root / "graphify-out"
    graph_root.mkdir(parents=True)
    (graph_root / "graph.json").write_text(
        json.dumps(
            {
                "built_at_commit": revision,
                "directed": False,
                "graph": {},
                "links": [{"relation": "calls", "source": "entry", "target": "worker"}],
                "multigraph": False,
                "nodes": [
                    {"id": "entry", "label": "entry", "source_file": "src/app.py"},
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
    registered = registry.register_committed(
        project_id="alpha",
        commit_sha=revision,
        snapshot_id="alpha-reviewed",
        generated_at="2026-09-05T12:00:00+08:00",
    )
    executable = tmp_path / "graphify"
    executable.write_text(
        """#!/usr/bin/env python3
import json, sys
if sys.argv[1] == '--version':
    print('graphify 0.9.48')
else:
    print(json.dumps({'question': sys.argv[2], 'answer': 'entry calls worker'}))
""",
        encoding="utf-8",
    )
    executable.chmod(0o700)
    layer = GraphifyLayer(config, executable)

    status = layer.status("reader-client", "alpha")
    found = layer.search("reader-client", "alpha", "谁调用 worker", limit=8)
    fetched = layer.get(
        "reader-client", "alpha", StableRef.from_dict(status["current"])
    )

    assert status["current"] == registered.stable_ref.as_dict()
    assert json.loads(found[0]["result"])["question"] == "谁调用 worker"
    assert fetched["snapshot_id"] == "alpha-reviewed"


def test_installed_builder_discovers_present_layers_and_types_absence(
    config, tmp_path: Path, monkeypatch
) -> None:
    release_root = tmp_path / "release"
    executable_root = release_root / ".venv/bin"
    graphify = executable_root / "graphify"
    openspec = (
        release_root
        / "vendor/openspec-runtime/node_modules/@fission-ai/openspec/bin/openspec.js"
    )
    teamai = release_root / "vendor/teamai-runtime/node_modules/teamai-cli/dist/index.js"
    teamai_literal_recall = (
        release_root
        / "vendor/teamai-runtime/project-continuity-literal-recall.mjs"
    )
    node = release_root / "vendor/node"
    for path in (graphify, openspec, teamai, teamai_literal_recall, node):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("runtime\n", encoding="utf-8")

    bindings = config.paths.data_root / BINDINGS_RELATIVE_PATH
    bindings.parent.mkdir(parents=True, exist_ok=True)
    bindings.write_text(
        json.dumps(
            {
                "projects": {
                    "alpha": {
                        "openspec": {
                            "repo_url": "https://github.com/example/alpha-spec",
                            "store_id": "alpha-spec",
                        }
                    }
                },
                "schema_version": 1,
            }
        ),
        encoding="utf-8",
    )
    bindings.chmod(0o600)

    monkeypatch.setattr(truth_plane.sys, "executable", str(executable_root / "python"))
    monkeypatch.setattr(truth_plane.sys, "prefix", str(release_root / ".venv"))
    monkeypatch.setenv("PROJECT_CONTINUITY_NODE_BIN", str(node))
    monkeypatch.setattr(
        truth_plane,
        "GraphifyLayer",
        lambda *_args: FakeLayer("code", "graphify"),
    )
    monkeypatch.setattr(
        truth_plane,
        "OpenSpecLayer",
        lambda *_args: FakeLayer("decisions", "openspec"),
    )

    def github_absent():
        raise GitHubResolverUnavailable("github_token_file_absent")

    monkeypatch.setattr(
        truth_plane.GitHubAuthorityResolver,
        "from_environment",
        github_absent,
    )

    result = build_installed_truth_plane(config).list_layers(
        "reader-client", "alpha"
    )

    assert result["layers"]["code"]["available"] is True
    assert result["layers"]["decisions"]["available"] is True
    assert result["layers"]["collaboration"] == {
        "available": False,
        "reason": "teamai_binding_absent",
    }
    assert result["layers"]["delivery"] == {
        "available": False,
        "reason": "github_token_file_absent",
    }
    assert result["coverage"]["complete"] is False
