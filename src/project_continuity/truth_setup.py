"""Operator entrypoint for installing the donor-owned truth-plane checkouts."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .config import Config, ConfigError, _identifier, _repository_url
from .managed_git import (
    ManagedGitConfig,
    ManagedGitError,
    inspect_managed_git_config,
    managed_git_environment,
)
from .runtime_lock import RuntimeLockError, runtime_lifetime_lock
from .truth_bindings import (
    BINDINGS_RELATIVE_PATH,
    OpenSpecBinding,
    ProjectTruthBinding,
    TeamAIBinding,
    TruthBindingError,
    TruthBindings,
    load_truth_bindings,
)


SCHEMA_VERSION = 1
REFRESH_RECEIPT_SCHEMA_VERSION = 1
MAX_OUTPUT = 256_000
ZERO_GIT_OBJECT = "0" * 40


class TruthSetupError(RuntimeError):
    """A managed truth-plane checkout or binding could not be installed safely."""

    def __init__(self, code: str, *, receipt: Optional[Dict[str, Any]] = None):
        super().__init__(code)
        self.receipt = receipt


@dataclass(frozen=True)
class TruthSetupRequest:
    project_id: str
    openspec: Optional[OpenSpecBinding]
    teamai: Optional[TeamAIBinding]


def install_truth_plane(config: Config, declaration_path: Path) -> Dict[str, Any]:
    """Install one project's managed checkouts and private routing projection.

    The command never starts or restarts the front.  It is safe to replay with
    the exact declaration; changed bindings or remotes fail closed.
    """

    request = _load_declaration(config, declaration_path)
    project = config.project(request.project_id)
    roots = {
        "delivery": config.paths.data_root / "delivery" / request.project_id,
    }
    remotes = {"delivery": project.repo_url}
    if request.openspec is not None:
        roots["openspec"] = (
            config.paths.data_root / "openspec" / request.project_id
        )
        remotes["openspec"] = request.openspec.repo_url
    if request.teamai is not None:
        roots["teamai"] = config.paths.data_root / "team" / request.project_id
        remotes["teamai"] = request.teamai.repo_url

    changed = False
    installed: list[Path] = []
    with _operation_lock(config):
        existing = load_truth_bindings(config)
        _assert_binding_compatible(existing, request)
        binding_before = _binding_preimage(config)
        try:
            for name, root in roots.items():
                if _verify_repo(
                    root,
                    remotes[name],
                    missing_ok=True,
                    custody_root=config.paths.data_root,
                ):
                    continue
                _install_repo(root, remotes[name])
                installed.append(root)
                changed = True
            projects = [
                existing.project(project_id)
                for project_id in existing.project_ids()
                if project_id != request.project_id
            ]
            if request.openspec is not None or request.teamai is not None:
                projects.append(
                    ProjectTruthBinding(
                        project_id=request.project_id,
                        openspec=request.openspec,
                        teamai=request.teamai,
                    )
                )
            desired = TruthBindings(projects)
            if desired.as_dict() != existing.as_dict():
                _write_bindings(config, desired)
                changed = True
            for name, root in roots.items():
                _verify_repo(
                    root,
                    remotes[name],
                    missing_ok=False,
                    custody_root=config.paths.data_root,
                )
            readback = load_truth_bindings(config).project(request.project_id)
            if readback != ProjectTruthBinding(
                request.project_id, request.openspec, request.teamai
            ):
                raise TruthSetupError("truth_plane_binding_readback_failed")
        except Exception:
            try:
                _restore_binding_preimage(config, binding_before)
                for root in reversed(installed):
                    _remove_new_repo(root)
            except Exception as rollback_exc:
                raise TruthSetupError("truth_plane_setup_rollback_failed") from rollback_exc
            raise

    return {
        "changed": changed,
        "installed_layers": sorted(roots),
        "ok": True,
        "project_id": request.project_id,
        "restart_required": changed,
    }


def refresh_truth_plane(
    config: Config, project_id: str, layers: Sequence[str]
) -> Dict[str, Any]:
    """Fast-forward selected managed Git projections after their owner merged."""

    project = config.project(project_id)
    selected = _refresh_layers(layers)
    bindings = load_truth_bindings(config).project(project_id)
    roots = {"delivery": config.paths.data_root / "delivery" / project_id}
    remotes = {"delivery": project.repo_url}
    if bindings.openspec is not None:
        roots["openspec"] = config.paths.data_root / "openspec" / project_id
        remotes["openspec"] = bindings.openspec.repo_url
    if bindings.teamai is not None:
        roots["teamai"] = config.paths.data_root / "team" / project_id
        remotes["teamai"] = bindings.teamai.repo_url
    missing = selected - set(roots)
    if missing:
        raise TruthSetupError(
            "truth_plane_refresh_binding_absent:%s" % ",".join(sorted(missing))
        )

    with _operation_lock(config):
        receipt = _load_refresh_receipt(config, project_id, selected)
        if receipt is None or receipt["operation_state"] == "complete":
            _assert_no_overlapping_refresh_receipt(
                config, project_id, selected
            )
            receipt = _prepare_refresh_receipt(
                config, project_id, selected, roots, remotes
            )
        else:
            _resume_refresh_receipt(config, receipt, roots, remotes)

        for layer in sorted(selected):
            entry = receipt["layers"][layer]
            if entry["state"] == "complete":
                continue
            root = roots[layer]
            try:
                target_ref = _verify_refresh_target_pin(
                    root, project_id, selected, layer, entry["target"]
                )
                after = _apply_fast_forward_target(root, target_ref)
                _verify_repo(
                    root,
                    remotes[layer],
                    missing_ok=False,
                    custody_root=config.paths.data_root,
                )
                if after != entry["target"]:
                    raise TruthSetupError("truth_plane_refresh_readback_failed")
            except Exception as exc:
                entry["state"] = "failed"
                receipt["operation_state"] = "partial"
                receipt["failed_layer"] = layer
                receipt["cause"] = (
                    str(exc)
                    if isinstance(exc, TruthSetupError)
                    else "truth_plane_refresh_failed"
                )
                public = _public_refresh_receipt(receipt)
                try:
                    _write_refresh_receipt(config, receipt)
                except TruthSetupError:
                    pass
                raise TruthSetupError(
                    "truth_plane_refresh_partial", receipt=public
                ) from exc
            entry.update(
                {
                    "after": after,
                    "changed": after != entry["before"],
                    "state": "complete",
                }
            )
            _checkpoint_refresh_receipt(config, receipt, layer)

        if receipt["operation_state"] != "complete":
            _checkpoint_refresh_receipt(
                config, receipt, sorted(selected)[-1]
            )
        _release_refresh_target_pins(project_id, roots, receipt)
        receipts = {
            layer: {
                "after": receipt["layers"][layer]["after"],
                "before": receipt["layers"][layer]["before"],
                "changed": receipt["layers"][layer]["changed"],
            }
            for layer in sorted(selected)
        }
    return {
        "layers": receipts,
        "ok": True,
        "operation_state": "complete",
        "project_id": project_id,
        "receipt_id": receipt["receipt_id"],
        "restart_required": any(
            item["changed"] for item in receipts.values()
        ),
    }


def _load_declaration(config: Config, path: Path) -> TruthSetupRequest:
    declaration = Path(path)
    if (
        not declaration.is_absolute()
        or declaration.is_symlink()
        or not declaration.is_file()
    ):
        raise TruthSetupError("truth_setup_declaration_unavailable")
    try:
        if declaration.resolve(strict=True) != declaration:
            raise TruthSetupError("truth_setup_declaration_unsafe")
    except OSError as exc:
        raise TruthSetupError("truth_setup_declaration_unavailable") from exc
    stat = declaration.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o022:
        raise TruthSetupError("truth_setup_declaration_unsafe")
    try:
        raw = json.loads(
            declaration.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TruthSetupError("truth_setup_declaration_malformed") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "openspec",
        "project_id",
        "schema_version",
        "teamai",
    }:
        raise TruthSetupError("truth_setup_declaration_malformed")
    if raw["schema_version"] != SCHEMA_VERSION:
        raise TruthSetupError("truth_setup_schema_unsupported")
    try:
        project_id = _identifier(raw["project_id"], "truth project id")
        config.project(project_id)
        openspec = _openspec_binding(raw["openspec"])
        teamai = _teamai_binding(raw["teamai"])
    except (ConfigError, ValueError) as exc:
        raise TruthSetupError(str(exc)) from exc
    return TruthSetupRequest(project_id, openspec, teamai)


def _openspec_binding(value: Any) -> Optional[OpenSpecBinding]:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"repo_url", "store_id"}:
        raise TruthSetupError("truth_setup_openspec_malformed")
    return OpenSpecBinding(
        _identifier(value["store_id"], "OpenSpec store_id"),
        _repository_url(value["repo_url"], "OpenSpec repo_url"),
    )


def _teamai_binding(value: Any) -> Optional[TeamAIBinding]:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "repo_url",
        "reviewers",
        "team_id",
    }:
        raise TruthSetupError("truth_setup_teamai_malformed")
    reviewers = value["reviewers"]
    if not isinstance(reviewers, list):
        raise TruthSetupError("truth_setup_teamai_reviewers_malformed")
    parsed = tuple(_identifier(item, "TeamAI reviewer") for item in reviewers)
    if not parsed or len(parsed) != len(set(parsed)):
        raise TruthSetupError("truth_setup_teamai_reviewers_malformed")
    return TeamAIBinding(
        _identifier(value["team_id"], "TeamAI team_id"),
        _repository_url(value["repo_url"], "TeamAI repo_url"),
        parsed,
    )


def _assert_binding_compatible(
    existing: TruthBindings, request: TruthSetupRequest
) -> None:
    if request.project_id not in existing.project_ids():
        return
    current = existing.project(request.project_id)
    desired = ProjectTruthBinding(
        request.project_id, request.openspec, request.teamai
    )
    if current != desired:
        raise TruthSetupError("truth_plane_binding_conflict")


def _install_repo(root: Path, remote: str) -> None:
    parent = root.parent
    _private_directory(parent, root.parents[1])
    if os.path.lexists(str(root)):
        raise TruthSetupError("truth_plane_checkout_conflict")
    with tempfile.TemporaryDirectory(prefix=".truth-clone-", dir=str(parent)) as name:
        staging = Path(name) / "repo"
        _clone_repo(remote, staging)
        _verify_repo(
            staging,
            remote,
            missing_ok=False,
            custody_root=root.parents[1],
        )
        try:
            os.replace(staging, root)
            _fsync_directory(parent)
        except OSError as exc:
            raise TruthSetupError("truth_plane_checkout_install_failed") from exc


def _clone_repo(remote: str, destination: Path) -> None:
    environment = _git_environment(remote)
    try:
        completed = subprocess.run(
            ["git", "clone", "--origin", "origin", remote, str(destination)],
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
            umask=0o077,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TruthSetupError("truth_plane_clone_failed") from exc
    if (
        completed.returncode != 0
        or len(completed.stdout) > MAX_OUTPUT
        or len(completed.stderr) > MAX_OUTPUT
    ):
        raise TruthSetupError("truth_plane_clone_failed")
    try:
        destination.chmod(0o700)
    except OSError as exc:
        raise TruthSetupError("truth_plane_checkout_unsafe") from exc


def _fetch_fast_forward(root: Path, expected_remote: str) -> str:
    target = _fetch_fast_forward_target(root, expected_remote)
    return _apply_fast_forward_target(root, target)


def _fetch_fast_forward_target(root: Path, expected_remote: str) -> str:
    config = _managed_git_config(root, expected_remote)
    branch = _git(root, "symbolic-ref", "--short", "HEAD")
    upstream = config.branch(branch)
    expected_merge = "refs/heads/%s" % branch
    if upstream != ("origin", expected_merge):
        raise TruthSetupError("truth_plane_checkout_upstream_unsupported")
    _git_write(
        root,
        "fetch",
        "--prune",
        expected_remote,
        branch,
        remote=expected_remote,
    )
    target = _git(root, "rev-parse", "FETCH_HEAD")
    before = _git(root, "rev-parse", "HEAD")
    try:
        _git(root, "merge-base", "--is-ancestor", before, target)
    except TruthSetupError as exc:
        raise TruthSetupError("truth_plane_checkout_diverged") from exc
    return target


def _apply_fast_forward_target(root: Path, target: str) -> str:
    _git_write(root, "merge", "--ff-only", target)
    return _git(root, "rev-parse", "HEAD")


def _prepare_refresh_receipt(
    config: Config,
    project_id: str,
    selected: set[str],
    roots: Mapping[str, Path],
    remotes: Mapping[str, str],
) -> Dict[str, Any]:
    entries: Dict[str, Any] = {}
    pins: list[Tuple[Path, str, str]] = []
    receipt: Optional[Dict[str, Any]] = None
    try:
        for layer in sorted(selected):
            root = roots[layer]
            _verify_repo(
                root,
                remotes[layer],
                missing_ok=False,
                custody_root=config.paths.data_root,
            )
            before = _git(root, "rev-parse", "HEAD")
            target = _fetch_fast_forward_target(root, remotes[layer])
            _verify_repo(
                root,
                remotes[layer],
                missing_ok=False,
                custody_root=config.paths.data_root,
            )
            target_ref = _pin_refresh_target(
                root, project_id, selected, layer, target
            )
            pins.append((root, target_ref, target))
            entries[layer] = {
                "after": None,
                "before": before,
                "changed": None,
                "state": "pending",
                "target": target,
            }
        receipt = {
            "cause": None,
            "failed_layer": None,
            "layers": entries,
            "operation": "truth-refresh",
            "operation_state": "prepared",
            "project_id": project_id,
            "receipt_id": _refresh_receipt_id(project_id, selected),
            "schema_version": REFRESH_RECEIPT_SCHEMA_VERSION,
            "selected_layers": sorted(selected),
        }
        _write_refresh_receipt(config, receipt)
    except Exception:
        try:
            if receipt is not None:
                _discard_prepared_refresh_receipt(config, receipt)
            for root, target_ref, target in reversed(pins):
                _remove_refresh_target_pin(root, target_ref, target)
        except TruthSetupError as cleanup_exc:
            raise TruthSetupError(
                "truth_plane_refresh_pin_cleanup_failed"
            ) from cleanup_exc
        raise
    return receipt


def _resume_refresh_receipt(
    config: Config,
    receipt: Dict[str, Any],
    roots: Mapping[str, Path],
    remotes: Mapping[str, str],
) -> None:
    for layer in receipt["selected_layers"]:
        root = roots[layer]
        entry = receipt["layers"][layer]
        _verify_repo(
            root,
            remotes[layer],
            missing_ok=False,
            custody_root=config.paths.data_root,
        )
        _verify_refresh_target_pin(
            root,
            receipt["project_id"],
            set(receipt["selected_layers"]),
            layer,
            entry["target"],
        )
        current = _git(root, "rev-parse", "HEAD")
        if current == entry["target"]:
            entry.update(
                {
                    "after": current,
                    "changed": current != entry["before"],
                    "state": "complete",
                }
            )
        elif current != entry["before"] or entry["state"] == "complete":
            raise TruthSetupError("truth_plane_refresh_receipt_conflict")
        else:
            entry.update({"after": None, "changed": None, "state": "pending"})
    receipt["operation_state"] = "prepared"
    receipt["failed_layer"] = None
    receipt["cause"] = None


def _checkpoint_refresh_receipt(
    config: Config, receipt: Dict[str, Any], layer: str
) -> None:
    receipt["operation_state"] = (
        "complete"
        if all(
            entry["state"] == "complete"
            for entry in receipt["layers"].values()
        )
        else "prepared"
    )
    receipt["failed_layer"] = None
    receipt["cause"] = None
    try:
        _write_refresh_receipt(config, receipt)
    except TruthSetupError as exc:
        receipt["operation_state"] = "partial"
        receipt["failed_layer"] = layer
        receipt["cause"] = str(exc)
        public = _public_refresh_receipt(receipt)
        try:
            _write_refresh_receipt(config, receipt)
        except TruthSetupError:
            pass
        raise TruthSetupError(
            "truth_plane_refresh_partial", receipt=public
        ) from exc


def _refresh_receipt_id(project_id: str, selected: set[str]) -> str:
    return "truth-refresh:%s:%s" % (project_id, "+".join(sorted(selected)))


def _assert_no_overlapping_refresh_receipt(
    config: Config, project_id: str, selected: set[str]
) -> None:
    layers = ("delivery", "openspec", "teamai")
    for mask in range(1, 1 << len(layers)):
        candidate = {
            layer for index, layer in enumerate(layers) if mask & (1 << index)
        }
        if candidate == selected or not candidate & selected:
            continue
        receipt = _load_refresh_receipt(config, project_id, candidate)
        if receipt is not None and receipt["operation_state"] != "complete":
            raise TruthSetupError("truth_plane_refresh_in_progress_conflict")


def _refresh_target_ref(
    project_id: str, selected: set[str], layer: str, target: str
) -> str:
    return "refs/project-continuity/truth-refresh/%s/%s/%s/%s" % (
        project_id,
        "+".join(sorted(selected)),
        layer,
        target,
    )


def _pin_refresh_target(
    root: Path,
    project_id: str,
    selected: set[str],
    layer: str,
    target: str,
) -> str:
    target_ref = _refresh_target_ref(project_id, selected, layer, target)
    current = _read_refresh_target_pin(root, target_ref)
    try:
        if current is None:
            _git_write(
                root,
                "update-ref",
                target_ref,
                target,
                ZERO_GIT_OBJECT,
            )
        return _verify_refresh_target_pin(
            root, project_id, selected, layer, target
        )
    except TruthSetupError as exc:
        try:
            if _read_refresh_target_pin(root, target_ref) == target:
                _remove_refresh_target_pin(root, target_ref, target)
        except TruthSetupError as cleanup_exc:
            raise TruthSetupError(
                "truth_plane_refresh_pin_cleanup_failed"
            ) from cleanup_exc
        raise


def _verify_refresh_target_pin(
    root: Path,
    project_id: str,
    selected: set[str],
    layer: str,
    target: str,
) -> str:
    target_ref = _refresh_target_ref(project_id, selected, layer, target)
    current = _read_refresh_target_pin(root, target_ref)
    if current is None:
        raise TruthSetupError("truth_plane_refresh_pin_missing")
    if current != target:
        raise TruthSetupError("truth_plane_refresh_pin_conflict")
    try:
        _git(root, "cat-file", "-e", "%s^{commit}" % target_ref)
    except TruthSetupError as exc:
        raise TruthSetupError("truth_plane_refresh_pin_conflict") from exc
    return target_ref


def _remove_refresh_target_pin(
    root: Path, target_ref: str, target: str
) -> None:
    current = _read_refresh_target_pin(root, target_ref)
    if current is None:
        return
    if current != target:
        raise TruthSetupError("truth_plane_refresh_pin_conflict")
    try:
        _git_write(root, "update-ref", "-d", target_ref, target)
    except TruthSetupError as exc:
        raise TruthSetupError("truth_plane_refresh_pin_cleanup_failed") from exc
    if _read_refresh_target_pin(root, target_ref) is not None:
        raise TruthSetupError("truth_plane_refresh_pin_cleanup_failed")


def _release_refresh_target_pins(
    project_id: str,
    roots: Mapping[str, Path],
    receipt: Mapping[str, Any],
) -> None:
    """Best-effort cleanup after the complete receipt is already durable."""

    for layer in receipt["selected_layers"]:
        target = receipt["layers"][layer]["target"]
        target_ref = _refresh_target_ref(
            project_id, set(receipt["selected_layers"]), layer, target
        )
        try:
            _remove_refresh_target_pin(roots[layer], target_ref, target)
        except TruthSetupError:
            # A retained exact pin is safe; a completed refresh must not be
            # reported as failed after its canonical receipt is durable.
            continue


def _refresh_receipt_path(
    config: Config, project_id: str, selected: set[str]
) -> Path:
    name = "%s--%s.json" % (project_id, "+".join(sorted(selected)))
    return config.paths.state_root / "truth-refresh" / name


def _load_refresh_receipt(
    config: Config, project_id: str, selected: set[str]
) -> Optional[Dict[str, Any]]:
    target = _refresh_receipt_path(config, project_id, selected)
    _assert_inside_custody(target, config.paths.state_root)
    if not os.path.lexists(str(target)):
        return None
    if target.is_symlink() or not target.is_file():
        raise TruthSetupError("truth_plane_refresh_receipt_unsafe")
    stat = target.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
        raise TruthSetupError("truth_plane_refresh_receipt_unsafe")
    try:
        receipt = json.loads(
            target.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TruthSetupError("truth_plane_refresh_receipt_malformed") from exc
    _validate_refresh_receipt(receipt, project_id, selected)
    return receipt


def _validate_refresh_receipt(
    receipt: Any, project_id: str, selected: set[str]
) -> None:
    expected = {
        "cause",
        "failed_layer",
        "layers",
        "operation",
        "operation_state",
        "project_id",
        "receipt_id",
        "schema_version",
        "selected_layers",
    }
    selected_layers = sorted(selected)
    if (
        not isinstance(receipt, dict)
        or set(receipt) != expected
        or receipt["schema_version"] != REFRESH_RECEIPT_SCHEMA_VERSION
        or receipt["operation"] != "truth-refresh"
        or receipt["project_id"] != project_id
        or receipt["selected_layers"] != selected_layers
        or receipt["receipt_id"] != _refresh_receipt_id(project_id, selected)
        or receipt["operation_state"] not in {"prepared", "partial", "complete"}
        or receipt["failed_layer"] not in {None, *selected}
        or not (receipt["cause"] is None or isinstance(receipt["cause"], str))
        or not isinstance(receipt["layers"], dict)
        or set(receipt["layers"]) != selected
    ):
        raise TruthSetupError("truth_plane_refresh_receipt_malformed")
    for entry in receipt["layers"].values():
        if (
            not isinstance(entry, dict)
            or set(entry) != {"after", "before", "changed", "state", "target"}
            or entry["state"] not in {"pending", "failed", "complete"}
            or not _is_commit_identity(entry["before"])
            or not _is_commit_identity(entry["target"])
            or not (entry["after"] is None or _is_commit_identity(entry["after"]))
            or not (
                entry["changed"] is None
                or isinstance(entry["changed"], bool)
            )
        ):
            raise TruthSetupError("truth_plane_refresh_receipt_malformed")
        if entry["state"] == "complete" and (
            entry["after"] != entry["target"]
            or entry["changed"] != (entry["after"] != entry["before"])
        ):
            raise TruthSetupError("truth_plane_refresh_receipt_malformed")
        if entry["state"] != "complete" and (
            entry["after"] is not None or entry["changed"] is not None
        ):
            raise TruthSetupError("truth_plane_refresh_receipt_malformed")
    if receipt["operation_state"] == "complete" and (
        receipt["failed_layer"] is not None
        or receipt["cause"] is not None
        or any(
            entry["state"] != "complete"
            for entry in receipt["layers"].values()
        )
    ):
        raise TruthSetupError("truth_plane_refresh_receipt_malformed")
    if receipt["operation_state"] == "prepared" and (
        receipt["failed_layer"] is not None
        or receipt["cause"] is not None
        or any(entry["state"] == "failed" for entry in receipt["layers"].values())
    ):
        raise TruthSetupError("truth_plane_refresh_receipt_malformed")
    if receipt["operation_state"] == "partial" and (
        receipt["failed_layer"] is None
        or not receipt["cause"]
        or receipt["layers"][receipt["failed_layer"]]["state"]
        not in {"failed", "complete"}
    ):
        raise TruthSetupError("truth_plane_refresh_receipt_malformed")


def _is_commit_identity(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(character in "0123456789abcdef" for character in value)
    )


def _write_refresh_receipt(config: Config, receipt: Dict[str, Any]) -> None:
    selected = set(receipt["selected_layers"])
    _validate_refresh_receipt(receipt, receipt["project_id"], selected)
    target = _refresh_receipt_path(config, receipt["project_id"], selected)
    parent_existed = target.parent.exists()
    _private_directory(target.parent, config.paths.state_root)
    if not parent_existed:
        _fsync_directory(config.paths.state_root)
    if os.path.lexists(str(target)) and (
        target.is_symlink()
        or not target.is_file()
        or target.stat().st_uid != os.getuid()
        or target.stat().st_mode & 0o077
    ):
        raise TruthSetupError("truth_plane_refresh_receipt_unsafe")
    temporary: Optional[Path] = None
    payload = _refresh_receipt_payload(receipt)
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            dir=target.parent,
            prefix=".%s." % target.name,
            suffix=".tmp",
        )
        temporary = Path(temporary_name)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except OSError as exc:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        raise TruthSetupError("truth_plane_refresh_receipt_write_failed") from exc


def _discard_prepared_refresh_receipt(
    config: Config, receipt: Mapping[str, Any]
) -> None:
    target = _refresh_receipt_path(
        config, receipt["project_id"], set(receipt["selected_layers"])
    )
    if not os.path.lexists(str(target)):
        return
    try:
        if (
            target.is_symlink()
            or not target.is_file()
            or target.stat().st_uid != os.getuid()
            or target.stat().st_mode & 0o077
        ):
            raise TruthSetupError("truth_plane_refresh_receipt_unsafe")
        if target.read_bytes() != _refresh_receipt_payload(receipt):
            return
        target.unlink()
        _fsync_directory(target.parent)
    except OSError as exc:
        raise TruthSetupError("truth_plane_refresh_pin_cleanup_failed") from exc


def _refresh_receipt_payload(receipt: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(receipt, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")


def _public_refresh_receipt(receipt: Mapping[str, Any]) -> Dict[str, Any]:
    layers = {
        layer: dict(receipt["layers"][layer])
        for layer in receipt["selected_layers"]
    }
    return {
        "cause": receipt["cause"],
        "failed_layer": receipt["failed_layer"],
        "layers": layers,
        "operation_state": receipt["operation_state"],
        "project_id": receipt["project_id"],
        "receipt_id": receipt["receipt_id"],
        "restart_required": any(
            entry["changed"] is True for entry in layers.values()
        ),
        "selected_layers": list(receipt["selected_layers"]),
    }


def _refresh_layers(values: Sequence[str]) -> set[str]:
    if isinstance(values, (str, bytes)) or not values:
        raise TruthSetupError("truth_plane_refresh_layers_malformed")
    allowed = {"delivery", "openspec", "teamai"}
    selected = set(values)
    if not selected <= allowed:
        raise TruthSetupError("truth_plane_refresh_layers_malformed")
    return selected


def _verify_repo(
    root: Path, remote: str, *, missing_ok: bool, custody_root: Path
) -> bool:
    _assert_inside_custody(root, custody_root)
    if not os.path.lexists(str(root)):
        if missing_ok:
            return False
        raise TruthSetupError("truth_plane_checkout_unavailable")
    if (
        root.is_symlink()
        or not root.is_dir()
        or (root / ".git").is_symlink()
        or not (root / ".git").is_dir()
    ):
        raise TruthSetupError("truth_plane_checkout_unsafe")
    if root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o022:
        raise TruthSetupError("truth_plane_checkout_unsafe")
    _verify_local_git_config(root, remote)
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise TruthSetupError("truth_plane_checkout_dirty")
    return True


def _verify_local_git_config(root: Path, remote: str) -> None:
    """Use the shared Git-native parser before any repo-scoped Git command."""

    _managed_git_config(root, remote)


def _managed_git_config(root: Path, remote: str) -> ManagedGitConfig:
    try:
        return inspect_managed_git_config(root, remote)
    except ManagedGitError as exc:
        if exc.code == "managed_git_remote_conflict":
            raise TruthSetupError("truth_plane_checkout_remote_conflict") from exc
        raise TruthSetupError("truth_plane_git_config_unsafe") from exc


def _assert_inside_custody(path: Path, custody_root: Path) -> None:
    try:
        relative = path.relative_to(custody_root)
    except ValueError as exc:
        raise TruthSetupError("truth_plane_path_escapes_custody") from exc
    current = custody_root
    if current.is_symlink():
        raise TruthSetupError("truth_plane_path_contains_symlink")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise TruthSetupError("truth_plane_path_contains_symlink")


def _write_bindings(config: Config, bindings: TruthBindings) -> None:
    payload = (
        json.dumps(bindings.as_dict(), ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    _write_binding_bytes(config, payload)


def _write_binding_bytes(config: Config, payload: bytes) -> None:
    target = config.paths.data_root / BINDINGS_RELATIVE_PATH
    _private_directory(target.parent, config.paths.data_root)
    temporary = target.parent / (".%s.tmp" % target.name)
    try:
        descriptor = os.open(
            temporary,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise TruthSetupError("truth_plane_binding_write_failed") from exc


def _binding_preimage(config: Config) -> Optional[bytes]:
    target = config.paths.data_root / BINDINGS_RELATIVE_PATH
    if not os.path.lexists(str(target)):
        return None
    try:
        return target.read_bytes()
    except OSError as exc:
        raise TruthSetupError("truth_plane_binding_read_failed") from exc


def _restore_binding_preimage(config: Config, preimage: Optional[bytes]) -> None:
    target = config.paths.data_root / BINDINGS_RELATIVE_PATH
    if preimage is None:
        if os.path.lexists(str(target)):
            if target.is_symlink() or not target.is_file():
                raise TruthSetupError("truth_plane_binding_rollback_refused")
            target.unlink()
            _fsync_directory(target.parent)
        return
    if not target.exists() or target.read_bytes() != preimage:
        _write_binding_bytes(config, preimage)


@contextmanager
def _operation_lock(config: Config):
    try:
        with runtime_lifetime_lock(config.paths.state_root):
            _private_root(config.paths.state_root)
            path = config.paths.state_root / "truth-plane-setup.lock"
            if path.is_symlink():
                raise TruthSetupError("truth_plane_setup_lock_unsafe")
            descriptor = os.open(
                path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "a+b") as handle:
                os.fchmod(handle.fileno(), 0o600)
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                yield
    except RuntimeLockError as exc:
        raise TruthSetupError("truth_plane_front_active") from exc
    except OSError as exc:
        raise TruthSetupError("truth_plane_setup_lock_failed") from exc


def _private_directory(path: Path, root: Path) -> None:
    _private_root(root)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise TruthSetupError("truth_plane_path_escapes_custody") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise TruthSetupError("truth_plane_path_contains_symlink")
        if not current.exists():
            current.mkdir(mode=0o700)
        if not current.is_dir() or current.stat().st_uid != os.getuid():
            raise TruthSetupError("truth_plane_directory_unsafe")
        current.chmod(0o700)


def _private_root(path: Path) -> None:
    if path.is_symlink():
        raise TruthSetupError("truth_plane_path_contains_symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or path.stat().st_uid != os.getuid():
        raise TruthSetupError("truth_plane_directory_unsafe")
    path.chmod(0o700)


def _remove_new_repo(root: Path) -> None:
    if root.is_symlink() or not root.is_dir():
        raise TruthSetupError("truth_plane_rollback_refused")
    shutil.rmtree(root)
    _fsync_directory(root.parent)


def _git(root: Path, *arguments: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            env=_git_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TruthSetupError("truth_plane_git_failed") from exc
    if completed.returncode != 0 or len(completed.stdout) > MAX_OUTPUT:
        raise TruthSetupError("truth_plane_git_failed")
    return completed.stdout.rstrip("\n")


def _read_refresh_target_pin(root: Path, target_ref: str) -> Optional[str]:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", target_ref],
            cwd=root,
            env=_git_environment(),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TruthSetupError("truth_plane_git_failed") from exc
    if completed.returncode == 1 and not completed.stdout and not completed.stderr:
        return None
    if (
        completed.returncode != 0
        or len(completed.stdout) > MAX_OUTPUT
        or len(completed.stderr) > MAX_OUTPUT
    ):
        raise TruthSetupError("truth_plane_git_failed")
    value = completed.stdout.rstrip("\n")
    if not _is_commit_identity(value):
        raise TruthSetupError("truth_plane_refresh_pin_conflict")
    return value


def _git_write(root: Path, *arguments: str, remote: str | None = None) -> None:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            env=_git_environment(remote),
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TruthSetupError("truth_plane_git_write_failed") from exc
    if (
        completed.returncode != 0
        or len(completed.stdout) > MAX_OUTPUT
        or len(completed.stderr) > MAX_OUTPUT
    ):
        raise TruthSetupError("truth_plane_git_write_failed")


def _git_environment(remote: str | None = None) -> Dict[str, str]:
    try:
        return managed_git_environment(remote)
    except ManagedGitError as exc:
        raise TruthSetupError("truth_plane_git_token_unsafe") from exc


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _reject_duplicate_keys(pairs: Sequence[Tuple[str, Any]]) -> Mapping[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise TruthSetupError("truth_setup_duplicate_key")
        result[key] = value
    return result
