from __future__ import annotations

import asyncio
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import sys
import threading
import time
from types import ModuleType
from typing import Dict, List, Optional

import pytest

from project_continuity.auth import AuthorizationError
from project_continuity.cognee_adapter import (
    BackendIdentityConflict,
    COGNEE_COMMIT,
    COGNEE_VERSION,
    CogneeCase,
    CogneeCaseRecord,
    CogneeCapabilityUnavailable,
    CogneeUnavailable,
    cognee_data_id,
)
import project_continuity.cognee_adapter as cognee_adapter
from project_continuity.evidence import StableRef
from project_continuity.front import FRONT_TOOLS, CognitionFront
from project_continuity.promotion import (
    MAX_HISTORICAL_RELATIONS,
    PROMOTION_KIND,
    PROMOTION_SCHEMA_VERSION,
    PromotionRequest,
    PromotionValidationError,
    StaleSourceRevision,
    build_envelope,
)
from project_continuity.receipts import (
    IdempotencyConflict,
    ReceiptStore,
    promotion_receipt_path,
)
from project_continuity.server import (
    ArchiveOperationBusy,
    ArchiveOperationTimeout,
    _ArchiveRunner,
)
from project_continuity.turritopsis_adapter import project_store_path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _digest(value: bytes) -> str:
    return "sha256:" + sha256(value).hexdigest()


def _ref(authority: str, name: str) -> StableRef:
    return StableRef(
        authority=authority,
        object_id="%s:%s" % (authority, name),
        version="a" * 40,
        digest=_digest(name.encode("utf-8")),
        producer="%s-test" % authority,
        provenance=(("source", name),),
        projection="reviewed",
    )


def _write_store(config) -> Path:
    path = project_store_path(config.paths.data_root, "alpha")
    path.parent.mkdir(parents=True, exist_ok=True)
    document = {
        "title": "Alpha",
        "subtitle": "Promotion canary",
        "version": 1,
        "currents": [
            {
                "id": "engineering",
                "name": "Engineering",
                "blurb": "Reviewed cases",
                "stages": [
                    {
                        "id": "engineering.wakeup-case",
                        "title": "Repeated wake investigation",
                        "body": (
                            "# 工程 Case\n\n"
                            "Symptom: 重复唤醒。\n"
                            "Hypothesis: AOS identity 随评分漂移。\n"
                            "Investigation: 对比 set membership 与 score。\n"
                            "Decision: identity 只绑定 immutable membership。\n"
                            "Implementation: canonical sort 后 hash。\n"
                            "Regression: 时间推进不改变 set_id。\n"
                            "Outcome: 生产 checkpoint Green。\n"
                            "Evidence: Graphify/OpenSpec/GitHub refs。\n"
                            "Status: current\n"
                            "Authority: project cognition and handoff\n"
                        ),
                    }
                ],
            }
        ],
    }
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


class FakeCogneeBackend:
    def __init__(self) -> None:
        self.records: Dict[tuple, CogneeCaseRecord] = {}
        self.writes = 0
        self.available = True
        self._lock: Optional[asyncio.Lock] = None

    async def lookup(
        self, project_id: str, promotion_id: str
    ) -> Optional[CogneeCaseRecord]:
        await asyncio.sleep(0)
        if not self.available:
            raise CogneeUnavailable("simulated outage")
        return self.records.get((project_id, promotion_id))

    async def upsert(self, case: CogneeCase) -> CogneeCaseRecord:
        await asyncio.sleep(0)
        if not self.available:
            raise CogneeUnavailable("simulated outage")
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            key = (case.project_id, case.promotion_id)
            existing = self.records.get(key)
            if existing is not None:
                if (
                    existing.envelope_digest != case.envelope_digest
                    or existing.content_digest != case.content_digest
                ):
                    raise BackendIdentityConflict("fake deterministic id conflict")
                return existing
            self.writes += 1
            record = CogneeCaseRecord(
                project_id=case.project_id,
                promotion_id=case.promotion_id,
                data_id=cognee_data_id(case.project_id, case.promotion_id),
                envelope_digest=case.envelope_digest,
                source_digest=case.source_digest,
                content=case.content,
                content_digest=case.content_digest,
                metadata=dict(case.metadata),
            )
            self.records[key] = record
            return record

    async def search(
        self,
        project_id: str,
        query: str,
        *,
        match: str = "keyword",
        limit: int = 8,
    ) -> List[dict]:
        if not self.available:
            raise CogneeUnavailable("simulated outage")
        matches = [
            {
                "promotion_id": record.promotion_id,
                "content": record.content,
            }
            for (record_project, _), record in self.records.items()
            if record_project == project_id and query in record.content
        ]
        return matches[:limit]


class CrashOnceReceiptStore(ReceiptStore):
    def __init__(self, path: Path) -> None:
        super().__init__(path)
        self.crash = True

    def commit(self, promotion_id: str, *, envelope_digest: str, backend_data_id: str):
        if self.crash:
            self.crash = False
            raise RuntimeError("simulated crash after backend success")
        return super().commit(
            promotion_id,
            envelope_digest=envelope_digest,
            backend_data_id=backend_data_id,
        )


class PartialArchiveBackend(FakeCogneeBackend):
    """Model Cognee add committed while cognify/archive processing failed."""

    async def lookup(
        self, project_id: str, promotion_id: str
    ) -> Optional[CogneeCaseRecord]:
        record = await super().lookup(project_id, promotion_id)
        return record if record is None or record.ready else None

    async def upsert(self, case: CogneeCase) -> CogneeCaseRecord:
        key = (case.project_id, case.promotion_id)
        existing = self.records.get(key)
        if existing is not None:
            completed = replace(existing, ready=True)
            self.records[key] = completed
            return completed
        self.writes += 1
        self.records[key] = CogneeCaseRecord(
            project_id=case.project_id,
            promotion_id=case.promotion_id,
            data_id=cognee_data_id(case.project_id, case.promotion_id),
            envelope_digest=case.envelope_digest,
            source_digest=case.source_digest,
            content=case.content,
            content_digest=case.content_digest,
            metadata=dict(case.metadata),
            ready=False,
        )
        raise CogneeUnavailable("simulated cognify failure after data commit")


class BlockingArchiveBackend(FakeCogneeBackend):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.active = 0
        self.maximum = 0

    async def upsert(self, case: CogneeCase) -> CogneeCaseRecord:
        self.active += 1
        self.maximum = max(self.maximum, self.active)
        self.entered.set()
        try:
            await asyncio.to_thread(self.release.wait, 2)
            return await super().upsert(case)
        finally:
            self.active -= 1


@pytest.fixture
def f4(config):
    _write_store(config)
    backend = FakeCogneeBackend()
    front = CognitionFront(config, cognee_backend=backend)
    stage = front.get_stage(
        "promoter-client", "alpha", "engineering.wakeup-case"
    )
    fields = {
        "principal_id": "promoter-client",
        "project_id": "alpha",
        "stage_id": "engineering.wakeup-case",
        "source_revision": stage["revision"],
        "idempotency_key": "case-wakeup-001",
        "provenance": (
            _ref("graphify", "graph-at-sha"),
            _ref("openspec", "decision-028"),
            _ref("github", "commit-123"),
            _ref("event", "event-456"),
        ),
        "review_authority": _ref("github", "review-789"),
    }
    return front, backend, stage, fields


def test_f4_exact_donor_coordinate_and_five_tool_surface_are_frozen() -> None:
    assert COGNEE_COMMIT == "a8f9760bb6da90a9956b3be77c0d0534134f533a"
    assert COGNEE_VERSION == "1.5.2"
    assert FRONT_TOOLS == ("list", "search", "get", "update", "promote")
    package = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert COGNEE_COMMIT in package


def test_case_keyword_search_reuses_cjk_donor_without_vector_configuration(
    monkeypatch,
) -> None:
    for name in ("EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS"):
        monkeypatch.delenv(name, raising=False)
    record = CogneeCaseRecord(
        project_id="alpha",
        promotion_id="promotion:" + "a" * 64,
        data_id="data-1",
        envelope_digest="sha256:" + "b" * 64,
        source_digest="sha256:" + "c" * 64,
        content="# Case\n\n中文检索支持大小写不敏感，并保留故障因果链。",
        content_digest="sha256:" + "d" * 64,
        metadata={},
    )
    results = cognee_adapter.case_keyword_search(
        [record], "大小写不敏感", limit=8
    )
    assert results == [
        {
            "promotion_id": record.promotion_id,
            "data_id": "data-1",
            "source_digest": record.source_digest,
            "content_digest": record.content_digest,
            "score": results[0]["score"],
            "matched": results[0]["matched"],
            "snippet": results[0]["snippet"],
            "ready": True,
        }
    ]
    assert "大小写不敏感" in results[0]["snippet"]


def test_semantic_case_search_requires_explicit_embedding_configuration(
    monkeypatch,
) -> None:
    for name in ("EMBEDDING_PROVIDER", "EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(CogneeCapabilityUnavailable, match="explicit embedding"):
        cognee_adapter._require_semantic_search_configuration()

    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai_compatible")
    monkeypatch.setenv("EMBEDDING_MODEL", "embedding-model")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "768")
    assert cognee_adapter._require_semantic_search_configuration() is None


def test_native_data_identity_is_stable_and_project_scoped() -> None:
    promotion_id = "promotion:" + "a" * 64
    assert cognee_data_id("alpha", promotion_id) == cognee_data_id(
        "alpha", promotion_id
    )
    assert cognee_data_id("alpha", promotion_id) != cognee_data_id(
        "beta", promotion_id
    )


def test_native_adapter_refuses_remote_mode_without_stable_data_id(
    monkeypatch,
) -> None:
    modules = {}
    for name in ("cognee", "cognee.api", "cognee.api.v1", "cognee.api.v1.serve"):
        module = ModuleType(name)
        module.__path__ = []
        modules[name] = module
        monkeypatch.setitem(sys.modules, name, module)
    state = ModuleType("cognee.api.v1.serve.state")
    state.get_remote_client = lambda: object()
    monkeypatch.setitem(sys.modules, "cognee.api.v1.serve.state", state)
    monkeypatch.setattr(cognee_adapter, "_assert_runtime_version", lambda: None)

    with pytest.raises(CogneeUnavailable, match="remote Cognee add"):
        cognee_adapter._assert_native_runtime()


def test_envelope_is_canonical_across_provenance_order(f4) -> None:
    _front, _backend, stage, fields = f4
    first = PromotionRequest.create(
        project_id="alpha",
        stage_id=fields["stage_id"],
        source_revision=stage["revision"],
        idempotency_key="canonical-1",
        provenance=fields["provenance"],
        review_authority=fields["review_authority"],
    )
    second = PromotionRequest.create(
        project_id="alpha",
        stage_id=fields["stage_id"],
        source_revision=stage["revision"],
        idempotency_key="canonical-1",
        provenance=reversed(fields["provenance"]),
        review_authority=fields["review_authority"],
    )
    one = build_envelope(first, actor="promoter-agent", stage=stage)
    two = build_envelope(second, actor="promoter-agent", stage=dict(stage))
    assert first.promotion_id == second.promotion_id
    assert one.envelope_digest == two.envelope_digest
    assert one.case_content_digest == two.case_content_digest


def test_one_reviewed_stage_becomes_one_case_and_source_does_not_change(f4) -> None:
    front, backend, before, fields = f4
    receipt = asyncio.run(front.promote_stage(**fields))
    after = front.get_stage(
        "promoter-client", "alpha", "engineering.wakeup-case"
    )

    assert receipt["ok"] is True and receipt["status"] == "committed"
    assert backend.writes == 1 and len(backend.records) == 1
    assert after["revision"] == before["revision"]
    assert after["body"] == before["body"]
    assert not (project_store_path(
        front.config.paths.data_root, "alpha"
    ).parent / "changelog.jsonl").exists()
    operation = ReceiptStore(
        promotion_receipt_path(front.config.paths.state_root)
    ).find("alpha", fields["idempotency_key"])
    assert operation is not None and operation.state == "committed"
    assert operation.frozen_case_json is None
    assert operation.frozen_payload_digest.startswith("sha256:")


def test_second_reader_retrieves_chinese_case_and_full_path(f4) -> None:
    front, _backend, _stage, fields = f4
    receipt = asyncio.run(front.promote_stage(**fields))
    found = asyncio.run(
        front.search_cases("reader-client", "alpha", "重复唤醒")
    )
    case = asyncio.run(
        front.get_case("reader-client", "alpha", receipt["promotion_id"])
    )

    assert found and found[0]["promotion_id"] == receipt["promotion_id"]
    assert case is not None
    for label in (
        "Hypothesis:",
        "Investigation:",
        "Decision:",
        "Implementation:",
        "Regression:",
        "Outcome:",
        "Evidence:",
    ):
        assert label in case.content


def test_same_idempotency_key_replay_returns_one_case_and_receipt(f4) -> None:
    front, backend, _stage, fields = f4
    first = asyncio.run(front.promote_stage(**fields))
    second = asyncio.run(front.promote_stage(**dict(fields)))
    assert second == first
    assert backend.writes == 1 and len(backend.records) == 1


def test_archive_timeout_keeps_prepared_promotion_owned_until_same_key_replay(
    config,
) -> None:
    _write_store(config)
    backend = BlockingArchiveBackend()
    front = CognitionFront(config, cognee_backend=backend)
    stage = front.get_stage(
        "promoter-client", "alpha", "engineering.wakeup-case"
    )
    fields = {
        "principal_id": "promoter-client",
        "project_id": "alpha",
        "stage_id": "engineering.wakeup-case",
        "source_revision": stage["revision"],
        "idempotency_key": "archive-timeout-replay-1",
        "provenance": (_ref("github", "timeout-source"),),
        "review_authority": _ref("github", "timeout-review"),
    }
    runner = _ArchiveRunner()
    try:
        with pytest.raises(ArchiveOperationTimeout):
            runner.run(
                lambda: front.promote_stage(**fields),
                timeout=0.01,
                capability="case_archive",
            )
        assert backend.entered.wait(1)
        prepared = ReceiptStore(
            promotion_receipt_path(config.paths.state_root)
        ).find("alpha", fields["idempotency_key"])
        assert prepared is not None and prepared.state == "prepared"
        assert backend.active == backend.maximum == 1

        with pytest.raises(ArchiveOperationBusy):
            runner.run(
                lambda: front.promote_stage(**dict(fields)),
                timeout=0.1,
                capability="case_archive",
            )
        assert backend.writes == 0

        backend.release.set()
        deadline = time.monotonic() + 2
        while True:
            try:
                receipt = runner.run(
                    lambda: front.promote_stage(**dict(fields)),
                    timeout=1,
                    capability="case_archive",
                )
            except ArchiveOperationBusy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
                continue
            break

        assert receipt["status"] == "committed"
        assert receipt["promotion_id"] == prepared.promotion_id
        assert receipt["backend_data_id"] == cognee_data_id(
            "alpha", prepared.promotion_id
        )
        assert backend.writes == 1
        assert len(backend.records) == 1
        assert backend.active == 0
        assert backend.maximum == 1
        after = front.get_stage(
            "promoter-client", "alpha", "engineering.wakeup-case"
        )
        assert after["revision"] == stage["revision"]
    finally:
        backend.release.set()
        runner.close()


def test_committed_replay_survives_later_source_stage_progress(f4) -> None:
    front, backend, stage, fields = f4
    first = asyncio.run(front.promote_stage(**fields))
    changed = front.update_stage(
        "promoter-client",
        "alpha",
        fields["stage_id"],
        stage["body"] + "\nNew current work after the archived Case.\n",
        expected_revision=stage["revision"],
    )
    assert changed["changed"] is True
    replay = asyncio.run(front.promote_stage(**dict(fields)))
    assert replay == first
    assert backend.writes == 1


def test_same_key_with_changed_envelope_is_rejected(f4) -> None:
    front, backend, _stage, fields = f4
    asyncio.run(front.promote_stage(**fields))
    changed = dict(fields)
    changed["provenance"] = fields["provenance"] + (_ref("github", "other"),)
    with pytest.raises(IdempotencyConflict):
        asyncio.run(front.promote_stage(**changed))
    assert backend.writes == 1


def test_existing_backend_identity_with_different_digest_fails_closed(f4) -> None:
    front, backend, stage, fields = f4
    request = PromotionRequest.create(
        project_id="alpha",
        stage_id=fields["stage_id"],
        source_revision=stage["revision"],
        idempotency_key=fields["idempotency_key"],
        provenance=fields["provenance"],
        review_authority=fields["review_authority"],
    )
    envelope = build_envelope(request, actor="promoter-agent", stage=stage)
    backend.records[("alpha", request.promotion_id)] = CogneeCaseRecord(
        project_id="alpha",
        promotion_id=request.promotion_id,
        data_id=cognee_data_id("alpha", request.promotion_id),
        envelope_digest="sha256:" + "0" * 64,
        source_digest=envelope.source_digest,
        content=envelope.case_content,
        content_digest=envelope.case_content_digest,
        metadata={},
    )
    with pytest.raises(BackendIdentityConflict, match="different archived evidence"):
        asyncio.run(front.promote_stage(**fields))
    prepared = ReceiptStore(
        promotion_receipt_path(front.config.paths.state_root)
    ).find("alpha", fields["idempotency_key"])
    assert prepared is not None and prepared.state == "prepared"
    assert backend.writes == 0


def test_crash_after_backend_write_reconciles_without_duplicate(config) -> None:
    _write_store(config)
    backend = FakeCogneeBackend()
    path = promotion_receipt_path(config.paths.state_root)
    crashing = CognitionFront(
        config,
        cognee_backend=backend,
        receipt_store=CrashOnceReceiptStore(path),
    )
    stage = crashing.get_stage(
        "promoter-client", "alpha", "engineering.wakeup-case"
    )
    fields = {
        "principal_id": "promoter-client",
        "project_id": "alpha",
        "stage_id": "engineering.wakeup-case",
        "source_revision": stage["revision"],
        "idempotency_key": "crash-window-1",
        "provenance": (_ref("github", "commit-crash"),),
        "review_authority": _ref("github", "review-crash"),
    }
    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(crashing.promote_stage(**fields))
    prepared = ReceiptStore(path).find("alpha", "crash-window-1")
    assert prepared is not None and prepared.state == "prepared"
    assert backend.writes == 1

    recovered = CognitionFront(
        config,
        cognee_backend=backend,
        receipt_store=ReceiptStore(path),
    )
    receipt = asyncio.run(recovered.promote_stage(**fields))
    assert receipt["status"] == "committed"
    assert backend.writes == 1 and len(backend.records) == 1


def test_two_concurrent_retries_upsert_one_case(f4) -> None:
    front, backend, _stage, fields = f4

    async def run():
        return await asyncio.gather(
            front.promote_stage(**fields),
            front.promote_stage(**dict(fields)),
        )

    first, second = asyncio.run(run())
    assert first["promotion_id"] == second["promotion_id"]
    assert backend.writes == 1 and len(backend.records) == 1


def test_partial_cognee_data_row_is_not_a_committed_case_and_retry_resumes(config) -> None:
    _write_store(config)
    backend = PartialArchiveBackend()
    front = CognitionFront(config, cognee_backend=backend)
    stage = front.get_stage(
        "promoter-client", "alpha", "engineering.wakeup-case"
    )
    fields = {
        "principal_id": "promoter-client",
        "project_id": "alpha",
        "stage_id": "engineering.wakeup-case",
        "source_revision": stage["revision"],
        "idempotency_key": "partial-cognify-1",
        "provenance": (_ref("github", "partial-commit"),),
        "review_authority": _ref("github", "partial-review"),
    }
    with pytest.raises(CogneeUnavailable, match="cognify failure"):
        asyncio.run(front.promote_stage(**fields))
    prepared = ReceiptStore(
        promotion_receipt_path(config.paths.state_root)
    ).find("alpha", "partial-cognify-1")
    assert prepared is not None and prepared.state == "prepared"
    assert prepared.frozen_case_json is not None
    assert asyncio.run(
        front.get_case("reader-client", "alpha", prepared.promotion_id)
    ) is None

    update = front.update_stage(
        "writer-client",
        "alpha",
        fields["stage_id"],
        stage["body"] + "\nCurrent Stage advances while old Case cognify is incomplete.\n",
        expected_revision=stage["revision"],
    )
    assert update["changed"] is True
    receipt = asyncio.run(front.promote_stage(**fields))
    assert receipt["status"] == "committed"
    assert backend.writes == 1
    archived = backend.records[("alpha", receipt["promotion_id"])]
    assert archived.ready is True
    assert "Current Stage advances" not in archived.content
    completed = ReceiptStore(
        promotion_receipt_path(config.paths.state_root)
    ).find("alpha", "partial-cognify-1")
    assert completed is not None and completed.frozen_case_json is None


def test_outage_leaves_prepared_but_normal_stage_work_stays_available(f4) -> None:
    front, backend, stage, fields = f4
    backend.available = False
    with pytest.raises(CogneeUnavailable, match="outage"):
        asyncio.run(front.promote_stage(**fields))
    prepared = ReceiptStore(
        promotion_receipt_path(front.config.paths.state_root)
    ).find("alpha", fields["idempotency_key"])
    assert prepared is not None and prepared.state == "prepared"
    assert prepared.frozen_case_json is not None

    update = front.update_stage(
        "writer-client",
        "alpha",
        fields["stage_id"],
        stage["body"] + "\nOngoing work remains writable.\n",
        expected_revision=stage["revision"],
    )
    assert update["changed"] is True

    backend.available = True
    receipt = asyncio.run(front.promote_stage(**fields))
    assert receipt["status"] == "committed"
    archived = backend.records[("alpha", receipt["promotion_id"])]
    assert "Ongoing work remains writable" not in archived.content
    assert stage["body"] in archived.content
    completed = ReceiptStore(
        promotion_receipt_path(front.config.paths.state_root)
    ).find("alpha", fields["idempotency_key"])
    assert completed is not None and completed.frozen_case_json is None


def test_stale_role_actor_provenance_and_life_memory_fail_before_backend(f4) -> None:
    front, backend, stage, fields = f4
    stale = dict(fields)
    stale["source_revision"] = "0" * 16
    with pytest.raises(StaleSourceRevision):
        asyncio.run(front.promote_stage(**stale))

    denied = dict(fields)
    denied["principal_id"] = "writer-client"
    with pytest.raises(AuthorizationError, match="cannot use promote"):
        asyncio.run(front.promote_stage(**denied))

    forged = dict(fields)
    forged["claimed_actor"] = "forged-agent"
    with pytest.raises(AuthorizationError, match="derived"):
        asyncio.run(front.promote_stage(**forged))

    missing = dict(fields)
    missing["provenance"] = ()
    with pytest.raises(PromotionValidationError, match="provenance"):
        asyncio.run(front.promote_stage(**missing))

    life = dict(fields)
    life["promotion_kind"] = "identity_memory"
    with pytest.raises(PromotionValidationError, match="identity/life"):
        asyncio.run(front.promote_stage(**life))

    private_memory = dict(fields)
    private_memory["provenance"] = (_ref("personal-memory", "life-memory"),)
    with pytest.raises(PromotionValidationError, match="authority boundary"):
        asyncio.run(front.promote_stage(**private_memory))

    secret_ref = StableRef(
        authority="github",
        object_id="github:secret-ref",
        version="a" * 40,
        digest=_digest(b"secret-ref"),
        producer="github-test",
        provenance=(("token", "ghp_not_real"),),
    )
    leaked = dict(fields)
    leaked["provenance"] = (secret_ref,)
    with pytest.raises(PromotionValidationError, match="secret-shaped"):
        asyncio.run(front.promote_stage(**leaked))

    assert backend.writes == 0


def test_correction_and_supersession_remain_historical_relations(f4) -> None:
    front, _backend, stage, fields = f4
    first = asyncio.run(front.promote_stage(**fields))

    update = front.update_stage(
        "promoter-client",
        "alpha",
        fields["stage_id"],
        stage["body"].replace(
            "Hypothesis: AOS identity 随评分漂移。",
            "Hypothesis: provider backfill 重放旧 failure。",
        ),
        expected_revision=stage["revision"],
    )
    assert update["changed"] is True
    updated_stage = front.get_stage(
        "promoter-client", "alpha", fields["stage_id"]
    )
    second_fields = dict(fields)
    second_fields.update(
        source_revision=updated_stage["revision"],
        idempotency_key="case-wakeup-002",
        corrects=(first["promotion_id"],),
        supersedes=(first["promotion_id"],),
    )
    with pytest.raises(PromotionValidationError, match="distinct"):
        asyncio.run(front.promote_stage(**second_fields))

    second_fields["corrects"] = (first["promotion_id"],)
    second_fields["supersedes"] = ()
    second = asyncio.run(front.promote_stage(**second_fields))
    archived = asyncio.run(
        front.get_case("reader-client", "alpha", second["promotion_id"])
    )
    assert archived is not None
    assert first["promotion_id"] in archived.content
    assert "provider backfill" in archived.content


def test_historical_relations_reject_self_reference_and_unbounded_fanout(f4) -> None:
    front, backend, stage, fields = f4
    request = PromotionRequest.create(
        project_id="alpha",
        stage_id=fields["stage_id"],
        source_revision=stage["revision"],
        idempotency_key="self-relation-1",
        provenance=fields["provenance"],
        review_authority=fields["review_authority"],
    )
    self_reference = dict(fields)
    self_reference.update(
        idempotency_key="self-relation-1",
        corrects=(request.promotion_id,),
    )
    with pytest.raises(PromotionValidationError, match="reference itself"):
        asyncio.run(front.promote_stage(**self_reference))

    unbounded = dict(fields)
    unbounded.update(
        idempotency_key="unbounded-relations-1",
        corrects=tuple(
            "promotion:" + format(index, "064x")
            for index in range(MAX_HISTORICAL_RELATIONS + 1)
        ),
    )
    with pytest.raises(PromotionValidationError, match="bounded"):
        asyncio.run(front.promote_stage(**unbounded))
    assert backend.writes == 0


def test_historical_relations_must_resolve_ready_in_the_same_project(f4) -> None:
    front, backend, _stage, fields = f4
    missing_id = "promotion:" + "1" * 64
    missing = dict(fields)
    missing.update(idempotency_key="missing-relation-1", corrects=(missing_id,))
    with pytest.raises(PromotionValidationError, match="ready Case in this project"):
        asyncio.run(front.promote_stage(**missing))

    foreign_id = "promotion:" + "2" * 64
    backend.records[("beta", foreign_id)] = CogneeCaseRecord(
        project_id="beta",
        promotion_id=foreign_id,
        data_id=cognee_data_id("beta", foreign_id),
        envelope_digest="sha256:" + "2" * 64,
        source_digest="sha256:" + "3" * 64,
        content="foreign project Case",
        content_digest=_digest(b"foreign project Case"),
        metadata={},
    )
    foreign = dict(fields)
    foreign.update(idempotency_key="foreign-relation-1", corrects=(foreign_id,))
    with pytest.raises(PromotionValidationError, match="ready Case in this project"):
        asyncio.run(front.promote_stage(**foreign))

    incomplete_id = "promotion:" + "3" * 64
    backend.records[("alpha", incomplete_id)] = CogneeCaseRecord(
        project_id="alpha",
        promotion_id=incomplete_id,
        data_id=cognee_data_id("alpha", incomplete_id),
        envelope_digest="sha256:" + "4" * 64,
        source_digest="sha256:" + "5" * 64,
        content="incomplete Case",
        content_digest=_digest(b"incomplete Case"),
        metadata={},
        ready=False,
    )
    incomplete = dict(fields)
    incomplete.update(
        idempotency_key="incomplete-relation-1",
        corrects=(incomplete_id,),
    )
    with pytest.raises(PromotionValidationError, match="ready Case in this project"):
        asyncio.run(front.promote_stage(**incomplete))
    assert backend.writes == 0


def test_receipt_store_is_private_and_not_created_by_f3_reads(config) -> None:
    _write_store(config)
    front = CognitionFront(config, cognee_backend=FakeCogneeBackend())
    front.get_stage("reader-client", "alpha", "engineering.wakeup-case")
    path = promotion_receipt_path(config.paths.state_root)
    assert not path.exists()

    stage = front.get_stage(
        "promoter-client", "alpha", "engineering.wakeup-case"
    )
    asyncio.run(
        front.promote_stage(
            "promoter-client",
            "alpha",
            "engineering.wakeup-case",
            source_revision=stage["revision"],
            idempotency_key="permissions-1",
            provenance=(_ref("github", "commit-permissions"),),
            review_authority=_ref("github", "review-permissions"),
        )
    )
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"


def test_receipt_path_rejects_symlink(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "promotion").symlink_to(outside, target_is_directory=True)
    store = ReceiptStore(state / "promotion" / "receipts.sqlite3")
    with pytest.raises(Exception, match="symlink"):
        store.find("alpha", "key")
    assert list(outside.iterdir()) == []


def test_cognee_outage_does_not_change_frozen_front_constants() -> None:
    assert PROMOTION_KIND == "engineering_case"
    assert PROMOTION_SCHEMA_VERSION == 1
    assert "promote" in FRONT_TOOLS
