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
    _isolated_teamai_checkout,
    _isolated_worktree,
    _managed_repo,
    _reconcile_teamai_branch,
    _teamai_request_marker,
    _verified_teamai_candidate,
)
from project_continuity.managed_git import managed_git_environment
from project_continuity.git_credential import credential_response
from project_continuity.teamai import render_teamai_guard_documents
from project_continuity.teamai_receipts import (
    TeamAIReceiptStore,
    authority_request_digest,
)
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
    def __init__(self, *, pulls=(), releases=(), collaboration=()) -> None:
        self.pulls = tuple(pulls)
        self.release_rows = tuple(releases)
        self.collaboration = tuple(collaboration)
        self.created_collaboration = []

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

    def collaboration_pull_requests(self, repo_url, *, deadline=None):
        del repo_url, deadline
        return ()

    def collaboration_pull_request(self, repo_url, pull_request, *, deadline=None):
        del repo_url, deadline
        return next(
            row
            for row in self.collaboration
            if row["pull_request"] == pull_request
        )

    def create_collaboration_pull_request(
        self,
        repo_url,
        *,
        head_ref,
        base_ref,
        subject,
        body,
        deadline=None,
    ):
        del deadline
        self.created_collaboration.append(
            {
                "base_ref": base_ref,
                "body": body,
                "head_ref": head_ref,
                "repo_url": repo_url,
                "subject": subject,
            }
        )
        return next(
            row for row in self.collaboration if row["head_ref"] == head_ref
        )


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
import json, os, pathlib, re, sys
if 'contribute' in sys.argv:
    expected = {
        'GIT_AUTHOR_EMAIL':'writer-agent@project-continuity.invalid',
        'GIT_AUTHOR_NAME':'writer-agent',
        'GIT_COMMITTER_EMAIL':'writer-agent@project-continuity.invalid',
        'GIT_COMMITTER_NAME':'writer-agent',
    }
    if any(os.environ.get(key) != value for key, value in expected.items()):
        raise SystemExit(9)
    if os.environ.get('GIT_CONFIG_KEY_1') != 'credential.helper':
        raise SystemExit(10)
    if os.environ.get('GIT_CONFIG_KEY_2') != 'credential.useHttpPath':
        raise SystemExit(10)
    if os.environ.get('GIT_CONFIG_VALUE_2') != 'true':
        raise SystemExit(10)
    if os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN'):
        raise SystemExit(11)
    title = sys.argv[sys.argv.index('--title') + 1]
    if not re.fullmatch(r'[0-9a-z]{50}', title):
        raise SystemExit(12)
    config = json.loads(pathlib.Path('.teamai/config.yaml').read_text())
    reports = pathlib.Path(config['repo']['localPath']) / 'reports-wt'
    reports.mkdir(parents=True, exist_ok=True)
    (reports / 'donor-owned-state').write_text('native worktree\\n')
    print('Branch teamai/push/writer-agent/canary has been pushed')
    print('Failed to create PR: GitHub authentication unavailable')
    print('You can create a PR manually')
else:
    if any(key.startswith('GIT_CONFIG_VALUE_') for key in os.environ):
        raise SystemExit(8)
    if any('Authorization:' in value or 'managed_github_token' in value for value in os.environ.values()):
        raise SystemExit(8)
    query = json.load(sys.stdin)['query']
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


def test_teamai_native_recall_and_reviewed_exact_get(
    config, tmp_path: Path, monkeypatch
) -> None:
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
    token_dir = tmp_path / "credentials"
    token_dir.mkdir(mode=0o700)
    token_file = token_dir / "github.token"
    token_file.write_text("managed_github_token_value_000001\n", encoding="ascii")
    token_file.chmod(0o600)
    monkeypatch.setenv("PROJECT_CONTINUITY_GITHUB_TOKEN_FILE", str(token_file))

    found = layer.search("reader-client", "alpha", "五工具", limit=8)
    reference = _ref(found[0]["reviewed_matches"][0]["stable_ref"])
    fetched = layer.get("reader-client", "alpha", reference)

    assert "TeamAI native recall" in found[0]["recall"]
    assert reference.authority == "teamai"
    assert fetched["title"] == "协作验收"
    assert "五工具" in fetched["content"]

    assert layer.search("reader-client", "alpha", "unrelated", limit=8) == []


def test_openspec_rejects_forged_revision_before_any_git_worktree_mutation(
    config, tmp_path: Path
) -> None:
    remote = "https://github.com/example/alpha-specs"
    root = _repo(config.paths.data_root / "openspec/alpha", remote)
    (root / "openspec").mkdir()
    (root / "openspec/config.yaml").write_text(
        "schema: spec-driven\n", encoding="utf-8"
    )
    _git(root, "add", "openspec")
    _git(root, "commit", "-m", "record exact decision")
    layer = OpenSpecLayer(
        config,
        OpenSpecBinding("alpha-specs", remote),
        _fake_openspec(tmp_path / "openspec-forged-ref"),
    )
    reference = dict(
        layer.search("reader-client", "alpha", "五工具", limit=8)[0][
            "stable_ref"
        ]
    )
    reference["version"] = "--lock"
    metadata = root / ".git/worktrees"
    before = _filesystem_snapshot(metadata)

    with pytest.raises(AuthorityLayerError, match="openspec_reference_invalid"):
        layer.get("reader-client", "alpha", _ref(reference))

    assert _filesystem_snapshot(metadata) == before


def test_openspec_exact_get_disables_post_checkout_hook(
    config, tmp_path: Path
) -> None:
    remote = "https://github.com/example/alpha-specs"
    root = _repo(config.paths.data_root / "openspec/alpha", remote)
    spec = root / "openspec/specs/authority-contract/spec.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("hook-free exact decision\n", encoding="utf-8")
    _git(root, "add", "openspec")
    _git(root, "commit", "-m", "record hook-free decision")
    marker = tmp_path / "post-checkout-ran"
    hook = root / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\ntouch '%s'\n" % marker, encoding="utf-8")
    hook.chmod(0o700)
    layer = OpenSpecLayer(
        config,
        OpenSpecBinding("alpha-specs", remote),
        _historical_openspec(tmp_path / "openspec-hook"),
    )
    reference = _ref(
        layer.search("reader-client", "alpha", "hook-free", limit=8)[0][
            "stable_ref"
        ]
    )

    fetched = layer.get("reader-client", "alpha", reference)

    assert fetched["payload"]["summary"] == "hook-free exact decision"
    assert not marker.exists()


def test_openspec_worktree_cleanup_removes_locked_metadata(
    config, tmp_path: Path
) -> None:
    root = _repo(
        config.paths.data_root / "openspec/alpha",
        "https://github.com/example/alpha-specs",
    )
    revision = _git(root, "rev-parse", "HEAD")

    with _isolated_worktree(root, revision, "locked-test") as snapshot:
        _git(root, "worktree", "lock", str(snapshot))

    assert _filesystem_snapshot(root / ".git/worktrees") == {}


def test_managed_git_environment_disables_hooks_and_composes_remote_auth(
    tmp_path: Path, monkeypatch
) -> None:
    local = managed_git_environment()
    assert local["GIT_CONFIG_COUNT"] == "1"
    assert local["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert local["GIT_CONFIG_VALUE_0"] == os.devnull

    token_dir = tmp_path / "credentials"
    token_dir.mkdir(mode=0o700)
    token_file = token_dir / "github.token"
    token_file.write_text("managed_github_token_value_000001\n", encoding="ascii")
    token_file.chmod(0o600)
    monkeypatch.setenv("PROJECT_CONTINUITY_GITHUB_TOKEN_FILE", str(token_file))
    remote = "https://github.com/example/private"
    authenticated = managed_git_environment(remote)
    assert authenticated["GIT_CONFIG_COUNT"] == "3"
    assert authenticated["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert authenticated["GIT_CONFIG_VALUE_0"] == os.devnull
    assert authenticated["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert authenticated["GIT_CONFIG_KEY_2"] == "credential.useHttpPath"
    assert authenticated["GIT_CONFIG_VALUE_2"] == "true"
    assert "managed_github_token_value_000001" not in json.dumps(authenticated)

    monkeypatch.setenv("PROJECT_CONTINUITY_MANAGED_GIT_REMOTE", remote)
    monkeypatch.setenv(
        "PROJECT_CONTINUITY_MANAGED_GIT_TOKEN_FILE", str(token_file)
    )
    approved = credential_response(
        "get", b"protocol=https\nhost=github.com\npath=example/private\n\n"
    )
    foreign = credential_response(
        "get", b"protocol=https\nhost=github.com\npath=example/other\n\n"
    )
    assert b"username=x-access-token" in approved
    assert b"managed_github_token_value_000001" in approved
    assert foreign == b""


def test_managed_git_credential_is_scoped_to_the_exact_repository(
    tmp_path: Path, monkeypatch
) -> None:
    token_dir = tmp_path / "credentials"
    token_dir.mkdir(mode=0o700)
    token_file = token_dir / "github.token"
    token_file.write_text("managed_github_token_value_000001\n", encoding="ascii")
    token_file.chmod(0o600)
    monkeypatch.setenv("PROJECT_CONTINUITY_GITHUB_TOKEN_FILE", str(token_file))
    environment = managed_git_environment("https://github.com/example/private")

    approved = subprocess.run(
        ["git", "credential", "fill"],
        env=environment,
        input=b"url=https://github.com/example/private\n\n",
        check=False,
        capture_output=True,
    )
    foreign = subprocess.run(
        ["git", "credential", "fill"],
        env=environment,
        input=b"url=https://github.com/example/other\n\n",
        check=False,
        capture_output=True,
    )

    assert approved.returncode == 0
    assert b"password=managed_github_token_value_000001" in approved.stdout
    assert foreign.returncode != 0
    assert b"managed_github_token_value_000001" not in foreign.stdout + foreign.stderr


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


def test_openspec_update_uses_native_validate_and_review_branch(
    config, tmp_path: Path, monkeypatch
) -> None:
    remote = "https://github.com/example/alpha-specs"
    root = _repo(config.paths.data_root / "openspec/alpha", remote)
    (root / "openspec").mkdir()
    (root / "openspec/config.yaml").write_text(
        "schema: spec-driven\n", encoding="utf-8"
    )
    _git(root, "add", "openspec/config.yaml")
    _git(root, "commit", "-m", "configure OpenSpec")
    layer = OpenSpecLayer(
        config,
        OpenSpecBinding("alpha-specs", remote),
        _fake_openspec(tmp_path / "openspec-write"),
    )
    monkeypatch.setattr(
        "project_continuity.authority_layers._remote_branch", lambda *_args: None
    )
    pushed = []
    monkeypatch.setattr(
        "project_continuity.authority_layers._push_branch",
        lambda _root, _worktree, branch, _remote: pushed.append(branch),
    )

    receipt = layer.update(
        "writer-client",
        "alpha",
        "prepare_change",
        {
            "change_id": "integrate-truth-plane",
            "artifacts": [
                {
                    "artifact_id": "proposal",
                    "relative_output": "proposal.md",
                    "body": "# Why\n\nRoute every authority through one surface.\n",
                }
            ],
        },
        expected_revision=_git(root, "rev-parse", "HEAD"),
    )

    assert receipt["actor"] == "writer-agent"
    assert receipt["review_state"] == "pending"
    assert pushed == [
        "project-continuity/openspec/writer-agent/prepare-change-integrate-truth-plane"
    ]
    assert _git(root, "status", "--porcelain=v1", "--untracked-files=all") == ""


def test_teamai_update_uses_derived_actor_and_write_only_remote_auth(
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
    _git(root, "commit", "-m", "configure TeamAI")
    entrypoint = tmp_path / "teamai.js"
    entrypoint.write_text("// fake\n", encoding="utf-8")
    candidate = {
        "base_ref": "main",
        "base_revision": "0" * 40,
        "body": "Contribute session knowledge: 统一验收",
        "head_ref": "teamai/push/writer-agent/canary",
        "head_revision": "1" * 40,
        "kind": "pull_request_candidate",
        "pull_request": 11,
        "state": "open",
        "subject": "[teamai] Contribute session knowledge from writer-agent",
        "url": "https://github.com/example/alpha-team/pull/11",
    }
    layer = TeamAILayer(
        config,
        TeamAIBinding("alpha-team", remote, ("reviewer-agent",)),
        _fake_node(tmp_path / "node-write"),
        entrypoint,
        github_resolver=FakeGitHubResolver(collaboration=(candidate,)),
    )
    token_dir = tmp_path / "credentials"
    token_dir.mkdir(mode=0o700)
    token_file = token_dir / "github.token"
    token_file.write_text("managed_github_token_value_000001\n", encoding="ascii")
    token_file.chmod(0o600)
    monkeypatch.setenv("PROJECT_CONTINUITY_GITHUB_TOKEN_FILE", str(token_file))
    monkeypatch.setattr(
        "project_continuity.authority_layers._verified_teamai_candidate",
        lambda *_args, **_kwargs: candidate,
    )
    monkeypatch.setattr(
        "project_continuity.authority_layers._reconcile_teamai_branch",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "project_continuity.authority_layers._verified_teamai_branch",
        lambda *_args, **_kwargs: {
            "head_ref": candidate["head_ref"],
            "head_revision": candidate["head_revision"],
        },
    )

    receipt = layer.update(
        "writer-client",
        "alpha",
        "contribute",
        {"title": "统一验收", "body": "# 协作记录\n\n同一五工具完成路由。\n"},
        expected_revision=_git(root, "rev-parse", "HEAD"),
    )

    assert receipt["actor"] == "writer-agent"
    assert receipt["pull_request"] == 11
    assert receipt["review_state"] == "pr_opened"
    assert receipt["source_revision"] == _git(root, "rev-parse", "HEAD")
    assert len(layer.github_resolver.created_collaboration) == 1
    assert "@project-continuity.invalid" not in json.dumps(receipt, sort_keys=True)
    assert not (root / ".teamai/config.yaml").exists()
    assert not (root / ".teamai/reports-wt").exists()
    assert _git(root, "status", "--porcelain=v1", "--untracked-files=all") == ""

    replayed = layer.update(
        "writer-client",
        "alpha",
        "contribute",
        {"title": "统一验收", "body": "# 协作记录\n\n同一五工具完成路由。\n"},
        expected_revision=_git(root, "rev-parse", "HEAD"),
    )
    assert replayed["changed"] is False
    assert replayed["operation_id"] == receipt["operation_id"]
    assert replayed["pull_request"] == receipt["pull_request"]

    (root / "unexpected-untracked.txt").write_text(
        "not donor state\n", encoding="utf-8"
    )
    with pytest.raises(AuthorityLayerError, match="managed_repo_is_dirty"):
        layer.status("writer-client", "alpha")


def test_teamai_exact_checkout_cannot_chase_a_newer_remote_main(
    tmp_path: Path,
) -> None:
    remote, source_revision = _bare_remote(tmp_path, "teamai-base", "source-a")
    managed = tmp_path / "managed-teamai"
    subprocess.run(
        ["git", "clone", remote.as_uri(), str(managed)],
        env=GIT_ENV,
        check=True,
        capture_output=True,
    )
    author = tmp_path / "teamai-base-author"
    (author / "README.md").write_text("remote-b\n", encoding="utf-8")
    _git(author, "add", "README.md")
    _git(author, "commit", "-m", "advance remote only")
    remote_revision = _git(author, "rev-parse", "HEAD")
    _git(author, "push", "origin", "main")
    assert _git(managed, "rev-parse", "HEAD") == source_revision
    assert remote_revision != source_revision

    runtime_root = tmp_path / "runtime"
    with _isolated_teamai_checkout(
        managed,
        source_revision,
        project_id="alpha",
        actor="writer-agent",
        push_remote=remote.as_uri(),
        runtime_root=runtime_root,
    ) as exact:
        _git(exact, "fetch", "origin", "main")
        assert _git(exact, "rev-parse", "origin/main") == source_revision
        assert _git(exact, "remote", "get-url", "--push", "origin") == remote.as_uri()
        assert _git(exact, "rev-parse", "HEAD") == source_revision


def test_teamai_candidate_is_read_back_from_exact_parent_content_and_actor(
    tmp_path: Path,
) -> None:
    remote, source_revision = _bare_remote(tmp_path, "teamai-candidate", "base")
    author = tmp_path / "teamai-candidate-author"
    branch = "teamai/push/writer-agent/20260905-120000"
    title = "Durable"
    body = "# Durable collaboration\n\nOne exact contribution.\n"
    request_digest = authority_request_digest(
        principal_id="writer-client",
        project_id="teamai-candidate",
        target="collaboration",
        operation="contribute",
        parameters={"body": body, "title": title},
        expected_revision=source_revision,
    )
    _git(author, "switch", "-c", branch)
    learning = author / (
        ".teamai/learnings/"
        + _teamai_request_marker(request_digest)
        + "-2026-09-05-canary.md"
    )
    learning.parent.mkdir(parents=True)
    learning.write_text(body, encoding="utf-8")
    _git(author, "add", str(learning.relative_to(author)))
    environment = {
        **GIT_ENV,
        "GIT_AUTHOR_EMAIL": "writer-agent@project-continuity.invalid",
        "GIT_AUTHOR_NAME": "writer-agent",
        "GIT_COMMITTER_EMAIL": "writer-agent@project-continuity.invalid",
        "GIT_COMMITTER_NAME": "writer-agent",
    }
    subprocess.run(
        ["git", "commit", "-m", "[teamai] Contribute session knowledge from writer-agent"],
        cwd=author,
        env=environment,
        check=True,
        capture_output=True,
    )
    head = _git(author, "rev-parse", "HEAD")
    _git(author, "push", "origin", branch)
    _git(remote, "update-ref", "refs/pull/17/head", head)
    _git(author, "push", "origin", "--delete", branch)
    assert _git(author, "ls-remote", "--heads", "origin", branch) == ""
    candidate = {
        "base_ref": "main",
        "base_revision": source_revision,
        "body": "Contribute session knowledge: Durable",
        "head_ref": branch,
        "head_revision": head,
        "kind": "pull_request_candidate",
        "pull_request": 17,
        "state": "open",
        "subject": "[teamai] Contribute session knowledge from writer-agent",
        "url": "https://github.com/example/teamai-candidate/pull/17",
    }
    receipt = {
        "actor": "writer-agent",
        "request_digest": request_digest,
        "source_revision": source_revision,
    }

    assert _verified_teamai_candidate(
        author,
        remote.as_uri(),
        candidate,
        receipt,
        title=title,
        body=body,
        base_branch="main",
    ) == candidate


def test_teamai_prepared_replay_discovers_one_exact_pushed_branch(
    tmp_path: Path,
) -> None:
    remote, source_revision = _bare_remote(tmp_path, "teamai-branch", "base")
    author = tmp_path / "teamai-branch-author"
    branch = "teamai/push/writer-agent/20260905-121500"
    title = "Branch recovery"
    body = "# Branch recovery\n\nThe first response was lost.\n"
    request_digest = authority_request_digest(
        principal_id="writer-client",
        project_id="teamai-branch",
        target="collaboration",
        operation="contribute",
        parameters={"body": body, "title": title},
        expected_revision=source_revision,
    )
    _git(author, "switch", "-c", branch)
    learning = author / (
        ".teamai/learnings/"
        + _teamai_request_marker(request_digest)
        + "-2026-09-05-canary.md"
    )
    learning.parent.mkdir(parents=True)
    learning.write_text(body, encoding="utf-8")
    _git(author, "add", str(learning.relative_to(author)))
    environment = {
        **GIT_ENV,
        "GIT_AUTHOR_EMAIL": "writer-agent@project-continuity.invalid",
        "GIT_AUTHOR_NAME": "writer-agent",
        "GIT_COMMITTER_EMAIL": "writer-agent@project-continuity.invalid",
        "GIT_COMMITTER_NAME": "writer-agent",
    }
    subprocess.run(
        [
            "git",
            "commit",
            "-m",
            "[teamai] Contribute session knowledge from writer-agent",
        ],
        cwd=author,
        env=environment,
        check=True,
        capture_output=True,
    )
    head = _git(author, "rev-parse", "HEAD")
    _git(author, "push", "origin", branch)

    recovered = _reconcile_teamai_branch(
        author,
        remote.as_uri(),
        {
            "actor": "writer-agent",
            "request_digest": request_digest,
            "source_revision": source_revision,
        },
        body=body,
    )

    assert recovered == {"head_ref": branch, "head_revision": head}


def test_teamai_branch_recovery_cannot_cross_adopt_same_body_different_title(
    tmp_path: Path,
) -> None:
    remote, source_revision = _bare_remote(tmp_path, "teamai-branch-title", "base")
    author = tmp_path / "teamai-branch-title-author"
    body = "# Same body\n\nTitles identify different operations.\n"
    receipts = []
    expected = []
    for index, title in enumerate(("First title", "Second title"), start=1):
        request_digest = authority_request_digest(
            principal_id="writer-client",
            project_id="teamai-branch-title",
            target="collaboration",
            operation="contribute",
            parameters={"body": body, "title": title},
            expected_revision=source_revision,
        )
        branch = "teamai/push/writer-agent/20260905-12150%d" % index
        _git(author, "switch", "--detach", source_revision)
        _git(author, "switch", "-c", branch)
        relative = (
            ".teamai/learnings/"
            + _teamai_request_marker(request_digest)
            + "-2026-09-05-canary.md"
        )
        learning = author / relative
        learning.parent.mkdir(parents=True, exist_ok=True)
        learning.write_text(body, encoding="utf-8")
        _git(author, "add", relative)
        environment = {
            **GIT_ENV,
            "GIT_AUTHOR_EMAIL": "writer-agent@project-continuity.invalid",
            "GIT_AUTHOR_NAME": "writer-agent",
            "GIT_COMMITTER_EMAIL": "writer-agent@project-continuity.invalid",
            "GIT_COMMITTER_NAME": "writer-agent",
        }
        subprocess.run(
            [
                "git",
                "commit",
                "-m",
                "[teamai] Contribute session knowledge from writer-agent",
            ],
            cwd=author,
            env=environment,
            check=True,
            capture_output=True,
        )
        head = _git(author, "rev-parse", "HEAD")
        _git(author, "push", "origin", branch)
        receipts.append(
            {
                "actor": "writer-agent",
                "request_digest": request_digest,
                "source_revision": source_revision,
            }
        )
        expected.append({"head_ref": branch, "head_revision": head})

    for receipt, candidate in zip(receipts, expected):
        assert (
            _reconcile_teamai_branch(
                author,
                remote.as_uri(),
                receipt,
                body=body,
            )
            == candidate
        )


def test_teamai_request_marker_preserves_the_complete_sha256_identity() -> None:
    digest = "sha256:" + "f" * 64
    marker = _teamai_request_marker(digest)

    assert len(marker) == 50
    assert marker.isalnum()
    assert int(marker, 36) == int(digest.removeprefix("sha256:"), 16)


def test_teamai_prepared_receipt_reconciles_without_invoking_donor_again(
    config, tmp_path: Path, monkeypatch
) -> None:
    remote = "https://github.com/example/alpha-team"
    root = _repo(config.paths.data_root / "team/alpha", remote)
    for relative, content in render_teamai_guard_documents(
        team_id="alpha-team", repo_url=remote, reviewers=("reviewer-agent",)
    ).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", ".teamai")
    _git(root, "commit", "-m", "configure TeamAI")
    revision = _git(root, "rev-parse", "HEAD")
    title = "Lost response"
    body = "# Recovered\n\nDo not create a second PR.\n"
    digest = authority_request_digest(
        principal_id="writer-client",
        project_id="alpha",
        target="collaboration",
        operation="contribute",
        parameters={"body": body, "title": title},
        expected_revision=revision,
    )
    receipt_store = TeamAIReceiptStore(config.paths.state_root)
    prepared, _created = receipt_store.prepare(
        actor="writer-agent",
        project_id="alpha",
        request_digest=digest,
        source_revision=revision,
    )
    candidate = {
        "base_ref": "main",
        "base_revision": revision,
        "body": "Contribute session knowledge: " + title,
        "head_ref": "teamai/push/writer-agent/recovered",
        "head_revision": "f" * 40,
        "kind": "pull_request_candidate",
        "pull_request": 23,
        "state": "open",
        "subject": "[teamai] Contribute session knowledge from writer-agent",
        "url": "https://github.com/example/alpha-team/pull/23",
    }
    receipt_store.publish_branch(
        prepared,
        branch=candidate["head_ref"],
        head_revision=candidate["head_revision"],
    )
    marker = tmp_path / "donor-was-called"
    node = tmp_path / "node-never"
    node.write_text(
        "#!/bin/sh\nprintf called > %s\nexit 99\n" % marker,
        encoding="utf-8",
    )
    node.chmod(0o700)
    entrypoint = tmp_path / "teamai.js"
    entrypoint.write_text("// fake\n", encoding="utf-8")
    layer = TeamAILayer(
        config,
        TeamAIBinding("alpha-team", remote, ("reviewer-agent",)),
        node,
        entrypoint,
        github_resolver=FakeGitHubResolver(collaboration=(candidate,)),
    )
    monkeypatch.setattr(
        layer.github_resolver,
        "collaboration_pull_requests",
        lambda *_args, **_kwargs: (candidate,),
    )
    monkeypatch.setattr(
        "project_continuity.authority_layers._verified_teamai_candidate",
        lambda *_args, **_kwargs: candidate,
    )

    result = layer.update(
        "writer-client",
        "alpha",
        "contribute",
        {"title": title, "body": body},
        expected_revision=revision,
    )

    assert result["changed"] is False
    assert result["operation_id"] == "authority:" + digest.removeprefix("sha256:")
    assert result["pull_request"] == 23
    assert not marker.exists()


def test_teamai_prepared_receipt_without_remote_effect_resumes_after_restart(
    config, tmp_path: Path, monkeypatch
) -> None:
    remote = "https://github.com/example/alpha-team"
    root = _repo(config.paths.data_root / "team/alpha", remote)
    for relative, content in render_teamai_guard_documents(
        team_id="alpha-team", repo_url=remote, reviewers=("reviewer-agent",)
    ).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", ".teamai")
    _git(root, "commit", "-m", "configure TeamAI")
    revision = _git(root, "rev-parse", "HEAD")
    title = "Resume after process death"
    body = "# Resume\n\nNo remote effect existed before restart.\n"
    digest = authority_request_digest(
        principal_id="writer-client",
        project_id="alpha",
        target="collaboration",
        operation="contribute",
        parameters={"body": body, "title": title},
        expected_revision=revision,
    )
    TeamAIReceiptStore(config.paths.state_root).prepare(
        actor="writer-agent",
        project_id="alpha",
        request_digest=digest,
        source_revision=revision,
    )
    marker = tmp_path / "donor-calls"
    node = tmp_path / "node-resume"
    node.write_text(
        """#!/usr/bin/env python3
import os, pathlib
if os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN'):
    raise SystemExit(12)
path = pathlib.Path(%r)
path.write_text(path.read_text() + 'called\\n' if path.exists() else 'called\\n')
print('Branch teamai/push/writer-agent/restarted has been pushed')
print('Failed to create PR: GitHub authentication unavailable')
print('You can create a PR manually')
""" % str(marker),
        encoding="utf-8",
    )
    node.chmod(0o700)
    entrypoint = tmp_path / "teamai.js"
    entrypoint.write_text("// fake\n", encoding="utf-8")
    candidate = {
        "base_ref": "main",
        "base_revision": revision,
        "body": "Contribute session knowledge: " + title,
        "head_ref": "teamai/push/writer-agent/restarted",
        "head_revision": "d" * 40,
        "kind": "pull_request_candidate",
        "pull_request": 31,
        "state": "open",
        "subject": "[teamai] Contribute session knowledge from writer-agent",
        "url": "https://github.com/example/alpha-team/pull/31",
    }
    resolver = FakeGitHubResolver(collaboration=(candidate,))
    layer = TeamAILayer(
        config,
        TeamAIBinding("alpha-team", remote, ("reviewer-agent",)),
        node,
        entrypoint,
        github_resolver=resolver,
    )
    monkeypatch.setattr(
        "project_continuity.authority_layers._reconcile_teamai_branch",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "project_continuity.authority_layers._verified_teamai_branch",
        lambda *_args, **_kwargs: {
            "head_ref": candidate["head_ref"],
            "head_revision": candidate["head_revision"],
        },
    )
    monkeypatch.setattr(
        "project_continuity.authority_layers._verified_teamai_candidate",
        lambda *_args, **_kwargs: candidate,
    )

    recovered = layer.update(
        "writer-client",
        "alpha",
        "contribute",
        {"title": title, "body": body},
        expected_revision=revision,
    )
    restarted_layer = TeamAILayer(
        config,
        TeamAIBinding("alpha-team", remote, ("reviewer-agent",)),
        node,
        entrypoint,
        github_resolver=FakeGitHubResolver(collaboration=(candidate,)),
    )
    replayed = restarted_layer.update(
        "writer-client",
        "alpha",
        "contribute",
        {"title": title, "body": body},
        expected_revision=revision,
    )

    assert recovered["changed"] is False
    assert replayed == {**recovered, "changed": False}
    assert marker.read_text(encoding="utf-8") == "called\n"
    assert len(resolver.created_collaboration) == 1


def test_teamai_prepared_receipt_adopts_pushed_branch_before_creating_pr(
    config, tmp_path: Path, monkeypatch
) -> None:
    remote = "https://github.com/example/alpha-team"
    root = _repo(config.paths.data_root / "team/alpha", remote)
    for relative, content in render_teamai_guard_documents(
        team_id="alpha-team", repo_url=remote, reviewers=("reviewer-agent",)
    ).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _git(root, "add", ".teamai")
    _git(root, "commit", "-m", "configure TeamAI")
    revision = _git(root, "rev-parse", "HEAD")
    title = "Branch response lost"
    body = "# Branch exists\n\nCreate exactly one PR.\n"
    digest = authority_request_digest(
        principal_id="writer-client",
        project_id="alpha",
        target="collaboration",
        operation="contribute",
        parameters={"body": body, "title": title},
        expected_revision=revision,
    )
    TeamAIReceiptStore(config.paths.state_root).prepare(
        actor="writer-agent",
        project_id="alpha",
        request_digest=digest,
        source_revision=revision,
    )
    branch = {
        "head_ref": "teamai/push/writer-agent/already-pushed",
        "head_revision": "e" * 40,
    }
    candidate = {
        "base_ref": "main",
        "base_revision": revision,
        "body": "Contribute session knowledge: " + title,
        **branch,
        "kind": "pull_request_candidate",
        "pull_request": 32,
        "state": "open",
        "subject": "[teamai] Contribute session knowledge from writer-agent",
        "url": "https://github.com/example/alpha-team/pull/32",
    }
    marker = tmp_path / "donor-must-not-run"
    node = tmp_path / "node-never"
    node.write_text("#!/bin/sh\nprintf called > %s\nexit 99\n" % marker)
    node.chmod(0o700)
    entrypoint = tmp_path / "teamai.js"
    entrypoint.write_text("// fake\n", encoding="utf-8")
    resolver = FakeGitHubResolver(collaboration=(candidate,))
    layer = TeamAILayer(
        config,
        TeamAIBinding("alpha-team", remote, ("reviewer-agent",)),
        node,
        entrypoint,
        github_resolver=resolver,
    )
    monkeypatch.setattr(
        "project_continuity.authority_layers._reconcile_teamai_branch",
        lambda *_args, **_kwargs: branch,
    )
    monkeypatch.setattr(
        "project_continuity.authority_layers._verified_teamai_branch",
        lambda *_args, **_kwargs: branch,
    )
    monkeypatch.setattr(
        "project_continuity.authority_layers._verified_teamai_candidate",
        lambda *_args, **_kwargs: candidate,
    )

    recovered = layer.update(
        "writer-client",
        "alpha",
        "contribute",
        {"title": title, "body": body},
        expected_revision=revision,
    )

    assert recovered["changed"] is False
    assert recovered["pull_request"] == 32
    assert not marker.exists()
    assert len(resolver.created_collaboration) == 1


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


def _filesystem_snapshot(root: Path):
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): (
            "symlink:" + os.readlink(path)
            if path.is_symlink()
            else "directory"
            if path.is_dir()
            else path.read_bytes()
        )
        for path in root.rglob("*")
        if path.is_symlink() or path.is_dir() or path.is_file()
    }
