"""Production read adapters over donor-owned OpenSpec, TeamAI, and Git truth."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, Mapping, Sequence, Tuple

from .auth import authenticate
from .config import Config
from .evidence import StableRef, sanitize_evidence
from .github_resolver import (
    GitHubAuthorityResolver,
    GitHubResolverError,
    GitHubResolverUnavailable,
    canonical_digest,
)
from .managed_git import (
    ManagedGitConfig,
    ManagedGitError,
    inspect_managed_git_config,
    managed_git_environment,
)
from .refs import (
    github_delivery_ref,
    github_pull_request_ref,
    github_release_ref,
    openspec_decision_ref,
    teamai_reviewed_ref,
)
from .teamai import (
    TeamAIContractError,
    assert_no_teamai_implicit_inputs,
    classify_teamai_publish,
    resolve_teamai_identity,
    teamai_explicit_environment,
    teamai_readonly_recall_request,
    verify_teamai_guard_documents,
)
from .truth_bindings import OpenSpecBinding, TeamAIBinding


MAX_COMMAND_OUTPUT = 256_000
MAX_ITEMS = 100
MAX_WRITE_BODY = 100_000
WRITE_TIMEOUT_SECONDS = 180
READ_DEADLINE_SECONDS = 15.0
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_PULL_REQUEST = re.compile(r"^Merge pull request #(\d+)\b")
_CHANGE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ARTIFACT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_TEAMAI_RECALL_HEADER = re.compile(
    r"^--- \[teamai:recall:start\] --- \(([0-9]+) results?\)$",
    re.MULTILINE,
)
_TEAMAI_PATHS = {
    "agents": ".teamai/agents",
    "docs": ".teamai/docs",
    "learnings": ".teamai/learnings",
    "rules": ".teamai/rules",
    "skills": ".teamai/skills",
}
_TEAMAI_KINDS = {
    "agents": "assignment",
    "docs": "minutes",
    "learnings": "learning",
    "rules": "workstream",
    "skills": "workstream",
}


class AuthorityLayerError(RuntimeError):
    """A donor-owned authority could not return bounded, verified truth."""


class AuthorityLayerUnavailable(AuthorityLayerError):
    """A configured donor or managed checkout is not available on this host."""


class OpenSpecLayer:
    authority = "openspec"
    layer = "decisions"

    def __init__(
        self,
        config: Config,
        binding: OpenSpecBinding,
        executable: Path,
        node_executable: Path | None = None,
    ) -> None:
        self.config = config
        self.binding = binding
        if node_executable is None:
            self.command = (str(_executable(executable, "OpenSpec")),)
        else:
            self.command = (
                str(_executable(node_executable, "Node")),
                str(_file(executable, "OpenSpec")),
            )
        self.root_base = config.paths.data_root / "openspec"
        _probe_version(self.command, "1.10.0", "OpenSpec")

    def status(self, principal_id: str, project_id: str) -> Mapping[str, Any]:
        del principal_id
        deadline = _deadline()
        root, revision = self._root(project_id, deadline=deadline)
        changes = self._list(root, "change", deadline=deadline)
        specs = self._list(root, "spec", deadline=deadline)
        return {
            "changes": len(changes),
            "specs": len(specs),
            "store_id": self.binding.store_id,
            "version": revision,
        }

    def search(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        *,
        limit: int,
        selector: str = "",
    ) -> Sequence[Mapping[str, Any]]:
        del principal_id
        if selector:
            raise AuthorityLayerError("openspec_selector_unsupported")
        deadline = _deadline()
        root, revision = self._root(project_id, deadline=deadline)
        needle = _query(query).casefold()
        results = []
        for kind in ("change", "spec"):
            for item_id in self._list(root, kind, deadline=deadline)[:MAX_ITEMS]:
                payload = self._show(root, item_id, kind, deadline=deadline)
                public_payload = _openspec_public_payload(
                    payload,
                    project_id=project_id,
                    store_id=self.binding.store_id,
                    item_id=item_id,
                )
                haystack = json.dumps(
                    public_payload, ensure_ascii=False, sort_keys=True
                ).casefold()
                if needle not in item_id.casefold() and needle not in haystack:
                    continue
                reference = self._reference(
                    item_id, kind, revision, public_payload
                )
                results.append(
                    {
                        "id": item_id,
                        "kind": kind,
                        "stable_ref": reference.as_dict(),
                        "summary": _bounded_preview(public_payload),
                    }
                )
                if len(results) >= limit:
                    return results
        return results

    def get(
        self, principal_id: str, project_id: str, reference: StableRef
    ) -> Mapping[str, Any]:
        del principal_id
        deadline = _deadline()
        item_id, kind = self._validated_reference(reference)
        root, _revision = self._root(project_id, deadline=deadline)
        with _isolated_worktree(
            root, reference.version, "readonly", deadline=deadline
        ) as snapshot:
            payload = self._show(snapshot, item_id, kind, deadline=deadline)
        public_payload = _openspec_public_payload(
            payload,
            project_id=project_id,
            store_id=self.binding.store_id,
            item_id=item_id,
        )
        current = self._reference(
            item_id, kind, reference.version, public_payload
        )
        if current != reference:
            raise AuthorityLayerError("openspec_reference_changed")
        return {
            "id": item_id,
            "kind": kind,
            "payload": sanitize_evidence(
                public_payload, max_depth=10, max_items=100, max_string=20_000
            ),
            "stable_ref": current.as_dict(),
        }

    def update(
        self,
        principal_id: str,
        project_id: str,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        expected_revision: str,
    ) -> Mapping[str, Any]:
        """Create a validated proposal/archive branch in an isolated worktree."""

        root, revision = self._root(project_id)
        if expected_revision != revision:
            raise AuthorityLayerError("openspec_expected_revision_conflict")
        if operation == "prepare_change":
            change_id, artifacts = _openspec_change_arguments(arguments)
        elif operation == "archive_change":
            change_id = _openspec_archive_arguments(arguments)
            artifacts = ()
        else:
            raise AuthorityLayerError("openspec_update_operation_unsupported")
        actor = _safe_actor(authenticate(self.config, principal_id).actor)
        request_digest = _request_digest(
            {
                "arguments": arguments,
                "expected_revision": expected_revision,
                "operation": operation,
                "project_id": project_id,
            }
        )
        branch = "project-continuity/openspec/%s/%s-%s" % (
            actor,
            operation.replace("_", "-"),
            change_id,
        )
        existing = _remote_branch(root, branch, self.binding.repo_url)
        if existing is not None:
            _assert_request_commit(root, existing, request_digest)
            return {
                "actor": actor,
                "branch": branch,
                "changed": False,
                "ok": True,
                "operation": operation,
                "review_state": "pending",
                "revision": existing,
            }

        with _isolated_worktree(root, revision, actor) as worktree:
            if operation == "prepare_change":
                self._prepare_change(worktree, change_id, artifacts)
            else:
                self._archive_change(worktree, change_id)
            commit = _commit_worktree(
                worktree,
                actor=actor,
                subject="[openspec] %s %s" % (operation, change_id),
                request_digest=request_digest,
            )
            _push_branch(root, worktree, branch, self.binding.repo_url)
        return {
            "actor": actor,
            "branch": branch,
            "changed": True,
            "ok": True,
            "operation": operation,
            "review_state": "pending",
            "revision": commit,
        }

    def _prepare_change(
        self,
        worktree: Path,
        change_id: str,
        artifacts: Sequence[Mapping[str, str]],
    ) -> None:
        _json_command(
            self.command,
            ["new", "change", change_id, "--json"],
            cwd=worktree,
            label="OpenSpec",
            timeout=WRITE_TIMEOUT_SECONDS,
        )
        for artifact in artifacts:
            instructions = _json_command(
                self.command,
                [
                    "instructions",
                    artifact["artifact_id"],
                    "--change",
                    change_id,
                    "--json",
                ],
                cwd=worktree,
                label="OpenSpec",
                timeout=WRITE_TIMEOUT_SECONDS,
            )
            output_pattern = instructions.get("outputPath")
            change_dir = instructions.get("changeDir")
            if not isinstance(output_pattern, str) or not isinstance(change_dir, str):
                raise AuthorityLayerError("openspec_instructions_malformed")
            destination = _openspec_artifact_path(
                worktree,
                change_dir,
                output_pattern,
                artifact["relative_output"],
            )
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(artifact["body"], encoding="utf-8")
        _json_command(
            self.command,
            [
                "validate",
                change_id,
                "--type",
                "change",
                "--strict",
                "--json",
                "--no-interactive",
            ],
            cwd=worktree,
            label="OpenSpec",
            timeout=WRITE_TIMEOUT_SECONDS,
        )

    def _archive_change(self, worktree: Path, change_id: str) -> None:
        _json_command(
            self.command,
            [
                "validate",
                change_id,
                "--type",
                "change",
                "--strict",
                "--json",
                "--no-interactive",
            ],
            cwd=worktree,
            label="OpenSpec",
            timeout=WRITE_TIMEOUT_SECONDS,
        )
        _json_command(
            self.command,
            ["archive", change_id, "--yes", "--json"],
            cwd=worktree,
            label="OpenSpec",
            timeout=WRITE_TIMEOUT_SECONDS,
        )

    def _validated_reference(self, reference: StableRef) -> Tuple[str, str]:
        prefix = "decision:%s:" % self.binding.store_id
        if not reference.object_id.startswith(prefix):
            raise AuthorityLayerError("openspec_reference_invalid")
        item_id = reference.object_id[len(prefix) :]
        kinds = {"current": "spec", "proposal": "change"}
        kind = kinds.get(reference.projection or "")
        if kind is None:
            raise AuthorityLayerError("openspec_reference_invalid")
        try:
            expected = openspec_decision_ref(
                store_id=self.binding.store_id,
                decision_id=item_id,
                revision=reference.version,
                artifact_digest=reference.digest,
                state=reference.projection,
                repo_url=self.binding.repo_url,
            )
        except ValueError as exc:
            raise AuthorityLayerError("openspec_reference_invalid") from exc
        if reference != expected:
            raise AuthorityLayerError("openspec_reference_invalid")
        return item_id, kind

    def _root(
        self, project_id: str, *, deadline: float | None = None
    ) -> Tuple[Path, str]:
        self.config.project(project_id)
        root = _managed_repo(
            self.root_base, project_id, self.binding.repo_url, deadline=deadline
        )
        revision = _git(
            root, "rev-parse", "HEAD", timeout=_remaining_timeout(deadline)
        )
        _clean_checkout(root, deadline=deadline)
        return root, revision

    def _list(
        self, root: Path, kind: str, *, deadline: float | None = None
    ) -> Tuple[str, ...]:
        arguments = ["list", "--json", "--sort", "name"]
        if kind == "spec":
            arguments.append("--specs")
        payload = _json_command(
            self.command,
            arguments,
            cwd=root,
            label="OpenSpec",
            timeout=_remaining_timeout(deadline),
        )
        key = "specs" if kind == "spec" else "changes"
        values = payload.get(key)
        if not isinstance(values, list) or len(values) > MAX_ITEMS:
            raise AuthorityLayerError("openspec_list_malformed")
        ids = []
        for value in values:
            if not isinstance(value, dict):
                raise AuthorityLayerError("openspec_list_malformed")
            item_id = value.get("id" if kind == "spec" else "name")
            if not isinstance(item_id, str) or not item_id or item_id != item_id.strip():
                raise AuthorityLayerError("openspec_list_malformed")
            ids.append(item_id)
        return tuple(ids)

    def _show(
        self,
        root: Path,
        item_id: str,
        kind: str,
        *,
        deadline: float | None = None,
    ) -> Mapping[str, Any]:
        payload = _json_command(
            self.command,
            ["show", item_id, "--type", kind, "--json", "--no-interactive"],
            cwd=root,
            label="OpenSpec",
            timeout=_remaining_timeout(deadline),
        )
        return payload

    def _reference(
        self,
        item_id: str,
        kind: str,
        revision: str,
        payload: Mapping[str, Any],
    ) -> StableRef:
        state = "current" if kind == "spec" else "proposal"
        return openspec_decision_ref(
            store_id=self.binding.store_id,
            decision_id=item_id,
            revision=revision,
            artifact_digest=_json_digest(payload),
            state=state,
            repo_url=self.binding.repo_url,
        )


class TeamAILayer:
    authority = "teamai"
    layer = "collaboration"

    def __init__(
        self,
        config: Config,
        binding: TeamAIBinding,
        node_executable: Path,
        entrypoint: Path,
        literal_recall_wrapper: Path | None = None,
    ) -> None:
        self.config = config
        self.binding = binding
        self.node_executable = _executable(node_executable, "Node")
        self.entrypoint = _file(entrypoint, "TeamAI entrypoint")
        if literal_recall_wrapper is None:
            literal_recall_wrapper = (
                Path(__file__).resolve().parents[2]
                / "vendor/teamai-runtime/project-continuity-literal-recall.mjs"
            )
        self.literal_recall_wrapper = _file(
            literal_recall_wrapper, "TeamAI literal recall wrapper"
        )
        self.root_base = config.paths.data_root / "team"

    def status(self, principal_id: str, project_id: str) -> Mapping[str, Any]:
        del principal_id
        deadline = _deadline()
        root, revision = self._root(project_id, deadline=deadline)
        reviewed = self._reviewed_files(root, deadline=deadline)
        return {
            "reviewed_objects": len(reviewed),
            "team_id": self.binding.team_id,
            "version": revision,
        }

    def search(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        *,
        limit: int,
        selector: str = "",
    ) -> Sequence[Mapping[str, Any]]:
        if selector:
            raise AuthorityLayerError("teamai_selector_unsupported")
        deadline = _deadline()
        root, _revision = self._root(project_id, deadline=deadline)
        identity = resolve_teamai_identity(self.config, principal_id, project_id)
        normalized_query = _query(query)
        with self._runtime_identity(
            root, identity, deadline=deadline
        ) as (environment, invocation_root):
            recall = _command(
                [
                    str(self.node_executable),
                    str(self.literal_recall_wrapper),
                    str(self.entrypoint),
                ],
                cwd=invocation_root,
                label="TeamAI",
                environment=environment,
                timeout=_remaining_timeout(deadline),
                input_text=teamai_readonly_recall_request(normalized_query),
            )
        recall_hits = _teamai_recall_hit_count(recall, normalized_query)
        needle = query.casefold()
        refs = []
        for relative, reference, title, content in self._reviewed_files(
            root, deadline=deadline
        ):
            if needle not in relative.casefold() and needle not in content.casefold():
                continue
            refs.append(
                {
                    "path": relative,
                    "stable_ref": reference.as_dict(),
                    "title": title,
                }
            )
            if len(refs) >= limit:
                break
        if recall_hits == 0 and not refs:
            return []
        return [
            {
                "recall": sanitize_evidence(recall, max_string=8_000),
                "recall_hit_count": recall_hits,
                "reviewed_matches": refs,
            }
        ]

    def get(
        self, principal_id: str, project_id: str, reference: StableRef
    ) -> Mapping[str, Any]:
        del principal_id
        deadline = _deadline()
        root, _revision = self._root(project_id, deadline=deadline)
        relative = dict(reference.provenance).get("relative_path")
        if not isinstance(relative, str):
            raise AuthorityLayerError("teamai_reference_has_no_path")
        pull_request = _reviewed_merge_at(
            root, reference.version, relative, deadline=deadline
        )
        content_bytes = _git_bytes(
            root,
            "show",
            reference.version + ":" + relative,
            timeout=_remaining_timeout(deadline),
        )
        current, title, content = self._reviewed_record(
            root,
            relative,
            reference.version,
            pull_request,
            content_bytes,
        )
        if current != reference:
            raise AuthorityLayerError("teamai_reference_changed")
        return {
            "content": sanitize_evidence(content, max_string=100_000),
            "path": relative,
            "stable_ref": current.as_dict(),
            "title": title,
        }

    def update(
        self,
        principal_id: str,
        project_id: str,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        expected_revision: str,
    ) -> Mapping[str, Any]:
        """Contribute one collaboration candidate through TeamAI's PR flow."""

        if operation != "contribute":
            raise AuthorityLayerError("teamai_update_operation_unsupported")
        title, body = _teamai_contribution_arguments(arguments)
        root, revision = self._root(project_id)
        if expected_revision != revision:
            raise AuthorityLayerError("teamai_expected_revision_conflict")
        identity = resolve_teamai_identity(self.config, principal_id, project_id)
        runtime = self.config.paths.data_root / "truth-plane/teamai-runtime" / project_id
        _private_directory(runtime)
        contribution = runtime / (
            "contribution-%s.md"
            % _request_digest(
                {
                    "body": body,
                    "expected_revision": expected_revision,
                    "project_id": project_id,
                    "title": title,
                }
            ).removeprefix("sha256:")
        )
        try:
            contribution.write_text(body, encoding="utf-8")
            contribution.chmod(0o600)
            with self._runtime_identity(
                root, identity, remote_auth=True
            ) as (environment, invocation_root):
                completed = _run_command(
                    [
                        str(self.node_executable),
                        str(self.entrypoint),
                        "contribute",
                        "--file",
                        str(contribution),
                        "--title",
                        title,
                        "--scope",
                        "project",
                    ],
                    cwd=invocation_root,
                    label="TeamAI",
                    environment=environment,
                    timeout=WRITE_TIMEOUT_SECONDS,
                )
        finally:
            contribution.unlink(missing_ok=True)
        output = completed.stdout + "\n" + completed.stderr
        try:
            receipt = classify_teamai_publish(
                completed.returncode,
                output,
                expected_repo_url=self.binding.repo_url,
            )
        except TeamAIContractError as exc:
            raise AuthorityLayerError("teamai_publish_unverified") from exc
        _clean_checkout(root)
        if _git(root, "rev-parse", "HEAD") != revision:
            raise AuthorityLayerError("teamai_active_checkout_changed")
        return {
            "actor": identity.actor_id,
            "branch": receipt.branch,
            "changed": True,
            "ok": True,
            "operation": operation,
            "pull_request": receipt.pull_request,
            "pull_request_url": receipt.pull_request_url,
            "review_state": receipt.state,
            "source_revision": revision,
        }

    def _root(
        self, project_id: str, *, deadline: float | None = None
    ) -> Tuple[Path, str]:
        self.config.project(project_id)
        root = _managed_repo(
            self.root_base, project_id, self.binding.repo_url, deadline=deadline
        )
        _clean_checkout(root, deadline=deadline)
        verify_teamai_guard_documents(
            root,
            expected_team_id=self.binding.team_id,
            expected_repo_url=self.binding.repo_url,
            expected_reviewers=self.binding.reviewers,
        )
        assert_no_teamai_implicit_inputs(root)
        return root, _git(
            root, "rev-parse", "HEAD", timeout=_remaining_timeout(deadline)
        )

    def _reviewed_files(
        self, root: Path, *, deadline: float | None = None
    ) -> Sequence[Tuple[str, StableRef, str, str]]:
        results = []
        for directory, kind in _TEAMAI_KINDS.items():
            base = root / _TEAMAI_PATHS[directory]
            if not base.exists():
                continue
            _inside(base, root)
            for path in sorted(base.rglob("*.md")):
                _inside(path, root)
                if path.is_symlink() or not path.is_file():
                    raise AuthorityLayerError("teamai_reviewed_path_unsafe")
                relative = path.relative_to(root).as_posix()
                merge_revision, pull_request = _reviewed_merge(
                    root, relative, deadline=deadline
                )
                content_bytes = _git_bytes(
                    root,
                    "show",
                    merge_revision + ":" + relative,
                    timeout=_remaining_timeout(deadline),
                )
                if path.read_bytes() != content_bytes:
                    continue
                reference, title, content = self._reviewed_record(
                    root,
                    relative,
                    merge_revision,
                    pull_request,
                    content_bytes,
                )
                results.append((relative, reference, title, content))
        return tuple(results)

    def _reviewed_record(
        self,
        root: Path,
        relative: str,
        merge_revision: str,
        pull_request: int,
        content_bytes: bytes,
    ) -> Tuple[StableRef, str, str]:
        try:
            content = content_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AuthorityLayerError("teamai_reviewed_content_not_utf8") from exc
        if len(content_bytes) > MAX_WRITE_BODY:
            raise AuthorityLayerError("teamai_reviewed_content_too_large")
        directory = relative.split("/", 2)[1]
        kind = _TEAMAI_KINDS.get(directory)
        if kind is None:
            raise AuthorityLayerError("teamai_reviewed_path_unsupported")
        actor = _frontmatter(content, "author") or "reviewed-git-import"
        object_id = sha256(relative.encode("utf-8")).hexdigest()[:24]
        reference = teamai_reviewed_ref(
            project_id=root.name,
            object_kind=kind,
            object_id=object_id,
            revision=merge_revision,
            artifact_digest=_digest(content_bytes),
            repo_url=self.binding.repo_url,
            relative_path=relative,
            actor_id=_safe_actor(actor),
            endpoint_id="reviewed-git-import",
            pull_request=pull_request,
        )
        title = _frontmatter(content, "title") or Path(relative).stem
        return reference, title, content

    @contextmanager
    def _runtime_identity(
        self,
        root: Path,
        identity: Any,
        *,
        deadline: float | None = None,
        remote_auth: bool = False,
    ):
        """Render the donor's ignored project config under a process-wide lock."""

        runtime = self.config.paths.data_root / "truth-plane/teamai-runtime" / root.name
        _private_directory(runtime)
        lock_path = runtime / "command.lock"
        with lock_path.open("a+b") as lock:
            os.fchmod(lock.fileno(), 0o600)
            _teamai_lock(lock, deadline=deadline)
            ignored = _run_command(
                ["git", "check-ignore", "-q", ".teamai/config.yaml"],
                cwd=root,
                label="TeamAI",
                environment=_managed_git_environment(),
                timeout=_remaining_timeout(deadline),
            )
            if ignored.returncode != 0:
                raise AuthorityLayerError("teamai_local_config_not_ignored")
            invocation_root = Path(tempfile.mkdtemp(prefix="teamai-", dir=runtime))
            config_path = invocation_root / ".teamai/config.yaml"
            config_path.parent.mkdir(mode=0o700)
            local_config = {
                "additionalRoles": [],
                "inheritUserScope": False,
                "projectRoot": str(invocation_root),
                "recallEnabled": False,
                "repo": {
                    "businessRepoRoot": str(root),
                    "kind": "self",
                    "localPath": str(root / ".teamai"),
                    "remote": self.binding.repo_url,
                },
                "scope": "project",
                "updatePolicy": "skip",
                "username": identity.username,
            }
            config_path.write_text(
                json.dumps(local_config, ensure_ascii=False, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            config_path.chmod(0o600)
            environment = (
                _managed_git_environment(self.binding.repo_url)
                if remote_auth
                else teamai_explicit_environment()
            )
            if remote_auth:
                environment.update(teamai_explicit_environment())
            environment.update(
                {
                    "GIT_AUTHOR_EMAIL": identity.actor_id
                    + "@project-continuity.invalid",
                    "GIT_AUTHOR_NAME": identity.actor_id,
                    "GIT_COMMITTER_EMAIL": identity.actor_id
                    + "@project-continuity.invalid",
                    "GIT_COMMITTER_NAME": identity.actor_id,
                    "HOME": str(runtime),
                    "TEAMAI_CACHE_DIR": str(invocation_root / ".teamai/cache/repos"),
                }
            )
            try:
                yield environment, invocation_root
            finally:
                shutil.rmtree(invocation_root, ignore_errors=True)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


class GitHubDeliveryLayer:
    authority = "github"
    layer = "delivery"

    def __init__(self, config: Config, resolver: GitHubAuthorityResolver) -> None:
        self.config = config
        self.resolver = resolver
        self.root_base = config.paths.data_root / "delivery"

    def status(self, principal_id: str, project_id: str) -> Mapping[str, Any]:
        del principal_id
        deadline = time.monotonic() + 15
        try:
            root, revision = self._root(project_id, deadline=deadline)
            reference, metadata = self._commit(project_id, revision, deadline)
            repo_url = self.config.project(project_id).repo_url
            return {
                "current": reference.as_dict(),
                "merged_pull_requests": len(
                    self.resolver.pull_requests(repo_url, deadline=deadline)
                ),
                "releases": len(
                    self.resolver.releases(repo_url, deadline=deadline)
                ),
                "subject": metadata["subject"],
            }
        except GitHubResolverUnavailable as exc:
            raise AuthorityLayerUnavailable(str(exc)) from exc
        except GitHubResolverError as exc:
            raise AuthorityLayerError(str(exc)) from exc

    def search(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        *,
        limit: int,
        selector: str = "",
    ) -> Sequence[Mapping[str, Any]]:
        del principal_id
        if selector:
            raise AuthorityLayerError("github_selector_unsupported")
        deadline = time.monotonic() + 15
        try:
            root, _revision = self._root(project_id, deadline=deadline)
            repo_url = self.config.project(project_id).repo_url
            needle = _query(query).casefold()
            results = []
            for metadata in self.resolver.pull_requests(repo_url, deadline=deadline):
                if needle not in _searchable(metadata):
                    continue
                reference = self._pull_request_ref(project_id, metadata)
                results.append({"stable_ref": reference.as_dict(), **metadata})
                if len(results) >= limit:
                    return results
            for summary in self.resolver.releases(repo_url, deadline=deadline):
                if needle not in _searchable(summary):
                    continue
                metadata = self.resolver.release(
                    repo_url, summary["tag"], deadline=deadline
                )
                reference = self._release_ref(project_id, metadata)
                results.append({"stable_ref": reference.as_dict(), **metadata})
                if len(results) >= limit:
                    return results
            for revision, subject, body in _git_records(
                root, max(limit * 6, 30), deadline=deadline
            ):
                if needle not in (subject + "\n" + body).casefold():
                    continue
                reference, metadata = self._commit(project_id, revision, deadline)
                if needle not in _searchable(metadata):
                    continue
                results.append({"stable_ref": reference.as_dict(), **metadata})
                if len(results) >= limit:
                    break
            return results
        except GitHubResolverUnavailable as exc:
            raise AuthorityLayerUnavailable(str(exc)) from exc
        except GitHubResolverError as exc:
            raise AuthorityLayerError(str(exc)) from exc

    def get(
        self, principal_id: str, project_id: str, reference: StableRef
    ) -> Mapping[str, Any]:
        del principal_id
        deadline = time.monotonic() + 15
        try:
            self._root(project_id, deadline=deadline)
            repo_url = self.config.project(project_id).repo_url
            commit_prefix = "commit:%s:" % project_id
            pull_prefix = "pull-request:%s:" % project_id
            release_prefix = "release:%s:" % project_id
            if reference.object_id.startswith(commit_prefix):
                revision = reference.object_id[len(commit_prefix) :]
                current, metadata = self._commit(project_id, revision, deadline)
            elif reference.object_id.startswith(pull_prefix):
                pull_request = int(reference.object_id[len(pull_prefix) :])
                metadata = self.resolver.pull_request(
                    repo_url, pull_request, deadline=deadline
                )
                current = self._pull_request_ref(project_id, metadata)
            elif reference.object_id.startswith(release_prefix):
                tag = reference.object_id[len(release_prefix) :]
                metadata = self.resolver.release(repo_url, tag, deadline=deadline)
                current = self._release_ref(project_id, metadata)
            else:
                raise AuthorityLayerError("delivery_reference_project_mismatch")
            if current != reference:
                raise AuthorityLayerError("delivery_reference_changed")
            return {"stable_ref": current.as_dict(), **metadata}
        except (ValueError, GitHubResolverError) as exc:
            if isinstance(exc, GitHubResolverUnavailable):
                raise AuthorityLayerUnavailable(str(exc)) from exc
            if isinstance(exc, AuthorityLayerError):
                raise
            raise AuthorityLayerError(str(exc)) from exc

    def update(
        self,
        principal_id: str,
        project_id: str,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        expected_revision: str,
    ) -> Mapping[str, Any]:
        del principal_id, project_id, operation, arguments, expected_revision
        raise AuthorityLayerError("delivery_is_read_only")

    def _root(
        self, project_id: str, *, deadline: float | None = None
    ) -> Tuple[Path, str]:
        project = self.config.project(project_id)
        root = _managed_repo(
            self.root_base, project_id, project.repo_url, deadline=deadline
        )
        _clean_checkout(root, deadline=deadline)
        return root, _git(
            root, "rev-parse", "HEAD", timeout=_remaining_timeout(deadline)
        )

    def _commit(
        self, project_id: str, revision: str, deadline: float
    ) -> Tuple[StableRef, Mapping[str, Any]]:
        repo_url = self.config.project(project_id).repo_url
        metadata = self.resolver.commit(
            repo_url, revision, deadline=deadline
        )
        reference = github_delivery_ref(
            project_id=project_id,
            revision=revision,
            artifact_digest=canonical_digest(metadata),
            repo_url=repo_url,
            subject=metadata["subject"],
        )
        return reference, metadata

    def _pull_request_ref(
        self, project_id: str, metadata: Mapping[str, Any]
    ) -> StableRef:
        return github_pull_request_ref(
            project_id=project_id,
            pull_request=metadata["pull_request"],
            merge_revision=metadata["revision"],
            artifact_digest=canonical_digest(metadata),
            repo_url=self.config.project(project_id).repo_url,
            subject=metadata["subject"],
        )

    def _release_ref(
        self, project_id: str, metadata: Mapping[str, Any]
    ) -> StableRef:
        return github_release_ref(
            project_id=project_id,
            tag=metadata["tag"],
            revision=metadata["revision"],
            artifact_digest=canonical_digest(metadata),
            repo_url=self.config.project(project_id).repo_url,
        )


def _openspec_change_arguments(
    value: Mapping[str, Any],
) -> Tuple[str, Tuple[Mapping[str, str], ...]]:
    if not isinstance(value, dict) or set(value) != {"artifacts", "change_id"}:
        raise AuthorityLayerError("openspec_change_arguments_malformed")
    change_id = value.get("change_id")
    if not isinstance(change_id, str) or not _CHANGE_ID.fullmatch(change_id):
        raise AuthorityLayerError("openspec_change_id_malformed")
    raw_artifacts = value.get("artifacts")
    if not isinstance(raw_artifacts, list) or not 1 <= len(raw_artifacts) <= 12:
        raise AuthorityLayerError("openspec_artifacts_malformed")
    artifacts = []
    seen = set()
    for item in raw_artifacts:
        if not isinstance(item, dict) or set(item) != {
            "artifact_id",
            "body",
            "relative_output",
        }:
            raise AuthorityLayerError("openspec_artifacts_malformed")
        artifact_id = item.get("artifact_id")
        body = item.get("body")
        relative = item.get("relative_output")
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID.fullmatch(artifact_id):
            raise AuthorityLayerError("openspec_artifact_id_malformed")
        _reviewed_body(body, "openspec_artifact_body")
        _relative_posix(relative, "openspec_relative_output")
        if artifact_id in seen or relative in {
            row["relative_output"] for row in artifacts
        }:
            raise AuthorityLayerError("openspec_artifacts_duplicate")
        seen.add(artifact_id)
        artifacts.append(
            {
                "artifact_id": artifact_id,
                "body": body,
                "relative_output": relative,
            }
        )
    return change_id, tuple(artifacts)


def _openspec_archive_arguments(value: Mapping[str, Any]) -> str:
    if not isinstance(value, dict) or set(value) != {"change_id"}:
        raise AuthorityLayerError("openspec_archive_arguments_malformed")
    change_id = value.get("change_id")
    if not isinstance(change_id, str) or not _CHANGE_ID.fullmatch(change_id):
        raise AuthorityLayerError("openspec_change_id_malformed")
    return change_id


def _teamai_contribution_arguments(value: Mapping[str, Any]) -> Tuple[str, str]:
    if not isinstance(value, dict) or set(value) != {"body", "title"}:
        raise AuthorityLayerError("teamai_contribution_arguments_malformed")
    title = value.get("title")
    body = value.get("body")
    if (
        not isinstance(title, str)
        or not title
        or title != title.strip()
        or len(title) > 160
        or "\n" in title
        or "\r" in title
    ):
        raise AuthorityLayerError("teamai_contribution_title_malformed")
    _reviewed_body(body, "teamai_contribution_body")
    return title, body


def _reviewed_body(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value.encode("utf-8")) > MAX_WRITE_BODY
        or "\x00" in value
    ):
        raise AuthorityLayerError(field + "_malformed")
    sanitized = sanitize_evidence(value, max_string=max(len(value), 1))
    if sanitized != value:
        raise AuthorityLayerError(field + "_contains_sensitive_material")
    return value


def _relative_posix(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 500
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise AuthorityLayerError(field + "_malformed")
    return value


def _openspec_artifact_path(
    worktree: Path,
    change_dir: str,
    output_pattern: str,
    relative_output: str,
) -> Path:
    import fnmatch

    relative = _relative_posix(relative_output, "openspec_relative_output")
    if not fnmatch.fnmatchcase(relative, output_pattern):
        raise AuthorityLayerError("openspec_relative_output_mismatch")
    raw_change = Path(change_dir)
    change_root = raw_change if raw_change.is_absolute() else worktree / raw_change
    try:
        resolved_change = change_root.resolve(strict=True)
        resolved_change.relative_to(worktree.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise AuthorityLayerError("openspec_change_path_unsafe") from exc
    destination = resolved_change / relative
    _inside(destination, resolved_change)
    return destination


def _request_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _digest(encoded)


@contextmanager
def _isolated_worktree(
    root: Path,
    revision: str,
    actor: str,
    *,
    deadline: float | None = None,
):
    parent = root.parent / ".project-continuity-worktrees"
    _private_directory(parent)
    temporary = Path(tempfile.mkdtemp(prefix="openspec-%s-" % actor, dir=parent))
    checkout = temporary / "checkout"
    try:
        _git(
            root,
            "worktree",
            "add",
            "--detach",
            "--",
            str(checkout),
            revision,
            timeout=_remaining_timeout(deadline),
        )
        yield checkout
    finally:
        remove = subprocess.run(
            [
                "git",
                "worktree",
                "remove",
                "--force",
                "--force",
                "--",
                str(checkout),
            ],
            cwd=root,
            env=_managed_git_environment(),
            check=False,
            capture_output=True,
            timeout=30,
        )
        shutil.rmtree(temporary, ignore_errors=True)
        prune = subprocess.run(
            ["git", "worktree", "prune", "--expire", "now"],
            cwd=root,
            env=_managed_git_environment(),
            check=False,
            capture_output=True,
            timeout=30,
        )
        listed = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=root,
            env=_managed_git_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if (
            prune.returncode != 0
            or listed.returncode != 0
            or ("worktree " + str(checkout)) in listed.stdout.splitlines()
        ):
            raise AuthorityLayerError("openspec_worktree_cleanup_failed")


def _commit_worktree(
    worktree: Path, *, actor: str, subject: str, request_digest: str
) -> str:
    _git(worktree, "add", "--", "openspec")
    if not _git(worktree, "status", "--porcelain=v1", "--", "openspec"):
        raise AuthorityLayerError("openspec_update_created_no_change")
    environment = _managed_git_environment()
    environment.update(
        {
            "GIT_AUTHOR_EMAIL": actor + "@project-continuity.invalid",
            "GIT_AUTHOR_NAME": actor,
            "GIT_COMMITTER_EMAIL": actor + "@project-continuity.invalid",
            "GIT_COMMITTER_NAME": actor,
        }
    )
    _command(
        [
            "git",
            "commit",
            "-m",
            subject,
            "-m",
            "ProjectContinuity-Request: " + request_digest,
        ],
        cwd=worktree,
        label="Git",
        environment=environment,
        timeout=WRITE_TIMEOUT_SECONDS,
    )
    return _git(worktree, "rev-parse", "HEAD")


def _push_branch(
    root: Path, worktree: Path, branch: str, expected_remote: str
) -> None:
    _managed_git_config(root, expected_remote)
    _command(
        ["git", "push", expected_remote, "HEAD:refs/heads/" + branch],
        cwd=worktree,
        label="Git",
        environment=_managed_git_environment(expected_remote),
        timeout=WRITE_TIMEOUT_SECONDS,
    )


def _remote_branch(root: Path, branch: str, expected_remote: str) -> str | None:
    _managed_git_config(root, expected_remote)
    output = _command(
        [
            "git",
            "ls-remote",
            "--heads",
            expected_remote,
            "refs/heads/" + branch,
        ],
        cwd=root,
        label="Git",
        environment=_managed_git_environment(expected_remote),
        timeout=WRITE_TIMEOUT_SECONDS,
    ).strip()
    if not output:
        return None
    fields = output.split("\t")
    if len(fields) != 2 or not _COMMIT.fullmatch(fields[0]):
        raise AuthorityLayerError("git_remote_branch_malformed")
    _command(
        ["git", "fetch", "--no-tags", expected_remote, fields[0]],
        cwd=root,
        label="Git",
        environment=_managed_git_environment(expected_remote),
        timeout=WRITE_TIMEOUT_SECONDS,
    )
    return fields[0]


def _assert_request_commit(root: Path, revision: str, request_digest: str) -> None:
    body = _git(root, "show", "-s", "--format=%B", revision)
    marker = "ProjectContinuity-Request: " + request_digest
    if marker not in body.splitlines():
        raise AuthorityLayerError("authority_branch_request_conflict")


def _private_directory(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise AuthorityLayerError("managed_path_contains_symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    stat = path.stat()
    if not path.is_dir() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise AuthorityLayerError("managed_directory_unsafe")


def _managed_repo(
    base: Path,
    project_id: str,
    expected_remote: str,
    *,
    deadline: float | None = None,
) -> Path:
    root = base / project_id
    _inside(root, base)
    if root.is_symlink() or not root.is_dir():
        raise AuthorityLayerUnavailable("managed_repo_unavailable")
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o022:
        raise AuthorityLayerError("managed_repo_unsafe")
    git_dir = root / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise AuthorityLayerError("managed_repo_git_unavailable")
    _managed_git_config(root, expected_remote)
    return root


def _clean_checkout(root: Path, *, deadline: float | None = None) -> None:
    if _git(
        root,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        timeout=_remaining_timeout(deadline),
    ):
        raise AuthorityLayerError("managed_repo_is_dirty")


def _reviewed_merge(
    root: Path, relative: str, *, deadline: float | None = None
) -> Tuple[str, int]:
    output = _git(
        root,
        "log",
        "--first-parent",
        "--merges",
        "--format=%H%x00%s",
        timeout=_remaining_timeout(deadline),
    )
    for line in output.splitlines():
        fields = line.split("\x00", 1)
        if len(fields) != 2 or not _COMMIT.fullmatch(fields[0]):
            continue
        match = _PULL_REQUEST.match(fields[1])
        if match is None:
            continue
        changed = _git(
            root,
            "diff",
            "--name-only",
            fields[0] + "^1",
            fields[0],
            timeout=_remaining_timeout(deadline),
        )
        if relative in changed.splitlines():
            return fields[0], int(match.group(1))
    raise AuthorityLayerError("teamai_object_has_no_reviewed_merge")


def _reviewed_merge_at(
    root: Path,
    revision: str,
    relative: str,
    *,
    deadline: float | None = None,
) -> int:
    if not _COMMIT.fullmatch(revision):
        raise AuthorityLayerError("teamai_review_revision_malformed")
    subject = _git(
        root,
        "show",
        "-s",
        "--format=%s",
        revision,
        timeout=_remaining_timeout(deadline),
    )
    match = _PULL_REQUEST.match(subject)
    parents = _git(
        root,
        "show",
        "-s",
        "--format=%P",
        revision,
        timeout=_remaining_timeout(deadline),
    ).split()
    if match is None or len(parents) < 2:
        raise AuthorityLayerError("teamai_reference_not_reviewed_merge")
    changed = _git(
        root,
        "diff",
        "--name-only",
        revision + "^1",
        revision,
        timeout=_remaining_timeout(deadline),
    )
    if relative not in changed.splitlines():
        raise AuthorityLayerError("teamai_reference_path_not_reviewed")
    return int(match.group(1))


def _git_records(
    root: Path, limit: int, *, deadline: float | None = None
) -> Sequence[Tuple[str, str, str]]:
    output = _git(
        root,
        "log",
        "--all",
        "-n",
        str(min(limit, 300)),
        "--format=%H%x00%s%x00%b%x1e",
        timeout=_remaining_timeout(deadline),
    )
    records = []
    for raw in output.split("\x1e"):
        raw = raw.strip("\n")
        if not raw:
            continue
        fields = raw.split("\x00", 2)
        if len(fields) == 3 and _COMMIT.fullmatch(fields[0]):
            records.append(tuple(fields))
    return tuple(records)


def _searchable(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True).casefold()


def _deadline() -> float:
    return time.monotonic() + READ_DEADLINE_SECONDS


def _remaining_timeout(deadline: float | None) -> float:
    if deadline is None:
        return 30
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise AuthorityLayerUnavailable("authority_read_timeout")
    return remaining


def _teamai_lock(lock: Any, *, deadline: float | None) -> None:
    if deadline is None:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return
    while True:
        if time.monotonic() >= deadline:
            raise AuthorityLayerUnavailable("teamai_timeout")
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            time.sleep(min(0.01, max(0.0, deadline - time.monotonic())))


def _git(root: Path, *arguments: str, timeout: float = 30) -> str:
    return _command(
        ["git", *arguments],
        cwd=root,
        label="Git",
        environment=_managed_git_environment(),
        timeout=timeout,
    ).rstrip("\n")


def _git_bytes(root: Path, *arguments: str, timeout: float = 30) -> bytes:
    environment = _managed_git_environment()
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            env=environment,
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AuthorityLayerUnavailable("git_timeout") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuthorityLayerError("git_command_failed") from exc
    if completed.returncode != 0 or len(completed.stdout) > MAX_COMMAND_OUTPUT:
        raise AuthorityLayerError("git_command_failed")
    return completed.stdout


def _managed_git_config(root: Path, expected_remote: str) -> ManagedGitConfig:
    try:
        return inspect_managed_git_config(root, expected_remote)
    except ManagedGitError as exc:
        if exc.code == "managed_git_remote_conflict":
            raise AuthorityLayerError("managed_repo_remote_mismatch") from exc
        raise AuthorityLayerError("managed_repo_git_config_unsafe") from exc


def _managed_git_environment(remote: str | None = None) -> Dict[str, str]:
    try:
        return managed_git_environment(remote)
    except ManagedGitError as exc:
        raise AuthorityLayerError("managed_git_environment_unsafe") from exc


def _json_command(
    command: Sequence[str],
    arguments: Sequence[str],
    *,
    cwd: Path,
    label: str,
    timeout: float = 30,
) -> Mapping[str, Any]:
    output = _command(
        [*command, *arguments], cwd=cwd, label=label, timeout=timeout
    )
    try:
        value = json.loads(output, object_pairs_hook=_reject_duplicate_keys)
    except (json.JSONDecodeError, AuthorityLayerError) as exc:
        raise AuthorityLayerError(label.lower() + "_json_malformed") from exc
    if not isinstance(value, dict):
        raise AuthorityLayerError(label.lower() + "_json_malformed")
    return value


def _command(
    command: Sequence[str],
    *,
    cwd: Path,
    label: str,
    environment: Mapping[str, str] | None = None,
    timeout: float = 30,
    input_text: str | None = None,
) -> str:
    completed = _run_command(
        command,
        cwd=cwd,
        label=label,
        environment=environment,
        timeout=timeout,
        input_text=input_text,
    )
    if completed.returncode != 0:
        raise AuthorityLayerError(label.lower() + "_command_failed")
    return completed.stdout


def _run_command(
    command: Sequence[str],
    *,
    cwd: Path,
    label: str,
    environment: Mapping[str, str] | None = None,
    timeout: float = 30,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = _safe_environment()
    if environment:
        env.update(environment)
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=env,
            check=False,
            capture_output=True,
            text=True,
            input=input_text,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AuthorityLayerUnavailable(label.lower() + "_timeout") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuthorityLayerError(label.lower() + "_command_failed") from exc
    if len(completed.stdout) > MAX_COMMAND_OUTPUT or len(completed.stderr) > MAX_COMMAND_OUTPUT:
        raise AuthorityLayerError(label.lower() + "_command_failed")
    return completed


def _safe_environment() -> Dict[str, str]:
    allowed = {
        name: value
        for name, value in os.environ.items()
        if name in {"HOME", "LANG", "LC_ALL", "PATH", "TMPDIR"}
    }
    allowed.update(
        {
            "CI": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "NO_COLOR": "1",
        }
    )
    return allowed


def _probe_version(command: Sequence[str], version: str, label: str) -> None:
    output = _command(
        [*command, "--version"], cwd=Path(command[-1]).parent, label=label
    )
    if version not in output.strip().split():
        raise AuthorityLayerError(label.lower() + "_version_mismatch")


def _executable(path: Path, label: str) -> Path:
    value = Path(path)
    if value.is_symlink() or not value.is_file() or not os.access(value, os.X_OK):
        raise AuthorityLayerError(label.lower() + "_executable_unavailable")
    return value


def _file(path: Path, label: str) -> Path:
    value = Path(path)
    if value.is_symlink() or not value.is_file():
        raise AuthorityLayerError(label.lower().replace(" ", "_") + "_unavailable")
    return value


def _inside(path: Path, root: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise AuthorityLayerError("managed_path_contains_symlink")
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise AuthorityLayerError("managed_path_escapes_root") from exc


def _query(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 1_000
        or any(ord(character) < 32 and character not in "\n\t" for character in value)
    ):
        raise AuthorityLayerError("query_malformed")
    return value


def _teamai_recall_hit_count(output: str, query: str) -> int:
    """Read the exact donor-owned result count instead of treating text as a hit."""

    if output == 'ℹ No matching learnings found for "%s".\n' % query:
        return 0
    matches = _TEAMAI_RECALL_HEADER.findall(output)
    if len(matches) != 1 or "--- [teamai:recall:end] ---" not in output:
        raise AuthorityLayerError("teamai_recall_output_malformed")
    count = int(matches[0])
    if count > MAX_ITEMS:
        raise AuthorityLayerError("teamai_recall_output_malformed")
    return count


def _openspec_public_payload(
    payload: Mapping[str, Any],
    *,
    project_id: str,
    store_id: str,
    item_id: str,
) -> Dict[str, Any]:
    """Replace donor checkout paths with one stable, client-safe identity."""

    public = dict(payload)
    root = public.get("root")
    if isinstance(root, dict) and "path" in root:
        public_root = dict(root)
        public_root["path"] = "openspec://%s/%s/%s" % (
            project_id,
            store_id,
            item_id,
        )
        public["root"] = public_root
    return public


def _json_digest(value: Mapping[str, Any]) -> str:
    return _digest(
        json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    )


def _digest(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _bounded_preview(value: Mapping[str, Any]) -> Any:
    return sanitize_evidence(value, max_depth=4, max_items=30, max_string=1_000)


def _reject_duplicate_keys(pairs: Sequence[Tuple[str, Any]]) -> Mapping[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AuthorityLayerError("duplicate_json_key")
        result[key] = value
    return result


def _frontmatter(content: str, key: str) -> str | None:
    if not content.startswith("---\n"):
        return None
    end = content.find("\n---\n", 4)
    if end < 0:
        return None
    pattern = re.compile(r"^%s:\s*[\"']?([^\n\"']+)[\"']?\s*$" % re.escape(key), re.M)
    match = pattern.search(content[4:end])
    return match.group(1).strip() if match else None


def _safe_actor(value: str) -> str:
    normalized = value.strip().lower().replace("_", "-")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", normalized):
        return "reviewed-git-import"
    return normalized
