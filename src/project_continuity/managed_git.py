"""One fail-closed Git boundary for managed authority checkouts."""

from __future__ import annotations

import base64
from dataclasses import dataclass
import os
from pathlib import Path
import re
import subprocess
from typing import Dict, Optional, Tuple
from urllib.parse import urlsplit

from .github_resolver import GitHubResolverError, _private_token_file, _read_token


MAX_CONFIG_OUTPUT = 256_000
_DETACHED_HOME = "/nonexistent/project-continuity-git-home"
_SAFE_CORE_VALUES = {
    "core.bare": frozenset({"false"}),
    "core.filemode": frozenset({"false", "true"}),
    "core.ignorecase": frozenset({"false", "true"}),
    "core.logallrefupdates": frozenset({"true"}),
    "core.precomposeunicode": frozenset({"false", "true"}),
    "core.repositoryformatversion": frozenset({"0"}),
}
_REQUIRED_CORE_KEYS = frozenset(
    {
        "core.bare",
        "core.filemode",
        "core.logallrefupdates",
        "core.repositoryformatversion",
    }
)
_BRANCH_KEY = re.compile(r"^branch\.(.+)\.(merge|remote)$", re.IGNORECASE)


class ManagedGitError(RuntimeError):
    """A managed Git environment or checkout failed its closed contract."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ManagedGitConfig:
    """Canonical values parsed by Git itself before repo-scoped commands run."""

    remote: str
    branches: Tuple[Tuple[str, str, str], ...]

    def branch(self, name: str) -> Optional[Tuple[str, str]]:
        for branch, remote, merge in self.branches:
            if branch == name:
                return remote, merge
        return None


def managed_git_environment(remote: Optional[str] = None) -> Dict[str, str]:
    """Return an ambient-config-free environment for every managed Git process."""

    environment = {
        name: value
        for name, value in os.environ.items()
        if name in {"LANG", "LC_ALL", "PATH", "TMPDIR"}
    }
    environment.update(
        {
            "CI": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": _DETACHED_HOME,
            "NO_COLOR": "1",
            "XDG_CONFIG_HOME": _DETACHED_HOME,
        }
    )
    if not _is_exact_github_remote(remote):
        return environment
    raw_path = os.environ.get("PROJECT_CONTINUITY_GITHUB_TOKEN_FILE", "")
    if not raw_path:
        return environment
    try:
        token = _read_token(_private_token_file(Path(raw_path)))
    except GitHubResolverError as exc:
        raise ManagedGitError("managed_git_token_unsafe") from exc
    credential = base64.b64encode(
        ("x-access-token:" + token).encode("ascii")
    ).decode("ascii")
    environment.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.%s.extraheader" % remote,
            "GIT_CONFIG_VALUE_0": "Authorization: Basic " + credential,
        }
    )
    return environment


def inspect_managed_git_config(root: Path, expected_remote: str) -> ManagedGitConfig:
    """Parse one local config with Git, then enforce the exact managed grammar.

    The parser runs with ``--file`` and ``--no-includes`` from the filesystem
    root.  It therefore observes Git's real syntax without entering the target
    repository or executing hooks, fsmonitor commands, helpers, or transports.
    """

    checkout = Path(root)
    git_dir = checkout / ".git"
    if (
        not checkout.is_absolute()
        or checkout.is_symlink()
        or not checkout.is_dir()
        or git_dir.is_symlink()
        or not git_dir.is_dir()
    ):
        raise ManagedGitError("managed_git_config_unsafe")
    path = git_dir / "config"
    if (
        not os.path.lexists(str(path))
        or path.is_symlink()
        or not path.is_file()
    ):
        raise ManagedGitError("managed_git_config_unsafe")
    stat = path.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o022:
        raise ManagedGitError("managed_git_config_unsafe")
    try:
        completed = subprocess.run(
            [
                "git",
                "config",
                "--file",
                str(path),
                "--null",
                "--list",
                "--no-includes",
            ],
            cwd=Path(checkout.anchor),
            env=managed_git_environment(),
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ManagedGitError("managed_git_config_unsafe") from exc
    if (
        completed.returncode != 0
        or len(completed.stdout) > MAX_CONFIG_OUTPUT
        or len(completed.stderr) > MAX_CONFIG_OUTPUT
    ):
        raise ManagedGitError("managed_git_config_unsafe")
    values = _config_records(completed.stdout)
    return _validate_config_records(values, expected_remote)


def _config_records(payload: bytes) -> Tuple[Tuple[str, str], ...]:
    records = []
    seen = set()
    for raw in payload.split(b"\0"):
        if not raw:
            continue
        if b"\n" not in raw:
            raise ManagedGitError("managed_git_config_unsafe")
        key_raw, value_raw = raw.split(b"\n", 1)
        try:
            key = key_raw.decode("utf-8")
            value = value_raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManagedGitError("managed_git_config_unsafe") from exc
        folded = key.casefold()
        if (
            not key
            or key != key.strip()
            or "\n" in value
            or "\r" in value
            or folded in seen
        ):
            raise ManagedGitError("managed_git_config_unsafe")
        seen.add(folded)
        records.append((key, value))
    return tuple(records)


def _validate_config_records(
    records: Tuple[Tuple[str, str], ...], expected_remote: str
) -> ManagedGitConfig:
    core_seen = set()
    remote_values: Dict[str, str] = {}
    branches: Dict[str, Dict[str, str]] = {}
    for key, value in records:
        folded = key.casefold()
        if folded in _SAFE_CORE_VALUES:
            if value not in _SAFE_CORE_VALUES[folded]:
                raise ManagedGitError("managed_git_config_unsafe")
            core_seen.add(folded)
            continue
        if folded in {"remote.origin.fetch", "remote.origin.url"}:
            remote_values[folded.rsplit(".", 1)[1]] = value
            continue
        match = _BRANCH_KEY.fullmatch(key)
        if match is not None:
            branch = match.group(1)
            field = match.group(2).casefold()
            if (
                not branch
                or len(branch) > 240
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in branch
                )
            ):
                raise ManagedGitError("managed_git_config_unsafe")
            branches.setdefault(branch, {})[field] = value
            continue
        raise ManagedGitError("managed_git_config_unsafe")
    if not _REQUIRED_CORE_KEYS <= core_seen:
        raise ManagedGitError("managed_git_config_unsafe")
    if set(remote_values) != {"fetch", "url"}:
        raise ManagedGitError("managed_git_config_unsafe")
    actual_remote = remote_values["url"]
    if actual_remote.removesuffix(".git") != expected_remote.removesuffix(".git"):
        raise ManagedGitError("managed_git_remote_conflict")
    if remote_values["fetch"] != "+refs/heads/*:refs/remotes/origin/*":
        raise ManagedGitError("managed_git_config_unsafe")
    canonical_branches = []
    for branch, fields in branches.items():
        if (
            set(fields) != {"merge", "remote"}
            or fields["remote"] != "origin"
            or fields["merge"] != "refs/heads/%s" % branch
        ):
            raise ManagedGitError("managed_git_config_unsafe")
        canonical_branches.append((branch, fields["remote"], fields["merge"]))
    return ManagedGitConfig(actual_remote, tuple(sorted(canonical_branches)))


def _is_exact_github_remote(remote: Optional[str]) -> bool:
    if not isinstance(remote, str):
        return False
    try:
        parsed = urlsplit(remote)
        port = parsed.port
    except ValueError as exc:
        raise ManagedGitError("managed_git_remote_unsafe") from exc
    return bool(
        parsed.scheme == "https"
        and parsed.hostname == "github.com"
        and port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and len([part for part in parsed.path.split("/") if part]) == 2
    )
