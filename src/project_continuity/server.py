"""Authenticated loopback HTTP entrypoint over the existing CognitionFront."""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import asdict, is_dataclass
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import stat
import threading
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Sequence, Tuple

from .auth import AuthorizationError
from .client import (
    FrontClientError,
    TOKEN_PATTERN,
    load_private_token_file,
    path_has_symlink,
)
from .cognee_adapter import CogneeAdapterError, CogneeCapabilityUnavailable
from .config import Config, ConfigError, load_config
from .evidence import StableRef
from .front import CognitionFront
from .promotion import PromotionError, PromotionValidationError
from .receipts import ReceiptError
from .runtime_environment import (
    RuntimeEnvironmentError,
    cognee_environment_for_config,
    validate_cognee_environment,
)
from .runtime_lock import RuntimeLockError, runtime_lifetime_lock
from .turritopsis_adapter import TurritopsisAdapterError
from .teamai_receipts import authority_request_digest
from .truth_plane import LayerUnavailable, TruthPlaneError


BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
CASE_SEARCH_TIMEOUT_SECONDS = 15
ARCHIVE_OPERATION_TIMEOUT_SECONDS = 60
INTEGRATED_HISTORY_TIMEOUT_SECONDS = 15
AUTHORITY_OPERATION_TIMEOUT_SECONDS = 60
_TOOLS = frozenset({"list", "search", "get", "update", "promote"})
_ARCHIVE_TIMEOUT_ERRORS = (
    FutureTimeoutError,
    TimeoutError,
    asyncio.TimeoutError,
)


class ServerConfigError(ValueError):
    """The runtime entrypoint cannot start with the supplied local inputs."""


class RequestError(ValueError):
    """One authenticated request does not match the frozen five-tool API."""


class StageRevisionConflict(RuntimeError):
    """A donor-native Stage CAS refusal reached the five-tool boundary."""


class ArchiveOperationBusy(RuntimeError):
    """The sole archive worker still owns the backend after a prior request."""

    def __init__(self, capability: str, operation_id: str | None = None) -> None:
        super().__init__("archive backend is still owned by an in-flight operation")
        self.capability = capability
        self.operation_id = operation_id


class ArchiveOperationTimeout(TimeoutError):
    """The request deadline expired while the archive operation continues."""

    def __init__(self, capability: str, operation_id: str | None = None) -> None:
        super().__init__("archive operation exceeded its request deadline")
        self.capability = capability
        self.operation_id = operation_id


class ArchiveBackendTimeout(TimeoutError):
    """The archive backend reached a terminal timeout for this request."""

    def __init__(self, capability: str, operation_id: str | None = None) -> None:
        super().__init__("archive backend returned a terminal timeout")
        self.capability = capability
        self.operation_id = operation_id


class _ArchiveRunner:
    """Own one persistent event loop and at most one active archive operation."""

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._active: Optional[Future[Any]] = None
        self._active_operation_id: str | None = None
        self._closed = False
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self._serve,
            name="project-continuity-archive",
            daemon=True,
        )
        try:
            self.thread.start()
        except BaseException:
            self.loop.close()
            raise

    def _serve(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        timeout: float,
        capability: str,
        operation_id: str | None = None,
    ) -> Any:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("ProjectContinuity archive loop is closed")
            if self._active is not None and self._active.done():
                self._active = None
                self._active_operation_id = None
            if self._active is not None:
                active_id = (
                    self._active_operation_id
                    if operation_id == self._active_operation_id
                    else None
                )
                raise ArchiveOperationBusy(capability, active_id)
            awaitable = operation()
            try:
                future = asyncio.run_coroutine_threadsafe(awaitable, self.loop)
            except BaseException:
                close = getattr(awaitable, "close", None)
                if callable(close):
                    close()
                raise
            self._active = future
            self._active_operation_id = operation_id
        # A Future may finish before registration, in which case
        # add_done_callback() invokes synchronously. Register outside the
        # non-reentrant state lock so an immediate result cannot self-deadlock.
        future.add_done_callback(self._release)

        try:
            return future.result(timeout=timeout)
        except _ARCHIVE_TIMEOUT_ERRORS:
            if future.done():
                try:
                    return future.result()
                except _ARCHIVE_TIMEOUT_ERRORS as exc:
                    raise ArchiveBackendTimeout(capability, operation_id) from exc
            raise ArchiveOperationTimeout(capability, operation_id) from None

    def _release(self, future: Future[Any]) -> None:
        with self._state_lock:
            if self._active is future:
                self._active = None
                self._active_operation_id = None

    def close(self) -> None:
        """Stop only after any retained archive worker reaches terminal state."""

        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            active = self._active
        if active is not None:
            try:
                active.result()
            except BaseException:
                pass

        async def shutdown() -> None:
            await self.loop.shutdown_asyncgens()
            await self.loop.shutdown_default_executor()

        future = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        try:
            future.result(timeout=5)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError("ProjectContinuity archive loop did not stop")
            self.loop.close()


def load_private_service_config(path: Path) -> Config:
    """Apply service-runtime ownership checks before using the pure TOML parser."""

    config_path = Path(path)
    if (
        not config_path.is_absolute()
        or config_path != Path(os.path.abspath(config_path))
        or config_path.is_symlink()
        or path_has_symlink(config_path)
        or not config_path.is_file()
    ):
        raise ServerConfigError("config must be an absolute real private file")
    parent = config_path.parent
    try:
        parent_stat = parent.stat()
        config_stat = config_path.stat()
    except OSError as exc:
        raise ServerConfigError("config ownership cannot be inspected") from exc
    if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_mode & 0o077:
        raise ServerConfigError("config parent must be owner-only")
    if not stat.S_ISREG(config_stat.st_mode) or config_stat.st_mode & 0o077:
        raise ServerConfigError("config file must be owner-only")
    if hasattr(os, "geteuid") and (
        parent_stat.st_uid != os.geteuid() or config_stat.st_uid != os.geteuid()
    ):
        raise ServerConfigError("config file and parent must have the service owner")
    return load_config(config_path)


def bind_cognee_environment(config: Config) -> Dict[str, str]:
    """Bind the reviewed front to the Cognee roots derived from operator config."""

    try:
        expected = validate_cognee_environment(
            cognee_environment_for_config(config),
            Path(__file__).resolve().parents[2],
        )
    except RuntimeEnvironmentError as exc:
        raise ServerConfigError("Cognee writable-root contract is unsafe") from exc
    mismatched = sorted(
        name
        for name, value in expected.items()
        if name in os.environ and os.environ[name] != value
    )
    if mismatched:
        raise ServerConfigError(
            "Cognee environment does not match operator config: %s"
            % ", ".join(mismatched)
        )
    os.environ.update(expected)
    return expected


class CredentialSet:
    """Map private bearer-token files to operator-configured principals."""

    def __init__(self, entries: Tuple[Tuple[str, str], ...]) -> None:
        self._entries = entries

    @classmethod
    def load(cls, config: Config, directory: Path) -> "CredentialSet":
        root = Path(directory)
        if (
            not root.is_absolute()
            or root != Path(os.path.abspath(root))
            or not root.is_dir()
            or path_has_symlink(root)
        ):
            raise ServerConfigError("credentials_dir must be a real private directory")
        root_stat = root.stat()
        if root_stat.st_mode & 0o077:
            raise ServerConfigError("credentials_dir must not be group/world accessible")
        if hasattr(os, "geteuid") and root_stat.st_uid != os.geteuid():
            raise ServerConfigError("credentials_dir has a different owner")

        entries = []
        seen_tokens = set()
        for principal in config.principals:
            token_path = root / (principal.principal_id + ".token")
            if not token_path.is_file() or token_path.is_symlink():
                raise ServerConfigError(
                    "credential file is absent or unsafe for principal: %s"
                    % principal.principal_id
                )
            token_stat = token_path.stat()
            if not stat.S_ISREG(token_stat.st_mode) or token_stat.st_mode & 0o077:
                raise ServerConfigError(
                    "credential file must be private for principal: %s"
                    % principal.principal_id
                )
            if hasattr(os, "geteuid") and token_stat.st_uid != os.geteuid():
                raise ServerConfigError(
                    "credential file has a different owner for principal: %s"
                    % principal.principal_id
                )
            try:
                token = load_private_token_file(token_path)
            except FrontClientError as exc:
                raise ServerConfigError(
                    "credential token is malformed for principal: %s"
                    % principal.principal_id
                ) from exc
            if token in seen_tokens:
                raise ServerConfigError("credential tokens must be unique")
            seen_tokens.add(token)
            entries.append((token, principal.principal_id))
        return cls(tuple(entries))

    def authenticate(self, header: Optional[str]) -> Optional[str]:
        if not isinstance(header, str) or not header.startswith("Bearer "):
            return None
        candidate = header[7:]
        if not TOKEN_PATTERN.fullmatch(candidate):
            return None
        matched = None
        for token, principal_id in self._entries:
            if hmac.compare_digest(candidate, token):
                matched = principal_id
        return matched


def _is_stage_revision_conflict(value: Any) -> bool:
    if not isinstance(value, Mapping) or set(value) != {
        "ok",
        "conflict",
        "requested_revision",
        "current_revision",
        "current_stage",
    }:
        return False
    current_stage = value["current_stage"]
    return (
        value["ok"] is False
        and value["conflict"] is True
        and isinstance(value["requested_revision"], str)
        and isinstance(value["current_revision"], str)
        and value["requested_revision"] != value["current_revision"]
        and isinstance(current_stage, Mapping)
        and current_stage.get("revision") == value["current_revision"]
    )


class FrontApplication:
    """Validate transport input, then delegate to the existing front."""

    def __init__(self, front: CognitionFront, credentials: CredentialSet) -> None:
        self.front = front
        self.credentials = credentials
        self._archive_runner = _ArchiveRunner()
        self._authority_runner = _ArchiveRunner()

    def close(self) -> None:
        self._archive_runner.close()
        self._authority_runner.close()

    def invoke(self, principal_id: str, request: Mapping[str, Any]) -> Any:
        _require_exact_keys(request, {"tool", "project_id", "arguments"}, "request")
        tool = request["tool"]
        project_id = request["project_id"]
        arguments = request["arguments"]
        if not isinstance(tool, str) or tool not in _TOOLS:
            raise RequestError("tool must be one of the five frozen tools")
        if not isinstance(project_id, str) or not project_id:
            raise RequestError("project_id must be a non-empty string")
        if not isinstance(arguments, dict):
            raise RequestError("arguments must be an object")

        if tool == "list":
            _require_exact_keys(arguments, set(), "list arguments", optional={"current"})
            base = self.front.list_project(
                principal_id, project_id, current=arguments.get("current", "")
            )
            try:
                return self._run_archive(
                    lambda: self.front.list_project_complete(
                        principal_id,
                        project_id,
                        current=arguments.get("current", ""),
                        base=base,
                    ),
                    timeout=INTEGRATED_HISTORY_TIMEOUT_SECONDS,
                )
            except ArchiveOperationBusy:
                return _list_with_archive_gap(base, "history_archive_busy")
            except ArchiveOperationTimeout:
                return _list_with_archive_gap(base, "history_archive_timeout")
        if tool == "search":
            return self._search(principal_id, project_id, arguments)
        if tool == "get":
            return self._get(principal_id, project_id, arguments)
        if tool == "update":
            target = arguments.get("target", "current")
            if target == "current":
                _require_exact_keys(
                    arguments,
                    {"stage_id", "body", "expected_revision"},
                    "Stage update arguments",
                    optional={"mode", "target"},
                )
                result = self.front.update_stage(
                    principal_id,
                    project_id,
                    arguments["stage_id"],
                    arguments["body"],
                    expected_revision=arguments["expected_revision"],
                    mode=arguments.get("mode", "replace"),
                )
                if _is_stage_revision_conflict(result):
                    raise StageRevisionConflict("stage_revision_conflict")
                return result
            _require_exact_keys(
                arguments,
                {"target", "operation", "parameters", "expected_revision"},
                "authority update arguments",
            )
            operation_id = "authority:" + authority_request_digest(
                principal_id=principal_id,
                project_id=project_id,
                target=target,
                operation=arguments["operation"],
                parameters=arguments["parameters"],
                expected_revision=arguments["expected_revision"],
            ).removeprefix("sha256:")
            return self._authority_runner.run(
                lambda: asyncio.to_thread(
                    self.front.update_authority,
                    principal_id,
                    project_id,
                    target,
                    arguments["operation"],
                    arguments["parameters"],
                    expected_revision=arguments["expected_revision"],
                ),
                timeout=AUTHORITY_OPERATION_TIMEOUT_SECONDS,
                capability="authority_write",
                operation_id=operation_id,
            )
        return self._run_archive(
            lambda: self._promote(principal_id, project_id, arguments),
            timeout=ARCHIVE_OPERATION_TIMEOUT_SECONDS,
        )

    def _search(
        self, principal_id: str, project_id: str, arguments: Mapping[str, Any]
    ) -> Any:
        _require_exact_keys(
            arguments,
            {"query"},
            "search arguments",
            optional={
                "scope",
                "match",
                "current",
                "stage_id",
                "context",
                "limit",
                "case_sensitive",
                "selector",
            },
        )
        scope = arguments.get("scope", "auto")
        if scope == "cases":
            _require_exact_keys(
                arguments,
                {"query"},
                "case search arguments",
                optional={"scope", "match", "limit"},
            )
            return self._run_archive(
                lambda: self.front.search_cases(
                    principal_id,
                    project_id,
                    arguments["query"],
                    match=arguments.get("match", "keyword"),
                    limit=arguments.get("limit", 8),
                ),
                timeout=CASE_SEARCH_TIMEOUT_SECONDS,
                capability="case_search",
            )
        if scope == "stages":
            return self.front.search_stages(
                principal_id,
                project_id,
                arguments["query"],
                match=arguments.get("match", "semantic"),
                current=arguments.get("current", ""),
                stage_id=arguments.get("stage_id", ""),
                context=arguments.get("context", 2),
                limit=arguments.get("limit", 8),
                case_sensitive=arguments.get("case_sensitive", False),
            )
        base = self.front.search_project_base(
            principal_id,
            project_id,
            arguments["query"],
            scope=scope,
            current=arguments.get("current", ""),
            stage_id=arguments.get("stage_id", ""),
            context=arguments.get("context", 2),
            limit=arguments.get("limit", 8),
            case_sensitive=arguments.get("case_sensitive", False),
            selector=arguments.get("selector", ""),
        )
        if scope not in {"auto", "all"}:
            return base
        try:
            return self._run_archive(
                lambda: self.front.complete_project_search(
                    base,
                    principal_id,
                    project_id,
                    arguments["query"],
                    match=arguments.get("match", ""),
                    limit=arguments.get("limit", 8),
                ),
                timeout=CASE_SEARCH_TIMEOUT_SECONDS,
                capability="case_search",
            )
        except ArchiveOperationBusy:
            return _search_with_archive_gap(base, "history_archive_busy")
        except ArchiveOperationTimeout:
            return _search_with_archive_gap(base, "history_archive_timeout")

    def _get(
        self, principal_id: str, project_id: str, arguments: Mapping[str, Any]
    ) -> Any:
        _require_exact_keys(
            arguments,
            set(),
            "get arguments",
            optional={"stage_id", "promotion_id", "resource_ref"},
        )
        stage_id = arguments.get("stage_id")
        promotion_id = arguments.get("promotion_id")
        resource_ref = arguments.get("resource_ref")
        if sum(bool(value) for value in (stage_id, promotion_id, resource_ref)) != 1:
            raise RequestError(
                "get requires exactly one stage_id, promotion_id, or resource_ref"
            )
        if stage_id:
            return self.front.get_stage(principal_id, project_id, stage_id)
        if resource_ref:
            return self.front.get_resource(principal_id, project_id, resource_ref)
        return self._run_archive(
            lambda: self.front.get_case(principal_id, project_id, promotion_id),
            timeout=ARCHIVE_OPERATION_TIMEOUT_SECONDS,
        )

    def _run_archive(
        self,
        operation: Callable[[], Awaitable[Any]],
        *,
        timeout: float,
        capability: str = "case_archive",
    ) -> Any:
        return self._archive_runner.run(
            operation,
            timeout=timeout,
            capability=capability,
        )

    async def _promote(
        self, principal_id: str, project_id: str, arguments: Mapping[str, Any]
    ) -> Any:
        _require_exact_keys(
            arguments,
            {
                "stage_id",
                "source_revision",
                "idempotency_key",
                "provenance",
                "review_authority",
            },
            "promote arguments",
            optional={
                "promotion_kind",
                "schema_version",
                "corrects",
                "supersedes",
            },
        )
        provenance = arguments["provenance"]
        if not isinstance(provenance, list):
            raise RequestError("provenance must be a list")
        return await self.front.promote_stage(
            principal_id,
            project_id,
            arguments["stage_id"],
            source_revision=arguments["source_revision"],
            idempotency_key=arguments["idempotency_key"],
            provenance=[StableRef.from_dict(item) for item in provenance],
            review_authority=StableRef.from_dict(arguments["review_authority"]),
            promotion_kind=arguments.get("promotion_kind", "engineering_case"),
            schema_version=arguments.get("schema_version", 1),
            corrects=arguments.get("corrects", ()),
            supersedes=arguments.get("supersedes", ()),
        )


def create_server(
    config: Config,
    credentials_dir: Path,
    *,
    host: str = BIND_HOST,
    port: int = DEFAULT_PORT,
    front: Optional[CognitionFront] = None,
) -> ThreadingHTTPServer:
    if host != BIND_HOST:
        raise ServerConfigError("the front must bind exactly 127.0.0.1")
    if type(port) is not int or port < 0 or port > 65535:
        raise ServerConfigError("port must be between 0 and 65535")
    if port == 0 and front is None:
        raise ServerConfigError("ephemeral port is test-only")
    application = FrontApplication(
        front or CognitionFront(config), CredentialSet.load(config, credentials_dir)
    )

    class Handler(_RequestHandler):
        app = application

    try:
        return _LoopbackServer((host, port), Handler)
    except Exception:
        application.close()
        raise


class _LoopbackServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self) -> None:
        try:
            self.RequestHandlerClass.app.close()
        finally:
            super().server_close()


class _RequestHandler(BaseHTTPRequestHandler):
    app: FrontApplication
    server_version = "ProjectContinuity"
    sys_version = ""

    def do_GET(self) -> None:
        if self.path != "/health":
            self._send(404, {"ok": False, "error": "not_found"})
            return
        self._send(
            200,
            {
                "ok": True,
                "status": "front_ready",
                "transport": "loopback_http",
                "dependencies": "not_probed",
            },
        )

    def do_POST(self) -> None:
        if self.path != "/v1/invoke":
            self._send(404, {"ok": False, "error": "not_found"})
            return
        principal_id = self.app.credentials.authenticate(
            self.headers.get("Authorization")
        )
        if principal_id is None:
            self._send(
                401,
                {"ok": False, "error": "unauthorized"},
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return
        try:
            request = self._read_request()
            result = self.app.invoke(principal_id, request)
        except RequestError as exc:
            self._send(400, {"ok": False, "error": "invalid_request", "detail": str(exc)})
            return
        except (AuthorizationError,) as exc:
            self._send(403, {"ok": False, "error": "forbidden", "detail": str(exc)})
            return
        except (PromotionValidationError, KeyError, ValueError, TypeError) as exc:
            self._send(422, {"ok": False, "error": "invalid_operation", "detail": str(exc)})
            return
        except StageRevisionConflict as exc:
            self._send(409, {"ok": False, "error": "operation_conflict", "detail": str(exc)})
            return
        except (ReceiptError, PromotionError) as exc:
            self._send(409, {"ok": False, "error": "operation_conflict", "detail": str(exc)})
            return
        except LayerUnavailable as exc:
            self._send(
                503,
                {"ok": False, "error": "authority_unavailable", "detail": str(exc)},
            )
            return
        except TruthPlaneError as exc:
            detail = str(exc)
            if "conflict" in detail:
                self._send(
                    409,
                    {"ok": False, "error": "operation_conflict", "detail": detail},
                )
            elif detail.endswith(("_failed", "_unavailable")):
                self._send(
                    503,
                    {"ok": False, "error": "authority_unavailable", "detail": detail},
                )
            else:
                self._send(
                    422,
                    {"ok": False, "error": "invalid_operation", "detail": detail},
                )
            return
        except CogneeCapabilityUnavailable:
            self._send(
                503,
                {
                    "ok": False,
                    "error": "capability_unavailable",
                    "capability": "case_semantic_search",
                },
            )
            return
        except ArchiveOperationBusy as exc:
            payload = {
                "ok": False,
                "error": "backend_busy",
                "capability": exc.capability,
                "operation_state": "in_progress",
            }
            if exc.operation_id is not None:
                payload["operation_id"] = exc.operation_id
            self._send(
                503,
                payload,
            )
            return
        except ArchiveOperationTimeout as exc:
            payload = {
                "ok": False,
                "error": "backend_timeout",
                "capability": exc.capability,
                "operation_state": "in_progress",
            }
            if exc.operation_id is not None:
                payload["operation_id"] = exc.operation_id
            self._send(
                504,
                payload,
            )
            return
        except ArchiveBackendTimeout as exc:
            payload = {
                "ok": False,
                "error": "backend_timeout",
                "capability": exc.capability,
            }
            if exc.operation_id is not None:
                payload["operation_id"] = exc.operation_id
            self._send(
                504,
                payload,
            )
            return
        except (TurritopsisAdapterError, CogneeAdapterError):
            self._send(503, {"ok": False, "error": "backend_unavailable"})
            return
        except Exception:
            self._send(500, {"ok": False, "error": "internal_error"})
            return
        self._send(200, {"ok": True, "result": _jsonable(result)})

    def _read_request(self) -> Mapping[str, Any]:
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip() != "application/json":
            raise RequestError("Content-Type must be application/json")
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError as exc:
            raise RequestError("Content-Length must be an integer") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise RequestError("request body length is outside the allowed bound")
        try:
            value = json.loads(
                self.rfile.read(length).decode("utf-8"),
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite_number,
            )
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RequestError("request body must be valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise RequestError("request body must be a JSON object")
        return value

    def _send(
        self,
        status: int,
        payload: Mapping[str, Any],
        *,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        try:
            content = (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
        except (TypeError, ValueError):
            status = 500
            content = b'{"error":"response_not_json","ok":false}\n'
        if len(content) > MAX_RESPONSE_BYTES:
            status = 500
            content = b'{"error":"response_too_large","ok":false}\n'
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, _format: str, *args: Any) -> None:
        return


def _require_exact_keys(
    value: Mapping[str, Any],
    required: set,
    where: str,
    *,
    optional: frozenset = frozenset(),
) -> None:
    if not isinstance(value, dict):
        raise RequestError("%s must be an object" % where)
    missing = required - set(value)
    unknown = set(value) - required - optional
    if missing or unknown:
        raise RequestError("%s has missing or unknown keys" % where)


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RequestError("request JSON contains a duplicate key")
        result[key] = value
    return result


def _list_with_archive_gap(base: Mapping[str, Any], reason: str) -> Dict[str, Any]:
    result = dict(base)
    external = dict(result["truth_plane"]["coverage"])
    unavailable = dict(external["unavailable"])
    unavailable["history"] = reason
    result["history_archive"] = {"available": False, "reason": reason}
    result["coverage"] = {
        "consulted": ["current", "history", *external["consulted"]],
        "matched": ["current", *external["matched"]],
        "unavailable": unavailable,
        "failed": dict(external["failed"]),
        "complete": False,
    }
    return result


def _search_with_archive_gap(base: Mapping[str, Any], reason: str) -> Dict[str, Any]:
    result = dict(base)
    results = dict(result["results"])
    results["history"] = []
    coverage = dict(result["coverage"])
    consulted = list(coverage["consulted"])
    if "history" not in consulted:
        consulted.insert(1 if "current" in consulted else 0, "history")
    unavailable = dict(coverage["unavailable"])
    unavailable["history"] = reason
    failed = dict(coverage["failed"])
    result["results"] = results
    result["coverage"] = {
        "consulted": consulted,
        "matched": list(coverage["matched"]),
        "unavailable": unavailable,
        "failed": failed,
        "complete": False,
    }
    result["ok"] = not failed
    return result


def _reject_nonfinite_number(_value):
    raise RequestError("request JSON must not contain a non-finite number")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="project-continuity-front")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--credentials-dir", required=True, type=Path)
    parser.add_argument("--host", default=BIND_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        config = load_private_service_config(args.config)
        with runtime_lifetime_lock(config.paths.state_root):
            bind_cognee_environment(config)
            server = create_server(
                config,
                args.credentials_dir,
                host=args.host,
                port=args.port,
            )
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
    except (ConfigError, ServerConfigError, RuntimeLockError, OSError) as exc:
        raise SystemExit("project-continuity-front: %s" % exc) from exc


if __name__ == "__main__":
    main()
