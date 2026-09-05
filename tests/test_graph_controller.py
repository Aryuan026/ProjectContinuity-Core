import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from project_continuity.graph_controller import (
    GraphControllerError,
    GraphSnapshotController,
)
from project_continuity.graph_router import GraphRegistry


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_AUTHOR_NAME": "Test Agent",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test Agent",
}


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        env=GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _delivery_repo(config) -> tuple[Path, str]:
    root = config.paths.data_root / "delivery/alpha"
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "remote", "add", "origin", "https://github.com/example/alpha")
    (root / "app.py").write_text("def entry():\n    return worker()\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-m", "add entry")
    return root, _git(root, "rev-parse", "HEAD")


def _committed_blob(root: Path, revision: str, relative_path: str) -> bytes:
    return subprocess.run(
        ["git", "cat-file", "blob", "%s:%s" % (revision, relative_path)],
        cwd=root,
        env=GIT_ENV,
        check=True,
        capture_output=True,
    ).stdout


def _attribute_delivery_repo(
    config, attribute: str, working_bytes: bytes
) -> tuple[Path, str, bytes]:
    root = config.paths.data_root / "delivery/alpha"
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "remote", "add", "origin", "https://github.com/example/alpha")
    (root / ".gitattributes").write_text("app.py %s\n" % attribute, encoding="utf-8")
    (root / "app.py").write_bytes(working_bytes)
    _git(root, "add", ".gitattributes", "app.py")
    _git(root, "commit", "-m", "add attributed source")
    revision = _git(root, "rev-parse", "HEAD")
    committed = _committed_blob(root, revision, "app.py")
    (root / "app.py").unlink()
    _git(root, "checkout", "--", "app.py")
    assert (root / "app.py").read_bytes() != committed
    return root, revision, committed


def _built_graph_files(controller, revision: str) -> tuple[bytes, bytes]:
    root = controller.registry.committed_output_root(
        "alpha", revision, "alpha-%s" % revision[:12]
    ) / "graphify-out"
    return (root / "graph.json").read_bytes(), (root / "manifest.json").read_bytes()


def _symlink_delivery_repo(config, *, chained: bool) -> tuple[Path, str]:
    root = config.paths.data_root / "delivery/alpha"
    root.mkdir(parents=True)
    _git(root, "init", "-b", "main")
    _git(root, "remote", "add", "origin", "https://github.com/example/alpha")
    if chained:
        (root / "hop.py").symlink_to(".git/config")
        (root / "alias.py").symlink_to("hop.py")
        _git(root, "add", "alias.py", "hop.py")
    else:
        (root / "alias.py").symlink_to(".git/config")
        _git(root, "add", "alias.py")
    _git(root, "commit", "-m", "add source symlink")
    return root, _git(root, "rev-parse", "HEAD")


def _recording_graphify(path: Path, marker: Path) -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "Path(%r).write_text('invoked')\n" % str(marker),
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _fake_graphify(path: Path) -> Path:
    path.write_text(
        """#!/usr/bin/env python3
import hashlib, json, pathlib, subprocess, sys
if sys.argv[1] == '--version':
    print('graphify 0.9.48')
    raise SystemExit(0)
if sys.argv[1] != 'extract':
    raise SystemExit(2)
source = pathlib.Path(sys.argv[2])
target = pathlib.Path(sys.argv[sys.argv.index('--out') + 1]) / 'graphify-out'
target.mkdir(parents=True)
revision = subprocess.check_output(['git','rev-parse','HEAD'], cwd=source, text=True).strip()
for key in ('core.excludesfile', 'core.fsmonitor', 'include.path', 'r2.marker'):
    visible = subprocess.run(
        ['git', 'config', '--get', key],
        cwd=source,
        text=True,
        capture_output=True,
    )
    if visible.returncode == 0:
        raise SystemExit(9)
(target / 'graph.json').write_text(json.dumps({
    'app_sha256': hashlib.sha256((source / 'app.py').read_bytes()).hexdigest(),
    'built_at_commit': revision,
    'detached_head': subprocess.run(
        ['git', 'symbolic-ref', '-q', 'HEAD'], cwd=source, capture_output=True
    ).returncode != 0,
    'directed': False,
    'graph': {},
    'links': [{'relation':'calls','source':'entry','target':'worker'}],
    'multigraph': False,
    'nodes': [
        {'id':'entry','label':'entry','source_file':'app.py'},
        {'id':'worker','label':'worker','source_file':'app.py'},
    ],
}, sort_keys=True))
(target / 'manifest.json').write_text(json.dumps({'app.py': {'ast_hash':'one','semantic_hash':''}}, sort_keys=True))
""",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def test_controller_builds_exact_committed_graph_and_replay_is_idempotent(
    config, tmp_path: Path
) -> None:
    root, revision = _delivery_repo(config)
    controller = GraphSnapshotController(config, _fake_graphify(tmp_path / "graphify"))

    first = controller.register_committed(
        "alpha",
        {"commit_sha": revision},
        actor="writer-agent",
        expected_revision="absent",
    )
    second = controller.register_committed(
        "alpha",
        {"commit_sha": revision},
        actor="writer-agent",
        expected_revision=revision,
    )

    current = GraphRegistry(config).resolve("alpha", selector="current_canonical")
    assert first["changed"] is True
    assert first["actor"] == "writer-agent"
    assert second["changed"] is False
    assert first["current"]["stable_ref"] == second["current"]["stable_ref"]
    assert current.commit_sha == revision
    assert current.coverage.nodes == 2
    graph = json.loads(
        (
            controller.registry.committed_output_root(
                "alpha", revision, "alpha-%s" % revision[:12]
            )
            / "graphify-out/graph.json"
        ).read_text(encoding="utf-8")
    )
    assert graph["built_at_commit"] == revision
    assert graph["detached_head"] is True
    assert graph["app_sha256"] == hashlib.sha256(
        _committed_blob(root, revision, "app.py")
    ).hexdigest()


@pytest.mark.parametrize(
    ("attribute", "working_bytes"),
    [
        ("text eol=crlf", b"def entry():\n    return worker()\n"),
        ("ident", b'IDENT = "$Id$"\n'),
        (
            "working-tree-encoding=UTF-16LE",
            "def entry():\n    return worker()\n".encode("utf-16-le"),
        ),
    ],
)
def test_controller_graphify_observes_exact_blob_bytes_despite_attributes(
    config, tmp_path: Path, attribute: str, working_bytes: bytes
) -> None:
    _root, revision, committed = _attribute_delivery_repo(
        config, attribute, working_bytes
    )
    controller = GraphSnapshotController(config, _fake_graphify(tmp_path / "graphify"))

    receipt = controller.register_committed(
        "alpha",
        {"commit_sha": revision},
        actor="writer-agent",
        expected_revision="absent",
    )

    graph_bytes, _manifest_bytes = _built_graph_files(controller, revision)
    graph = json.loads(graph_bytes)
    assert graph["app_sha256"] == hashlib.sha256(committed).hexdigest()
    assert receipt["current"]["stable_ref"]["version"] == revision


def test_two_fresh_exact_materializations_converge_on_one_graph_identity(
    config, tmp_path: Path
) -> None:
    _root, revision, committed = _attribute_delivery_repo(
        config,
        "text eol=crlf",
        b"def entry():\n    return worker()\n",
    )
    executable = _fake_graphify(tmp_path / "graphify")
    first_controller = GraphSnapshotController(config, executable)
    first = first_controller.register_committed(
        "alpha",
        {"commit_sha": revision},
        actor="writer-agent",
        expected_revision="absent",
    )
    first_graph, first_manifest = _built_graph_files(first_controller, revision)

    shutil.rmtree(first_controller.registry.root)
    second_controller = GraphSnapshotController(config, executable)
    second = second_controller.register_committed(
        "alpha",
        {"commit_sha": revision},
        actor="writer-agent",
        expected_revision="absent",
    )
    second_graph, second_manifest = _built_graph_files(second_controller, revision)

    assert json.loads(first_graph)["app_sha256"] == hashlib.sha256(committed).hexdigest()
    assert first_graph == second_graph
    assert first_manifest == second_manifest
    assert first["current"]["graph_digest"] == second["current"]["graph_digest"]
    assert first["current"]["manifest_digest"] == second["current"]["manifest_digest"]
    assert first["current"]["stable_ref"] == second["current"]["stable_ref"]


@pytest.mark.parametrize("chained", [False, True])
def test_controller_rejects_git_symlink_before_graphify_or_registry_write(
    config, tmp_path: Path, chained: bool
) -> None:
    root, revision = _symlink_delivery_repo(config, chained=chained)
    assert _committed_blob(root, revision, "alias.py") == (
        b"hop.py" if chained else b".git/config"
    )
    marker = tmp_path / "graphify-invoked"
    controller = GraphSnapshotController(
        config, _recording_graphify(tmp_path / "graphify", marker)
    )

    with pytest.raises(GraphControllerError, match="snapshot_symlink_forbidden"):
        controller.register_committed(
            "alpha",
            {"commit_sha": revision},
            actor="writer-agent",
            expected_revision="absent",
        )

    output = controller.registry.committed_output_root(
        "alpha", revision, "alpha-%s" % revision[:12]
    )
    assert marker.exists() is False
    assert output.exists() is False
    assert controller.registry.registry_path.exists() is False


def test_controller_rejects_stale_cas_and_dirty_delivery_checkout(
    config, tmp_path: Path
) -> None:
    root, revision = _delivery_repo(config)
    controller = GraphSnapshotController(config, _fake_graphify(tmp_path / "graphify"))
    with pytest.raises(GraphControllerError, match="expected_revision_conflict"):
        controller.register_committed(
            "alpha",
            {"commit_sha": revision},
            actor="writer-agent",
            expected_revision="f" * 40,
        )

    (root / "dirty.py").write_text("SECRET = 'not indexed'\n", encoding="utf-8")
    with pytest.raises(GraphControllerError, match="checkout_dirty"):
        controller.register_committed(
            "alpha",
            {"commit_sha": revision},
            actor="writer-agent",
            expected_revision="absent",
        )
    assert not (config.paths.data_root / "graphs/registry.json").exists()


def test_controller_rejects_dirty_checkout_hidden_by_global_excludes(
    config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, revision = _delivery_repo(config)
    ignored = root / "dirty.py"
    ignored.write_text("SECRET = 'still untracked'\n", encoding="utf-8")
    home = tmp_path / "ambient-home"
    home.mkdir()
    excludes = home / "global-excludes"
    excludes.write_text("dirty.py\n", encoding="utf-8")
    (home / ".gitconfig").write_text(
        "[core]\n\texcludesFile = %s\n" % excludes,
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    controller = GraphSnapshotController(config, _fake_graphify(tmp_path / "graphify"))

    with pytest.raises(GraphControllerError, match="checkout_dirty"):
        controller.register_committed(
            "alpha",
            {"commit_sha": revision},
            actor="writer-agent",
            expected_revision="absent",
        )
    assert not (config.paths.data_root / "graphs/registry.json").exists()


@pytest.mark.parametrize("ambient", ["excludes", "fsmonitor", "include"])
def test_controller_and_graphify_ignore_ambient_global_git_config(
    config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ambient: str,
) -> None:
    _root, revision = _delivery_repo(config)
    home = tmp_path / "ambient-home"
    home.mkdir()
    if ambient == "excludes":
        excluded = home / "global-excludes"
        excluded.write_text("*.py\n", encoding="utf-8")
        body = "[core]\n\texcludesFile = %s\n" % excluded
    elif ambient == "fsmonitor":
        body = "[core]\n\tfsmonitor = /definitely/not/a/managed-hook\n"
    else:
        included = home / "included.gitconfig"
        included.write_text("[r2]\n\tmarker = visible\n", encoding="utf-8")
        body = "[include]\n\tpath = %s\n" % included
    (home / ".gitconfig").write_text(body, encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    controller = GraphSnapshotController(config, _fake_graphify(tmp_path / "graphify"))

    receipt = controller.register_committed(
        "alpha",
        {"commit_sha": revision},
        actor="writer-agent",
        expected_revision="absent",
    )

    assert receipt["ok"] is True
    assert receipt["current"]["stable_ref"]["version"] == revision


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("core.excludesFile", "/tmp/unmanaged-excludes"),
        ("core.fsmonitor", "/tmp/unmanaged-fsmonitor"),
        ("include.path", "/tmp/unmanaged-include"),
    ],
)
def test_controller_rejects_unsafe_local_git_config(
    config, tmp_path: Path, key: str, value: str
) -> None:
    root, revision = _delivery_repo(config)
    _git(root, "config", key, value)
    controller = GraphSnapshotController(config, _fake_graphify(tmp_path / "graphify"))

    with pytest.raises(GraphControllerError, match="checkout_unsafe"):
        controller.register_committed(
            "alpha",
            {"commit_sha": revision},
            actor="writer-agent",
            expected_revision="absent",
        )
    assert not (config.paths.data_root / "graphs/registry.json").exists()


def test_controller_registers_prebuilt_overlay_without_accepting_a_caller_path(
    config, tmp_path: Path
) -> None:
    _root, revision = _delivery_repo(config)
    controller = GraphSnapshotController(config, _fake_graphify(tmp_path / "graphify"))
    digest = "sha256:" + "d" * 64
    snapshot_id = "alpha-working-overlay"
    output = controller.registry.overlay_output_root(
        "alpha", revision, digest, snapshot_id
    )
    graph_root = output / "graphify-out"
    graph_root.mkdir(parents=True)
    controller.registry.root.chmod(0o700)
    (graph_root / "graph.json").write_text(
        json.dumps(
            {
                "built_at_commit": revision,
                "directed": False,
                "graph": {},
                "links": [],
                "multigraph": False,
                "nodes": [
                    {"id": "dirty-entry", "label": "dirty-entry", "source_file": "app.py"}
                ],
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    (graph_root / "manifest.json").write_text(
        json.dumps({"app.py": {"ast_hash": "overlay", "semantic_hash": ""}}),
        encoding="utf-8",
    )
    arguments = {
        "base_sha": revision,
        "evidence_time": "2026-08-30T12:00:00Z",
        "overlay_digest": digest,
        "snapshot_id": snapshot_id,
    }

    first = controller.register_overlay(
        "alpha", arguments, actor="writer-agent", expected_revision="absent"
    )
    second = controller.register_overlay(
        "alpha",
        arguments,
        actor="writer-agent",
        expected_revision=first["current"]["stable_ref"]["version"],
    )

    assert first["changed"] is True
    assert first["operation"] == "register_overlay"
    assert second["changed"] is False
    assert GraphRegistry(config).resolve(
        "alpha", selector="working_overlay"
    ).snapshot_id == snapshot_id
    with pytest.raises(GraphControllerError, match="arguments_malformed"):
        controller.register_overlay(
            "alpha",
            {**arguments, "project_path": "/tmp/forbidden"},
            actor="writer-agent",
            expected_revision=first["current"]["stable_ref"]["version"],
        )
