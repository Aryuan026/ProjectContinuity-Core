"""Federate donor-owned truth without merging their authorities or tool surfaces."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Dict, Mapping, Optional, Protocol, Sequence

from .auth import authenticate
from .config import Config
from .authority_layers import (
    AuthorityLayerError,
    AuthorityLayerUnavailable,
    GitHubDeliveryLayer,
    OpenSpecLayer,
    TeamAILayer,
)
from .evidence import StableRef
from .graph_router import (
    GraphQueryError,
    GraphQueryRouter,
    GraphRouterError,
)
from .graph_controller import GraphControllerError, GraphSnapshotController
from .github_resolver import GitHubAuthorityResolver, GitHubResolverUnavailable
from .truth_bindings import load_truth_bindings


EXTERNAL_LAYERS = ("code", "decisions", "collaboration", "delivery")
SEARCH_SCOPES = frozenset(("auto", "all", "stages", "cases") + EXTERNAL_LAYERS)


class TruthPlaneError(RuntimeError):
    """One integrated truth-plane request could not be completed safely."""


class LayerUnavailable(TruthPlaneError):
    """A registered authority layer is not physically available."""


class LayerAdapter(Protocol):
    authority: str
    layer: str

    def status(self, principal_id: str, project_id: str) -> Mapping[str, Any]: ...

    def search(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        *,
        limit: int,
        selector: str = "",
    ) -> Sequence[Mapping[str, Any]]: ...

    def get(
        self, principal_id: str, project_id: str, reference: StableRef
    ) -> Mapping[str, Any]: ...

    def update(
        self,
        principal_id: str,
        project_id: str,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        expected_revision: str,
    ) -> Mapping[str, Any]: ...



@dataclass(frozen=True)
class LayerCoverage:
    consulted: tuple[str, ...]
    matched: tuple[str, ...]
    unavailable: tuple[tuple[str, str], ...]
    failed: tuple[tuple[str, str], ...]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "consulted": list(self.consulted),
            "matched": list(self.matched),
            "unavailable": dict(self.unavailable),
            "failed": dict(self.failed),
            "complete": not self.unavailable and not self.failed,
        }


class IntegratedTruthPlane:
    """Route four external authority layers behind the existing five tools."""

    def __init__(self, adapters: Sequence[LayerAdapter] = ()) -> None:
        by_layer: Dict[str, LayerAdapter] = {}
        by_authority: Dict[str, LayerAdapter] = {}
        for adapter in adapters:
            if adapter.layer not in EXTERNAL_LAYERS:
                raise ValueError("adapter layer is outside the integrated truth plane")
            if adapter.layer in by_layer or adapter.authority in by_authority:
                raise ValueError("truth-plane adapters must have unique layers and authorities")
            by_layer[adapter.layer] = adapter
            by_authority[adapter.authority] = adapter
        self._by_layer = by_layer
        self._by_authority = by_authority

    def list_layers(self, principal_id: str, project_id: str) -> Dict[str, Any]:
        layers: Dict[str, Any] = {}
        coverage = _CoverageBuilder()
        for layer in EXTERNAL_LAYERS:
            adapter = self._by_layer.get(layer)
            if adapter is None:
                coverage.unavailable(layer, "not_configured")
                layers[layer] = {"available": False, "reason": "not_configured"}
                continue
            coverage.consult(layer)
            try:
                status = dict(adapter.status(principal_id, project_id))
            except (LayerUnavailable, AuthorityLayerUnavailable) as exc:
                reason = _public_reason(exc, "unavailable")
                coverage.unavailable(layer, reason)
                layers[layer] = {"available": False, "reason": reason}
            except Exception as exc:
                reason = _public_reason(exc, "failed")
                coverage.fail(layer, reason)
                layers[layer] = {"available": False, "reason": reason}
            else:
                layers[layer] = {"available": True, **status}
                coverage.match(layer)
        return {"coverage": coverage.freeze().as_dict(), "layers": layers}

    def search(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        *,
        scopes: Sequence[str],
        limit: int,
        selectors: Optional[Mapping[str, str]] = None,
    ) -> Dict[str, Any]:
        requested = _external_scopes(scopes)
        results: Dict[str, Any] = {}
        coverage = _CoverageBuilder()
        for layer in requested:
            adapter = self._by_layer.get(layer)
            if adapter is None:
                coverage.unavailable(layer, "not_configured")
                results[layer] = []
                continue
            coverage.consult(layer)
            try:
                rows = [
                    dict(row)
                    for row in adapter.search(
                        principal_id,
                        project_id,
                        query,
                        limit=limit,
                        selector=(selectors or {}).get(layer, ""),
                    )
                ]
            except (LayerUnavailable, AuthorityLayerUnavailable) as exc:
                coverage.unavailable(layer, _public_reason(exc, "unavailable"))
                rows = []
            except Exception as exc:
                coverage.fail(layer, _public_reason(exc, "failed"))
                rows = []
            if rows:
                coverage.match(layer)
            results[layer] = rows
        return {"coverage": coverage.freeze().as_dict(), "results": results}

    def get(
        self,
        principal_id: str,
        project_id: str,
        reference: StableRef,
    ) -> Dict[str, Any]:
        adapter = self._by_authority.get(reference.authority)
        if adapter is None:
            raise LayerUnavailable("authority_not_configured")
        try:
            result = dict(adapter.get(principal_id, project_id, reference))
        except (LayerUnavailable, AuthorityLayerUnavailable) as exc:
            raise LayerUnavailable(_public_reason(exc, "unavailable")) from exc
        except Exception as exc:
            raise TruthPlaneError(_public_reason(exc, "authority_failed")) from exc
        return {
            "coverage": {
                "consulted": [adapter.layer],
                "matched": [adapter.layer],
                "unavailable": {},
                "failed": {},
                "complete": True,
            },
            "layer": adapter.layer,
            "result": result,
        }

    def update(
        self,
        principal_id: str,
        project_id: str,
        layer: str,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        expected_revision: str,
    ) -> Dict[str, Any]:
        """Route one explicit write to the registered authority owner."""

        if layer not in EXTERNAL_LAYERS:
            raise ValueError("authority update layer is unsupported")
        adapter = self._by_layer.get(layer)
        if adapter is None:
            raise LayerUnavailable("authority_not_configured")
        try:
            result = dict(
                adapter.update(
                    principal_id,
                    project_id,
                    operation,
                    arguments,
                    expected_revision=expected_revision,
                )
            )
        except (LayerUnavailable, AuthorityLayerUnavailable) as exc:
            raise LayerUnavailable(_public_reason(exc, "unavailable")) from exc
        except Exception as exc:
            raise TruthPlaneError(_public_reason(exc, "authority_update_failed")) from exc
        return {"layer": layer, "result": result}



class UnavailableLayerAdapter:
    """Keep an expected layer visible when its runtime or binding is absent."""

    def __init__(self, authority: str, layer: str, reason: str) -> None:
        self.authority = authority
        self.layer = layer
        self.reason = reason

    def status(self, principal_id: str, project_id: str) -> Mapping[str, Any]:
        del principal_id, project_id
        raise LayerUnavailable(self.reason)

    def search(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        *,
        limit: int,
        selector: str = "",
    ) -> Sequence[Mapping[str, Any]]:
        del principal_id, project_id, query, limit, selector
        raise LayerUnavailable(self.reason)

    def get(
        self, principal_id: str, project_id: str, reference: StableRef
    ) -> Mapping[str, Any]:
        del principal_id, project_id, reference
        raise LayerUnavailable(self.reason)

    def update(
        self,
        principal_id: str,
        project_id: str,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        expected_revision: str,
    ) -> Mapping[str, Any]:
        del principal_id, project_id, operation, arguments, expected_revision
        raise LayerUnavailable(self.reason)



class ProjectMappedLayer:
    """Expose one authority while retaining independent per-project donor bindings."""

    def __init__(
        self,
        authority: str,
        layer: str,
        adapters: Mapping[str, LayerAdapter],
        *,
        missing_reason: str,
    ) -> None:
        self.authority = authority
        self.layer = layer
        self._adapters = dict(adapters)
        self._missing_reason = missing_reason

    def status(self, principal_id: str, project_id: str) -> Mapping[str, Any]:
        return self._adapter(project_id).status(principal_id, project_id)

    def search(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        *,
        limit: int,
        selector: str = "",
    ) -> Sequence[Mapping[str, Any]]:
        return self._adapter(project_id).search(
            principal_id, project_id, query, limit=limit, selector=selector
        )

    def get(
        self, principal_id: str, project_id: str, reference: StableRef
    ) -> Mapping[str, Any]:
        return self._adapter(project_id).get(principal_id, project_id, reference)

    def update(
        self,
        principal_id: str,
        project_id: str,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        expected_revision: str,
    ) -> Mapping[str, Any]:
        return self._adapter(project_id).update(
            principal_id,
            project_id,
            operation,
            arguments,
            expected_revision=expected_revision,
        )

    def _adapter(self, project_id: str) -> LayerAdapter:
        adapter = self._adapters.get(project_id)
        if adapter is None:
            raise LayerUnavailable(self._missing_reason)
        return adapter


class GraphifyLayer:
    """Production adapter over the existing F1 Graphify query router."""

    authority = "graphify"
    layer = "code"

    def __init__(self, config: Config, graphify_executable: Path) -> None:
        try:
            self.router = GraphQueryRouter(
                config, graphify_executable, timeout_seconds=15
            )
            self.controller = GraphSnapshotController(config, graphify_executable)
        except (GraphQueryError, GraphControllerError) as exc:
            raise LayerUnavailable("graphify_runtime_unavailable") from exc

    def status(self, principal_id: str, project_id: str) -> Mapping[str, Any]:
        del principal_id
        pointers: Dict[str, Any] = {}
        for selector in ("current_canonical", "working_overlay"):
            try:
                artifact = self.router.registry.resolve(
                    project_id, selector=selector
                )
            except GraphRouterError:
                pointers[selector] = None
            else:
                pointers[selector] = artifact.as_dict()
        if not any(pointers.values()):
            raise LayerUnavailable("graph_artifact_unavailable")
        primary = pointers["current_canonical"] or pointers["working_overlay"]
        return {
            "current": primary["stable_ref"],
            "current_canonical": pointers["current_canonical"],
            "working_overlay": pointers["working_overlay"],
        }

    def search(
        self,
        principal_id: str,
        project_id: str,
        query: str,
        *,
        limit: int,
        selector: str = "",
    ) -> Sequence[Mapping[str, Any]]:
        del limit
        selected = selector or "current_canonical"
        if selected not in {"current_canonical", "working_overlay"}:
            raise TruthPlaneError("graph_selector_unsupported")
        try:
            return [
                self.router.query(
                    principal_id=principal_id,
                    project_id=project_id,
                    question=query,
                    selector=selected,
                )
            ]
        except GraphRouterError as exc:
            raise LayerUnavailable("graph_query_unavailable") from exc

    def get(
        self, principal_id: str, project_id: str, reference: StableRef
    ) -> Mapping[str, Any]:
        del principal_id
        prefix = "graph:%s:" % project_id
        if not reference.object_id.startswith(prefix):
            raise TruthPlaneError("graph_reference_project_mismatch")
        snapshot_id = reference.object_id[len(prefix) :]
        try:
            artifact = self.router.registry.resolve(
                project_id, snapshot_id=snapshot_id
            )
        except GraphRouterError as exc:
            raise LayerUnavailable("graph_snapshot_unavailable") from exc
        if artifact.stable_ref != reference:
            raise TruthPlaneError("graph_reference_changed")
        return artifact.as_dict()

    def update(
        self,
        principal_id: str,
        project_id: str,
        operation: str,
        arguments: Mapping[str, Any],
        *,
        expected_revision: str,
    ) -> Mapping[str, Any]:
        if operation not in {"register_committed", "register_overlay"}:
            raise TruthPlaneError("graph_update_operation_unsupported")
        try:
            method = (
                self.controller.register_committed
                if operation == "register_committed"
                else self.controller.register_overlay
            )
            return method(
                project_id,
                arguments,
                actor=authenticate(self.controller.config, principal_id).actor,
                expected_revision=expected_revision,
            )
        except GraphControllerError as exc:
            raise TruthPlaneError(str(exc)) from exc



def build_installed_truth_plane(config: Config) -> IntegratedTruthPlane:
    """Discover only release-owned adapters; absence stays visible in coverage."""

    adapters: list[LayerAdapter] = []
    graphify = Path(sys.executable).with_name("graphify")
    if graphify.is_file() and not graphify.is_symlink():
        try:
            adapters.append(GraphifyLayer(config, graphify))
        except LayerUnavailable:
            adapters.append(
                UnavailableLayerAdapter(
                    "graphify", "code", "graphify_runtime_unavailable"
                )
            )
    else:
        adapters.append(
            UnavailableLayerAdapter("graphify", "code", "graphify_runtime_absent")
        )

    bindings = load_truth_bindings(config)
    release_root = Path(sys.prefix).resolve(strict=False).parent
    openspec_executable = (
        release_root
        / "vendor/openspec-runtime/node_modules/@fission-ai/openspec/bin/openspec.js"
    )
    node = _configured_node()
    openspec_projects: Dict[str, LayerAdapter] = {}
    if (
        node is not None
        and openspec_executable.is_file()
        and not openspec_executable.is_symlink()
    ):
        for project_id in bindings.project_ids():
            binding = bindings.project(project_id).openspec
            if binding is not None:
                try:
                    openspec_projects[project_id] = OpenSpecLayer(
                        config, binding, openspec_executable, node
                    )
                except AuthorityLayerError:
                    openspec_projects[project_id] = UnavailableLayerAdapter(
                        "openspec", "decisions", "openspec_runtime_invalid"
                    )
    adapters.append(
        ProjectMappedLayer(
            "openspec",
            "decisions",
            openspec_projects,
            missing_reason=(
                "openspec_binding_absent"
                if node is not None and openspec_executable.is_file()
                else "openspec_runtime_absent"
            ),
        )
    )

    teamai_entrypoint = (
        release_root / "vendor/teamai-runtime/node_modules/teamai-cli/dist/index.js"
    )
    teamai_literal_recall = (
        release_root
        / "vendor/teamai-runtime/project-continuity-literal-recall.mjs"
    )
    teamai_projects: Dict[str, LayerAdapter] = {}
    if (
        node is not None
        and teamai_entrypoint.is_file()
        and teamai_literal_recall.is_file()
    ):
        for project_id in bindings.project_ids():
            binding = bindings.project(project_id).teamai
            if binding is not None:
                try:
                    teamai_projects[project_id] = TeamAILayer(
                        config,
                        binding,
                        node,
                        teamai_entrypoint,
                        teamai_literal_recall,
                    )
                except AuthorityLayerError:
                    teamai_projects[project_id] = UnavailableLayerAdapter(
                        "teamai", "collaboration", "teamai_runtime_invalid"
                    )
    adapters.append(
        ProjectMappedLayer(
            "teamai",
            "collaboration",
            teamai_projects,
            missing_reason=(
                "teamai_binding_absent"
                if (
                    node is not None
                    and teamai_entrypoint.is_file()
                    and teamai_literal_recall.is_file()
                )
                else "teamai_runtime_absent"
            ),
        )
    )
    try:
        github = GitHubAuthorityResolver.from_environment()
    except GitHubResolverUnavailable as exc:
        adapters.append(UnavailableLayerAdapter("github", "delivery", str(exc)))
    else:
        adapters.append(GitHubDeliveryLayer(config, github))
    return IntegratedTruthPlane(tuple(adapters))


def _configured_node() -> Path | None:
    configured = os.environ.get("PROJECT_CONTINUITY_NODE_BIN")
    if configured:
        path = Path(configured)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            return None
        return path
    discovered = shutil.which("node")
    return Path(discovered).resolve() if discovered else None


class _CoverageBuilder:
    def __init__(self) -> None:
        self._consulted: list[str] = []
        self._matched: list[str] = []
        self._unavailable: Dict[str, str] = {}
        self._failed: Dict[str, str] = {}

    def consult(self, layer: str) -> None:
        if layer not in self._consulted:
            self._consulted.append(layer)

    def match(self, layer: str) -> None:
        if layer not in self._matched:
            self._matched.append(layer)

    def unavailable(self, layer: str, reason: str) -> None:
        self._unavailable[layer] = reason

    def fail(self, layer: str, reason: str) -> None:
        self._failed[layer] = reason

    def freeze(self) -> LayerCoverage:
        return LayerCoverage(
            consulted=tuple(self._consulted),
            matched=tuple(self._matched),
            unavailable=tuple(self._unavailable.items()),
            failed=tuple(self._failed.items()),
        )


def _external_scopes(scopes: Sequence[str]) -> tuple[str, ...]:
    if isinstance(scopes, (str, bytes)):
        raise ValueError("scopes must be a sequence")
    selected = []
    for scope in scopes:
        if scope not in EXTERNAL_LAYERS:
            raise ValueError("external search scope is unsupported")
        if scope not in selected:
            selected.append(scope)
    return tuple(selected)


def _public_reason(error: Exception, fallback: str) -> str:
    value = str(error)
    if value and len(value) <= 120 and all(
        character.isalnum() or character in "._-" for character in value
    ):
        return value
    return fallback
