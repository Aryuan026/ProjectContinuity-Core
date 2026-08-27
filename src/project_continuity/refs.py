"""Stable references to donor-owned Graphify, OpenSpec, and TeamAI truth."""

from __future__ import annotations

import re
from typing import Optional, Tuple

from .config import _repository_url as _validated_repository_url
from .evidence import StableRef


GRAPHIFY_PRODUCER = "graphify@0.9.48"
OPENSPEC_PRODUCER = "openspec@1.10.0"
TEAMAI_PRODUCER = "teamai-cli@0.20.0"
OPENSPEC_STATES = frozenset({"proposal", "current", "superseded", "rejected"})
TEAMAI_REVIEWED_KINDS = frozenset(
    {"assignment", "learning", "minutes", "workstream"}
)
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_GIT_REVISION = re.compile(r"^[0-9a-f]{40,64}$")
_OVERLAY_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SHA256_DIGEST = _OVERLAY_DIGEST


def graph_artifact_ref(
    *,
    project_id: str,
    snapshot_id: str,
    repo_url: str,
    graph_digest: str,
    manifest_digest: str,
    artifact_kind: str,
    commit_sha: Optional[str] = None,
    base_sha: Optional[str] = None,
    overlay_digest: Optional[str] = None,
) -> StableRef:
    """Describe one immutable Graphify artifact without owning graph content."""

    _identifier(project_id, "project_id")
    _identifier(snapshot_id, "snapshot_id")
    repo_url = _validated_repository_url(repo_url, "repo_url")
    _sha256_digest(graph_digest, "graph_digest")
    _sha256_digest(manifest_digest, "manifest_digest")
    if artifact_kind == "committed":
        revision = _git_revision(commit_sha, "commit_sha")
        if base_sha is not None or overlay_digest is not None:
            raise ValueError("committed graph identity cannot include overlay fields")
        identity: Tuple[Tuple[str, str], ...] = (
            ("artifact_kind", "committed"),
            ("commit_sha", revision),
            ("manifest_digest", manifest_digest),
            ("repo_url", repo_url),
        )
        projection = "canonical-eligible"
    elif artifact_kind == "overlay":
        revision = _git_revision(base_sha, "base_sha")
        digest = _overlay_digest(overlay_digest)
        if commit_sha is not None:
            raise ValueError("overlay graph identity cannot include commit_sha")
        identity = (
            ("artifact_kind", "overlay"),
            ("base_sha", revision),
            ("manifest_digest", manifest_digest),
            ("overlay_digest", digest),
            ("repo_url", repo_url),
        )
        revision = "%s+%s" % (revision, digest)
        projection = "diagnostic-overlay"
    else:
        raise ValueError("artifact_kind must be committed or overlay")
    return StableRef(
        authority="graphify",
        object_id="graph:%s:%s" % (project_id, snapshot_id),
        version=revision,
        digest=graph_digest,
        producer=GRAPHIFY_PRODUCER,
        provenance=identity,
        projection=projection,
    )


def openspec_decision_ref(
    *,
    store_id: str,
    decision_id: str,
    revision: str,
    artifact_digest: str,
    state: str,
    repo_url: Optional[str] = None,
) -> StableRef:
    """Reference a reviewed OpenSpec artifact; Git/OpenSpec remain its owner."""

    _identifier(store_id, "store_id")
    _identifier(decision_id, "decision_id")
    git_revision = _git_revision(revision, "revision")
    if state not in OPENSPEC_STATES:
        raise ValueError("state must be proposal, current, superseded, or rejected")
    provenance = [("state", state), ("store_id", store_id)]
    if repo_url is not None:
        provenance.append(
            ("repo_url", _validated_repository_url(repo_url, "repo_url"))
        )
    return StableRef(
        authority="openspec",
        object_id="decision:%s:%s" % (store_id, decision_id),
        version=git_revision,
        digest=artifact_digest,
        producer=OPENSPEC_PRODUCER,
        provenance=tuple(provenance),
        projection=state,
    )


def teamai_reviewed_ref(
    *,
    project_id: str,
    object_kind: str,
    object_id: str,
    revision: str,
    artifact_digest: str,
    repo_url: str,
    relative_path: str,
    actor_id: str,
    endpoint_id: str,
    pull_request: int,
) -> StableRef:
    """Reference reviewed TeamAI knowledge without owning its Git or content."""

    _identifier(project_id, "project_id")
    if object_kind not in TEAMAI_REVIEWED_KINDS:
        raise ValueError("object_kind must be reviewed TeamAI collaboration content")
    _identifier(object_id, "object_id")
    git_revision = _git_revision(revision, "revision")
    _sha256_digest(artifact_digest, "artifact_digest")
    remote = _validated_repository_url(repo_url, "repo_url")
    path = _teamai_relative_path(relative_path)
    _identifier(actor_id, "actor_id")
    _identifier(endpoint_id, "endpoint_id")
    if type(pull_request) is not int or pull_request < 1:
        raise ValueError("pull_request must be a positive integer")
    return StableRef(
        authority="teamai",
        object_id="collaboration:%s:%s:%s"
        % (project_id, object_kind, object_id),
        version=git_revision,
        digest=artifact_digest,
        producer=TEAMAI_PRODUCER,
        provenance=(
            ("actor_id", actor_id),
            ("endpoint_id", endpoint_id),
            ("merge_revision", git_revision),
            ("pull_request", str(pull_request)),
            ("relative_path", path),
            ("repo_url", remote),
            ("review_state", "merged"),
        ),
        projection="reviewed-shared-collaboration",
    )


def _identifier(value: object, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("%s must be a stable identifier" % field)
    return value


def _git_revision(value: object, field: str) -> str:
    if not isinstance(value, str) or not _GIT_REVISION.fullmatch(value):
        raise ValueError("%s must be a full lowercase Git object id" % field)
    return value


def _overlay_digest(value: object) -> str:
    if not isinstance(value, str) or not _OVERLAY_DIGEST.fullmatch(value):
        raise ValueError("overlay_digest must be sha256:<64 lowercase hex>")
    return value


def _sha256_digest(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256_DIGEST.fullmatch(value):
        raise ValueError("%s must be sha256:<64 lowercase hex>" % field)
    return value


def _teamai_relative_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 500
        or value.startswith("/")
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("relative_path must be a bounded relative POSIX path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("relative_path must be a bounded relative POSIX path")
    allowed_prefixes = (
        ".teamai/agents/",
        ".teamai/docs/",
        ".teamai/learnings/",
        ".teamai/rules/",
        ".teamai/skills/",
    )
    if not value.startswith(allowed_prefixes):
        raise ValueError("relative_path is outside TeamAI reviewed knowledge")
    return value
