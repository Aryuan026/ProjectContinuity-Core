"""Strict operator-owned configuration for the contract kernel."""

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Optional, Tuple
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10
    import tomli as tomllib


SCHEMA_VERSION = 1
ROLES = frozenset({"reader", "writer", "promoter"})
_IDENTIFIER = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class ConfigError(ValueError):
    """The operator configuration does not satisfy the frozen contract."""


@dataclass(frozen=True)
class RuntimePaths:
    install_root: Path
    data_root: Path
    state_root: Path


@dataclass(frozen=True)
class ProjectConfig:
    project_id: str
    repo_url: str


@dataclass(frozen=True)
class PrincipalConfig:
    principal_id: str
    actor: str
    roles: Tuple[Tuple[str, str], ...]

    def role_for(self, project_id: str) -> Optional[str]:
        return dict(self.roles).get(project_id)


@dataclass(frozen=True)
class Config:
    paths: RuntimePaths
    projects: Tuple[ProjectConfig, ...]
    principals: Tuple[PrincipalConfig, ...]

    def project(self, project_id: str) -> ProjectConfig:
        for project in self.projects:
            if project.project_id == project_id:
                return project
        raise ConfigError("unknown project_id: %s" % project_id)

    def principal(self, principal_id: str) -> PrincipalConfig:
        for principal in self.principals:
            if principal.principal_id == principal_id:
                return principal
        raise ConfigError("unknown principal_id: %s" % principal_id)


def load_config(path: Path) -> Config:
    """Load one strict TOML configuration without accepting extension keys."""

    config_path = Path(path)
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError("cannot read config: %s" % exc) from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a table")

    _require_keys(raw, {"schema_version", "paths", "projects", "principals"}, "config")
    if type(raw["schema_version"]) is not int or raw["schema_version"] != SCHEMA_VERSION:
        raise ConfigError("schema_version must be %d" % SCHEMA_VERSION)

    paths = _parse_paths(raw["paths"])
    projects = _parse_projects(raw["projects"])
    principals = _parse_principals(raw["principals"], projects)
    return Config(paths=paths, projects=projects, principals=principals)


def _parse_paths(value: Any) -> RuntimePaths:
    table = _as_mapping(value, "paths")
    _require_keys(table, {"install_root", "data_root", "state_root"}, "paths")
    paths = RuntimePaths(
        install_root=_absolute_private_root(table["install_root"], "paths.install_root"),
        data_root=_absolute_private_root(table["data_root"], "paths.data_root"),
        state_root=_absolute_private_root(table["state_root"], "paths.state_root"),
    )
    roots = (paths.install_root, paths.data_root, paths.state_root)
    if len({str(root) for root in roots}) != len(roots):
        raise ConfigError("install_root, data_root, and state_root must be distinct")
    for left in roots:
        for right in roots:
            if left != right and _is_within(left, right):
                raise ConfigError("runtime roots must not contain one another")
    return paths


def _parse_projects(value: Any) -> Tuple[ProjectConfig, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("projects must be a non-empty array of tables")
    projects = []
    seen = set()
    for index, item in enumerate(value):
        table = _as_mapping(item, "projects[%d]" % index)
        _require_keys(table, {"id", "repo_url"}, "projects[%d]" % index)
        project_id = _identifier(table["id"], "projects[%d].id" % index)
        if project_id in seen:
            raise ConfigError("duplicate project id: %s" % project_id)
        seen.add(project_id)
        repo_url = _repository_url(
            table["repo_url"], "projects[%d].repo_url" % index
        )
        projects.append(ProjectConfig(project_id=project_id, repo_url=repo_url))
    return tuple(projects)


def _parse_principals(
    value: Any, projects: Iterable[ProjectConfig]
) -> Tuple[PrincipalConfig, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("principals must be a non-empty array of tables")
    project_ids = {project.project_id for project in projects}
    principals = []
    seen = set()
    for index, item in enumerate(value):
        table = _as_mapping(item, "principals[%d]" % index)
        _require_keys(table, {"id", "actor", "roles"}, "principals[%d]" % index)
        principal_id = _identifier(table["id"], "principals[%d].id" % index)
        if principal_id in seen:
            raise ConfigError("duplicate principal id: %s" % principal_id)
        seen.add(principal_id)
        actor = _identifier(table["actor"], "principals[%d].actor" % index)
        role_table = _as_mapping(table["roles"], "principals[%d].roles" % index)
        if not role_table:
            raise ConfigError("principal roles must not be empty")
        roles = []
        for project_id, role_value in sorted(role_table.items()):
            if project_id not in project_ids:
                raise ConfigError("role references unknown project: %s" % project_id)
            role = _nonempty_string(role_value, "role for %s" % project_id)
            if role not in ROLES:
                raise ConfigError("invalid role for %s: %s" % (project_id, role))
            roles.append((project_id, role))
        principals.append(
            PrincipalConfig(principal_id=principal_id, actor=actor, roles=tuple(roles))
        )
    return tuple(principals)


def _require_keys(table: Mapping[str, Any], expected: set, where: str) -> None:
    actual = set(table)
    unknown = actual - expected
    missing = expected - actual
    if unknown:
        raise ConfigError("unknown %s keys: %s" % (where, ", ".join(sorted(unknown))))
    if missing:
        raise ConfigError("missing %s keys: %s" % (where, ", ".join(sorted(missing))))


def _as_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError("%s must be a table" % where)
    return value


def _nonempty_string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("%s must be a non-empty string" % where)
    return value.strip()


def _identifier(value: Any, where: str) -> str:
    identifier = _nonempty_string(value, where)
    if not _IDENTIFIER.fullmatch(identifier):
        raise ConfigError("%s is not a stable identifier" % where)
    return identifier


def _repository_url(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ConfigError("%s repository URL must be a non-empty trimmed string" % where)
    url = value
    if "\\" in url or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in url
    ):
        raise ConfigError("%s repository URL contains forbidden characters" % where)
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        parsed.port  # force validation of malformed and out-of-range ports
    except ValueError as exc:
        raise ConfigError("%s repository URL is malformed" % where) from exc
    if parsed.scheme != "https" or not hostname:
        raise ConfigError("%s repository URL must use https and include a host" % where)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError(
            "%s repository URL must not contain credentials, query, or fragment" % where
        )
    if not parsed.path.strip("/"):
        raise ConfigError("%s repository URL must include a repository path" % where)
    return url


def _absolute_private_root(value: Any, where: str) -> Path:
    raw = _nonempty_string(value, where)
    path = Path(raw)
    if not path.is_absolute():
        raise ConfigError("%s must be an absolute resolved path" % where)
    resolved = path.resolve(strict=False)
    if resolved == Path(resolved.anchor):
        raise ConfigError("%s cannot be a filesystem root" % where)
    return resolved


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
