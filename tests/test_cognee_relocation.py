from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from hashlib import sha256
import importlib
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from project_continuity.cognee_adapter import CASE_LABEL, cognee_data_id
from project_continuity.cognee_relocation import (
    CASE_SCHEMA,
    CogneeRelocationError,
    _direct_ladybug_graph,
    _location,
    _plan_row,
    _relocate_cognee_case_storage,
    _with_graph_state,
    relocate_cognee_case_storage,
)
from project_continuity.config import load_config
from project_continuity.runtime_lock import runtime_lifetime_lock

from conftest import write_config

PROJECT_ID = "alpha"
PROMOTION_ID = "promotion:" + "a" * 64
DATA_ID = cognee_data_id(PROJECT_ID, PROMOTION_ID)
DATASET_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def _digest(value: str) -> str:
    return "sha256:" + sha256(value.encode("utf-8")).hexdigest()


def _tree_digest(root: Path) -> str:
    digest = sha256()
    for path in sorted(root.rglob("*")):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        if path.is_dir():
            digest.update(b"directory")
        else:
            digest.update(b"file")
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    previous = Path("/previous/project-continuity/cognee/data")
    target = tmp_path / "current/cognee/data"
    target.mkdir(parents=True)
    relative = Path("documents/alpha/case.txt")
    file_path = target / relative
    file_path.parent.mkdir(parents=True)
    file_path.write_text("连续性恢复测试", encoding="utf-8")
    return previous, target, relative


def _row(source_uri: str, content: str = "连续性恢复测试") -> SimpleNamespace:
    return SimpleNamespace(
        id=UUID(DATA_ID),
        label=CASE_LABEL,
        dataset_id=DATASET_ID,
        raw_data_location=source_uri,
        original_data_location=source_uri,
        pipeline_status={
            "cognify_pipeline": {str(DATASET_ID): "DATA_ITEM_PROCESSING_COMPLETED"}
        },
        external_metadata={
            "schema": CASE_SCHEMA,
            "project_id": PROJECT_ID,
            "promotion_id": PROMOTION_ID,
            "case_content_digest": _digest(content),
        },
    )


def test_plan_rebases_only_location_and_preserves_case_identity(tmp_path: Path) -> None:
    source_root, target_root, relative = _roots(tmp_path)
    row = _row((source_root / relative).as_uri())

    plan = _plan_row(
        row,
        project_id=PROJECT_ID,
        dataset_id=DATASET_ID,
        source_root=source_root,
        target_root=target_root,
    )
    plan = _with_graph_state(
        plan,
        {"id": DATA_ID, "raw_data_location": (source_root / relative).as_uri()},
        source_root,
        target_root,
    )

    assert plan.data_id == DATA_ID
    assert plan.promotion_id == PROMOTION_ID
    assert plan.target_uri == (target_root / relative).as_uri()
    assert plan.content_digest == _digest("连续性恢复测试")
    assert plan.relational_change is True
    assert plan.graph_change is True


def test_plan_rejects_a_case_without_a_ready_pipeline_marker(tmp_path: Path) -> None:
    source_root, target_root, relative = _roots(tmp_path)
    row = _row((source_root / relative).as_uri())
    del row.pipeline_status

    with pytest.raises(CogneeRelocationError, match="processing is incomplete"):
        _plan_row(
            row,
            project_id=PROJECT_ID,
            dataset_id=DATASET_ID,
            source_root=source_root,
            target_root=target_root,
        )


def test_already_relocated_row_and_graph_are_idempotent(tmp_path: Path) -> None:
    source_root, target_root, relative = _roots(tmp_path)
    target_uri = (target_root / relative).as_uri()
    plan = _plan_row(
        _row(target_uri),
        project_id=PROJECT_ID,
        dataset_id=DATASET_ID,
        source_root=source_root,
        target_root=target_root,
    )
    plan = _with_graph_state(
        plan,
        {"raw_data_location": target_uri},
        source_root,
        target_root,
    )

    assert plan.relational_change is False
    assert plan.graph_change is False


@pytest.mark.parametrize(
    "mutation, expected",
    [
        (lambda row: setattr(row, "label", "foreign"), "non-ProjectContinuity"),
        (
            lambda row: row.external_metadata.__setitem__("project_id", "beta"),
            "project or dataset identity",
        ),
        (
            lambda row: row.external_metadata.__setitem__(
                "case_content_digest", "sha256:" + "0" * 64
            ),
            "content digest changed",
        ),
        (lambda row: setattr(row, "id", UUID(int=1)), "deterministic Data identity"),
    ],
)
def test_case_identity_and_content_drift_fail_closed(
    tmp_path: Path, mutation, expected: str
) -> None:
    source_root, target_root, relative = _roots(tmp_path)
    row = _row((source_root / relative).as_uri())
    mutation(row)

    with pytest.raises(CogneeRelocationError, match=expected):
        _plan_row(
            row,
            project_id=PROJECT_ID,
            dataset_id=DATASET_ID,
            source_root=source_root,
            target_root=target_root,
        )


def test_location_rejects_foreign_scheme_root_and_query(tmp_path: Path) -> None:
    source_root, target_root, relative = _roots(tmp_path)
    invalid = (
        "https://example.invalid/case.txt",
        "file:///outside/case.txt",
        (source_root / relative).as_uri() + "?token=secret",
    )
    for value in invalid:
        with pytest.raises(CogneeRelocationError):
            _location(value, source_root, target_root)


def test_target_symlink_escape_is_rejected(tmp_path: Path) -> None:
    source_root, target_root, relative = _roots(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("连续性恢复测试", encoding="utf-8")
    target_file = target_root / relative
    target_file.unlink()
    target_file.symlink_to(outside)

    with pytest.raises(CogneeRelocationError, match="symlink"):
        _plan_row(
            _row((source_root / relative).as_uri()),
            project_id=PROJECT_ID,
            dataset_id=DATASET_ID,
            source_root=source_root,
            target_root=target_root,
        )


def test_graph_node_must_share_the_exact_relative_case_path(tmp_path: Path) -> None:
    source_root, target_root, relative = _roots(tmp_path)
    plan = _plan_row(
        _row((source_root / relative).as_uri()),
        project_id=PROJECT_ID,
        dataset_id=DATASET_ID,
        source_root=source_root,
        target_root=target_root,
    )

    with pytest.raises(CogneeRelocationError, match="locations disagree"):
        _with_graph_state(
            plan,
            {"raw_data_location": (source_root / "different.txt").as_uri()},
            source_root,
            target_root,
        )


def test_non_ready_case_fails_before_graph_or_relational_mutation(
    monkeypatch, tmp_path: Path
) -> None:
    config = load_config(write_config(tmp_path / "runtime"))
    target_root = config.paths.data_root / "cognee/data"
    target_root.mkdir(parents=True)
    relative = Path("documents/alpha/case.txt")
    target_file = target_root / relative
    target_file.parent.mkdir(parents=True)
    target_file.write_text("连续性恢复测试", encoding="utf-8")
    graph_root = config.paths.data_root / "cognee/system/databases"
    graph_file = graph_root / "user/alpha.lbug/graph.bin"
    graph_file.parent.mkdir(parents=True)
    graph_file.write_bytes(b"existing-ladybug-state")
    source_root = tmp_path / "previous/cognee/data"
    row = _row((source_root / relative).as_uri())
    row.pipeline_status = {
        "cognify_pipeline": {str(DATASET_ID): "DATA_ITEM_PROCESSING_STARTED"}
    }
    before = target_file.read_bytes()
    graph_before = _tree_digest(graph_root)
    graph_engine_calls = []
    relational_calls = []

    async def forbidden_graph(_config):
        graph_engine_calls.append("opened")
        raise AssertionError("graph engine must not open for a non-ready Case")

    async def fake_setup():
        return None

    async def fake_user():
        return SimpleNamespace(id=UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"))

    async def fake_datasets(name, _user_id):
        if name.endswith("alpha"):
            return [SimpleNamespace(id=DATASET_ID)]
        return []

    async def fake_rows(_dataset_id):
        return [row]

    @asynccontextmanager
    async def fake_context(*_arguments):
        yield

    async def fake_relational(*_arguments):
        relational_calls.append("write")

    monkeypatch.setattr(
        "project_continuity.cognee_relocation._assert_native_runtime", lambda: None
    )
    monkeypatch.setattr(
        "project_continuity.cognee_relocation._direct_ladybug_graph", forbidden_graph
    )
    monkeypatch.setattr(
        "project_continuity.cognee_relocation._update_relational_rows",
        fake_relational,
    )
    monkeypatch.setattr("cognee.modules.engine.operations.setup.setup", fake_setup)
    monkeypatch.setattr("cognee.modules.users.methods.get_default_user", fake_user)
    monkeypatch.setattr(
        "cognee.modules.data.methods.get_datasets_by_name", fake_datasets
    )
    dataset_data_module = importlib.import_module(
        "cognee.modules.data.methods.get_dataset_data"
    )
    monkeypatch.setattr(dataset_data_module, "get_dataset_data", fake_rows)
    monkeypatch.setattr(
        "cognee.context_global_variables.set_database_global_context_variables",
        fake_context,
    )

    with pytest.raises(CogneeRelocationError, match="processing is incomplete"):
        asyncio.run(_relocate_cognee_case_storage(config, tmp_path / "previous"))

    assert graph_engine_calls == []
    assert relational_calls == []
    assert target_file.read_bytes() == before
    assert _tree_digest(graph_root) == graph_before


@pytest.mark.parametrize(
    "provider, subprocess_enabled, graph_location",
    [
        ("neo4j", False, "inside"),
        ("ladybug", False, "outside"),
        ("ladybug", True, "inside"),
    ],
)
def test_dataset_graph_context_drift_fails_before_engine_access(
    monkeypatch,
    tmp_path: Path,
    provider: str,
    subprocess_enabled: bool,
    graph_location: str,
) -> None:
    config = load_config(write_config(tmp_path / "runtime"))
    system_root = config.paths.data_root / "cognee/system"
    system_root.mkdir(parents=True)
    inside = system_root / "databases/user/alpha.lbug"
    outside = tmp_path / "outside/alpha.lbug"
    selected = inside if graph_location == "inside" else outside
    selected.parent.mkdir(parents=True)
    selected.write_text("graph", encoding="utf-8")
    engine_calls = []

    graph_config_module = importlib.import_module(
        "cognee.infrastructure.databases.graph.config"
    )
    graph_engine_module = importlib.import_module(
        "cognee.infrastructure.databases.graph.get_graph_engine"
    )

    monkeypatch.setattr(
        graph_config_module,
        "get_graph_context_config",
        lambda: {
            "graph_database_provider": provider,
            "graph_database_subprocess_enabled": subprocess_enabled,
            "graph_file_path": str(selected),
        },
    )

    async def forbidden_engine():
        engine_calls.append("opened")

    monkeypatch.setattr(graph_engine_module, "get_graph_engine", forbidden_engine)

    with pytest.raises(CogneeRelocationError):
        asyncio.run(_direct_ladybug_graph(config))
    assert engine_calls == []


def test_active_front_lock_refuses_relocation_before_backend_mutation(
    monkeypatch, tmp_path: Path
) -> None:
    config = load_config(write_config(tmp_path / "runtime"))
    called = False

    async def fake_relocation(_config, _previous):
        nonlocal called
        called = True
        return {"ok": True}

    monkeypatch.setattr(
        "project_continuity.cognee_relocation._relocate_cognee_case_storage",
        fake_relocation,
    )
    with runtime_lifetime_lock(config.paths.state_root):
        with pytest.raises(CogneeRelocationError, match="already active"):
            asyncio.run(relocate_cognee_case_storage(config, Path("/previous")))

    assert called is False
