"""Derive and validate the one Cognee writable-root contract."""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping

from .config import Config


_REQUIRED = frozenset(
    {
        "PYTHON_DOTENV_DISABLED",
        "GRAPH_DATABASE_PROVIDER",
        "GRAPH_DATABASE_SUBPROCESS_ENABLED",
        "DATA_ROOT_DIRECTORY",
        "SYSTEM_ROOT_DIRECTORY",
        "CACHE_ROOT_DIRECTORY",
        "COGNEE_LOGS_DIR",
    }
)


class RuntimeEnvironmentError(ValueError):
    """The configured archive environment would escape its managed roots."""


def cognee_environment_for_config(config: Config) -> Dict[str, str]:
    """Derive Cognee paths from the operator-approved data and state roots."""

    return {
        "PYTHON_DOTENV_DISABLED": "1",
        "GRAPH_DATABASE_PROVIDER": "ladybug",
        "GRAPH_DATABASE_SUBPROCESS_ENABLED": "false",
        "DATA_ROOT_DIRECTORY": str(config.paths.data_root / "cognee/data"),
        "SYSTEM_ROOT_DIRECTORY": str(config.paths.data_root / "cognee/system"),
        "CACHE_ROOT_DIRECTORY": str(config.paths.data_root / "cognee/cache"),
        "COGNEE_LOGS_DIR": str(config.paths.state_root / "logs/cognee"),
    }


def validate_cognee_environment(
    value: Mapping[str, str], release_root: Path
) -> Dict[str, str]:
    """Fail closed before Cognee can write inside code or through a symlink."""

    if set(value) != _REQUIRED:
        raise RuntimeEnvironmentError("Cognee runtime environment is incomplete")
    environment = dict(value)
    if environment["PYTHON_DOTENV_DISABLED"] != "1":
        raise RuntimeEnvironmentError("Cognee dotenv loading must remain disabled")
    if environment["GRAPH_DATABASE_PROVIDER"] != "ladybug":
        raise RuntimeEnvironmentError("Cognee graph provider must remain ladybug")
    if environment["GRAPH_DATABASE_SUBPROCESS_ENABLED"] != "false":
        raise RuntimeEnvironmentError("Cognee graph subprocess mode must remain disabled")

    release = release_root.resolve(strict=False)
    for name in (
        "DATA_ROOT_DIRECTORY",
        "SYSTEM_ROOT_DIRECTORY",
        "CACHE_ROOT_DIRECTORY",
        "COGNEE_LOGS_DIR",
    ):
        raw = environment[name]
        if not isinstance(raw, str) or not raw or raw != raw.strip():
            raise RuntimeEnvironmentError("Cognee runtime path is malformed")
        path = Path(raw)
        if (
            not path.is_absolute()
            or _path_has_symlink(path)
            or _within(path.resolve(strict=False), release)
        ):
            raise RuntimeEnvironmentError(
                "Cognee writable roots must remain outside the release"
            )
    return environment


def _path_has_symlink(path: Path) -> bool:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
