"""Explicit reviewed Stage revision promotion into Project Cognee."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
import re
from typing import Any, Callable, Dict, Mapping, Sequence, Tuple

from .cognee_adapter import (
    BackendIdentityConflict,
    CogneeBackend,
    CogneeCase,
    CogneeCaseRecord,
    verify_case_record,
)
from .evidence import StableRef, sanitize_evidence
from .receipts import (
    IdempotencyConflict,
    PromotionOperation,
    ReceiptError,
    ReceiptStore,
    new_prepared_operation,
)


PROMOTION_SCHEMA_VERSION = 1
PROMOTION_KIND = "engineering_case"
_REVISION = re.compile(r"^[0-9a-f]{16}$")
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_PROMOTION_ID = re.compile(r"^promotion:[0-9a-f]{64}$")
MAX_HISTORICAL_RELATIONS = 32
_PROVENANCE_AUTHORITIES = frozenset(
    {"event", "github", "graphify", "openspec", "teamai", "turritopsis"}
)
_REVIEW_AUTHORITIES = frozenset({"github", "openspec", "teamai"})


class PromotionError(RuntimeError):
    """The explicit promotion could not satisfy its frozen contract."""


class PromotionValidationError(ValueError):
    """The caller supplied an invalid or authority-crossing envelope."""


class StaleSourceRevision(PromotionError):
    """The current Stage no longer matches the reviewed source revision."""


@dataclass(frozen=True)
class PromotionRequest:
    project_id: str
    stage_id: str
    source_revision: str
    idempotency_key: str
    promotion_kind: str
    schema_version: int
    provenance: Tuple[StableRef, ...]
    review_authority: StableRef
    corrects: Tuple[str, ...]
    supersedes: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.project_id, str) or not self.project_id:
            raise PromotionValidationError("project_id is required")
        if not isinstance(self.stage_id, str) or not self.stage_id:
            raise PromotionValidationError("stage_id is required")
        if not isinstance(self.source_revision, str) or not _REVISION.fullmatch(
            self.source_revision
        ):
            raise PromotionValidationError("source_revision must be exact 16-hex")
        if not isinstance(self.idempotency_key, str) or not _IDEMPOTENCY_KEY.fullmatch(
            self.idempotency_key
        ):
            raise PromotionValidationError("idempotency_key must be a stable bounded key")
        if self.promotion_kind != PROMOTION_KIND:
            raise PromotionValidationError(
                "promotion_kind must be engineering_case; identity/life memory is outside Cognee"
            )
        if type(self.schema_version) is not int or self.schema_version != PROMOTION_SCHEMA_VERSION:
            raise PromotionValidationError(
                "schema_version must be %d" % PROMOTION_SCHEMA_VERSION
            )
        if (
            not isinstance(self.provenance, tuple)
            or not self.provenance
            or len(self.provenance) > 32
        ):
            raise PromotionValidationError("provenance must contain stable references")
        if not all(isinstance(item, StableRef) for item in self.provenance):
            raise PromotionValidationError("provenance must contain StableRef values")
        if any(item.authority not in _PROVENANCE_AUTHORITIES for item in self.provenance):
            raise PromotionValidationError("provenance crosses the engineering authority boundary")
        serialized = [item.as_dict() for item in self.provenance]
        if len({_hash_json(item) for item in serialized}) != len(serialized):
            raise PromotionValidationError("provenance contains duplicate references")
        if any(_unsafe_reference(item) for item in serialized):
            raise PromotionValidationError("provenance contains secret-shaped or unbounded data")
        if not isinstance(self.review_authority, StableRef):
            raise PromotionValidationError("review_authority must be a StableRef")
        if self.review_authority.authority not in _REVIEW_AUTHORITIES:
            raise PromotionValidationError("review_authority is not an approved review owner")
        if _unsafe_reference(self.review_authority.as_dict()):
            raise PromotionValidationError(
                "review_authority contains secret-shaped or unbounded data"
            )
        for field_name in ("corrects", "supersedes"):
            values = getattr(self, field_name)
            if not isinstance(values, tuple) or any(
                not isinstance(value, str) or not _PROMOTION_ID.fullmatch(value)
                for value in values
            ):
                raise PromotionValidationError(
                    "%s must contain promotion ids" % field_name
                )
            if len(set(values)) != len(values):
                raise PromotionValidationError("%s contains duplicates" % field_name)
        if set(self.corrects) & set(self.supersedes):
            raise PromotionValidationError("corrects and supersedes must be distinct")
        if len(self.corrects) + len(self.supersedes) > MAX_HISTORICAL_RELATIONS:
            raise PromotionValidationError(
                "historical relations exceed the bounded promotion limit"
            )
        if self.promotion_id in set(self.corrects) | set(self.supersedes):
            raise PromotionValidationError("a promotion cannot reference itself")

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        stage_id: str,
        source_revision: str,
        idempotency_key: str,
        provenance: Sequence[StableRef],
        review_authority: StableRef,
        promotion_kind: str = PROMOTION_KIND,
        schema_version: int = PROMOTION_SCHEMA_VERSION,
        corrects: Sequence[str] = (),
        supersedes: Sequence[str] = (),
    ) -> "PromotionRequest":
        return cls(
            project_id=project_id,
            stage_id=stage_id,
            source_revision=source_revision,
            idempotency_key=idempotency_key,
            promotion_kind=promotion_kind,
            schema_version=schema_version,
            provenance=tuple(sorted(provenance, key=_ref_sort_key)),
            review_authority=review_authority,
            corrects=tuple(sorted(corrects)),
            supersedes=tuple(sorted(supersedes)),
        )

    @property
    def promotion_id(self) -> str:
        identity = {
            "idempotency_key": self.idempotency_key,
            "project_id": self.project_id,
            "promotion_kind": self.promotion_kind,
            "schema_version": self.schema_version,
            "source_revision": self.source_revision,
            "stage_id": self.stage_id,
        }
        return "promotion:" + _hash_json(identity)

    def request_digest(self, actor: str) -> str:
        value = {
            "actor": actor,
            "corrects": list(self.corrects),
            "idempotency_key": self.idempotency_key,
            "project_id": self.project_id,
            "promotion_id": self.promotion_id,
            "promotion_kind": self.promotion_kind,
            "provenance": [item.as_dict() for item in self.provenance],
            "review_authority": self.review_authority.as_dict(),
            "schema_version": self.schema_version,
            "source_revision": self.source_revision,
            "stage_id": self.stage_id,
            "supersedes": list(self.supersedes),
        }
        return "sha256:" + _hash_json(value)


@dataclass(frozen=True)
class PromotionEnvelope:
    request: PromotionRequest
    actor: str
    source: Dict[str, Any]
    source_digest: str
    envelope_digest: str
    case_content: str
    case_content_digest: str

    def cognee_case(self) -> CogneeCase:
        metadata = {
            "schema": "project-continuity.promotion.v1",
            "project_id": self.request.project_id,
            "stage_id": self.request.stage_id,
            "source_revision": self.request.source_revision,
            "promotion_id": self.request.promotion_id,
            "promotion_kind": self.request.promotion_kind,
            "envelope_digest": self.envelope_digest,
            "source_digest": self.source_digest,
            "case_content_digest": self.case_content_digest,
            "corrects": list(self.request.corrects),
            "supersedes": list(self.request.supersedes),
        }
        return CogneeCase(
            project_id=self.request.project_id,
            promotion_id=self.request.promotion_id,
            envelope_digest=self.envelope_digest,
            source_digest=self.source_digest,
            content=self.case_content,
            content_digest=self.case_content_digest,
            metadata=metadata,
        )

    def frozen_case(self) -> Tuple[str, str]:
        case = self.cognee_case()
        payload = _canonical_json(
            {
                "content": case.content,
                "metadata": case.metadata,
            }
        )
        return payload, "sha256:" + sha256(payload.encode("utf-8")).hexdigest()


class PromotionCoordinator:
    """Synchronously reconcile one explicit request; never scan for work."""

    def __init__(self, receipts: ReceiptStore, backend: CogneeBackend) -> None:
        self.receipts = receipts
        self.backend = backend

    async def promote(
        self,
        request: PromotionRequest,
        *,
        actor: str,
        read_stage: Callable[[str], Mapping[str, Any]],
    ) -> dict:
        request_digest = request.request_digest(actor)
        existing = self.receipts.find(request.project_id, request.idempotency_key)
        if existing is not None:
            if (
                existing.promotion_id != request.promotion_id
                or existing.request_digest != request_digest
                or existing.actor != actor
            ):
                raise IdempotencyConflict(
                    "idempotency key belongs to another promotion request"
                )
            if existing.state == "committed":
                return existing.receipt()
            await self._validate_relations(request)
            archived = await self.backend.lookup(request.project_id, request.promotion_id)
            if archived is not None:
                _verify_operation_record(existing, archived)
                return self.receipts.commit(
                    request.promotion_id,
                    envelope_digest=existing.envelope_digest,
                    backend_data_id=archived.data_id,
                ).receipt()
            case = _case_from_operation(existing)
            archived = await self.backend.upsert(case)
            verify_case_record(case, archived)
            _verify_operation_record(existing, archived)
            return self.receipts.commit(
                request.promotion_id,
                envelope_digest=existing.envelope_digest,
                backend_data_id=archived.data_id,
            ).receipt()

        stage = read_stage(request.stage_id)
        envelope = build_envelope(request, actor=actor, stage=stage)
        frozen_case_json, frozen_payload_digest = envelope.frozen_case()
        operation = new_prepared_operation(
            promotion_id=request.promotion_id,
            project_id=request.project_id,
            stage_id=request.stage_id,
            source_revision=request.source_revision,
            idempotency_key=request.idempotency_key,
            request_digest=request_digest,
            envelope_digest=envelope.envelope_digest,
            source_digest=envelope.source_digest,
            case_content_digest=envelope.case_content_digest,
            frozen_payload_digest=frozen_payload_digest,
            frozen_case_json=frozen_case_json,
            actor=actor,
        )
        prepared = self.receipts.prepare(operation)
        if prepared.state == "committed":
            return prepared.receipt()
        await self._validate_relations(request)
        archived = await self.backend.lookup(request.project_id, request.promotion_id)
        if archived is None:
            archived = await self.backend.upsert(envelope.cognee_case())
        verify_case_record(envelope.cognee_case(), archived)
        _verify_operation_record(prepared, archived)
        return self.receipts.commit(
            request.promotion_id,
            envelope_digest=envelope.envelope_digest,
            backend_data_id=archived.data_id,
        ).receipt()

    async def _validate_relations(self, request: PromotionRequest) -> None:
        for relation_id in request.corrects + request.supersedes:
            target = await self.backend.lookup(request.project_id, relation_id)
            if (
                target is None
                or not target.ready
                or target.project_id != request.project_id
                or target.promotion_id != relation_id
            ):
                raise PromotionValidationError(
                    "historical relation must resolve to a ready Case in this project"
                )


def build_envelope(
    request: PromotionRequest,
    *,
    actor: str,
    stage: Mapping[str, Any],
) -> PromotionEnvelope:
    if not isinstance(actor, str) or not actor or actor != actor.strip():
        raise PromotionValidationError("actor must be derived before promotion")
    if not isinstance(stage, Mapping):
        raise PromotionValidationError("source Stage is unavailable")
    if stage.get("revision") != request.source_revision:
        raise StaleSourceRevision("source Stage revision changed after review")
    if stage.get("id") not in (None, request.stage_id):
        raise PromotionValidationError("source Stage id does not match the request")
    title = stage.get("title", "")
    body = stage.get("body")
    metadata = stage.get("metadata", {})
    if not isinstance(title, str) or not isinstance(body, str) or not isinstance(metadata, Mapping):
        raise PromotionValidationError("source Stage projection is malformed")
    source = {
        "body": body,
        "metadata": dict(metadata),
        "revision": request.source_revision,
        "stage_id": request.stage_id,
        "title": title,
    }
    source_digest = "sha256:" + _hash_json(source)
    document = {
        "actor": actor,
        "corrects": list(request.corrects),
        "idempotency_key": request.idempotency_key,
        "project_id": request.project_id,
        "promotion_id": request.promotion_id,
        "promotion_kind": request.promotion_kind,
        "provenance": [item.as_dict() for item in request.provenance],
        "review_authority": request.review_authority.as_dict(),
        "schema_version": request.schema_version,
        "source": source,
        "source_digest": source_digest,
        "supersedes": list(request.supersedes),
    }
    envelope_digest = "sha256:" + _hash_json(document)
    content = _render_case(document, envelope_digest)
    content_digest = "sha256:" + sha256(content.encode("utf-8")).hexdigest()
    return PromotionEnvelope(
        request=request,
        actor=actor,
        source=source,
        source_digest=source_digest,
        envelope_digest=envelope_digest,
        case_content=content,
        case_content_digest=content_digest,
    )


def _render_case(document: Dict[str, Any], envelope_digest: str) -> str:
    source = document["source"]
    references = json.dumps(
        document["provenance"], ensure_ascii=False, sort_keys=True, indent=2
    )
    review = json.dumps(
        document["review_authority"], ensure_ascii=False, sort_keys=True, indent=2
    )
    return (
        "# Reviewed project engineering Case\n\n"
        "Promotion ID: {promotion_id}\n"
        "Project: {project_id}\n"
        "Source Stage: {stage_id}@{revision}\n"
        "Promotion kind: {kind}\n"
        "Envelope digest: {envelope_digest}\n"
        "Source digest: {source_digest}\n\n"
        "## Source Stage title\n\n{title}\n\n"
        "## Case path and evidence\n\n{body}\n\n"
        "## Review authority\n\n```json\n{review}\n```\n\n"
        "## Provenance references (references only; source systems retain authority)\n\n"
        "```json\n{references}\n```\n\n"
        "## Historical relations\n\nCorrects: {corrects}\nSupersedes: {supersedes}\n"
    ).format(
        promotion_id=document["promotion_id"],
        project_id=document["project_id"],
        stage_id=source["stage_id"],
        revision=source["revision"],
        kind=document["promotion_kind"],
        envelope_digest=envelope_digest,
        source_digest=document["source_digest"],
        title=source["title"],
        body=source["body"],
        review=review,
        references=references,
        corrects=json.dumps(document["corrects"], ensure_ascii=False),
        supersedes=json.dumps(document["supersedes"], ensure_ascii=False),
    )


def _verify_operation_record(
    operation: PromotionOperation, record: CogneeCaseRecord
) -> None:
    if (
        record.project_id != operation.project_id
        or record.promotion_id != operation.promotion_id
        or record.envelope_digest != operation.envelope_digest
        or record.source_digest != operation.source_digest
        or record.content_digest != operation.case_content_digest
        or not record.ready
    ):
        raise BackendIdentityConflict(
            "Cognee record does not match the prepared promotion receipt"
        )


def _case_from_operation(operation: PromotionOperation) -> CogneeCase:
    payload = operation.frozen_case_json
    if not isinstance(payload, str) or not payload:
        raise ReceiptError("prepared promotion is missing its frozen Case payload")
    digest = "sha256:" + sha256(payload.encode("utf-8")).hexdigest()
    if digest != operation.frozen_payload_digest:
        raise ReceiptError("prepared frozen Case payload digest changed")
    try:
        document = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise ReceiptError("prepared frozen Case payload is invalid JSON") from exc
    if not isinstance(document, dict) or _canonical_json(document) != payload:
        raise ReceiptError("prepared frozen Case payload is not canonical")
    content = document.get("content")
    metadata = document.get("metadata")
    if not isinstance(content, str) or not isinstance(metadata, dict):
        raise ReceiptError("prepared frozen Case payload is malformed")
    content_digest = "sha256:" + sha256(content.encode("utf-8")).hexdigest()
    if content_digest != operation.case_content_digest:
        raise ReceiptError("prepared frozen Case content digest changed")
    expected_metadata = {
        "project_id": operation.project_id,
        "promotion_id": operation.promotion_id,
        "envelope_digest": operation.envelope_digest,
        "source_digest": operation.source_digest,
        "case_content_digest": operation.case_content_digest,
    }
    if any(metadata.get(key) != value for key, value in expected_metadata.items()):
        raise ReceiptError("prepared frozen Case metadata changed")
    return CogneeCase(
        project_id=operation.project_id,
        promotion_id=operation.promotion_id,
        envelope_digest=operation.envelope_digest,
        source_digest=operation.source_digest,
        content=content,
        content_digest=operation.case_content_digest,
        metadata=metadata,
    )


def _hash_json(value: Any) -> str:
    payload = _canonical_json(value).encode("utf-8")
    return sha256(payload).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _ref_sort_key(ref: StableRef) -> Tuple[str, str, str, str]:
    return (ref.authority, ref.object_id, ref.version, ref.digest)


def validate_promotion_id(value: str) -> str:
    if not isinstance(value, str) or not _PROMOTION_ID.fullmatch(value):
        raise PromotionValidationError("promotion_id must be a canonical identity")
    return value


def _unsafe_reference(value: Dict[str, Any]) -> bool:
    return sanitize_evidence(
        value,
        max_depth=8,
        max_items=64,
        max_string=2_000,
    ) != value
