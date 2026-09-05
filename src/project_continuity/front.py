"""One in-process cognition front over independent per-project Stores."""

import asyncio

from typing import Any, Dict, Mapping, Optional, Sequence

from .acl import StageAccessError, StaticACL
from .cognee_adapter import (
    CogneeBackend,
    CogneeCapabilityUnavailable,
    CogneeCaseRecord,
    CogneeUnavailable,
    NativeCogneeBackend,
)
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
from .truth_plane import (
    EXTERNAL_LAYERS,
    IntegratedTruthPlane,
    build_installed_truth_plane,
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
        truth_plane: Optional[IntegratedTruthPlane] = None,
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
        self._truth_plane = truth_plane or build_installed_truth_plane(config)

    def list_project(
        self, principal_id: str, project_id: str, current: str = ""
    ) -> Dict[str, Any]:
        """Preserve donor orientation and expose every authority layer honestly."""

        result = dict(self.list_stages(principal_id, project_id, current))
        result["truth_plane"] = self._truth_plane.list_layers(
            principal_id, project_id
        )
        return result

    async def list_project_complete(
        self,
        principal_id: str,
        project_id: str,
        current: str = "",
        *,
        base: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Add provider-free history status to the complete authority inventory."""

        result = dict(base or self.list_project(principal_id, project_id, current))
        external = result["truth_plane"]["coverage"]
        consulted = ["current", "history", *external["consulted"]]
        matched = ["current", *external["matched"]]
        unavailable = dict(external["unavailable"])
        failed = dict(external["failed"])
        try:
            history = dict(await self._archive_backend().status(project_id))
        except CogneeUnavailable:
            reason = "history_archive_unavailable"
            result["history_archive"] = {
                "available": False,
                "reason": reason,
            }
            unavailable["history"] = reason
        except Exception:
            reason = "history_archive_failed"
            result["history_archive"] = {
                "available": False,
                "reason": reason,
            }
            failed["history"] = reason
        else:
            result["history_archive"] = {"available": True, **history}
            matched.insert(1, "history")
        result["coverage"] = {
            "consulted": consulted,
            "matched": matched,
            "unavailable": unavailable,
            "failed": failed,
            "complete": not unavailable and not failed,
        }
        return result

    async def search_project(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        *,
        scope: str = "auto",
        match: str = "",
        current: str = "",
        stage_id: str = "",
        context: int = 2,
        limit: int = 8,
        case_sensitive: bool = False,
        selector: str = "",
    ) -> Dict[str, Any]:
        """Search one or every registered authority without hiding omissions."""

        if scope == "stages":
            return self.search_stages(
                principal_id,
                project_id,
                query,
                match=match or "semantic",
                current=current,
                stage_id=stage_id,
                context=context,
                limit=limit,
                case_sensitive=case_sensitive,
            )
        if scope == "cases":
            return {
                "results": await self.search_cases(
                    principal_id,
                    project_id,
                    query,
                    match=match or "keyword",
                    limit=limit,
                )
            }
        base = await asyncio.to_thread(
            self.search_project_base,
            principal_id,
            project_id,
            query,
            scope=scope,
            current=current,
            stage_id=stage_id,
            context=context,
            limit=limit,
            case_sensitive=case_sensitive,
            selector=selector,
        )
        return await self.complete_project_search(
            base,
            principal_id,
            project_id,
            query,
            match=match,
            limit=limit,
        )

    def search_project_base(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        *,
        scope: str,
        current: str = "",
        stage_id: str = "",
        context: int = 2,
        limit: int = 8,
        case_sensitive: bool = False,
        selector: str = "",
    ) -> Dict[str, Any]:
        """Consult Stage and external owners before taking the Cognee lock."""

        if scope not in {"auto", "all", *EXTERNAL_LAYERS}:
            raise ValueError("search scope is unsupported")
        self.acl.grant(principal_id, project_id, "search")
        scopes = EXTERNAL_LAYERS if scope in {"auto", "all"} else (scope,)
        external = self._truth_plane.search(
            principal_id,
            project_id,
            query,
            scopes=scopes,
            limit=limit,
            selectors={"code": selector} if selector else None,
        )
        results: Dict[str, Any] = dict(external["results"])
        coverage = dict(external["coverage"])
        consulted = list(coverage["consulted"])
        matched = list(coverage["matched"])
        unavailable = dict(coverage["unavailable"])
        failed = dict(coverage["failed"])

        if scope in {"auto", "all"}:
            consulted.insert(0, "current")
            try:
                stages = self.search_stages(
                    principal_id,
                    project_id,
                    query,
                    match="semantic",
                    current=current,
                    stage_id=stage_id,
                    context=context,
                    limit=limit,
                    case_sensitive=case_sensitive,
                )
            except Exception:
                stages = {"results": []}
                failed["current"] = "current_search_failed"
            results["current"] = stages.get("results", [])
            if results["current"]:
                matched.insert(0, "current")

        return {
            "ok": not failed,
            "project_id": project_id,
            "query": query,
            "scope": scope,
            "results": results,
            "coverage": {
                "consulted": consulted,
                "matched": matched,
                "unavailable": unavailable,
                "failed": failed,
                "complete": not unavailable and not failed,
            },
        }

    async def complete_project_search(
        self,
        base: Mapping[str, Any],
        principal_id: str,
        project_id: str,
        query: str,
        *,
        match: str = "",
        limit: int = 8,
    ) -> Dict[str, Any]:
        """Add only the serialized Cognee history portion to a prepared search."""

        result = {
            **dict(base),
            "results": dict(base["results"]),
            "coverage": {
                key: (dict(value) if isinstance(value, dict) else list(value))
                for key, value in dict(base["coverage"]).items()
                if key != "complete"
            },
        }
        coverage = result["coverage"]
        consulted = coverage["consulted"]
        matched = coverage["matched"]
        unavailable = coverage["unavailable"]
        failed = coverage["failed"]
        if result["scope"] in {"auto", "all"}:
            consulted.insert(1, "history")
            try:
                cases = await self.search_cases(
                    principal_id,
                    project_id,
                    query,
                    match=match or "keyword",
                    limit=limit,
                )
            except CogneeCapabilityUnavailable:
                cases = []
                unavailable["history"] = "history_capability_unavailable"
            except CogneeUnavailable:
                cases = []
                unavailable["history"] = "history_archive_unavailable"
            except Exception:
                cases = []
                failed["history"] = "history_search_failed"
            result["results"]["history"] = cases
            if cases:
                matched.insert(1, "history")
        coverage["complete"] = not unavailable and not failed
        result["ok"] = not failed
        return result

    def get_resource(
        self,
        principal_id: str,
        project_id: str,
        reference: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """Resolve one exact donor-owned object through its immutable StableRef."""

        self.acl.grant(principal_id, project_id, "get")
        return self._truth_plane.get(
            principal_id, project_id, StableRef.from_dict(reference)
        )


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

    def update_authority(
        self,
        principal_id: str,
        project_id: str,
        layer: str,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        expected_revision: str,
    ) -> Dict[str, Any]:
        """Route a typed write without moving authority into this front."""

        self.acl.grant(principal_id, project_id, "update")
        if not isinstance(arguments, dict):
            raise ValueError("authority update arguments must be an object")
        return self._truth_plane.update(
            principal_id,
            project_id,
            layer,
            operation,
            arguments,
            expected_revision=expected_revision,
        )

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
