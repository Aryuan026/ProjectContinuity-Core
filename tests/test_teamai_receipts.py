import json

import pytest

from project_continuity.teamai_receipts import (
    TeamAIReceiptError,
    TeamAIReceiptStore,
    authority_request_digest,
    public_teamai_receipt,
)


def test_teamai_receipt_prepare_commit_and_exact_replay(config) -> None:
    store = TeamAIReceiptStore(config.paths.state_root)
    digest = "sha256:" + "a" * 64
    prepared, created = store.prepare(
        actor="writer-agent",
        project_id="alpha",
        request_digest=digest,
        source_revision="b" * 40,
    )
    replay, replay_created = store.prepare(
        actor="writer-agent",
        project_id="alpha",
        request_digest=digest,
        source_revision="b" * 40,
    )
    committed = store.commit(
        replay,
        branch="teamai/push/writer-agent/20260905-120000",
        head_revision="c" * 40,
        pull_request=11,
        pull_request_url="https://github.com/example/alpha/pull/11",
        review_state="open",
    )
    final, final_created = store.prepare(
        actor="writer-agent",
        project_id="alpha",
        request_digest=digest,
        source_revision="b" * 40,
    )

    assert created is True
    assert replay_created is final_created is False
    assert prepared == replay
    assert final == committed
    assert public_teamai_receipt(final, changed=False)["operation_id"] == (
        "authority:" + "a" * 64
    )
    path = (
        config.paths.state_root
        / "authority/teamai/alpha"
        / (("a" * 64) + ".json")
    )
    assert (path.stat().st_mode & 0o777) == 0o600
    assert "writer-agent@" not in json.dumps(final)


def test_teamai_receipt_rejects_identity_drift_and_symlink(config) -> None:
    store = TeamAIReceiptStore(config.paths.state_root)
    digest = "sha256:" + "d" * 64
    store.prepare(
        actor="writer-agent",
        project_id="alpha",
        request_digest=digest,
        source_revision="e" * 40,
    )
    with pytest.raises(TeamAIReceiptError, match="teamai_receipt_conflict"):
        store.prepare(
            actor="other-agent",
            project_id="alpha",
            request_digest=digest,
            source_revision="e" * 40,
        )

    path = (
        config.paths.state_root
        / "authority/teamai/alpha"
        / (("d" * 64) + ".json")
    )
    path.unlink()
    path.symlink_to(config.paths.state_root / "outside.json")
    with pytest.raises(TeamAIReceiptError, match="teamai_receipt_unsafe"):
        store.prepare(
            actor="writer-agent",
            project_id="alpha",
            request_digest=digest,
            source_revision="e" * 40,
        )


def test_authority_request_digest_is_canonical_across_parameter_order() -> None:
    first = authority_request_digest(
        principal_id="writer-client",
        project_id="alpha",
        target="collaboration",
        operation="contribute",
        parameters={"title": "Exact", "body": "Body"},
        expected_revision="a" * 40,
    )
    second = authority_request_digest(
        principal_id="writer-client",
        project_id="alpha",
        target="collaboration",
        operation="contribute",
        parameters={"body": "Body", "title": "Exact"},
        expected_revision="a" * 40,
    )

    assert first == second
    assert first.startswith("sha256:")
