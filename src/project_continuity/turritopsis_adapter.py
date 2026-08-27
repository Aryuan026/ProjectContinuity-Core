"""Thin adapter over the exact Turritopsis donor implementation."""

from dataclasses import dataclass
import importlib.metadata
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, Optional, Protocol

from .evidence import is_excluded_path, sanitize_evidence


TURRITOPSIS_COMMIT = "fd94c75f362260abb81ddd02296f14dc22350e73"
TURRITOPSIS_VERSION = "0.2.0"
MAX_STAGE_BODY = 100_000
MAX_RESULT_STRING = MAX_STAGE_BODY + 10_000
MAX_SCAN_SOURCE_BYTES = 300_000


class TurritopsisAdapterError(RuntimeError):
    """The donor boundary rejected or could not complete an operation."""


class TurritopsisUnavailable(TurritopsisAdapterError):
    """The selected project Store or exact donor runtime is unavailable."""


class StoreBoundaryError(TurritopsisAdapterError):
    """A managed per-project Store path escaped or aliased its fixed location."""


class EvidenceRejected(ValueError):
    """Stage content exceeded the boundary or contained credential-shaped text."""


class TurritopsisService(Protocol):
    def list_stages(self, current: str = "") -> Dict[str, Any]: ...

    def search_stages(
        self,
        query: str,
        match: str = "semantic",
        current: str = "",
        stage_id: str = "",
        context: int = 2,
        limit: int = 8,
        case_sensitive: bool = False,
    ) -> Dict[str, Any]: ...

    def get_stage(self, stage_id: str) -> Dict[str, Any]: ...

    def update_stage(
        self,
        stage_id: str,
        body: str,
        mode: str = "replace",
        expected_revision: str = "",
        actor: str = "agent",
    ) -> Dict[str, Any]: ...


ServiceFactory = Callable[[Path], TurritopsisService]


def project_store_path(data_root: Path, project_id: str) -> Path:
    """Return the sole canonical Store path for one opaque project id."""

    return (
        Path(data_root)
        / "projects"
        / project_id
        / "turritopsis"
        / "stages.json"
    )


def _assert_runtime_version() -> None:
    try:
        installed = importlib.metadata.version("turritopsis")
    except importlib.metadata.PackageNotFoundError as exc:
        raise TurritopsisUnavailable(
            "exact Turritopsis runtime is not installed"
        ) from exc
    if installed != TURRITOPSIS_VERSION:
        raise TurritopsisUnavailable(
            "Turritopsis runtime version mismatch: expected %s" % TURRITOPSIS_VERSION
        )


def _native_service(data_path: Path) -> TurritopsisService:
    _assert_runtime_version()
    try:
        from turritopsis.api import Turritopsis
    except ImportError as exc:
        raise TurritopsisUnavailable(
            "exact Turritopsis runtime is unavailable"
        ) from exc
    return Turritopsis(data_path)


def _safe_result(value: Any) -> Any:
    return sanitize_evidence(
        value,
        max_depth=10,
        max_items=250,
        max_string=MAX_RESULT_STRING,
    )


_PATH_LIST_KEYS = frozenset({"tree", "git_changes", "evidence_paths"})


def _prune_excluded_evidence(value: Any, parent_key: str = "") -> Any:
    if isinstance(value, dict):
        path = value.get("path")
        if isinstance(path, str) and is_excluded_path(path):
            return None
        result = {}
        for key, item in value.items():
            if isinstance(key, str) and is_excluded_path(key):
                continue
            safe = _prune_excluded_evidence(item, str(key))
            if safe is not None:
                result[key] = safe
        return result
    if isinstance(value, list):
        result = []
        for item in value:
            if (
                parent_key in _PATH_LIST_KEYS
                and isinstance(item, str)
                and is_excluded_path(item)
            ):
                continue
            safe = _prune_excluded_evidence(item, parent_key)
            if safe is not None:
                result.append(safe)
        return result
    return value


def _prune_structure_map(document: Any) -> Any:
    if not isinstance(document, dict) or not isinstance(
        document.get("structure_map"), str
    ):
        return document
    kept = []
    excluded_sections = 0
    keep_section = True
    for line in document["structure_map"].splitlines(keepends=True):
        if line and not line[0].isspace():
            keep_section = not is_excluded_path(line.strip())
            if not keep_section:
                excluded_sections += 1
        if keep_section:
            kept.append(line)
    result = dict(document)
    result["structure_map"] = "".join(kept)
    coverage = result.get("structure_coverage")
    if excluded_sections and isinstance(coverage, dict):
        coverage = dict(coverage)
        for key in ("files_mapped", "code_files_found"):
            value = coverage.get(key)
            if isinstance(value, int):
                coverage[key] = max(0, value - excluded_sections)
        coverage["files_excluded_by_boundary"] = excluded_sections
        coverage["chars"] = len(result["structure_map"])
        result["structure_coverage"] = coverage
    return result


def donor_evidence_preview(value: Any) -> Any:
    """Apply the shared exclude, bound, and secret boundary to donor evidence."""

    return _safe_result(
        _prune_excluded_evidence(_prune_structure_map(value))
    )


def _validate_stage_body(body: str) -> str:
    if not isinstance(body, str):
        raise TypeError("body must be a string")
    if len(body) > MAX_STAGE_BODY:
        raise EvidenceRejected("Stage body exceeds the bounded front limit")
    sanitized = sanitize_evidence(
        body,
        max_depth=1,
        max_items=1,
        max_string=MAX_STAGE_BODY + 1,
    )
    if sanitized != body:
        raise EvidenceRejected(
            "Stage body contains credential-shaped text; store a redacted reference"
        )
    return body


def _materialize_scan_projection(
    project_root: Path,
    projection_root: Path,
    *,
    max_files: int,
    include_file: Callable[[Path, Path], bool],
) -> None:
    """Give the donor a bounded, excluded, pre-sanitized project projection."""

    root = Path(project_root)
    if root.is_symlink() or not root.is_dir():
        raise TurritopsisUnavailable("scan project root must be a real directory")
    copied = 0
    for source in sorted(root.rglob("*")):
        if copied >= max_files:
            break
        relative = source.relative_to(root)
        if (
            source.is_symlink()
            or is_excluded_path(relative)
            or not source.is_file()
            or not include_file(source, root)
        ):
            continue
        target = projection_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as handle:
            payload = handle.read(MAX_SCAN_SOURCE_BYTES + 1)
        if b"\x00" in payload:
            target.touch()
        else:
            text = payload.decode("utf-8", errors="replace")
            target.write_text(
                sanitize_evidence(
                    text,
                    max_depth=1,
                    max_items=1,
                    max_string=MAX_SCAN_SOURCE_BYTES,
                ),
                encoding="utf-8",
            )
        copied += 1


@dataclass
class TurritopsisAdapter:
    """Delegate all cognition behavior to one exact, per-project donor Store."""

    project_id: str
    data_root: Path
    data_path: Path
    service_factory: ServiceFactory = _native_service

    def __post_init__(self) -> None:
        self.data_root = Path(self.data_root).resolve(strict=False)
        self.data_path = Path(self.data_path)
        expected = project_store_path(self.data_root, self.project_id)
        if self.data_path != expected:
            raise StoreBoundaryError(
                "project Store does not match its fixed managed location"
            )
        self._service: Optional[TurritopsisService] = None

    def list_stages(self, current: str = "") -> Dict[str, Any]:
        return self._call("list_stages", current)

    def search_stages(
        self,
        query: str,
        match: str = "semantic",
        current: str = "",
        stage_id: str = "",
        context: int = 2,
        limit: int = 8,
        case_sensitive: bool = False,
    ) -> Dict[str, Any]:
        return self._call(
            "search_stages",
            query,
            match,
            current,
            stage_id,
            context,
            limit,
            case_sensitive,
        )

    def get_stage(self, stage_id: str) -> Dict[str, Any]:
        return self._call("get_stage", stage_id)

    def update_stage(
        self,
        stage_id: str,
        body: str,
        *,
        expected_revision: str,
        actor: str,
        mode: str = "replace",
    ) -> Dict[str, Any]:
        if (
            not isinstance(expected_revision, str)
            or len(expected_revision) != 16
            or any(character not in "0123456789abcdef" for character in expected_revision)
        ):
            raise ValueError("expected_revision must be the exact 16-hex Stage revision")
        if not isinstance(mode, str) or mode.lower().strip() != "replace":
            raise ValueError("the current adapter supports replace mode only")
        body = _validate_stage_body(body)
        self._assert_write_boundary()
        return self._call(
            "update_stage",
            stage_id,
            body,
            "replace",
            expected_revision,
            actor,
        )

    def scan_preview(self, project_root: Path) -> Dict[str, Any]:
        """Run donor scanners over a pre-sanitized, excluded project projection."""

        self._assert_store_boundary()
        try:
            _assert_runtime_version()
            from turritopsis.init_scan import (
                MAX_TREE_FILES,
                _safe_file,
                scan_anomalies,
                scan_project,
            )

            root = Path(project_root)
            with TemporaryDirectory(prefix="project-continuity-scan-") as temporary:
                projection_root = Path(temporary) / (root.name or "project")
                projection_root.mkdir()
                _materialize_scan_projection(
                    root,
                    projection_root,
                    max_files=MAX_TREE_FILES,
                    include_file=_safe_file,
                )
                evidence = scan_project(projection_root)
                evidence["anomalies"] = scan_anomalies(projection_root)
            return donor_evidence_preview(evidence)
        except OSError as exc:
            raise TurritopsisUnavailable(
                "Turritopsis scan is unavailable for project: %s" % self.project_id
            ) from exc

    def maintenance_preview(
        self, max_age_days: int = 30, now: Any = None
    ) -> Dict[str, Any]:
        """Refuse custody-root evidence until an operator binds the real checkout."""

        raise TurritopsisUnavailable(
            "repository-aware maintenance is HOLD until an operator-approved "
            "project root is bound"
        )

    def _assert_write_boundary(self) -> None:
        self._assert_store_boundary()
        root = self.data_path.parent
        targets = (
            root / "backups",
            root / "changelog.jsonl",
            self.data_path.with_suffix(self.data_path.suffix + ".lock"),
        )
        for target in targets:
            current = root
            for part in target.relative_to(root).parts:
                current = current / part
                if current.is_symlink():
                    raise StoreBoundaryError(
                        "managed Turritopsis write path must not traverse a symlink"
                    )

    def _assert_store_boundary(self) -> None:
        expected = project_store_path(self.data_root, self.project_id)
        current = self.data_root
        for part in expected.relative_to(self.data_root).parts:
            current = current / part
            if current.is_symlink():
                raise StoreBoundaryError(
                    "managed project Store must not traverse a symlink"
                )
        try:
            expected.resolve(strict=False).relative_to(self.data_root)
        except ValueError as exc:
            raise StoreBoundaryError(
                "managed project Store escaped the data root"
            ) from exc

    def _get_service(self) -> TurritopsisService:
        self._assert_store_boundary()
        if self._service is None:
            self._service = self.service_factory(self.data_path)
        return self._service

    def _call(self, method: str, *args: Any) -> Any:
        try:
            service = self._get_service()
            result = getattr(service, method)(*args)
        except (OSError, json.JSONDecodeError) as exc:
            raise TurritopsisUnavailable(
                "Turritopsis Store is unavailable for project: %s" % self.project_id
            ) from exc
        return _safe_result(result)
