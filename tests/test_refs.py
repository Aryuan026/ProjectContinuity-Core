from hashlib import sha256

import pytest

from project_continuity.refs import (
    graph_artifact_ref,
    openspec_decision_ref,
    teamai_reviewed_ref,
)


COMMIT = "a" * 40
BASE = "b" * 40
OVERLAY = "sha256:" + "c" * 64


def _digest(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def test_graph_refs_separate_committed_and_overlay_identity() -> None:
    committed = graph_artifact_ref(
        project_id="alpha",
        snapshot_id="alpha-a",
        repo_url="https://github.com/example/alpha",
        graph_digest=_digest(b"committed graph"),
        manifest_digest=_digest(b"committed manifest"),
        artifact_kind="committed",
        commit_sha=COMMIT,
    )
    overlay = graph_artifact_ref(
        project_id="alpha",
        snapshot_id="alpha-working",
        repo_url="https://github.com/example/alpha",
        graph_digest=_digest(b"working graph"),
        manifest_digest=_digest(b"working manifest"),
        artifact_kind="overlay",
        base_sha=BASE,
        overlay_digest=OVERLAY,
    )

    assert committed.authority == overlay.authority == "graphify"
    assert committed.version == COMMIT
    assert overlay.version == BASE + "+" + OVERLAY
    assert committed.projection == "canonical-eligible"
    assert overlay.projection == "diagnostic-overlay"
    assert committed.object_id != overlay.object_id


def test_openspec_ref_is_same_after_git_clone_readback() -> None:
    content = b"# Decision\n\nUse the exact-SHA graph.\n"
    machine_a = openspec_decision_ref(
        store_id="project-continuity-specs",
        decision_id="graph-truth-plane",
        revision="d" * 40,
        artifact_digest=_digest(content),
        state="current",
        repo_url="https://github.com/example/project-continuity-specs",
    )
    machine_b = openspec_decision_ref(
        store_id="project-continuity-specs",
        decision_id="graph-truth-plane",
        revision="d" * 40,
        artifact_digest=_digest(bytes(content)),
        state="current",
        repo_url="https://github.com/example/project-continuity-specs",
    )

    assert machine_a == machine_b
    assert machine_a.object_id == (
        "decision:project-continuity-specs:graph-truth-plane"
    )
    assert machine_a.producer == "openspec@1.10.0"


@pytest.mark.parametrize("state", ("accepted", "archived", "unknown"))
def test_openspec_ref_refuses_to_invent_donor_states(state: str) -> None:
    with pytest.raises(ValueError, match="state"):
        openspec_decision_ref(
            store_id="specs",
            decision_id="one",
            revision="d" * 40,
            artifact_digest=_digest(b"decision"),
            state=state,
        )


@pytest.mark.parametrize(
    "repo_url",
    [
        "https://token@github.com/example/specs",
        "https://github.com/example/specs?token=secret",
        "https://github.com/example/specs#fragment",
        "https://github.com\\example/specs",
        "https://github.com\n/example/specs",
        "https://github.com",
    ],
)
def test_stable_refs_reuse_strict_repository_url_boundary(repo_url: str) -> None:
    with pytest.raises(ValueError, match="repository URL"):
        openspec_decision_ref(
            store_id="specs",
            decision_id="one",
            revision="d" * 40,
            artifact_digest=_digest(b"decision"),
            state="current",
            repo_url=repo_url,
        )
    with pytest.raises(ValueError, match="repository URL"):
        graph_artifact_ref(
            project_id="alpha",
            snapshot_id="alpha-a",
            repo_url=repo_url,
            graph_digest=_digest(b"graph"),
            manifest_digest=_digest(b"manifest"),
            artifact_kind="committed",
            commit_sha=COMMIT,
        )


def test_two_clients_read_the_same_reviewed_teamai_reference() -> None:
    content = b"# Workstream\n\nOwner: writer-agent\n"
    fields = {
        "project_id": "alpha",
        "object_kind": "workstream",
        "object_id": "f2-canary",
        "revision": "e" * 40,
        "artifact_digest": _digest(content),
        "repo_url": "https://github.com/example/project-continuity-team",
        "relative_path": ".teamai/docs/workstreams/writer-agent/f2-canary.md",
        "actor_id": "writer-agent",
        "endpoint_id": "writer-client",
        "pull_request": 12,
    }

    local_codex = teamai_reviewed_ref(**fields)
    second_client = teamai_reviewed_ref(**dict(fields))

    assert local_codex == second_client
    assert local_codex.authority == "teamai"
    assert local_codex.producer == "teamai-cli@0.20.0"
    assert local_codex.projection == "reviewed-shared-collaboration"
    assert dict(local_codex.provenance)["review_state"] == "merged"


@pytest.mark.parametrize(
    "field,value,expected",
    [
        ("object_kind", "transcript", "object_kind"),
        ("relative_path", "sessions/writer-agent/raw.json", "relative_path"),
        ("relative_path", ".teamai/docs/../secrets.txt", "relative_path"),
        ("relative_path", "/tmp/minutes.md", "relative_path"),
        ("pull_request", 0, "pull_request"),
        ("pull_request", True, "pull_request"),
        ("revision", "draft", "revision"),
    ],
)
def test_teamai_ref_requires_reviewed_merged_git_identity(
    field: str, value, expected: str
) -> None:
    fields = {
        "project_id": "alpha",
        "object_kind": "minutes",
        "object_id": "meeting-one",
        "revision": "e" * 40,
        "artifact_digest": _digest(b"reviewed minutes"),
        "repo_url": "https://github.com/example/project-continuity-team",
        "relative_path": ".teamai/docs/minutes/meeting-one.md",
        "actor_id": "writer-agent",
        "endpoint_id": "writer-client",
        "pull_request": 12,
    }
    fields[field] = value

    with pytest.raises(ValueError, match=expected):
        teamai_reviewed_ref(**fields)
