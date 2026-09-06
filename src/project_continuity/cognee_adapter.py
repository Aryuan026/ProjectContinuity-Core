"""Thin native-SDK adapter for one project-scoped Cognee Case dataset."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import importlib.metadata
import os
from typing import Any, Dict, List, Mapping, Optional, Protocol
from uuid import NAMESPACE_URL, uuid5

from .evidence import sanitize_evidence


COGNEE_COMMIT = "a8f9760bb6da90a9956b3be77c0d0534134f533a"
COGNEE_VERSION = "1.5.2"
CASE_LABEL = "project-continuity-engineering-case"
CASE_ARCHIVE_MODE_ENV = "PROJECT_CONTINUITY_CASE_ARCHIVE_MODE"
CASE_ARCHIVE_MODE_KEY = "project_continuity_archive_mode"
CASE_ARCHIVE_MODES = frozenset({"keyword", "semantic"})


class CogneeAdapterError(RuntimeError):
    """The Cognee archive boundary rejected or could not complete a call."""


class CogneeUnavailable(CogneeAdapterError):
    """The exact Cognee runtime or project dataset is unavailable."""


class CogneeCapabilityUnavailable(CogneeUnavailable):
    """An optional Cognee capability was not explicitly configured."""


class BackendIdentityConflict(CogneeAdapterError):
    """One deterministic promotion id resolved to different archived bytes."""


@dataclass(frozen=True)
class CogneeCase:
    project_id: str
    promotion_id: str
    envelope_digest: str
    source_digest: str
    content: str
    content_digest: str
    metadata: Dict[str, Any]


@dataclass(frozen=True)
class CogneeCaseRecord:
    project_id: str
    promotion_id: str
    data_id: str
    envelope_digest: str
    source_digest: str
    content: str
    content_digest: str
    metadata: Dict[str, Any]
    ready: bool = True


class CogneeBackend(Protocol):
    async def status(self, project_id: str) -> Mapping[str, Any]: ...

    async def lookup(
        self, project_id: str, promotion_id: str
    ) -> Optional[CogneeCaseRecord]: ...

    async def upsert(self, case: CogneeCase) -> CogneeCaseRecord: ...

    async def search(
        self, project_id: str, query: str, *, match: str = "keyword", limit: int = 8
    ) -> List[Any]: ...


def project_dataset_name(project_id: str) -> str:
    return "project-continuity-%s" % project_id


def cognee_data_id(project_id: str, promotion_id: str) -> str:
    """Map an opaque promotion identity onto Cognee's native UUID primary key."""

    return str(
        uuid5(
            NAMESPACE_URL,
            "project-continuity:cognee:%s:%s" % (project_id, promotion_id),
        )
    )


async def _add_keyword_case(
    item: Any,
    dataset: Any,
    user: Any,
    *,
    node_set: List[str],
    incremental: bool,
) -> None:
    """Run Cognee's native add tasks without probing semantic providers."""

    from cognee.modules.migrations.startup import run_migrations_and_block
    from cognee.modules.pipelines import Task
    from cognee.modules.pipelines.layers.reset_dataset_pipeline_run_status import (
        reset_dataset_pipeline_run_status,
    )
    from cognee.modules.run_custom_pipeline import run_custom_pipeline
    from cognee.tasks.ingestion import ingest_data, resolve_data_directories

    await run_migrations_and_block(dataset.id, user)
    await reset_dataset_pipeline_run_status(
        dataset.id,
        user,
        pipeline_names=["add_pipeline", "cognify_pipeline"],
    )
    await run_custom_pipeline(
        tasks=[
            Task(resolve_data_directories, include_subdirectories=True),
            Task(
                ingest_data,
                dataset.name,
                user,
                node_set,
                dataset.id,
                None,
                0.5,
            ),
        ],
        data=item,
        dataset=dataset.id,
        user=user,
        pipeline_name="add_pipeline",
        incremental_loading=incremental,
        data_cache=incremental,
        run_in_background=False,
        skip_connection_test=True,
    )


class NativeCogneeBackend:
    """Use Cognee's pinned DataItem id + dataset-scoped get_data seam.

    ProjectContinuity clients never receive this SDK surface.  The sole front
    unit calls it on the backend side, preserving Cognee's primary-key
    uniqueness and exact dataset-scoped lookup without forking the donor.
    """

    async def lookup(
        self, project_id: str, promotion_id: str
    ) -> Optional[CogneeCaseRecord]:
        _assert_native_runtime()
        record = await self._lookup_record(project_id, promotion_id)
        return record if record is not None and record.ready else None

    async def status(self, project_id: str) -> Mapping[str, Any]:
        """Count provider-free readable and incomplete Cases for one project."""

        _assert_native_runtime()
        _user, dataset = await self._user_and_dataset(project_id, create=False)
        if dataset is None:
            return {
                "archive_mode": configured_case_archive_mode(),
                "dataset_name": project_dataset_name(project_id),
                "partial_cases": 0,
                "ready_cases": 0,
            }
        from cognee.modules.data.methods.get_dataset_data import get_dataset_data

        ready = 0
        partial = 0
        for row in await get_dataset_data(dataset.id):
            if getattr(row, "label", None) != CASE_LABEL:
                continue
            metadata = (
                row.external_metadata
                if isinstance(row.external_metadata, dict)
                else {}
            )
            if metadata.get("project_id") != project_id or not metadata.get(
                "promotion_id"
            ):
                continue
            if cognee_row_is_ready(row, dataset.id, metadata):
                ready += 1
            else:
                partial += 1
        return {
            "archive_mode": configured_case_archive_mode(),
            "dataset_name": project_dataset_name(project_id),
            "partial_cases": partial,
            "ready_cases": ready,
        }

    async def _lookup_record(
        self, project_id: str, promotion_id: str
    ) -> Optional[CogneeCaseRecord]:
        user, dataset = await self._user_and_dataset(project_id, create=False)
        if dataset is None:
            return None
        from cognee.modules.data.methods import get_data

        row = await get_data(
            user.id,
            _uuid(cognee_data_id(project_id, promotion_id)),
            dataset_id=dataset.id,
        )
        if row is None:
            return None
        return await self._record_from_row(project_id, row, dataset.id)

    async def upsert(self, case: CogneeCase) -> CogneeCaseRecord:
        _assert_native_runtime()
        existing = await self._lookup_record(case.project_id, case.promotion_id)
        if existing is not None:
            verify_case_record(case, existing, require_ready=False)
            if existing.ready:
                return existing

        user, dataset = await self._user_and_dataset(case.project_id, create=True)
        if dataset is None:  # pragma: no cover - create=True contract guard
            raise CogneeUnavailable("Cognee did not create the project dataset")

        import cognee
        from cognee.tasks.ingestion.data_item import DataItem

        archive_mode = configured_case_archive_mode()
        data_id = _uuid(cognee_data_id(case.project_id, case.promotion_id))
        metadata = dict(case.metadata)
        metadata[CASE_ARCHIVE_MODE_KEY] = archive_mode
        item = DataItem(
            data=case.content,
            data_id=data_id,
            label=CASE_LABEL,
            external_metadata=metadata,
        )
        if archive_mode == "keyword":
            # Cognee's native add tasks are the durable keyword archive.  Its
            # public add() does not forward the donor's caller-scoped
            # skip_connection_test seam, so use the public custom-pipeline
            # runner with the exact native tasks instead of a process-global
            # provider bypass.  A later semantic call still performs its own
            # connection checks.
            await _add_keyword_case(
                item,
                dataset,
                user,
                node_set=["project:%s" % case.project_id, "engineering-case"],
                incremental=existing is None,
            )
        elif existing is None:
            await cognee.remember(
                item,
                dataset_id=dataset.id,
                dataset_name=dataset.name,
                user=user,
                node_set=["project:%s" % case.project_id, "engineering-case"],
                run_in_background=False,
                self_improvement=False,
            )
        else:
            # Mark legacy/partial rows with the explicit semantic contract
            # before resuming cognify.  Regular add updates metadata without
            # relying on the prior add-pipeline cache.
            await cognee.add(
                item,
                dataset_id=dataset.id,
                dataset_name=dataset.name,
                user=user,
                node_set=["project:%s" % case.project_id, "engineering-case"],
                incremental_loading=False,
                data_cache=False,
                run_in_background=False,
            )
            await cognee.cognify(
                datasets=[dataset.id],
                user=user,
                run_in_background=False,
            )
        record = await self.lookup(case.project_id, case.promotion_id)
        if record is None:
            raise CogneeUnavailable("Cognee write completed without exact readback")
        verify_case_record(case, record)
        return record

    async def search(
        self, project_id: str, query: str, *, match: str = "keyword", limit: int = 8
    ) -> List[Any]:
        _assert_native_runtime()
        if not isinstance(query, str) or not query.strip() or len(query) > 2_000:
            raise ValueError("query must be a bounded non-empty string")
        if type(limit) is not int or limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        if match not in {"keyword", "semantic"}:
            raise ValueError("Case search match must be keyword or semantic")
        if match == "semantic":
            _require_semantic_search_configuration()
        user, dataset = await self._user_and_dataset(project_id, create=False)
        if dataset is None:
            return []
        if match == "keyword":
            from cognee.modules.data.methods.get_dataset_data import get_dataset_data

            records = []
            for row in await get_dataset_data(dataset.id):
                record = await self._record_from_row(project_id, row, dataset.id)
                if record.ready and record.promotion_id:
                    records.append(record)
            return case_keyword_search(records, query.strip(), limit=limit)
        import cognee
        from cognee.modules.search.types import SearchType

        results = await cognee.search(
            query.strip(),
            query_type=SearchType.CHUNKS,
            user=user,
            dataset_ids=[dataset.id],
            top_k=limit,
        )
        return sanitize_evidence(
            [_jsonable(result) for result in results],
            max_depth=10,
            max_items=limit,
            max_string=20_000,
        )

    async def _record_from_row(
        self, project_id: str, row: Any, dataset_id: Any
    ) -> CogneeCaseRecord:
        content = await _read_row_content(row)
        metadata = row.external_metadata if isinstance(row.external_metadata, dict) else {}
        return CogneeCaseRecord(
            project_id=project_id,
            promotion_id=str(metadata.get("promotion_id", "")),
            data_id=str(row.id),
            envelope_digest=str(metadata.get("envelope_digest", "")),
            source_digest=str(metadata.get("source_digest", "")),
            content=content,
            content_digest=_digest(content),
            metadata=dict(metadata),
            ready=cognee_row_is_ready(row, dataset_id, metadata),
        )

    async def _user_and_dataset(self, project_id: str, *, create: bool):
        from cognee.modules.data.methods import (
            create_authorized_dataset,
            get_datasets_by_name,
        )
        from cognee.modules.engine.operations.setup import setup
        from cognee.modules.users.methods import get_default_user

        await setup()
        user = await get_default_user()
        name = project_dataset_name(project_id)
        datasets = await get_datasets_by_name(name, user.id)
        if len(datasets) > 1:
            raise BackendIdentityConflict("multiple Cognee datasets share one project name")
        if datasets:
            return user, datasets[0]
        if not create:
            return user, None
        return user, await create_authorized_dataset(name, user)


def verify_case_record(
    expected: CogneeCase,
    actual: CogneeCaseRecord,
    *,
    require_ready: bool = True,
) -> None:
    fields = (
        "project_id",
        "promotion_id",
        "envelope_digest",
        "source_digest",
        "content_digest",
    )
    if any(getattr(expected, field) != getattr(actual, field) for field in fields):
        raise BackendIdentityConflict(
            "Cognee promotion identity exists with different archived evidence"
        )
    if require_ready and not actual.ready:
        raise CogneeUnavailable("Cognee Case exists but archive processing is incomplete")


def cognee_row_is_ready(
    row: Any,
    dataset_id: Any,
    metadata: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Accept the donor completion marker for the row's explicit archive mode.

    Rows written before the keyword mode existed have no marker and retain the
    original cognify-completed contract.  This keeps historical semantic Cases
    readable without silently reclassifying partial legacy writes as ready.
    """

    raw_status = getattr(row, "pipeline_status", None)
    pipeline_status = raw_status if isinstance(raw_status, dict) else {}
    raw_metadata = (
        metadata
        if isinstance(metadata, Mapping)
        else getattr(row, "external_metadata", None)
    )
    row_metadata = raw_metadata if isinstance(raw_metadata, Mapping) else {}
    archive_mode = row_metadata.get(CASE_ARCHIVE_MODE_KEY)
    if archive_mode == "keyword":
        add_status = pipeline_status.get("add_pipeline", {})
        return (
            isinstance(add_status, dict)
            and add_status.get(str(dataset_id)) == "DATA_ITEM_PROCESSING_COMPLETED"
        )
    if archive_mode not in {None, "semantic"}:
        return False
    cognify_status = pipeline_status.get("cognify_pipeline", {})
    return (
        isinstance(cognify_status, dict)
        and cognify_status.get(str(dataset_id)) == "DATA_ITEM_PROCESSING_COMPLETED"
    )


def configured_case_archive_mode() -> str:
    """Return the reviewed archive mode; keyword is the provider-free default."""

    value = os.environ.get(CASE_ARCHIVE_MODE_ENV, "keyword")
    if value not in CASE_ARCHIVE_MODES:
        raise CogneeAdapterError("Case archive mode must be keyword or semantic")
    return value


def case_keyword_search(
    records: List[CogneeCaseRecord], query: str, *, limit: int = 8
) -> List[Dict[str, Any]]:
    """Reuse Turritopsis' pinned CJK lexical scorer without a second index."""

    from turritopsis.search import semantic_search

    by_id = {record.promotion_id: record for record in records}
    data = {
        "currents": [
            {
                "id": "project-cases",
                "name": "Reviewed engineering Cases",
                "blurb": "Project history with provenance",
                "stages": [
                    {
                        "id": record.promotion_id,
                        "title": record.promotion_id,
                        "body": record.content,
                    }
                    for record in records
                ],
            }
        ]
    }
    results = []
    for match in semantic_search(data, query, limit=limit):
        promotion_id = str(match["stage_id"])
        record = by_id[promotion_id]
        results.append(
            {
                "promotion_id": promotion_id,
                "data_id": record.data_id,
                "source_digest": record.source_digest,
                "content_digest": record.content_digest,
                "score": match["score"],
                "matched": match["matched"],
                "snippet": match["snippet"],
                "ready": True,
            }
        )
    return results


def _require_semantic_search_configuration() -> None:
    required = ("EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS")
    if any(not os.environ.get(name, "").strip() for name in required):
        raise CogneeCapabilityUnavailable(
            "case semantic search requires explicit embedding configuration"
        )


def _assert_runtime_version() -> None:
    try:
        installed = importlib.metadata.version("cognee")
    except importlib.metadata.PackageNotFoundError as exc:
        raise CogneeUnavailable("exact Cognee runtime is not installed") from exc
    if installed != COGNEE_VERSION:
        raise CogneeUnavailable(
            "Cognee runtime version mismatch: expected %s" % COGNEE_VERSION
        )


def _assert_native_runtime() -> None:
    _assert_runtime_version()
    from cognee.api.v1.serve.state import get_remote_client

    if get_remote_client() is not None:
        raise CogneeUnavailable(
            "remote Cognee add cannot preserve deterministic promotion identity"
        )


async def _read_row_content(row: Any) -> str:
    from cognee.infrastructure.files.utils.open_data_file import open_data_file

    async with open_data_file(row.raw_data_location) as handle:
        content = handle.read()
        if hasattr(content, "__await__"):
            content = await content
    if isinstance(content, bytes):
        return content.decode("utf-8")
    return str(content)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump()
    if isinstance(value, (dict, list, tuple, str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return str(value)


def _uuid(value: str):
    from uuid import UUID

    return UUID(value)


def _digest(content: str) -> str:
    return "sha256:" + sha256(content.encode("utf-8")).hexdigest()
