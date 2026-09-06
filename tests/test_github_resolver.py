import json
from pathlib import Path

import pytest

from project_continuity.github_resolver import (
    GitHubAuthorityResolver,
    GitHubResolverUnavailable,
)


class Response:
    def __init__(self, value) -> None:
        self.content = json.dumps(value).encode("utf-8")

    def read(self, _limit):
        return self.content


class Opener:
    def __init__(self, routes) -> None:
        self.routes = routes
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        route = request.full_url.split("/repos/example/alpha/", 1)[1]
        return Response(self.routes[route])


def _token(path: Path) -> Path:
    path.write_text("github_test_token_00000000000000000000\n", encoding="ascii")
    path.chmod(0o600)
    return path


def test_authenticated_resolver_reads_squash_pr_and_published_release(
    tmp_path: Path,
) -> None:
    revision = "a" * 40
    opener = Opener(
        {
            "pulls?state=closed&sort=updated&direction=desc&per_page=100": [
                {
                    "number": 7,
                    "merge_commit_sha": revision,
                    "merged_at": "2026-08-30T00:00:00Z",
                    "title": "Squash-merged feature",
                    "html_url": "https://github.com/example/alpha/pull/7",
                },
                {
                    "number": 8,
                    "merge_commit_sha": None,
                    "merged_at": None,
                    "title": "Closed without merge",
                    "html_url": "https://github.com/example/alpha/pull/8",
                },
            ],
            "releases?per_page=100": [
                {
                    "id": 22,
                    "tag_name": "v1.0.0",
                    "published_at": "2026-08-30T01:00:00Z",
                    "name": "Version 1",
                    "html_url": "https://github.com/example/alpha/releases/tag/v1.0.0",
                    "prerelease": False,
                    "draft": False,
                }
            ],
            "releases/tags/v1.0.0": {
                "id": 22,
                "tag_name": "v1.0.0",
                "published_at": "2026-08-30T01:00:00Z",
                "name": "Version 1",
                "html_url": "https://github.com/example/alpha/releases/tag/v1.0.0",
                "prerelease": False,
                "draft": False,
            },
            "commits/v1.0.0": {"sha": revision},
        }
    )
    resolver = GitHubAuthorityResolver(_token(tmp_path / "github-token"), opener=opener)

    pulls = resolver.pull_requests("https://github.com/example/alpha")
    releases = resolver.releases("https://github.com/example/alpha")
    release = resolver.release("https://github.com/example/alpha", "v1.0.0")

    assert [row["pull_request"] for row in pulls] == [7]
    assert pulls[0]["revision"] == revision
    assert releases[0]["tag"] == "v1.0.0"
    assert release["revision"] == revision
    assert all(
        request.headers["Authorization"].startswith("Bearer github_test_token_")
        for request, _timeout in opener.requests
    )


def test_authenticated_resolver_reads_exact_collaboration_pr_candidate(
    tmp_path: Path,
) -> None:
    base = "a" * 40
    head = "b" * 40
    row = {
        "number": 9,
        "state": "open",
        "title": "[teamai] Contribute session knowledge from writer-agent",
        "body": "Contribute session knowledge: exact handoff",
        "html_url": "https://github.com/example/alpha/pull/9",
        "head": {
            "ref": "teamai/push/writer-agent/20260905-120000",
            "sha": head,
            "repo": {"full_name": "example/alpha"},
        },
        "base": {
            "ref": "main",
            "sha": base,
            "repo": {"full_name": "example/alpha"},
        },
    }
    opener = Opener(
        {
            "pulls?state=all&sort=created&direction=desc&per_page=100": [row],
            "pulls/9": row,
        }
    )
    resolver = GitHubAuthorityResolver(_token(tmp_path / "github-token"), opener=opener)

    listed = resolver.collaboration_pull_requests("https://github.com/example/alpha")
    exact = resolver.collaboration_pull_request(
        "https://github.com/example/alpha", 9
    )

    assert listed == (exact,)
    assert exact["head_revision"] == head
    assert exact["base_revision"] == base
    assert exact["head_ref"].startswith("teamai/push/writer-agent/")


def test_authenticated_resolver_recovers_collaboration_pr_by_exact_head(
    tmp_path: Path,
) -> None:
    base = "a" * 40
    head = "b" * 40
    branch = "teamai/push/writer-agent/20260905-120000"
    target = {
        "number": 9,
        "state": "open",
        "title": "[teamai] Contribute session knowledge from writer-agent",
        "body": "Contribute session knowledge: exact handoff",
        "html_url": "https://github.com/example/alpha/pull/9",
        "head": {
            "ref": branch,
            "sha": head,
            "repo": {"full_name": "example/alpha"},
        },
        "base": {
            "ref": "main",
            "sha": base,
            "repo": {"full_name": "example/alpha"},
        },
    }
    noise = [{"number": value} for value in range(100, 201)]
    route = (
        "pulls?state=all&head="
        "example%3Ateamai%2Fpush%2Fwriter-agent%2F20260905-120000"
        "&per_page=100"
    )
    opener = Opener(
        {
            "pulls?state=all&sort=created&direction=desc&per_page=100": noise,
            route: [target],
        }
    )
    resolver = GitHubAuthorityResolver(
        _token(tmp_path / "github-token"), opener=opener
    )

    recovered = resolver.collaboration_pull_requests_for_head(
        "https://github.com/example/alpha", branch
    )

    assert recovered[0]["pull_request"] == 9
    assert recovered[0]["head_ref"] == branch
    request, _timeout = opener.requests[0]
    assert len(opener.requests) == 1
    assert request.full_url.endswith(route)
    assert "sort=created" not in request.full_url


def test_authenticated_resolver_creates_one_exact_collaboration_pr(
    tmp_path: Path,
) -> None:
    base = "a" * 40
    head = "b" * 40
    row = {
        "number": 9,
        "state": "open",
        "title": "[teamai] Contribute session knowledge from writer-agent",
        "body": "Contribute session knowledge: exact handoff",
        "html_url": "https://github.com/example/alpha/pull/9",
        "head": {
            "ref": "teamai/push/writer-agent/20260905-120000",
            "sha": head,
            "repo": {"full_name": "example/alpha"},
        },
        "base": {
            "ref": "main",
            "sha": base,
            "repo": {"full_name": "example/alpha"},
        },
    }
    opener = Opener({"pulls": row})
    resolver = GitHubAuthorityResolver(_token(tmp_path / "github-token"), opener=opener)

    created = resolver.create_collaboration_pull_request(
        "https://github.com/example/alpha",
        head_ref=row["head"]["ref"],
        base_ref="main",
        subject=row["title"],
        body=row["body"],
    )

    request, _timeout = opener.requests[0]
    assert request.method == "POST"
    assert json.loads(request.data) == {
        "base": "main",
        "body": row["body"],
        "head": row["head"]["ref"],
        "title": row["title"],
    }
    assert created["pull_request"] == 9


def test_non_github_repository_is_rejected_before_any_request(tmp_path: Path) -> None:
    opener = Opener({})
    resolver = GitHubAuthorityResolver(_token(tmp_path / "github-token"), opener=opener)

    with pytest.raises(GitHubResolverUnavailable, match="github_repository_required"):
        resolver.pull_requests("https://gitlab.com/example/alpha")

    assert opener.requests == []


def test_token_file_must_be_owner_private_and_not_a_symlink(tmp_path: Path) -> None:
    token = _token(tmp_path / "github-token")
    token.chmod(0o644)
    with pytest.raises(Exception, match="github_token_file_unsafe"):
        GitHubAuthorityResolver(token)

    token.chmod(0o600)
    link = tmp_path / "linked-token"
    link.symlink_to(token)
    with pytest.raises(Exception, match="github_token_file_unsafe"):
        GitHubAuthorityResolver(link)
