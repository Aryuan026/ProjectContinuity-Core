"""Controller-owned Graphify builds from approved managed Git checkouts."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Dict, Iterator, Mapping

from .config import Config
from .graph_router import (
    GraphArtifact,
    GraphArtifactError,
    GraphRegistry,
    GraphRegistryConflict,
    GraphRouterError,
    _ensure_private_directory,
    _graphify_environment,
)
from .managed_git import (
    ManagedGitError,
    inspect_managed_git_config,
    managed_git_environment,
)


BUILD_TIMEOUT_SECONDS = 600
MAX_TREE_LIST_BYTES = 16 * 1024 * 1024


class GraphControllerError(RuntimeError):
    """A requested graph build could not preserve exact source identity."""


class GraphSnapshotController:
    """Build a clean committed graph without accepting caller filesystem paths."""

    def __init__(self, config: Config, graphify_executable: Path) -> None:
        self.config = config
        self.graphify_executable = Path(graphify_executable)
        self.registry = GraphRegistry(config)
        if (
            not self.graphify_executable.is_file()
            or self.graphify_executable.is_symlink()
            or not os.access(self.graphify_executable, os.X_OK)
        ):
            raise GraphControllerError("graphify_runtime_unavailable")

    def register_committed(
        self,
        project_id: str,
        arguments: Mapping[str, Any],
        *,
        actor: str,
        expected_revision: str,
    ) -> Dict[str, Any]:
        if not isinstance(actor, str) or not actor or actor != actor.strip():
            raise GraphControllerError("graph_actor_malformed")
        if not isinstance(arguments, dict) or set(arguments) != {"commit_sha"}:
            raise GraphControllerError("graph_register_arguments_malformed")
        commit_sha = arguments["commit_sha"]
        if not isinstance(commit_sha, str) or len(commit_sha) != 40:
            raise GraphControllerError("graph_commit_sha_malformed")
        project = self.config.project(project_id)
        source = self.config.paths.data_root / "delivery" / project_id
        _managed_clean_checkout(
            source,
            project.repo_url,
            commit_sha,
            custody_root=self.config.paths.data_root,
        )
        current = self._current(project_id, "current_canonical")
        current_revision = current.stable_ref.version if current is not None else "absent"
        if expected_revision != current_revision:
            raise GraphControllerError("graph_expected_revision_conflict")

        snapshot_id = "%s-%s" % (project_id, commit_sha[:12])
        if current is not None and current.snapshot_id == snapshot_id:
            return {
                "actor": actor,
                "changed": False,
                "current": current.as_dict(),
                "ok": True,
                "operation": "register_committed",
            }

        output_root = self.registry.committed_output_root(
            project_id, commit_sha, snapshot_id
        )
        if os.path.lexists(str(output_root)):
            try:
                artifact = self.registry.register_committed(
                    project_id=project_id,
                    commit_sha=commit_sha,
                    snapshot_id=snapshot_id,
                    generated_at=_now(),
                    promote=True,
                    expected_revision=expected_revision,
                )
            except GraphRegistryConflict as exc:
                raise GraphControllerError("graph_expected_revision_conflict") from exc
            except (GraphArtifactError, GraphRouterError) as exc:
                raise GraphControllerError("graph_registration_failed") from exc
            return _receipt(
                artifact,
                actor=actor,
                changed=current != artifact,
                operation="register_committed",
            )

        _ensure_private_directory(self.registry.root)
        staging_root = self.registry.root / ".build"
        _ensure_private_directory(staging_root)
        with (
            _exact_source_snapshot(source, commit_sha, staging_root) as snapshot,
            tempfile.TemporaryDirectory(
                prefix=".%s." % snapshot_id, dir=str(staging_root)
            ) as temporary_name,
        ):
            temporary = Path(temporary_name)
            command = (
                str(self.graphify_executable),
                "extract",
                str(snapshot),
                "--out",
                str(temporary),
                "--code-only",
                "--max-workers",
                "1",
            )
            try:
                completed = subprocess.run(
                    command,
                    cwd=snapshot,
                    env=_graphify_build_environment(),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=BUILD_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise GraphControllerError("graph_build_failed") from exc
            if completed.returncode != 0:
                raise GraphControllerError("graph_build_failed")
            graph = temporary / "graphify-out/graph.json"
            manifest = temporary / "graphify-out/manifest.json"
            if not graph.is_file() or not manifest.is_file():
                raise GraphControllerError("graph_build_incomplete")
            _ensure_private_directory(output_root.parent)
            try:
                os.replace(temporary, output_root)
            except OSError as exc:
                raise GraphControllerError("graph_artifact_install_failed") from exc

        try:
            artifact = self.registry.register_committed(
                project_id=project_id,
                commit_sha=commit_sha,
                snapshot_id=snapshot_id,
                generated_at=_now(),
                promote=True,
                expected_revision=expected_revision,
            )
        except GraphRegistryConflict as exc:
            raise GraphControllerError("graph_expected_revision_conflict") from exc
        except (GraphArtifactError, GraphRouterError) as exc:
            raise GraphControllerError("graph_registration_failed") from exc
        return _receipt(
            artifact,
            actor=actor,
            changed=True,
            operation="register_committed",
        )

    def register_overlay(
        self,
        project_id: str,
        arguments: Mapping[str, Any],
        *,
        actor: str,
        expected_revision: str,
    ) -> Dict[str, Any]:
        """Promote one prebuilt managed diagnostic artifact to working_overlay.

        The artifact must already occupy GraphRegistry's deterministic overlay
        namespace.  Clients provide only immutable identity fields—never a
        filesystem path or product-worktree location.
        """

        if not isinstance(actor, str) or not actor or actor != actor.strip():
            raise GraphControllerError("graph_actor_malformed")
        required = {"base_sha", "evidence_time", "overlay_digest", "snapshot_id"}
        if not isinstance(arguments, dict) or set(arguments) != required:
            raise GraphControllerError("graph_overlay_arguments_malformed")
        base_sha = arguments["base_sha"]
        overlay_digest = arguments["overlay_digest"]
        snapshot_id = arguments["snapshot_id"]
        evidence_time = arguments["evidence_time"]
        current = self._current(project_id, "working_overlay")
        current_revision = current.stable_ref.version if current is not None else "absent"
        if expected_revision != current_revision:
            raise GraphControllerError("graph_expected_revision_conflict")
        try:
            artifact = self.registry.register_overlay(
                project_id=project_id,
                base_sha=base_sha,
                overlay_digest=overlay_digest,
                snapshot_id=snapshot_id,
                evidence_time=evidence_time,
                promote=True,
                expected_revision=expected_revision,
            )
        except GraphRegistryConflict as exc:
            raise GraphControllerError("graph_expected_revision_conflict") from exc
        except (GraphArtifactError, GraphRouterError, ValueError) as exc:
            raise GraphControllerError("graph_overlay_registration_failed") from exc
        changed = current != artifact
        return _receipt(
            artifact,
            actor=actor,
            changed=changed,
            operation="register_overlay",
        )

    def _current(self, project_id: str, selector: str) -> GraphArtifact | None:
        try:
            return self.registry.resolve(project_id, selector=selector)
        except GraphRouterError as exc:
            if str(exc) in {
                "project has no registered graph artifacts",
                "graph selector is empty: %s" % selector,
            }:
                return None
            raise GraphControllerError("graph_current_readback_failed") from exc


def _managed_clean_checkout(
    root: Path,
    expected_remote: str,
    expected_revision: str,
    *,
    custody_root: Path,
) -> None:
    try:
        relative = root.relative_to(custody_root)
    except ValueError as exc:
        raise GraphControllerError("delivery_checkout_unsafe") from exc
    current = custody_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise GraphControllerError("delivery_checkout_unsafe")
    if (
        root.is_symlink()
        or not root.is_dir()
        or (root / ".git").is_symlink()
        or not (root / ".git").is_dir()
    ):
        raise GraphControllerError("delivery_checkout_unavailable")
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o022:
        raise GraphControllerError("delivery_checkout_unsafe")
    try:
        inspect_managed_git_config(root, expected_remote)
    except ManagedGitError as exc:
        if exc.code == "managed_git_remote_conflict":
            raise GraphControllerError("delivery_checkout_remote_mismatch") from exc
        raise GraphControllerError("delivery_checkout_unsafe") from exc
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise GraphControllerError("delivery_checkout_dirty")
    if _git(root, "rev-parse", "HEAD") != expected_revision:
        raise GraphControllerError("delivery_checkout_revision_mismatch")


@contextmanager
def _exact_source_snapshot(
    root: Path, revision: str, staging_root: Path
) -> Iterator[Path]:
    """Yield one detached Git tree materialized from raw committed blob bytes."""

    with tempfile.TemporaryDirectory(prefix=".source.", dir=str(staging_root)) as name:
        snapshot = Path(name) / "checkout"
        _git(
            staging_root,
            "-c",
            "core.hooksPath=%s" % os.devnull,
            "clone",
            "--shared",
            "--no-checkout",
            "--",
            str(root),
            str(snapshot),
        )
        _git(
            snapshot,
            "update-ref",
            "--no-deref",
            "HEAD",
            revision,
        )
        _git(snapshot, "read-tree", revision)
        _materialize_exact_tree(snapshot, revision)
        if _git(snapshot, "rev-parse", "HEAD") != revision:
            raise GraphControllerError("delivery_snapshot_revision_mismatch")
        yield snapshot


def _materialize_exact_tree(root: Path, revision: str) -> None:
    records = _git_bytes(
        root,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        revision,
        max_output=MAX_TREE_LIST_BYTES,
    )
    for record in records.split(b"\0"):
        if not record:
            continue
        try:
            header, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id = header.split(b" ", 2)
        except ValueError as exc:
            raise GraphControllerError("delivery_snapshot_tree_invalid") from exc
        if mode == b"160000" and object_type == b"commit":
            continue
        if object_type == b"blob" and mode == b"120000":
            raise GraphControllerError("delivery_snapshot_symlink_forbidden")
        if object_type != b"blob" or mode not in {b"100644", b"100755"}:
            raise GraphControllerError("delivery_snapshot_tree_invalid")
        parts = raw_path.split(b"/")
        if (
            not raw_path
            or raw_path.startswith(b"/")
            or any(part in {b"", b".", b".."} or part.lower() == b".git" for part in parts)
        ):
            raise GraphControllerError("delivery_snapshot_tree_unsafe")
        destination = root.joinpath(*(os.fsdecode(part) for part in parts))
        try:
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            object_name = object_id.decode("ascii")
            _write_git_blob(root, object_name, destination)
            destination.chmod(0o700 if mode == b"100755" else 0o600)
        except (OSError, UnicodeError) as exc:
            raise GraphControllerError("delivery_snapshot_materialization_failed") from exc


def _write_git_blob(root: Path, object_name: str, destination: Path) -> None:
    environment = managed_git_environment()
    environment["GIT_ATTR_NOSYSTEM"] = "1"
    try:
        with destination.open("xb") as target:
            completed = subprocess.run(
                ("git", "cat-file", "blob", object_name),
                cwd=root,
                env=environment,
                check=False,
                stdout=target,
                stderr=subprocess.PIPE,
                timeout=30,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GraphControllerError("delivery_git_failed") from exc
    if completed.returncode != 0 or len(completed.stderr) > 256_000:
        raise GraphControllerError("delivery_git_failed")


def _git(root: Path, *arguments: str) -> str:
    raw = _git_bytes(root, *arguments)
    try:
        return raw.decode("utf-8").rstrip("\n")
    except UnicodeDecodeError as exc:
        raise GraphControllerError("delivery_git_failed") from exc


def _git_bytes(
    root: Path, *arguments: str, max_output: int | None = 256_000
) -> bytes:
    environment = managed_git_environment()
    environment["GIT_ATTR_NOSYSTEM"] = "1"
    try:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GraphControllerError("delivery_git_failed") from exc
    if completed.returncode != 0 or (
        max_output is not None and len(completed.stdout) > max_output
    ):
        raise GraphControllerError("delivery_git_failed")
    return completed.stdout


def _graphify_build_environment() -> Dict[str, str]:
    environment = _graphify_environment()
    for name in tuple(environment):
        if name.startswith("GIT_") or name in {"HOME", "XDG_CONFIG_HOME"}:
            environment.pop(name)
    environment.update(managed_git_environment())
    environment["GIT_ATTR_NOSYSTEM"] = "1"
    return environment


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _receipt(
    artifact: GraphArtifact, *, actor: str, changed: bool, operation: str
) -> Dict[str, Any]:
    return {
        "actor": actor,
        "changed": changed,
        "current": artifact.as_dict(),
        "ok": True,
        "operation": operation,
    }
