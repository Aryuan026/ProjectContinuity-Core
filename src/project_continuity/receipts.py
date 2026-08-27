"""Caller-driven SQLite receipts for explicit Cognee promotions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import closing
import os
from pathlib import Path
import sqlite3
from typing import Optional


class ReceiptError(RuntimeError):
    """The promotion receipt ledger could not preserve its contract."""


class IdempotencyConflict(ReceiptError):
    """One idempotency key was reused for a different frozen promotion."""


@dataclass(frozen=True)
class PromotionOperation:
    promotion_id: str
    project_id: str
    stage_id: str
    source_revision: str
    idempotency_key: str
    request_digest: str
    envelope_digest: str
    source_digest: str
    case_content_digest: str
    frozen_payload_digest: str
    frozen_case_json: Optional[str]
    actor: str
    state: str
    backend_data_id: Optional[str]
    prepared_at: str
    committed_at: Optional[str]

    def receipt(self) -> dict:
        return {
            "ok": self.state == "committed",
            "status": self.state,
            "promotion_id": self.promotion_id,
            "project_id": self.project_id,
            "stage_id": self.stage_id,
            "source_revision": self.source_revision,
            "idempotency_key": self.idempotency_key,
            "request_digest": self.request_digest,
            "envelope_digest": self.envelope_digest,
            "source_digest": self.source_digest,
            "case_content_digest": self.case_content_digest,
            "frozen_payload_digest": self.frozen_payload_digest,
            "actor": self.actor,
            "backend_data_id": self.backend_data_id,
            "prepared_at": self.prepared_at,
            "committed_at": self.committed_at,
        }


def promotion_receipt_path(state_root: Path) -> Path:
    return Path(state_root) / "promotion" / "receipts.sqlite3"


class ReceiptStore:
    """One small operation ledger; it is not a queue or a memory backend."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def find(self, project_id: str, idempotency_key: str) -> Optional[PromotionOperation]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM promotion_operations
                WHERE project_id = ? AND idempotency_key = ?
                """,
                (project_id, idempotency_key),
            ).fetchone()
        return _operation(row) if row is not None else None

    def prepare(self, operation: PromotionOperation) -> PromotionOperation:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT * FROM promotion_operations
                WHERE promotion_id = ?
                   OR (project_id = ? AND idempotency_key = ?)
                """,
                (
                    operation.promotion_id,
                    operation.project_id,
                    operation.idempotency_key,
                ),
            ).fetchall()
            if len(rows) > 1:
                connection.rollback()
                raise IdempotencyConflict(
                    "promotion id and idempotency key resolve to different operations"
                )
            row = rows[0] if rows else None
            if row is None:
                connection.execute(
                    """
                    INSERT INTO promotion_operations (
                        promotion_id, project_id, stage_id, source_revision,
                        idempotency_key, request_digest, envelope_digest, source_digest,
                        case_content_digest, frozen_payload_digest, frozen_case_json,
                        actor, state, backend_data_id,
                        prepared_at, committed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', NULL, ?, NULL)
                    """,
                    (
                        operation.promotion_id,
                        operation.project_id,
                        operation.stage_id,
                        operation.source_revision,
                        operation.idempotency_key,
                        operation.request_digest,
                        operation.envelope_digest,
                        operation.source_digest,
                        operation.case_content_digest,
                        operation.frozen_payload_digest,
                        operation.frozen_case_json,
                        operation.actor,
                        operation.prepared_at,
                    ),
                )
                connection.commit()
                return operation
            existing = _operation(row)
            _same_operation(existing, operation)
            connection.commit()
            return existing

    def commit(
        self,
        promotion_id: str,
        *,
        envelope_digest: str,
        backend_data_id: str,
    ) -> PromotionOperation:
        committed_at = _now()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM promotion_operations WHERE promotion_id = ?",
                (promotion_id,),
            ).fetchone()
            if row is None:
                connection.rollback()
                raise ReceiptError("promotion was not prepared")
            existing = _operation(row)
            if existing.envelope_digest != envelope_digest:
                connection.rollback()
                raise IdempotencyConflict("prepared promotion digest changed")
            if existing.state == "committed":
                if existing.backend_data_id != backend_data_id:
                    connection.rollback()
                    raise ReceiptError("committed backend identity changed")
                connection.commit()
                return existing
            if existing.state != "prepared":
                connection.rollback()
                raise ReceiptError("invalid promotion receipt state")
            connection.execute(
                """
                UPDATE promotion_operations
                SET state = 'committed', backend_data_id = ?, committed_at = ?,
                    frozen_case_json = NULL
                WHERE promotion_id = ? AND state = 'prepared'
                """,
                (backend_data_id, committed_at, promotion_id),
            )
            row = connection.execute(
                "SELECT * FROM promotion_operations WHERE promotion_id = ?",
                (promotion_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise ReceiptError("committed promotion disappeared")
        return _operation(row)

    def _connect(self) -> sqlite3.Connection:
        directory = self.path.parent
        if directory.is_symlink() or self.path.is_symlink():
            raise ReceiptError("promotion receipt path must not be a symlink")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not directory.is_dir():
            raise ReceiptError("promotion receipt directory is not a directory")
        os.chmod(directory, 0o700)
        if self.path.exists() and not self.path.is_file():
            raise ReceiptError("promotion receipt database is not a regular file")
        connection = sqlite3.connect(str(self.path), timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS promotion_operations (
                promotion_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                stage_id TEXT NOT NULL,
                source_revision TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                request_digest TEXT NOT NULL,
                envelope_digest TEXT NOT NULL,
                source_digest TEXT NOT NULL,
                case_content_digest TEXT NOT NULL,
                frozen_payload_digest TEXT NOT NULL,
                frozen_case_json TEXT,
                actor TEXT NOT NULL,
                state TEXT NOT NULL CHECK (state IN ('prepared', 'committed')),
                backend_data_id TEXT,
                prepared_at TEXT NOT NULL,
                committed_at TEXT,
                CHECK (
                    (state = 'prepared' AND frozen_case_json IS NOT NULL)
                    OR (state = 'committed' AND frozen_case_json IS NULL)
                ),
                UNIQUE (project_id, idempotency_key)
            )
            """
        )
        os.chmod(self.path, 0o600)
        return connection


def new_prepared_operation(
    *,
    promotion_id: str,
    project_id: str,
    stage_id: str,
    source_revision: str,
    idempotency_key: str,
    request_digest: str,
    envelope_digest: str,
    source_digest: str,
    case_content_digest: str,
    frozen_payload_digest: str,
    frozen_case_json: str,
    actor: str,
) -> PromotionOperation:
    return PromotionOperation(
        promotion_id=promotion_id,
        project_id=project_id,
        stage_id=stage_id,
        source_revision=source_revision,
        idempotency_key=idempotency_key,
        request_digest=request_digest,
        envelope_digest=envelope_digest,
        source_digest=source_digest,
        case_content_digest=case_content_digest,
        frozen_payload_digest=frozen_payload_digest,
        frozen_case_json=frozen_case_json,
        actor=actor,
        state="prepared",
        backend_data_id=None,
        prepared_at=_now(),
        committed_at=None,
    )


def _operation(row: sqlite3.Row) -> PromotionOperation:
    return PromotionOperation(**dict(row))


def _same_operation(existing: PromotionOperation, requested: PromotionOperation) -> None:
    fields = (
        "promotion_id",
        "project_id",
        "stage_id",
        "source_revision",
        "idempotency_key",
        "request_digest",
        "envelope_digest",
        "source_digest",
        "case_content_digest",
        "frozen_payload_digest",
        "actor",
    )
    if any(getattr(existing, field) != getattr(requested, field) for field in fields):
        raise IdempotencyConflict("idempotency key belongs to another promotion envelope")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
