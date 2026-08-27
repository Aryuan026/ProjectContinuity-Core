from __future__ import annotations

import json
from pathlib import Path

import pytest

from project_continuity.acl import StageAccessError
from project_continuity.auth import AuthorizationError
from project_continuity.front import (
    AUTOMATIC_SCHEDULE_ENABLED,
    EXTERNAL_LLM_MAINTAIN_ENABLED,
    FRONT_TOOLS,
    CognitionFront,
)
from project_continuity.turritopsis_adapter import (
    EvidenceRejected,
    StoreBoundaryError,
    TURRITOPSIS_COMMIT,
    TURRITOPSIS_VERSION,
    TurritopsisAdapter,
    TurritopsisUnavailable,
    project_store_path,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _stage(stage_id: str, title: str, body: str) -> dict:
    return {"id": stage_id, "title": title, "body": body}


def _write_store(config, project_id: str, *, beta: bool = False) -> Path:
    path = project_store_path(config.paths.data_root, project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if beta:
        document = {
            "title": "Beta Project",
            "subtitle": "Independent Store",
            "version": 1,
            "currents": [{
                "id": "beta",
                "name": "Beta",
                "blurb": "Second project",
                "stages": [_stage(
                    "beta.handoff",
                    "Beta handoff",
                    "# Beta handoff\n\nSummary: Beta stays independent.\n"
                    "Status: current\nAuthority: beta current work\n",
                )],
            }],
        }
    else:
        document = {
            "title": "Alpha Project",
            "subtitle": "Seven-Stage canary",
            "version": 1,
            "currents": [
                {
                    "id": "project",
                    "name": "Project",
                    "blurb": "Current cognition and history",
                    "stages": [
                        _stage(
                            "project.handoff",
                            "Current work and handoff",
                            "# Current work\n\n"
                            "Purpose: Tell the next Agent where work stopped.\n"
                            "Search hints: 交接 下一步 blocker handoff\n"
                            "Summary: 下一步验证 authenticated front。\n"
                            "Verified: 2026-08-25 by canary\n"
                            "Status: current\n"
                            "Authority: current work and next action\n\n"
                            "## Next\n\nRun the two-client canary.\n",
                        ),
                        _stage(
                            "project.timeline",
                            "Historical decisions",
                            "# Timeline\n\nSearch hints: 旧方案 历史\n"
                            "Summary: The old shared JSON design was retired.\n"
                            "Status: historical\nAuthority: historical explanation\n",
                        ),
                        _stage(
                            "project.sensitive",
                            "Redaction canary",
                            "# Redaction\n\nSummary: A fixture exercises output hygiene.\n"
                            "Status: current\nAuthority: redaction canary\n\n"
                            "OPENAI_API_KEY=sk-test-not-a-real-secret\n",
                        ),
                    ],
                },
                {
                    "id": "architecture",
                    "name": "Architecture",
                    "blurb": "System shape",
                    "stages": [
                        _stage(
                            "architecture.front",
                            "Front topology",
                            "# Front\n\nSearch hints: 认知前台 多项目\n"
                            "Summary: One front routes independent Stores.\n"
                            "Status: current\nAuthority: cognition-front topology\n",
                        ),
                        _stage(
                            "architecture.authority",
                            "Authority map",
                            "# Authority\n\nSearch hints: 权威 owner\n"
                            "Summary: Turritopsis owns current cognition only.\n"
                            "Status: current\nAuthority: project cognition boundary\n",
                        ),
                    ],
                },
                {
                    "id": "operations",
                    "name": "Operations",
                    "blurb": "Operate and recover",
                    "stages": [_stage(
                        "operations.restore",
                        "Backup and restore",
                        "# Restore\n\nSearch hints: 备份 恢复 rollback\n"
                        "Summary: Rolling backups are runtime recovery material.\n"
                        "Status: current\nAuthority: Store recovery\n",
                    )],
                },
                {
                    "id": "verification",
                    "name": "Verification",
                    "blurb": "Evidence and gates",
                    "stages": [_stage(
                        "verification.f3",
                        "Front acceptance",
                        "# Front canary\n\nSearch hints: canary 验收 ACL CAS\n"
                        "Summary: acceptance needs two authenticated clients.\n"
                        "Status: current\nAuthority: front verification\n",
                    )],
                },
            ],
        }
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def front(config):
    _write_store(config, "alpha")
    _write_store(config, "beta", beta=True)
    return CognitionFront(config)


def test_exact_donor_coordinate_and_front_tool_surface_are_frozen() -> None:
    assert TURRITOPSIS_COMMIT == "fd94c75f362260abb81ddd02296f14dc22350e73"
    assert TURRITOPSIS_VERSION == "0.2.0"
    assert FRONT_TOOLS == ("list", "search", "get", "update", "promote")
    assert EXTERNAL_LLM_MAINTAIN_ENABLED is False
    assert AUTOMATIC_SCHEDULE_ENABLED is False
    package = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert TURRITOPSIS_COMMIT in package


def test_every_active_donor_entry_rejects_runtime_version_drift(
    config, tmp_path, monkeypatch
) -> None:
    data_path = _write_store(config, "alpha")
    adapter = TurritopsisAdapter(
        project_id="alpha",
        data_root=config.paths.data_root,
        data_path=data_path,
    )
    monkeypatch.setattr(
        "project_continuity.turritopsis_adapter.importlib.metadata.version",
        lambda _name: "0.1.0",
    )
    with pytest.raises(TurritopsisUnavailable, match="version mismatch"):
        adapter.list_stages()
    with pytest.raises(TurritopsisUnavailable, match="version mismatch"):
        adapter.scan_preview(tmp_path)


def test_repository_aware_maintenance_stays_held_without_real_project_root(
    config,
) -> None:
    data_path = _write_store(config, "alpha")
    adapter = TurritopsisAdapter(
        project_id="alpha",
        data_root=config.paths.data_root,
        data_path=data_path,
    )
    with pytest.raises(TurritopsisUnavailable, match="maintenance is HOLD"):
        adapter.maintenance_preview()


def test_one_front_routes_two_independent_project_stores(front, config) -> None:
    alpha = front.list_stages("writer-client", "alpha")
    beta = front.list_stages("writer-client", "beta")
    assert alpha["title"] == "Alpha Project"
    assert beta["title"] == "Beta Project"
    assert project_store_path(config.paths.data_root, "alpha") != project_store_path(
        config.paths.data_root, "beta"
    )

    before = front.get_stage("writer-client", "alpha", "project.handoff")
    result = front.update_stage(
        "writer-client",
        "alpha",
        "project.handoff",
        before["body"] + "\nAlpha advanced.\n",
        expected_revision=before["revision"],
    )
    assert result["changed"] is True
    assert "Alpha advanced" in front.get_stage(
        "writer-client", "alpha", "project.handoff"
    )["body"]
    assert "Beta stays independent" in front.get_stage(
        "writer-client", "beta", "beta.handoff"
    )["body"]


def test_two_authenticated_clients_share_cas_and_derived_actor(front, config) -> None:
    first = front.get_stage("writer-client", "alpha", "project.handoff")
    write = front.update_stage(
        "promoter-client",
        "alpha",
        "project.handoff",
        first["body"] + "\nSecond client wrote this.\n",
        expected_revision=first["revision"],
    )
    assert write["actor"] == "promoter-agent"

    stale = front.update_stage(
        "writer-client",
        "alpha",
        "project.handoff",
        "stale replacement",
        expected_revision=first["revision"],
    )
    assert stale["conflict"] is True
    assert "Second client wrote this" in stale["current_stage"]["body"]

    data_path = project_store_path(config.paths.data_root, "alpha")
    records = [
        json.loads(line)
        for line in (data_path.parent / "changelog.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert records[-1]["actor"] == "promoter-agent"
    backups = list((data_path.parent / "backups").glob("stages-*.json"))
    assert backups
    backup = json.loads(backups[-1].read_text(encoding="utf-8"))
    backup_body = backup["currents"][0]["stages"][0]["body"]
    assert backup_body == first["body"]


def test_handoff_history_and_chinese_search_retain_donor_behavior(front) -> None:
    handoff = front.search_stages(
        "reader-client", "alpha", "下一步怎么交接"
    )["results"][0]
    assert handoff["stage_id"] == "project.handoff"
    assert handoff["status"] == "current"

    history = front.search_stages(
        "reader-client", "alpha", "旧方案 历史"
    )["results"][0]
    assert history["stage_id"] == "project.timeline"
    assert history["status"] == "historical"
    assert front.get_stage(
        "reader-client", "alpha", "project.handoff"
    )["metadata"]["authority"] == "current work and next action"


def test_project_role_and_stage_scope_fail_closed(front) -> None:
    with pytest.raises(AuthorizationError, match="cannot use update"):
        current = front.get_stage("reader-client", "alpha", "project.handoff")
        front.update_stage(
            "reader-client",
            "alpha",
            "project.handoff",
            current["body"],
            expected_revision=current["revision"],
        )
    with pytest.raises(AuthorizationError, match="cannot use update"):
        current = front.get_stage("writer-client", "beta", "beta.handoff")
        front.update_stage(
            "writer-client",
            "beta",
            "beta.handoff",
            current["body"],
            expected_revision=current["revision"],
        )
    with pytest.raises(StageAccessError, match="not available in project"):
        front.get_stage("writer-client", "alpha", "beta.handoff")
    with pytest.raises(StageAccessError, match="not available in project"):
        front.search_stages(
            "writer-client",
            "alpha",
            "Beta",
            match="exact",
            stage_id="beta.handoff",
        )
    with pytest.raises(AuthorizationError, match="no role"):
        front.get_stage("reader-client", "beta", "beta.handoff")


def test_actor_claim_and_blind_write_are_rejected(front) -> None:
    stage = front.get_stage("writer-client", "alpha", "project.handoff")
    with pytest.raises(AuthorizationError, match="derived"):
        front.update_stage(
            "writer-client",
            "alpha",
            "project.handoff",
            stage["body"],
            expected_revision=stage["revision"],
            claimed_actor="forged-actor",
        )
    with pytest.raises(ValueError, match="expected_revision"):
        front.update_stage(
            "writer-client",
            "alpha",
            "project.handoff",
            stage["body"],
            expected_revision="",
        )


def test_front_redacts_existing_secret_and_rejects_new_secret(front) -> None:
    stage = front.get_stage(
        "writer-client", "alpha", "project.sensitive"
    )
    assert "sk-test-not-a-real-secret" not in stage["body"]
    assert "[REDACTED]" in stage["body"]

    handoff = front.get_stage("writer-client", "alpha", "project.handoff")
    with pytest.raises(EvidenceRejected, match="credential-shaped"):
        front.update_stage(
            "writer-client",
            "alpha",
            "project.handoff",
            handoff["body"] + "\nGITHUB_TOKEN=ghp_test_not_real\n",
            expected_revision=handoff["revision"],
        )
    assert front.get_stage(
        "writer-client", "alpha", "project.handoff"
    )["revision"] == handoff["revision"]


def test_f3_v1_rejects_append_before_any_donor_write(config) -> None:
    data_path = _write_store(config, "alpha")
    document = json.loads(data_path.read_text(encoding="utf-8"))
    document["currents"][0]["stages"][0]["body"] = "a" * 60_000
    data_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    before = data_path.read_bytes()
    front = CognitionFront(config)
    stage = front.get_stage("writer-client", "alpha", "project.handoff")

    with pytest.raises(ValueError, match="replace mode only"):
        front.update_stage(
            "writer-client",
            "alpha",
            "project.handoff",
            "b" * 60_000,
            expected_revision=stage["revision"],
            mode="append",
        )

    assert data_path.read_bytes() == before
    assert not (data_path.parent / "stages.json.lock").exists()
    assert not (data_path.parent / "backups").exists()
    assert not (data_path.parent / "changelog.jsonl").exists()


def test_scan_sanitizes_and_excludes_before_donor_transformation(
    config, tmp_path
) -> None:
    data_path = _write_store(config, "alpha")
    project = tmp_path / "project"
    project.mkdir()
    (project / "README.md").write_text(
        "# Demo\nOPENAI_API_KEY=sk-test-not-a-real-secret\n",
        encoding="utf-8",
    )
    (project / "alpha.py").write_text(
        'API_TOKEN = "alpha-secret-123456"\n', encoding="utf-8"
    )
    (project / "beta.py").write_text(
        'API_TOKEN = "alpha-secret-654321"\n', encoding="utf-8"
    )
    excluded = project / "graphify-out"
    excluded.mkdir()
    (excluded / "poison.py").write_text(
        'API_TOKEN = "alpha-secret-999999"\n', encoding="utf-8"
    )

    from turritopsis.init_scan import scan_anomalies

    raw_blob = json.dumps(scan_anomalies(project), ensure_ascii=False)
    assert "constant_forked" in raw_blob
    assert "alpha-secret-123456" in raw_blob
    assert "graphify-out/poison.py" in raw_blob

    adapter = TurritopsisAdapter(
        project_id="alpha",
        data_root=config.paths.data_root,
        data_path=data_path,
    )
    scan = adapter.scan_preview(project)
    scan_blob = json.dumps(scan, ensure_ascii=False)
    assert "sk-test-not-a-real-secret" not in scan_blob
    assert "alpha-secret-123456" not in scan_blob
    assert "alpha-secret-654321" not in scan_blob
    assert "alpha-secret-999999" not in scan_blob
    assert "graphify-out" not in scan_blob
    assert "[REDACTED]" in scan_blob


@pytest.mark.parametrize(
    ("relative", "target_is_directory"),
    [
        ("backups", True),
        ("changelog.jsonl", False),
        ("stages.json.lock", False),
    ],
)
@pytest.mark.parametrize("broken", [False, True])
def test_donor_write_sidecar_symlink_fails_before_external_mutation(
    config, tmp_path, relative, target_is_directory, broken
) -> None:
    data_path = _write_store(config, "alpha")
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / (relative.replace("/", "-") + "-target")
    if not broken:
        if target_is_directory:
            target.mkdir()
            (target / "sentinel").write_bytes(b"unchanged-directory")
        else:
            target.write_bytes(b"unchanged-file")
    link = data_path.parent / relative
    link.symlink_to(target, target_is_directory=target_is_directory)
    before_store = data_path.read_bytes()
    before_outside = {
        path.relative_to(outside).as_posix(): path.read_bytes()
        for path in outside.rglob("*")
        if path.is_file()
    }
    front = CognitionFront(config)
    stage = front.get_stage("writer-client", "alpha", "project.handoff")

    with pytest.raises(StoreBoundaryError, match="write path"):
        front.update_stage(
            "writer-client",
            "alpha",
            "project.handoff",
            stage["body"] + "\nblocked write\n",
            expected_revision=stage["revision"],
        )

    assert data_path.read_bytes() == before_store
    assert {
        path.relative_to(outside).as_posix(): path.read_bytes()
        for path in outside.rglob("*")
        if path.is_file()
    } == before_outside
    if broken:
        assert not target.exists()


def test_missing_store_is_real_outage_with_no_shadow_write(config) -> None:
    front = CognitionFront(config)
    expected = project_store_path(config.paths.data_root, "alpha")
    with pytest.raises(TurritopsisUnavailable) as caught:
        front.list_stages("writer-client", "alpha")
    assert str(config.paths.data_root) not in str(caught.value)
    with pytest.raises(TurritopsisUnavailable):
        front.update_stage(
            "writer-client",
            "alpha",
            "project.handoff",
            "must not be queued",
            expected_revision="0" * 16,
        )
    assert not expected.exists()
    assert not (expected.parent / "changelog.jsonl").exists()
    assert not (expected.parent / "backups").exists()
    assert {path.name for path in expected.parent.iterdir()} <= {
        "stages.json.lock"
    }


def test_parent_symlink_cannot_alias_another_project_store(config, tmp_path) -> None:
    beta = _write_store(config, "beta", beta=True)
    alpha_parent = project_store_path(config.paths.data_root, "alpha").parents[1]
    alpha_parent.parent.mkdir(parents=True, exist_ok=True)
    alpha_parent.symlink_to(beta.parents[1], target_is_directory=True)
    with pytest.raises(StoreBoundaryError, match="alias"):
        front = CognitionFront(config)
        front.list_stages("writer-client", "alpha")


@pytest.mark.parametrize(
    "stage_id",
    ["", " project.handoff", "project/handoff", "project\\handoff", "x\x00y"],
)
def test_stage_identifier_is_bounded_and_path_opaque(front, stage_id) -> None:
    with pytest.raises(StageAccessError, match="opaque identifier"):
        front.get_stage("writer-client", "alpha", stage_id)
