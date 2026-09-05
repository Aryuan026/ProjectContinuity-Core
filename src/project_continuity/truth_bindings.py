"""Operator-owned routing bindings for donor stores with independent Git remotes."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .config import Config, _identifier, _repository_url


SCHEMA_VERSION = 1
BINDINGS_RELATIVE_PATH = Path("truth-plane/bindings.json")


class TruthBindingError(ValueError):
    """The operator routing projection is absent, unsafe, or malformed."""


@dataclass(frozen=True)
class OpenSpecBinding:
    store_id: str
    repo_url: str


@dataclass(frozen=True)
class TeamAIBinding:
    team_id: str
    repo_url: str
    reviewers: Tuple[str, ...]


@dataclass(frozen=True)
class ProjectTruthBinding:
    project_id: str
    openspec: Optional[OpenSpecBinding] = None
    teamai: Optional[TeamAIBinding] = None


class TruthBindings:
    """Read-only projection telling the front where donor-owned Git stores live."""

    def __init__(self, projects: Sequence[ProjectTruthBinding] = ()) -> None:
        frozen_projects = tuple(projects)
        self._projects = {
            project.project_id: project for project in frozen_projects
        }
        if len(self._projects) != len(frozen_projects):
            raise TruthBindingError("truth binding project ids must be unique")

    def project(self, project_id: str) -> ProjectTruthBinding:
        return self._projects.get(project_id, ProjectTruthBinding(project_id))

    def project_ids(self) -> Tuple[str, ...]:
        return tuple(sorted(self._projects))

    def as_dict(self) -> Dict[str, Any]:
        projects: Dict[str, Any] = {}
        for project_id, binding in sorted(self._projects.items()):
            value: Dict[str, Any] = {}
            if binding.openspec is not None:
                value["openspec"] = {
                    "repo_url": binding.openspec.repo_url,
                    "store_id": binding.openspec.store_id,
                }
            if binding.teamai is not None:
                value["teamai"] = {
                    "repo_url": binding.teamai.repo_url,
                    "reviewers": list(binding.teamai.reviewers),
                    "team_id": binding.teamai.team_id,
                }
            projects[project_id] = value
        return {"projects": projects, "schema_version": SCHEMA_VERSION}


def load_truth_bindings(config: Config) -> TruthBindings:
    """Load the exact private projection; no file means no donor-store bindings."""

    path = config.paths.data_root / BINDINGS_RELATIVE_PATH
    if not os.path.lexists(str(path)):
        return TruthBindings()
    _safe_private_file(path, config.paths.data_root)
    try:
        raw = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TruthBindingError("cannot read truth bindings") from exc
    if not isinstance(raw, dict) or set(raw) != {"projects", "schema_version"}:
        raise TruthBindingError("truth bindings have an unsupported shape")
    if raw["schema_version"] != SCHEMA_VERSION:
        raise TruthBindingError("truth bindings schema is unsupported")
    projects_raw = raw["projects"]
    if not isinstance(projects_raw, dict):
        raise TruthBindingError("truth binding projects must be an object")
    configured_projects = {project.project_id for project in config.projects}
    projects = []
    for project_id, value in sorted(projects_raw.items()):
        project = _identifier(project_id, "truth project id")
        if project not in configured_projects:
            raise TruthBindingError("truth binding references unknown project")
        if not isinstance(value, dict) or set(value) - {"openspec", "teamai"}:
            raise TruthBindingError("project truth binding has unknown keys")
        openspec = _parse_openspec(value.get("openspec"))
        teamai = _parse_teamai(value.get("teamai"))
        if openspec is None and teamai is None:
            raise TruthBindingError("empty project truth bindings are forbidden")
        projects.append(ProjectTruthBinding(project, openspec, teamai))
    return TruthBindings(projects)


def truth_bindings_path(config: Config) -> Path:
    return config.paths.data_root / BINDINGS_RELATIVE_PATH


def _parse_openspec(value: Any) -> Optional[OpenSpecBinding]:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"repo_url", "store_id"}:
        raise TruthBindingError("OpenSpec binding has an unsupported shape")
    try:
        store_id = _identifier(value["store_id"], "OpenSpec store_id")
        repo_url = _repository_url(value["repo_url"], "OpenSpec repo_url")
    except ValueError as exc:
        raise TruthBindingError(str(exc)) from exc
    return OpenSpecBinding(store_id, repo_url)


def _parse_teamai(value: Any) -> Optional[TeamAIBinding]:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "repo_url",
        "reviewers",
        "team_id",
    }:
        raise TruthBindingError("TeamAI binding has an unsupported shape")
    reviewers = value["reviewers"]
    if not isinstance(reviewers, list):
        raise TruthBindingError("TeamAI reviewers must be a list")
    try:
        team_id = _identifier(value["team_id"], "TeamAI team_id")
        repo_url = _repository_url(value["repo_url"], "TeamAI repo_url")
        parsed_reviewers = tuple(
            _identifier(item, "TeamAI reviewer") for item in reviewers
        )
    except ValueError as exc:
        raise TruthBindingError(str(exc)) from exc
    if len(set(parsed_reviewers)) != len(parsed_reviewers):
        raise TruthBindingError("TeamAI reviewers must be unique")
    return TeamAIBinding(team_id, repo_url, parsed_reviewers)


def _safe_private_file(path: Path, root: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise TruthBindingError("truth binding path contains a symlink")
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
        stat = path.stat()
    except (OSError, ValueError) as exc:
        raise TruthBindingError("truth binding path is outside data custody") from exc
    if not path.is_file() or stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise TruthBindingError("truth binding file is not owner-private")


def _reject_duplicate_keys(pairs: Sequence[Tuple[str, Any]]) -> Mapping[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TruthBindingError("duplicate truth binding key")
        result[key] = value
    return result
