"""Durable request receipts for TeamAI collaboration contributions."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Dict, Mapping, Tuple


SCHEMA_VERSION = 1
MAX_RECEIPT_BYTES = 64 * 1024
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_PROJECT = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")


class TeamAIReceiptError(RuntimeError):
    """A collaboration receipt could not preserve exact replay identity."""


class TeamAIReceiptStore:
    def __init__(self, state_root: Path) -> None:
        self.root = Path(state_root) / "authority/teamai"

    def prepare(
        self,
        *,
        actor: str,
        project_id: str,
        request_digest: str,
        source_revision: str,
    ) -> Tuple[Dict[str, Any], bool]:
        expected = {
            "actor": actor,
            "branch": None,
            "committed_at": None,
            "head_revision": None,
            "operation": "contribute",
            "operation_id": _operation_id(request_digest),
            "prepared_at": _now(),
            "project_id": project_id,
            "pull_request": None,
            "pull_request_url": None,
            "request_digest": request_digest,
            "review_state": None,
            "schema_version": SCHEMA_VERSION,
            "source_revision": source_revision,
            "state": "prepared",
        }
        path = self._path(project_id, request_digest)
        if os.path.lexists(str(path)):
            existing = self._read(path, project_id, request_digest)
            for field in (
                "actor",
                "operation",
                "operation_id",
                "project_id",
                "request_digest",
                "source_revision",
            ):
                if existing[field] != expected[field]:
                    raise TeamAIReceiptError("teamai_receipt_conflict")
            return existing, False
        self._write(path, expected)
        return expected, True

    def commit(
        self,
        receipt: Mapping[str, Any],
        *,
        branch: str,
        head_revision: str,
        pull_request: int,
        pull_request_url: str,
        review_state: str,
    ) -> Dict[str, Any]:
        path = self._path(receipt["project_id"], receipt["request_digest"])
        current = self._read(
            path, receipt["project_id"], receipt["request_digest"]
        )
        if current["state"] == "committed":
            return current
        committed = {
            **current,
            "branch": branch,
            "committed_at": _now(),
            "head_revision": head_revision,
            "pull_request": pull_request,
            "pull_request_url": pull_request_url,
            "review_state": review_state,
            "state": "committed",
        }
        self._write(path, committed)
        return committed

    def _path(self, project_id: str, request_digest: str) -> Path:
        if not _PROJECT.fullmatch(project_id) or not _DIGEST.fullmatch(request_digest):
            raise TeamAIReceiptError("teamai_receipt_identity_invalid")
        project_root = self.root / project_id
        _private_directory(project_root)
        return project_root / (request_digest.removeprefix("sha256:") + ".json")

    def _read(
        self, path: Path, project_id: str, request_digest: str
    ) -> Dict[str, Any]:
        try:
            item = os.lstat(path)
            if (
                not stat.S_ISREG(item.st_mode)
                or item.st_uid != os.getuid()
                or item.st_mode & 0o077
                or item.st_size < 1
                or item.st_size > MAX_RECEIPT_BYTES
            ):
                raise TeamAIReceiptError("teamai_receipt_unsafe")
            value = json.loads(path.read_text(encoding="utf-8"))
        except TeamAIReceiptError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise TeamAIReceiptError("teamai_receipt_unreadable") from exc
        _validate(value, project_id, request_digest)
        return value

    def _write(self, path: Path, receipt: Mapping[str, Any]) -> None:
        _validate(receipt, receipt["project_id"], receipt["request_digest"])
        payload = (
            json.dumps(receipt, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            + "\n"
        ).encode("utf-8")
        descriptor, name = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            directory = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError as exc:
            temporary.unlink(missing_ok=True)
            raise TeamAIReceiptError("teamai_receipt_write_failed") from exc


def public_teamai_receipt(receipt: Mapping[str, Any], *, changed: bool) -> Dict[str, Any]:
    if receipt["state"] != "committed":
        raise TeamAIReceiptError("teamai_receipt_not_committed")
    return {
        "actor": receipt["actor"],
        "branch": receipt["branch"],
        "changed": changed,
        "head_revision": receipt["head_revision"],
        "ok": True,
        "operation": "contribute",
        "operation_id": receipt["operation_id"],
        "pull_request": receipt["pull_request"],
        "pull_request_url": receipt["pull_request_url"],
        "review_state": receipt["review_state"],
        "source_revision": receipt["source_revision"],
    }


def authority_request_digest(
    *,
    principal_id: str,
    project_id: str,
    target: str,
    operation: str,
    parameters: Mapping[str, Any],
    expected_revision: str,
) -> str:
    """Return the operation identity shared by transport and durable receipt."""

    payload = json.dumps(
        {
            "expected_revision": expected_revision,
            "operation": operation,
            "parameters": dict(parameters),
            "principal_id": principal_id,
            "project_id": project_id,
            "target": target,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + sha256(payload).hexdigest()


def _operation_id(request_digest: str) -> str:
    if not _DIGEST.fullmatch(request_digest):
        raise TeamAIReceiptError("teamai_receipt_identity_invalid")
    return "authority:" + request_digest.removeprefix("sha256:")


def _validate(value: Any, project_id: str, request_digest: str) -> None:
    fields = {
        "actor",
        "branch",
        "committed_at",
        "head_revision",
        "operation",
        "operation_id",
        "prepared_at",
        "project_id",
        "pull_request",
        "pull_request_url",
        "request_digest",
        "review_state",
        "schema_version",
        "source_revision",
        "state",
    }
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or value["schema_version"] != SCHEMA_VERSION
        or value["operation"] != "contribute"
        or value["project_id"] != project_id
        or value["request_digest"] != request_digest
        or value["operation_id"] != _operation_id(request_digest)
        or value["state"] not in {"prepared", "committed"}
        or not isinstance(value["actor"], str)
        or not value["actor"]
        or not isinstance(value["prepared_at"], str)
        or not _COMMIT.fullmatch(value["source_revision"])
    ):
        raise TeamAIReceiptError("teamai_receipt_malformed")
    committed_fields = (
        "branch",
        "committed_at",
        "head_revision",
        "pull_request",
        "pull_request_url",
        "review_state",
    )
    if value["state"] == "prepared":
        if any(value[field] is not None for field in committed_fields):
            raise TeamAIReceiptError("teamai_receipt_malformed")
        return
    if (
        not isinstance(value["branch"], str)
        or not value["branch"]
        or not isinstance(value["committed_at"], str)
        or not _COMMIT.fullmatch(value["head_revision"] or "")
        or type(value["pull_request"]) is not int
        or value["pull_request"] < 1
        or not isinstance(value["pull_request_url"], str)
        or not isinstance(value["review_state"], str)
        or not value["review_state"]
    ):
        raise TeamAIReceiptError("teamai_receipt_malformed")


def _private_directory(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise TeamAIReceiptError("teamai_receipt_path_unsafe")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    item = path.stat()
    if not stat.S_ISDIR(item.st_mode) or item.st_uid != os.getuid() or item.st_mode & 0o077:
        raise TeamAIReceiptError("teamai_receipt_path_unsafe")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
