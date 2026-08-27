from __future__ import annotations

import asyncio
from contextlib import contextmanager
import http.client
import json
import os
from pathlib import Path
import threading
import time

import pytest

from project_continuity.config import load_config
from project_continuity.cognee_adapter import CogneeCapabilityUnavailable
from project_continuity.server import (
    BIND_HOST,
    CredentialSet,
    ServerConfigError,
    bind_cognee_environment,
    create_server,
    load_private_service_config,
    main,
)
from project_continuity.runtime_environment import cognee_environment_for_config
from project_continuity.runtime_lock import RuntimeLockError, runtime_lifetime_lock
from project_continuity.turritopsis_adapter import TurritopsisUnavailable

from conftest import write_config


TOKENS = {
    "reader-client": "reader-client-token-000000000000000001",
    "writer-client": "writer-client-token-000000000000000001",
    "promoter-client": "promoter-client-token-0000000000000001",
}


class FakeFront:
    def __init__(self) -> None:
        self.calls = []
        self.fail = False

    def _result(self, name, principal_id, project_id, **arguments):
        if self.fail:
            raise TurritopsisUnavailable("backend path /srv/private must stay hidden")
        self.calls.append((name, principal_id, project_id, arguments))
        return {"tool": name, "principal_id": principal_id, **arguments}

    def list_stages(self, principal_id, project_id, current=""):
        return self._result("list", principal_id, project_id, current=current)

    def search_stages(self, principal_id, project_id, query, **arguments):
        return self._result(
            "search_stages", principal_id, project_id, query=query, **arguments
        )

    async def search_cases(self, principal_id, project_id, query, **arguments):
        return [
            self._result(
                "search_cases", principal_id, project_id, query=query, **arguments
            )
        ]

    def get_stage(self, principal_id, project_id, stage_id):
        return self._result(
            "get_stage", principal_id, project_id, stage_id=stage_id
        )

    async def get_case(self, principal_id, project_id, promotion_id):
        return self._result(
            "get_case", principal_id, project_id, promotion_id=promotion_id
        )

    def update_stage(
        self,
        principal_id,
        project_id,
        stage_id,
        body,
        *,
        expected_revision,
        mode,
    ):
        return self._result(
            "update",
            principal_id,
            project_id,
            stage_id=stage_id,
            body=body,
            expected_revision=expected_revision,
            mode=mode,
        )

    async def promote_stage(self, principal_id, project_id, stage_id, **arguments):
        return self._result(
            "promote", principal_id, project_id, stage_id=stage_id, **arguments
        )


def _credentials(config, root: Path) -> Path:
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    for principal in config.principals:
        path = root / (principal.principal_id + ".token")
        path.write_text(TOKENS[principal.principal_id] + "\n", encoding="ascii")
        path.chmod(0o600)
    return root


@contextmanager
def _running(tmp_path: Path):
    config = load_config(write_config(tmp_path / "runtime"))
    credentials = _credentials(config, tmp_path / "credentials")
    front = FakeFront()
    server = create_server(config, credentials, port=0, front=front)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], front
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def _request(port: int, method: str, path: str, *, token=None, body=None):
    headers = {}
    encoded = None
    if token is not None:
        headers["Authorization"] = "Bearer " + token
    if body is not None:
        encoded = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    connection = http.client.HTTPConnection(BIND_HOST, port, timeout=3)
    try:
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        return response.status, payload, dict(response.getheaders())
    finally:
        connection.close()


def _invoke(port: int, token: str, tool: str, arguments: dict):
    return _request(
        port,
        "POST",
        "/v1/invoke",
        token=token,
        body={"tool": tool, "project_id": "alpha", "arguments": arguments},
    )


def _ref(authority="github"):
    return {
        "authority": authority,
        "object_id": "commit:abc",
        "version": "abc",
        "digest": "sha256:" + "a" * 64,
        "producer": "github",
        "provenance": {},
    }


def test_health_is_loopback_only_and_does_not_require_a_token(tmp_path: Path) -> None:
    with _running(tmp_path) as (port, _front):
        status, payload, headers = _request(port, "GET", "/health")
    assert status == 200
    assert payload == {
        "ok": True,
        "status": "front_ready",
        "transport": "loopback_http",
        "dependencies": "not_probed",
    }
    assert headers["Cache-Control"] == "no-store"

    config = load_config(write_config(tmp_path / "second"))
    credentials = _credentials(config, tmp_path / "second-credentials")
    with pytest.raises(ServerConfigError, match="bind exactly"):
        create_server(config, credentials, host="0.0.0.0", port=8766)


def test_bearer_identity_routes_all_five_tools_without_actor_claim(tmp_path: Path) -> None:
    with _running(tmp_path) as (port, front):
        token = TOKENS["promoter-client"]
        calls = [
            _invoke(port, token, "list", {}),
            _invoke(port, token, "search", {"query": "交接"}),
            _invoke(port, token, "get", {"stage_id": "project.handoff"}),
            _invoke(
                port,
                token,
                "update",
                {
                    "stage_id": "project.handoff",
                    "body": "new body",
                    "expected_revision": "a" * 16,
                },
            ),
            _invoke(
                port,
                token,
                "promote",
                {
                    "stage_id": "project.handoff",
                    "source_revision": "a" * 16,
                    "idempotency_key": "reviewed-case-1",
                    "provenance": [_ref()],
                    "review_authority": _ref("github"),
                },
            ),
        ]
    assert [status for status, _payload, _headers in calls] == [200] * 5
    assert [call[0] for call in front.calls] == [
        "list",
        "search_stages",
        "get_stage",
        "update",
        "promote",
    ]
    assert {call[1] for call in front.calls} == {"promoter-client"}
    assert all("claimed_actor" not in call[3] for call in front.calls)


def test_case_get_and_search_reuse_the_existing_get_search_tools(tmp_path: Path) -> None:
    with _running(tmp_path) as (port, front):
        token = TOKENS["promoter-client"]
        search = _invoke(
            port,
            token,
            "search",
            {"scope": "cases", "query": "回归", "match": "keyword", "limit": 3},
        )
        get = _invoke(
            port, token, "get", {"promotion_id": "promotion:" + "a" * 64}
        )
    assert search[0] == 200
    assert get[0] == 200
    assert [call[0] for call in front.calls] == ["search_cases", "get_case"]
    assert front.calls[0][3]["match"] == "keyword"


def test_overlapping_archive_requests_are_serialized_without_blocking_stage_reads(
    tmp_path: Path,
) -> None:
    class OverlapFront(FakeFront):
        def __init__(self) -> None:
            super().__init__()
            self.first_entered = threading.Event()
            self.release_first = threading.Event()
            self.state_lock = threading.Lock()
            self.active_archive_calls = 0
            self.maximum_archive_calls = 0
            self.archive_entries = 0

        async def _archive_result(self, name, principal_id, project_id, **arguments):
            with self.state_lock:
                self.active_archive_calls += 1
                self.archive_entries += 1
                self.maximum_archive_calls = max(
                    self.maximum_archive_calls, self.active_archive_calls
                )
                entry = self.archive_entries
            if entry == 1:
                self.first_entered.set()
                await asyncio.to_thread(self.release_first.wait, 2)
            try:
                return self._result(name, principal_id, project_id, **arguments)
            finally:
                with self.state_lock:
                    self.active_archive_calls -= 1

        async def search_cases(self, principal_id, project_id, query, **arguments):
            return [
                await self._archive_result(
                    "search_cases",
                    principal_id,
                    project_id,
                    query=query,
                    **arguments,
                )
            ]

        async def promote_stage(self, principal_id, project_id, stage_id, **arguments):
            return await self._archive_result(
                "promote",
                principal_id,
                project_id,
                stage_id=stage_id,
                **arguments,
            )

    config = load_config(write_config(tmp_path / "runtime"))
    credentials = _credentials(config, tmp_path / "credentials")
    front = OverlapFront()
    server = create_server(config, credentials, port=0, front=front)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    port = server.server_address[1]
    results = {}

    def request_search():
        results["search"] = _invoke(
            port,
            TOKENS["promoter-client"],
            "search",
            {"scope": "cases", "query": "故障", "match": "semantic"},
        )

    def request_promote():
        results["promote"] = _invoke(
            port,
            TOKENS["promoter-client"],
            "promote",
            {
                "stage_id": "project.handoff",
                "source_revision": "a" * 16,
                "idempotency_key": "overlapping-case",
                "provenance": [_ref()],
                "review_authority": _ref(),
            },
        )

    first = threading.Thread(target=request_search)
    second = threading.Thread(target=request_promote)
    try:
        first.start()
        assert front.first_entered.wait(timeout=1)
        second.start()
        time.sleep(0.05)
        assert front.archive_entries == 1
        stage = _invoke(port, TOKENS["promoter-client"], "list", {})
        assert stage[0] == 200
        front.release_first.set()
        first.join(timeout=3)
        second.join(timeout=3)
    finally:
        front.release_first.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)

    assert not first.is_alive()
    assert not second.is_alive()
    assert results["search"][0] == 200
    assert results["promote"][0] == 200
    assert front.maximum_archive_calls == 1


def test_unconfigured_case_semantic_search_is_typed_and_bounded(tmp_path: Path) -> None:
    with _running(tmp_path) as (port, front):
        async def unavailable(*_args, **_kwargs):
            raise CogneeCapabilityUnavailable("provider path must stay hidden")

        front.search_cases = unavailable
        status, payload, _headers = _invoke(
            port,
            TOKENS["reader-client"],
            "search",
            {"scope": "cases", "query": "故障", "match": "semantic"},
        )
    assert status == 503
    assert payload == {
        "ok": False,
        "error": "capability_unavailable",
        "capability": "case_semantic_search",
    }
    assert "provider path" not in json.dumps(payload)


def test_case_search_timeout_is_typed_without_stopping_the_front(
    monkeypatch, tmp_path: Path
) -> None:
    with _running(tmp_path) as (port, front):
        async def slow(*_args, **_kwargs):
            import asyncio

            await asyncio.sleep(1)

        front.search_cases = slow
        monkeypatch.setattr(
            "project_continuity.server.CASE_SEARCH_TIMEOUT_SECONDS", 0.01
        )
        status, payload, _headers = _invoke(
            port,
            TOKENS["reader-client"],
            "search",
            {"scope": "cases", "query": "故障", "match": "semantic"},
        )
        health = _request(port, "GET", "/health")
    assert status == 504
    assert payload == {
        "ok": False,
        "error": "backend_timeout",
        "capability": "case_search",
    }
    assert health[0] == 200


def test_missing_or_wrong_token_is_rejected_without_invoking_front(tmp_path: Path) -> None:
    with _running(tmp_path) as (port, front):
        missing = _invoke(port, "wrong-token-that-is-long-enough-000000", "list", {})
        no_header = _request(
            port,
            "POST",
            "/v1/invoke",
            body={"tool": "list", "project_id": "alpha", "arguments": {}},
        )
    assert missing[0] == 401
    assert no_header[0] == 401
    assert front.calls == []


def test_unknown_arguments_cannot_smuggle_an_actor_claim(tmp_path: Path) -> None:
    with _running(tmp_path) as (port, front):
        status, payload, _headers = _invoke(
            port,
            TOKENS["writer-client"],
            "update",
            {
                "stage_id": "project.handoff",
                "body": "body",
                "expected_revision": "a" * 16,
                "claimed_actor": "forged",
            },
        )
    assert status == 400
    assert payload["error"] == "invalid_request"
    assert front.calls == []


def test_malformed_stable_ref_is_a_client_error_not_an_internal_failure(
    tmp_path: Path,
) -> None:
    with _running(tmp_path) as (port, front):
        status, payload, _headers = _invoke(
            port,
            TOKENS["promoter-client"],
            "promote",
            {
                "stage_id": "project.handoff",
                "source_revision": "a" * 16,
                "idempotency_key": "reviewed-case-1",
                "provenance": [{}],
                "review_authority": _ref(),
            },
        )
    assert status == 422
    assert payload["error"] == "invalid_operation"
    assert front.calls == []


def test_backend_failure_does_not_expose_filesystem_paths(tmp_path: Path) -> None:
    with _running(tmp_path) as (port, front):
        front.fail = True
        status, payload, _headers = _invoke(
            port, TOKENS["reader-client"], "list", {}
        )
    assert status == 503
    assert payload == {"ok": False, "error": "backend_unavailable"}
    assert "/srv/private" not in json.dumps(payload)


def test_credentials_are_complete_private_regular_files(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path / "runtime"))
    credentials = _credentials(config, tmp_path / "credentials")
    assert CredentialSet.load(config, credentials).authenticate(
        "Bearer " + TOKENS["reader-client"]
    ) == "reader-client"

    (credentials / "reader-client.token").chmod(0o644)
    with pytest.raises(ServerConfigError, match="must be private"):
        CredentialSet.load(config, credentials)


def test_credentials_reject_a_parent_symlink(tmp_path: Path) -> None:
    config = load_config(write_config(tmp_path / "runtime"))
    (tmp_path / "outside").mkdir()
    outside = _credentials(config, tmp_path / "outside/credentials")
    alias = tmp_path / "alias"
    alias.symlink_to(outside.parent, target_is_directory=True)

    with pytest.raises(ServerConfigError, match="real private directory"):
        CredentialSet.load(config, alias / "credentials")


def test_service_config_requires_private_file_and_parent(tmp_path: Path) -> None:
    parent = tmp_path / "private-config"
    config_path = write_config(parent)
    parent.chmod(0o700)
    config_path.chmod(0o600)

    assert load_private_service_config(config_path).project("alpha").project_id == "alpha"

    config_path.chmod(0o640)
    with pytest.raises(ServerConfigError, match="file must be owner-only"):
        load_private_service_config(config_path)
    config_path.chmod(0o600)
    parent.chmod(0o750)
    with pytest.raises(ServerConfigError, match="parent must be owner-only"):
        load_private_service_config(config_path)


def test_service_config_rejects_file_and_parent_symlinks(tmp_path: Path) -> None:
    private = tmp_path / "private"
    config_path = write_config(private)
    private.chmod(0o700)
    config_path.chmod(0o600)
    file_alias = tmp_path / "config-alias.toml"
    file_alias.symlink_to(config_path)

    with pytest.raises(ServerConfigError, match="absolute real private file"):
        load_private_service_config(file_alias)

    parent_alias = tmp_path / "parent-alias"
    parent_alias.symlink_to(private, target_is_directory=True)
    with pytest.raises(ServerConfigError, match="absolute real private file"):
        load_private_service_config(parent_alias / "config.toml")


def test_front_entrypoint_binds_config_derived_cognee_environment_before_server(
    monkeypatch, tmp_path: Path
) -> None:
    private = tmp_path / "private"
    config_path = write_config(private)
    private.chmod(0o700)
    config_path.chmod(0o600)
    config = load_config(config_path)
    expected = cognee_environment_for_config(config)
    for name in expected:
        monkeypatch.delenv(name, raising=False)
    seen = {}

    class FakeServer:
        def serve_forever(self):
            seen["served"] = True

        def server_close(self):
            seen["closed"] = True

    def fake_create_server(received, _credentials, **_arguments):
        seen["config"] = received
        seen["environment"] = {name: os.environ.get(name) for name in expected}
        return FakeServer()

    monkeypatch.setattr("project_continuity.server.create_server", fake_create_server)
    main(
        [
            "--config",
            str(config_path),
            "--credentials-dir",
            str(tmp_path / "credentials"),
        ]
    )

    assert seen == {
        "config": config,
        "environment": expected,
        "served": True,
        "closed": True,
    }


def test_front_holds_runtime_lock_for_its_whole_serving_lifetime(
    monkeypatch, tmp_path: Path
) -> None:
    private = tmp_path / "private"
    config_path = write_config(private)
    private.chmod(0o700)
    config_path.chmod(0o600)
    config = load_config(config_path)
    for name in cognee_environment_for_config(config):
        monkeypatch.delenv(name, raising=False)
    seen = {}

    class FakeServer:
        def serve_forever(self):
            with pytest.raises(RuntimeLockError, match="already active"):
                with runtime_lifetime_lock(config.paths.state_root):
                    pass
            seen["locked"] = True

        def server_close(self):
            seen["closed"] = True

    monkeypatch.setattr(
        "project_continuity.server.create_server",
        lambda *_arguments, **_keywords: FakeServer(),
    )
    main(
        [
            "--config",
            str(config_path),
            "--credentials-dir",
            str(tmp_path / "credentials"),
        ]
    )

    assert seen == {"locked": True, "closed": True}


def test_active_relocation_lock_refuses_front_before_server_construction(
    monkeypatch, tmp_path: Path
) -> None:
    private = tmp_path / "private"
    config_path = write_config(private)
    private.chmod(0o700)
    config_path.chmod(0o600)
    config = load_config(config_path)
    for name in cognee_environment_for_config(config):
        monkeypatch.delenv(name, raising=False)

    def forbidden(*_arguments, **_keywords):
        raise AssertionError("server construction must not run under relocation lock")

    monkeypatch.setattr("project_continuity.server.create_server", forbidden)
    with runtime_lifetime_lock(config.paths.state_root):
        with pytest.raises(SystemExit, match="already active"):
            main(
                [
                    "--config",
                    str(config_path),
                    "--credentials-dir",
                    str(tmp_path / "credentials"),
                ]
            )


def test_cognee_environment_mismatch_fails_before_any_partial_binding(
    monkeypatch, tmp_path: Path
) -> None:
    config = load_config(write_config(tmp_path / "runtime"))
    expected = cognee_environment_for_config(config)
    names = tuple(expected)
    monkeypatch.setenv(names[0], "wrong")
    for name in names[1:]:
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(ServerConfigError, match="does not match operator config"):
        bind_cognee_environment(config)

    assert os.environ[names[0]] == "wrong"
    assert all(name not in os.environ for name in names[1:])

    for name, value in expected.items():
        monkeypatch.setenv(name, value)
    assert bind_cognee_environment(config) == expected


def test_duplicate_json_keys_are_rejected(tmp_path: Path) -> None:
    with _running(tmp_path) as (port, front):
        connection = http.client.HTTPConnection(BIND_HOST, port, timeout=3)
        body = b'{"tool":"list","tool":"get","project_id":"alpha","arguments":{}}'
        connection.request(
            "POST",
            "/v1/invoke",
            body=body,
            headers={
                "Authorization": "Bearer " + TOKENS["reader-client"],
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
    assert response.status == 400
    assert payload["error"] == "invalid_request"
    assert front.calls == []


def test_nonfinite_json_number_is_rejected(tmp_path: Path) -> None:
    with _running(tmp_path) as (port, front):
        connection = http.client.HTTPConnection(BIND_HOST, port, timeout=3)
        body = b'{"tool":"search","project_id":"alpha","arguments":{"query":NaN}}'
        connection.request(
            "POST",
            "/v1/invoke",
            body=body,
            headers={
                "Authorization": "Bearer " + TOKENS["reader-client"],
                "Content-Type": "application/json",
            },
        )
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()
    assert response.status == 400
    assert payload["error"] == "invalid_request"
    assert front.calls == []


def test_non_json_backend_value_becomes_a_bounded_internal_error(tmp_path: Path) -> None:
    with _running(tmp_path) as (port, front):
        front.list_stages = lambda *_args, **_kwargs: {"score": float("nan")}
        status, payload, _headers = _invoke(
            port, TOKENS["reader-client"], "list", {}
        )
    assert status == 500
    assert payload == {"error": "response_not_json", "ok": False}
