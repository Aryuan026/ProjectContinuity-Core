"""One in-process cognition front over independent per-project Stores."""

from typing import Any, Dict, Optional, Sequence

from .acl import StageAccessError, StaticACL
from .cognee_adapter import CogneeBackend, CogneeCaseRecord, NativeCogneeBackend
from .config import Config
from .evidence import StableRef
from .promotion import (
    PromotionCoordinator,
    PromotionRequest,
    validate_promotion_id,
)
from .receipts import ReceiptStore, promotion_receipt_path
from .turritopsis_adapter import (
    ServiceFactory,
    StoreBoundaryError,
    TurritopsisAdapter,
    project_store_path,
)


FRONT_TOOLS = ("list", "search", "get", "update", "promote")
EXTERNAL_LLM_MAINTAIN_ENABLED = False
AUTOMATIC_SCHEDULE_ENABLED = False


class CognitionFront:
    """Authenticate once, route to exactly one project Store, then delegate."""

    def __init__(
        self,
        config: Config,
        *,
        service_factory: Optional[ServiceFactory] = None,
        cognee_backend: Optional[CogneeBackend] = None,
        receipt_store: Optional[ReceiptStore] = None,
    ) -> None:
        self.config = config
        self.acl = StaticACL(config)
        self._adapters: Dict[str, TurritopsisAdapter] = {}
        resolved_paths = set()
        for project in config.projects:
            path = project_store_path(config.paths.data_root, project.project_id)
            resolved = path.resolve(strict=False)
            if resolved in resolved_paths:
                raise StoreBoundaryError(
                    "projects must not alias one Turritopsis Store"
                )
            resolved_paths.add(resolved)
            arguments = {
                "project_id": project.project_id,
                "data_root": config.paths.data_root,
                "data_path": path,
            }
            if service_factory is not None:
                arguments["service_factory"] = service_factory
            self._adapters[project.project_id] = TurritopsisAdapter(**arguments)
        self._cognee_backend = cognee_backend
        self._receipt_store = receipt_store
        self._promotion: Optional[PromotionCoordinator] = None

    def list_stages(
        self, principal_id: str, project_id: str, current: str = ""
    ) -> Dict[str, Any]:
        self.acl.grant(principal_id, project_id, "list")
        return self._adapter(project_id).list_stages(current)

    def search_stages(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        *,
        match: str = "semantic",
        current: str = "",
        stage_id: str = "",
        context: int = 2,
        limit: int = 8,
        case_sensitive: bool = False,
    ) -> Dict[str, Any]:
        self.acl.grant(
            principal_id,
            project_id,
            "search",
            stage_id=stage_id or None,
        )
        adapter = self._adapter(project_id)
        try:
            if stage_id:
                adapter.get_stage(stage_id)
            return adapter.search_stages(
                query,
                match,
                current,
                stage_id,
                context,
                limit,
                case_sensitive,
            )
        except KeyError as exc:
            raise self.acl.unavailable_stage(project_id) from exc

    def get_stage(
        self, principal_id: str, project_id: str, stage_id: str
    ) -> Dict[str, Any]:
        self.acl.grant(
            principal_id, project_id, "get", stage_id=stage_id
        )
        try:
            return self._adapter(project_id).get_stage(stage_id)
        except KeyError as exc:
            raise self.acl.unavailable_stage(project_id) from exc

    def update_stage(
        self,
        principal_id: str,
        project_id: str,
        stage_id: str,
        body: str,
        *,
        expected_revision: str,
        mode: str = "replace",
        claimed_actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        context = self.acl.grant(
            principal_id,
            project_id,
            "update",
            claimed_actor=claimed_actor,
            stage_id=stage_id,
        )
        try:
            return self._adapter(project_id).update_stage(
                stage_id,
                body,
                expected_revision=expected_revision,
                actor=context.actor,
                mode=mode,
            )
        except KeyError as exc:
            raise self.acl.unavailable_stage(project_id) from exc

    async def promote_stage(
        self,
        principal_id: str,
        project_id: str,
        stage_id: str,
        *,
        source_revision: str,
        idempotency_key: str,
        provenance: Sequence[StableRef],
        review_authority: StableRef,
        promotion_kind: str = "engineering_case",
        schema_version: int = 1,
        corrects: Sequence[str] = (),
        supersedes: Sequence[str] = (),
        claimed_actor: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Archive one reviewed exact Stage revision; never couple to update."""

        context = self.acl.grant(
            principal_id,
            project_id,
            "promote",
            claimed_actor=claimed_actor,
            stage_id=stage_id,
        )
        request = PromotionRequest.create(
            project_id=project_id,
            stage_id=stage_id,
            source_revision=source_revision,
            idempotency_key=idempotency_key,
            promotion_kind=promotion_kind,
            schema_version=schema_version,
            provenance=provenance,
            review_authority=review_authority,
            corrects=corrects,
            supersedes=supersedes,
        )
        adapter = self._adapter(project_id)
        try:
            return await self._promotion_coordinator().promote(
                request,
                actor=context.actor,
                read_stage=adapter.get_stage,
            )
        except KeyError as exc:
            raise self.acl.unavailable_stage(project_id) from exc

    async def get_case(
        self, principal_id: str, project_id: str, promotion_id: str
    ) -> Optional[CogneeCaseRecord]:
        """Use the existing `get` permission for an archived Case identity."""

        self.acl.grant(principal_id, project_id, "get")
        return await self._archive_backend().lookup(
            project_id, validate_promotion_id(promotion_id)
        )

    async def search_cases(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        *,
        match: str = "keyword",
        limit: int = 8,
    ):
        """Use the existing `search` permission over the project archive."""

        self.acl.grant(principal_id, project_id, "search")
        if not isinstance(query, str) or not query.strip() or len(query) > 2_000:
            raise ValueError("query must be a bounded non-empty string")
        if type(limit) is not int or limit < 1 or limit > 50:
            raise ValueError("limit must be between 1 and 50")
        return await self._archive_backend().search(
            project_id, query.strip(), match=match, limit=limit
        )

    def _adapter(self, project_id: str) -> TurritopsisAdapter:
        try:
            return self._adapters[project_id]
        except KeyError as exc:
            raise StageAccessError("unknown project: %s" % project_id) from exc

    def _archive_backend(self) -> CogneeBackend:
        if self._cognee_backend is None:
            self._cognee_backend = NativeCogneeBackend()
        return self._cognee_backend

    def _promotion_coordinator(self) -> PromotionCoordinator:
        if self._promotion is None:
            if self._receipt_store is None:
                self._receipt_store = ReceiptStore(
                    promotion_receipt_path(self.config.paths.state_root)
                )
            self._promotion = PromotionCoordinator(
                self._receipt_store,
                self._archive_backend(),
            )
        return self._promotion
