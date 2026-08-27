"""Thin registry and query router for immutable Graphify artifacts."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import errno
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, Tuple

from .auth import authenticate, authorize
from .config import Config
from .evidence import StableRef, sanitize_evidence
from .refs import GRAPHIFY_PRODUCER, graph_artifact_ref


REGISTRY_SCHEMA_VERSION = 1
REGISTRY_NAME = "registry.json"
LOCK_NAME = ".registry.lock"
MAX_GRAPH_BYTES = 256 * 1024 * 1024
MAX_QUERY_CHARS = 2_000
MAX_QUERY_OUTPUT_CHARS = 100_000
DEFAULT_QUERY_BUDGET = 2_000
MIN_QUERY_BUDGET = 100
MAX_QUERY_BUDGET = 10_000
_SNAPSHOT_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_GIT_REVISION = re.compile(r"^[0-9a-f]{40,64}$")
_OVERLAY_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_ENV = re.compile(
    r"(?i)(?:api[_-]?key|access[_-]?key|authorization|credential|password|secret|token)"
)
_SELECTORS = frozenset({"current_canonical", "working_overlay"})


class GraphRouterError(RuntimeError):
    """The graph truth plane could not safely complete the request."""


class GraphArtifactError(GraphRouterError):
    """A Graphify artifact failed immutable identity or health checks."""


class GraphRegistryConflict(GraphRouterError):
    """The requested write conflicts with an existing immutable record."""


class GraphQueryError(GraphRouterError):
    """Graphify query execution failed without changing registry state."""


@dataclass(frozen=True)
class GraphCoverage:
    manifest_files: int
    graph_source_files: int
    nodes: int
    links: int

    def as_dict(self) -> Dict[str, int]:
        return {
            "manifest_files": self.manifest_files,
            "graph_source_files": self.graph_source_files,
            "nodes": self.nodes,
            "links": self.links,
        }


@dataclass(frozen=True)
class GraphArtifact:
    project_id: str
    repo_url: str
    snapshot_id: str
    artifact_kind: str
    recorded_at: str
    coverage: GraphCoverage
    graph_digest: str
    manifest_digest: str
    stable_ref: StableRef
    commit_sha: Optional[str] = None
    base_sha: Optional[str] = None
    overlay_digest: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        result: Dict[str, Any] = {
            "artifact_kind": self.artifact_kind,
            "coverage": self.coverage.as_dict(),
            "graph_digest": self.graph_digest,
            "health": "healthy",
            "manifest_digest": self.manifest_digest,
            "project_id": self.project_id,
            "repo_url": self.repo_url,
            "snapshot_id": self.snapshot_id,
            "stable_ref": self.stable_ref.as_dict(),
        }
        if self.commit_sha is not None:
            result["commit_sha"] = self.commit_sha
            result["generated_at"] = self.recorded_at
        if self.base_sha is not None:
            result["base_sha"] = self.base_sha
            result["evidence_time"] = self.recorded_at
        if self.overlay_digest is not None:
            result["overlay_digest"] = self.overlay_digest
        return result


class GraphRegistry:
    """Single-writer metadata around donor-owned immutable graph directories."""

    def __init__(self, config: Config):
        self.config = config
        self.root = config.paths.data_root / "graphs"
        self.registry_path = self.root / REGISTRY_NAME

    def committed_output_root(
        self, project_id: str, commit_sha: str, snapshot_id: str, *, create: bool = False
    ) -> Path:
        self.config.project(project_id)
        revision = _git_revision(commit_sha, "commit_sha")
        snapshot = _snapshot_id(snapshot_id)
        return self._output_root(
            Path(project_id) / "commits" / revision / snapshot, create=create
        )

    def overlay_output_root(
        self,
        project_id: str,
        base_sha: str,
        overlay_digest: str,
        snapshot_id: str,
        *,
        create: bool = False,
    ) -> Path:
        self.config.project(project_id)
        revision = _git_revision(base_sha, "base_sha")
        digest = _overlay_digest(overlay_digest).split(":", 1)[1]
        snapshot = _snapshot_id(snapshot_id)
        return self._output_root(
            Path(project_id) / "overlays" / revision / digest / snapshot,
            create=create,
        )

    def register_committed(
        self,
        *,
        project_id: str,
        commit_sha: str,
        snapshot_id: str,
        generated_at: str,
        promote: bool = True,
    ) -> GraphArtifact:
        project = self.config.project(project_id)
        revision = _git_revision(commit_sha, "commit_sha")
        snapshot = _snapshot_id(snapshot_id)
        evidence_time = _timestamp(generated_at, "generated_at")
        output_root = self.committed_output_root(project_id, revision, snapshot)
        artifact = self._inspect_artifact(
            project_id=project_id,
            repo_url=project.repo_url,
            snapshot_id=snapshot,
            artifact_kind="committed",
            recorded_at=evidence_time,
            output_root=output_root,
            commit_sha=revision,
        )
        self._register(artifact, "current_canonical" if promote else None)
        return artifact

    def register_overlay(
        self,
        *,
        project_id: str,
        base_sha: str,
        overlay_digest: str,
        snapshot_id: str,
        evidence_time: str,
        promote: bool = True,
    ) -> GraphArtifact:
        project = self.config.project(project_id)
        revision = _git_revision(base_sha, "base_sha")
        digest = _overlay_digest(overlay_digest)
        snapshot = _snapshot_id(snapshot_id)
        observed_at = _timestamp(evidence_time, "evidence_time")
        output_root = self.overlay_output_root(
            project_id, revision, digest, snapshot
        )
        artifact = self._inspect_artifact(
            project_id=project_id,
            repo_url=project.repo_url,
            snapshot_id=snapshot,
            artifact_kind="overlay",
            recorded_at=observed_at,
            output_root=output_root,
            base_sha=revision,
            overlay_digest=digest,
        )
        self._register(artifact, "working_overlay" if promote else None)
        return artifact

    def resolve(
        self,
        project_id: str,
        *,
        snapshot_id: Optional[str] = None,
        selector: Optional[str] = None,
    ) -> GraphArtifact:
        project = self.config.project(project_id)
        if (snapshot_id is None) == (selector is None):
            raise GraphRouterError("provide exactly one snapshot_id or selector")
        state = self._load_state()
        project_state = state["projects"].get(project_id)
        if not isinstance(project_state, dict):
            raise GraphRouterError("project has no registered graph artifacts")
        if project_state.get("repo_url") != project.repo_url:
            raise GraphArtifactError("registered repo_url differs from operator config")
        if selector is not None:
            if selector not in _SELECTORS:
                raise GraphRouterError("unknown graph selector: %s" % selector)
            selected = project_state.get(selector)
            if selected is None:
                raise GraphRouterError("graph selector is empty: %s" % selector)
            snapshot = selected
        else:
            snapshot = _snapshot_id(snapshot_id)
        raw = project_state.get("artifacts", {}).get(snapshot)
        if not isinstance(raw, dict):
            raise GraphRouterError("unknown graph snapshot: %s" % snapshot)
        artifact = _artifact_from_dict(raw)
        if artifact.project_id != project_id or artifact.repo_url != project.repo_url:
            raise GraphArtifactError("graph record belongs to another project")
        output_root = self._artifact_output_root(artifact)
        current = self._inspect_artifact(
            project_id=artifact.project_id,
            repo_url=artifact.repo_url,
            snapshot_id=artifact.snapshot_id,
            artifact_kind=artifact.artifact_kind,
            recorded_at=artifact.recorded_at,
            output_root=output_root,
            commit_sha=artifact.commit_sha,
            base_sha=artifact.base_sha,
            overlay_digest=artifact.overlay_digest,
        )
        if current.as_dict() != artifact.as_dict():
            raise GraphArtifactError("graph artifact changed after registration")
        return artifact

    def readback(self) -> Dict[str, Any]:
        return self._load_state()

    def graph_path(self, artifact: GraphArtifact) -> Path:
        return self._artifact_output_root(artifact) / "graphify-out" / "graph.json"

    def _register(self, artifact: GraphArtifact, selector: Optional[str]) -> None:
        with _registry_lock(self.root):
            state = self._load_state()
            projects = state["projects"]
            project_state = projects.setdefault(
                artifact.project_id,
                {
                    "artifacts": {},
                    "current_canonical": None,
                    "repo_url": artifact.repo_url,
                    "working_overlay": None,
                },
            )
            if project_state.get("repo_url") != artifact.repo_url:
                raise GraphRegistryConflict("project registry repo_url is immutable")
            existing = project_state["artifacts"].get(artifact.snapshot_id)
            record = artifact.as_dict()
            if existing is not None and existing != record:
                raise GraphRegistryConflict("snapshot_id already has another identity")
            project_state["artifacts"][artifact.snapshot_id] = record
            if selector is not None:
                if selector == "current_canonical" and artifact.artifact_kind != "committed":
                    raise GraphRegistryConflict("overlay cannot become current_canonical")
                if selector == "working_overlay" and artifact.artifact_kind != "overlay":
                    raise GraphRegistryConflict("committed graph cannot become working_overlay")
                project_state[selector] = artifact.snapshot_id
            try:
                _write_json_atomic(self.registry_path, state)
            except OSError as exc:
                raise GraphRouterError("graph registry write failed") from exc

    def _inspect_artifact(
        self,
        *,
        project_id: str,
        repo_url: str,
        snapshot_id: str,
        artifact_kind: str,
        recorded_at: str,
        output_root: Path,
        commit_sha: Optional[str] = None,
        base_sha: Optional[str] = None,
        overlay_digest: Optional[str] = None,
    ) -> GraphArtifact:
        graph_path = output_root / "graphify-out" / "graph.json"
        manifest_path = output_root / "graphify-out" / "manifest.json"
        learning_path = output_root / "graphify-out" / ".graphify_learning.json"
        if os.path.lexists(str(learning_path)):
            raise GraphArtifactError("Graphify learning sidecar is forbidden")
        graph_raw, graph_bytes = _load_artifact_json(graph_path, "graph")
        manifest_raw, manifest_bytes = _load_artifact_json(manifest_path, "manifest")
        if not isinstance(graph_raw, dict):
            raise GraphArtifactError("graph.json root must be an object")
        nodes = graph_raw.get("nodes")
        links = graph_raw.get("links")
        if not isinstance(nodes, list) or not nodes:
            raise GraphArtifactError("graph artifact is empty")
        if not isinstance(links, list):
            raise GraphArtifactError("graph links must be a list")
        expected_revision = commit_sha if artifact_kind == "committed" else base_sha
        if graph_raw.get("built_at_commit") != expected_revision:
            raise GraphArtifactError("graph built_at_commit is stale or missing")
        if not isinstance(manifest_raw, dict) or not manifest_raw:
            raise GraphArtifactError("Graphify manifest is empty or missing")
        manifest_files = {_normalized_source_path(item) for item in manifest_raw}
        graph_sources = set()
        for node in nodes:
            if not isinstance(node, dict) or not isinstance(node.get("id"), str):
                raise GraphArtifactError("graph node shape is invalid")
            source_file = node.get("source_file")
            if source_file:
                if not isinstance(source_file, str):
                    raise GraphArtifactError("graph source_file must be a string")
                graph_sources.add(_normalized_source_path(source_file))
        missing_sources = graph_sources - manifest_files
        if missing_sources:
            raise GraphArtifactError("graph source coverage is absent from manifest")
        coverage = GraphCoverage(
            manifest_files=len(manifest_files),
            graph_source_files=len(graph_sources),
            nodes=len(nodes),
            links=len(links),
        )
        graph_digest = _digest(graph_bytes)
        manifest_digest = _digest(manifest_bytes)
        reference = graph_artifact_ref(
            project_id=project_id,
            snapshot_id=snapshot_id,
            repo_url=repo_url,
            graph_digest=graph_digest,
            manifest_digest=manifest_digest,
            artifact_kind=artifact_kind,
            commit_sha=commit_sha,
            base_sha=base_sha,
            overlay_digest=overlay_digest,
        )
        return GraphArtifact(
            project_id=project_id,
            repo_url=repo_url,
            snapshot_id=snapshot_id,
            artifact_kind=artifact_kind,
            recorded_at=recorded_at,
            coverage=coverage,
            graph_digest=graph_digest,
            manifest_digest=manifest_digest,
            stable_ref=reference,
            commit_sha=commit_sha,
            base_sha=base_sha,
            overlay_digest=overlay_digest,
        )

    def _artifact_output_root(self, artifact: GraphArtifact) -> Path:
        if artifact.artifact_kind == "committed":
            return self.committed_output_root(
                artifact.project_id, artifact.commit_sha, artifact.snapshot_id
            )
        return self.overlay_output_root(
            artifact.project_id,
            artifact.base_sha,
            artifact.overlay_digest,
            artifact.snapshot_id,
        )

    def _output_root(self, relative: Path, *, create: bool) -> Path:
        target = self.root / relative
        try:
            target.resolve(strict=False).relative_to(self.root.resolve(strict=False))
        except ValueError as exc:
            raise GraphArtifactError("graph artifact path escapes managed root") from exc
        if create:
            _ensure_private_directory(self.root)
            _ensure_private_directory(target)
        return target

    def _load_state(self) -> Dict[str, Any]:
        if not self.registry_path.exists() and not self.registry_path.is_symlink():
            return {"projects": {}, "schema_version": REGISTRY_SCHEMA_VERSION}
        if not self.registry_path.is_file() or self.registry_path.is_symlink():
            raise GraphArtifactError("graph registry is not a safe regular file")
        if self.registry_path.stat().st_mode & 0o022:
            raise GraphArtifactError("graph registry has unsafe permissions")
        try:
            raw = json.loads(
                self.registry_path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
            )
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise GraphArtifactError("cannot read graph registry: %s" % exc) from exc
        _validate_state_shape(raw)
        return raw


class GraphQueryRouter:
    """Authorize a bounded question, then invoke Graphify on one resolved graph."""

    def __init__(
        self,
        config: Config,
        graphify_executable: Path,
        *,
        timeout_seconds: int = 30,
    ):
        self.config = config
        self.registry = GraphRegistry(config)
        self.graphify_executable = Path(graphify_executable)
        self.timeout_seconds = timeout_seconds
        if (
            not self.graphify_executable.is_file()
            or self.graphify_executable.is_symlink()
            or not os.access(str(self.graphify_executable), os.X_OK)
        ):
            raise GraphQueryError("operator Graphify executable is absent or unsafe")
        _probe_graphify_version(self.graphify_executable)
        if timeout_seconds < 1 or timeout_seconds > 120:
            raise GraphQueryError("query timeout must be between 1 and 120 seconds")

    def query(
        self,
        *,
        principal_id: str,
        project_id: str,
        question: str,
        snapshot_id: Optional[str] = None,
        selector: Optional[str] = None,
        budget: int = DEFAULT_QUERY_BUDGET,
    ) -> Dict[str, Any]:
        context = authenticate(self.config, principal_id)
        authorize(context, project_id, "get")
        if (
            not isinstance(question, str)
            or not question
            or question != question.strip()
            or len(question) > MAX_QUERY_CHARS
            or question.startswith("-")
            or any(ord(character) < 32 and character not in "\n\t" for character in question)
        ):
            raise GraphQueryError("question must be bounded, trimmed text")
        if (
            type(budget) is not int
            or budget < MIN_QUERY_BUDGET
            or budget > MAX_QUERY_BUDGET
        ):
            raise GraphQueryError(
                "query token budget must be an integer from %d to %d"
                % (MIN_QUERY_BUDGET, MAX_QUERY_BUDGET)
            )
        artifact = self.registry.resolve(
            project_id, snapshot_id=snapshot_id, selector=selector
        )
        graph_path = self.registry.graph_path(artifact)
        environment = _graphify_environment()
        # Graphify may update cache/last_query_stamp for its strict hook. That
        # timestamp contains no query text and is deliberately outside the
        # digest-bound graph.json + manifest.json identity.
        command: Sequence[str] = (
            str(self.graphify_executable),
            "query",
            question,
            "--graph",
            str(graph_path),
            "--budget",
            str(budget),
        )
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                env=environment,
                text=True,
                timeout=self.timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise GraphQueryError(
                "Graphify query timed out for %s"
                % _opaque_graph_identity(project_id, artifact.snapshot_id)
            ) from exc
        except OSError as exc:
            raise GraphQueryError(
                "Graphify query could not start for %s (%s)"
                % (
                    _opaque_graph_identity(project_id, artifact.snapshot_id),
                    type(exc).__name__,
                )
            ) from exc
        if completed.returncode != 0:
            detail = sanitize_evidence(
                _present_graphify_text(
                    completed.stderr,
                    data_root=self.config.paths.data_root,
                    graph_path=graph_path,
                    project_id=project_id,
                    snapshot_id=artifact.snapshot_id,
                ),
                max_string=1_000,
            )
            raise GraphQueryError(
                "Graphify query failed with code %d: %s"
                % (completed.returncode, detail)
            )
        if len(completed.stdout) > MAX_QUERY_OUTPUT_CHARS:
            raise GraphQueryError("Graphify query output exceeded the router bound")
        result = _present_graphify_text(
            completed.stdout,
            data_root=self.config.paths.data_root,
            graph_path=graph_path,
            project_id=project_id,
            snapshot_id=artifact.snapshot_id,
        )
        return {
            "actor": context.actor,
            "artifact_kind": artifact.artifact_kind,
            "ok": True,
            "project_id": project_id,
            "result": result,
            "snapshot_id": artifact.snapshot_id,
            "stable_ref": artifact.stable_ref.as_dict(),
        }


def _present_graphify_text(
    value: str,
    *,
    data_root: Path,
    graph_path: Path,
    project_id: str,
    snapshot_id: str,
) -> str:
    """Replace managed filesystem locations with one opaque graph identity."""

    opaque = _opaque_graph_identity(project_id, snapshot_id)
    managed_paths = [graph_path]
    managed_paths.extend(
        parent
        for parent in graph_path.parents
        if parent == data_root or data_root in parent.parents
    )
    result = value
    for path in sorted({str(path) for path in managed_paths}, key=len, reverse=True):
        result = result.replace(path, opaque)
    return result


def _opaque_graph_identity(project_id: str, snapshot_id: str) -> str:
    return "graph://%s/%s" % (project_id, snapshot_id)


def _artifact_from_dict(raw: Mapping[str, Any]) -> GraphArtifact:
    required = {
        "artifact_kind",
        "coverage",
        "graph_digest",
        "health",
        "manifest_digest",
        "project_id",
        "repo_url",
        "snapshot_id",
        "stable_ref",
    }
    kind = raw.get("artifact_kind")
    if kind == "committed":
        optional = {"commit_sha", "generated_at"}
        recorded_at = raw.get("generated_at")
    elif kind == "overlay":
        optional = {"base_sha", "evidence_time", "overlay_digest"}
        recorded_at = raw.get("evidence_time")
    else:
        raise GraphArtifactError("graph artifact kind is unsupported")
    if set(raw) != required | optional or raw.get("health") != "healthy":
        raise GraphArtifactError("graph record has an unsupported shape")
    coverage = raw["coverage"]
    if not isinstance(coverage, dict) or set(coverage) != {
        "manifest_files",
        "graph_source_files",
        "nodes",
        "links",
    }:
        raise GraphArtifactError("graph coverage has an unsupported shape")
    counts = tuple(
        coverage[name]
        for name in ("manifest_files", "graph_source_files", "nodes", "links")
    )
    if any(type(value) is not int or value < 0 for value in counts):
        raise GraphArtifactError("graph coverage counts are invalid")
    return GraphArtifact(
        project_id=raw["project_id"],
        repo_url=raw["repo_url"],
        snapshot_id=raw["snapshot_id"],
        artifact_kind=kind,
        recorded_at=_timestamp(recorded_at, "artifact timestamp"),
        coverage=GraphCoverage(*counts),
        graph_digest=raw["graph_digest"],
        manifest_digest=raw["manifest_digest"],
        stable_ref=StableRef.from_dict(raw["stable_ref"]),
        commit_sha=raw.get("commit_sha"),
        base_sha=raw.get("base_sha"),
        overlay_digest=raw.get("overlay_digest"),
    )


def _probe_graphify_version(executable: Path) -> None:
    expected = "graphify " + GRAPHIFY_PRODUCER.split("@", 1)[1]
    try:
        completed = subprocess.run(
            (str(executable), "--version"),
            check=False,
            capture_output=True,
            env=_graphify_environment(),
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GraphQueryError("cannot verify the operator Graphify version") from exc
    if completed.returncode != 0 or completed.stdout.strip() != expected:
        raise GraphQueryError("Graphify executable must be exact %s" % expected)


def _graphify_environment() -> Dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if not _SECRET_ENV.search(name)
    }
    for name in (
        "GRAPHIFY_QUERY_LOG",
        "GRAPHIFY_QUERY_LOG_ENABLE",
        "GRAPHIFY_QUERY_LOG_RESPONSES",
    ):
        environment.pop(name, None)
    environment["GRAPHIFY_QUERY_LOG_DISABLE"] = "1"
    return environment


def _validate_state_shape(raw: Any) -> None:
    if not isinstance(raw, dict) or set(raw) != {"projects", "schema_version"}:
        raise GraphArtifactError("graph registry has an unsupported shape")
    if raw["schema_version"] != REGISTRY_SCHEMA_VERSION or not isinstance(raw["projects"], dict):
        raise GraphArtifactError("graph registry schema is unsupported")
    for project_id, project in raw["projects"].items():
        if not isinstance(project, dict) or set(project) != {
            "artifacts",
            "current_canonical",
            "repo_url",
            "working_overlay",
        }:
            raise GraphArtifactError("project registry entry has an unsupported shape")
        if not isinstance(project["artifacts"], dict):
            raise GraphArtifactError("project artifacts must be an object")
        for snapshot_id, artifact in project["artifacts"].items():
            if not isinstance(artifact, dict):
                raise GraphArtifactError("artifact registry entry must be an object")
            if snapshot_id != artifact.get("snapshot_id"):
                raise GraphArtifactError("artifact key differs from snapshot_id")
            parsed = _artifact_from_dict(artifact)
            if parsed.project_id != project_id or parsed.repo_url != project["repo_url"]:
                raise GraphArtifactError("artifact does not match its project entry")
        for selector in _SELECTORS:
            value = project[selector]
            if value is not None and value not in project["artifacts"]:
                raise GraphArtifactError("graph selector references an unknown snapshot")
        canonical = project["current_canonical"]
        if (
            canonical is not None
            and project["artifacts"][canonical]["artifact_kind"] != "committed"
        ):
            raise GraphArtifactError("current_canonical references an overlay")
        working = project["working_overlay"]
        if working is not None and project["artifacts"][working]["artifact_kind"] != "overlay":
            raise GraphArtifactError("working_overlay references a committed graph")


def _load_artifact_json(path: Path, label: str) -> Tuple[Any, bytes]:
    if not path.is_file() or path.is_symlink():
        raise GraphArtifactError("%s artifact is absent or unsafe" % label)
    size = path.stat().st_size
    if size < 1 or size > MAX_GRAPH_BYTES:
        raise GraphArtifactError("%s artifact size is invalid" % label)
    content = path.read_bytes()
    try:
        raw = json.loads(content.decode("utf-8"), object_pairs_hook=_reject_duplicate_json_keys)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GraphArtifactError("%s artifact is not valid JSON" % label) from exc
    return raw, content


def _normalized_source_path(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise GraphArtifactError("manifest/source path must be non-empty text")
    path = value.replace("\\", "/")
    if path.startswith("./"):
        path = path[2:]
    if (
        not path
        or path.startswith("/")
        or any(ord(character) < 32 for character in path)
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or ":" in path.split("/", 1)[0]
    ):
        raise GraphArtifactError("artifact source path must be root-relative")
    return path


def _timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise GraphArtifactError("%s must be a timezone-aware ISO timestamp" % field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GraphArtifactError("%s must be a timezone-aware ISO timestamp" % field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GraphArtifactError("%s must be timezone-aware" % field)
    return value


def _snapshot_id(value: object) -> str:
    if not isinstance(value, str) or not _SNAPSHOT_ID.fullmatch(value):
        raise GraphArtifactError("snapshot_id must be an opaque stable identifier")
    return value


def _git_revision(value: object, field: str) -> str:
    if not isinstance(value, str) or not _GIT_REVISION.fullmatch(value):
        raise GraphArtifactError("%s must be a full lowercase Git object id" % field)
    return value


def _overlay_digest(value: object) -> str:
    if not isinstance(value, str) or not _OVERLAY_DIGEST.fullmatch(value):
        raise GraphArtifactError("overlay_digest must be sha256:<64 lowercase hex>")
    return value


def _digest(content: bytes) -> str:
    return "sha256:" + sha256(content).hexdigest()


def _reject_duplicate_json_keys(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GraphArtifactError("duplicate JSON key: %s" % key)
        result[key] = value
    return result


@contextmanager
def _registry_lock(root: Path) -> Iterator[None]:
    _ensure_private_directory(root)
    lock_path = root / LOCK_NAME
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(lock_path), flags, 0o600)
    except OSError as exc:
        raise GraphRegistryConflict("cannot open graph registry lock") from exc
    locked = False
    try:
        lock_stat = os.fstat(descriptor)
        if not stat.S_ISREG(lock_stat.st_mode) or lock_stat.st_mode & 0o022:
            raise GraphRegistryConflict("graph registry lock is unsafe")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise GraphRegistryConflict("another graph registry write is active") from exc
            raise
        yield
    finally:
        if locked:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        path.mkdir(parents=True, mode=0o700)
        os.chmod(path, 0o700)
    if not path.is_dir() or path.is_symlink() or path.stat().st_mode & 0o077:
        raise GraphArtifactError("managed graph directory is unsafe")


def _write_json_atomic(path: Path, value: Mapping[str, Any]) -> None:
    _ensure_private_directory(path.parent)
    content = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".%s." % path.name, dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
        directory = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        if temporary.exists():
            temporary.unlink()
        raise
