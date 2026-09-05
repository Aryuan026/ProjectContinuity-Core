import json
import os
from pathlib import Path
import subprocess
import sys
import stat

import pytest

import project_continuity.truth_setup as truth_setup
from project_continuity import cli
from project_continuity.config import Config, ProjectConfig
from project_continuity.truth_setup import (
    TruthSetupError,
    _clone_repo,
    _fetch_fast_forward,
    _git_environment,
    _verify_repo,
    install_truth_plane,
    refresh_truth_plane,
)
from project_continuity.runtime_lock import runtime_lifetime_lock


GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_AUTHOR_NAME": "Test Agent",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test Agent",
}


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(
        ["git", *arguments],
        cwd=root,
        env=GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    )


def _git_output(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        env=GIT_ENV,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _detached_target(root: Path, name: str) -> tuple[str, str]:
    before = _git_output(root, "rev-parse", "HEAD")
    (root / name).write_text("reviewed target\n", encoding="utf-8")
    _git(root, "add", name)
    _git(root, "commit", "-m", "reviewed target")
    target = _git_output(root, "rev-parse", "HEAD")
    _git(root, "reset", "--hard", before)
    return before, target


def _fake_clone(remote: str, destination: Path) -> None:
    destination.mkdir(parents=True)
    _git(destination, "init", "-b", "main")
    _git(destination, "remote", "add", "origin", remote)
    (destination / "README.md").write_text("managed truth\n", encoding="utf-8")
    _git(destination, "add", "README.md")
    _git(destination, "commit", "-m", "initial truth")


def _declaration(
    path: Path,
    *,
    include_openspec: bool = True,
    include_teamai: bool = True,
    team_repo: str = "https://github.com/example/team",
) -> Path:
    openspec = (
        {
            "repo_url": "https://github.com/example/specs",
            "store_id": "alpha-specs",
        }
        if include_openspec
        else None
    )
    teamai = (
        {
            "repo_url": team_repo,
            "reviewers": ["reviewer-agent"],
            "team_id": "alpha-team",
        }
        if include_teamai
        else None
    )
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "project_id": "alpha",
                "openspec": openspec,
                "teamai": teamai,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_truth_setup_installs_all_managed_repos_and_replays(
    config, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("project_continuity.truth_setup._clone_repo", _fake_clone)
    declaration = _declaration(tmp_path / "truth.json")

    first = install_truth_plane(config, declaration)
    second = install_truth_plane(config, declaration)

    assert first == {
        "changed": True,
        "installed_layers": ["delivery", "openspec", "teamai"],
        "ok": True,
        "project_id": "alpha",
        "restart_required": True,
    }
    assert second["changed"] is False
    assert second["restart_required"] is False
    for relative in ("delivery/alpha", "openspec/alpha", "team/alpha"):
        assert (config.paths.data_root / relative / ".git").is_dir()
    binding = json.loads(
        (config.paths.data_root / "truth-plane/bindings.json").read_text(
            encoding="utf-8"
        )
    )
    assert binding["projects"]["alpha"]["teamai"]["team_id"] == "alpha-team"
    assert (config.paths.data_root / "truth-plane/bindings.json").stat().st_mode & 0o077 == 0


def test_clone_normalizes_checkout_root_to_owner_private_mode(
    tmp_path: Path, monkeypatch
) -> None:
    destination = tmp_path / "checkout"
    invocation = {}

    def fake_run(*_args, **_kwargs):
        invocation.update(_kwargs)
        destination.mkdir(mode=0o775)
        destination.chmod(0o775)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr("project_continuity.truth_setup.subprocess.run", fake_run)

    _clone_repo("https://github.com/example/private", destination)

    assert destination.stat().st_mode & 0o077 == 0
    assert invocation["umask"] == 0o077


def test_real_install_and_replay_are_private_under_permissive_umask(
    tmp_path: Path,
) -> None:
    approved = tmp_path / "approved"
    _fake_clone("https://github.com/example/approved", approved)
    runtime = tmp_path / "runtime"
    project_root = Path(__file__).resolve().parents[1]
    script = """
import sys
from pathlib import Path

import project_continuity.truth_setup as truth_setup
from project_continuity.config import Config, PrincipalConfig, ProjectConfig, RuntimePaths
from project_continuity.truth_setup import TruthSetupRequest, install_truth_plane

runtime = Path(sys.argv[1])
remote = sys.argv[2]
config = Config(
    paths=RuntimePaths(
        install_root=runtime / "install",
        data_root=runtime / "data",
        state_root=runtime / "state",
    ),
    projects=(ProjectConfig("alpha", remote),),
    principals=(
        PrincipalConfig("promoter", "promoter", (("alpha", "promoter"),)),
    ),
)
truth_setup._load_declaration = lambda *_args: TruthSetupRequest(
    "alpha", None, None
)
first = install_truth_plane(config, Path("/unused"))
second = install_truth_plane(config, Path("/unused"))
assert first["changed"] is True
assert second["changed"] is False
"""
    environment = dict(GIT_ENV)
    environment["PYTHONPATH"] = str(project_root / "src")

    subprocess.run(
        [sys.executable, "-c", script, str(runtime), str(approved)],
        cwd=project_root,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        umask=0o000,
    )

    checkout = runtime / "data/delivery/alpha"
    config_path = checkout / ".git/config"
    assert stat.S_IMODE(checkout.stat().st_mode) == 0o700
    assert stat.S_IMODE((checkout / ".git").stat().st_mode) == 0o700
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o600

    config_path.chmod(0o666)
    with pytest.raises(TruthSetupError, match="git_config_unsafe"):
        _verify_repo(
            checkout,
            str(approved),
            missing_ok=False,
            custody_root=runtime / "data",
        )


def test_github_git_transport_uses_only_the_private_token_file(
    tmp_path: Path, monkeypatch
) -> None:
    token = "github_token_for_test_1234567890"
    token_path = tmp_path / "github-token"
    token_path.write_text(token + "\n", encoding="ascii")
    token_path.chmod(0o600)
    monkeypatch.setenv("PROJECT_CONTINUITY_GITHUB_TOKEN_FILE", str(token_path))

    environment = _git_environment("https://github.com/example/private")

    assert environment["GIT_CONFIG_GLOBAL"] == os.devnull
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["HOME"] != os.environ.get("HOME")
    assert environment["XDG_CONFIG_HOME"] == environment["HOME"]
    assert environment["GIT_CONFIG_COUNT"] == "3"
    assert environment["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert environment["GIT_CONFIG_VALUE_0"] == os.devnull
    assert environment["GIT_CONFIG_KEY_1"] == "credential.helper"
    assert environment["GIT_CONFIG_KEY_2"] == "credential.useHttpPath"
    assert environment["GIT_CONFIG_VALUE_2"] == "true"
    assert token not in environment["GIT_CONFIG_VALUE_1"]
    exact = subprocess.run(
        ["git", "credential", "fill"],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        input="protocol=https\nhost=github.com\npath=example/private\n\n",
    )
    foreign_github = subprocess.run(
        ["git", "credential", "fill"],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        input="protocol=https\nhost=github.com\npath=example/other\n\n",
    )
    assert "username=x-access-token" in exact.stdout
    assert "password=" + token in exact.stdout
    assert foreign_github.returncode != 0
    assert token not in foreign_github.stdout + foreign_github.stderr
    foreign = _git_environment("https://example.com/example/public")
    assert foreign["GIT_CONFIG_COUNT"] == "1"
    assert foreign["GIT_CONFIG_KEY_0"] == "core.hooksPath"
    assert foreign["GIT_CONFIG_VALUE_0"] == os.devnull


def test_github_git_transport_rejects_unsafe_token_file(
    tmp_path: Path, monkeypatch
) -> None:
    token_path = tmp_path / "github-token"
    token_path.write_text("github_token_for_test_1234567890\n", encoding="ascii")
    token_path.chmod(0o644)
    monkeypatch.setenv("PROJECT_CONTINUITY_GITHUB_TOKEN_FILE", str(token_path))

    with pytest.raises(TruthSetupError, match="git_token_unsafe"):
        _git_environment("https://github.com/example/private")


def test_clone_ignores_callers_global_insteadof_config(
    tmp_path: Path, monkeypatch
) -> None:
    approved = tmp_path / "approved"
    attacker = tmp_path / "attacker"
    _fake_clone("https://github.com/example/approved", approved)
    _fake_clone("https://github.com/example/attacker", attacker)
    (approved / "README.md").write_text("approved bytes\n", encoding="utf-8")
    _git(approved, "add", "README.md")
    _git(approved, "commit", "-m", "approved bytes")
    (attacker / "README.md").write_text("attacker bytes\n", encoding="utf-8")
    _git(attacker, "add", "README.md")
    _git(attacker, "commit", "-m", "attacker bytes")
    caller_home = tmp_path / "caller-home"
    caller_home.mkdir()
    (caller_home / ".gitconfig").write_text(
        '[url "%s"]\n\tinsteadOf = %s\n'
        % (attacker.as_uri(), approved.as_uri()),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(caller_home))

    destination = tmp_path / "managed-clone"
    _clone_repo(approved.as_uri(), destination)

    assert (destination / "README.md").read_text(encoding="utf-8") == (
        "approved bytes\n"
    )


def test_managed_git_environment_cannot_read_global_transport_or_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    caller_home = tmp_path / "caller-home"
    caller_home.mkdir()
    helper_marker = tmp_path / "credential-helper-ran"
    helper = tmp_path / "credential-helper"
    helper.write_text(
        "#!/bin/sh\ntouch %s\necho username=attacker\necho password=attacker\n"
        % helper_marker,
        encoding="utf-8",
    )
    helper.chmod(0o700)
    (caller_home / ".gitconfig").write_text(
        """[url "file:///attacker.git"]
    insteadOf = https://github.com/example/approved
[credential]
    helper = %s
[http "https://github.com/"]
    extraHeader = Authorization: injected
[include]
    path = /tmp/foreign-git-config
"""
        % helper,
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(caller_home))

    completed = subprocess.run(
        ["git", "config", "--global", "--get-regexp", ".*"],
        env=_git_environment(),
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert completed.stdout == ""
    credential = subprocess.run(
        ["git", "credential", "fill"],
        env=_git_environment(),
        input="protocol=https\nhost=github.com\n\n",
        check=False,
        capture_output=True,
        text=True,
    )
    assert credential.returncode != 0
    assert not helper_marker.exists()


@pytest.mark.parametrize(
    "unsafe_config",
    [
        '[url "file:///attacker.git"]\n\tinsteadOf = https://github.com/example/approved\n',
        '[credential]\n\thelper = /tmp/credential-helper\n',
        '[http "https://github.com/"]\n\textraHeader = Authorization: injected\n',
        '[include]\n\tpath = /tmp/foreign-git-config\n',
        '[remote "origin"]\n\tpushurl = file:///attacker.git\n',
    ],
)
def test_existing_checkout_rejects_transport_overrides_before_git_runs(
    config, tmp_path: Path, monkeypatch, unsafe_config: str
) -> None:
    remote = "https://github.com/example/approved"
    root = config.paths.data_root / "delivery/alpha"
    _fake_clone(remote, root)
    root.chmod(0o700)
    config_path = root / ".git/config"
    config_path.write_text(
        config_path.read_text(encoding="utf-8") + unsafe_config,
        encoding="utf-8",
    )
    real_run = truth_setup.subprocess.run
    commands = []

    def record_run(*args, **kwargs):
        commands.append(args[0])
        return real_run(*args, **kwargs)

    monkeypatch.setattr("project_continuity.truth_setup.subprocess.run", record_run)

    with pytest.raises(TruthSetupError, match="git_config_unsafe"):
        _verify_repo(
            root,
            remote,
            missing_ok=False,
            custody_root=config.paths.data_root,
        )

    assert len(commands) == 1
    assert commands[0][:3] == ["git", "config", "--file"]
    assert "--no-includes" in commands[0]


@pytest.mark.parametrize("unsafe_key", ["fsmonitor", "hooksPath"])
def test_git_native_config_parser_rejects_deeper_indented_core_keys_before_repo_command(
    config, tmp_path: Path, monkeypatch, unsafe_key: str
) -> None:
    remote = "https://github.com/example/approved"
    root = config.paths.data_root / "delivery/alpha"
    _fake_clone(remote, root)
    root.chmod(0o700)
    marker = tmp_path / (unsafe_key + "-ran")
    executable = tmp_path / (unsafe_key + "-command")
    executable.write_text("#!/bin/sh\ntouch %s\n" % marker, encoding="utf-8")
    executable.chmod(0o700)
    path = root / ".git/config"
    original = path.read_text(encoding="utf-8")
    path.write_text(
        original.replace(
            "\trepositoryformatversion = 0\n",
            "\trepositoryformatversion = 0\n\t\t%s = %s\n"
            % (unsafe_key, executable),
            1,
        ),
        encoding="utf-8",
    )
    repo_commands = []
    real_run = truth_setup.subprocess.run

    def record_repo_run(*args, **kwargs):
        if Path(kwargs.get("cwd", "/")) == root:
            repo_commands.append(tuple(args[0]))
        return real_run(*args, **kwargs)

    monkeypatch.setattr("project_continuity.truth_setup.subprocess.run", record_repo_run)

    with pytest.raises(TruthSetupError, match="git_config_unsafe"):
        _verify_repo(
            root,
            remote,
            missing_ok=False,
            custody_root=config.paths.data_root,
        )

    assert repo_commands == []
    assert not marker.exists()


@pytest.mark.parametrize(
    "unsafe_config",
    [
        "[credential]\n\thelper\n",
        "[core]\n\tfsmonitor = first\n\t second-line\n",
    ],
)
def test_git_native_config_parser_rejects_no_value_and_multiline_entries(
    config, unsafe_config: str
) -> None:
    remote = "https://github.com/example/approved"
    root = config.paths.data_root / "delivery/alpha"
    _fake_clone(remote, root)
    root.chmod(0o700)
    path = root / ".git/config"
    path.write_text(
        path.read_text(encoding="utf-8") + unsafe_config,
        encoding="utf-8",
    )

    with pytest.raises(TruthSetupError, match="git_config_unsafe"):
        _verify_repo(
            root,
            remote,
            missing_ok=False,
            custody_root=config.paths.data_root,
        )


@pytest.mark.parametrize(
    ("include_openspec", "include_teamai", "installed_layers", "binding_keys"),
    [
        (False, False, ["delivery"], set()),
        (True, False, ["delivery", "openspec"], {"openspec"}),
        (False, True, ["delivery", "teamai"], {"teamai"}),
        (True, True, ["delivery", "openspec", "teamai"], {"openspec", "teamai"}),
    ],
)
def test_truth_setup_supports_every_optional_binding_combination(
    config,
    tmp_path: Path,
    monkeypatch,
    include_openspec: bool,
    include_teamai: bool,
    installed_layers,
    binding_keys,
) -> None:
    monkeypatch.setattr("project_continuity.truth_setup._clone_repo", _fake_clone)
    declaration = _declaration(
        tmp_path / "truth.json",
        include_openspec=include_openspec,
        include_teamai=include_teamai,
    )

    first = install_truth_plane(config, declaration)
    second = install_truth_plane(config, declaration)

    assert first["installed_layers"] == installed_layers
    assert first["changed"] is True
    assert second["changed"] is False
    binding_path = config.paths.data_root / "truth-plane/bindings.json"
    if binding_keys:
        value = json.loads(binding_path.read_text(encoding="utf-8"))
        assert set(value["projects"]["alpha"]) == binding_keys
    else:
        assert not binding_path.exists()


def test_truth_setup_rejects_existing_binding_drift(
    config, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("project_continuity.truth_setup._clone_repo", _fake_clone)
    install_truth_plane(config, _declaration(tmp_path / "truth.json"))

    with pytest.raises(TruthSetupError, match="binding_conflict"):
        install_truth_plane(
            config,
            _declaration(
                tmp_path / "changed.json",
                team_repo="https://github.com/example/different-team",
            ),
        )


def test_truth_setup_rolls_back_new_checkouts_when_clone_fails(
    config, tmp_path: Path, monkeypatch
) -> None:
    calls = []

    def fail_second(remote: str, destination: Path) -> None:
        calls.append(remote)
        if len(calls) == 2:
            raise TruthSetupError("injected_clone_failure")
        _fake_clone(remote, destination)

    monkeypatch.setattr("project_continuity.truth_setup._clone_repo", fail_second)

    with pytest.raises(TruthSetupError, match="injected_clone_failure"):
        install_truth_plane(config, _declaration(tmp_path / "truth.json"))

    assert not (config.paths.data_root / "delivery/alpha").exists()
    assert not (config.paths.data_root / "truth-plane/bindings.json").exists()


def test_truth_setup_rolls_back_binding_and_checkouts_when_readback_fails(
    config, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("project_continuity.truth_setup._clone_repo", _fake_clone)
    real_verify = truth_setup._verify_repo

    def fail_after_binding(root, remote, *, missing_ok, custody_root):
        binding = config.paths.data_root / "truth-plane/bindings.json"
        if binding.exists() and root == config.paths.data_root / "delivery/alpha":
            raise TruthSetupError("injected_readback_failure")
        return real_verify(
            root,
            remote,
            missing_ok=missing_ok,
            custody_root=custody_root,
        )

    monkeypatch.setattr("project_continuity.truth_setup._verify_repo", fail_after_binding)

    with pytest.raises(TruthSetupError, match="injected_readback_failure"):
        install_truth_plane(config, _declaration(tmp_path / "truth.json"))

    assert not (config.paths.data_root / "truth-plane/bindings.json").exists()
    assert not (config.paths.data_root / "delivery/alpha").exists()
    assert not (config.paths.data_root / "openspec/alpha").exists()
    assert not (config.paths.data_root / "team/alpha").exists()


def test_truth_setup_refuses_before_mutation_while_front_lifetime_lock_is_held(
    config, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("project_continuity.truth_setup._clone_repo", _fake_clone)
    declaration = _declaration(tmp_path / "truth.json")

    with runtime_lifetime_lock(config.paths.state_root):
        with pytest.raises(TruthSetupError, match="front_active"):
            install_truth_plane(config, declaration)

    assert not (config.paths.data_root / "delivery/alpha").exists()
    assert not (config.paths.data_root / "truth-plane/bindings.json").exists()


def test_truth_refresh_routes_only_selected_installed_layers(
    config, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("project_continuity.truth_setup._clone_repo", _fake_clone)
    install_truth_plane(config, _declaration(tmp_path / "truth.json"))
    refreshed = []

    def fake_target(root: Path, _remote: str) -> str:
        refreshed.append(root)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    monkeypatch.setattr(
        truth_setup, "_fetch_fast_forward_target", fake_target
    )
    monkeypatch.setattr(
        truth_setup,
        "_apply_fast_forward_target",
        lambda root, target_ref: _git_output(root, "rev-parse", target_ref),
    )
    receipt = refresh_truth_plane(config, "alpha", ("delivery", "teamai"))

    assert sorted(receipt["layers"]) == ["delivery", "teamai"]
    assert receipt["layers"]["delivery"]["changed"] is False
    assert receipt["restart_required"] is False
    assert refreshed == [
        config.paths.data_root / "delivery/alpha",
        config.paths.data_root / "team/alpha",
    ]
    for layer, relative in (
        ("delivery", "delivery/alpha"),
        ("teamai", "team/alpha"),
    ):
        root = config.paths.data_root / relative
        target = _git_output(root, "rev-parse", "HEAD")
        target_ref = truth_setup._refresh_target_ref(
            "alpha", {"delivery", "teamai"}, layer, target
        )
        assert truth_setup._read_refresh_target_pin(root, target_ref) is None


def test_truth_refresh_persists_partial_progress_and_replay_converges(
    config, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("project_continuity.truth_setup._clone_repo", _fake_clone)
    install_truth_plane(config, _declaration(tmp_path / "truth.json"))
    delivery = config.paths.data_root / "delivery/alpha"
    teamai = config.paths.data_root / "team/alpha"
    delivery_before, delivery_target = _detached_target(delivery, "delivery.txt")
    team_before, team_target = _detached_target(teamai, "team.txt")
    targets = {delivery: delivery_target, teamai: team_target}
    monkeypatch.setattr(
        truth_setup,
        "_fetch_fast_forward_target",
        lambda root, _remote: targets[root],
    )
    real_apply = truth_setup._apply_fast_forward_target

    def fail_teamai(root: Path, target: str) -> str:
        if root == teamai:
            raise TruthSetupError("injected_second_layer_failure")
        return real_apply(root, target)

    monkeypatch.setattr(truth_setup, "_apply_fast_forward_target", fail_teamai)
    with pytest.raises(TruthSetupError, match="truth_plane_refresh_partial") as caught:
        refresh_truth_plane(config, "alpha", ("delivery", "teamai"))

    failure = caught.value.receipt
    assert failure["operation_state"] == "partial"
    assert failure["failed_layer"] == "teamai"
    assert failure["cause"] == "injected_second_layer_failure"
    assert failure["layers"]["delivery"] == {
        "after": delivery_target,
        "before": delivery_before,
        "changed": True,
        "state": "complete",
        "target": delivery_target,
    }
    assert failure["layers"]["teamai"]["state"] == "failed"
    assert _git_output(delivery, "rev-parse", "HEAD") == delivery_target
    assert _git_output(teamai, "rev-parse", "HEAD") == team_before

    receipt_path = truth_setup._refresh_receipt_path(
        config, "alpha", {"delivery", "teamai"}
    )
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert persisted["operation_state"] == "partial"
    assert persisted["failed_layer"] == "teamai"
    assert receipt_path.stat().st_mode & 0o077 == 0
    for layer, root, target in (
        ("delivery", delivery, delivery_target),
        ("teamai", teamai, team_target),
    ):
        target_ref = truth_setup._refresh_target_ref(
            "alpha", {"delivery", "teamai"}, layer, target
        )
        assert truth_setup._read_refresh_target_pin(root, target_ref) == target

    (teamai / ".git/FETCH_HEAD").write_text(
        "%s\t\tbranch 'main' of expired.example\n" % team_before,
        encoding="utf-8",
    )
    _git(teamai, "reflog", "expire", "--expire=now", "--all")
    _git(teamai, "gc", "--prune=now")
    _git(teamai, "cat-file", "-e", "%s^{commit}" % team_target)

    monkeypatch.setattr(
        truth_setup,
        "_fetch_fast_forward_target",
        lambda *_args: pytest.fail("replay chased a newer remote target"),
    )
    monkeypatch.setattr(truth_setup, "_apply_fast_forward_target", real_apply)
    recovered = refresh_truth_plane(config, "alpha", ("delivery", "teamai"))

    assert recovered["operation_state"] == "complete"
    assert recovered["receipt_id"] == "truth-refresh:alpha:delivery+teamai"
    assert recovered["layers"]["delivery"]["changed"] is True
    assert recovered["layers"]["teamai"]["changed"] is True
    assert _git_output(delivery, "rev-parse", "HEAD") == delivery_target
    assert _git_output(teamai, "rev-parse", "HEAD") == team_target
    final = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert final["operation_state"] == "complete"
    assert all(item["state"] == "complete" for item in final["layers"].values())
    for layer, root, target in (
        ("delivery", delivery, delivery_target),
        ("teamai", teamai, team_target),
    ):
        target_ref = truth_setup._refresh_target_ref(
            "alpha", {"delivery", "teamai"}, layer, target
        )
        assert truth_setup._read_refresh_target_pin(root, target_ref) is None


@pytest.mark.parametrize(
    ("pin_state", "error"),
    [
        ("missing", "truth_plane_refresh_pin_missing"),
        ("drifted", "truth_plane_refresh_pin_conflict"),
    ],
)
def test_truth_refresh_pin_drift_fails_before_another_head_change(
    config,
    tmp_path: Path,
    monkeypatch,
    pin_state: str,
    error: str,
) -> None:
    monkeypatch.setattr("project_continuity.truth_setup._clone_repo", _fake_clone)
    install_truth_plane(config, _declaration(tmp_path / "truth.json"))
    delivery = config.paths.data_root / "delivery/alpha"
    teamai = config.paths.data_root / "team/alpha"
    delivery_before, delivery_target = _detached_target(delivery, "delivery.txt")
    team_before, team_target = _detached_target(teamai, "team.txt")
    targets = {delivery: delivery_target, teamai: team_target}
    monkeypatch.setattr(
        truth_setup,
        "_fetch_fast_forward_target",
        lambda root, _remote: targets[root],
    )
    real_apply = truth_setup._apply_fast_forward_target

    def fail_teamai(root: Path, target_ref: str) -> str:
        if root == teamai:
            raise TruthSetupError("injected_second_layer_failure")
        return real_apply(root, target_ref)

    monkeypatch.setattr(truth_setup, "_apply_fast_forward_target", fail_teamai)
    with pytest.raises(TruthSetupError, match="truth_plane_refresh_partial"):
        refresh_truth_plane(config, "alpha", ("delivery", "teamai"))

    team_ref = truth_setup._refresh_target_ref(
        "alpha", {"delivery", "teamai"}, "teamai", team_target
    )
    if pin_state == "missing":
        _git(teamai, "update-ref", "-d", team_ref, team_target)
    else:
        _git(teamai, "update-ref", team_ref, team_before, team_target)
    delivery_at_failure = _git_output(delivery, "rev-parse", "HEAD")
    team_at_failure = _git_output(teamai, "rev-parse", "HEAD")
    monkeypatch.setattr(
        truth_setup,
        "_fetch_fast_forward_target",
        lambda *_args: pytest.fail("pin conflict replay chased the remote"),
    )
    monkeypatch.setattr(
        truth_setup,
        "_apply_fast_forward_target",
        lambda *_args: pytest.fail("pin conflict changed another HEAD"),
    )

    with pytest.raises(TruthSetupError, match=error):
        refresh_truth_plane(config, "alpha", ("delivery", "teamai"))

    assert delivery_at_failure == delivery_target != delivery_before
    assert team_at_failure == team_before
    assert _git_output(delivery, "rev-parse", "HEAD") == delivery_at_failure
    assert _git_output(teamai, "rev-parse", "HEAD") == team_at_failure


def test_new_overlapping_refresh_is_refused_before_fetch_or_head_change(
    config, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("project_continuity.truth_setup._clone_repo", _fake_clone)
    install_truth_plane(config, _declaration(tmp_path / "truth.json"))
    delivery = config.paths.data_root / "delivery/alpha"
    teamai = config.paths.data_root / "team/alpha"
    delivery_before, delivery_target = _detached_target(
        delivery, "delivery.txt"
    )
    team_before = _git_output(teamai, "rev-parse", "HEAD")
    monkeypatch.setattr(
        truth_setup,
        "_fetch_fast_forward_target",
        lambda *_args: delivery_target,
    )
    real_apply = truth_setup._apply_fast_forward_target

    def fail_single_layer(*_args):
        raise TruthSetupError("injected_single_layer_failure")

    monkeypatch.setattr(
        truth_setup,
        "_apply_fast_forward_target",
        fail_single_layer,
    )

    with pytest.raises(TruthSetupError, match="truth_plane_refresh_partial"):
        refresh_truth_plane(config, "alpha", ("delivery",))

    single_ref = truth_setup._refresh_target_ref(
        "alpha", {"delivery"}, "delivery", delivery_target
    )
    assert (
        truth_setup._read_refresh_target_pin(delivery, single_ref)
        == delivery_target
    )

    _git(delivery, "reset", "--hard", delivery_target)
    (delivery / "second-target.txt").write_text("second target\n", encoding="utf-8")
    _git(delivery, "add", "second-target.txt")
    _git(delivery, "commit", "-m", "second reviewed target")
    second_target = _git_output(delivery, "rev-parse", "HEAD")
    _git(delivery, "reset", "--hard", delivery_before)
    combined_ref = truth_setup._refresh_target_ref(
        "alpha", {"delivery", "teamai"}, "delivery", second_target
    )
    monkeypatch.setattr(
        truth_setup,
        "_fetch_fast_forward_target",
        lambda *_args: pytest.fail("overlapping refresh reached fetch"),
    )
    monkeypatch.setattr(
        truth_setup,
        "_apply_fast_forward_target",
        lambda *_args: pytest.fail("overlapping refresh changed a HEAD"),
    )

    with pytest.raises(
        TruthSetupError, match="truth_plane_refresh_in_progress_conflict"
    ):
        refresh_truth_plane(config, "alpha", ("delivery", "teamai"))

    assert _git_output(delivery, "rev-parse", "HEAD") == delivery_before
    assert _git_output(teamai, "rev-parse", "HEAD") == team_before
    assert truth_setup._read_refresh_target_pin(delivery, combined_ref) is None
    assert (
        truth_setup._read_refresh_target_pin(delivery, single_ref)
        == delivery_target
    )

    openspec = config.paths.data_root / "openspec/alpha"
    openspec_target = _git_output(openspec, "rev-parse", "HEAD")
    monkeypatch.setattr(
        truth_setup,
        "_fetch_fast_forward_target",
        lambda *_args: openspec_target,
    )
    monkeypatch.setattr(truth_setup, "_apply_fast_forward_target", real_apply)
    disjoint = refresh_truth_plane(config, "alpha", ("openspec",))

    assert disjoint["operation_state"] == "complete"
    assert _git_output(delivery, "rev-parse", "HEAD") == delivery_before
    assert (
        truth_setup._read_refresh_target_pin(delivery, single_ref)
        == delivery_target
    )

    monkeypatch.setattr(
        truth_setup,
        "_fetch_fast_forward_target",
        lambda *_args: pytest.fail("partial replay chased the remote"),
    )
    recovered = refresh_truth_plane(config, "alpha", ("delivery",))

    assert recovered["operation_state"] == "complete"
    assert recovered["layers"]["delivery"]["after"] == delivery_target
    assert truth_setup._read_refresh_target_pin(delivery, single_ref) is None


def test_pin_readback_failure_cleans_the_created_ref_before_receipt(
    config, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("project_continuity.truth_setup._clone_repo", _fake_clone)
    install_truth_plane(config, _declaration(tmp_path / "truth.json"))
    delivery = config.paths.data_root / "delivery/alpha"
    before, target = _detached_target(delivery, "delivery.txt")
    selected = {"delivery"}
    target_ref = truth_setup._refresh_target_ref(
        "alpha", selected, "delivery", target
    )
    monkeypatch.setattr(
        truth_setup, "_fetch_fast_forward_target", lambda *_args: target
    )

    def fail_pin_readback(root, project_id, layers, layer, expected_target):
        assert (project_id, layers, layer, expected_target) == (
            "alpha",
            selected,
            "delivery",
            target,
        )
        assert truth_setup._read_refresh_target_pin(root, target_ref) == target
        raise TruthSetupError("injected_pin_readback_failure")

    monkeypatch.setattr(
        truth_setup, "_verify_refresh_target_pin", fail_pin_readback
    )

    with pytest.raises(TruthSetupError, match="injected_pin_readback_failure"):
        refresh_truth_plane(config, "alpha", ("delivery",))

    assert _git_output(delivery, "rev-parse", "HEAD") == before
    assert truth_setup._read_refresh_target_pin(delivery, target_ref) is None
    assert not truth_setup._refresh_receipt_path(
        config, "alpha", selected
    ).exists()


@pytest.mark.parametrize("failure_point", ["checkpoint", "directory_fsync"])
def test_truth_refresh_checkpoint_failure_keeps_a_valid_replayable_receipt(
    config, tmp_path: Path, monkeypatch, failure_point: str
) -> None:
    monkeypatch.setattr("project_continuity.truth_setup._clone_repo", _fake_clone)
    install_truth_plane(config, _declaration(tmp_path / "truth.json"))
    delivery = config.paths.data_root / "delivery/alpha"
    before, target = _detached_target(delivery, "delivery.txt")
    monkeypatch.setattr(
        truth_setup, "_fetch_fast_forward_target", lambda *_args: target
    )
    receipt_path = truth_setup._refresh_receipt_path(
        config, "alpha", {"delivery"}
    )
    real_write = truth_setup._write_refresh_receipt
    real_fsync = truth_setup._fsync_directory
    write_calls = 0
    write_states = []
    receipt_fsync_calls = 0

    def flaky_write(runtime_config, receipt):
        nonlocal write_calls
        write_calls += 1
        write_states.append(receipt["operation_state"])
        if failure_point == "checkpoint" and write_calls == 2:
            raise TruthSetupError("truth_plane_refresh_receipt_write_failed")
        return real_write(runtime_config, receipt)

    def flaky_fsync(path: Path) -> None:
        nonlocal receipt_fsync_calls
        if path == receipt_path.parent:
            receipt_fsync_calls += 1
            if failure_point == "directory_fsync" and receipt_fsync_calls == 2:
                raise OSError("injected post-replace fsync failure")
        real_fsync(path)

    monkeypatch.setattr(truth_setup, "_write_refresh_receipt", flaky_write)
    monkeypatch.setattr(truth_setup, "_fsync_directory", flaky_fsync)
    with pytest.raises(TruthSetupError, match="truth_plane_refresh_partial") as caught:
        refresh_truth_plane(config, "alpha", ("delivery",))

    failure = caught.value.receipt
    assert write_states[1] == "complete"
    assert failure["operation_state"] == "partial"
    assert failure["failed_layer"] == "delivery"
    assert failure["cause"] == "truth_plane_refresh_receipt_write_failed"
    assert failure["layers"]["delivery"] == {
        "after": target,
        "before": before,
        "changed": True,
        "state": "complete",
        "target": target,
    }
    persisted = json.loads(receipt_path.read_text(encoding="utf-8"))
    truth_setup._validate_refresh_receipt(
        persisted, "alpha", {"delivery"}
    )
    assert persisted["operation_state"] == "partial"

    monkeypatch.setattr(truth_setup, "_write_refresh_receipt", real_write)
    monkeypatch.setattr(truth_setup, "_fsync_directory", real_fsync)
    monkeypatch.setattr(
        truth_setup,
        "_fetch_fast_forward_target",
        lambda *_args: pytest.fail("checkpoint replay chased the remote"),
    )
    recovered = refresh_truth_plane(config, "alpha", ("delivery",))

    assert recovered["operation_state"] == "complete"
    assert recovered["layers"]["delivery"]["after"] == target
    assert _git_output(delivery, "rev-parse", "HEAD") == target


def test_truth_refresh_preflights_every_layer_before_first_head_change(
    config, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("project_continuity.truth_setup._clone_repo", _fake_clone)
    install_truth_plane(config, _declaration(tmp_path / "truth.json"))
    delivery = config.paths.data_root / "delivery/alpha"
    teamai = config.paths.data_root / "team/alpha"
    delivery_before, delivery_target = _detached_target(delivery, "delivery.txt")
    team_before = _git_output(teamai, "rev-parse", "HEAD")

    def fail_team_preflight(root: Path, _remote: str) -> str:
        if root == teamai:
            raise TruthSetupError("injected_preflight_failure")
        return delivery_target

    monkeypatch.setattr(
        truth_setup, "_fetch_fast_forward_target", fail_team_preflight
    )
    monkeypatch.setattr(
        truth_setup,
        "_apply_fast_forward_target",
        lambda *_args: pytest.fail("projection changed before all-layer preflight"),
    )

    with pytest.raises(TruthSetupError, match="injected_preflight_failure"):
        refresh_truth_plane(config, "alpha", ("delivery", "teamai"))

    assert _git_output(delivery, "rev-parse", "HEAD") == delivery_before
    assert _git_output(teamai, "rev-parse", "HEAD") == team_before
    assert not truth_setup._refresh_receipt_path(
        config, "alpha", {"delivery", "teamai"}
    ).exists()
    delivery_ref = truth_setup._refresh_target_ref(
        "alpha", {"delivery", "teamai"}, "delivery", delivery_target
    )
    assert truth_setup._read_refresh_target_pin(delivery, delivery_ref) is None


@pytest.mark.parametrize("replaced_receipt", [False, True])
def test_truth_refresh_receipt_prepare_failure_removes_target_pins(
    config, tmp_path: Path, monkeypatch, replaced_receipt: bool
) -> None:
    monkeypatch.setattr("project_continuity.truth_setup._clone_repo", _fake_clone)
    install_truth_plane(config, _declaration(tmp_path / "truth.json"))
    delivery = config.paths.data_root / "delivery/alpha"
    _before, target = _detached_target(delivery, "delivery.txt")
    monkeypatch.setattr(
        truth_setup, "_fetch_fast_forward_target", lambda *_args: target
    )
    real_write = truth_setup._write_refresh_receipt

    def fail_receipt_write(runtime_config, receipt):
        if replaced_receipt:
            real_write(runtime_config, receipt)
        raise TruthSetupError("truth_plane_refresh_receipt_write_failed")

    monkeypatch.setattr(truth_setup, "_write_refresh_receipt", fail_receipt_write)

    with pytest.raises(
        TruthSetupError, match="truth_plane_refresh_receipt_write_failed"
    ):
        refresh_truth_plane(config, "alpha", ("delivery",))

    target_ref = truth_setup._refresh_target_ref(
        "alpha", {"delivery"}, "delivery", target
    )
    assert truth_setup._read_refresh_target_pin(delivery, target_ref) is None
    assert not truth_setup._refresh_receipt_path(
        config, "alpha", {"delivery"}
    ).exists()


@pytest.mark.parametrize("broken", [False, True])
def test_truth_refresh_refuses_receipt_parent_symlink_before_fetch(
    config, tmp_path: Path, monkeypatch, broken: bool
) -> None:
    config.paths.state_root.mkdir(parents=True, mode=0o700)
    outside = tmp_path / "outside"
    if not broken:
        outside.mkdir()
    (config.paths.state_root / "truth-refresh").symlink_to(
        outside if not broken else tmp_path / "missing",
        target_is_directory=True,
    )
    monkeypatch.setattr(
        truth_setup,
        "_fetch_fast_forward_target",
        lambda *_args: pytest.fail("unsafe receipt path reached Git fetch"),
    )

    with pytest.raises(TruthSetupError, match="truth_plane_path_contains_symlink"):
        refresh_truth_plane(config, "alpha", ("delivery",))


@pytest.mark.parametrize("broken", [False, True])
def test_truth_refresh_refuses_receipt_file_symlink_before_fetch(
    config, tmp_path: Path, monkeypatch, broken: bool
) -> None:
    receipt = truth_setup._refresh_receipt_path(config, "alpha", {"delivery"})
    config.paths.state_root.mkdir(parents=True, mode=0o700)
    config.paths.state_root.chmod(0o700)
    receipt.parent.mkdir(mode=0o700)
    outside = tmp_path / "outside.json"
    if not broken:
        outside.write_text("{}\n", encoding="utf-8")
    receipt.symlink_to(outside if not broken else tmp_path / "missing.json")
    monkeypatch.setattr(
        truth_setup,
        "_fetch_fast_forward_target",
        lambda *_args: pytest.fail("unsafe receipt path reached Git fetch"),
    )

    with pytest.raises(TruthSetupError, match="truth_plane_path_contains_symlink"):
        refresh_truth_plane(config, "alpha", ("delivery",))


@pytest.mark.parametrize(
    ("payload", "mode", "error"),
    [
        ("{}\n", 0o600, "truth_plane_refresh_receipt_malformed"),
        ("{}\n", 0o644, "truth_plane_refresh_receipt_unsafe"),
    ],
)
def test_truth_refresh_refuses_malformed_or_public_receipt_before_fetch(
    config, monkeypatch, payload: str, mode: int, error: str
) -> None:
    receipt = truth_setup._refresh_receipt_path(config, "alpha", {"delivery"})
    config.paths.state_root.mkdir(parents=True, mode=0o700)
    config.paths.state_root.chmod(0o700)
    receipt.parent.mkdir(mode=0o700)
    receipt.write_text(payload, encoding="utf-8")
    receipt.chmod(mode)
    monkeypatch.setattr(
        truth_setup,
        "_fetch_fast_forward_target",
        lambda *_args: pytest.fail("unsafe receipt reached Git fetch"),
    )

    with pytest.raises(TruthSetupError, match=error):
        refresh_truth_plane(config, "alpha", ("delivery",))


def test_truth_refresh_refuses_semantically_inconsistent_receipt_before_fetch(
    config, monkeypatch
) -> None:
    receipt = truth_setup._refresh_receipt_path(config, "alpha", {"delivery"})
    config.paths.state_root.mkdir(parents=True, mode=0o700)
    config.paths.state_root.chmod(0o700)
    receipt.parent.mkdir(mode=0o700)
    receipt.write_text(
        json.dumps(
            {
                "cause": None,
                "failed_layer": None,
                "layers": {
                    "delivery": {
                        "after": None,
                        "before": "a" * 40,
                        "changed": None,
                        "state": "pending",
                        "target": "b" * 40,
                    }
                },
                "operation": "truth-refresh",
                "operation_state": "complete",
                "project_id": "alpha",
                "receipt_id": "truth-refresh:alpha:delivery",
                "schema_version": 1,
                "selected_layers": ["delivery"],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    receipt.chmod(0o600)
    monkeypatch.setattr(
        truth_setup,
        "_fetch_fast_forward_target",
        lambda *_args: pytest.fail("malformed receipt reached Git fetch"),
    )

    with pytest.raises(TruthSetupError, match="truth_plane_refresh_receipt_malformed"):
        refresh_truth_plane(config, "alpha", ("delivery",))


def test_truth_refresh_cli_returns_bounded_partial_receipt(
    config, tmp_path: Path, monkeypatch, capsys
) -> None:
    partial = {
        "cause": "injected_second_layer_failure",
        "failed_layer": "teamai",
        "layers": {
            "delivery": {
                "after": "b" * 40,
                "before": "a" * 40,
                "changed": True,
                "state": "complete",
                "target": "b" * 40,
            },
            "teamai": {
                "after": None,
                "before": "c" * 40,
                "changed": None,
                "state": "failed",
                "target": "d" * 40,
            },
        },
        "operation_state": "partial",
        "project_id": "alpha",
        "receipt_id": "truth-refresh:alpha:delivery+teamai",
        "restart_required": True,
        "selected_layers": ["delivery", "teamai"],
    }

    def fail_refresh(*_args, **_kwargs):
        raise TruthSetupError("truth_plane_refresh_partial", receipt=partial)

    monkeypatch.setattr(cli, "refresh_truth_plane", fail_refresh)
    monkeypatch.setattr(
        "project_continuity.server.load_private_service_config",
        lambda _path: config,
    )
    result = cli.run(
        [
            "--config",
            str(tmp_path / "config.toml"),
            "truth-refresh",
            "--project-id",
            "alpha",
            "--layer",
            "delivery",
            "--layer",
            "teamai",
        ]
    )

    assert result == 2
    output = json.loads(capsys.readouterr().err)
    assert output == {
        "error": "truth_plane_refresh_partial",
        "ok": False,
        "receipt": partial,
    }


def test_truth_refresh_refuses_while_front_lifetime_lock_is_held(
    config, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("project_continuity.truth_setup._clone_repo", _fake_clone)
    install_truth_plane(config, _declaration(tmp_path / "truth.json"))

    with runtime_lifetime_lock(config.paths.state_root):
        with pytest.raises(TruthSetupError, match="front_active"):
            refresh_truth_plane(config, "alpha", ("delivery",))


def test_fetch_fast_forward_tracks_reviewed_remote_head(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    author = tmp_path / "author"
    subprocess.run(
        ["git", "clone", str(origin), str(author)], check=True, capture_output=True
    )
    _git(author, "switch", "-c", "main")
    (author / "README.md").write_text("one\n", encoding="utf-8")
    _git(author, "add", "README.md")
    _git(author, "commit", "-m", "one")
    _git(author, "push", "-u", "origin", "main")
    consumer = tmp_path / "consumer"
    subprocess.run(
        ["git", "clone", "--branch", "main", str(origin), str(consumer)],
        check=True,
        capture_output=True,
    )
    (author / "README.md").write_text("two\n", encoding="utf-8")
    _git(author, "add", "README.md")
    _git(author, "commit", "-m", "two")
    _git(author, "push")

    after = _fetch_fast_forward(consumer, str(origin))

    assert (consumer / "README.md").read_text(encoding="utf-8") == "two\n"
    assert after == subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=consumer,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_truth_refresh_cli_reports_divergence_without_mutation(
    config, tmp_path: Path, monkeypatch, capsys
) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    author = tmp_path / "author"
    subprocess.run(
        ["git", "clone", str(origin), str(author)], check=True, capture_output=True
    )
    _git(author, "switch", "-c", "main")
    (author / "README.md").write_text("base\n", encoding="utf-8")
    _git(author, "add", "README.md")
    _git(author, "commit", "-m", "base")
    _git(author, "push", "-u", "origin", "main")

    consumer = config.paths.data_root / "delivery/alpha"
    consumer.parent.mkdir(parents=True, mode=0o700)
    subprocess.run(
        ["git", "clone", "--branch", "main", str(origin), str(consumer)],
        check=True,
        capture_output=True,
    )
    (consumer / "local.txt").write_text("local\n", encoding="utf-8")
    _git(consumer, "add", "local.txt")
    _git(consumer, "commit", "-m", "local")
    before = _git_output(consumer, "rev-parse", "HEAD")

    (author / "remote.txt").write_text("remote\n", encoding="utf-8")
    _git(author, "add", "remote.txt")
    _git(author, "commit", "-m", "remote")
    _git(author, "push")

    local_config = Config(
        paths=config.paths,
        projects=(ProjectConfig("alpha", str(origin)),),
        principals=config.principals,
    )
    monkeypatch.setattr(
        "project_continuity.server.load_private_service_config",
        lambda _path: local_config,
    )

    result = cli.run(
        [
            "--config",
            str(tmp_path / "config.toml"),
            "truth-refresh",
            "--project-id",
            "alpha",
            "--layer",
            "delivery",
        ]
    )

    assert result == 2
    assert json.loads(capsys.readouterr().err) == {
        "error": "truth_plane_checkout_diverged",
        "ok": False,
    }
    assert _git_output(consumer, "rev-parse", "HEAD") == before
    assert not truth_setup._refresh_receipt_path(
        local_config, "alpha", {"delivery"}
    ).exists()
    assert not _git_output(
        consumer, "for-each-ref", "--format=%(refname)", "refs/project-continuity"
    )
