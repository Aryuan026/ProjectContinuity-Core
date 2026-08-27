"""Offline relocation of ProjectContinuity Case files inside one Cognee store.

Cognee intentionally records local ``file://`` locations for ingested Data.
Those locations remain correct across process restarts, but a byte-for-byte
snapshot restored under another custody root needs one explicit rebase.  This
module keeps that operation narrow: only deterministic ProjectContinuity Case
rows are accepted, and Cognee's own relational and graph APIs remain the
writers of their respective stores.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import os
from pathlib import Path
import stat
from typing import Any, Dict, List, Mapping, Sequence
from urllib.parse import unquote, urlsplit
from uuid import UUID

from .client import path_has_symlink
from .cognee_adapter import (
    CASE_LABEL,
    NativeCogneeBackend,
    _assert_native_runtime,
    cognee_row_is_ready,
    cognee_data_id,
    project_dataset_name,
)
from .config import Config
from .promotion import validate_promotion_id
from .runtime_lock import RuntimeLockError, runtime_lifetime_lock

MAX_CASE_BYTES = 10 * 1024 * 1024
CASE_SCHEMA = "project-continuity.promotion.v1"


class CogneeRelocationError(RuntimeError):
    """An offline Cognee snapshot cannot be safely rebased."""


@dataclass(frozen=True)
class CasePathRelocation:
    project_id: str
    promotion_id: str
    data_id: str
    dataset_id: Any
    target_uri: str
    content_digest: str
    relational_change: bool
    graph_change: bool


async def relocate_cognee_case_storage(
    config: Config, previous_data_root: Path
) -> Dict[str, Any]:
    """Rebase restored Case file locations to ``config.paths.data_root``.

    A shared non-blocking lifetime lock refuses the operation while the front
    is active.  The function first validates every configured project dataset
    and target file, then patches Cognee graph nodes through
    ``GraphDBInterface`` and Data rows through its SQLAlchemy engine.
    Graph-first ordering makes an interrupted run idempotently resumable:
    graph-at-target + row-at-source is an accepted recovery state, while the
    inverse is never committed here.
    """

    try:
        with runtime_lifetime_lock(config.paths.state_root):
            return await _relocate_cognee_case_storage(config, previous_data_root)
    except RuntimeLockError as exc:
        raise CogneeRelocationError(str(exc)) from exc
    except CogneeRelocationError:
        raise
    except Exception as exc:
        raise CogneeRelocationError("Cognee relocation backend failed") from exc


async def _relocate_cognee_case_storage(
    config: Config, previous_data_root: Path
) -> Dict[str, Any]:
    _assert_native_runtime()
    previous_root = _logical_root(previous_data_root, "previous data root")
    source_root = previous_root / "cognee/data"
    target_root = _target_root(config)
    if source_root == target_root:
        raise CogneeRelocationError("previous and target Cognee data roots are equal")

    from cognee.context_global_variables import set_database_global_context_variables
    from cognee.modules.data.methods import get_datasets_by_name
    from cognee.modules.data.methods.get_dataset_data import get_dataset_data
    from cognee.modules.engine.operations.setup import setup
    from cognee.modules.users.methods import get_default_user

    await setup()
    user = await get_default_user()
    relational_inventory: List[tuple[Any, List[CasePathRelocation]]] = []

    for project in config.projects:
        datasets = await get_datasets_by_name(
            project_dataset_name(project.project_id), user.id
        )
        if len(datasets) > 1:
            raise CogneeRelocationError(
                "multiple Cognee datasets share project identity %s"
                % project.project_id
            )
        if not datasets:
            continue
        dataset = datasets[0]
        async with set_database_global_context_variables(dataset.id, user.id):
            plans = []
            for row in await get_dataset_data(dataset.id):
                plans.append(
                    _plan_row(
                        row,
                        project_id=project.project_id,
                        dataset_id=dataset.id,
                        source_root=source_root,
                        target_root=target_root,
                    )
                )
        relational_inventory.append((dataset, plans))

    inventory: List[tuple[Any, List[CasePathRelocation]]] = []
    for dataset, relational_plans in relational_inventory:
        async with set_database_global_context_variables(dataset.id, user.id):
            graph = await _direct_ladybug_graph(config)
            plans = []
            for plan in relational_plans:
                node = await graph.get_node(plan.data_id)
                plans.append(_with_graph_state(plan, node, source_root, target_root))
        inventory.append((dataset, plans))

    # All predictable validation has completed before the first mutation.
    for dataset, plans in inventory:
        async with set_database_global_context_variables(dataset.id, user.id):
            graph = await _direct_ladybug_graph(config)
            for plan in plans:
                if not plan.graph_change:
                    continue
                updated = await graph.update_node(
                    plan.data_id, {"raw_data_location": plan.target_uri}
                )
                if not updated:
                    raise CogneeRelocationError(
                        "Cognee graph node disappeared during relocation"
                    )
                node = await graph.get_node(plan.data_id)
                if (
                    not isinstance(node, Mapping)
                    or node.get("raw_data_location") != plan.target_uri
                ):
                    raise CogneeRelocationError(
                        "Cognee graph relocation readback failed"
                    )

    await _update_relational_rows(inventory, user.id, source_root, target_root)

    backend = NativeCogneeBackend()
    records = []
    for _dataset, plans in inventory:
        for plan in plans:
            record = await backend.lookup(plan.project_id, plan.promotion_id)
            if (
                record is None
                or record.data_id != plan.data_id
                or record.content_digest != plan.content_digest
            ):
                raise CogneeRelocationError("relocated Case exact readback failed")
            records.append(
                {
                    "project_id": plan.project_id,
                    "promotion_id": plan.promotion_id,
                    "data_id": plan.data_id,
                    "content_digest": plan.content_digest,
                    "status": (
                        "relocated"
                        if plan.relational_change or plan.graph_change
                        else "already_target"
                    ),
                }
            )

    return {
        "ok": True,
        "action": "relocate_cognee_case_storage",
        "schema": "project-continuity.cognee-relocation.v1",
        "source_root_digest": _text_digest(str(source_root)),
        "target_root_digest": _text_digest(str(target_root)),
        "case_count": len(records),
        "records": sorted(
            records, key=lambda item: (item["project_id"], item["promotion_id"])
        ),
    }


async def _update_relational_rows(
    inventory: Sequence[tuple[Any, List[CasePathRelocation]]],
    user_id: Any,
    source_root: Path,
    target_root: Path,
) -> None:
    from cognee.context_global_variables import set_database_global_context_variables
    from cognee.infrastructure.databases.relational import get_relational_engine
    from cognee.modules.data.models import Data

    for dataset, plans in inventory:
        async with set_database_global_context_variables(dataset.id, user_id):
            engine = get_relational_engine()
            async with engine.get_async_session() as session:
                for plan in plans:
                    row = await session.get(Data, UUID(plan.data_id))
                    if row is None or row.dataset_id != dataset.id:
                        raise CogneeRelocationError(
                            "Cognee Data row disappeared during relocation"
                        )
                    current = _plan_row(
                        row,
                        project_id=plan.project_id,
                        dataset_id=dataset.id,
                        source_root=source_root,
                        target_root=target_root,
                    )
                    if (
                        current.data_id != plan.data_id
                        or current.target_uri != plan.target_uri
                    ):
                        raise CogneeRelocationError(
                            "Cognee Data row changed during relocation"
                        )
                    row.raw_data_location = plan.target_uri
                    row.original_data_location = plan.target_uri
                await session.commit()


def _plan_row(
    row: Any,
    *,
    project_id: str,
    dataset_id: Any,
    source_root: Path,
    target_root: Path,
) -> CasePathRelocation:
    metadata = (
        row.external_metadata if isinstance(row.external_metadata, dict) else None
    )
    if (
        row.label != CASE_LABEL
        or metadata is None
        or metadata.get("schema") != CASE_SCHEMA
    ):
        raise CogneeRelocationError(
            "project Cognee dataset contains a non-ProjectContinuity Case row"
        )
    if not cognee_row_is_ready(row, dataset_id):
        raise CogneeRelocationError("Cognee Case archive processing is incomplete")
    if (
        str(metadata.get("project_id", "")) != project_id
        or row.dataset_id != dataset_id
    ):
        raise CogneeRelocationError("Cognee Case project or dataset identity changed")
    try:
        promotion_id = validate_promotion_id(str(metadata.get("promotion_id", "")))
    except ValueError as exc:
        raise CogneeRelocationError(
            "Cognee Case promotion identity is invalid"
        ) from exc
    data_id = str(row.id)
    if data_id != cognee_data_id(project_id, promotion_id):
        raise CogneeRelocationError("Cognee Case deterministic Data identity changed")

    raw_relative, raw_state = _location(row.raw_data_location, source_root, target_root)
    original_relative, original_state = _location(
        row.original_data_location, source_root, target_root
    )
    if raw_relative != original_relative:
        raise CogneeRelocationError("Cognee Case source locations disagree")
    target = target_root / raw_relative
    _validate_target_file(target, target_root)
    try:
        content = target.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CogneeRelocationError("Cognee Case file cannot be read as UTF-8") from exc
    content_digest = _text_digest(content)
    if metadata.get("case_content_digest") != content_digest:
        raise CogneeRelocationError("Cognee Case content digest changed in snapshot")
    return CasePathRelocation(
        project_id=project_id,
        promotion_id=promotion_id,
        data_id=data_id,
        dataset_id=dataset_id,
        target_uri=target.as_uri(),
        content_digest=content_digest,
        relational_change=raw_state == "source" or original_state == "source",
        graph_change=False,
    )


async def _direct_ladybug_graph(config: Config):
    from cognee.infrastructure.databases.graph.config import get_graph_context_config
    from cognee.infrastructure.databases.graph.get_graph_engine import get_graph_engine

    _validate_ladybug_context(
        get_graph_context_config(),
        config.paths.data_root / "cognee/system",
    )
    return await get_graph_engine()


def _validate_ladybug_context(value: Any, system_root: Path) -> None:
    if not isinstance(value, Mapping):
        raise CogneeRelocationError("Cognee dataset graph context is malformed")
    if value.get("graph_database_provider") != "ladybug":
        raise CogneeRelocationError("Cognee dataset graph provider is not ladybug")
    if value.get("graph_database_subprocess_enabled") is not False:
        raise CogneeRelocationError("Cognee dataset graph subprocess mode is enabled")

    root = Path(system_root)
    raw_path = value.get("graph_file_path")
    if (
        not root.is_absolute()
        or path_has_symlink(root)
        or not root.is_dir()
        or root.resolve(strict=True) != root
        or not isinstance(raw_path, str)
        or not raw_path
        or raw_path != raw_path.strip()
    ):
        raise CogneeRelocationError("Cognee dataset graph path is outside custody")
    graph_path = Path(raw_path)
    try:
        resolved = graph_path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise CogneeRelocationError(
            "Cognee dataset graph path is outside custody"
        ) from exc
    if not graph_path.is_absolute() or path_has_symlink(graph_path):
        raise CogneeRelocationError("Cognee dataset graph path is outside custody")


def _with_graph_state(
    plan: CasePathRelocation,
    node: Any,
    source_root: Path,
    target_root: Path,
) -> CasePathRelocation:
    if not isinstance(node, Mapping):
        raise CogneeRelocationError("Cognee Case graph node is missing")
    relative, state = _location(node.get("raw_data_location"), source_root, target_root)
    if (target_root / relative).as_uri() != plan.target_uri:
        raise CogneeRelocationError("Cognee graph and Data row locations disagree")
    return CasePathRelocation(
        project_id=plan.project_id,
        promotion_id=plan.promotion_id,
        data_id=plan.data_id,
        dataset_id=plan.dataset_id,
        target_uri=plan.target_uri,
        content_digest=plan.content_digest,
        relational_change=plan.relational_change,
        graph_change=state == "source",
    )


def _location(value: Any, source_root: Path, target_root: Path) -> tuple[Path, str]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CogneeRelocationError("Cognee Case location is malformed")
    parsed = urlsplit(value)
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        raise CogneeRelocationError("Cognee Case location must be a local file URI")
    decoded = unquote(parsed.path)
    if not decoded or "\x00" in decoded:
        raise CogneeRelocationError("Cognee Case file URI is malformed")
    path = Path(decoded)
    if not path.is_absolute():
        raise CogneeRelocationError("Cognee Case file URI is not absolute")
    for root, state in ((source_root, "source"), (target_root, "target")):
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        if not relative.parts or any(
            part in {"", ".", ".."} for part in relative.parts
        ):
            raise CogneeRelocationError(
                "Cognee Case file URI has no safe relative path"
            )
        return relative, state
    raise CogneeRelocationError(
        "Cognee Case file URI is outside source and target roots"
    )


def _target_root(config: Config) -> Path:
    root = config.paths.data_root / "cognee/data"
    if (
        not root.is_absolute()
        or path_has_symlink(root)
        or not root.is_dir()
        or root.resolve(strict=True) != root
    ):
        raise CogneeRelocationError(
            "target Cognee data root is not a safe real directory"
        )
    return root


def _validate_target_file(path: Path, root: Path) -> None:
    if path_has_symlink(path):
        raise CogneeRelocationError("Cognee Case target path contains a symlink")
    try:
        file_stat = path.stat()
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise CogneeRelocationError(
            "Cognee Case target file is outside custody"
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size > MAX_CASE_BYTES:
        raise CogneeRelocationError("Cognee Case target is not a bounded regular file")
    if file_stat.st_mode & 0o022:
        raise CogneeRelocationError("Cognee Case target file is group/world writable")


def _logical_root(value: Path, where: str) -> Path:
    path = Path(value)
    if (
        not path.is_absolute()
        or path == Path(path.anchor)
        or path != Path(os.path.abspath(os.fspath(path)))
    ):
        raise CogneeRelocationError("%s must be an absolute normalized path" % where)
    return path


def _text_digest(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()
