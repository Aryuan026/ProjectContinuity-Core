import fcntl
import json
import os
from pathlib import Path
import subprocess
import time

import pytest

from project_continuity.authority_layers import (
    AuthorityLayerError,
    AuthorityLayerUnavailable,
    GitHubDeliveryLayer,
    OpenSpecLayer,
    TeamAILayer,
    _managed_repo,
)
from project_continuity.managed_git import managed_git_environment
from project_continuity.teamai import render_teamai_guard_documents
from project_continuity.truth_bindings import OpenSpecBinding, TeamAIBinding


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_AUTHOR_NAME": "Test Agent",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test Agent",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        env=GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(root: Path, remote: str) -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "remote", "add", "origin", remote)
    (root / "README.md").write_text("initial\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "initial truth")
    return root


def _bare_remote(tmp_path: Path, name: str, body: str) -> tuple[Path, str]:
    bare = tmp_path / (name + ".git")
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)],
        env=GIT_ENV,
        check=True,
        capture_output=True,
    )
    author = _repo(tmp_path / (name + "-author"), bare.as_uri())
    (author / "README.md").write_text(body + "\n", encoding="utf-8")
    _git(author, "add", "README.md")
    _git(author, "commit", "-m", body)
    revision = _git(author, "rev-parse", "HEAD")
    _git(author, "push", "-u", "origin", "main")
    return bare, revision


class FakeGitHubResolver:
    def __init__(self, *, pulls=(), releases=()) -> None:
        self.pulls = tuple(pulls)
        self.release_rows = tuple(releases)

    def commit(self, repo_url, revision, *, deadline=None):
        del repo_url, deadline
        return {
            "committed_at": "2026-08-30T00:00:00Z",
            "files": ["feature.py"],
            "kind": "commit",
            "revision": revision,
            "subject": "deliver integrated truth plane",
        }

    def pull_requests(self, repo_url, *, deadline=None):
        del repo_url, deadline
        return self.pulls

    def pull_request(self, repo_url, pull_request, *, deadline=None):
        del repo_url, deadline
        return next(row for row in self.pulls if row["pull_request"] == pull_request)

    def releases(self, repo_url, *, deadline=None):
        del repo_url, deadline
        return tuple({key: value for key, value in row.items() if key != "revision"} for row in self.release_rows)

    def release(self, repo_url, tag, *, deadline=None):
        del repo_url, deadline
        return next(row for row in self.release_rows if row["tag"] == tag)


def _fake_openspec(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
if sys.argv[1] == '--version':
    print('OpenSpec 1.10.0')
elif sys.argv[1:3] == ['new', 'change']:
    root = pathlib.Path('openspec/changes') / sys.argv[3]
    root.mkdir(parents=True)
    (root / '.openspec.yaml').write_text('schema: spec-driven\\n')
    print(json.dumps({'changeName':sys.argv[3]}))
elif sys.argv[1] == 'instructions':
    change = sys.argv[sys.argv.index('--change') + 1]
    artifact = sys.argv[2]
    outputs = {'proposal':'proposal.md','design':'design.md','specs':'specs/**/*.md','tasks':'tasks.md'}
    root = pathlib.Path.cwd() / 'openspec/changes' / change
    print(json.dumps({'artifactId':artifact,'changeDir':str(root),'outputPath':outputs[artifact]}))
elif sys.argv[1] == 'validate':
    print(json.dumps({'valid':True}))
elif sys.argv[1] == 'archive':
    source = pathlib.Path('openspec/changes') / sys.argv[2]
    target = pathlib.Path('openspec/changes/archive') / sys.argv[2]
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))
    print(json.dumps({'archived':sys.argv[2]}))
elif sys.argv[1] == 'list' and '--specs' in sys.argv:
    print(json.dumps({'specs':[{'id':'authority-contract','requirementCount':2}]}))
elif sys.argv[1] == 'list':
    print(json.dumps({'changes':[{'name':'integrate-truth-plane','status':'in-progress'}]}))
elif sys.argv[1] == 'show':
    print(json.dumps({
        'id':sys.argv[2],
        'summary':'统一五工具检索正式决定',
        'root': {
            'path': str(pathlib.Path.cwd() / 'srv/hermes/custody-marker'),
            'source': 'nearest',
        },
    }))
else:
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _fake_node(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import os, pathlib, sys
if 'contribute' in sys.argv:
    expected = {
        'GIT_AUTHOR_EMAIL':'writer-agent@project-continuity.invalid',
        'GIT_AUTHOR_NAME':'writer-agent',
        'GIT_COMMITTER_EMAIL':'writer-agent@project-continuity.invalid',
        'GIT_COMMITTER_NAME':'writer-agent',
    }
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise SystemExit(9)
    reports = pathlib.Path('.teamai/reports-wt')
    reports.mkdir(parents=True, exist_ok=True)
    (reports / 'donor-owned-state').write_text('native worktree\\n')
    print('Branch teamai/push/writer-agent/canary has been pushed')
    print('Pull Request created: https://github.com/example/alpha-team/pull/11')
else:
    query = sys.argv[-1]
    if query == 'unrelated':
        print('ℹ No matching learnings found for "unrelated".')
    else:
        print('--- [teamai:recall:start] --- (1 result)')
        print('[1/1] TeamAI native recall: 协作验收已通过')
        print('--- [teamai:recall:end] ---')
""",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _historical_openspec(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import json, pathlib, sys
if sys.argv[1] == '--version':
    print('OpenSpec 1.10.0')
elif sys.argv[1] == 'list' and '--specs' in sys.argv:
    print(json.dumps({'specs':[{'id':'authority-contract'}]}))
elif sys.argv[1] == 'list':
    print(json.dumps({'changes':[]}))
elif sys.argv[1] == 'show':
    content = pathlib.Path('openspec/specs/authority-contract/spec.md').read_text()
    print(json.dumps({'id':'authority-contract', 'summary':content.strip()}))
else:
    raise SystemExit(2)
""",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _slow_openspec(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import json, sys, time
if sys.argv[1] == '--version':
    print('OpenSpec 1.10.0')
elif sys.argv[1] == 'list' and '--specs' in sys.argv:
    print(json.dumps({'specs':[{'id':'slow-spec'}]}))
elif sys.argv[1] == 'list':
    print(json.dumps({'changes':[]}))
elif sys.argv[1] == 'show':
    time.sleep(5)
    print(json.dumps({'id':'slow-spec'}))
""",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def test_openspec_native_cli_search_and_exact_get(config, tmp_path: Path) -> None:
    remote = "https://github.com/example/alpha-specs"
    root = _repo(config.paths.data_root / "openspec/alpha", remote)
    (root / "openspec").mkdir()
    (root / "openspec/config.yaml").write_text("schema: spec-driven\n", encoding="utf-8")
    _git(root, "add", "openspec/config.yaml")
    _git(root, "commit", "-m", "add authority decision")
    layer = OpenSpecLayer(
        config,
        OpenSpecBinding("alpha-specs", remote),
        _fake_openspec(tmp_path / "openspec"),
    )

    status = layer.status("reader-client", "alpha")
    found = layer.search("reader-client", "alpha", "五工具", limit=8)
    path_only = layer.search("reader-client", "alpha", "hermes", limit=8)
    fetched = layer.get(
        "reader-client", "alpha", found[0]["stable_ref"] and _ref(found[0]["stable_ref"])
    )

    assert status["changes"] == status["specs"] == 1
    assert path_only == []
    assert found[0]["stable_ref"]["authority"] == "openspec"
    expected_uri = "openspec://alpha/alpha-specs/%s" % found[0]["id"]
    assert found[0]["summary"]["root"]["path"] == expected_uri
    assert str(root) not in str(found[0])
    assert fetched["payload"]["summary"] == "统一五工具检索正式决定"
    assert fetched["payload"]["root"]["path"] == expected_uri
    assert fetched["stable_ref"] == found[0]["stable_ref"]
    assert str(root) not in str(fetched)


def test_teamai_native_recall_and_reviewed_exact_get(config, tmp_path: Path) -> None:
    remote = "https://github.com/example/alpha-team"
    root = _repo(config.paths.data_root / "team/alpha", remote)
    (root / ".gitignore").write_text(
        ".teamai/config.yaml\n.teamai/knowledge-wt/\n.teamai/search-index.json\n",
        encoding="utf-8",
    )
    documents = render_teamai_guard_documents(
        team_id="alpha-team", repo_url=remote, reviewers=("reviewer-agent",)
    )
    for relative, content in documents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", ".gitignore", ".teamai")
    _git(root, "commit", "-m", "configure guarded collaboration")
    _git(root, "switch", "-c", "teamai/push/writer-agent/canary")
    learning = root / ".teamai/learnings/reviewed-canary.md"
    learning.parent.mkdir(parents=True)
    learning.write_text(
        "---\ntitle: \"协作验收\"\nauthor: writer-agent\n---\n\n统一五工具协作证据。\n",
        encoding="utf-8",
    )
    _git(root, "add", str(learning.relative_to(root)))
    _git(root, "commit", "-m", "[teamai] reviewed canary")
    _git(root, "switch", "main")
    _git(
        root,
        "merge",
        "--no-ff",
        "teamai/push/writer-agent/canary",
        "-m",
        "Merge pull request #7 from example/teamai/push/writer-agent/canary",
    )
    entrypoint = tmp_path / "teamai.js"
    entrypoint.write_text("// reviewed fake entrypoint\n", encoding="utf-8")
    layer = TeamAILayer(
        config,
        TeamAIBinding("alpha-team", remote, ("reviewer-agent",)),
        _fake_node(tmp_path / "node"),
        entrypoint,
    )

    found = layer.search("reader-client", "alpha", "五工具", limit=8)
    reference = _ref(found[0]["reviewed_matches"][0]["stable_ref"])
    fetched = layer.get("reader-client", "alpha", reference)

    assert "TeamAI native recall" in found[0]["recall"]
    assert reference.authority == "teamai"
    assert fetched["title"] == "协作验收"
    assert "五工具" in fetched["content"]

    assert layer.search("reader-client", "alpha", "unrelated", limit=8) == []


def test_teamai_read_deadline_includes_command_lock_wait(
    config, tmp_path: Path, monkeypatch
) -> None:
    remote = "https://github.com/example/alpha-team"
    root = _repo(config.paths.data_root / "team/alpha", remote)
    (root / ".gitignore").write_text(
        ".teamai/config.yaml\n.teamai/knowledge-wt/\n.teamai/search-index.json\n",
        encoding="utf-8",
    )
    for relative, content in render_teamai_guard_documents(
        team_id="alpha-team", repo_url=remote, reviewers=("reviewer-agent",)
    ).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", ".gitignore", ".teamai")
    _git(root, "commit", "-m", "configure guarded collaboration")

    config_path = root / ".teamai/config.yaml"
    preimage = b"owner: preexisting-local-config\n"
    config_path.write_bytes(preimage)
    config_path.chmod(0o600)
    marker = tmp_path / "teamai-command-entered"
    node = tmp_path / "node-lock-deadline"
    node.write_text(
        """#!/usr/bin/env python3
from pathlib import Path
Path(%r).write_text('entered\\n', encoding='utf-8')
print('--- [teamai:recall:start] --- (1 result)')
print('[1/1] TeamAI native recall: 锁释放后恢复')
print('--- [teamai:recall:end] ---')
""" % str(marker),
        encoding="utf-8",
    )
    node.chmod(0o700)
    entrypoint = tmp_path / "teamai-lock-deadline.js"
    entrypoint.write_text("// reviewed fake entrypoint\n", encoding="utf-8")
    layer = TeamAILayer(
        config,
        TeamAIBinding("alpha-team", remote, ("reviewer-agent",)),
        node,
        entrypoint,
    )
    monkeypatch.setattr(
        "project_continuity.authority_layers.READ_DEADLINE_SECONDS", 1.0
    )

    runtime = config.paths.data_root / "truth-plane/teamai-runtime/alpha"
    runtime.mkdir(parents=True)
    runtime.chmod(0o700)
    lock_path = runtime / "command.lock"
    with lock_path.open("a+b") as held_lock:
        os.fchmod(held_lock.fileno(), 0o600)
        fcntl.flock(held_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        started = time.monotonic()
        with pytest.raises(AuthorityLayerUnavailable, match="teamai_timeout"):
            layer.search("reader-client", "alpha", "锁", limit=8)
        elapsed = time.monotonic() - started

        assert elapsed < 2
        assert not marker.exists()
        assert config_path.read_bytes() == preimage

    monkeypatch.setattr(
        "project_continuity.authority_layers.READ_DEADLINE_SECONDS", 3.0
    )
    found = layer.search("reader-client", "alpha", "锁", limit=8)
    assert found[0]["recall_hit_count"] == 1
    assert marker.read_text(encoding="utf-8") == "entered\n"
    assert config_path.read_bytes() == preimage


def test_openspec_old_ref_survives_refresh_edit_and_delete(
    config, tmp_path: Path
) -> None:
    remote = "https://github.com/example/alpha-specs"
    root = _repo(config.paths.data_root / "openspec/alpha", remote)
    spec = root / "openspec/specs/authority-contract/spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("原始正式决定\n", encoding="utf-8")
    _git(root, "add", "openspec")
    _git(root, "commit", "-m", "record formal decision")
    layer = OpenSpecLayer(
        config,
        OpenSpecBinding("alpha-specs", remote),
        _historical_openspec(tmp_path / "openspec-history"),
    )
    reference = _ref(
        layer.search("reader-client", "alpha", "原始", limit=8)[0]["stable_ref"]
    )

    (root / "README.md").write_text("unrelated refresh\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "unrelated refresh")
    assert layer.get("reader-client", "alpha", reference)["payload"]["summary"] == "原始正式决定"

    spec.write_text("后来修改的决定\n", encoding="utf-8")
    _git(root, "add", "openspec")
    _git(root, "commit", "-m", "revise formal decision")
    assert layer.get("reader-client", "alpha", reference)["payload"]["summary"] == "原始正式决定"

    spec.unlink()
    _git(root, "add", "-u", "openspec")
    _git(root, "commit", "-m", "remove current decision")
    assert layer.get("reader-client", "alpha", reference)["payload"]["summary"] == "原始正式决定"


def test_openspec_read_deadline_terminates_the_real_subprocess(
    config, tmp_path: Path, monkeypatch
) -> None:
    import time

    remote = "https://github.com/example/alpha-specs"
    root = _repo(config.paths.data_root / "openspec/alpha", remote)
    (root / "openspec").mkdir()
    (root / "openspec/config.yaml").write_text(
        "schema: spec-driven\n", encoding="utf-8"
    )
    _git(root, "add", "openspec")
    _git(root, "commit", "-m", "configure OpenSpec")
    layer = OpenSpecLayer(
        config,
        OpenSpecBinding("alpha-specs", remote),
        _slow_openspec(tmp_path / "openspec-slow"),
    )
    monkeypatch.setattr(
        "project_continuity.authority_layers.READ_DEADLINE_SECONDS", 1.0
    )

    started = time.monotonic()
    with pytest.raises(AuthorityLayerUnavailable, match="openspec_timeout"):
        layer.search("reader-client", "alpha", "slow", limit=8)
    assert time.monotonic() - started < 2


def test_teamai_old_ref_survives_refresh_edit_and_delete_without_review_drift(
    config, tmp_path: Path
) -> None:
    remote = "https://github.com/example/alpha-team"
    root = _repo(config.paths.data_root / "team/alpha", remote)
    (root / ".gitignore").write_text(
        ".teamai/config.yaml\n.teamai/knowledge-wt/\n.teamai/search-index.json\n",
        encoding="utf-8",
    )
    for relative, content in render_teamai_guard_documents(
        team_id="alpha-team", repo_url=remote, reviewers=("reviewer-agent",)
    ).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", ".gitignore", ".teamai")
    _git(root, "commit", "-m", "configure guarded collaboration")
    _git(root, "switch", "-c", "teamai/push/writer-agent/history")
    learning = root / ".teamai/learnings/history.md"
    learning.parent.mkdir(parents=True)
    learning.write_text(
        "---\ntitle: \"原始协作史\"\nauthor: writer-agent\n---\n\n原始审核内容。\n",
        encoding="utf-8",
    )
    _git(root, "add", str(learning.relative_to(root)))
    _git(root, "commit", "-m", "[teamai] reviewed history")
    _git(root, "switch", "main")
    _git(
        root,
        "merge",
        "--no-ff",
        "teamai/push/writer-agent/history",
        "-m",
        "Merge pull request #9 from example/teamai/push/writer-agent/history",
    )
    entrypoint = tmp_path / "teamai-history.js"
    entrypoint.write_text("// fake\n", encoding="utf-8")
    layer = TeamAILayer(
        config,
        TeamAIBinding("alpha-team", remote, ("reviewer-agent",)),
        _fake_node(tmp_path / "node-history"),
        entrypoint,
    )
    reference = _ref(
        layer.search("reader-client", "alpha", "原始", limit=8)[0][
            "reviewed_matches"
        ][0]["stable_ref"]
    )

    (root / "README.md").write_text("unrelated refresh\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-m", "unrelated refresh")
    assert "原始审核内容" in layer.get("reader-client", "alpha", reference)["content"]

    learning.write_text(
        "---\ntitle: \"未经审核的直推\"\nauthor: writer-agent\n---\n\n后来内容。\n",
        encoding="utf-8",
    )
    _git(root, "add", str(learning.relative_to(root)))
    _git(root, "commit", "-m", "direct unreviewed edit")
    assert layer.search("reader-client", "alpha", "后来内容", limit=8)[0][
        "reviewed_matches"
    ] == []
    assert "原始审核内容" in layer.get("reader-client", "alpha", reference)["content"]

    learning.unlink()
    _git(root, "add", "-u", ".teamai")
    _git(root, "commit", "-m", "delete current collaboration file")
    assert "原始审核内容" in layer.get("reader-client", "alpha", reference)["content"]


def test_delivery_git_search_and_exact_get(config) -> None:
    remote = "https://github.com/example/alpha"
    root = _repo(config.paths.data_root / "delivery/alpha", remote)
    (root / "feature.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "feature.py")
    _git(root, "commit", "-m", "deliver integrated truth plane")
    layer = GitHubDeliveryLayer(config, FakeGitHubResolver())

    found = layer.search("reader-client", "alpha", "integrated truth", limit=5)
    reference = _ref(found[0]["stable_ref"])
    fetched = layer.get("reader-client", "alpha", reference)

    assert layer.status("reader-client", "alpha")["current"]["authority"] == "github"
    assert fetched["revision"] == reference.version
    assert fetched["files"] == ["feature.py"]


def test_delivery_exposes_github_verified_squash_pr_and_release(config) -> None:
    remote = "https://github.com/example/alpha"
    root = _repo(config.paths.data_root / "delivery/alpha", remote)
    (root / "feature.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(root, "add", "feature.py")
    _git(root, "commit", "-m", "squashed reviewed feature")
    revision = _git(root, "rev-parse", "HEAD")
    pull = {
        "kind": "pull_request",
        "merged_at": "2026-08-30T00:00:00Z",
        "pull_request": 17,
        "revision": revision,
        "subject": "Reviewed feature",
        "url": "https://github.com/example/alpha/pull/17",
    }
    release = {
        "kind": "release",
        "name": "Version 1",
        "prerelease": False,
        "published_at": "2026-08-30T01:00:00Z",
        "release_id": 99,
        "revision": revision,
        "tag": "v1.0.0",
        "url": "https://github.com/example/alpha/releases/tag/v1.0.0",
    }
    layer = GitHubDeliveryLayer(
        config, FakeGitHubResolver(pulls=(pull,), releases=(release,))
    )

    found_pull = layer.search("reader-client", "alpha", "reviewed feature", limit=5)[0]
    found_release = layer.search("reader-client", "alpha", "v1.0.0", limit=5)[0]

    assert found_pull["kind"] == "pull_request"
    assert layer.get("reader-client", "alpha", _ref(found_pull["stable_ref"]))[
        "pull_request"
    ] == 17
    assert found_release["kind"] == "release"
    assert layer.get("reader-client", "alpha", _ref(found_release["stable_ref"]))[
        "tag"
    ] == "v1.0.0"


def test_delivery_does_not_infer_pr_or_release_from_local_git_syntax(config) -> None:
    remote = "https://github.com/example/alpha"
    root = _repo(config.paths.data_root / "delivery/alpha", remote)
    _git(root, "switch", "-c", "fabricated")
    (root / "fabricated.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "fabricated.py")
    _git(root, "commit", "-m", "fabricated")
    _git(root, "switch", "main")
    _git(
        root,
        "merge",
        "--no-ff",
        "fabricated",
        "-m",
        "Merge pull request #777 from local/fabricated",
    )
    _git(root, "tag", "not-a-github-release")
    layer = GitHubDeliveryLayer(config, FakeGitHubResolver())

    fabricated = layer.search(
        "reader-client", "alpha", "pull request #777", limit=5
    )
    assert all(row["kind"] != "pull_request" for row in fabricated)
    assert layer.search("reader-client", "alpha", "not-a-github-release", limit=5) == []


def test_wrong_remote_and_dirty_authority_repos_fail_closed(config) -> None:
    wrong = _repo(
        config.paths.data_root / "delivery/alpha",
        "https://github.com/example/not-alpha",
    )
    layer = GitHubDeliveryLayer(config, FakeGitHubResolver())
    with pytest.raises(AuthorityLayerError, match="remote_mismatch"):
        layer.status("reader-client", "alpha")

    _git(wrong, "remote", "set-url", "origin", "https://github.com/example/alpha")
    (wrong / "dirty.txt").write_text("unreviewed", encoding="utf-8")
    with pytest.raises(AuthorityLayerError, match="dirty"):
        layer.status("reader-client", "alpha")


def test_managed_repo_rejects_git_parser_differential_before_fsmonitor_runs(
    config, tmp_path: Path
) -> None:
    remote = "https://github.com/example/alpha"
    root = _repo(config.paths.data_root / "delivery/alpha", remote)
    marker = tmp_path / "authority-fsmonitor-ran"
    monitor = tmp_path / "authority-fsmonitor"
    monitor.write_text("#!/bin/sh\ntouch %s\n" % marker, encoding="utf-8")
    monitor.chmod(0o700)
    path = root / ".git/config"
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "\trepositoryformatversion = 0\n",
            "\trepositoryformatversion = 0\n\t\tfsmonitor = %s\n" % monitor,
            1,
        ),
        encoding="utf-8",
    )

    with pytest.raises(AuthorityLayerError, match="git_config_unsafe"):
        _managed_repo(
            config.paths.data_root / "delivery",
            "alpha",
            remote,
        )

    assert not marker.exists()


def _ref(value):
    from project_continuity.evidence import StableRef

    return StableRef.from_dict(value)
