"""Authenticated GitHub delivery facts with bounded, secret-free failures."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Dict, Mapping, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


API_ROOT = "https://api.github.com"
MAX_RESPONSE_BYTES = 2_000_000
MAX_ITEMS = 100
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_]+$")


class GitHubResolverError(RuntimeError):
    """GitHub returned malformed or unverifiable delivery evidence."""


class GitHubResolverUnavailable(GitHubResolverError):
    """The authenticated GitHub authority cannot currently be consulted."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


@dataclass(frozen=True)
class GitHubRepository:
    owner: str
    name: str


class GitHubAuthorityResolver:
    """Resolve GitHub API objects; never infer PRs or Releases from Git syntax."""

    def __init__(
        self,
        token_file: Path,
        *,
        timeout_seconds: float = 15.0,
        opener: Any = None,
    ) -> None:
        self.token_file = _private_token_file(Path(token_file))
        if not 1 <= timeout_seconds <= 60:
            raise GitHubResolverError("github_timeout_invalid")
        self.timeout_seconds = float(timeout_seconds)
        self._opener = opener or build_opener(_NoRedirect())

    @classmethod
    def from_environment(cls) -> "GitHubAuthorityResolver":
        raw = os.environ.get("PROJECT_CONTINUITY_GITHUB_TOKEN_FILE", "")
        if not raw:
            raise GitHubResolverUnavailable("github_token_file_absent")
        path = Path(raw)
        if not path.is_absolute():
            raise GitHubResolverUnavailable("github_token_file_unsafe")
        try:
            return cls(path)
        except GitHubResolverError as exc:
            raise GitHubResolverUnavailable("github_token_file_unsafe") from exc

    def repository(self, repo_url: str) -> GitHubRepository:
        try:
            parsed = urlsplit(repo_url)
            host = parsed.hostname
            port = parsed.port
        except ValueError as exc:
            raise GitHubResolverError("github_repository_url_invalid") from exc
        if (
            parsed.scheme != "https"
            or host != "github.com"
            or parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise GitHubResolverUnavailable("github_repository_required")
        parts = [part for part in parsed.path.removesuffix(".git").split("/") if part]
        if len(parts) != 2 or any(part in {".", ".."} for part in parts):
            raise GitHubResolverUnavailable("github_repository_required")
        return GitHubRepository(parts[0], parts[1])

    def commit(
        self, repo_url: str, revision: str, *, deadline: float | None = None
    ) -> Mapping[str, Any]:
        if not _COMMIT.fullmatch(revision):
            raise GitHubResolverError("github_commit_malformed")
        repo = self.repository(repo_url)
        payload = self._get(repo, "commits/" + revision, deadline=deadline)
        if not isinstance(payload, dict) or payload.get("sha") != revision:
            raise GitHubResolverError("github_commit_malformed")
        commit = payload.get("commit")
        files = payload.get("files", [])
        if not isinstance(commit, dict) or not isinstance(files, list):
            raise GitHubResolverError("github_commit_malformed")
        message = commit.get("message")
        author = commit.get("author")
        if not isinstance(message, str) or not isinstance(author, dict):
            raise GitHubResolverError("github_commit_malformed")
        committed_at = author.get("date")
        if not isinstance(committed_at, str):
            raise GitHubResolverError("github_commit_malformed")
        names = []
        for item in files[:MAX_ITEMS]:
            if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
                raise GitHubResolverError("github_commit_malformed")
            names.append(item["filename"])
        subject = message.splitlines()[0].strip()
        if not subject:
            raise GitHubResolverError("github_commit_malformed")
        return {
            "committed_at": committed_at,
            "files": names,
            "kind": "commit",
            "revision": revision,
            "subject": subject,
        }

    def pull_requests(
        self, repo_url: str, *, deadline: float | None = None
    ) -> Sequence[Mapping[str, Any]]:
        repo = self.repository(repo_url)
        payload = self._get(
            repo,
            "pulls?state=closed&sort=updated&direction=desc&per_page=100",
            deadline=deadline,
        )
        if not isinstance(payload, list) or len(payload) > MAX_ITEMS:
            raise GitHubResolverError("github_pull_requests_malformed")
        results = []
        for item in payload:
            if not isinstance(item, dict) or item.get("merged_at") is None:
                continue
            results.append(_pull_request_record(item))
        return tuple(results)

    def pull_request(
        self,
        repo_url: str,
        pull_request: int,
        *,
        deadline: float | None = None,
    ) -> Mapping[str, Any]:
        if type(pull_request) is not int or pull_request < 1:
            raise GitHubResolverError("github_pull_request_malformed")
        repo = self.repository(repo_url)
        payload = self._get(repo, "pulls/%d" % pull_request, deadline=deadline)
        if not isinstance(payload, dict) or payload.get("merged_at") is None:
            raise GitHubResolverError("github_pull_request_unavailable")
        record = _pull_request_record(payload)
        if record["pull_request"] != pull_request:
            raise GitHubResolverError("github_pull_request_malformed")
        return record

    def releases(
        self, repo_url: str, *, deadline: float | None = None
    ) -> Sequence[Mapping[str, Any]]:
        repo = self.repository(repo_url)
        payload = self._get(repo, "releases?per_page=100", deadline=deadline)
        if not isinstance(payload, list) or len(payload) > MAX_ITEMS:
            raise GitHubResolverError("github_releases_malformed")
        results = []
        for item in payload:
            if not isinstance(item, dict) or item.get("draft") is True:
                continue
            results.append(_release_summary(item))
        return tuple(results)

    def release(
        self, repo_url: str, tag: str, *, deadline: float | None = None
    ) -> Mapping[str, Any]:
        if not isinstance(tag, str) or not tag or tag != tag.strip():
            raise GitHubResolverError("github_release_tag_malformed")
        repo = self.repository(repo_url)
        payload = self._get(
            repo, "releases/tags/" + quote(tag, safe=""), deadline=deadline
        )
        if not isinstance(payload, dict) or payload.get("draft") is True:
            raise GitHubResolverError("github_release_unavailable")
        summary = _release_summary(payload)
        commit = self._get(
            repo, "commits/" + quote(tag, safe=""), deadline=deadline
        )
        revision = commit.get("sha") if isinstance(commit, dict) else None
        if not isinstance(revision, str) or not _COMMIT.fullmatch(revision):
            raise GitHubResolverError("github_release_commit_malformed")
        return {**summary, "revision": revision}

    def _get(
        self,
        repo: GitHubRepository,
        route: str,
        *,
        deadline: float | None,
    ) -> Any:
        timeout = _remaining(deadline, self.timeout_seconds)
        token = _read_token(self.token_file)
        url = "%s/repos/%s/%s/%s" % (
            API_ROOT,
            quote(repo.owner, safe=""),
            quote(repo.name, safe=""),
            route,
        )
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": "Bearer " + token,
                "User-Agent": "ProjectContinuity/0.1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        try:
            response = self._opener.open(request, timeout=timeout)
            try:
                content = response.read(MAX_RESPONSE_BYTES + 1)
            finally:
                close = getattr(response, "close", None)
                if close is not None:
                    close()
        except HTTPError as exc:
            if exc.code == 404:
                raise GitHubResolverError("github_object_unavailable") from exc
            raise GitHubResolverUnavailable("github_authority_unavailable") from exc
        except (OSError, TimeoutError, URLError) as exc:
            raise GitHubResolverUnavailable("github_authority_unavailable") from exc
        if len(content) > MAX_RESPONSE_BYTES:
            raise GitHubResolverError("github_response_too_large")
        try:
            return json.loads(content.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, json.JSONDecodeError, GitHubResolverError) as exc:
            raise GitHubResolverError("github_response_malformed") from exc


def canonical_digest(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    from hashlib import sha256

    return "sha256:" + sha256(encoded).hexdigest()


def _pull_request_record(value: Mapping[str, Any]) -> Mapping[str, Any]:
    number = value.get("number")
    revision = value.get("merge_commit_sha")
    merged_at = value.get("merged_at")
    title = value.get("title")
    html_url = value.get("html_url")
    if (
        type(number) is not int
        or number < 1
        or not isinstance(revision, str)
        or not _COMMIT.fullmatch(revision)
        or not isinstance(merged_at, str)
        or not isinstance(title, str)
        or not title.strip()
        or not isinstance(html_url, str)
    ):
        raise GitHubResolverError("github_pull_request_malformed")
    return {
        "kind": "pull_request",
        "merged_at": merged_at,
        "pull_request": number,
        "revision": revision,
        "subject": title.strip(),
        "url": html_url,
    }


def _release_summary(value: Mapping[str, Any]) -> Mapping[str, Any]:
    release_id = value.get("id")
    tag = value.get("tag_name")
    published_at = value.get("published_at")
    name = value.get("name")
    html_url = value.get("html_url")
    prerelease = value.get("prerelease")
    if (
        type(release_id) is not int
        or release_id < 1
        or not isinstance(tag, str)
        or not tag
        or not isinstance(published_at, str)
        or not isinstance(html_url, str)
        or type(prerelease) is not bool
        or (name is not None and not isinstance(name, str))
    ):
        raise GitHubResolverError("github_release_malformed")
    return {
        "kind": "release",
        "name": name or tag,
        "prerelease": prerelease,
        "published_at": published_at,
        "release_id": release_id,
        "tag": tag,
        "url": html_url,
    }


def _private_token_file(path: Path) -> Path:
    if not path.is_absolute():
        raise GitHubResolverError("github_token_file_unsafe")
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        try:
            item = os.lstat(current)
        except OSError as exc:
            raise GitHubResolverError("github_token_file_unavailable") from exc
        if stat.S_ISLNK(item.st_mode):
            raise GitHubResolverError("github_token_file_unsafe")
    item = path.stat()
    if (
        not stat.S_ISREG(item.st_mode)
        or item.st_uid != os.getuid()
        or item.st_mode & 0o077
    ):
        raise GitHubResolverError("github_token_file_unsafe")
    _read_token(path)
    return path


def _read_token(path: Path) -> str:
    try:
        token = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise GitHubResolverUnavailable("github_token_unavailable") from exc
    if not 20 <= len(token) <= 512 or not _TOKEN.fullmatch(token):
        raise GitHubResolverError("github_token_malformed")
    return token


def _remaining(deadline: float | None, maximum: float) -> float:
    if deadline is None:
        return maximum
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise GitHubResolverUnavailable("github_authority_timeout")
    return min(maximum, remaining)


def _unique_object(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    value: Dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise GitHubResolverError("github_response_duplicate_key")
        value[key] = item
    return value
